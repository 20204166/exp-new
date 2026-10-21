from tests.support.scanner import make_scanner

"""Focused tests for the concise GPU display name on the overview card."""

import unittest
from unittest import mock

from maintenance.components.gpu import (
    GPU_INFORMATION_UNAVAILABLE,
    GpuProbe,
    gpu_probe_from_read,
)
from maintenance.models import CapabilityState
from maintenance.scanner import SystemScanner


class GpuDisplayNameTests(unittest.TestCase):
    def test_concise_name_rewrites_vendor_and_bracket_forms(self) -> None:
        cases = (
            (
                "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 5700 XT (rev c1)",
                "Radeon RX 5700 XT",
            ),
            (
                "Advanced Micro Devices, Inc. [AMD/ATI] Barts PRO [Radeon HD 6850]",
                "Radeon HD 6850",
            ),
            (
                "NVIDIA Corporation GL104 [GeForce RTX 4080] (rev a1)",
                "GeForce RTX 4080",
            ),
            ("Intel Corporation UHD Graphics 620 (rev 02)", "UHD Graphics 620"),
            (
                "Intel Corporation Device [WhiskeyLake-U GT2 [UHD Graphics 620]]",
                "UHD Graphics 620",
            ),
            ("Advanced Micro Devices, Inc., Radeon RX 5700 XT", "Radeon RX 5700 XT"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(SystemScanner._concise_gpu_name(raw), expected)

    def test_plain_short_names_pass_through(self) -> None:
        for name in (
            "Test GPU",
            "GPU One",
            "NVIDIA GeForce RTX 4090",
            "AMD Radeon Pro 5500M",
            "NVIDIA GeForce RTX 3060 Laptop GPU",
            "Intel Iris",
        ):
            with self.subTest(name=name):
                self.assertEqual(SystemScanner._concise_gpu_name(name), None)

    def test_unavailable_messages_pass_through(self) -> None:
        for name in (
            "GPU information unavailable",
            "GPU information unavailable: query timed out",
        ):
            with self.subTest(name=name):
                self.assertEqual(SystemScanner._concise_gpu_name(name), None)

    def test_unavailable_gpu_card_uses_information_subtitle(self) -> None:
        scanner = make_scanner()

        for message in (
            "GPU information unavailable",
            "GPU information unavailable: query timed out",
        ):
            with self.subTest(message=message):
                with mock.patch.object(
                    scanner,
                    "gpu_details",
                    return_value=(message,),
                ):
                    resource = scanner.scan_component("gpu")

                self.assertEqual(resource.value, message)
                self.assertEqual(resource.subtitle, "Information unavailable")
                self.assertEqual(resource.details[0], message)
                # Transient GPU failures are flagged so the window's
                # keep-last-valid merge shields a previously healthy card.
                self.assertTrue(resource.failed)

    def test_healthy_gpu_card_keeps_graphics_hardware_subtitle(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=("NVIDIA GeForce RTX 4090",),
        ):
            resource = scanner.scan_component("gpu")

        self.assertEqual(resource.subtitle, "Graphics hardware")

    def test_vendorless_unknown_line_passes_through(self) -> None:
        self.assertEqual(SystemScanner._concise_gpu_name("Unknown GPU"), None)

    def test_display_name_falls_back_to_raw_when_not_rewritten(self) -> None:
        self.assertEqual(SystemScanner._gpu_display_name(("Test GPU",)), "Test GPU")
        self.assertEqual(
            SystemScanner._gpu_display_name(("GPU information unavailable",)),
            "GPU information unavailable",
        )

    def test_display_name_keeps_raw_identifier_in_details(self) -> None:
        scanner = make_scanner()
        raw = "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 5700 XT (rev c1)"

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=(raw, "GPU usage: 12%", "Memory: 4.00 GiB used of 8.00 GiB"),
        ):
            resource = scanner.scan_component("gpu")

        self.assertEqual(resource.value, "Radeon RX 5700 XT")
        self.assertIn(raw, resource.details)
        self.assertIn("GPU usage: 12%", resource.details)

    def test_dashboard_gpu_card_uses_concise_value_and_raw_details(self) -> None:
        scanner = make_scanner()
        raw = "NVIDIA Corporation GL104 [GeForce RTX 4080] (rev a1)"

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=(raw, "Memory: 4.00 GiB used of 8.00 GiB"),
        ):
            snapshot = scanner.scan_dashboard()

        gpu = snapshot.get("gpu")
        self.assertEqual(gpu.value, "GeForce RTX 4080")
        self.assertEqual(gpu.details[0], raw)
        self.assertEqual(gpu.subtitle, "Graphics hardware")
        self.assertIsNone(gpu.percent)

    def test_component_and_dashboard_share_the_concise_value(self) -> None:
        scanner = make_scanner()
        raw = "Intel Corporation UHD Graphics 620 (rev 02)"

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=(raw,),
        ):
            component = scanner.scan_component("gpu")
            dashboard = scanner.scan_dashboard().get("gpu")

        self.assertEqual(component.value, "UHD Graphics 620")
        self.assertEqual(dashboard.value, component.value)

    def test_patched_gpu_details_yield_unknown_capability(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=("NVIDIA GeForce RTX 4090",),
        ):
            resource = scanner.scan_component("gpu")

        self.assertEqual(resource.value, "NVIDIA GeForce RTX 4090")
        self.assertEqual(resource.capability, CapabilityState.UNKNOWN)

    def test_linux_probe_classifies_absent_gpu_as_unsupported(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            SystemScanner,
            "_linux_gpu_read",
            return_value=((), None),
        ):
            probe = scanner._linux_gpu_probe()

        self.assertEqual(probe.details, ("GPU information unavailable",))
        self.assertEqual(probe.capability, CapabilityState.UNSUPPORTED)

    def test_linux_probe_classifies_error_as_temporarily_unavailable(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            SystemScanner,
            "_linux_gpu_read",
            return_value=((), "lspci: command not found"),
        ):
            probe = scanner._linux_gpu_probe()

        self.assertEqual(probe.capability, CapabilityState.TEMPORARILY_UNAVAILABLE)

    def test_linux_probe_classifies_found_gpu_as_supported(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            SystemScanner,
            "_linux_gpu_read",
            return_value=(("AMD Radeon RX 5700 XT",), None),
        ):
            probe = scanner._linux_gpu_probe()

        self.assertEqual(probe.details, ("AMD Radeon RX 5700 XT",))
        self.assertEqual(probe.capability, CapabilityState.SUPPORTED)

    def test_multi_gpu_keeps_all_raw_lines_in_details(self) -> None:
        scanner = make_scanner()

        with mock.patch.object(
            scanner,
            "gpu_details",
            return_value=(
                "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 5700 XT (rev c1)",
                "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 6900 XT (rev c1)",
                "Memory: 4 GiB",
            ),
        ):
            resource = scanner.scan_component("gpu")

        self.assertEqual(resource.value, "Radeon RX 5700 XT")
        self.assertIn(
            "Advanced Micro Devices, Inc. [AMD/ATI] Radeon RX 6900 XT (rev c1)",
            resource.details,
        )
        self.assertIn("Memory: 4 GiB", resource.details)


