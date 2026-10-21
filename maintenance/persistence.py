"""Shared persistence helpers for atomic text stores.

These helpers keep the filesystem transaction scaffold in one place while
leaving schema-specific parsing, validation, and serialization in the owning
modules.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def read_text_or_none(
    path: Path,
    *,
    logger: logging.Logger,
    warning_template: str,
) -> str | None:
    """Read UTF-8 text or return ``None`` when the file is missing or unreadable."""

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as error:
        logger.warning(warning_template, error)
        return None
    except OSError as error:
        logger.warning(warning_template, error)
        return None


def atomic_write_text(
    path: Path,
    payload: str,
    *,
    temp_prefix: str,
    create_directory_message: str,
    save_message: str,
    save_error_factory: Callable[[str], Exception],
    fsync_warning_template: str,
    logger: logging.Logger,
) -> None:
    """Atomically replace ``path`` with ``payload`` written as UTF-8 text."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise save_error_factory(f"{create_directory_message}: {error}") from error

    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=temp_prefix,
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
    except OSError as error:
        raise save_error_factory(f"{save_message}: {error}") from error

    fsync_directory(path, logger=logger, warning_template=fsync_warning_template)


def fsync_directory(
    path: Path,
    *,
    logger: logging.Logger,
    warning_template: str,
) -> None:
    """Best-effort fsync of ``path``'s parent directory after a commit."""

    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        logger.warning(warning_template, path.parent)
    finally:
        with contextlib.suppress(OSError):
            os.close(dir_fd)
