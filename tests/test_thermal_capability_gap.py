"""Regression coverage for bounded thermal capability resolution."""

from __future__ import annotations

import unittest

from maintenance.components.temperature import TemperatureState, TemperatureTelemetry
from maintenance.models import CapabilityState
from tests.support.models import make_summary


class ThermalCapabilityGapTests(unittest.TestCase):
    def test_slow_sensor_still_waits_before_confirm_limit(self) -> None:
        telemetry = TemperatureTelemetry()
        cpu_card = make_summary(
            "cpu", "CPU", capability=CapabilityState.SUPPORTED, temperatures=()
        )

        telemetry.record_summary("cpu", cpu_card)

        self.assertEqual(
            telemetry.series_snapshot("cpu").state, TemperatureState.NO_DATA
        )

    def test_empty_supported_reads_reach_unsupported_at_policy_limit(self) -> None:
        telemetry = TemperatureTelemetry()
        cpu_card = make_summary(
            "cpu", "CPU", capability=CapabilityState.SUPPORTED, temperatures=()
        )

        for _ in range(20):
            telemetry.record_summary("cpu", cpu_card)

        self.assertEqual(
            telemetry.series_snapshot("cpu").state, TemperatureState.UNSUPPORTED
        )

    def test_permanently_empty_temperature_samples_resolve_to_unsupported(
        self,
    ) -> None:
        telemetry = TemperatureTelemetry()
        cpu_card = make_summary(
            "cpu", "CPU", capability=CapabilityState.SUPPORTED, temperatures=()
        )
        for _ in range(1000):
            telemetry.record_summary("cpu", cpu_card)
        snapshot = telemetry.series_snapshot("cpu")
        self.assertNotEqual(snapshot.state, TemperatureState.NO_DATA)
