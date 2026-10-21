"""Scanner-specific responsibility support.

The mixin preserves SystemScanner method names while keeping dependency
lookups on maintenance.scanner for existing monkeypatch seams.
"""

# The mixin is intentionally completed by SystemScanner; the concrete state
# and class constants live on that owner rather than being duplicated here.
# pyright: reportAttributeAccessIssue=false, reportUndefinedVariable=false, reportGeneralTypeIssues=false, reportOptionalMemberAccess=false, reportArgumentType=false
# mypy: disable-error-code="attr-defined,misc,has-type,assignment,valid-type,name-defined"

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable

from maintenance.components import GpuDetector, gpu_unavailable_message
from maintenance.components.gpu import (
    GpuProbe,
    gpu_probe_from_read,
    nvidia_device_readings,
)
from maintenance.external_commands import run_json_command, run_text_command
from maintenance.models import CapabilityState

from ._compat import scanner_module


class GpuMixin:
    """Own one cohesive scanner implementation responsibility."""

    def gpu_details(self) -> tuple[str, ...]:
        """Return platform-appropriate GPU details within a bounded time.

        Kept as the tuple-returning compatibility surface; the structured
        capability-aware probe lives in `_gpu_probe`, which this delegates to.
        """

        return self._gpu_probe().details

    def _gpu_probe(self) -> GpuProbe:
        """Return GPU details and typed capability within a bounded time.

        GPU detection can call pynvml, which cannot be interrupted in-process,
        so the detection runs on a short-lived worker that is joined with a
        timeout. The query lifecycle is guarded by one lock and a generation
        counter, so concurrent calls (full scan plus component refresh) can
        never start duplicate probes, a late worker can never clear a newer
        query's state, and `_stop_gpu_query` can abandon an in-flight worker.

        While a previous worker is still running (hung or slower than the
        budget), later calls return the timeout message immediately so a
        wedged driver cannot stack up one blocked worker per scan. A worker
        that eventually finishes clears the state, so a merely slow query
        recovers on the next scan; a query that is still hung after
        `GPU_QUERY_ABANDON_SECONDS` is abandoned on the next call so GPU
        reporting always recovers.
        """

        with self._gpu_query_lock:
            if self._gpu_query_in_flight:
                timed_out_at = self._gpu_query_timed_out_at
                if (
                    timed_out_at is not None
                    and scanner_module.time.monotonic() - timed_out_at
                    >= self.GPU_QUERY_ABANDON_SECONDS
                ):
                    self._gpu_query_generation += 1
                    self._gpu_query_in_flight = False
                    self._gpu_query_timed_out_at = None
                    scanner_module.LOGGER.warning(
                        "Abandoning wedged GPU query and restarting"
                    )
                else:
                    probe = GpuProbe(
                        (self.GPU_QUERY_TIMEOUT_MESSAGE,),
                        CapabilityState.TEMPORARILY_UNAVAILABLE,
                    )
                    self._remember_gpu_capability(probe.capability)
                    return probe

            generation = self._gpu_query_generation
            results: list[GpuProbe] = []

            def detect() -> None:
                try:
                    results.append(self._make_gpu_detector().detect_with_capability())
                except Exception as error:  # noqa: BLE001 - GPU queries must not raise.
                    results.append(
                        GpuProbe(
                            (gpu_unavailable_message(error),),
                            CapabilityState.TEMPORARILY_UNAVAILABLE,
                        )
                    )
                finally:
                    with self._gpu_query_lock:
                        if self._gpu_query_generation == generation:
                            self._gpu_query_in_flight = False
                            self._gpu_query_timed_out_at = None

            worker = threading.Thread(target=detect, daemon=True)
            try:
                self._gpu_query_in_flight = True
                worker.start()
            except RuntimeError as error:
                self._gpu_query_in_flight = False
                probe = GpuProbe(
                    (gpu_unavailable_message(error),),
                    CapabilityState.TEMPORARILY_UNAVAILABLE,
                )
                self._remember_gpu_capability(probe.capability)
                return probe

        worker.join(self.GPU_QUERY_TIMEOUT_SECONDS)
        if worker.is_alive():
            with self._gpu_query_lock:
                self._gpu_query_timed_out_at = scanner_module.time.monotonic()
            probe = GpuProbe(
                (self.GPU_QUERY_TIMEOUT_MESSAGE,),
                CapabilityState.TEMPORARILY_UNAVAILABLE,
            )
        else:
            probe = (
                results[0]
                if results
                else GpuProbe(
                    (self.GPU_QUERY_TIMEOUT_MESSAGE,),
                    CapabilityState.TEMPORARILY_UNAVAILABLE,
                )
            )
        self._remember_gpu_capability(probe.capability)
        return probe

    def _remember_gpu_capability(self, capability: CapabilityState) -> None:
        """Record the calling thread's GPU capability for its own scan.

        ``scan_component`` calls ``gpu_details()`` for the tuple and reads the
        capability back here on the same thread; when ``gpu_details`` is
        monkeypatched (no probe ran) no entry exists and ``UNKNOWN`` is used.
        Each thread only writes and later reads its own key, so the dict
        needs no lock (and must not re-enter ``_gpu_query_lock``).
        """

        self._gpu_capabilities_by_thread[threading.get_ident()] = capability

    def _gpu_capability_for_thread(self) -> CapabilityState:
        return self._gpu_capabilities_by_thread.pop(
            threading.get_ident(),
            CapabilityState.UNKNOWN,
        )

    def _make_gpu_detector(self) -> GpuDetector:
        return GpuDetector(
            system=scanner_module.platform.system,
            nvidia_loader=self._nvidia_gpu_probe,
            mac_loader=lambda: self._cached_static_gpu_probe(self._mac_gpu_probe),
            windows_loader=lambda: self._cached_static_gpu_probe(
                self._windows_gpu_probe
            ),
            linux_loader=lambda: self._cached_static_gpu_probe(self._linux_gpu_probe),
        )

    def _cached_static_gpu_probe(
        self,
        loader: Callable[[], GpuProbe],
    ) -> GpuProbe:
        return self._cached_fingerprint_value(
            lock=self._static_gpu_lock,
            value_name="_static_gpu_details",
            fingerprint_name="_static_gpu_fingerprint",
            fingerprint=self._static_fingerprint(),
            loader=loader,
            acceptable=lambda probe: probe.capability == CapabilityState.SUPPORTED,
        )

    def _nvidia_gpu_probe(self) -> GpuProbe | None:
        """Return the NVIDIA probe, or None when NVIDIA is unavailable."""

        details = self._nvidia_gpu_details()
        if details is None:
            return None
        return GpuProbe(details, CapabilityState.SUPPORTED)

    def _nvidia_gpu_details(self) -> tuple[str, ...] | None:
        """Return NVIDIA GPU detail lines, or None when unavailable.

        A failed NVML probe is remembered per scanner so hosts without the
        NVML shared library do not re-attempt (and re-log) the failed
        ``nvmlInit`` on every scan; `reset_static_cache()` re-enables the
        probe. Successful probes are never remembered, so NVIDIA-capable
        hosts still query every scan exactly as before.
        """

        if scanner_module.pynvml is None:
            return None

        if self._nvml_probe_failed:
            return None

        try:
            scanner_module.pynvml.nvmlInit()
            lines: list[str] = []
            for (
                name,
                memory_used,
                memory_total,
                usage_gpu,
                _temperature,
            ) in nvidia_device_readings(
                scanner_module.pynvml,
                include_temperature=False,
            ):
                lines.extend(
                    [
                        name,
                        f"GPU usage: {usage_gpu}%",
                        (
                            f"Memory: {self.format_bytes(memory_used)} used of "
                            f"{self.format_bytes(memory_total)}"
                        ),
                    ]
                )
            return tuple(lines) or None
        except Exception as error:  # noqa: BLE001 - NVIDIA queries are best-effort text.
            scanner_module.LOGGER.warning("NVIDIA GPU query failed: %s", error)
            with self._static_gpu_lock:
                self._nvml_probe_failed = True
            return None
        finally:
            with contextlib.suppress(Exception):
                scanner_module.pynvml.nvmlShutdown()

    @staticmethod
    def _mac_gpu_read() -> tuple[tuple[str, ...], str | None]:
        payload, error = run_json_command(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            timeout_seconds=scanner_module.SystemScanner.GPU_COMMAND_TIMEOUT_SECONDS,
            runner=scanner_module.subprocess.run,
        )
        if error is not None:
            return (), error

        displays = payload.get("SPDisplaysDataType", [])
        lines: list[str] = []
        for display in displays:
            name = display.get("sppci_model") or display.get("_name")
            if not name:
                continue
            lines.append(str(name))
            if memory := display.get("spdisplays_vram"):
                lines.append(f"Memory: {memory}")
            if metal := display.get("spdisplays_metal"):
                lines.append(f"Metal: {metal}")
        return tuple(lines), None

    @classmethod
    def _mac_gpu_details(cls) -> tuple[str, ...]:
        return cls._mac_gpu_probe().details

    @classmethod
    def _mac_gpu_probe(cls) -> GpuProbe:
        return gpu_probe_from_read(cls._mac_gpu_read)

    @classmethod
    def _windows_gpu_read(cls) -> tuple[tuple[str, ...], str | None]:
        command = (
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json"
        )
        creationflags = (
            getattr(scanner_module.subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        controllers, error = run_json_command(
            ["powershell", "-NoProfile", "-Command", command],
            timeout_seconds=scanner_module.SystemScanner.GPU_COMMAND_TIMEOUT_SECONDS,
            runner=scanner_module.subprocess.run,
            empty_stdout_fallback="[]",
            creationflags=creationflags,
        )
        if error is not None:
            return (), error

        if isinstance(controllers, dict):
            controllers = [controllers]

        lines: list[str] = []
        for controller in controllers:
            name = controller.get("Name")
            if not name:
                continue
            lines.append(str(name))
            adapter_ram = controller.get("AdapterRAM")
            if isinstance(adapter_ram, int) and adapter_ram > 0:
                lines.append(f"Memory: {cls.format_bytes(adapter_ram)}")
            if driver := controller.get("DriverVersion"):
                lines.append(f"Driver: {driver}")
        return tuple(lines), None

    @classmethod
    def _windows_gpu_details(cls) -> tuple[str, ...]:
        return cls._windows_gpu_probe().details

    @classmethod
    def _windows_gpu_probe(cls) -> GpuProbe:
        return gpu_probe_from_read(cls._windows_gpu_read)

    @staticmethod
    def _linux_gpu_read() -> tuple[tuple[str, ...], str | None]:
        stdout, error = run_text_command(
            ["lspci"],
            timeout_seconds=scanner_module.SystemScanner.GPU_COMMAND_TIMEOUT_SECONDS,
            runner=scanner_module.subprocess.run,
        )
        if error is not None:
            return (), error

        gpu_lines = [
            line.split(": ", 1)[-1]
            for line in stdout.splitlines()
            if "VGA compatible controller" in line
            or "3D controller" in line
            or "Display controller" in line
        ]
        return tuple(gpu_lines), None

    @classmethod
    def _linux_gpu_details(cls) -> tuple[str, ...]:
        return cls._linux_gpu_probe().details

    @classmethod
    def _linux_gpu_probe(cls) -> GpuProbe:
        return gpu_probe_from_read(cls._linux_gpu_read)
