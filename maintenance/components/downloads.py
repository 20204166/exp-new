"""Downloads discovery: path resolution, duplicate detection, hash caching.

`DownloadsPathResolver` owns platform Downloads-root resolution (including
the Windows known-folder interop). `DownloadScanner` owns large-file and
verified-duplicate discovery with the bounded hash cache. Platform-specific
parsing stays in this module; generic command execution lives in
`maintenance.external_commands` and shared primitives in
`maintenance.scan_support`.
"""

import ctypes
import os
import platform
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from maintenance.models import FileCandidate

from .scan_support import (
    ProgressCallback,
    call_cancellable,
    call_legacy_compatible,
    check_cancelled,
    file_content_marker,
    file_sha256,
    stat_fingerprint,
    walk_directory_entries,
    windows_windll,
)

HashFingerprint = tuple[int, int, int, int, int]


class _WindowsGuid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


_WINDOWS_DOWNLOADS_GUID = _WindowsGuid(
    0x374DE290,
    0x123F,
    0x4565,
    (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
)


def local_scan_anchor(
    *,
    system: Callable[[], str] = lambda: platform.system(),
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the local filesystem anchor for an opt-in broad storage scan.

    Windows resolves to the system drive root (``%SystemDrive%``, falling
    back to ``C:\\``); every other platform resolves to ``/``. This is the
    scan *root* only - it carries no cleanup authority, which stays with
    ``FileManager.allowed_root``.
    """

    if system() == "Windows":
        env = environment or os.environ
        drive = env.get("SystemDrive", "C:")
        return Path(f"{drive}\\")
    return Path("/")


class DownloadsPathResolver:
    """Resolve the Downloads root.

    Extracted from the Downloads-root resolution code in `maintenance/scanner.py`.
    """

    def __init__(
        self,
        *,
        system: Callable[[], str] = lambda: platform.system(),
        home: Callable[[], Path] = lambda: Path.home(),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._system = system
        self._home = home
        self._environment = environment or os.environ

    def select(self, downloads_path: Path | None = None) -> Path:
        """Return the explicit Downloads path or the platform default."""

        if downloads_path is not None:
            return downloads_path
        return self._default_downloads_path()

    def _default_downloads_path(self) -> Path:
        if self._system() == "Windows":
            known_folder = self._windows_downloads_path()
            if known_folder is not None and self._is_directory(known_folder):
                return known_folder

            home = Path(self._environment.get("USERPROFILE", self._home()))
            one_drive = self._environment.get("OneDrive")
            one_drive_downloads = Path(one_drive) / "Downloads" if one_drive else None
            if one_drive_downloads and self._is_directory(one_drive_downloads):
                return one_drive_downloads
            return self._safe_downloads_fallback(home)
        return self._safe_downloads_fallback(Path(self._home()))

    @staticmethod
    def _is_directory(path: Path) -> bool:
        try:
            return path.is_dir()
        except (OSError, ValueError):
            return False

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except (OSError, ValueError):
            return True

    @classmethod
    def _safe_downloads_fallback(cls, home: Path) -> Path:
        candidate = home / "Downloads"
        if cls._path_exists(candidate) and not cls._is_directory(candidate):
            sentinel = home / "Downloads.__unavailable__"
            suffix = 0
            while cls._path_exists(sentinel):
                suffix += 1
                sentinel = home / f"Downloads.__unavailable__.{suffix}"
            return sentinel
        return candidate

    @staticmethod
    def _windows_downloads_path() -> Path | None:
        windll = windows_windll()
        try:
            shell32 = windll.shell32 if windll is not None else None
            ole32 = windll.ole32 if windll is not None else None
            get_known_folder_path = shell32.SHGetKnownFolderPath if shell32 else None
            co_initialize = ole32.CoInitializeEx if ole32 else None
            co_uninitialize = ole32.CoUninitialize if ole32 else None
            free_memory = ole32.CoTaskMemFree if ole32 else None
        except (AttributeError, OSError, ctypes.ArgumentError):
            return None
        if (
            get_known_folder_path is None
            or co_initialize is None
            or co_uninitialize is None
            or free_memory is None
        ):
            return None

        path_pointer = ctypes.c_wchar_p()
        initialization_result: int | None = None
        try:
            co_initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            co_initialize.restype = ctypes.c_long
            initialization_result = co_initialize(None, 0x2)
            if initialization_result not in (0, 1):
                return None
            co_uninitialize.argtypes = []
            co_uninitialize.restype = None
            free_memory.argtypes = [ctypes.c_void_p]
            free_memory.restype = None
            get_known_folder_path.argtypes = [
                ctypes.POINTER(_WindowsGuid),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            get_known_folder_path.restype = ctypes.c_long
            result = get_known_folder_path(
                ctypes.byref(_WINDOWS_DOWNLOADS_GUID),
                0,
                None,
                ctypes.byref(path_pointer),
            )
            if result != 0 or not path_pointer.value:
                return None
            return Path(path_pointer.value)
        except (OSError, TypeError, AttributeError, ctypes.ArgumentError):
            return None
        finally:
            pointer_address = ctypes.cast(path_pointer, ctypes.c_void_p).value
            if pointer_address:
                try:
                    free_memory(ctypes.c_void_p(pointer_address))
                except (OSError, TypeError, AttributeError, ctypes.ArgumentError):
                    pass
            if initialization_result in (0, 1):
                try:
                    co_uninitialize()
                except (OSError, TypeError, AttributeError, ctypes.ArgumentError):
                    pass


class DownloadScanner:
    """Scan Downloads for large files and verified duplicates.

    Extracted from `maintenance/scanner.py:352-585, 703-790`.
    """

    LARGE_FILE_BYTES: int = 100 * 1024**2
    DUPLICATE_MIN_BYTES: int = 1024**2
    HASH_CHUNK_BYTES: int = 1024**2
    HASH_CACHE_MAX_ENTRIES: int = 1024

    def __init__(
        self,
        downloads_path: Path | None = None,
        *,
        resolver: DownloadsPathResolver | None = None,
    ) -> None:
        self._resolver = resolver or DownloadsPathResolver()
        self.downloads_path = self._resolver.select(downloads_path)
        self._hash_cache: OrderedDict[Path, tuple[HashFingerprint, bytes, str]] = (
            OrderedDict()
        )
        self._hash_cache_lock = threading.Lock()
        self._downloads_scan_lock = threading.Lock()

    def scan_downloads(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        scan_root: Path | None = None,
    ) -> list[FileCandidate]:
        while not self._downloads_scan_lock.acquire(timeout=0.1):
            self._check_cancelled(cancel_event)
        try:
            return self._scan_downloads(
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                scan_root=scan_root,
            )
        finally:
            self._downloads_scan_lock.release()

    def _scan_downloads(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        scan_root: Path | None = None,
    ) -> list[FileCandidate]:
        self._check_cancelled(cancel_event)
        root = (self.downloads_path if scan_root is None else scan_root).expanduser()
        if not root.exists():
            self._prune_hash_cache({}, cancel_event=cancel_event)
            return []

        if progress_callback is None and cancel_event is None:
            file_stats = self._download_file_stats(root)
        else:
            file_stats = call_cancellable(
                lambda: self._download_file_stats(
                    root,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                ),
                lambda: self._download_file_stats(root),
                cancel_event,
            )
        self._prune_hash_cache(file_stats, cancel_event=cancel_event)
        if progress_callback is None and cancel_event is None:
            reasons = self._download_reasons(file_stats)
        else:
            reasons = call_cancellable(
                lambda: self._download_reasons(
                    file_stats,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                ),
                lambda: self._download_reasons(file_stats),
                cancel_event,
            )

        candidates: list[FileCandidate] = []
        for path, path_reasons in reasons.items():
            self._check_cancelled(cancel_event)
            candidates.append(
                self._download_candidate(path, file_stats[path], path_reasons)
            )
        self._check_cancelled(cancel_event)
        if progress_callback is not None:
            progress_callback(f"Scan complete: {len(candidates)} candidate(s)")
        return sorted(
            candidates,
            key=lambda item: (
                -item.size_bytes,
                str(item.path).casefold(),
                str(item.path),
            ),
        )

    def _download_file_stats(
        self,
        root: Path,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[Path, os.stat_result]:
        file_stats: dict[Path, os.stat_result] = {}
        last_reported_file_count = 0

        for entry in walk_directory_entries(
            root,
            skip_dotfiles=True,
            cancel_event=cancel_event,
        ):
            path = Path(entry.path)
            try:
                file_stats[path] = path.stat()
            except (OSError, ValueError):
                continue

            if progress_callback is not None and (
                len(file_stats) == 1
                or len(file_stats) - last_reported_file_count >= 100
            ):
                progress_callback(f"Found {len(file_stats)} file(s) in Downloads")
                last_reported_file_count = len(file_stats)

        if (
            progress_callback is not None
            and len(file_stats) != last_reported_file_count
        ):
            progress_callback(f"Found {len(file_stats)} file(s) in Downloads")

        return file_stats

    @staticmethod
    def _is_download_file(path: Path, root: Path) -> bool:
        if path.is_symlink() or not path.is_file():
            return False

        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            return False

        return not any(part.startswith(".") for part in relative_parts)

    def _download_reasons(
        self,
        file_stats: dict[Path, os.stat_result],
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[Path, list[str]]:
        reasons: dict[Path, list[str]] = defaultdict(list)
        self._mark_large_downloads(
            file_stats,
            reasons,
            cancel_event=cancel_event,
        )
        self._mark_duplicate_downloads(
            file_stats,
            reasons,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        return reasons

    def _mark_large_downloads(
        self,
        file_stats: dict[Path, os.stat_result],
        reasons: dict[Path, list[str]],
        cancel_event: threading.Event | None = None,
    ) -> None:
        for path, stat in file_stats.items():
            self._check_cancelled(cancel_event)
            if stat.st_size >= self.LARGE_FILE_BYTES:
                reasons[path].append("Large file")

    def _mark_duplicate_downloads(
        self,
        file_stats: dict[Path, os.stat_result],
        reasons: dict[Path, list[str]],
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Mark verified content duplicates using a staged approach.

        Stage 1 groups files by exact size (the cheap comparison); only groups
        with two or more entries are hashed, so a full content read never
        happens for size-unique files. Stage 2 hashes those plausible matches
        and groups by digest. Duplicates are identified by content only -
        never by filename - and all-but-one (by path order) are marked.
        """

        files_by_size: dict[int, list[Path]] = defaultdict(list)
        for path, stat in file_stats.items():
            self._check_cancelled(cancel_event)
            if stat.st_size >= self.DUPLICATE_MIN_BYTES:
                files_by_size[stat.st_size].append(path)

        hash_total = sum(
            len(same_size_paths)
            for same_size_paths in files_by_size.values()
            if len(same_size_paths) >= 2
        )
        hashed_count = 0

        for same_size_paths in files_by_size.values():
            self._check_cancelled(cancel_event)
            if len(same_size_paths) < 2:
                continue

            files_by_hash: dict[str, list[Path]] = defaultdict(list)
            for path in same_size_paths:
                self._check_cancelled(cancel_event)
                try:
                    files_by_hash[
                        self._cached_file_hash(
                            path,
                            file_stats[path],
                            cancel_event=cancel_event,
                        )
                    ].append(path)
                except OSError:
                    continue
                finally:
                    hashed_count += 1
                    if progress_callback is not None and (
                        hashed_count == 1
                        or hashed_count == hash_total
                        or hashed_count % 16 == 0
                    ):
                        progress_callback(
                            f"Checking duplicates: {hashed_count}/{hash_total}"
                        )

            for digest, duplicate_paths in files_by_hash.items():
                if len(duplicate_paths) < 2:
                    continue

                ordered_paths = sorted(
                    duplicate_paths,
                    key=lambda item: (str(item).casefold(), str(item)),
                )
                for duplicate_path in ordered_paths[1:]:
                    reasons[duplicate_path].append(f"Verified duplicate ({digest[:8]})")

    @staticmethod
    def _download_candidate(
        path: Path,
        stat: os.stat_result,
        reasons: list[str],
    ) -> FileCandidate:
        return FileCandidate(
            path=path,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).astimezone(),
            reason=", ".join(reasons),
        )

    @staticmethod
    def _file_hash(
        path: Path,
        cancel_event: threading.Event | None = None,
    ) -> str:
        return file_sha256(
            path,
            chunk_bytes=DownloadScanner.HASH_CHUNK_BYTES,
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

    def _prune_hash_cache(
        self,
        file_stats: dict[Path, os.stat_result],
        cancel_event: threading.Event | None = None,
    ) -> None:
        current_fingerprints: dict[Path, HashFingerprint] = {}
        for path, stat in file_stats.items():
            self._check_cancelled(cancel_event)
            current_fingerprints[path] = self._hash_fingerprint(stat)
        with self._hash_cache_lock:
            retained_items = [
                (
                    path,
                    cached,
                )
                for path, cached in self._hash_cache.items()
                if path in current_fingerprints
                and cached[0] == current_fingerprints[path]
            ]
            self._hash_cache.clear()
            self._hash_cache.update(retained_items)
            while len(self._hash_cache) > self.HASH_CACHE_MAX_ENTRIES:
                self._hash_cache.popitem(last=False)

    def _cached_file_hash(
        self,
        path: Path,
        stat: os.stat_result,
        cancel_event: threading.Event | None = None,
    ) -> str:
        for _attempt in range(2):
            self._check_cancelled(cancel_event)
            fingerprint = self._hash_fingerprint(stat)
            marker = self._file_content_marker(path, cancel_event)
            with self._hash_cache_lock:
                cached = self._hash_cache.get(path)

            if cached is not None and cached[0] == fingerprint and cached[1] == marker:
                current_stat = path.stat()
                current_marker = self._file_content_marker(path, cancel_event)
                if (
                    self._hash_fingerprint(current_stat) == fingerprint
                    and current_marker == marker
                ):
                    with self._hash_cache_lock:
                        self._hash_cache.move_to_end(path)
                    return cached[2]
                stat = current_stat
                continue

            if cancel_event is None:
                digest = self._file_hash(path)
            else:
                digest = call_legacy_compatible(
                    lambda: self._file_hash(path, cancel_event),
                    lambda: self._file_hash(path),
                    error_substring="positional argument",
                    after_fallback=lambda: self._check_cancelled(cancel_event),
                )
            current_stat = path.stat()
            current_marker = self._file_content_marker(path, cancel_event)
            if (
                self._hash_fingerprint(current_stat) == fingerprint
                and current_marker == marker
            ):
                with self._hash_cache_lock:
                    self._hash_cache[path] = (fingerprint, current_marker, digest)
                    self._hash_cache.move_to_end(path)
                    while len(self._hash_cache) > self.HASH_CACHE_MAX_ENTRIES:
                        self._hash_cache.popitem(last=False)
                return digest
            stat = current_stat

        raise OSError(f"File changed while hashing: {path}")
