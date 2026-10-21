# ruff: noqa: F401
# mypy: disable-error-code="attr-defined,misc,has-type,assignment,valid-type,name-defined"

# Compatibility imports remain on this historical module for callers and
# monkeypatch targets; support mixins resolve their runtime dependencies here.
import contextlib
import ctypes
import getpass
import logging
import math
import os
import platform
import re
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, TypeGuard, TypeVar, cast

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _NetworkObservation:
    """One best-effort read of the global/per-interface counters and interface stats.

    ``None`` means the corresponding query failed; an empty mapping means the
    query succeeded but produced no entries. Callers keep those two cases
    distinct (e.g. a disconnected host still reports up/down state).
    """

    global_counters: Any
    per_interface_counters: Any
    interface_stats: Any


from maintenance.components import (
    GPU_INFORMATION_UNAVAILABLE,
    PROTECTED_PROCESS_NAMES,
    DownloadsPathResolver,
    GpuDetector,
    HashFingerprint,
    ProgressCallback,
    ResourceFeatureCatalog,
    ScanCancelled,
    check_cancelled,
    gpu_unavailable_message,
    protected_process_pids,
    require_psutil,
    usernames_match,
    windows_windll,
)
from maintenance.components import (
    DownloadScanner as ComponentDownloadScanner,
)
from maintenance.components.gpu import (
    GpuProbe,
    gpu_probe_from_read,
    nvidia_device_readings,
)
from maintenance.components.process_safety import is_protected_process_name
from maintenance.components.scan_support import (
    BYTE_SCALE_UNITS,
    call_cancellable,
    detail_line_suffix,
    file_content_marker,
    file_sha256,
    stat_fingerprint,
    walk_directory_entries,
)
from maintenance.components.temperature import TemperatureSample, TemperatureScan
from maintenance.external_commands import run_json_command, run_text_command
from maintenance.models import (
    CapabilityState,
    DashboardSnapshot,
    FileCandidate,
    ProcessActionState,
    ProcessCandidate,
    ResourceSummary,
    unavailable_summary,
)

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None  # type: ignore[assignment]

try:
    import pynvml  # type: ignore[import-untyped]
except ImportError:
    pynvml = None


class _RecycleBinInfo(ctypes.Structure):
    _fields_ = [
        ("cb_size", ctypes.c_ulong),
        ("size", ctypes.c_longlong),
        ("item_count", ctypes.c_longlong),
    ]


from maintenance.scanner_support.dashboard import DashboardMixin
from maintenance.scanner_support.gpu import GpuMixin
from maintenance.scanner_support.network import NetworkMixin
from maintenance.scanner_support.paths import PathsMixin
from maintenance.scanner_support.processes import ProcessesMixin
from maintenance.scanner_support.storage import StorageMixin