class GpuProbeFromReadTests(unittest.TestCase):
    def test_error_classifies_as_temporarily_unavailable_with_message(self) -> None:
        probe = gpu_probe_from_read(lambda: ((), "lspci: command not found"))

        self.assertEqual(probe.details, (GPU_INFORMATION_UNAVAILABLE,))
        self.assertEqual(probe.capability, CapabilityState.TEMPORARILY_UNAVAILABLE)

    def test_authoritative_empty_classifies_as_unsupported(self) -> None:
        probe = gpu_probe_from_read(lambda: ((), None))

        self.assertEqual(probe.details, (GPU_INFORMATION_UNAVAILABLE,))
        self.assertEqual(probe.capability, CapabilityState.UNSUPPORTED)

    def test_detail_lines_classify_as_supported(self) -> None:
        probe = gpu_probe_from_read(
            lambda: (("AMD Radeon RX 5700 XT", "Memory: 4 GiB"), None)
        )

        self.assertIsInstance(probe, GpuProbe)
        self.assertEqual(probe.details, ("AMD Radeon RX 5700 XT", "Memory: 4 GiB"))
        self.assertEqual(probe.capability, CapabilityState.SUPPORTED)

    def test_all_three_platform_probes_share_the_classification(self) -> None:
        scanner = make_scanner()

        with (
            mock.patch.object(
                SystemScanner,
                "_mac_gpu_read",
                return_value=((), "system_profiler failed"),
            ),
            mock.patch.object(
                SystemScanner,
                "_windows_gpu_read",
                return_value=((), None),
            ),
            mock.patch.object(
                SystemScanner,
                "_linux_gpu_read",
                return_value=(("NVIDIA Corporation GeForce RTX 4080",), None),
            ),
        ):
            mac = scanner._mac_gpu_probe()
            windows = scanner._windows_gpu_probe()
            linux = scanner._linux_gpu_probe()

        self.assertEqual(mac.capability, CapabilityState.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(windows.capability, CapabilityState.UNSUPPORTED)
        self.assertEqual(linux.capability, CapabilityState.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
