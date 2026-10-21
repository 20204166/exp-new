"""Smoke checks for the pinned expra-connect public runtime API."""

import importlib.metadata
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

import expra_connect
from expra_connect.runtime import (
    ConnectConfig,
    ConnectRuntime,
    RuntimeState,
    RuntimeStatus,
)

from maintenance import _release


class ConnectPackageApiSmokeTests(unittest.TestCase):
    def test_connect_adapter_import_does_not_load_legacy_remote_backend(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import maintenance.connect_runtime; "
                    "assert 'maintenance.remote' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pinned_package_exposes_runtime_composition_api(self) -> None:
        self.assertEqual(expra_connect.__version__, _release._COMPANION_VERSION)
        self.assertEqual(
            importlib.metadata.version("expra-connect"), _release._COMPANION_VERSION
        )
        self.assertTrue(callable(ConnectConfig))
        self.assertTrue(callable(ConnectRuntime))
        self.assertTrue(issubclass(RuntimeState, str))
        self.assertTrue(callable(RuntimeStatus))

    def test_connect_import_resolves_from_installed_distribution_not_checkout(
        self,
    ) -> None:
        distribution = importlib.metadata.distribution("expra-connect")
        imported_module = Path(expra_connect.__file__).resolve()
        packaged_module = Path(
            str(distribution.locate_file("expra_connect/__init__.py"))
        ).resolve()
        purelib = Path(sysconfig.get_paths()["purelib"]).resolve()

        self.assertEqual(imported_module, packaged_module)
        self.assertIn(purelib, imported_module.parents)

    def test_runtime_construction_does_not_start_network_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = ConnectConfig(
                profile_dir=Path(directory),
                discovery_enabled=False,
                cluster_enabled=False,
            )
            runtime = ConnectRuntime(config)

            self.assertFalse(runtime.started)
            self.assertIs(runtime.status.state, RuntimeState.STOPPED)


if __name__ == "__main__":
    unittest.main()
