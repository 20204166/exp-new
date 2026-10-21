"""Focused tests for the Network card: rate deltas, interfaces, VPN."""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from maintenance.models import CapabilityState
from maintenance.scanner import SystemScanner
from tests.support.scanner import (
    make_baseline_psutil,
    make_scanner,
    scanner_environment,
)


def _counters(sent: int, received: int) -> SimpleNamespace:
    return SimpleNamespace(bytes_sent=sent, bytes_recv=received)


def _sensor_psutil(net_io_counters: Any) -> SimpleNamespace:
    return make_baseline_psutil(
        cpu_percent=lambda interval: 10.0,
        net_io_counters=net_io_counters,
    )


class NetworkRateTests(unittest.TestCase):
    def test_rate_pair_uses_one_shared_unit(self) -> None:
        self.assertEqual(
            SystemScanner._rate_pair_text(1382, 573),
            ("1.35 KiB/s", "0.56 KiB/s"),
        )

    def test_rate_pair_scales_with_the_larger_rate(self) -> None:
        self.assertEqual(
            SystemScanner._rate_pair_text(3 * 1024**2, 512 * 1024),
            ("3.00 MiB/s", "0.50 MiB/s"),
        )

    def test_rate_pair_handles_zero_and_bytes_scale(self) -> None:
        self.assertEqual(
            SystemScanner._rate_pair_text(0.0, 0.0),
            ("0.00 B/s", "0.00 B/s"),
        )
        self.assertEqual(
            SystemScanner._rate_pair_text(50, 10),
            ("50.00 B/s", "10.00 B/s"),
        )

    def test_network_rates_are_deltas_over_time(self) -> None:
        scanner = make_scanner()

        with patch("maintenance.scanner.time.monotonic", side_effect=[100.0, 101.0]):
            self.assertEqual(
                scanner._sample_network_rates(_counters(0, 0)),
                (None, None),
            )
            down, up = scanner._sample_network_rates(_counters(512 * 1024, 1024 * 1024))

        self.assertEqual(down, 1024 * 1024)
        self.assertEqual(up, 512 * 1024)

    def test_network_rates_clamp_counter_reset_to_zero(self) -> None:
        scanner = make_scanner()

        with patch("maintenance.scanner.time.monotonic", side_effect=[100.0, 101.0]):
            scanner._sample_network_rates(_counters(1000, 2000))
            down, up = scanner._sample_network_rates(_counters(10, 20))

        self.assertEqual(down, 0.0)
        self.assertEqual(up, 0.0)

    def test_network_rates_return_none_for_zero_elapsed(self) -> None:
        scanner = make_scanner()

        with patch("maintenance.scanner.time.monotonic", side_effect=[100.0, 100.0]):
            scanner._sample_network_rates(_counters(0, 0))
            self.assertEqual(
                scanner._sample_network_rates(_counters(10, 10)),
                (None, None),
            )


class NetworkInterfaceTests(unittest.TestCase):
    def test_up_interfaces_returns_up_names(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {
                "eth0": SimpleNamespace(isup=True),
                "wlan0": SimpleNamespace(isup=False),
            },
        )

        self.assertEqual(SystemScanner._up_interfaces(fake), {"eth0"})

    def test_up_interfaces_returns_none_on_failure(self) -> None:
        def raises() -> Any:
            raise OSError("boom")

        fake = SimpleNamespace(net_if_stats=raises)

        self.assertIsNone(SystemScanner._up_interfaces(fake))

    def test_up_interfaces_returns_empty_when_none_up(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {"eth0": SimpleNamespace(isup=False)},
        )

        self.assertEqual(SystemScanner._up_interfaces(fake), set())

    def test_active_interface_prefers_busiest_up_non_loopback(self) -> None:
        fake = SimpleNamespace(
            net_io_counters=lambda pernic: {
                "lo": _counters(0, 0),
                "eth0": _counters(1000, 2000),
                "wlan0": _counters(50, 60),
            },
            net_if_stats=lambda: {
                "lo": SimpleNamespace(isup=True),
                "eth0": SimpleNamespace(isup=True),
                "wlan0": SimpleNamespace(isup=True),
            },
        )

        self.assertEqual(SystemScanner._active_interface(fake), "eth0")

    def test_active_interface_excludes_loopback_only_state(self) -> None:
        fake = SimpleNamespace(
            net_io_counters=lambda pernic: {"lo": _counters(1, 2)},
            net_if_stats=lambda: {"lo": SimpleNamespace(isup=True)},
        )

        self.assertIsNone(SystemScanner._active_interface(fake))

    def test_active_interface_returns_none_when_no_interface_is_up(self) -> None:
        fake = SimpleNamespace(
            net_io_counters=lambda pernic: {"eth0": _counters(100, 200)},
            net_if_stats=lambda: {"eth0": SimpleNamespace(isup=False)},
        )

        self.assertIsNone(SystemScanner._active_interface(fake))

    def test_active_interface_falls_back_when_stats_missing(self) -> None:
        fake = SimpleNamespace(
            net_io_counters=lambda pernic: {"eth0": _counters(100, 200)},
        )

        self.assertEqual(SystemScanner._active_interface(fake), "eth0")

    def test_active_interface_returns_none_when_pernic_fails(self) -> None:
        def broken_pernic(*_args: Any, **_kwargs: Any) -> Any:
            raise PermissionError("denied")

        fake = SimpleNamespace(net_io_counters=broken_pernic)

        self.assertIsNone(SystemScanner._active_interface(fake))


