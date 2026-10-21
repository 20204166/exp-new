"""Focused tests for non-Linux thermal provider acquisition."""

import json
import subprocess
import unittest
from unittest.mock import patch

from maintenance.scanner_support import temperature_platform


def _completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args="powershell", returncode=0, stdout=json.dumps(payload), stderr=""
    )


class WindowsTemperatureProviderTests(unittest.TestCase):
    def test_acpi_converts_zones_and_stops_before_optional_providers(self) -> None:
        calls: list[str] = []

        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command[-1])
            return _completed(
                {
                    "InstanceName": "ACPI\\ThermalZone\\TZ00_0",
                    "CurrentTemperature": 3015,
                }
            )

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(scan.lines, ("CPU: 28°C",))
        sample = dict(scan.samples_by_component)["cpu"][0]
        self.assertAlmostEqual(sample.value_celsius, 28.35, places=2)
        self.assertEqual(sample.sensor_id, "acpi:ACPI\\ThermalZone\\TZ00_0")
        self.assertEqual(len(calls), 1)
        self.assertIn("MSAcpi_ThermalZoneTemperature", calls[0])

    def test_lhm_is_used_when_acpi_fails(self) -> None:
        commands: list[str] = []

        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command[-1])
            if "MSAcpi_ThermalZoneTemperature" in command[-1]:
                raise subprocess.CalledProcessError(1, command)
            return _completed(
                [
                    {
                        "Name": "CPU Package",
                        "SensorType": "Temperature",
                        "Value": 62.5,
                        "Identifier": "/cpu/0",
                    }
                ]
            )

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(scan.lines, ("CPU: 62°C",))
        sample = dict(scan.samples_by_component)["cpu"][0]
        self.assertEqual(sample.sensor_id, "lhm:/cpu/0")
        self.assertEqual(len(commands), 2)
        self.assertIn("LibreHardwareMonitor", commands[1])

    def test_ohm_is_used_when_acpi_and_lhm_are_unavailable(self) -> None:
        commands: list[str] = []

        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command[-1])
            if "OpenHardwareMonitor" not in command[-1]:
                raise subprocess.CalledProcessError(1, command)
            return _completed(
                [
                    {
                        "Name": "GPU Core",
                        "SensorType": "Temperature",
                        "Value": 55.0,
                        "Parent": "/gpu/0",
                    }
                ]
            )

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(scan.lines, ("GPU: 55°C",))
        sample = dict(scan.samples_by_component)["gpu"][0]
        self.assertEqual(sample.sensor_id, "ohm:/gpu/0")
        self.assertIn("OpenHardwareMonitor", commands[-1])

    def test_optional_provider_parser_classifies_conservatively(self) -> None:
        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "MSAcpi" in command[-1]:
                return _completed([])
            return _completed(
                [
                    {"Name": "NVMe Disk", "SensorType": "Temperature", "Value": 40},
                    {"Name": "Battery", "SensorType": "Temperature", "Value": 31},
                    {"Name": "Fan", "SensorType": "Fan", "Value": 99},
                    {"Name": "Mystery", "SensorType": "Temperature", "Value": 44},
                    {"Name": "GPU Hot", "SensorType": "Temperature", "Value": 0},
                ]
            )

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(
            tuple(component for component, _ in scan.samples_by_component),
            ("storage", "battery"),
        )

    def test_all_provider_failures_return_empty_scan(self) -> None:
        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, command)

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(scan.lines, ())
        self.assertEqual(scan.samples_by_component, ())
        self.assertIsNone(scan.unavailable_reason)

    def test_acpi_access_denied_produces_a_clear_reason(self) -> None:
        # A non-terminating Get-CimInstance error still exits 0 with empty
        # stdout, so the command wraps it in try/catch and emits {"error":
        # ...} on stdout instead of losing it -- this is what that looks
        # like once ACPI is genuinely present but permission-blocked, and
        # neither optional provider is installed.
        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "MSAcpi_ThermalZoneTemperature" in command[-1]:
                return _completed({"error": "Access denied"})
            raise subprocess.CalledProcessError(1, command)

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertEqual(scan.lines, ())
        self.assertEqual(scan.samples_by_component, ())
        self.assertEqual(
            scan.unavailable_reason,
            "CPU temperature requires administrator privileges",
        )

    def test_acpi_non_permission_error_does_not_set_a_reason(self) -> None:
        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "MSAcpi_ThermalZoneTemperature" in command[-1]:
                return _completed({"error": "Invalid namespace"})
            raise subprocess.CalledProcessError(1, command)

        scan = temperature_platform.windows_temperature_scan(runner=runner)

        self.assertIsNone(scan.unavailable_reason)


class MacosTemperatureProviderTests(unittest.TestCase):
    def test_wraps_smc_readings_into_the_shared_scan_shape(self) -> None:
        with patch.object(
            temperature_platform,
            "read_smc_temperatures",
            return_value={
                "cpu": [("smc:TC0P", "TC0P", 61.5)],
                "battery": [("smc:TB0T", "TB0T", 32.25)],
            },
        ):
            scan = temperature_platform.macos_temperature_scan()

        self.assertEqual(scan.lines, ("CPU: 62°C", "Battery: 32°C"))
        self.assertEqual(
            tuple(component for component, _ in scan.samples_by_component),
            ("cpu", "battery"),
        )


if __name__ == "__main__":
    unittest.main()
