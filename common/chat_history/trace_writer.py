# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, List

logger = logging.getLogger(__name__)


DEFAULT_MAX_QUEUE = 2048

DEFAULT_WORKERS = 4

try:  
    from common.metrics.prometheus_metrics import metrics as _metrics
except Exception: 
    _metrics = None


def _record(outcome: str) -> None:
    if _metrics is not None:
        try:
            _metrics.chat_trace_write_total.labels(outcome=outcome).inc()
        except Exception:  
            pass


class TraceWriter:
    """Drains trace writes on a pool of background worker threads.

    ``submit`` never blocks and never raises so the response path is never
    slowed by a slow database. Persistence is therefore best-effort: a burst
    that outruns the workers past the bounded queue is dropped rather than
    buffered without limit (which would risk memory) or blocked on (which
    would add latency). Drops are counted and metered so they are observable
    rather than silent.
    """

    def __init__(
        self,
        max_queue: int = DEFAULT_MAX_QUEUE,
        num_workers: int = DEFAULT_WORKERS,
    ):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._threads: List[threading.Thread] = []
        self._num_workers = max(1, int(num_workers))
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stopping = threading.Event()
        self._submitted = 0
        self._written = 0
        self._dropped = 0
        self._failed = 0

    def _ensure_workers(self) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            if len(self._threads) >= self._num_workers:
                return
            self._stopping.clear()
            while len(self._threads) < self._num_workers:
                thread = threading.Thread(
                    target=self._drain, name="chat-trace-writer", daemon=True
                )
                thread.start()
                self._threads.append(thread)

    def _drain(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                job()
                with self._stats_lock:
                    self._written += 1
                _record("written")
            except Exception:
                with self._stats_lock:
                    self._failed += 1
                _record("failed")
                logger.warning("Trace write failed", exc_info=True)
            finally:
                self._queue.task_done()

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue *job*. Returns False if it was dropped.

        Never raises and never blocks: the caller is on the response path.
        """
        self._ensure_workers()
        with self._stats_lock:
            self._submitted += 1
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            with self._stats_lock:
                self._dropped += 1
                dropped_total = self._dropped
            _record("dropped")
            logger.warning(
                "Trace queue full (%d); dropping trace. dropped_total=%d",
                self._queue.maxsize,
                dropped_total,
            )
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains. For tests and shutdown."""
        deadline = threading.Event()
        waiter = threading.Thread(target=lambda: (self._queue.join(), deadline.set()))
        waiter.daemon = True
        waiter.start()
        return deadline.wait(timeout)

    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "queued": self._queue.qsize(),
        }

trace_writer = TraceWriter()
