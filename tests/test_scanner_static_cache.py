"""Focused tests for modular scan recovery and the static-hardware cache."""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from maintenance.components.gpu import GpuProbe
from maintenance.models import CapabilityState
from maintenance.scanner import SystemScanner
from tests.support.scanner import (
    make_baseline_psutil,
    make_scanner,
    scanner_environment,
)


class StaticHardwareCacheTests(unittest.TestCase):
    def test_cpu_core_counts_are_cached_across_refreshes(self) -> None:
        scanner = make_scanner()
        calls: dict[str, int] = {"count": 0}
        fake_psutil = make_baseline_psutil()

        def counting_cpu_count(logical: bool) -> int:
            calls["count"] += 1
            return 8 if logical else 4

        fake_psutil.cpu_count = counting_cpu_count

        with scanner_environment(scanner, fake_psutil):
            first = scanner.scan_dashboard()
            second = scanner.scan_dashboard()

        self.assertEqual(calls["count"], 2)
        self.assertEqual(first.get("cpu").details, second.get("cpu").details)
        self.assertEqual(first.get("cpu").details[0], "Physical cores: 4")

    def test_system_label_is_cached_across_refreshes(self) -> None:
        scanner = make_scanner()
        counters: dict[str, int] = {"system": 0, "release": 0, "machine": 0}
        values = {"system": "Linux", "release": "6.1", "machine": "x86_64"}

        def counting(name: str):
            def wrapped() -> str:
                counters[name] += 1
                return values[name]

            return wrapped

        with scanner_environment(
            scanner,
            make_baseline_psutil(),
            system=counting("system"),
            release=counting("release"),
            machine=counting("machine"),
        ):
            scanner.scan_dashboard()
            scanner.scan_dashboard()

        # ``platform.system()`` is read once for the boot-session label and once
        # for the temperature platform gate (both cached across refreshes);
        # release/machine are read once each for the label only.
        self.assertEqual(counters, {"system": 2, "release": 1, "machine": 1})

    def test_static_cache_invalidates_after_boot_session_change(self) -> None:
        scanner = make_scanner()
        state: dict[str, float] = {"boot": 1000.0, "cores": 0.0}
        fake_psutil = make_baseline_psutil()
        fake_psutil.boot_time = lambda: state["boot"]

        def counting_cpu_count(logical: bool) -> int:
            state["cores"] += 1
            return 8 if logical else 4

        fake_psutil.cpu_count = counting_cpu_count

        with scanner_environment(scanner, fake_psutil):
            scanner.scan_dashboard()
            state["boot"] = 2000.0
            scanner.scan_dashboard()

        self.assertEqual(state["cores"], 4)

    def test_reset_static_cache_forces_re_read(self) -> None:
        scanner = make_scanner()
        calls: dict[str, int] = {"count": 0}
        fake_psutil = make_baseline_psutil()

        def counting_cpu_count(logical: bool) -> int:
            calls["count"] += 1
            return 8 if logical else 4

        fake_psutil.cpu_count = counting_cpu_count

        with scanner_environment(scanner, fake_psutil):
            scanner.scan_dashboard()
            scanner.reset_static_cache()
            scanner.scan_dashboard()

        self.assertEqual(calls["count"], 4)

    def test_reset_static_cache_clears_gpu_details(self) -> None:
        scanner = make_scanner()

        with (
            patch("maintenance.scanner.platform.system", return_value="Windows"),
            patch.object(scanner, "_nvidia_gpu_details", return_value=None),
            patch.object(
                scanner,
                "_windows_gpu_probe",
                return_value=GpuProbe(("AMD Radeon",), CapabilityState.SUPPORTED),
            ) as loader,
        ):
            self.assertEqual(scanner.gpu_details(), ("AMD Radeon",))
            scanner.reset_static_cache()
            self.assertEqual(scanner.gpu_details(), ("AMD Radeon",))

        self.assertEqual(loader.call_count, 2)

    def test_live_metrics_refresh_while_static_stays_cached(self) -> None:
        scanner = make_scanner()
        state: dict[str, Any] = {"cpu": 10.0, "cores": 0}
        fake_psutil = make_baseline_psutil()
        fake_psutil.cpu_percent = lambda interval: state["cpu"]

        def counting_cpu_count(logical: bool) -> int:
            state["cores"] += 1
            return 8 if logical else 4

        fake_psutil.cpu_count = counting_cpu_count

        with scanner_environment(scanner, fake_psutil):
            first = scanner.scan_dashboard()
            state["cpu"] = 90.0
            second = scanner.scan_dashboard()

        self.assertEqual(first.get("cpu").value, "10.0%")
        self.assertEqual(second.get("cpu").value, "90.0%")
        self.assertEqual(state["cores"], 2)


