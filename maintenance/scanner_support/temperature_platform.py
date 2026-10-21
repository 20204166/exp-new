"""Platform-specific thermal acquisition without GUI or scheduling concerns."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from maintenance.components.temperature import (
    TemperatureSample,
    TemperatureScan,
    is_valid_temperature_value,
)
from maintenance.external_commands import CommandRunner, run_json_command
from maintenance.scanner_support.smc import COMPONENT_LABELS, read_smc_temperatures

RawReadings = dict[str, list[tuple[str, str, float]]]

_ACPI_COMMAND = (
    "try { Get-CimInstance -Namespace root/WMI -ClassName "
    "MSAcpi_ThermalZoneTemperature -ErrorAction Stop | Select-Object "
    "InstanceName,CurrentTemperature | ConvertTo-Json } "
    "catch { @{ error = $_.Exception.Message } | ConvertTo-Json }"
)
_OPTIONAL_PROVIDER_FIELDS = "Name,SensorType,Value,Identifier,Parent"
_ACPI_ACCESS_DENIED_REASON = "CPU temperature requires administrator privileges"
_OPTIONAL_ACCESS_DENIED_REASON = "Thermal sensors require administrator privileges"
_COMPONENT_NAME_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gpu", ("gpu",)),
    ("cpu", ("cpu", "core", "package")),
    ("storage", ("nvme", "ssd", "hdd", "disk")),
    ("battery", ("battery",)),
)


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _empty_scan(*, unavailable_reason: str | None = None) -> TemperatureScan:
    captured_at = datetime.now(timezone.utc).astimezone()
    return TemperatureScan(captured_at, time.monotonic(), (), (), unavailable_reason)


def _scan_from_readings(readings: RawReadings) -> TemperatureScan:
    captured_at = datetime.now(timezone.utc).astimezone()
    captured_monotonic = time.monotonic()
    lines: list[str] = []
    grouped: list[tuple[str, tuple[TemperatureSample, ...]]] = []
    for component, entries in readings.items():
        samples = tuple(
            TemperatureSample(
                component=component,
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                value_celsius=value,
                sampled_at=captured_at,
                sampled_monotonic=captured_monotonic,
            )
            for sensor_id, sensor_name, value in entries
            if is_valid_temperature_value(value)
        )
        if not samples:
            continue
        grouped.append((component, samples))
        label = COMPONENT_LABELS.get(component, component)
        lines.append(
            f"{label}: {max(sample.value_celsius for sample in samples):.0f}°C"
        )
    return TemperatureScan(
        captured_at, captured_monotonic, tuple(lines), tuple(grouped)
    )


def _read_json(
    command: list[str], *, runner: CommandRunner | None
) -> tuple[Any, str | None]:
    try:
        return run_json_command(
            command,
            runner=runner,
            empty_stdout_fallback="[]",
            creationflags=_creationflags(),
        )
    except Exception as error:  # noqa: BLE001 - providers are best effort.
        return None, str(error)


def _records(payload: Any) -> list[dict[str, Any]]:
    values = [payload] if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def _permission_denied_reason(payload: Any, *, reason: str) -> str | None:
    """Detect the try/catch error payload, distinct from "not installed".

    ``Get-CimInstance`` raises a non-terminating error by default, which
    would otherwise exit 0 with empty stdout and silently look identical to
    "no thermal zones on this hardware" -- ``-ErrorAction Stop`` plus a
    catch block turns that into a ``{"error": ...}`` payload instead so a
    genuine permission wall isn't confused with unsupported hardware.
    English-only message matching; a differently localized Windows install
    would fall back to the ordinary "no data" behavior rather than raise.
    """

    if not isinstance(payload, dict) or "error" not in payload:
        return None
    message = str(payload.get("error", ""))
    if "denied" in message.casefold():
        return reason
    return None


def _acpi_readings(*, runner: CommandRunner | None) -> tuple[RawReadings, str | None]:
    payload, error = _read_json(
        ["powershell", "-NoProfile", "-Command", _ACPI_COMMAND], runner=runner
    )
    if error is not None:
        return {}, None
    reason = _permission_denied_reason(payload, reason=_ACPI_ACCESS_DENIED_REASON)
    if reason is not None:
        return {}, reason
    readings: list[tuple[str, str, float]] = []
    for zone in _records(payload):
        raw = zone.get("CurrentTemperature")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            continue
        value = raw / 10.0 - 273.15
        if is_valid_temperature_value(value):
            name = str(zone.get("InstanceName") or "ThermalZone")
            readings.append((f"acpi:{name}", name, value))
    return ({"cpu": readings} if readings else {}), None


def _classify(name: str) -> str | None:
    lowered = name.casefold()
    for component, markers in _COMPONENT_NAME_MARKERS:
        if any(marker in lowered for marker in markers):
            return component
    return None


def _optional_readings(
    namespace: str, prefix: str, *, runner: CommandRunner | None
) -> tuple[RawReadings, str | None]:
    command_text = (
        f"try {{ Get-CimInstance -Namespace root\\{namespace} -ClassName Sensor "
        f"-ErrorAction Stop | Select-Object {_OPTIONAL_PROVIDER_FIELDS} | "
        "ConvertTo-Json } catch { @{ error = $_.Exception.Message } | ConvertTo-Json }"
    )
    payload, error = _read_json(
        ["powershell", "-NoProfile", "-Command", command_text], runner=runner
    )
    if error is not None:
        return {}, None
    reason = _permission_denied_reason(payload, reason=_OPTIONAL_ACCESS_DENIED_REASON)
    if reason is not None:
        return {}, reason
    readings: RawReadings = {}
    for record in _records(payload):
        if str(record.get("SensorType", "")).casefold() != "temperature":
            continue
        name = str(record.get("Name") or "")
        component = _classify(name)
        value = record.get("Value")
        if component is None or not is_valid_temperature_value(value):
            continue
        identity = record.get("Identifier") or record.get("Parent") or name
        readings.setdefault(component, []).append(
            (f"{prefix}:{identity}", name, float(value))
        )
    return readings, None


def windows_temperature_scan(*, runner: CommandRunner | None = None) -> TemperatureScan:
    """Read Windows temperatures using ACPI, then installed monitor providers."""

    reason: str | None = None
    for provider in (
        lambda: _acpi_readings(runner=runner),
        lambda: _optional_readings("LibreHardwareMonitor", "lhm", runner=runner),
        lambda: _optional_readings("OpenHardwareMonitor", "ohm", runner=runner),
    ):
        readings, provider_reason = provider()
        if readings:
            return _scan_from_readings(readings)
        reason = reason or provider_reason
    return _empty_scan(unavailable_reason=reason)


def macos_temperature_scan(
    *, is_valid: Callable[[object], bool] = is_valid_temperature_value
) -> TemperatureScan:
    """Wrap the existing fail-soft SMC acquisition in the shared scan shape."""

    try:
        readings = read_smc_temperatures(is_valid=is_valid)
    except Exception:  # noqa: BLE001 - SMC failures degrade to no data.
        readings = {}
    return _scan_from_readings(readings)
