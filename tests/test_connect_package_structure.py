"""Import and packaging boundaries for the focused Connect harness."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from tests.support.toml import load_project

REPO = Path(__file__).parents[1]


class ConnectPackageStructureTests(unittest.TestCase):
    def test_main_import_does_not_load_tk_or_legacy_remote_authority(self) -> None:
        program = (
            "import sys; import main; "
            "assert 'tkinter' not in sys.modules; "
            "assert 'maintenance.remote' not in sys.modules; "
            "assert 'maintenance.remote_support.server' not in sys.modules; "
            "assert 'maintenance.components.coordinator' not in sys.modules; "
            "assert 'maintenance.components.placement' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=REPO,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_wheel_manifest_excludes_legacy_entrypoints_and_remote_package(
        self,
    ) -> None:
        setuptools = load_project()["tool"]["setuptools"]
        self.assertEqual(set(setuptools["py-modules"]), {"main", "algo"})
        self.assertNotIn("maintenance.remote_support", setuptools["packages"])

    def test_legacy_authority_and_window_sources_are_absent(self) -> None:
        for path in (
            "window.py",
            "legacy_tk_window.py",
            "maintenance/remote.py",
            "maintenance/remote_security.py",
            "maintenance/remote_support/server.py",
            "maintenance/components/network_discovery.py",
        ):
            with self.subTest(path=path):
                self.assertFalse((REPO / path).exists())

    def test_component_facade_exports_scanner_and_cluster_contracts_only(self) -> None:
        from maintenance import components

        self.assertTrue(hasattr(components, "ScanCancelled"))
        self.assertTrue(hasattr(components, "CoordinatorTimeline"))
        self.assertFalse(hasattr(components, "NetworkDiscovery"))
        self.assertFalse(hasattr(components, "PeerConnectionManager"))


if __name__ == "__main__":
    unittest.main()
