from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maintenance.components.temperature import TemperatureSample


class CapabilityState(str, Enum):
    """Typed hardware-capability state carried by a component result.

    ``SUPPORTED`` means the component genuinely exists and was read.
    ``UNSUPPORTED`` means an authoritative probe proved the capability is
    absent (e.g. no battery). ``TEMPORARILY_UNAVAILABLE`` means a supported
    provider could not be read this time. ``NO_DATA`` means no sample exists
    yet. ``PERMISSION_LIMITED`` means the provider was denied access.
    ``NOT_VERIFIED_ON_NATIVE_PLATFORM`` is reserved for declarations whose
    native evidence is absent. ``UNKNOWN`` is retained for old snapshots.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    NO_DATA = "no_data"
    PERMISSION_LIMITED = "permission_limited"
    NOT_VERIFIED_ON_NATIVE_PLATFORM = "not_verified_on_native_platform"
    UNKNOWN = "unknown"


class ProcessActionState(str, Enum):
    """Target-reported state for the process action affordance."""

    ALLOWED = "allowed"
    PROTECTED = "protected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    key: str
    title: str
    value: str
    subtitle: str
    percent: float | None
    details: tuple[str, ...]
    actionable: bool = False
    failed: bool = False
    capability: CapabilityState = CapabilityState.UNKNOWN
    temperatures: tuple[TemperatureSample, ...] = ()
    temperature_unavailable_reason: str | None = None


def unavailable_summary(
    key: str,
    title: str,
    *,
    capability: CapabilityState = CapabilityState.UNKNOWN,
) -> ResourceSummary:
    """Return the standard "Unavailable" summary for a failed component.

    Shared by the scanner (one card builder raised) and the window (a
    component worker raised), so the failure presentation never drifts
    between the two paths.
    """

    return ResourceSummary(
        key=key,
        title=title,
        value="Unavailable",
        subtitle="Information unavailable",
        percent=None,
        details=(f"{title} information is unavailable.",),
        actionable=key in ("cpu", "memory", "storage"),
        failed=True,
        capability=capability,
    )


def capability_label(state: CapabilityState) -> str:
    """Return concise user-facing wording for a capability state."""

    return {
        CapabilityState.SUPPORTED: "Supported",
        CapabilityState.UNSUPPORTED: "Unsupported",
        CapabilityState.TEMPORARILY_UNAVAILABLE: "Temporarily unavailable",
        CapabilityState.NO_DATA: "No data yet",
        CapabilityState.PERMISSION_LIMITED: "Permission required",
        CapabilityState.NOT_VERIFIED_ON_NATIVE_PLATFORM: "Not verified on this platform",
        CapabilityState.UNKNOWN: "Temporarily unavailable",
    }[state]


def resource_status(summary: ResourceSummary) -> str | None:
    """Return a secondary status only when it adds transparency."""

    if summary.failed and summary.capability is CapabilityState.UNKNOWN:
        return "Failed"
    if summary.capability is CapabilityState.SUPPORTED:
        return None
    return capability_label(summary.capability)


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    system_label: str
    scanned_at: datetime
    resources: tuple[ResourceSummary, ...]

    def get(self, key: str) -> ResourceSummary:
        resource = next((item for item in self.resources if item.key == key), None)
        if resource is None:
            raise KeyError(f"Unknown resource: {key}")
        return resource


@dataclass(frozen=True, slots=True)
class ProcessCandidate:
    pid: int
    name: str
    memory_bytes: int
    memory_percent: float
    cpu_percent: float
    activity: str
    username: str
    action_allowed: bool
    create_time: float | None = None
    protected: bool = False
    action_state: ProcessActionState = ProcessActionState.ALLOWED

    def __post_init__(self) -> None:
        # Legacy callers only supplied ``action_allowed``; keep that shape
        # equivalent to the richer target-reported state.
        if (
            not self.action_allowed
            and not self.protected
            and self.action_state is ProcessActionState.ALLOWED
        ):
            object.__setattr__(self, "protected", True)
            object.__setattr__(self, "action_state", ProcessActionState.PROTECTED)


@dataclass(frozen=True, slots=True)
class FileCandidate:
    path: Path
    size_bytes: int
    modified_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ProcessActionResult:
    requested: int
    stopped: tuple[int, ...]
    force_required: tuple[int, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileActionResult:
    requested: int
    moved: tuple[Path, ...]
    errors: tuple[str, ...]
