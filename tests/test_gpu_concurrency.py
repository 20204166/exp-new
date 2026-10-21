"""Focused concurrency tests for the hardened GPU query lifecycle."""

import threading
import time
import unittest

from maintenance.components.gpu import GpuProbe
from maintenance.models import DashboardSnapshot, ResourceSummary
from maintenance.scanner import SystemScanner
from tests.support.scanner import gpu_environment, make_gpu_probe, make_scanner

TIMEOUT = SystemScanner.GPU_QUERY_TIMEOUT_MESSAGE


class GpuConcurrencyTests(unittest.TestCase):
    def tearDown(self) -> None:
        scanner = getattr(self, "_scanner", None)
        if scanner is not None:
            scanner._stop_gpu_query()

    def _new_scanner(self) -> SystemScanner:
        scanner = make_scanner()
        scanner.GPU_QUERY_TIMEOUT_SECONDS = 0.05
        scanner.GPU_QUERY_ABANDON_SECONDS = 0.05
        self._scanner = scanner
        return scanner

    def test_simultaneous_calls_start_exactly_one_worker(self) -> None:
        scanner = self._new_scanner()
        gate = threading.Event()
        started = threading.Barrier(8)
        call_count = 0
        call_lock = threading.Lock()

        def slow_detect() -> GpuProbe:
            nonlocal call_count
            with call_lock:
                call_count += 1
            gate.wait(5)
            return make_gpu_probe("Slow GPU")

        with gpu_environment(scanner, linux_gpu_probe=slow_detect):
            results: list[tuple[str, ...]] = []
            errors: list[Exception] = []

            def caller() -> None:
                started.wait(5)
                try:
                    results.append(scanner.gpu_details())
                except Exception as error:  # noqa: BLE001 - tests must surface failures.
                    errors.append(error)

            threads = [threading.Thread(target=caller) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            gate.set()
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)
            fresh = scanner.gpu_details()

        self.assertEqual(errors, [])
        with call_lock:
            self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 8)
        self.assertEqual(results, [(TIMEOUT,)] * 8)
        self.assertEqual(fresh, ("Slow GPU",))
        self.assertFalse(scanner._gpu_query_in_flight)

    def test_slow_query_recovers_after_completion(self) -> None:
        scanner = self._new_scanner()
        release = threading.Event()

        def slow_but_finite() -> GpuProbe:
            release.wait(5)
            return make_gpu_probe("Late GPU")

        with gpu_environment(scanner, linux_gpu_probe=slow_but_finite):
            self.assertEqual(scanner.gpu_details(), (TIMEOUT,))
            self.assertTrue(scanner._gpu_query_in_flight)

            release.set()
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(scanner._gpu_query_in_flight)

            self.assertEqual(scanner.gpu_details(), ("Late GPU",))
            self.assertFalse(scanner._gpu_query_in_flight)

    def test_late_worker_finally_clears_own_state(self) -> None:
        scanner = self._new_scanner()
        release = threading.Event()

        def slow_but_finite() -> GpuProbe:
            release.wait(5)
            return make_gpu_probe("Late GPU")

        with gpu_environment(scanner, linux_gpu_probe=slow_but_finite):
            self.assertEqual(scanner.gpu_details(), (TIMEOUT,))
            release.set()
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertFalse(scanner._gpu_query_in_flight)
            self.assertIsNone(scanner._gpu_query_timed_out_at)
            self.assertEqual(scanner.gpu_details(), ("Late GPU",))

    def test_hung_query_is_abandoned_after_window(self) -> None:
        scanner = self._new_scanner()
        calls: list[threading.Event] = []

        def first_hung_then_fresh() -> GpuProbe:
            if not calls:
                event = threading.Event()
                calls.append(event)
                event.wait(5)
                return make_gpu_probe("Hung GPU")
            return make_gpu_probe("Recovered GPU")

        with gpu_environment(scanner, linux_gpu_probe=first_hung_then_fresh):
            self.assertEqual(scanner.gpu_details(), (TIMEOUT,))
            self.assertTrue(scanner._gpu_query_in_flight)

            time.sleep(0.1)
            result = scanner.gpu_details()

        self.assertEqual(result, ("Recovered GPU",))
        self.assertEqual(len(calls), 1)
        self.assertFalse(scanner._gpu_query_in_flight)

    def test_stale_finally_cannot_clear_newer_query(self) -> None:
        scanner = self._new_scanner()
        first_release = threading.Event()
        second_release = threading.Event()
        worker_one_done = threading.Event()
        results: list[tuple[str, ...]] = []
        call_counter = {"n": 0}
        call_lock = threading.Lock()

        def staged_detect() -> GpuProbe:
            with call_lock:
                call_counter["n"] += 1
                call = call_counter["n"]
            if call == 1:
                first_release.wait(5)
                worker_one_done.set()
                return make_gpu_probe("Old GPU")
            second_release.wait(5)
            return make_gpu_probe("New GPU")

        with gpu_environment(scanner, linux_gpu_probe=staged_detect):
            self.assertEqual(scanner.gpu_details(), (TIMEOUT,))

            time.sleep(0.1)
            thread = threading.Thread(
                target=lambda: results.append(scanner.gpu_details())
            )
            thread.start()
            time.sleep(0.1)
            self.assertTrue(scanner._gpu_query_in_flight)

            first_release.set()
            worker_one_done.wait(5)

            self.assertTrue(scanner._gpu_query_in_flight)

            second_release.set()
            thread.join(5)
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)

            scanner.reset_static_cache()
            fresh = scanner.gpu_details()

        self.assertEqual(results, [(TIMEOUT,)])
        self.assertEqual(fresh, ("New GPU",))
        self.assertEqual(call_counter["n"], 3)
        self.assertFalse(scanner._gpu_query_in_flight)

    def test_new_scan_while_old_scan_is_finishing(self) -> None:
        scanner = self._new_scanner()
        first_release = threading.Event()
        second_release = threading.Event()
        call_counter = {"n": 0}
        call_lock = threading.Lock()

        def staged_detect() -> GpuProbe:
            with call_lock:
                call_counter["n"] += 1
                call = call_counter["n"]
            if call == 1:
                first_release.wait(5)
                return make_gpu_probe("Old GPU")
            second_release.wait(5)
            return make_gpu_probe("New GPU")

        results: list[tuple[str, ...]] = []

        with gpu_environment(scanner, linux_gpu_probe=staged_detect):
            self.assertEqual(scanner.gpu_details(), (TIMEOUT,))
            time.sleep(0.1)
            thread = threading.Thread(
                target=lambda: results.append(scanner.gpu_details())
            )
            thread.start()
            time.sleep(0.1)

            first_release.set()
            second_release.set()
            thread.join(5)
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)

            scanner.reset_static_cache()
            fresh = scanner.gpu_details()

        self.assertEqual(results, [(TIMEOUT,)])
        self.assertEqual(fresh, ("New GPU",))
        self.assertEqual(call_counter["n"], 3)
        self.assertFalse(scanner._gpu_query_in_flight)

    def test_stop_gpu_query_invalidates_and_is_idempotent(self) -> None:
        scanner = self._new_scanner()
        gate = threading.Event()
        results: list[tuple[str, ...]] = []

        def gated_detect() -> GpuProbe:
            gate.wait(5)
            return make_gpu_probe("Gated GPU")

        with gpu_environment(scanner, linux_gpu_probe=gated_detect):
            thread = threading.Thread(
                target=lambda: results.append(scanner.gpu_details())
            )
            thread.start()
            time.sleep(0.1)
            self.assertTrue(scanner._gpu_query_in_flight)

            scanner._stop_gpu_query()
            self.assertFalse(scanner._gpu_query_in_flight)
            scanner._stop_gpu_query()
            self.assertFalse(scanner._gpu_query_in_flight)

            gate.set()
            self.assertEqual(scanner.gpu_details(), ("Gated GPU",))
            thread.join(5)

        self.assertEqual(results, [(TIMEOUT,)])
        self.assertFalse(scanner._gpu_query_in_flight)

    def test_full_scan_and_component_refresh_collision(self) -> None:
        scanner = self._new_scanner()
        scanner.GPU_QUERY_ABANDON_SECONDS = 5.0
        gate = threading.Event()
        call_counter = {"n": 0}
        call_lock = threading.Lock()

        def gated_detect() -> GpuProbe:
            with call_lock:
                call_counter["n"] += 1
            gate.wait(5)
            return make_gpu_probe("Collision GPU")

        started = threading.Barrier(2)
        errors: list[Exception] = []
        snapshot_result: list[DashboardSnapshot] = []
        component_result: list[ResourceSummary] = []

        with gpu_environment(scanner, linux_gpu_probe=gated_detect):

            def full_scan() -> None:
                started.wait(5)
                try:
                    snapshot_result.append(scanner.scan_dashboard())
                except Exception as error:  # noqa: BLE001 - tests must surface failures.
                    errors.append(error)

            def component_scan() -> None:
                started.wait(5)
                try:
                    component_result.append(scanner.scan_component("gpu"))
                except Exception as error:  # noqa: BLE001 - tests must surface failures.
                    errors.append(error)

            first = threading.Thread(target=full_scan)
            second = threading.Thread(target=component_scan)
            first.start()
            second.start()
            first.join(5)
            second.join(5)
            gate.set()
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)
            fresh = scanner.gpu_details()

        self.assertEqual(errors, [])
        with call_lock:
            self.assertEqual(call_counter["n"], 1)
        self.assertEqual(len(snapshot_result), 1)
        self.assertEqual(len(component_result), 1)
        gpu_cards = [
            snapshot_result[0].get("gpu"),
            component_result[0],
        ]
        self.assertTrue(all(card.key == "gpu" for card in gpu_cards))
        self.assertEqual(fresh, ("Collision GPU",))
        self.assertFalse(scanner._gpu_query_in_flight)


if __name__ == "__main__":
    unittest.main()
