"""Tests for bounded runtime observability metrics."""

import threading
import unittest
from typing import cast

from maintenance.observability import ObservabilityWatcher, Outcome


class ObservabilityWatcherTests(unittest.TestCase):
    def test_records_bounded_latency_and_outcome_counts(self) -> None:
        watcher = ObservabilityWatcher(sample_limit=2)

        watcher.record("app:scan", 0.10, outcome="success")
        watcher.record("app:scan", 0.20, outcome="failure", detail="permission denied")
        watcher.record("app:scan", 0.30, outcome="cancelled")

        metric = watcher.snapshot().metrics[0]
        self.assertEqual(metric.target, "app:scan")
        self.assertEqual(metric.count, 3)
        self.assertEqual(metric.successes, 1)
        self.assertEqual(metric.failures, 1)
        self.assertEqual(metric.cancellations, 1)
        self.assertEqual(metric.samples, (0.20, 0.30))
        self.assertEqual(metric.last_error, "permission denied")
        self.assertEqual(metric.distribution["p50"], 0.25)

    def test_in_flight_is_thread_safe_and_snapshot_is_immutable(self) -> None:
        watcher = ObservabilityWatcher()
        barrier = threading.Barrier(3)

        def worker() -> None:
            barrier.wait()
            token = watcher.begin("ui:render")
            barrier.wait()
            watcher.finish(token, outcome="success", duration_seconds=0.05)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        barrier.wait()
        for thread in threads:
            thread.join()

        metric = watcher.snapshot().metrics[0]
        self.assertEqual(metric.count, 2)
        self.assertEqual(metric.in_flight, 0)
        self.assertEqual(metric.peak_in_flight, 2)

    def test_invalid_values_are_rejected(self) -> None:
        watcher = ObservabilityWatcher()

        with self.assertRaises(ValueError):
            watcher.record("", 0.1)
        with self.assertRaises(ValueError):
            watcher.record("app:scan", -0.1)
        with self.assertRaises(ValueError):
            watcher.record("app:scan", 0.1, outcome=cast(Outcome, "unknown"))

    def test_reset_discards_previous_runtime_session(self) -> None:
        watcher = ObservabilityWatcher()
        watcher.record("app:scan", 0.1)

        watcher.reset()

        self.assertEqual(watcher.snapshot().metrics, ())

    def test_snapshot_reports_session_start_and_collection_duration(self) -> None:
        watcher = ObservabilityWatcher(
            clock=iter((10.0, 12.5)).__next__,
            wall_clock=iter((100.0, 105.0)).__next__,
        )

        watcher.record("app:scan", 0.1)
        snapshot = watcher.snapshot()

        self.assertEqual(snapshot.session_started_at, 100.0)
        self.assertEqual(snapshot.duration_seconds, 5.0)

    def test_records_coalesced_stale_and_rejected_events(self) -> None:
        watcher = ObservabilityWatcher()

        watcher.record_event("app:scan", "coalesced")
        watcher.record_event("app:scan", "stale")
        watcher.record_event("app:scan", "rejected")

        metric = watcher.snapshot().metrics[0]
        self.assertEqual(metric.coalesced, 1)
        self.assertEqual(metric.stale, 1)
        self.assertEqual(metric.rejected, 1)

    def test_event_counts_are_available_by_target_and_prefix(self) -> None:
        watcher = ObservabilityWatcher()
        watcher.record_event("ui:render:dashboard", "request")
        watcher.record_event("ui:render:dashboard", "commit")
        watcher.record_event("ui:render:settings", "request")

        self.assertEqual(watcher.event_count("ui:render:dashboard", "commit"), 1)
        self.assertEqual(watcher.event_total("ui:render:", "request"), 2)

    def test_double_finish_is_rejected_without_double_counting(self) -> None:
        watcher = ObservabilityWatcher()
        token = watcher.begin("app:scan")
        watcher.finish(token, duration_seconds=0.1)

        with self.assertRaises(ValueError):
            watcher.finish(token, duration_seconds=0.1)

        metric = watcher.snapshot().metrics[0]
        self.assertEqual(metric.count, 1)
        self.assertEqual(metric.in_flight, 0)

        with self.assertRaisesRegex(ValueError, "already finished"):
            watcher.finish(token, duration_seconds=-0.1)

    def test_invalid_finish_duration_does_not_consume_token(self) -> None:
        watcher = ObservabilityWatcher()
        token = watcher.begin("app:scan")

        with self.assertRaises(ValueError):
            watcher.finish(token, duration_seconds=-0.1)

        watcher.finish(token, duration_seconds=0.1)

        metric = watcher.snapshot().metrics[0]
        self.assertEqual(metric.count, 1)
        self.assertEqual(metric.in_flight, 0)


if __name__ == "__main__":
    unittest.main()
