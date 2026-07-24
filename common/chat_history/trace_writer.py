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
import os
import queue
import threading
import time
from typing import Callable, List

logger = logging.getLogger(__name__)


DEFAULT_MAX_QUEUE = 2048

DEFAULT_WORKERS = 4

DEFAULT_MAX_RETRIES = 2

DEFAULT_RETRY_BACKOFF = 0.5

try:
    from common.metrics.prometheus_metrics import metrics as _metrics
except Exception:
    _metrics = None


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read an int (>= minimum) from the environment, falling back on bad input."""
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Read a non-negative float from the environment, falling back on bad input."""
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _record(outcome: str) -> None:
    if _metrics is not None:
        try:
            _metrics.chat_trace_write_total.labels(outcome=outcome).inc()
        except Exception:  
            pass


class TraceWriter:
    """Drains trace writes on a pool of background worker threads.

    ``submit`` never blocks or raises, so the response path is never slowed by
    a slow database. Traces are best-effort observability data: transient
    failures are retried (idempotent, keyed on message_id), a full queue drops
    rather than block or grow unbounded, and ``flush()`` drains on graceful
    shutdown. Every drop/failure is metered on
    ``chat_trace_write_total{outcome=...}``.
    """

    def __init__(
        self,
        max_queue: int = DEFAULT_MAX_QUEUE,
        num_workers: int = DEFAULT_WORKERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._threads: List[threading.Thread] = []
        self._num_workers = max(1, int(num_workers))
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff))
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stopping = threading.Event()
        self._submitted = 0
        self._written = 0
        self._dropped = 0
        self._failed = 0
        self._retried = 0

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
                self._run_job(job)
            finally:
                self._queue.task_done()

    def _run_job(self, job: Callable[[], None]) -> None:
        """Run one job with a bounded, idempotent retry budget. Never raises.

        A transient database blip would otherwise drop the trace on the first
        failure; retrying a fixed, small number of times rides it out. The
        stall is bounded by ``max_retries * retry_backoff`` on the worker
        thread, so a fully-down database cannot wedge the pool — the job is
        dropped after the budget and the worker moves on.
        """
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                job()
                with self._stats_lock:
                    self._written += 1
                _record("written")
                return
            except Exception:
                if attempt < attempts:
                    with self._stats_lock:
                        self._retried += 1
                    _record("retried")
                    logger.warning(
                        "Trace write failed (attempt %d/%d); retrying",
                        attempt, attempts, exc_info=True,
                    )
                    if self._retry_backoff:
                        time.sleep(self._retry_backoff)
                    continue
                with self._stats_lock:
                    self._failed += 1
                _record("failed")
                logger.warning(
                    "Trace write failed after %d attempt(s); dropping",
                    attempts, exc_info=True,
                )
                return

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
            "retried": self._retried,
            "queued": self._queue.qsize(),
        }

trace_writer = TraceWriter(
    max_queue=_env_int("CHAT_TRACE_QUEUE_MAX", DEFAULT_MAX_QUEUE),
    num_workers=_env_int("CHAT_TRACE_WORKERS", DEFAULT_WORKERS),
    max_retries=_env_int("CHAT_TRACE_WRITE_RETRIES", DEFAULT_MAX_RETRIES, minimum=0),
    retry_backoff=_env_float("CHAT_TRACE_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF),
)
