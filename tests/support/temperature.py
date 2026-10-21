"""Shared deterministic builders for thermal telemetry tests."""

from __future__ import annotations

from datetime import datetime, timezone

from maintenance.components.temperature import TemperatureSample

FIXED_TEMPERATURE_AT = datetime(2026, 9, 5, 3, 42, 52, tzinfo=timezone.utc)


def make_temperature_sample(
    component: str,
    value_celsius: float,
    *,
    sensor_id: str = "sensor-0",
    sensor_name: str = "sensor",
    sampled_monotonic: float = 100.0,
) -> TemperatureSample:
    return TemperatureSample(
        component=component,
        sensor_id=sensor_id,
        sensor_name=sensor_name,
        value_celsius=value_celsius,
        sampled_at=FIXED_TEMPERATURE_AT,
        sampled_monotonic=sampled_monotonic,
    )
