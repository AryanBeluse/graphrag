# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.chat_history.trace_writer.TraceWriter``.

Covers the durability behaviour added for production hardening: bounded
idempotent retry on transient failure, drop-on-exhaustion, flush draining,
and configurable sizing. No database — jobs are plain callables.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock

sys.modules.setdefault("common.metrics.prometheus_metrics", MagicMock())

from common.chat_history.trace_writer import TraceWriter  


class TestRetry:
    def test_transient_failure_is_retried_then_succeeds(self):
        w = TraceWriter(num_workers=1, max_retries=3, retry_backoff=0.0)
        calls = {"n": 0}
        done = threading.Event()

        def job():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            done.set()

        assert w.submit(job) is True
        assert done.wait(2.0)
        assert w.flush(2.0)
        s = w.stats()
        assert s["written"] == 1
        assert s["failed"] == 0
        assert s["retried"] == 2  
        assert calls["n"] == 3

    def test_permanent_failure_is_dropped_after_budget(self):
        w = TraceWriter(num_workers=1, max_retries=2, retry_backoff=0.0)
        calls = {"n": 0}

        def job():
            calls["n"] += 1
            raise RuntimeError("always")

        w.submit(job)
        assert w.flush(2.0)
        s = w.stats()
        assert s["failed"] == 1
        assert s["written"] == 0
        assert calls["n"] == 3
        assert s["retried"] == 2

    def test_zero_retries_drops_on_first_failure(self):
        w = TraceWriter(num_workers=1, max_retries=0, retry_backoff=0.0)
        calls = {"n": 0}

        def job():
            calls["n"] += 1
            raise RuntimeError("boom")

        w.submit(job)
        assert w.flush(2.0)
        assert calls["n"] == 1
        assert w.stats()["failed"] == 1
        assert w.stats()["retried"] == 0


class TestSubmitAndDrop:
    def test_full_queue_drops_without_raising(self):
        w = TraceWriter(max_queue=1, num_workers=1, retry_backoff=0.0)
        block = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            block.wait(2.0)

        assert w.submit(slow) is True
        assert started.wait(2.0)
        w.submit(lambda: None)
        dropped_ok = w.submit(lambda: None) 
        assert dropped_ok is False
        assert w.stats()["dropped"] >= 1
        block.set()
        w.flush(2.0)

    def test_stats_has_all_outcomes(self):
        w = TraceWriter(num_workers=1)
        assert set(w.stats()) >= {
            "submitted", "written", "dropped", "failed", "retried", "queued"
        }


class TestFlush:
    def test_flush_drains_pending_writes(self):
        w = TraceWriter(num_workers=2, retry_backoff=0.0)
        seen = []
        lock = threading.Lock()

        def job(i):
            with lock:
                seen.append(i)

        for i in range(20):
            w.submit(lambda i=i: job(i))
        assert w.flush(3.0)
        assert sorted(seen) == list(range(20))
        assert w.stats()["written"] == 20


class TestConfigurableSizing:
    def test_constructor_sizes_are_applied(self):
        w = TraceWriter(max_queue=7, num_workers=3, max_retries=5, retry_backoff=0.25)
        assert w._queue.maxsize == 7
        assert w._num_workers == 3
        assert w._max_retries == 5
        assert w._retry_backoff == 0.25

    def test_negative_retries_clamped_to_zero(self):
        w = TraceWriter(max_retries=-4)
        assert w._max_retries == 0