class VpnDetectionTests(unittest.TestCase):
    def test_vpn_interface_detects_up_tunnel(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {
                "eth0": SimpleNamespace(isup=True),
                "tun0": SimpleNamespace(isup=True),
            },
        )

        self.assertEqual(SystemScanner._vpn_interface(fake), "tun0")

    def test_vpn_interface_detects_macos_utun_prefix(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {
                "en0": SimpleNamespace(isup=True),
                "utun3": SimpleNamespace(isup=True),
            },
        )

        self.assertEqual(SystemScanner._vpn_interface(fake), "utun3")

    def test_vpn_interface_ignores_down_tunnel(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {
                "eth0": SimpleNamespace(isup=True),
                "tun0": SimpleNamespace(isup=False),
            },
        )

        self.assertIsNone(SystemScanner._vpn_interface(fake))

    def test_vpn_interface_accepts_precomputed_up_interfaces(self) -> None:
        fake = SimpleNamespace()
        precomputed = {"eth0", "tun0"}

        self.assertEqual(
            SystemScanner._vpn_interface(fake, up_interfaces=precomputed),
            "tun0",
        )
        self.assertIsNone(
            SystemScanner._vpn_interface(fake, up_interfaces={"eth0"}),
        )

    def test_vpn_interface_returns_none_without_tunnel(self) -> None:
        fake = SimpleNamespace(
            net_if_stats=lambda: {
                "eth0": SimpleNamespace(isup=True),
                "wlan0": SimpleNamespace(isup=True),
            },
        )

        self.assertIsNone(SystemScanner._vpn_interface(fake))

    def test_vpn_interface_tolerates_missing_stats(self) -> None:
        fake = SimpleNamespace()

        self.assertIsNone(SystemScanner._vpn_interface(fake))


class NetworkObservationConsolidationTests(unittest.TestCase):
    def test_network_scan_reads_each_sensor_exactly_once(self) -> None:
        scanner = make_scanner()
        calls = {"global": 0, "pernic": 0, "stats": 0}

        def net_io_counters(pernic: bool = False) -> Any:
            if pernic:
                calls["pernic"] += 1
                return {"eth0": _counters(1000, 2000)}
            calls["global"] += 1
            return _counters(1000, 2000)

        def net_if_stats() -> Any:
            calls["stats"] += 1
            return {"eth0": SimpleNamespace(isup=True)}

        fake = _sensor_psutil(net_io_counters)
        fake.net_if_stats = net_if_stats

        with scanner_environment(scanner, fake, trash_size=0):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(calls, {"global": 1, "pernic": 1, "stats": 1})
        network = snapshot.get("network")
        self.assertEqual(network.subtitle, "Active: eth0")
        self.assertIn("Active interface: eth0", network.details)

    def test_network_observation_reuses_one_read_for_capability_and_rendering(
        self,
    ) -> None:
        scanner = make_scanner()
        pernic_reads: list[bool] = []
        stats_reads: list[bool] = []

        def net_io_counters(pernic: bool = False) -> Any:
            pernic_reads.append(pernic)
            if pernic:
                return {
                    "lo": _counters(0, 0),
                    "eth0": _counters(512 * 1024, 1024 * 1024),
                }
            return _counters(512 * 1024, 1024 * 1024)

        def net_if_stats() -> Any:
            stats_reads.append(True)
            return {
                "lo": SimpleNamespace(isup=True),
                "eth0": SimpleNamespace(isup=True),
            }

        fake = _sensor_psutil(net_io_counters)
        fake.net_if_stats = net_if_stats

        with scanner_environment(scanner, fake, trash_size=0):
            snapshot = scanner.scan_component("network")

        self.assertEqual(pernic_reads, [False, True])
        self.assertEqual(stats_reads, [True])
        self.assertEqual(snapshot.capability, CapabilityState.SUPPORTED)
        self.assertEqual(snapshot.subtitle, "Active: eth0")

    def test_observation_path_distinguishes_failed_read_from_empty_state(self) -> None:
        scanner = make_scanner()

        def net_io_counters(pernic: bool = False) -> Any:
            if pernic:
                raise PermissionError("denied")
            return _counters(100, 200)

        fake = _sensor_psutil(net_io_counters)

        with scanner_environment(scanner, fake, trash_size=0):
            snapshot = scanner.scan_component("network")

        self.assertEqual(snapshot.subtitle, "No active interface")
        self.assertIn("Active interface: Unknown", snapshot.details)

        def net_if_stats() -> Any:
            return {"eth0": SimpleNamespace(isup=False)}

        fake.net_if_stats = net_if_stats

        with scanner_environment(scanner, fake, trash_size=0):
            snapshot = scanner.scan_component("network")

        self.assertEqual(snapshot.subtitle, "Disconnected")
        self.assertIn("Active interface: none", snapshot.details)


