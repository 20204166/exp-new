"""Lazy access to historical scanner dependency seams."""

from typing import Any


class _ScannerModuleProxy:
    """Resolve patched scanner-module dependencies only when a scan uses them."""

    def __getattr__(self, name: str) -> Any:
        from importlib import import_module

        return getattr(import_module("maintenance.scanner"), name)


scanner_module = _ScannerModuleProxy()
