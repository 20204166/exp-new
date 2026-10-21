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
import re
import threading
from collections.abc import Callable
from typing import Any, TypeGuard, TypeVar, cast

from maintenance.components import GPU_INFORMATION_UNAVAILABLE, ScanCancelled
from maintenance.components.scan_support import detail_line_suffix
from maintenance.components.temperature import (
    TemperatureSample,
    TemperatureScan,
    is_valid_temperature_value,
)
from maintenance.models import CapabilityState, ResourceSummary, unavailable_summary
from maintenance.scanner_support import temperature_platform

from ._compat import scanner_module

_T = TypeVar("_T")


class DashboardMixin:
    """Own one cohesive scanner implementation responsibility."""

    @staticmethod
    def _psutil_value(query: Callable[[], Any]) -> Any:
        """Return one psutil reading, or None when the reading fails."""

        try:
            return query()
        except ScanCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - one failed sensor must not fail the scan.
            scanner_module.LOGGER.warning("Sensor read failed: %s", error)
            return None

    def _read_cpu_percent(
        self,
        psutil_module: Any,
        cancel_event: threading.Event | None = None,
    ) -> float | None:
        """Return one CPU usage reading without blocking after a baseline.

        The first read per scanner takes one short blocking sample
        (`CPU_PERCENT_SAMPLE_SECONDS`, matching the previous behaviour) and
        starts a persistent daemon worker. Every later read asks that worker
        for a non-blocking `psutil.cpu_percent(interval=None)` delta, whose
        window equals the request spacing (roughly the 1 second component
        refresh cadence), so no worker ever sleeps.
        """

        with self._cpu_read_lock:
            if not self._cpu_seeded:
                value = self._psutil_value(
                    lambda: psutil_module.cpu_percent(
                        interval=self.CPU_PERCENT_SAMPLE_SECONDS
                    )
                )
                if value is None:
                    return None
                self._cpu_seeded = True
                self._start_cpu_worker()
                return value
        return self._request_cpu_sample(cancel_event)

    def _request_cpu_sample(
        self,
        cancel_event: threading.Event | None = None,
    ) -> float | None:
        """Request one non-blocking delta from the persistent CPU worker."""

        with self._cpu_request_lock:
            if self._cpu_worker is None or not self._cpu_worker.is_alive():
                self._start_cpu_worker()
            self._cpu_result_event.clear()
            self._cpu_request_event.set()
            if not self._wait_for_cpu_result(cancel_event):
                self._start_cpu_worker()
                return None
            if self._cpu_error is not None:
                return None
            return self._cpu_value

    def _wait_for_cpu_result(
        self,
        cancel_event: threading.Event | None,
    ) -> bool:
        """Wait for one sampler result, honouring cancellation when given.

        Without a cancel event this is exactly one bounded wait on the result
        event (the historical behaviour). With one, the wait is sliced so a
        cancelled scan raises `ScanCancelled` instead of blocking out the full
        worker timeout.
        """

        if cancel_event is None:
            return self._cpu_result_event.wait(self.CPU_WORKER_TIMEOUT_SECONDS)

        deadline = scanner_module.time.monotonic() + self.CPU_WORKER_TIMEOUT_SECONDS
        while True:
            remaining = deadline - scanner_module.time.monotonic()
            if remaining <= 0:
                return False
            if self._cpu_result_event.wait(
                min(self.CPU_CANCEL_POLL_SECONDS, remaining)
            ):
                return True
            self._check_cancelled(cancel_event)

    def _start_cpu_worker(self) -> None:
        """Start (or restart) the persistent CPU delta worker thread."""

        with self._cpu_request_lock:
            worker = self._cpu_worker
            if worker is not None and worker.is_alive():
                return
            self._cpu_stop_event.clear()
            self._cpu_request_event.clear()
            self._cpu_result_event.clear()
            try:
                worker = threading.Thread(
                    target=self._cpu_worker_loop,
                    daemon=True,
                )
                worker.start()
            except RuntimeError as error:
                scanner_module.LOGGER.warning("CPU sampler start failed: %s", error)
                self._cpu_worker = None
                return
            self._cpu_worker = worker

    def _cpu_worker_loop(self) -> None:
        """Serve one non-blocking CPU delta per request.

        scanner_module.psutil keeps its non-blocking delta baseline per thread id, so the
        worker first arms its own baseline with a discarded reading, then
        answers requests with deltas since the previous request.
        """

        psutil_module: Any = scanner_module.psutil
        if psutil_module is not None:
            with contextlib.suppress(Exception):
                psutil_module.cpu_percent(interval=None)

        while not self._cpu_stop_event.is_set():
            try:
                self._cpu_request_event.wait()
            except Exception:  # noqa: BLE001 - a broken wait must not kill the worker.
                break
            self._cpu_request_event.clear()
            if self._cpu_stop_event.is_set():
                return
            self._publish_cpu_sample(self._cpu_sample_once())

    def _cpu_sample_once(self) -> tuple[float | None, str | None]:
        """Take one non-blocking system CPU reading or report its error."""

        try:
            psutil_module: Any = scanner_module.psutil
            if psutil_module is None:
                return None, "psutil is not installed"
            return float(psutil_module.cpu_percent(interval=None)), None
        except Exception as error:  # noqa: BLE001 - sampler failures degrade one card.
            return None, str(error)

    def _publish_cpu_sample(
        self,
        sample: tuple[float | None, str | None],
    ) -> None:
        """Store one sampler result, wake the requestor, and log at most
        once per consecutive-failure streak."""

        value, error = sample
        with self._cpu_read_lock:
            self._cpu_value = value
            self._cpu_error = error
        if error is not None:
            if error != self._cpu_last_logged_error:
                scanner_module.LOGGER.warning("CPU sample failed: %s", error)
                self._cpu_last_logged_error = error
        else:
            self._cpu_last_logged_error = None
        self._cpu_result_event.set()

    def _stop_cpu_sampler(self) -> None:
        """Stop the persistent CPU worker thread (idempotent)."""

        self._cpu_stop_event.set()
        self._cpu_request_event.set()
        worker = self._cpu_worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)
        self._cpu_worker = None

    def _stop_gpu_query(self) -> None:
        """Abandon any in-flight GPU query (idempotent).

        Bumping the generation makes a running worker's finally a no-op, so a
        late completion can never clear a newer query's state. No join: a
        wedged NVML call is uninterruptible, so we only invalidate, never
        wait.
        """

        with self._gpu_query_lock:
            self._gpu_query_generation += 1
            self._gpu_query_in_flight = False
            self._gpu_query_timed_out_at = None

    def _safe_trash_size(self) -> int:
        """Return the trash size without letting a storage failure fail the scan.

        The recursive trash walk is cached for `TRASH_SIZE_REFRESH_SECONDS`
        via the shared TTL-cache engine, so ordinary storage refreshes do
        not re-walk a large Trash tree on every tick. A full dashboard scan
        invalidates the cache first, so user-initiated scans and post-cleanup
        rescans always show the exact fresh value. A failed walk is never
        cached, so it retries on the next refresh.
        """

        try:
            return self._ttl_cached_value(
                lock=self._trash_size_cache_lock,
                cache_name="_trash_size_cache",
                ttl_seconds=self.TRASH_SIZE_REFRESH_SECONDS,
                loader=self.trash_size,
            )
        except Exception as error:  # noqa: BLE001 - storage sub-component must not fail the scan.
            scanner_module.LOGGER.warning("Trash size read failed: %s", error)
            return 0

    def _invalidate_trash_size(self) -> None:
        """Clear the cached trash size so the next read re-walks it."""

        with self._trash_size_cache_lock:
            self._trash_size_cache = None

    def _static_fingerprint(self) -> tuple[Any, ...]:
        """Identity of the machine and boot session for the static cache.

        When either changes (a different host or a new boot session), cached
        static hardware is treated as stale and re-read on the next scan.
        """

        boot_time: Any = None
        if scanner_module.psutil is not None:
            try:
                boot_time = scanner_module.psutil.boot_time()
            except Exception as error:  # noqa: BLE001 - boot time is a best-effort signal.
                scanner_module.LOGGER.warning("Boot time read failed: %s", error)
                boot_time = None
        return (scanner_module.platform.node(), boot_time)

    def _system_label(self) -> str:
        """Return the OS/kernel label, cached until the machine or boot changes."""

        return self._cached_fingerprint_value(
            lock=self._static_hardware_lock,
            value_name="_static_system_label",
            fingerprint_name="_static_system_label_fingerprint",
            fingerprint=self._static_fingerprint(),
            loader=lambda: (
                f"{scanner_module.platform.system()} {scanner_module.platform.release()} • {scanner_module.platform.machine()}"
            ),
        )

    def _cpu_core_counts(self) -> tuple[int | None, int | None]:
        """Return physical/logical core counts, cached until hardware changes.

        A failed read is not cached, so a transient error recovers on the next
        scan, and the error propagates so the CPU card alone degrades.
        """

        def read_cores() -> tuple[int | None, int | None]:
            psutil_module = self._require_psutil()
            return (
                psutil_module.cpu_count(logical=False),
                psutil_module.cpu_count(logical=True),
            )

        return self._cached_fingerprint_value(
            lock=self._static_hardware_lock,
            value_name="_static_cpu_cores",
            fingerprint_name="_static_cpu_cores_fingerprint",
            fingerprint=self._static_fingerprint(),
            loader=read_cores,
        )

    def _cached_fingerprint_value(
        self,
        *,
        lock: Any,
        value_name: str,
        fingerprint_name: str,
        fingerprint: tuple[Any, ...],
        loader: Callable[[], _T],
        acceptable: Callable[[_T], bool] | None = None,
    ) -> _T:
        """Return one fingerprint-invalidated cached value, computing it once.

        The cached value lives in the ``value_name`` instance slot and is
        reused while the ``fingerprint_name`` slot holds the current
        fingerprint (host/boot identity). A value is stored only when the
        ``loader`` succeeds and ``acceptable`` allows it, so failed or
        unavailable reads are never cached and retry on the next scan.
        """

        with lock:
            value = getattr(self, value_name)
            if value is not None and getattr(self, fingerprint_name) == fingerprint:
                return cast(_T, value)

        computed = loader()
        if acceptable is None or acceptable(computed):
            with lock:
                setattr(self, fingerprint_name, fingerprint)
                setattr(self, value_name, computed)
        return computed

    def _ttl_cached_value(
        self,
        *,
        lock: Any,
        cache_name: str,
        ttl_seconds: float,
        loader: Callable[[], _T],
    ) -> _T:
        """Return one time-based cached value, re-loading it after a TTL.

        The cached ``(monotonic timestamp, value)`` tuple lives in the
        ``cache_name`` instance slot; a value is stored only after the
        ``loader`` succeeds, so a failing loader is never cached and retries
        on the next call. Shared by the temperature and trash-size caches.
        """

        with lock:
            cached = getattr(self, cache_name)
            now = scanner_module.time.monotonic()
            if cached is not None and now - cached[0] < ttl_seconds:
                return cached[1]

        value = loader()
        with lock:
            setattr(self, cache_name, (scanner_module.time.monotonic(), value))
        return value

    def reset_static_cache(self) -> None:
        """Drop all cached static hardware so the next scan re-reads it.

        Static values are invalidated automatically when the boot session or
        machine identity changes; this method forces a fresh read for tests and
        any future "rescan hardware" control.
        """

        with self._static_hardware_lock:
            self._static_system_label = None
            self._static_system_label_fingerprint = None
            self._static_cpu_cores = None
            self._static_cpu_cores_fingerprint = None
        with self._static_gpu_lock:
            self._static_gpu_details = None
            self._static_gpu_fingerprint = None
            self._nvml_probe_failed = False

    def _resource_with_fallback(
        self,
        builder: Callable[[], ResourceSummary],
        key: str,
        title: str,
    ) -> ResourceSummary:
        """Build one dashboard card, degrading it alone when it fails."""

        try:
            return builder()
        except ScanCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - one failed card must not fail the scan.
            scanner_module.LOGGER.warning("Resource %r failed: %s", key, error)
            return unavailable_summary(
                key,
                title,
                capability=(
                    CapabilityState.PERMISSION_LIMITED
                    if isinstance(error, PermissionError)
                    else CapabilityState.TEMPORARILY_UNAVAILABLE
                ),
            )

    def _cpu_resource(
        self,
        cpu_percent: float | None,
        frequency: Any,
        temperature_lines: list[str],
        temperature_samples: tuple[TemperatureSample, ...] = (),
        *,
        temperature_unavailable_reason: str | None = None,
    ) -> ResourceSummary:
        physical_cores, logical_cores = self._cpu_core_counts()
        details: tuple[str, ...] = (
            f"Physical cores: {physical_cores or 'Unknown'}",
            f"Logical cores: {logical_cores or 'Unknown'}",
            self._frequency_detail(frequency),
        )
        cpu_temperature = self._temperature_value(temperature_lines, "CPU")
        if cpu_temperature is not None:
            details += (f"Temperature: {cpu_temperature}",)

        return ResourceSummary(
            key="cpu",
            title="CPU",
            value=f"{cpu_percent:.1f}%",
            subtitle="Current processor usage",
            percent=cpu_percent,
            details=details,
            actionable=True,
            capability=CapabilityState.SUPPORTED,
            temperatures=temperature_samples,
            temperature_unavailable_reason=temperature_unavailable_reason,
        )

    @staticmethod
    def _frequency_detail(frequency: Any) -> str:
        """Render one CPU-frequency reading as a stable detail line.

        ``frequency`` is a psutil ``scpufreq`` value (or ``None``). Missing,
        zero, or reversed min/max fields are tolerated so unusual or
        unavailable frequency reporting never fails the CPU card.
        """

        current = getattr(frequency, "current", None)
        if not isinstance(current, (int, float)) or current <= 0:
            return "Current frequency: Unavailable"

        maximum = getattr(frequency, "max", None)
        if isinstance(maximum, (int, float)) and maximum > 0 and maximum >= current:
            return (
                f"Current frequency: {current / 1000:.2f} GHz / "
                f"Max {maximum / 1000:.2f} GHz"
            )
        return f"Current frequency: {current / 1000:.2f} GHz"

    def _memory_resource(self, memory: Any, swap: Any) -> ResourceSummary:
        total = memory.total
        available = memory.available
        in_use = max(total - available, 0)
        swap_lines = self._swap_details(swap, self._swap_devices())

        return ResourceSummary(
            key="memory",
            title="Memory",
            value=f"{memory.percent:.1f}%",
            subtitle=f"{self.format_bytes(available)} available",
            percent=memory.percent,
            details=(
                f"Physical RAM: {self.format_bytes(total)}",
                f"Available: {self.format_bytes(available)}",
                f"In use: {self.format_bytes(in_use)} ({memory.percent:.1f}%)",
                *swap_lines,
            ),
            actionable=True,
            capability=CapabilityState.SUPPORTED,
        )

    @staticmethod
    def _swap_devices() -> list[tuple[str, int, int]]:
        """Return Linux swap devices as ``(name, size_bytes, used_bytes)``.

        zram is reported by name so the memory card can keep compressed RAM
        separate from disk-backed swap. On other platforms, or when the kernel
        table cannot be read, an empty list is returned and callers fall back
        to the aggregate psutil reading.
        """

        if scanner_module.platform.system() != "Linux":
            return []

        try:
            lines = scanner_module.Path("/proc/swaps").read_text().splitlines()
        except OSError:
            return []

        devices: list[tuple[str, int, int]] = []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                size_bytes = int(parts[2]) * 1024
                used_bytes = int(parts[3]) * 1024
            except ValueError:
                continue
            devices.append((parts[0], size_bytes, used_bytes))
        return devices

    @staticmethod
    def _is_zram_device(name: str) -> bool:
        """Return whether a swap device name is compressed-RAM (zram)."""

        return name.startswith("/dev/zram")

    def _swap_details(
        self,
        swap: Any,
        devices: list[tuple[str, int, int]],
    ) -> tuple[str, ...]:
        """Build the swap detail lines, keeping zram and disk swap distinct."""

        if devices:
            total = sum(size for _, size, _ in devices)
            used = sum(used_bytes for _, _, used_bytes in devices)
            lines = [
                f"Swap: {self.format_bytes(used)} used of {self.format_bytes(total)}"
            ]

            zram_total = sum(
                size for name, size, _ in devices if self._is_zram_device(name)
            )
            zram_used = sum(
                used_bytes
                for name, _, used_bytes in devices
                if self._is_zram_device(name)
            )
            disk_total = sum(
                size for name, size, _ in devices if not self._is_zram_device(name)
            )
            disk_used = sum(
                used_bytes
                for name, _, used_bytes in devices
                if not self._is_zram_device(name)
            )
            if zram_total or zram_used:
                lines.append(
                    f"zram: {self.format_bytes(zram_used)} of "
                    f"{self.format_bytes(zram_total)}"
                )
            if disk_total or disk_used:
                lines.append(
                    f"Disk-backed swap: {self.format_bytes(disk_used)} of "
                    f"{self.format_bytes(disk_total)}"
                )
            return tuple(lines)

        if swap is None:
            return ("Swap: Unavailable",)
        if swap.total <= 0:
            return ("Swap: none configured",)
        return (
            (
                f"Swap: {self.format_bytes(swap.used)} used of "
                f"{self.format_bytes(swap.total)} ({swap.percent:.1f}%)"
            ),
        )

    def _storage_resource(
        self,
        disk: Any,
        trash_bytes: int,
        temperature_lines: list[str],
        temperature_samples: tuple[TemperatureSample, ...] = (),
    ) -> ResourceSummary:
        details: tuple[str, ...] = (
            f"Main disk: {scanner_module.Path.home()}",
            f"Total: {self.format_bytes(disk.total)}",
            f"Used: {self.format_bytes(disk.used)}",
            f"Free: {self.format_bytes(disk.free)}",
            f"Downloads: {self.downloads_path}",
            f"Trash size: {self.format_bytes(trash_bytes)}",
        )
        drive_temperature = self._temperature_value(temperature_lines, "NVMe")
        if drive_temperature is not None:
            details += (f"Drive temperature: {drive_temperature}",)

        return ResourceSummary(
            key="storage",
            title="Storage",
            value=f"{disk.percent:.1f}%",
            subtitle=f"{self.format_bytes(disk.free)} free",
            percent=disk.percent,
            details=details,
            actionable=True,
            capability=CapabilityState.SUPPORTED,
            temperatures=temperature_samples,
        )

    def _gpu_resource(
        self,
        gpu_details: tuple[str, ...],
        temperature_lines: list[str],
        temperature_samples: tuple[TemperatureSample, ...] = (),
        capability: CapabilityState = CapabilityState.UNKNOWN,
    ) -> ResourceSummary:
        details = gpu_details
        gpu_temperature = self._temperature_value(temperature_lines, "GPU")
        if gpu_temperature is not None:
            details += (f"Temperature: {gpu_temperature}",)

        unavailable = gpu_details[0].startswith(GPU_INFORMATION_UNAVAILABLE)
        return ResourceSummary(
            key="gpu",
            title="GPU",
            value=self._gpu_display_name(gpu_details),
            subtitle=(
                "Information unavailable" if unavailable else "Graphics hardware"
            ),
            percent=None,
            details=details,
            failed=unavailable,
            capability=capability,
            temperatures=temperature_samples,
        )

    @classmethod
    def _gpu_display_name(cls, gpu_details: tuple[str, ...]) -> str:
        """Return a concise card name for the first GPU detail line.

        The full raw/canonical identifier stays in ``gpu_details`` (and
        therefore in View Details); only the overview headline is shortened
        when a trustworthy concise name can be derived, otherwise the raw
        line is returned unchanged.
        """

        raw = gpu_details[0]
        concise = cls._concise_gpu_name(raw)
        return concise or raw

    @classmethod
    def _concise_gpu_name(cls, raw: str) -> str | None:
        """Derive a concise GPU product name, or None when unsure.

        Only lspci-style descriptions (a ``[...]`` group or a known vendor
        prefix) are rewritten, so NVML, macOS, Windows, unavailable, and
        short names pass through untouched and no model is ever fabricated.
        """

        line = raw.strip()
        brackets = re.findall(r"\[([^\]]+)\]", line)
        if brackets:
            candidate: str | None = None
            for group in reversed(brackets):
                product = group.rsplit("[", 1)[-1].strip()
                if product and product.casefold() not in cls.GPU_VENDOR_TAGS:
                    candidate = product
                    break
            if candidate is None:
                candidate = line.rsplit("]", 1)[-1]
        else:
            for prefix in cls.GPU_VENDOR_PREFIXES:
                if line.startswith(prefix):
                    candidate = line[len(prefix) :]
                    break
            else:
                return None

        candidate = cls.GPU_REVISION_PATTERN.sub("", candidate)
        candidate = candidate.strip().lstrip(",; ")
        return candidate or None

    def _battery_resource(
        self,
        battery: Any,
        battery_error: Exception | None,
        temperature_lines: list[str],
        temperature_samples: tuple[TemperatureSample, ...] = (),
    ) -> ResourceSummary:
        if battery is not None:
            subtitle = "Charging" if battery.power_plugged else "Not charging"
            details: list[str] = [
                f"Charge: {battery.percent:.1f}%",
                f"Power: {subtitle}",
            ]
            remaining = self._battery_time_remaining(battery)
            if remaining is not None:
                details.append(f"Time remaining: {remaining}")
            if temperature_lines:
                details.extend(temperature_lines)
            return ResourceSummary(
                key="battery",
                title="Battery",
                value=f"{battery.percent:.0f}%",
                subtitle=subtitle,
                percent=battery.percent,
                details=tuple(details),
                capability=CapabilityState.SUPPORTED,
                temperatures=temperature_samples,
            )

        if battery_error is not None:
            return ResourceSummary(
                key="battery",
                title="Battery",
                value="Unavailable",
                subtitle="Battery unavailable",
                percent=None,
                details=("Battery information is unavailable.",),
                failed=True,
                capability=(
                    CapabilityState.PERMISSION_LIMITED
                    if isinstance(battery_error, PermissionError)
                    else CapabilityState.TEMPORARILY_UNAVAILABLE
                ),
                temperatures=temperature_samples,
            )

        if temperature_lines:
            return ResourceSummary(
                key="battery",
                title="Battery",
                value="No battery",
                subtitle="Not present on this system",
                percent=None,
                details=tuple(temperature_lines),
                capability=CapabilityState.UNSUPPORTED,
                temperatures=temperature_samples,
            )
        return ResourceSummary(
            key="battery",
            title="Battery",
            value="No battery",
            subtitle="Not present on this system",
            percent=None,
            details=("No temperature sensors detected.",),
            capability=CapabilityState.UNSUPPORTED,
            temperatures=temperature_samples,
        )

    @staticmethod
    def _temperature_value(
        temperature_lines: list[str],
        category: str,
    ) -> str | None:
        """Return one category's temperature suffix (e.g. ``"49°C"``) or None."""

        return detail_line_suffix(temperature_lines, f"{category}: ")

    @staticmethod
    def _battery_time_remaining(battery: Any) -> str | None:
        """Return a human time-left suffix for a discharging battery, or None.

        psutil reports ``secsleft`` only on real battery objects; missing,
        infinite, and non-positive values are treated as unknown so no
        fabricated estimate ever reaches the card.
        """

        if getattr(battery, "power_plugged", True):
            return None
        secsleft = getattr(battery, "secsleft", None)
        if not isinstance(secsleft, (int, float)) or not scanner_module.math.isfinite(
            secsleft
        ):
            return None
        if secsleft <= 0:
            return None
        minutes = int(secsleft // 60) + 1
        return f"~{minutes // 60}h {minutes % 60:02d}m"

    @staticmethod
    def _read_battery(psutil_module: Any) -> tuple[Any, Exception | None]:
        """Return the battery reading and any error, keeping them distinct.

        `sensors_battery()` returns `None` when no battery is present and
        raises when the reading itself failed, so the card can tell "no
        battery" apart from "temporarily unreadable".
        """

        try:
            return psutil_module.sensors_battery(), None
        except Exception as error:  # noqa: BLE001 - battery is best-effort.
            scanner_module.LOGGER.warning("Battery read failed: %s", error)
            return None, error

    def _cached_temperature_lines(self, psutil_module: Any) -> list[str]:
        """Return temperature lines, re-read at most every refresh window.

        Temperature reads cost a few milliseconds, so per-component refreshes
        (e.g. CPU at ~1s) reuse this cached copy until it is older than
        `TEMPERATURE_REFRESH_SECONDS` instead of hammering the sensor.
        """

        return list(self._cached_temperature_scan(psutil_module).lines)

    def _cached_temperature_scan(self, psutil_module: Any) -> TemperatureScan:
        return self._ttl_cached_value(
            lock=self._temperature_cache_lock,
            cache_name="_temperature_cache",
            ttl_seconds=self.TEMPERATURE_REFRESH_SECONDS,
            loader=lambda: self._temperature_scan(psutil_module),
        )

    @classmethod
    def _temperature_scan(cls, psutil_module: Any) -> TemperatureScan:
        """Return normalised temperature samples plus the concise card lines."""

        system = scanner_module.platform.system()
        if not cls._temperature_sensors_supported(system):
            now = scanner_module.datetime.now(scanner_module.timezone.utc).astimezone()
            return TemperatureScan(now, scanner_module.time.monotonic(), (), ())
        if system == "Darwin":
            return temperature_platform.macos_temperature_scan()
        if system == "Windows":
            return temperature_platform.windows_temperature_scan(
                runner=scanner_module.subprocess.run
            )

        sensors = cls._psutil_value(lambda: psutil_module.sensors_temperatures())
        if not isinstance(sensors, dict) or not sensors:
            now = scanner_module.datetime.now(scanner_module.timezone.utc).astimezone()
            return TemperatureScan(now, scanner_module.time.monotonic(), (), ())

        captured_at = scanner_module.datetime.now(
            scanner_module.timezone.utc
        ).astimezone()
        captured_monotonic = scanner_module.time.monotonic()
        component_map: dict[str, tuple[str, tuple[str, ...]]] = {
            "cpu": ("CPU", cls.CPU_TEMP_DRIVERS),
            "gpu": ("GPU", cls.GPU_TEMP_DRIVERS),
            "storage": ("NVMe", cls.NVME_TEMP_DRIVERS),
        }
        lines: list[str] = []
        grouped: list[tuple[str, tuple[TemperatureSample, ...]]] = []

        for component, (label, drivers) in component_map.items():
            readings: list[TemperatureSample] = []
            for name, entries in sensors.items():
                if not any(driver in name.casefold() for driver in drivers):
                    continue
                if not isinstance(entries, (list, tuple)):
                    continue
                for index, entry in enumerate(entries):
                    current = getattr(entry, "current", None)
                    if not cls._sensible_temperature(current):
                        continue
                    sensor_name = str(
                        getattr(entry, "label", None)
                        or getattr(entry, "sensor", None)
                        or name
                    )
                    sensor_id = f"{name}:{index}:{sensor_name}"
                    readings.append(
                        TemperatureSample(
                            component=component,
                            sensor_id=sensor_id,
                            sensor_name=sensor_name,
                            value_celsius=float(current),
                            sampled_at=captured_at,
                            sampled_monotonic=captured_monotonic,
                        )
                    )
            if readings:
                grouped.append((component, tuple(readings)))
                lines.append(
                    f"{label}: {max(sample.value_celsius for sample in readings):.0f}°C"
                )

        return TemperatureScan(
            captured_at, captured_monotonic, tuple(lines), tuple(grouped)
        )

    @staticmethod
    def _temperature_sensors_supported(system: str | None) -> bool:
        """Return whether the OS can produce temperature readings.

        On Linux this uses psutil's ``sensors_temperatures``; on macOS the
        Apple SMC is read directly on Intel machines; on Windows the ACPI
        thermal zones are read through PowerShell/WMI. Any unsupported or
        failing source degrades to no-data without a misleading sensor-read
        warning.
        """

        return system in ("Linux", "Darwin", "Windows")

    @classmethod
    def _temperature_lines(cls, psutil_module: Any) -> list[str]:
        """Return the legacy concise temperature lines for compatibility tests."""

        return list(cls._temperature_scan(psutil_module).lines)

    @staticmethod
    def _sensible_temperature(value: Any) -> TypeGuard[int | float]:
        return is_valid_temperature_value(value)
