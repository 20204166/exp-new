import getpass
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import maintenance.components.scan_support as scan_support_module
from maintenance.actions import FileManager, ProcessManager
from maintenance.components import DownloadScanner
from maintenance.components.gpu import GpuProbe
from maintenance.models import CapabilityState, unavailable_summary
from maintenance.scanner import ScanCancelled, SystemScanner
from tests.support.models import make_snapshot, make_summary
from tests.support.process_actions import ActionProcess as FakeProcess
from tests.support.process_actions import ActionPsutil as FakePsutil
from tests.support.scanner import (
    make_baseline_psutil,
    make_gpu_probe,
    make_scanner,
    scanner_environment,
)


class ScannerTests(unittest.TestCase):
    def test_download_scan_finds_large_files_and_only_extra_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            first = downloads / "a-copy.bin"
            second = downloads / "b-copy.bin"
            large = downloads / "large.bin"
            first.write_bytes(b"same contents")
            second.write_bytes(b"same contents")
            large.write_bytes(b"large file contents")

            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            scanner.LARGE_FILE_BYTES = 15
            candidates = scanner.scan_downloads()

            by_path = {candidate.path: candidate for candidate in candidates}
            self.assertNotIn(first, by_path)
            self.assertIn(second, by_path)
            self.assertIn("Verified duplicate", by_path[second].reason)
            self.assertIn(large, by_path)
            self.assertIn("Large file", by_path[large].reason)

    def test_download_scan_ignores_hidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            visible = downloads / "visible.bin"
            hidden_dir = downloads / ".hidden"
            hidden_file = hidden_dir / "secret.bin"

            hidden_dir.mkdir()
            visible.write_bytes(b"visible contents")
            hidden_file.write_bytes(b"hidden contents")

            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            scanner.LARGE_FILE_BYTES = 1
            candidates = scanner.scan_downloads()

            by_path = {candidate.path for candidate in candidates}
            self.assertIn(visible, by_path)
            self.assertNotIn(hidden_file, by_path)

    def test_download_scan_keeps_equal_size_candidates_in_stable_path_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            first_directory = downloads / "a"
            second_directory = downloads / "b"
            first_directory.mkdir()
            second_directory.mkdir()
            first = first_directory / "file.bin"
            second = second_directory / "file.bin"
            first.write_bytes(b"same size")
            second.write_bytes(b"same size")

            scanner = SystemScanner(downloads)
            scanner.LARGE_FILE_BYTES = 1
            scanner.DUPLICATE_MIN_BYTES = 100

            candidates = scanner.scan_downloads()

            self.assertEqual(
                [candidate.path for candidate in candidates], [first, second]
            )

    def test_download_scan_keeps_iterator_errors_fail_soft(self) -> None:
        class BrokenEntries:
            def __iter__(self) -> "BrokenEntries":
                return self

            def __next__(self) -> object:
                raise OSError("directory changed during scan")

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            scanner = SystemScanner(Path(directory))
            with patch(
                "maintenance.scanner.os.scandir",
                return_value=BrokenEntries(),
            ):
                self.assertEqual(scanner._download_file_stats(Path(directory)), {})

    def test_duplicate_hash_cache_reuses_hashes_and_invalidates_on_mtime_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            first = downloads / "a-copy.bin"
            second = downloads / "b-copy.bin"
            first.write_bytes(b"same contents")
            second.write_bytes(b"same contents")

            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            real_hash = SystemScanner._file_hash

            with patch.object(
                SystemScanner, "_file_hash", wraps=real_hash
            ) as hash_mock:
                self.assertEqual(len(scanner.scan_downloads()), 1)
                self.assertEqual(len(scanner.scan_downloads()), 1)
                self.assertEqual(hash_mock.call_count, 2)

                updated_mtime = first.stat().st_mtime_ns + 2_000_000
                os.utime(first, ns=(updated_mtime, updated_mtime))
                self.assertEqual(len(scanner.scan_downloads()), 1)

            self.assertEqual(hash_mock.call_count, 3)

    def test_duplicate_hash_cache_rechecks_same_timestamp_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            first = downloads / "a-copy.bin"
            second = downloads / "b-copy.bin"
            first.write_bytes(b"same contents")
            second.write_bytes(b"same contents")

            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            scanner.scan_downloads()
            original_mtime = first.stat().st_mtime_ns

            first.write_bytes(b"other content")
            os.utime(first, ns=(original_mtime, original_mtime))

            self.assertEqual(scanner.scan_downloads(), [])

    def test_duplicate_hash_cache_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            for index in range(4):
                (downloads / f"copy-{index}.bin").write_bytes(b"same contents")

            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            scanner.HASH_CACHE_MAX_ENTRIES = 2

            scanner.scan_downloads()

            self.assertLessEqual(len(scanner._hash_cache), 2)

    def test_download_scan_reports_progress_and_honours_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            (downloads / "one.bin").write_bytes(b"same contents")
            (downloads / "two.bin").write_bytes(b"same contents")
            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            messages: list[str] = []

            scanner.scan_downloads(progress_callback=messages.append)

            self.assertTrue(
                any("Checking duplicates" in message for message in messages)
            )

            cancel_event = threading.Event()
            cancel_event.set()
            with self.assertRaises(ScanCancelled):
                scanner.scan_downloads(cancel_event=cancel_event)

    def test_cancellable_scan_keeps_one_argument_hash_hooks_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            (downloads / "one.bin").write_bytes(b"same contents")
            (downloads / "two.bin").write_bytes(b"same contents")
            scanner = SystemScanner(downloads)
            scanner.DUPLICATE_MIN_BYTES = 1
            original_hash = SystemScanner._file_hash
            # Installing an older one-argument hook is the point of this test;
            # the scanner must keep working through its TypeError fallback.
            scanner._file_hash = lambda path: original_hash(path)  # type: ignore[method-assign,assignment,misc]

            self.assertEqual(
                len(scanner.scan_downloads(cancel_event=threading.Event())),
                1,
            )

    def test_scanner_and_component_share_hashing_and_fingerprinting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.bin"
            path.write_bytes(b"shared content" * 1000)

            self.assertEqual(
                SystemScanner._file_hash(path),
                DownloadScanner._file_hash(path),
            )
            self.assertEqual(
                SystemScanner(root)._file_content_marker(path),
                DownloadScanner(root)._file_content_marker(path),
            )
            self.assertEqual(
                SystemScanner._hash_fingerprint(path.stat()),
                DownloadScanner._hash_fingerprint(path.stat()),
            )

    def test_process_scan_honours_cancellation_before_enumeration(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(ScanCancelled):
            make_scanner().scan_processes(cancel_event=cancel_event)

    def test_snapshot_get_returns_resource_and_rejects_unknown_key(self) -> None:
        resource = make_summary(
            "cpu",
            "CPU",
            value="10%",
            subtitle="Current usage",
            percent=10,
            details=("Two cores",),
        )
        snapshot = make_snapshot(
            resource,
            system_label="Test System",
            scanned_at=datetime(2026, 9, 5, 3, 42, 52, tzinfo=timezone.utc),
        )

        self.assertEqual(snapshot.get("cpu"), resource)
        with self.assertRaises(KeyError):
            snapshot.get("missing")

    def test_windows_usernames_are_compared_without_the_domain(self) -> None:
        self.assertTrue(SystemScanner._same_user("WORKGROUP\\Iryna", "iryna"))

    def test_mac_windows_and_linux_gpu_fallbacks_are_parsed(self) -> None:
        mac_result = SimpleNamespace(
            stdout=json.dumps(
                {
                    "SPDisplaysDataType": [
                        {"sppci_model": "Intel Iris", "spdisplays_vram": "1 GB"}
                    ]
                }
            )
        )
        windows_result = SimpleNamespace(
            stdout=json.dumps(
                {
                    "Name": "AMD Radeon",
                    "AdapterRAM": 2 * 1024**3,
                    "DriverVersion": "1.2.3",
                }
            )
        )
        linux_result = SimpleNamespace(
            stdout="00:02.0 VGA compatible controller: Intel Corporation UHD\n"
        )

        with patch("maintenance.scanner.subprocess.run", return_value=mac_result):
            self.assertIn("Intel Iris", SystemScanner._mac_gpu_details())
        with patch("maintenance.scanner.subprocess.run", return_value=windows_result):
            self.assertIn("AMD Radeon", SystemScanner._windows_gpu_details())
        with patch("maintenance.scanner.subprocess.run", return_value=linux_result):
            self.assertIn(
                "Intel Corporation UHD",
                SystemScanner._linux_gpu_details(),
            )

    def test_missing_gpu_command_reports_unavailable_for_each_platform(self) -> None:
        for details in (
            SystemScanner._mac_gpu_details,
            SystemScanner._windows_gpu_details,
            SystemScanner._linux_gpu_details,
        ):
            with (
                self.subTest(details=details.__name__),
                patch(
                    "maintenance.scanner.subprocess.run",
                    side_effect=FileNotFoundError(2, "No such file or directory"),
                ),
                self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
            ):
                lines = details()

            self.assertEqual(lines, ("GPU information unavailable",))
            self.assertIn("No such file or directory", "\n".join(log.output))

    def test_hung_gpu_command_reports_timeout_message(self) -> None:
        timeout = subprocess.TimeoutExpired(cmd=["lspci"], timeout=10)
        for details in (
            SystemScanner._mac_gpu_details,
            SystemScanner._windows_gpu_details,
            SystemScanner._linux_gpu_details,
        ):
            with (
                self.subTest(details=details.__name__),
                patch(
                    "maintenance.scanner.subprocess.run",
                    side_effect=timeout,
                ),
                self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
            ):
                lines = details()

            self.assertEqual(lines, ("GPU information unavailable",))
            self.assertIn("timed out after 10 seconds", "\n".join(log.output))

    def test_non_zero_gpu_command_exit_reports_unavailable(self) -> None:
        failure = subprocess.CalledProcessError(returncode=3, cmd=["lspci"])
        for details in (
            SystemScanner._mac_gpu_details,
            SystemScanner._windows_gpu_details,
            SystemScanner._linux_gpu_details,
        ):
            with (
                self.subTest(details=details.__name__),
                patch(
                    "maintenance.scanner.subprocess.run",
                    side_effect=failure,
                ),
                self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
            ):
                lines = details()

            self.assertEqual(lines, ("GPU information unavailable",))
            self.assertIn("exit status 3", "\n".join(log.output))

    def test_malformed_gpu_json_reports_unavailable_message(self) -> None:
        mac_result = SimpleNamespace(stdout="{not-json")
        windows_result = SimpleNamespace(stdout="{not-json")

        with (
            patch("maintenance.scanner.subprocess.run", return_value=mac_result),
            self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
        ):
            mac_lines = SystemScanner._mac_gpu_details()
        self.assertEqual(mac_lines, ("GPU information unavailable",))
        self.assertIn("Expecting", "\n".join(log.output))

        with (
            patch("maintenance.scanner.subprocess.run", return_value=windows_result),
            self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
        ):
            windows_lines = SystemScanner._windows_gpu_details()
        self.assertEqual(windows_lines, ("GPU information unavailable",))
        self.assertIn("Expecting", "\n".join(log.output))

    def test_windows_empty_gpu_stdout_falls_back_to_empty_list(self) -> None:
        windows_result = SimpleNamespace(stdout="")

        with patch("maintenance.scanner.subprocess.run", return_value=windows_result):
            self.assertEqual(
                SystemScanner._windows_gpu_details(),
                ("GPU information unavailable",),
            )

    def test_gpu_fallback_details_are_cached_per_scanner(self) -> None:
        scanner = make_scanner()
        with (
            patch("maintenance.scanner.platform.system", return_value="Windows"),
            patch.object(scanner, "_nvidia_gpu_details", return_value=None),
            patch.object(
                scanner,
                "_windows_gpu_probe",
                return_value=make_gpu_probe("AMD Radeon"),
            ) as loader,
        ):
            self.assertEqual(scanner.gpu_details(), ("AMD Radeon",))
            self.assertEqual(scanner.gpu_details(), ("AMD Radeon",))

        loader.assert_called_once_with()

    def test_gpu_fallback_errors_are_retried_for_recovery(self) -> None:
        scanner = make_scanner()
        with (
            patch("maintenance.scanner.platform.system", return_value="Darwin"),
            patch.object(scanner, "_mac_gpu_probe") as loader,
        ):
            loader.side_effect = [
                GpuProbe(
                    ("GPU information unavailable: first failure",),
                    CapabilityState.UNKNOWN,
                ),
                make_gpu_probe("Intel Iris"),
            ]

            self.assertIn("unavailable", scanner.gpu_details()[0])
            self.assertEqual(scanner.gpu_details(), ("Intel Iris",))

        self.assertEqual(loader.call_count, 2)

    def test_gpu_details_uses_gpu_detector(self) -> None:
        scanner = make_scanner()

        with patch(
            "maintenance.scanner.GpuDetector.detect_with_capability",
            return_value=make_gpu_probe("AMD Radeon"),
        ) as detect:
            self.assertEqual(scanner.gpu_details(), ("AMD Radeon",))

        detect.assert_called_once_with()

    def test_nvml_failure_is_remembered_and_probes_are_skipped(self) -> None:
        scanner = make_scanner()
        calls = {"n": 0}

        def failing_init() -> None:
            calls["n"] += 1
            raise OSError("NVML Shared Library Not Found")

        fake_pynvml = SimpleNamespace(nvmlInit=failing_init, nvmlShutdown=lambda: None)
        with (
            patch("maintenance.scanner.pynvml", fake_pynvml),
            self.assertLogs("maintenance.scanner", level="WARNING") as log,
        ):
            self.assertIsNone(scanner._nvidia_gpu_details())
            self.assertIsNone(scanner._nvidia_gpu_details())

        self.assertEqual(calls["n"], 1)
        self.assertTrue(scanner._nvml_probe_failed)
        self.assertIn("NVIDIA GPU query failed", "\n".join(log.output))

    def test_reset_static_cache_re_enables_nvml_probe(self) -> None:
        scanner = make_scanner()
        calls = {"n": 0}

        def failing_init() -> None:
            calls["n"] += 1
            raise OSError("NVML Shared Library Not Found")

        fake_pynvml = SimpleNamespace(nvmlInit=failing_init, nvmlShutdown=lambda: None)
        with patch("maintenance.scanner.pynvml", fake_pynvml):
            self.assertIsNone(scanner._nvidia_gpu_details())
            self.assertTrue(scanner._nvml_probe_failed)

            scanner.reset_static_cache()
            self.assertFalse(scanner._nvml_probe_failed)
            self.assertIsNone(scanner._nvidia_gpu_details())

        self.assertEqual(calls["n"], 2)

    def test_nvml_success_path_probes_every_call(self) -> None:
        scanner = make_scanner()
        calls = {"n": 0}

        def succeeding_init() -> None:
            calls["n"] += 1

        fake_pynvml = SimpleNamespace(
            nvmlInit=succeeding_init,
            nvmlShutdown=lambda: None,
            nvmlDeviceGetCount=lambda: 0,
        )
        with patch("maintenance.scanner.pynvml", fake_pynvml):
            self.assertIsNone(scanner._nvidia_gpu_details())
            self.assertIsNone(scanner._nvidia_gpu_details())

        self.assertEqual(calls["n"], 2)
        self.assertFalse(scanner._nvml_probe_failed)

    def test_gpu_details_times_out_hung_query(self) -> None:
        scanner = make_scanner()
        scanner.GPU_QUERY_TIMEOUT_SECONDS = 0.05

        def hang() -> GpuProbe:
            threading.Event().wait(5)
            return make_gpu_probe("Late GPU")

        started = time.monotonic()
        with (
            patch.object(scanner, "_nvidia_gpu_details", return_value=None),
            patch("maintenance.scanner.platform.system", return_value="Linux"),
            patch.object(SystemScanner, "_linux_gpu_probe", side_effect=hang),
        ):
            result = scanner.gpu_details()
        elapsed = time.monotonic() - started

        self.assertEqual(
            result,
            ("GPU information unavailable: query timed out",),
        )
        self.assertLess(elapsed, 2)

    def test_gpu_details_bounds_hung_nvidia_query(self) -> None:
        scanner = make_scanner()
        scanner.GPU_QUERY_TIMEOUT_SECONDS = 0.05

        def hang_nvidia() -> tuple[str, ...] | None:
            threading.Event().wait(5)
            return None

        with (
            patch.object(scanner, "_nvidia_gpu_details", side_effect=hang_nvidia),
            patch("maintenance.scanner.platform.system", return_value="Linux"),
        ):
            self.assertEqual(
                scanner.gpu_details(),
                ("GPU information unavailable: query timed out",),
            )
            self.assertTrue(scanner._gpu_query_in_flight)

            with patch("maintenance.scanner.threading.Thread") as thread_factory:
                self.assertEqual(
                    scanner.gpu_details(),
                    ("GPU information unavailable: query timed out",),
                )

        thread_factory.assert_not_called()

    def test_gpu_details_avoids_new_worker_while_previous_query_in_flight(
        self,
    ) -> None:
        scanner = make_scanner()
        scanner.GPU_QUERY_TIMEOUT_SECONDS = 0.05

        def hang() -> GpuProbe:
            threading.Event().wait(5)
            return make_gpu_probe("Late GPU")

        with (
            patch.object(scanner, "_nvidia_gpu_details", return_value=None),
            patch("maintenance.scanner.platform.system", return_value="Linux"),
            patch.object(SystemScanner, "_linux_gpu_probe", side_effect=hang),
        ):
            first = scanner.gpu_details()
            self.assertEqual(
                first,
                ("GPU information unavailable: query timed out",),
            )
            self.assertTrue(scanner._gpu_query_in_flight)

            with patch("maintenance.scanner.threading.Thread") as thread_factory:
                second = scanner.gpu_details()

        self.assertEqual(
            second,
            ("GPU information unavailable: query timed out",),
        )
        thread_factory.assert_not_called()

    def test_gpu_details_recovers_after_late_worker_completion(self) -> None:
        scanner = make_scanner()
        scanner.GPU_QUERY_TIMEOUT_SECONDS = 0.05
        release = threading.Event()

        def slow_but_finite() -> GpuProbe:
            release.wait(5)
            return make_gpu_probe("Late GPU")

        with (
            patch.object(scanner, "_nvidia_gpu_details", return_value=None),
            patch("maintenance.scanner.platform.system", return_value="Linux"),
            patch.object(
                SystemScanner,
                "_linux_gpu_probe",
                side_effect=slow_but_finite,
            ),
        ):
            self.assertEqual(
                scanner.gpu_details(),
                ("GPU information unavailable: query timed out",),
            )
            self.assertTrue(scanner._gpu_query_in_flight)

            release.set()
            deadline = time.monotonic() + 2
            while scanner._gpu_query_in_flight and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(scanner._gpu_query_in_flight)

            self.assertEqual(scanner.gpu_details(), ("Late GPU",))
            self.assertFalse(scanner._gpu_query_in_flight)

    def test_gpu_details_reports_worker_error_as_unavailable(self) -> None:
        scanner = make_scanner()

        with (
            patch.object(
                scanner,
                "_make_gpu_detector",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs("maintenance.components.gpu", level="WARNING") as log,
        ):
            self.assertEqual(
                scanner.gpu_details(),
                ("GPU information unavailable",),
            )

        self.assertIn("boom", "\n".join(log.output))

    def test_scan_dashboard_still_honours_cancellation(self) -> None:
        scanner = make_scanner()
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(ScanCancelled):
            scanner.scan_dashboard(cancel_event=cancel_event)

    def test_scan_dashboard_output_matches_expected_cards(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        with scanner_environment(scanner, fake_psutil):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(snapshot.system_label, "Linux 6.1 • x86_64")
        by_key = {resource.key: resource for resource in snapshot.resources}
        self.assertEqual(by_key["cpu"].value, "12.3%")
        self.assertEqual(
            by_key["cpu"].details,
            (
                "Physical cores: 4",
                "Logical cores: 8",
                "Current frequency: 2.40 GHz / Max 4.00 GHz",
            ),
        )
        self.assertEqual(by_key["memory"].value, "50.0%")
        self.assertEqual(
            by_key["memory"].details,
            (
                "Physical RAM: 16.00 GiB",
                "Available: 8.00 GiB",
                "In use: 8.00 GiB (50.0%)",
                "Swap: 1.00 GiB used of 4.00 GiB (25.0%)",
            ),
        )
        self.assertEqual(by_key["storage"].value, "40.0%")
        self.assertIn("Trash size: 2.00 KiB", by_key["storage"].details)
        self.assertEqual(by_key["gpu"].value, "Test GPU")
        self.assertEqual(by_key["network"].value, "—")
        self.assertIn(
            "Received this boot: 1.95 KiB",
            by_key["network"].details,
        )
        self.assertIn("Sent this boot: 1000.00 B", by_key["network"].details)
        self.assertEqual(by_key["battery"].value, "75%")
        self.assertEqual(by_key["battery"].subtitle, "Charging")

    def test_scan_dashboard_degrades_failing_battery_sensor(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        def deny_battery() -> None:
            raise PermissionError("access denied")

        fake_psutil.sensors_battery = deny_battery

        with scanner_environment(scanner, fake_psutil):
            snapshot = scanner.scan_dashboard()

        by_key = {resource.key: resource for resource in snapshot.resources}
        self.assertEqual(by_key["battery"].value, "Unavailable")
        self.assertEqual(
            by_key["battery"].details,
            ("Battery information is unavailable.",),
        )
        self.assertEqual(by_key["cpu"].value, "12.3%")
        self.assertEqual(by_key["memory"].value, "50.0%")
        self.assertEqual(by_key["storage"].value, "40.0%")
        self.assertEqual(by_key["network"].value, "—")

    def test_scan_dashboard_replaces_failing_card_with_unavailable_summary(
        self,
    ) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        def broken_cpu_count(logical: bool) -> int:
            raise RuntimeError("cpu count unavailable")

        fake_psutil.cpu_count = broken_cpu_count

        with scanner_environment(scanner, fake_psutil):
            snapshot = scanner.scan_dashboard()

        by_key = {resource.key: resource for resource in snapshot.resources}
        self.assertEqual(by_key["cpu"].value, "Unavailable")
        self.assertEqual(by_key["cpu"].subtitle, "Information unavailable")
        self.assertIsNone(by_key["cpu"].percent)
        self.assertEqual(
            by_key["cpu"].details,
            ("CPU information is unavailable.",),
        )
        self.assertTrue(by_key["cpu"].actionable)
        self.assertEqual(by_key["memory"].value, "50.0%")
        self.assertEqual(by_key["storage"].value, "40.0%")
        self.assertEqual(by_key["gpu"].value, "Test GPU")
        self.assertEqual(by_key["network"].value, "—")
        self.assertEqual(by_key["battery"].value, "75%")

    def test_scan_dashboard_survives_every_sensor_failing(self) -> None:
        scanner = make_scanner()

        def deny(*_args: Any, **_kwargs: Any) -> Any:
            raise PermissionError("sensor denied")

        fake_psutil = SimpleNamespace(
            cpu_percent=deny,
            cpu_freq=deny,
            cpu_count=deny,
            virtual_memory=deny,
            swap_memory=deny,
            disk_usage=deny,
            net_io_counters=deny,
            sensors_battery=deny,
        )

        with scanner_environment(scanner, fake_psutil, trash_size=0):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(len(snapshot.resources), 6)
        by_key = {resource.key: resource for resource in snapshot.resources}
        for key in ("cpu", "memory", "storage", "network", "battery"):
            self.assertEqual(by_key[key].value, "Unavailable", key)
            self.assertIsNone(by_key[key].percent, key)
        self.assertEqual(by_key["gpu"].value, "Test GPU")
        self.assertEqual(snapshot.system_label, "Linux 6.1 • x86_64")

    def test_scan_dashboard_gpu_card_shows_unavailable_when_probe_fails(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        with scanner_environment(
            scanner,
            fake_psutil,
            gpu_details=("GPU information unavailable",),
        ):
            snapshot = scanner.scan_dashboard()

        gpu = snapshot.get("gpu")
        self.assertEqual(gpu.value, "GPU information unavailable")
        self.assertEqual(gpu.details, ("GPU information unavailable",))
        self.assertEqual(snapshot.get("cpu").value, "12.3%")

    def test_scan_dashboard_gpu_card_lists_multiple_gpus(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        with scanner_environment(
            scanner,
            fake_psutil,
            gpu_details=("GPU One", "GPU Two", "Memory: 4 GiB"),
        ):
            snapshot = scanner.scan_dashboard()

        gpu = snapshot.get("gpu")
        self.assertEqual(gpu.value, "GPU One")
        self.assertIn("GPU Two", gpu.details)
        self.assertIn("Memory: 4 GiB", gpu.details)

    def test_scan_component_is_independent_of_other_components(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        def deny_battery() -> Any:
            raise PermissionError("denied")

        fake_psutil.sensors_battery = deny_battery

        with scanner_environment(scanner, fake_psutil):
            cpu = scanner.scan_component("cpu")
            memory = scanner.scan_component("memory")
            battery = scanner.scan_component("battery")

        self.assertEqual(cpu.value, "12.3%")
        self.assertEqual(memory.value, "50.0%")
        self.assertEqual(battery.value, "Unavailable")

    def test_scan_component_rejects_unknown_key(self) -> None:
        scanner = make_scanner()

        with self.assertRaisesRegex(ValueError, "Unknown component"):
            scanner.scan_component("not-a-component")

    def test_component_value_handles_missing_and_failing_psutil(self) -> None:
        scanner = make_scanner()

        with patch("maintenance.scanner.psutil", None):
            self.assertIsNone(scanner._component_value(lambda: 42))

        fake_psutil = SimpleNamespace(reading=lambda: 7)
        with patch("maintenance.scanner.psutil", fake_psutil):
            self.assertEqual(scanner._component_value(fake_psutil.reading), 7)

        def broken() -> Any:
            raise PermissionError("denied")

        with patch("maintenance.scanner.psutil", fake_psutil):
            self.assertIsNone(scanner._component_value(broken))

    def test_scan_component_matches_dashboard_card(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        with scanner_environment(scanner, fake_psutil):
            snapshot = scanner.scan_dashboard()
            cpu_card = scanner.scan_component("cpu")

        self.assertEqual(snapshot.get("cpu").details, cpu_card.details)
        self.assertEqual(snapshot.get("cpu").value, cpu_card.value)

    def test_unavailable_summary_is_the_shared_failure_presentation(self) -> None:
        summary = unavailable_summary("cpu", "CPU")

        self.assertEqual(summary.value, "Unavailable")
        self.assertEqual(summary.subtitle, "Information unavailable")
        self.assertEqual(summary.details, ("CPU information is unavailable.",))
        self.assertTrue(summary.actionable)
        self.assertTrue(summary.failed)
        self.assertFalse(unavailable_summary("gpu", "GPU").actionable)

    def test_byte_unit_choices_match_format_bytes_boundaries(self) -> None:
        for value, expected in (
            (0, "0.00 B"),
            (1023, "1023.00 B"),
            (1024, "1.00 KiB"),
            (1024**2, "1.00 MiB"),
            (1024**3, "1.00 GiB"),
            (1024**4, "1.00 TiB"),
            (1024**5, "1024.00 TiB"),
            (-2048, "-2.00 KiB"),
        ):
            with self.subTest(value=value):
                self.assertEqual(SystemScanner.format_bytes(value), expected)

        self.assertEqual(SystemScanner._byte_unit(573), (1, "B"))
        self.assertEqual(SystemScanner._byte_unit(2048), (1024, "KiB"))
        self.assertEqual(SystemScanner._byte_unit(5 * 1024**2), (1024**2, "MiB"))

    def test_scan_dashboard_reports_component_progress(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()
        messages: list[str] = []

        with scanner_environment(scanner, fake_psutil):
            scanner.scan_dashboard(progress_callback=messages.append)

        self.assertEqual(
            messages,
            [
                "Scanning CPU...",
                "Scanning Memory...",
                "Scanning Storage...",
                "Scanning GPU...",
                "Scanning Network...",
                "Scanning Battery...",
            ],
        )

    def test_frequency_detail_shows_current_and_max(self) -> None:
        frequency = SimpleNamespace(current=1350.0, min=800.0, max=4000.0)

        self.assertEqual(
            SystemScanner._frequency_detail(frequency),
            "Current frequency: 1.35 GHz / Max 4.00 GHz",
        )

    def test_frequency_detail_omits_zero_or_missing_max(self) -> None:
        zero_max = SimpleNamespace(current=2400.0, max=0.0)
        missing_max = SimpleNamespace(current=2400.0)

        self.assertEqual(
            SystemScanner._frequency_detail(zero_max),
            "Current frequency: 2.40 GHz",
        )
        self.assertEqual(
            SystemScanner._frequency_detail(missing_max),
            "Current frequency: 2.40 GHz",
        )

    def test_frequency_detail_reports_unavailable_for_missing_current(self) -> None:
        for frequency in (
            None,
            SimpleNamespace(current=0.0, max=4000.0),
            SimpleNamespace(),
        ):
            self.assertEqual(
                SystemScanner._frequency_detail(frequency),
                "Current frequency: Unavailable",
            )

    def test_frequency_detail_tolerates_reversed_max(self) -> None:
        frequency = SimpleNamespace(current=4000.0, max=1000.0)

        self.assertEqual(
            SystemScanner._frequency_detail(frequency),
            "Current frequency: 4.00 GHz",
        )

    def test_swap_details_uses_psutil_aggregate_without_devices(self) -> None:
        scanner = make_scanner()
        swap = SimpleNamespace(
            total=4 * 1024**3,
            used=1 * 1024**3,
            percent=25.0,
        )

        self.assertEqual(
            scanner._swap_details(swap, []),
            ("Swap: 1.00 GiB used of 4.00 GiB (25.0%)",),
        )

    def test_swap_details_reports_no_swap_and_unavailable(self) -> None:
        scanner = make_scanner()

        self.assertEqual(
            scanner._swap_details(SimpleNamespace(total=0, used=0, percent=0.0), []),
            ("Swap: none configured",),
        )
        self.assertEqual(
            scanner._swap_details(None, []),
            ("Swap: Unavailable",),
        )

    def test_swap_details_separates_zram_from_disk_swap(self) -> None:
        scanner = make_scanner()
        devices = [
            ("/dev/zram0", 2 * 1024**3, 512 * 1024**2),
            ("/swapfile", 2 * 1024**3, 0),
            ("/dev/nvme0n1p9", 2 * 1024**3, 256 * 1024**2),
        ]

        self.assertEqual(
            scanner._swap_details(None, devices),
            (
                "Swap: 768.00 MiB used of 6.00 GiB",
                "zram: 512.00 MiB of 2.00 GiB",
                "Disk-backed swap: 256.00 MiB of 4.00 GiB",
            ),
        )

    def test_swap_details_handles_zram_only(self) -> None:
        scanner = make_scanner()
        devices = [("/dev/zram0", 2 * 1024**3, 1 * 1024**3)]

        self.assertEqual(
            scanner._swap_details(None, devices),
            (
                "Swap: 1.00 GiB used of 2.00 GiB",
                "zram: 1.00 GiB of 2.00 GiB",
            ),
        )

    def test_swap_devices_parses_linux_table_and_skips_malformed(self) -> None:
        table = (
            "Filename\tType\tSize\tUsed\tPriority\n"
            "/dev/zram0\tpartition\t2048\t512\t100\n"
            "/swapfile\tfile\t4096\t0\t-2\n"
            "broken-line\n"
            "/dev/nvme0n1p9\tpartition\tnot-a-number\t10\t-3\n"
        )

        with (
            patch("maintenance.scanner.platform.system", return_value="Linux"),
            patch("maintenance.scanner.Path.read_text", return_value=table),
        ):
            self.assertEqual(
                SystemScanner._swap_devices(),
                [
                    ("/dev/zram0", 2048 * 1024, 512 * 1024),
                    ("/swapfile", 4096 * 1024, 0),
                ],
            )

    def test_swap_devices_returns_empty_off_linux_or_on_read_failure(self) -> None:
        with patch("maintenance.scanner.platform.system", return_value="Darwin"):
            self.assertEqual(SystemScanner._swap_devices(), [])

        with (
            patch("maintenance.scanner.platform.system", return_value="Linux"),
            patch(
                "maintenance.scanner.Path.read_text",
                side_effect=OSError("permission denied"),
            ),
        ):
            self.assertEqual(SystemScanner._swap_devices(), [])

    def test_memory_card_shows_zram_breakdown(self) -> None:
        scanner = make_scanner()
        fake_psutil = make_baseline_psutil()

        with scanner_environment(
            scanner,
            fake_psutil,
            trash_size=0,
            swap_devices=[("/dev/zram0", 2 * 1024**3, 1 * 1024**3)],
        ):
            snapshot = scanner.scan_dashboard()

        memory = snapshot.get("memory")
        self.assertEqual(memory.value, "50.0%")
        self.assertIn("Physical RAM: 16.00 GiB", memory.details)
        self.assertIn("zram: 1.00 GiB of 2.00 GiB", memory.details)
        self.assertNotIn("Disk-backed swap", memory.details)

    def test_default_downloads_path_uses_downloads_path_resolver(self) -> None:
        sentinel = Path("/tmp/downloads")

        with patch(
            "maintenance.scanner.DownloadsPathResolver.select",
            return_value=sentinel,
        ) as select:
            self.assertEqual(SystemScanner._default_downloads_path(), sentinel)

        select.assert_called_once_with()

    def test_windows_known_folder_is_used_before_environment_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            known_folder = Path(directory)
            with (
                patch("maintenance.scanner.platform.system", return_value="Windows"),
                patch.object(
                    SystemScanner,
                    "_windows_downloads_path",
                    return_value=known_folder,
                ),
            ):
                self.assertEqual(
                    SystemScanner._default_downloads_path(),
                    known_folder,
                )

    def test_missing_windows_known_folder_uses_existing_onedrive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            one_drive = Path(directory)
            downloads = one_drive / "Downloads"
            downloads.mkdir()
            malformed_known_folder = one_drive / "KnownFolderFile"
            malformed_known_folder.write_text("not a directory")
            with (
                patch("maintenance.scanner.platform.system", return_value="Windows"),
                patch.object(
                    SystemScanner,
                    "_windows_downloads_path",
                    return_value=malformed_known_folder,
                ),
                patch.dict(
                    os.environ,
                    {
                        "USERPROFILE": str(one_drive / "Profile"),
                        "OneDrive": str(one_drive),
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(SystemScanner._default_downloads_path(), downloads)

    def test_file_home_downloads_falls_back_to_unavailable_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "Downloads").write_text("not a directory")
            with (
                patch("maintenance.scanner.platform.system", return_value="Windows"),
                patch.object(
                    SystemScanner, "_windows_downloads_path", return_value=None
                ),
                patch.dict(
                    os.environ,
                    {"USERPROFILE": str(home), "OneDrive": str(home / "Missing")},
                    clear=False,
                ),
            ):
                result = SystemScanner._default_downloads_path()

            self.assertEqual(result.name, "Downloads.__unavailable__")
            self.assertFalse(result.is_file())

    def test_windows_recycle_bin_size_uses_shell_query(self) -> None:
        class FakeQuery:
            restype: object = None

            def __call__(self, _root: object, info_pointer: Any) -> int:
                info_pointer._obj.size = 1234
                return 0

        fake_windll: Any = type(
            "FakeWindll",
            (),
            {"shell32": type("FakeShell", (), {"SHQueryRecycleBinW": FakeQuery()})()},
        )()

        with (
            patch("maintenance.scanner.platform.system", return_value="Windows"),
            patch.object(
                scan_support_module.ctypes,
                "windll",
                fake_windll,
                create=True,
            ),
        ):
            self.assertEqual(SystemScanner().trash_size(), 1234)
            self.assertIsNotNone(fake_windll.shell32.SHQueryRecycleBinW.argtypes)


class SharedWalkDirectoryTests(unittest.TestCase):
    def test_download_walk_skips_hidden_files_and_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            visible = root / "visible.bin"
            hidden_dir = root / ".hidden"
            hidden_file = hidden_dir / "secret.bin"
            hidden_dir.mkdir()
            visible.write_bytes(b"visible contents")
            hidden_file.write_bytes(b"hidden contents")

            scanner = SystemScanner(root)
            stats = scanner._download_file_stats(root)

            self.assertEqual(set(stats), {visible})

    def test_trash_walk_counts_hidden_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            visible = root / "visible.bin"
            hidden_dir = root / ".hidden"
            hidden_file = hidden_dir / "secret.bin"
            hidden_dir.mkdir()
            visible.write_bytes(b"visible contents")
            hidden_file.write_bytes(b"hidden contents")

            self.assertEqual(
                SystemScanner._directory_file_size(root),
                len(b"visible contents") + len(b"hidden contents"),
            )

    def test_walk_never_descends_into_symlinked_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_dir = root / "real"
            real_dir.mkdir()
            real_file = real_dir / "inside.bin"
            real_file.write_bytes(b"inside contents")
            link = root / "link"
            try:
                link.symlink_to(real_dir, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not supported on this filesystem")

            scanner = SystemScanner(root)
            stats = scanner._download_file_stats(root)
            trash_size = SystemScanner._directory_file_size(root)

            self.assertEqual(set(stats), {real_file})
            self.assertEqual(trash_size, len(b"inside contents"))

    def test_download_walk_stats_values_match_file_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.txt"
            second = root / "b.txt"
            first.write_bytes(b"aaaa")
            second.write_bytes(b"bbbbbb")

            scanner = SystemScanner(root)
            stats = scanner._download_file_stats(root)

            self.assertEqual(stats[first].st_size, 4)
            self.assertEqual(stats[second].st_size, 6)


class FileManagerTests(unittest.TestCase):
    def test_file_root_cannot_be_used_as_trash_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_file = Path(directory) / "Downloads"
            root_file.write_text("not a directory")
            moved: list[str] = []
            manager = FileManager(root_file)

            with patch("maintenance.actions.send2trash", moved.append):
                result = manager.move_to_trash([root_file])

            self.assertEqual(result.moved, ())
            self.assertEqual(moved, [])

    def test_only_files_inside_allowed_root_can_move_to_trash(self) -> None:
        with (
            tempfile.TemporaryDirectory() as allowed_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            allowed = Path(allowed_directory)
            inside = allowed / "inside.txt"
            outside = Path(outside_directory) / "outside.txt"
            inside.write_text("inside")
            outside.write_text("outside")
            moved: list[str] = []

            manager = FileManager(allowed)
            with patch("maintenance.actions.send2trash", moved.append):
                result = manager.move_to_trash([inside, outside])

            self.assertEqual(result.moved, (inside.resolve(),))
            self.assertEqual(moved, [str(inside.resolve())])
            self.assertEqual(len(result.errors), 1)


class ProcessManagerTests(unittest.TestCase):
    def test_current_process_is_protected_and_user_process_can_quit(self) -> None:
        current = FakeProcess(os.getpid(), "python", getpass.getuser())
        allowed = FakeProcess(50001, "Example App", getpass.getuser())
        fake_psutil = FakePsutil({os.getpid(): current, 50001: allowed})

        with patch("maintenance.actions.psutil", fake_psutil):
            result = ProcessManager().request_quit([os.getpid(), 50001])

        self.assertFalse(current.terminated)
        self.assertTrue(allowed.terminated)
        self.assertEqual(result.stopped, (50001,))
        self.assertTrue(any("protected" in error for error in result.errors))

    def test_empty_quit_request_returns_without_waiting(self) -> None:
        current = FakeProcess(os.getpid(), "python", getpass.getuser())
        fake_psutil = FakePsutil({os.getpid(): current})

        with patch("maintenance.actions.psutil", fake_psutil):
            result = ProcessManager().request_quit([])

        self.assertEqual(result.requested, 0)
        self.assertEqual(result.stopped, ())
        self.assertEqual(result.force_required, ())
        self.assertEqual(result.errors, ())
        self.assertEqual(fake_psutil.wait_procs_calls, 0)


class SourceIntegrationTests(unittest.TestCase):
    def test_attached_analyzer_exposes_dashboard_methods(self) -> None:
        path = Path(__file__).parents[1] / "algo.py"
        spec = importlib.util.spec_from_file_location("attached_algo", path)
        if spec is None or spec.loader is None:
            self.fail("Could not load algo.py for the integration test")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        analyzer = module.Analyzer()
        self.assertTrue(callable(analyzer.dashboard_snapshot))
        self.assertTrue(callable(analyzer.component_summary))
        self.assertTrue(callable(analyzer.process_candidates))
        self.assertTrue(callable(analyzer.storage_candidates))
        self.assertTrue(callable(analyzer.reset_component_sample))
        self.assertTrue(callable(analyzer.stop_background_workers))

    def test_legacy_report_methods_are_removed(self) -> None:
        from algo import Analyzer

        for name in (
            "cpu_info",
            "memory_info",
            "storage_info",
            "gpu_info",
            "network_info",
            "battery_info",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(Analyzer, name))

    def test_call_with_cancel_passes_event_only_when_provided(self) -> None:
        from algo import Analyzer

        analyzer = Analyzer()
        func = Mock(return_value="ok")

        self.assertEqual(analyzer._call_with_cancel(func, None), "ok")
        func.assert_called_once_with()

        cancel_event = threading.Event()
        self.assertEqual(analyzer._call_with_cancel(func, cancel_event), "ok")
        func.assert_called_with(cancel_event=cancel_event)

    def test_reset_component_sample_ignores_non_network_components(self) -> None:
        from algo import Analyzer

        analyzer = Analyzer()
        with patch.object(analyzer.scanner, "_reset_network_sample") as reset:
            analyzer.reset_component_sample("cpu")

        reset.assert_not_called()

    def test_analyzer_constructor_rejects_removed_compatibility_args(self) -> None:
        from algo import Analyzer

        analyzer = cast(Any, Analyzer)
        with self.assertRaises(TypeError):
            analyzer(memory_test_percent=1.0)


if __name__ == "__main__":
    unittest.main()
