"""Focused tests for non-blocking delta-based CPU live sampling."""

import threading
import time
import unittest
from typing import Any
from unittest.mock import Mock, patch

from maintenance.components import ScanCancelled
from maintenance.scanner import SystemScanner
from tests.support.scanner import make_baseline_psutil, make_scanner


def _fake_psutil(
    cpu_percent: Any,
    *,
    boot_time: float = 1000.0,
) -> Any:
    return make_baseline_psutil(
        cpu_percent=cpu_percent,
        sensors_temperatures=dict,
        boot_time=Mock(return_value=boot_time),
    )


class CpuSamplingTests(unittest.TestCase):
    def tearDown(self) -> None:
        scanner = getattr(self, "_scanner", None)
        if scanner is not None:
            scanner._stop_cpu_sampler()

    def _new_scanner(self) -> SystemScanner:
        scanner = make_scanner()
        self._scanner = scanner
        return scanner

    def test_baseline_blocking_sample_then_only_delta_reads(self) -> None:
        state: dict[str, float] = {"value": 12.3}
        calls: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            calls.append(interval)
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_component("cpu")
            state["value"] = 90.0
            second = scanner.scan_component("cpu")

        self.assertEqual(first.value, "12.3%")
        self.assertEqual(second.value, "90.0%")
        self.assertEqual(calls[0], SystemScanner.CPU_PERCENT_SAMPLE_SECONDS)
        self.assertTrue(all(interval is None for interval in calls[1:]))

    def test_baseline_failure_retries_blocking_sample(self) -> None:
        state = {"fail": True}
        calls: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            calls.append(interval)
            if state["fail"]:
                raise PermissionError("denied")
            return 7.0

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_component("cpu")
            self.assertEqual(first.value, "Unavailable")

            state["fail"] = False
            second = scanner.scan_component("cpu")

        self.assertEqual(second.value, "7.0%")
        self.assertEqual(calls.count(SystemScanner.CPU_PERCENT_SAMPLE_SECONDS), 2)

    def test_sampler_failure_degrades_card_and_recovers(self) -> None:
        state: dict[str, Any] = {"value": 12.3, "fail": False}

        def cpu_percent(interval: float | None = None) -> float:
            if state["fail"]:
                raise RuntimeError("sensor broke")
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_component("cpu")
            self.assertEqual(first.value, "12.3%")

            state["fail"] = True
            self.assertEqual(scanner.scan_component("cpu").value, "Unavailable")

            state["fail"] = False
            state["value"] = 40.0
            self.assertEqual(scanner.scan_component("cpu").value, "40.0%")

    def test_consecutive_failures_log_once_per_streak(self) -> None:
        state: dict[str, Any] = {"fail": False}

        def cpu_percent(interval: float | None = None) -> float:
            if state["fail"]:
                raise RuntimeError("sensor broke")
            return 12.3

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            scanner.scan_component("cpu")
            state["fail"] = True
            with self.assertLogs("maintenance.scanner", level="WARNING") as captured:
                scanner.scan_component("cpu")
                scanner.scan_component("cpu")
                sampler_warnings = [
                    line for line in captured.output if "CPU sample failed" in line
                ]
                self.assertEqual(len(sampler_warnings), 1)

    def test_dashboard_and_component_share_the_published_value(self) -> None:
        state: dict[str, float] = {"value": 10.0}

        def cpu_percent(interval: float | None = None) -> float:
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_dashboard()
            state["value"] = 90.0
            second = scanner.scan_dashboard()
            component = scanner.scan_component("cpu")

        self.assertEqual(first.get("cpu").value, "10.0%")
        self.assertEqual(second.get("cpu").value, "90.0%")
        self.assertEqual(component.value, "90.0%")

    def test_no_background_sampling_between_requests(self) -> None:
        calls: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            calls.append(interval)
            return 5.0

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            scanner.scan_component("cpu")
            calls_after_seed = len(calls)
            time.sleep(0.05)
            self.assertEqual(len(calls), calls_after_seed)

    def test_stop_sampler_halts_worker(self) -> None:
        calls: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            calls.append(interval)
            return 5.0

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            scanner.scan_component("cpu")
            calls_after_seed = len(calls)
            scanner._stop_cpu_sampler()
            time.sleep(0.05)
            self.assertEqual(len(calls), calls_after_seed)

    def test_dead_worker_is_restarted_on_next_request(self) -> None:
        state: dict[str, float] = {"value": 12.3}

        def cpu_percent(interval: float | None = None) -> float:
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_component("cpu")
            self.assertEqual(first.value, "12.3%")

            scanner._cpu_worker = None
            state["value"] = 55.0
            second = scanner.scan_component("cpu")

        self.assertEqual(second.value, "55.0%")
        worker = scanner._cpu_worker
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertTrue(worker.is_alive())

    def test_missing_psutil_never_starts_worker(self) -> None:
        scanner = self._new_scanner()
        with (
            patch("maintenance.scanner.psutil", None),
            patch.object(scanner, "gpu_details", return_value=("Test GPU",)),
            patch.object(SystemScanner, "trash_size", return_value=0),
        ):
            result = scanner.scan_component("cpu")

        self.assertEqual(result.value, "Unavailable")
        self.assertIsNone(scanner._cpu_worker)

    def test_startup_value_is_discarded_until_first_request(self) -> None:
        state: dict[str, float] = {"value": 12.3}

        def cpu_percent(interval: float | None = None) -> float:
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            first = scanner.scan_component("cpu")
            self.assertEqual(first.value, "12.3%")
            self.assertIsNone(scanner._cpu_value)

            state["value"] = 40.0
            second = scanner.scan_component("cpu")

        self.assertEqual(second.value, "40.0%")

    def test_idle_to_load_transition_publishes_fresh_delta(self) -> None:
        state: dict[str, float] = {"value": 5.0}
        calls: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            calls.append(interval)
            return state["value"]

        scanner = self._new_scanner()
        with patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)):
            idle = scanner.scan_component("cpu")
            state["value"] = 90.0
            loaded = scanner.scan_component("cpu")
            state["value"] = 5.0
            idle_again = scanner.scan_component("cpu")

        self.assertEqual(idle.value, "5.0%")
        self.assertEqual(loaded.value, "90.0%")
        self.assertEqual(idle_again.value, "5.0%")
        self.assertEqual(calls[0], SystemScanner.CPU_PERCENT_SAMPLE_SECONDS)
        self.assertTrue(all(interval is None for interval in calls[1:]))

    def test_cancel_event_interrupts_cpu_sample_wait(self) -> None:
        scanner = self._new_scanner()
        cancel_event = threading.Event()
        wait_timeouts: list[float | None] = []

        def cpu_percent(interval: float | None = None) -> float:
            return 12.3

        def blocked_wait(timeout: float | None = None) -> bool:
            wait_timeouts.append(timeout)
            cancel_event.set()
            return False

        with (
            patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)),
            patch.object(scanner, "_cpu_result_event", autospec=True) as result_event,
        ):
            scanner.scan_component("cpu")
            result_event.wait.side_effect = blocked_wait

            with self.assertRaises(ScanCancelled):
                scanner.scan_component("cpu", cancel_event=cancel_event)

        self.assertEqual(wait_timeouts, [SystemScanner.CPU_CANCEL_POLL_SECONDS])

    def test_no_cancel_event_keeps_single_timed_wait(self) -> None:
        scanner = self._new_scanner()

        def cpu_percent(interval: float | None = None) -> float:
            return 12.3

        with (
            patch("maintenance.scanner.psutil", _fake_psutil(cpu_percent)),
            patch.object(scanner, "_cpu_result_event", autospec=True) as result_event,
        ):
            scanner.scan_component("cpu")
            result_event.wait.return_value = False

            self.assertEqual(scanner.scan_component("cpu").value, "Unavailable")

        result_event.wait.assert_called_once_with(
            SystemScanner.CPU_WORKER_TIMEOUT_SECONDS
        )
        worker = scanner._cpu_worker
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertTrue(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
