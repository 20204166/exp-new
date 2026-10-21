"""GPU platform selection shared by the dashboard scanner."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from maintenance.models import CapabilityState

LOGGER = logging.getLogger(__name__)

GPU_INFORMATION_UNAVAILABLE = "GPU information unavailable"


def gpu_unavailable_message(error: Exception | str) -> str:
    """Return the concise unavailable message and log the failure detail.

    The raw exception or subprocess output never reaches the UI; it is kept
    in the application log for diagnostics instead.
    """

    LOGGER.warning("GPU probe failed: %s", error)
    return GPU_INFORMATION_UNAVAILABLE


@dataclass(frozen=True, slots=True)
class GpuProbe:
    """Structured GPU probe result: details plus a typed capability state.

    Capability is never derived from the display string: the platform loader
    classifies authoritative absence separately from command/probe failures.
    """

    details: tuple[str, ...]
    capability: CapabilityState


def gpu_probe_from_read(
    read: Callable[[], tuple[tuple[str, ...], str | None]],
) -> GpuProbe:
    """Classify one platform GPU read into a typed probe.

    Shared by the macOS, Windows, and Linux readers: a command error becomes
    an unavailable message with ``TEMPORARILY_UNAVAILABLE`` capability, an
    empty result becomes ``UNSUPPORTED`` (not exposed by that provider), and detail lines
    become ``SUPPORTED``. Callers keep their own ``_*_gpu_read`` parsing;
    this owns only the uniform classification. ``GpuDetector._normalize``
    is intentionally different: it classifies injected loader values, where
    an empty result stays temporarily unavailable rather than proving absence.
    """

    lines, error = read()
    if error is not None:
        return GpuProbe(
            (gpu_unavailable_message(error),),
            CapabilityState.TEMPORARILY_UNAVAILABLE,
        )
    if not lines:
        return GpuProbe((GPU_INFORMATION_UNAVAILABLE,), CapabilityState.UNSUPPORTED)
    return GpuProbe(lines, CapabilityState.SUPPORTED)


def nvidia_device_readings(
    pynvml_module: Any,
    *,
    include_temperature: bool,
) -> list[tuple[str, int, int, int, int | None]]:
    """Return one tuple per NVIDIA device for an initialised NVML session.

    Each tuple is ``(name, memory_used, memory_total, gpu_usage_percent,
    temperature_or_None)``. ``pynvml_module`` is injected so this module
    stays free of the optional ``pynvml`` import; callers own ``nvmlInit``/
    ``nvmlShutdown`` and their own failure handling. The active dashboard
    scanner uses ``include_temperature=False`` so the temperature query is
    skipped there.
    """

    readings: list[tuple[str, int, int, int, int | None]] = []
    for index in range(pynvml_module.nvmlDeviceGetCount()):
        handle = pynvml_module.nvmlDeviceGetHandleByIndex(index)
        name = pynvml_module.nvmlDeviceGetName(handle)
        memory = pynvml_module.nvmlDeviceGetMemoryInfo(handle)
        usage = pynvml_module.nvmlDeviceGetUtilizationRates(handle)
        if include_temperature:
            temperature = pynvml_module.nvmlDeviceGetTemperature(
                handle,
                pynvml_module.NVML_TEMPERATURE_GPU,
            )
        else:
            temperature = None
        if isinstance(name, bytes):
            name = name.decode(errors="replace")
        readings.append(
            (
                str(name),
                int(memory.used),
                int(memory.total),
                int(usage.gpu),
                temperature,
            )
        )
    return readings


class GpuDetector:
    """Select the correct GPU detail loader for the active platform.

    Extracted from the platform-dispatch path in
    `maintenance/scanner.py:792-837` and now used by `SystemScanner.gpu_details()`.

    Loaders may return either a plain ``tuple[str, ...]`` (legacy tuple-only
    loaders, classified conservatively) or a ``GpuProbe`` (exact
    classification). ``detect()`` always returns the tuple form so existing
    callers and monkeypatch seams keep working.
    """

    def __init__(
        self,
        *,
        system: Callable[[], str],
        nvidia_loader: Callable[[], Any],
        mac_loader: Callable[[], Any],
        windows_loader: Callable[[], Any],
        linux_loader: Callable[[], Any],
    ) -> None:
        self._system = system
        self._nvidia_loader = nvidia_loader
        self._mac_loader = mac_loader
        self._windows_loader = windows_loader
        self._linux_loader = linux_loader

    def detect(self) -> tuple[str, ...]:
        """Return the first applicable GPU detail set for this platform."""

        return self.detect_with_capability().details

    def detect_with_capability(self) -> GpuProbe:
        """Return the GPU details together with a typed capability state."""

        system = self._system()
        if system == "Darwin":
            # macOS keeps its native path first so the future wiring stays cheap.
            return self._normalize(self._mac_loader())

        nvidia_details = self._nvidia_loader()
        if nvidia_details:
            return self._normalize(nvidia_details)

        if system == "Windows":
            return self._normalize(self._windows_loader())
        if system == "Linux":
            return self._normalize(self._linux_loader())
        return GpuProbe(
            (GPU_INFORMATION_UNAVAILABLE,),
            CapabilityState.NOT_VERIFIED_ON_NATIVE_PLATFORM,
        )

    @staticmethod
    def _normalize(value: Any) -> GpuProbe:
        """Classify a loader result, accepting both GpuProbe and legacy tuples."""

        if isinstance(value, GpuProbe):
            return value
        if not value:
            return GpuProbe(
                (GPU_INFORMATION_UNAVAILABLE,),
                CapabilityState.TEMPORARILY_UNAVAILABLE,
            )
        return GpuProbe(tuple(value), CapabilityState.SUPPORTED)
