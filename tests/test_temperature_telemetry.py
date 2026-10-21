"""Focused tests for the shared thermal telemetry store."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from maintenance.cluster import resource_summary_from_dict, resource_summary_to_dict
from maintenance.components.temperature import (
    TemperaturePolicy,
    TemperatureScan,
    TemperatureSeriesSnapshot,
    TemperatureState,
    TemperatureTelemetry,
    TemperatureTelemetryUpdate,
    parse_temperature_value,
    temperature_sample_to_dict,
)
from maintenance.models import CapabilityState
from maintenance.observability import ObservabilityWatcher
from tests.support.models import make_summary
from tests.support.temperature import FIXED_TEMPERATURE_AT, make_temperature_sample


def _summary_with_temp(value_celsius: float, monotonic: float):
    return make_summary(
        "cpu",
        "CPU",
        value="45%",
        subtitle="running",
        percent=45.0,
        details=(f"Temperature: {value_celsius:.0f}°C",),
        capability=CapabilityState.SUPPORTED,
        temperatures=(
            make_temperature_sample("cpu", value_celsius, sampled_monotonic=monotonic),
        ),
    )


class TemperatureTelemetryTests(unittest.TestCase):
    def test_summary_recording_is_observed(self) -> None:
        observer = ObservabilityWatcher()
        telemetry = TemperatureTelemetry(observer=observer)

        telemetry.record_summary("cpu", _summary_with_temp(50.0, 10.0))

        metric = observer.snapshot().metrics[0]
        self.assertEqual(metric.target, "thermal:cpu")
        self.assertEqual(metric.successes, 1)

    def test_temperature_value_uses_finite_local_range(self) -> None:
        from maintenance.components.temperature import is_valid_temperature_value

        for value in (0.1, 249.9):
            self.assertTrue(is_valid_temperature_value(value))
        for invalid in (
            0,
            250,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            "45",
        ):
            with self.subTest(value=invalid):
                self.assertFalse(is_valid_temperature_value(invalid))

    def test_parse_temperature_value_handles_valid_and_malformed_text(self) -> None:
        self.assertEqual(parse_temperature_value("Temperature: 45°C"), 45.0)
        self.assertEqual(parse_temperature_value("Drive temperature: 38.5°C"), 38.5)
        self.assertIsNone(parse_temperature_value("Temperature: unavailable"))

    def test_record_summary_populates_current_and_history(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(history_limit=4, rapid_rise_celsius=None)
            }
        )
        summary = make_summary(
            "cpu",
            "CPU",
            value="45%",
            subtitle="running",
            percent=45.0,
            details=("Temperature: 45°C",),
            capability=CapabilityState.SUPPORTED,
            temperatures=(make_temperature_sample("cpu", 45.0),),
        )

        telemetry.record_summary("cpu", summary)
        snapshot = telemetry.series_snapshot("cpu", title="CPU Temperature")

        self.assertIsInstance(snapshot, TemperatureSeriesSnapshot)
        self.assertEqual(snapshot.state, TemperatureState.VALID)
        self.assertEqual(snapshot.current_celsius, 45.0)
        self.assertEqual(len(snapshot.samples), 1)
        self.assertEqual(snapshot.samples[0].value_celsius, 45.0)

    def test_invalid_decoded_sample_does_not_change_history_current_or_events(
        self,
    ) -> None:
        telemetry = TemperatureTelemetry()
        telemetry.record_summary(
            "cpu",
            make_summary(
                "cpu",
                "CPU",
                capability=CapabilityState.SUPPORTED,
                temperatures=(make_temperature_sample("cpu", 90.0),),
            ),
        )
        before = telemetry.series_snapshot("cpu")

        invalid_payload = resource_summary_to_dict(
            make_summary(
                "cpu",
                "CPU",
                capability=CapabilityState.SUPPORTED,
                temperatures=(make_temperature_sample("cpu", 90.0),),
            )
        )
        invalid_payload["temperatures"] = [
            {
                **temperature_sample_to_dict(make_temperature_sample("cpu", 90.0)),
                "value_celsius": float("nan"),
            }
        ]
        telemetry.record_summary("cpu", resource_summary_from_dict(invalid_payload))

        after = telemetry.series_snapshot("cpu")
        self.assertEqual(after.samples, before.samples)
        self.assertEqual(after.current_celsius, before.current_celsius)
        self.assertEqual(after.events, before.events)

    def test_record_returns_update_and_render_state_is_cached_data(self) -> None:
        telemetry = TemperatureTelemetry()
        update = telemetry.record_summary(
            "cpu",
            make_summary(
                "cpu",
                "CPU",
                capability=CapabilityState.SUPPORTED,
                temperatures=(make_temperature_sample("cpu", 45.0),),
            ),
        )

        state = telemetry.render_state(("cpu", "battery"))

        self.assertEqual(update.component, "cpu")
        self.assertEqual(update.snapshot.current_celsius, 45.0)
        cpu = state.series_for("cpu")
        battery = state.series_for("battery")
        assert cpu is not None
        assert battery is not None
        self.assertEqual(cpu.current_celsius, 45.0)
        self.assertEqual(battery.samples, ())
        self.assertEqual(state.events, ())

    def test_history_is_bounded_and_oldest_sample_is_discarded(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(history_limit=3, rapid_rise_celsius=None)
            }
        )
        for index, value in enumerate((40.0, 41.0, 42.0, 43.0), start=1):
            telemetry.record_summary(
                "cpu",
                make_summary(
                    "cpu",
                    "CPU",
                    value="45%",
                    subtitle="running",
                    percent=45.0,
                    details=(f"Temperature: {value:.0f}°C",),
                    capability=CapabilityState.SUPPORTED,
                    temperatures=(
                        make_temperature_sample(
                            "cpu", value, sampled_monotonic=float(index)
                        ),
                    ),
                ),
            )

        history = telemetry.history("cpu")
        self.assertEqual(
            [sample.value_celsius for sample in history], [41.0, 42.0, 43.0]
        )

    def test_unsupported_summary_marks_state_without_fabricating_history(self) -> None:
        telemetry = TemperatureTelemetry()
        summary = make_summary(
            "battery",
            "Battery",
            value="No battery",
            subtitle="Not present",
            percent=None,
            details=("No temperature sensors detected.",),
            capability=CapabilityState.UNSUPPORTED,
        )

        telemetry.record_summary("battery", summary)
        snapshot = telemetry.series_snapshot("battery", title="Battery Temperature")

        self.assertEqual(snapshot.state, TemperatureState.UNSUPPORTED)
        self.assertEqual(snapshot.samples, ())
        self.assertIsNone(snapshot.current_celsius)

    def test_heat_event_is_bounded_and_recovers(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(
                    warning_celsius=90.0,
                    critical_celsius=95.0,
                    rapid_rise_celsius=None,
                    history_limit=8,
                )
            }
        )
        samples = (91.0, 92.0, 84.0)
        for index, value in enumerate(samples, start=1):
            telemetry.record_summary(
                "cpu",
                make_summary(
                    "cpu",
                    "CPU",
                    value="45%",
                    subtitle="running",
                    percent=45.0,
                    details=(f"Temperature: {value:.0f}°C",),
                    capability=CapabilityState.SUPPORTED,
                    temperatures=(
                        make_temperature_sample(
                            "cpu", value, sampled_monotonic=float(index)
                        ),
                    ),
                ),
            )

        events = telemetry.recent_events("cpu")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].peak_celsius, 92.0)
        self.assertGreaterEqual(len(events[0].samples), 3)

    def test_failed_summary_marks_error_state(self) -> None:
        telemetry = TemperatureTelemetry()
        telemetry.record_summary(
            "cpu",
            make_summary(
                "cpu",
                "CPU",
                value="Unavailable",
                failed=True,
                capability=CapabilityState.SUPPORTED,
            ),
        )

        snapshot = telemetry.series_snapshot("cpu")
        self.assertEqual(snapshot.state, TemperatureState.ERROR)
        self.assertEqual(snapshot.samples, ())
        self.assertIsNone(snapshot.current_celsius)

    def test_platform_unavailable_reason_marks_error_state_immediately(self) -> None:
        # A permission-denied provider (e.g. Windows ACPI without admin
        # rights) must not wait through several empty reads before saying
        # anything, and must not be confused with genuinely unsupported
        # hardware -- it needs to be visible as an actionable error on the
        # very first read.
        telemetry = TemperatureTelemetry()
        telemetry.record_summary(
            "cpu",
            make_summary(
                "cpu",
                "CPU",
                value="45%",
                capability=CapabilityState.SUPPORTED,
                temperature_unavailable_reason=(
                    "CPU temperature requires administrator privileges"
                ),
            ),
        )

        snapshot = telemetry.series_snapshot("cpu")
        self.assertEqual(snapshot.state, TemperatureState.ERROR)
        self.assertEqual(
            snapshot.error, "CPU temperature requires administrator privileges"
        )
        self.assertEqual(snapshot.samples, ())

    def test_unknown_summary_marks_no_data_state(self) -> None:
        telemetry = TemperatureTelemetry()
        telemetry.record_summary(
            "cpu",
            make_summary("cpu", "CPU", capability=CapabilityState.UNKNOWN),
        )

        self.assertEqual(
            telemetry.series_snapshot("cpu").state, TemperatureState.NO_DATA
        )

    def test_single_critical_sample_surfaces_critical_event_immediately(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(
                    warning_celsius=90.0, critical_celsius=95.0, rapid_rise_celsius=None
                )
            }
        )

        telemetry.record_summary("cpu", _summary_with_temp(96.0, 1.0))

        events = telemetry.recent_events("cpu")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].severity, "critical")
        self.assertEqual(events[0].peak_celsius, 96.0)
        self.assertIsNone(events[0].ended_monotonic)

        telemetry.record_summary("cpu", _summary_with_temp(84.0, 2.0))
        self.assertIsNotNone(telemetry.recent_events("cpu")[0].ended_monotonic)

    def test_rapid_rise_surfaces_spike_event_immediately(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(
                    warning_celsius=90.0,
                    critical_celsius=95.0,
                    rapid_rise_celsius=12.0,
                )
            }
        )

        for index, value in enumerate((60.0, 60.0, 60.0, 60.0, 85.0), start=1):
            telemetry.record_summary("cpu", _summary_with_temp(value, float(index)))

        events = telemetry.recent_events("cpu")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].severity, "spike")
        self.assertIsNone(events[0].ended_monotonic)

    def test_cooldown_blocks_new_event_after_recovery(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(
                    warning_celsius=90.0,
                    critical_celsius=None,
                    rapid_rise_celsius=None,
                    recovery_celsius=85.0,
                    cooldown_seconds=30.0,
                    consecutive_samples=1,
                )
            }
        )
        for index, value in enumerate((91.0, 84.0, 91.0, 91.0), start=1):
            telemetry.record_summary("cpu", _summary_with_temp(value, float(index)))

        self.assertEqual(len(telemetry.recent_events("cpu")), 1)

        telemetry.record_summary("cpu", _summary_with_temp(91.0, 33.0))
        telemetry.record_summary("cpu", _summary_with_temp(84.0, 34.0))

        self.assertEqual(len(telemetry.recent_events("cpu")), 2)

    def test_no_data_gap_settles_open_event_so_resume_starts_fresh(self) -> None:
        telemetry = TemperatureTelemetry(
            policies={
                "cpu": TemperaturePolicy(
                    warning_celsius=90.0,
                    critical_celsius=None,
                    rapid_rise_celsius=None,
                    recovery_celsius=85.0,
                    consecutive_samples=1,
                )
            }
        )
        telemetry.record_summary("cpu", _summary_with_temp(92.0, 1.0))
        self.assertEqual(len(telemetry.recent_events("cpu")), 1)
        self.assertIsNone(telemetry.recent_events("cpu")[0].ended_monotonic)

        telemetry.record_summary(
            "cpu",
            make_summary("cpu", "CPU", capability=CapabilityState.UNKNOWN),
        )
        self.assertIsNotNone(telemetry.recent_events("cpu")[0].ended_monotonic)

        telemetry.record_summary("cpu", _summary_with_temp(92.0, 5.0))
        telemetry.record_summary("cpu", _summary_with_temp(80.0, 6.0))
        self.assertEqual(len(telemetry.recent_events("cpu")), 2)

    def test_battery_policy_without_thresholds_never_emits_events(self) -> None:
        telemetry = TemperatureTelemetry()
        for index, value in enumerate((60.0, 91.0, 95.0), start=1):
            telemetry.record_summary(
                "battery",
                make_summary(
                    "battery",
                    "Battery",
                    capability=CapabilityState.SUPPORTED,
                    temperatures=(
                        make_temperature_sample(
                            "battery", value, sampled_monotonic=float(index)
                        ),
                    ),
                ),
            )

        self.assertEqual(telemetry.recent_events("battery"), ())
        self.assertEqual(
            telemetry.series_snapshot("battery").state, TemperatureState.VALID
        )

    def test_legacy_detail_line_is_extracted_when_no_temperatures(self) -> None:
        telemetry = TemperatureTelemetry()
        telemetry.record_summary(
            "cpu",
            make_summary(
                "cpu",
                "CPU",
                details=("Temperature: 45°C",),
                capability=CapabilityState.SUPPORTED,
            ),
        )

        snapshot = telemetry.series_snapshot("cpu")
        self.assertEqual(snapshot.state, TemperatureState.VALID)
        self.assertEqual(snapshot.current_celsius, 45.0)

    def test_record_scan_records_samples_and_state(self) -> None:
        telemetry = TemperatureTelemetry()
        sample = make_temperature_sample("cpu", 50.0, sampled_monotonic=1.0)
        scan = TemperatureScan(
            captured_at=FIXED_TEMPERATURE_AT,
            captured_monotonic=1.0,
            lines=("Temperature: 50°C",),
            samples_by_component=(("cpu", (sample,)),),
        )

        telemetry.record_scan(scan)

        snapshot = telemetry.series_snapshot("cpu")
        self.assertEqual(snapshot.state, TemperatureState.VALID)
        self.assertEqual(snapshot.current_celsius, 50.0)

    def test_empty_scan_keeps_telemetry_in_no_data_state(self) -> None:
        telemetry = TemperatureTelemetry()
        scan = TemperatureScan(
            captured_at=FIXED_TEMPERATURE_AT,
            captured_monotonic=1.0,
            lines=(),
            samples_by_component=(),
        )

        telemetry.record_scan(scan)

        snapshot = telemetry.series_snapshot("cpu")
        self.assertEqual(snapshot.state, TemperatureState.NO_DATA)
        self.assertEqual(snapshot.samples, ())
        self.assertIsNone(snapshot.current_celsius)

    def test_record_scan_resets_empty_read_count_like_record_summary(self) -> None:
        telemetry = TemperatureTelemetry()
        confirm_samples = telemetry.policy_for("cpu").unsupported_confirm_samples

        for _ in range(confirm_samples - 1):
            telemetry.record_summary("cpu", make_summary("cpu", "CPU"))
        self.assertEqual(
            telemetry.series_snapshot("cpu").state, TemperatureState.NO_DATA
        )

        scan = TemperatureScan(
            captured_at=FIXED_TEMPERATURE_AT,
            captured_monotonic=1.0,
            lines=("Temperature: 50°C",),
            samples_by_component=(
                ("cpu", (make_temperature_sample("cpu", 50.0, sampled_monotonic=1.0),)),
            ),
        )
        telemetry.record_scan(scan)

        for _ in range(confirm_samples - 1):
            telemetry.record_summary("cpu", make_summary("cpu", "CPU"))

        self.assertEqual(
            telemetry.series_snapshot("cpu").state,
            TemperatureState.NO_DATA,
            "record_scan should reset the empty-read count on a valid sample, "
            "just like record_summary -- otherwise the pre-scan and post-scan "
            "empty reads accumulate together and flip to UNSUPPORTED too early.",
        )

    def test_telemetry_models_are_frozen(self) -> None:
        telemetry = TemperatureTelemetry()
        update = telemetry.record_summary("cpu", _summary_with_temp(45.0, 1.0))
        render = telemetry.render_state(("cpu",))

        self.assertIsInstance(update, TemperatureTelemetryUpdate)
        with self.assertRaises(FrozenInstanceError):
            update.component = "gpu"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            render.series = ()  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
