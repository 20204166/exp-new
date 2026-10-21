"""Shared scan-support primitives used across the maintenance package.

This module is the single home for the cancellation error, the cancellation
check, the psutil availability guard, the Windows DLL accessor, the progress
callback aliases, the legacy-signature compatibility helper, and the file
hashing/fingerprinting helpers shared by the scanner and the downloads
scanner. It imports nothing from the rest of the package so every other
module can depend on it without cycles.
"""

import ctypes
import hashlib
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

ProgressCallback = Callable[[str], None]
ProgressTask = Callable[[ProgressCallback, threading.Event], Any]

DOWNLOADS_SCAN_CANCELLED = "Downloads scan cancelled"
SCAN_CANCELLED_NOTICE = "Scan cancelled; previous results remain"

PSUTIL_INSTALL_HINT = "psutil is not installed. Run: python -m pip install psutil"

BYTE_SCALE_UNITS: tuple[tuple[int, str], ...] = (
    (1024**4, "TiB"),
    (1024**3, "GiB"),
    (1024**2, "MiB"),
    (1024, "KiB"),
)


class ScanCancelled(Exception):
    """Raised when a cancellable Downloads scan is stopped by the caller."""


def check_cancelled(
    cancel_event: threading.Event | None,
    message: str = DOWNLOADS_SCAN_CANCELLED,
) -> None:
    """Raise `ScanCancelled` when the caller has cancelled the operation."""

    if cancel_event is not None and cancel_event.is_set():
        raise ScanCancelled(message)


def require_psutil(psutil_module: Any) -> Any:
    """Return the psutil module or raise a consistent installation hint."""

    if psutil_module is None:
        raise RuntimeError(PSUTIL_INSTALL_HINT)
    return psutil_module


def windows_windll() -> Any:
    """Return `ctypes.windll` on Windows and `None` elsewhere, never raising."""

    return getattr(ctypes, "windll", None)


def call_legacy_compatible(
    primary: Callable[[], Any],
    fallback: Callable[[], Any],
    *,
    error_substring: str = "unexpected keyword argument",
    before_fallback: Callable[[], None] | None = None,
    after_fallback: Callable[[], None] | None = None,
) -> Any:
    """Call `primary`, falling back to `fallback` for a legacy signature.

    Several extension points accept an optional ``cancel_event`` or
    ``progress_callback`` keyword; older one-argument hooks (kept as test
    monkeypatch seams) reject that keyword with a TypeError. When the raised
    TypeError message contains `error_substring`, `fallback` is used instead,
    with optional cancellation checks before and after it.
    """

    try:
        return primary()
    except TypeError as error:
        if error_substring not in str(error):
            raise
        if before_fallback is not None:
            before_fallback()
        result = fallback()
        if after_fallback is not None:
            after_fallback()
        return result


def call_cancellable(
    primary: Callable[[], Any],
    fallback: Callable[[], Any],
    cancel_event: threading.Event | None,
) -> Any:
    """Call `primary`, falling back to `fallback` for a legacy signature.

    The standard wrapper for optional-``cancel_event`` hooks that older
    one-argument extensions reject: the fallback only runs when the scan
    has not been cancelled.
    """

    return call_legacy_compatible(
        primary,
        fallback,
        before_fallback=lambda: check_cancelled(cancel_event),
    )


def detail_line_suffix(lines: Iterable[str], prefix: str) -> str | None:
    """Return the suffix of the first line that starts with `prefix`.

    Shared by the scanner (which builds detail lines such as
    ``"Temperature: 51°C"``) and the health parser (which reads the same
    lines back), so the prefix-matching semantics can never drift between
    producer and consumer.
    """

    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def file_sha256(
    path: Path,
    *,
    chunk_bytes: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """Hash one file chunk by chunk, checking cancellation between reads.

    Shared by `SystemScanner._file_hash` and `DownloadScanner._file_hash`; the
    caller supplies its own chunk size so each class keeps its configuration.
    """

    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(chunk_bytes):
            check_cancelled(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def file_content_marker(
    path: Path,
    *,
    chunk_bytes: int,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Hash one file's full content with BLAKE2b for cache validation."""

    check_cancelled(cancel_event)
    marker = hashlib.blake2b(digest_size=16)
    with path.open("rb") as file:
        while chunk := file.read(chunk_bytes):
            check_cancelled(cancel_event)
            marker.update(chunk)
    check_cancelled(cancel_event)
    return marker.digest()


def stat_fingerprint(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the identity fingerprint used to detect changed files."""

    return (
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ctime_ns", 0),
        getattr(stat, "st_dev", 0),
        getattr(stat, "st_ino", 0),
    )


def walk_directory_entries(
    root: Path,
    *,
    skip_dotfiles: bool,
    cancel_event: threading.Event | None = None,
) -> Iterator[os.DirEntry]:
    """Yield one file entry per non-directory file under ``root``.

    Shared by the downloads scan (``DownloadScanner._download_file_stats``)
    and the trash-size walk (``SystemScanner._directory_file_size``): a
    single iterative, OSError-tolerant scandir engine keeps the traversal
    contract identical for both consumers. Symlinked directories are never
    descended into and symlinked files are never yielded
    (``follow_symlinks=False``), matching both historical walkers.

    - ``skip_dotfiles`` also prunes hidden directories, so a downloads walk
      never descends into ``.hidden`` folders while a trash walk counts them.
    - One ``os.scandir`` handle is opened, fully consumed, and eagerly
      closed per directory; a mid-iteration ``OSError`` discards the rest of
      that directory but never fails the walk.
    - ``ScanCancelled`` (via ``check_cancelled``) propagates exactly as the
      downloads walker raised it.
    """

    pending_directories = [root]
    while pending_directories:
        check_cancelled(cancel_event)
        directory = pending_directories.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue

        try:
            for entry in entries:
                check_cancelled(cancel_event)
                if skip_dotfiles and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending_directories.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except (OSError, ValueError):
                    continue
                yield entry
        except OSError:
            pass
        finally:
            entries.close()
