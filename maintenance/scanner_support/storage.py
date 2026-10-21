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

from maintenance.components.scan_support import walk_directory_entries, windows_windll

from ._compat import scanner_module


class StorageMixin:
    """Own one cohesive scanner implementation responsibility."""

    def trash_size(self) -> int:
        if scanner_module.platform.system() == "Windows":
            return self._windows_trash_size()

        total = 0
        for trash in self._trash_paths():
            if not trash.exists():
                continue
            total += self._directory_file_size(trash)
        return total

    @staticmethod
    def _directory_file_size(root: Path) -> int:
        total = 0
        for entry in walk_directory_entries(root, skip_dotfiles=False):
            try:
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _windows_trash_size() -> int:
        windll = windows_windll()
        try:
            shell32 = windll.shell32 if windll is not None else None
            query_recycle_bin = (
                shell32.SHQueryRecycleBinW if shell32 is not None else None
            )
        except (AttributeError, OSError, scanner_module.ctypes.ArgumentError):
            return 0
        if query_recycle_bin is None:
            return 0

        info = scanner_module._RecycleBinInfo()
        info.cb_size = scanner_module.ctypes.sizeof(info)
        try:
            query_recycle_bin.argtypes = [
                scanner_module.ctypes.c_wchar_p,
                scanner_module.ctypes.POINTER(scanner_module._RecycleBinInfo),
            ]
            query_recycle_bin.restype = scanner_module.ctypes.c_long
            result = query_recycle_bin(None, scanner_module.ctypes.byref(info))
        except (
            OSError,
            TypeError,
            AttributeError,
            scanner_module.ctypes.ArgumentError,
        ):
            return 0

        if result != 0 or info.size < 0:
            return 0
        return int(info.size)

    @staticmethod
    def _trash_paths() -> tuple[Path, ...]:
        system = scanner_module.platform.system()
        if system == "Darwin":
            return (scanner_module.Path.home() / ".Trash",)
        if system == "Linux":
            return (
                scanner_module.Path.home() / ".local" / "share" / "Trash" / "files",
            )
        return ()