class SystemScanner(
    DashboardMixin,
    NetworkMixin,
    GpuMixin,
    ProcessesMixin,
    StorageMixin,
    PathsMixin,
):
    """Read system state and discover reviewable cleanup candidates."""

    LARGE_FILE_BYTES: int = 100 * 1024**2
    DUPLICATE_MIN_BYTES: int = 1024**2
    HASH_CHUNK_BYTES: int = 1024**2
    HASH_CACHE_MAX_ENTRIES: int = 1024
    PROCESS_MIN_BYTES: int = 10 * 1024**2
    PROCESS_ACTIVITY_MIN_CPU_PERCENT: float = 1.0
    PROCESS_SAMPLE_SECONDS: float = 0.25
    CPU_PERCENT_SAMPLE_SECONDS: float = 0.2
    CPU_WORKER_TIMEOUT_SECONDS: float = 5.0
    CPU_CANCEL_POLL_SECONDS: float = 0.05
    TEMPERATURE_REFRESH_SECONDS: float = 5.0
    TRASH_SIZE_REFRESH_SECONDS: float = 60.0
    GPU_COMMAND_TIMEOUT_SECONDS: int = 10
    GPU_QUERY_TIMEOUT_SECONDS: float = 12.0
    GPU_QUERY_ABANDON_SECONDS: float = 60.0
    GPU_QUERY_TIMEOUT_MESSAGE = f"{GPU_INFORMATION_UNAVAILABLE}: query timed out"
    _COMPONENT_FEATURES = ResourceFeatureCatalog().all()
    COMPONENT_KEYS: tuple[str, ...] = tuple(
        feature.key for feature in _COMPONENT_FEATURES
    )
    COMPONENT_TITLES: ClassVar[dict[str, str]] = {
        feature.key: feature.title for feature in _COMPONENT_FEATURES
    }
    TUNNEL_INTERFACE_PREFIXES: frozenset[str] = frozenset(
        {"tun", "tap", "utun", "ppp", "ipsec", "wg"}
    )
    CPU_TEMP_DRIVERS: tuple[str, ...] = (
        "coretemp",
        "k10temp",
        "zenpower",
        "cpu_thermal",
        "x86_pkg_temp",
        "acpitz",
    )
    GPU_TEMP_DRIVERS: tuple[str, ...] = ("amdgpu", "nouveau", "radeon")
    NVME_TEMP_DRIVERS: tuple[str, ...] = ("nvme",)
    GPU_VENDOR_TAGS: frozenset[str] = frozenset({"amd", "ati", "amd/ati"})
    GPU_VENDOR_PREFIXES: tuple[str, ...] = (
        "Advanced Micro Devices, Inc. ",
        "Advanced Micro Devices, Inc., ",
        "NVIDIA Corporation ",
        "Intel Corporation ",
        "Corporation ",
    )
    GPU_REVISION_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"\s*\(rev [0-9a-fA-F.]+\)$"
    )
    PROTECTED_PROCESS_NAMES = PROTECTED_PROCESS_NAMES

    def __init__(self, downloads_path: Path | None = None) -> None:
        self.downloads_path = downloads_path or self._default_downloads_path()
        self._download_scanner = ComponentDownloadScanner(self.downloads_path)
        self._hash_cache: OrderedDict[Path, tuple[HashFingerprint, bytes, str]] = (
            self._download_scanner._hash_cache
        )
        self._hash_cache_lock = self._download_scanner._hash_cache_lock
        self._downloads_scan_lock = self._download_scanner._downloads_scan_lock
        self._static_gpu_details: tuple[str, ...] | None = None
        self._static_gpu_lock = threading.Lock()
        self._nvml_probe_failed = False
        self._gpu_query_lock = threading.Lock()
        self._gpu_query_in_flight = False
        self._gpu_query_generation = 0
        self._gpu_query_timed_out_at: float | None = None
        self._gpu_capabilities_by_thread: dict[int, CapabilityState] = {}
        self._static_hardware_lock = threading.Lock()
        self._static_system_label: str | None = None
        self._static_system_label_fingerprint: tuple[Any, ...] | None = None
        self._static_cpu_cores: tuple[int | None, int | None] | None = None
        self._static_cpu_cores_fingerprint: tuple[Any, ...] | None = None
        self._static_gpu_fingerprint: tuple[Any, ...] | None = None
        self._network_sample: tuple[float, int, int] | None = None
        self._network_sample_lock = threading.Lock()
        self._temperature_cache: tuple[float, TemperatureScan] | None = None
        self._temperature_cache_lock = threading.Lock()
        self._trash_size_cache: tuple[float, int] | None = None
        self._trash_size_cache_lock = threading.Lock()
        self._cpu_read_lock = threading.Lock()
        self._cpu_request_lock = threading.RLock()
        self._cpu_seeded = False
        self._cpu_request_event = threading.Event()
        self._cpu_result_event = threading.Event()
        self._cpu_value: float | None = None
        self._cpu_error: str | None = None
        self._cpu_stop_event = threading.Event()
        self._cpu_worker: threading.Thread | None = None
        self._cpu_last_logged_error: str | None = None

    def _sync_download_scanner(self) -> ComponentDownloadScanner:
        scanner = self._download_scanner
        scanner.downloads_path = self.downloads_path
        scanner.LARGE_FILE_BYTES = self.LARGE_FILE_BYTES
        scanner.DUPLICATE_MIN_BYTES = self.DUPLICATE_MIN_BYTES
        scanner.HASH_CHUNK_BYTES = self.HASH_CHUNK_BYTES
        scanner.HASH_CACHE_MAX_ENTRIES = self.HASH_CACHE_MAX_ENTRIES
        scanner._check_cancelled = self._check_cancelled  # type: ignore[attr-defined,method-assign]
        scanner._file_hash = self._download_file_hash  # type: ignore[attr-defined,method-assign]
        scanner._file_content_marker = self._download_file_content_marker  # type: ignore[attr-defined,method-assign]
        return scanner

    @classmethod
    def _make_downloads_path_resolver(cls) -> DownloadsPathResolver:
        resolver = DownloadsPathResolver()
        resolver._windows_downloads_path = cls._windows_downloads_path  # type: ignore[attr-defined,method-assign]
        resolver._is_directory = cls._is_directory  # type: ignore[attr-defined,method-assign]
        resolver._path_exists = cls._path_exists  # type: ignore[attr-defined,method-assign]
        resolver._safe_downloads_fallback = cls._safe_downloads_fallback  # type: ignore[attr-defined,method-assign]
        return resolver

    def _download_file_hash(
        self,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> str:
        return call_cancellable(
            lambda: self._file_hash(path, cancel_event=cancel_event),
            lambda: self._file_hash(path),
            cancel_event,
        )

    def _download_file_content_marker(
        self,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        return call_cancellable(
            lambda: self._file_content_marker(path, cancel_event=cancel_event),
            lambda: self._file_content_marker(path),
            cancel_event,
        )

    @staticmethod
    def _require_psutil() -> Any:
        return require_psutil(psutil)

    @staticmethod
    def _byte_unit(value: float) -> tuple[int, str]:
        """Return the (scale, unit) of the largest unit a value fits in.

        The scale/unit table is shared with `maintenance.health` so the
        units the scanner prints are always the units health parses.
        """

        for scale, unit in BYTE_SCALE_UNITS:
            if abs(value) >= scale:
                return scale, unit
        return 1, "B"

    @classmethod
    def format_bytes(cls, number_of_bytes: float) -> str:
        value = float(number_of_bytes)
        scale, unit = cls._byte_unit(value)
        return f"{value / scale:.2f} {unit}"

    def scan_dashboard(
        self,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> DashboardSnapshot:
        self._check_cancelled(cancel_event)
        self._invalidate_trash_size()
        resources = []
        for key in self.COMPONENT_KEYS:
            self._check_cancelled(cancel_event)
            if progress_callback is not None:
                progress_callback(f"Scanning {self._component_title(key)}...")
            resources.append(self.scan_component(key, cancel_event))
        self._check_cancelled(cancel_event)
        return DashboardSnapshot(
            system_label=self._system_label(),
            scanned_at=datetime.now(timezone.utc).astimezone(),
            resources=tuple(resources),
        )

    @staticmethod
    def _component_title(key: str) -> str:
        return SystemScanner.COMPONENT_TITLES.get(key, key)

    def scan_component(
        self,
        key: str,
        cancel_event: threading.Event | None = None,
    ) -> ResourceSummary:
        """Scan one dashboard component independently and return its card.

        Each component reads only its own sensors, so one slow or failing
        component never delays the others. Static hardware (CPU cores, GPU
        model, system label) stays cached; live values are re-read here.
        """

        self._check_cancelled(cancel_event)
        psutil_module: Any = psutil

        if key == "cpu":
            cpu_percent = self._component_value(
                lambda: self._read_cpu_percent(psutil_module, cancel_event)
            )
            frequency = self._component_value(lambda: psutil_module.cpu_freq())
            temperature_scan = self._cached_temperature_scan(psutil_module)
            return self._resource_with_fallback(
                lambda: self._cpu_resource(
                    cpu_percent,
                    frequency,
                    list(temperature_scan.lines),
                    temperature_scan.samples_for("cpu"),
                    temperature_unavailable_reason=temperature_scan.unavailable_reason,
                ),
                "cpu",
                self._component_title("cpu"),
            )

        if key == "memory":
            memory = self._component_value(lambda: psutil_module.virtual_memory())
            swap = self._component_value(lambda: psutil_module.swap_memory())
            return self._resource_with_fallback(
                lambda: self._memory_resource(memory, swap),
                "memory",
                self._component_title("memory"),
            )

        if key == "storage":
            disk = self._component_value(
                lambda: psutil_module.disk_usage(str(Path.home()))
            )
            trash_bytes = self._safe_trash_size()
            temperature_scan = self._cached_temperature_scan(psutil_module)
            return self._resource_with_fallback(
                lambda: self._storage_resource(
                    disk,
                    trash_bytes,
                    list(temperature_scan.lines),
                    temperature_scan.samples_for("storage"),
                ),
                "storage",
                self._component_title("storage"),
            )

        if key == "gpu":
            details = self.gpu_details()
            capability = self._gpu_capability_for_thread()
            temperature_scan = self._cached_temperature_scan(psutil_module)
            return self._resource_with_fallback(
                lambda: self._gpu_resource(
                    details,
                    list(temperature_scan.lines),
                    temperature_scan.samples_for("gpu"),
                    capability=capability,
                ),
                "gpu",
                self._component_title("gpu"),
            )

        if key == "network":
            observation = self._read_network_observation(psutil_module)
            capability = self._network_capability(observation)
            return self._resource_with_fallback(
                lambda: self._network_resource(
                    observation,
                    capability=capability,
                ),
                "network",
                self._component_title("network"),
            )

        if key == "battery":
            battery, battery_error = self._read_battery(psutil_module)
            temperature_scan = self._cached_temperature_scan(psutil_module)
            return self._resource_with_fallback(
                lambda: self._battery_resource(
                    battery,
                    battery_error,
                    list(temperature_scan.lines),
                    temperature_scan.samples_for("battery"),
                ),
                "battery",
                self._component_title("battery"),
            )

        raise ValueError(f"Unknown component: {key}")

    def _component_value(self, query: Callable[[], Any]) -> Any:
        """Read one component sensor, or None when psutil is unavailable.

        Per-component scans use this so the missing-psutil case is handled in
        exactly one place instead of in every component branch.
        """

        if psutil is None:
            return None
        return self._psutil_value(query)

    def scan_downloads(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        scan_root: Path | None = None,
    ) -> list[FileCandidate]:
        return self._sync_download_scanner().scan_downloads(
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            scan_root=scan_root,
        )

    def _download_file_stats(
        self,
        root: Path,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[Path, os.stat_result]:
        return self._sync_download_scanner()._download_file_stats(
            root,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

    @classmethod
    def _file_hash(
        cls,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> str:
        return file_sha256(
            path,
            chunk_bytes=cls.HASH_CHUNK_BYTES,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _check_cancelled(cancel_event: threading.Event | None) -> None:
        check_cancelled(cancel_event)

    @staticmethod
    def _hash_fingerprint(stat: os.stat_result) -> HashFingerprint:
        return stat_fingerprint(stat)

    def _file_content_marker(
        self,
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        """Validate the complete file content before reusing a digest."""
        return file_content_marker(
            path,
            chunk_bytes=self.HASH_CHUNK_BYTES,
            cancel_event=cancel_event,
        )