class IndependentFailureTests(unittest.TestCase):
    def test_dashboard_without_psutil_degrades_cards_independently(self) -> None:
        scanner = make_scanner()

        with scanner_environment(scanner, None, trash_size=0):
            snapshot = scanner.scan_dashboard()

        by_key = {resource.key: resource for resource in snapshot.resources}
        for key in ("cpu", "memory", "storage", "network", "battery"):
            self.assertEqual(by_key[key].value, "Unavailable", key)
            self.assertIsNone(by_key[key].percent, key)
        self.assertEqual(by_key["gpu"].value, "Test GPU")
        self.assertEqual(snapshot.system_label, "Linux 6.1 • x86_64")

    def test_storage_failure_degrades_only_storage_card(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        def deny_disk(mount: str) -> SimpleNamespace:
            raise PermissionError("disk denied")

        fake_psutil.disk_usage = deny_disk

        with scanner_environment(scanner, fake_psutil):
            snapshot = scanner.scan_dashboard()

        by_key = {resource.key: resource for resource in snapshot.resources}
        self.assertEqual(by_key["storage"].value, "Unavailable")
        self.assertEqual(
            by_key["storage"].details, ("Storage information is unavailable.",)
        )
        self.assertEqual(by_key["cpu"].value, "12.3%")
        self.assertEqual(by_key["memory"].value, "50.0%")
        self.assertEqual(by_key["gpu"].value, "Test GPU")
        self.assertEqual(by_key["network"].value, "—")
        self.assertEqual(by_key["battery"].value, "75%")


class FingerprintSlotHelperTests(unittest.TestCase):
    def _scanner(self) -> SystemScanner:
        return make_scanner()

    def test_helper_reuses_cached_value_without_recomputing(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> str:
            calls.append("compute")
            return "value"

        first = scanner._cached_fingerprint_value(
            lock=scanner._static_hardware_lock,
            value_name="_static_system_label",
            fingerprint_name="_static_system_label_fingerprint",
            fingerprint=("host", 1000.0),
            loader=loader,
        )
        second = scanner._cached_fingerprint_value(
            lock=scanner._static_hardware_lock,
            value_name="_static_system_label",
            fingerprint_name="_static_system_label_fingerprint",
            fingerprint=("host", 1000.0),
            loader=loader,
        )

        self.assertEqual((first, second), ("value", "value"))
        self.assertEqual(calls, ["compute"])

    def test_helper_invalidates_on_fingerprint_change(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> str:
            calls.append("compute")
            return "value"

        for fingerprint in (("host", 1000.0), ("host", 2000.0)):
            scanner._cached_fingerprint_value(
                lock=scanner._static_hardware_lock,
                value_name="_static_system_label",
                fingerprint_name="_static_system_label_fingerprint",
                fingerprint=fingerprint,
                loader=loader,
            )

        self.assertEqual(calls, ["compute", "compute"])

    def test_helper_failed_loader_never_caches(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> int:
            calls.append("compute")
            if len(calls) == 1:
                raise RuntimeError("read failed")
            return 42

        with self.assertRaisesRegex(RuntimeError, "read failed"):
            scanner._cached_fingerprint_value(
                lock=scanner._static_hardware_lock,
                value_name="_static_cpu_cores",
                fingerprint_name="_static_cpu_cores_fingerprint",
                fingerprint=("host", 1000.0),
                loader=loader,
            )

        self.assertEqual(
            scanner._cached_fingerprint_value(
                lock=scanner._static_hardware_lock,
                value_name="_static_cpu_cores",
                fingerprint_name="_static_cpu_cores_fingerprint",
                fingerprint=("host", 1000.0),
                loader=loader,
            ),
            42,
        )
        self.assertEqual(calls, ["compute", "compute"])

    def test_helper_unacceptable_value_is_returned_but_not_cached(self) -> None:
        scanner = self._scanner()
        calls: list[str] = []

        def loader() -> str:
            calls.append("compute")
            return "unavailable"

        for _attempt in range(2):
            result = scanner._cached_fingerprint_value(
                lock=scanner._static_gpu_lock,
                value_name="_static_gpu_details",
                fingerprint_name="_static_gpu_fingerprint",
                fingerprint=("host", 1000.0),
                loader=loader,
                acceptable=lambda details: details != "unavailable",
            )
            self.assertEqual(result, "unavailable")

        self.assertEqual(calls, ["compute", "compute"])


if __name__ == "__main__":
    unittest.main()