class NetworkCardTests(unittest.TestCase):
    def test_network_card_value_shows_rate_from_previous_sample(self) -> None:
        scanner = make_scanner()
        scanner._network_sample = (100.0, 0, 0)
        fake = _sensor_psutil(
            lambda: _counters(512 * 1024, 1024 * 1024),
        )

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
            monotonic=101.0,
        ):
            snapshot = scanner.scan_dashboard()

        network = snapshot.get("network")
        self.assertEqual(network.value, "↓ 1.00 MiB/s  ↑ 0.50 MiB/s")
        self.assertIn("Download rate: 1.00 MiB/s", network.details)
        self.assertIn("Upload rate: 0.50 MiB/s", network.details)
        self.assertIn("Received this boot: 1.00 MiB", network.details)
        self.assertIn("Sent this boot: 512.00 KiB", network.details)
        self.assertIn("VPN: Not detected", network.details)

    def test_network_card_reports_vpn_and_interface(self) -> None:
        scanner = make_scanner()
        scanner._network_sample = (100.0, 0, 0)
        fake = _sensor_psutil(
            lambda: _counters(512 * 1024, 1024 * 1024),
        )

        def net_io_counters(pernic: bool = False):
            if pernic:
                return {
                    "lo": _counters(0, 0),
                    "eth0": _counters(512 * 1024, 1024 * 1024),
                    "tun0": _counters(0, 0),
                }
            return _counters(512 * 1024, 1024 * 1024)

        fake.net_io_counters = net_io_counters
        fake.net_if_stats = lambda: {
            "lo": SimpleNamespace(isup=True),
            "eth0": SimpleNamespace(isup=True),
            "tun0": SimpleNamespace(isup=True),
        }

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
            monotonic=101.0,
        ):
            snapshot = scanner.scan_dashboard()

        network = snapshot.get("network")
        self.assertEqual(network.subtitle, "Active: eth0")
        self.assertIn("Active interface: eth0", network.details)
        self.assertIn("VPN: Connected", network.details)

    def test_network_card_disconnected_state_is_intentional(self) -> None:
        scanner = make_scanner()
        fake = _sensor_psutil(
            lambda pernic=False: (
                {"eth0": _counters(100, 200)} if pernic else _counters(100, 200)
            )
        )
        fake.net_if_stats = lambda: {"eth0": SimpleNamespace(isup=False)}

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
        ):
            snapshot = scanner.scan_dashboard()

        network = snapshot.get("network")
        self.assertEqual(network.subtitle, "Disconnected")
        self.assertIn("Active interface: none", network.details)

    def test_network_card_unknown_state_when_stats_unreadable(self) -> None:
        scanner = make_scanner()

        def net_io_counters(pernic: bool = False) -> Any:
            if pernic:
                raise PermissionError("denied")
            return _counters(100, 200)

        fake = _sensor_psutil(net_io_counters)

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
        ):
            snapshot = scanner.scan_dashboard()

        network = snapshot.get("network")
        self.assertEqual(network.subtitle, "No active interface")
        self.assertIn("Active interface: Unknown", network.details)

    def test_network_capability_supported_when_global_counters_work(self) -> None:
        scanner = make_scanner()
        fake = _sensor_psutil(lambda: _counters(100, 200))
        fake.net_if_stats = lambda: {"eth0": SimpleNamespace(isup=False)}

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
        ):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(
            snapshot.get("network").capability,
            CapabilityState.SUPPORTED,
        )

    def test_network_capability_temporarily_unavailable_when_counters_fail(
        self,
    ) -> None:
        scanner = make_scanner()

        def broken() -> Any:
            raise PermissionError("denied")

        fake = _sensor_psutil(broken)

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
        ):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(
            snapshot.get("network").capability,
            CapabilityState.TEMPORARILY_UNAVAILABLE,
        )

    def test_network_capability_unsupported_when_loopback_only(self) -> None:
        scanner = make_scanner()

        def net_io_counters(pernic: bool = False) -> Any:
            if pernic:
                return {"lo": _counters(0, 0)}
            return _counters(0, 0)

        fake = _sensor_psutil(net_io_counters)
        fake.net_if_stats = lambda: {"lo": SimpleNamespace(isup=True)}

        with scanner_environment(
            scanner,
            fake,
            trash_size=0,
        ):
            snapshot = scanner.scan_dashboard()

        self.assertEqual(
            snapshot.get("network").capability,
            CapabilityState.UNSUPPORTED,
        )


if __name__ == "__main__":
    unittest.main()
