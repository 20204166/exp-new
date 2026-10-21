"""Scanner-specific responsibility support.

The mixin preserves SystemScanner method names while keeping dependency
lookups on maintenance.scanner for existing monkeypatch seams.
"""

# The mixin is intentionally completed by SystemScanner; the concrete state
# and class constants live on that owner rather than being duplicated here.
# pyright: reportAttributeAccessIssue=false, reportUndefinedVariable=false
# mypy: disable-error-code="attr-defined,misc,has-type,assignment,valid-type,name-defined"

from __future__ import annotations

from pathlib import Path

from maintenance.components import DownloadsPathResolver, usernames_match


class PathsMixin:
    """Own one cohesive scanner implementation responsibility."""

    @classmethod
    def _default_downloads_path(cls) -> Path:
        return cls._make_downloads_path_resolver().select()

    @staticmethod
    def _is_directory(path: Path) -> bool:
        return DownloadsPathResolver._is_directory(path)

    @staticmethod
    def _safe_downloads_fallback(home: Path) -> Path:
        return DownloadsPathResolver._safe_downloads_fallback(home)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return DownloadsPathResolver._path_exists(path)

    @staticmethod
    def _windows_downloads_path() -> Path | None:
        return DownloadsPathResolver._windows_downloads_path()

    @staticmethod
    def _same_user(username: str, current_user: str) -> bool:
        return usernames_match(username, current_user)
