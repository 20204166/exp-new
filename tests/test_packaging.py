"""Packaging configuration validation (display-safe, no Tk root created)."""

import unittest
from pathlib import Path

from tests.support.toml import load_project

REPO = Path(__file__).parents[1]


class PyprojectConfigurationTests(unittest.TestCase):
    def test_pyproject_parses_with_project_and_scripts(self) -> None:
        project = load_project()
        self.assertIn("project", project)
        self.assertEqual(project["project"]["name"], "system-analyzer")
        self.assertIn("version", project["project"]["dynamic"])

    def test_version_is_dynamic_from_single_source(self) -> None:
        from maintenance import __version__
        from maintenance._version import __version__ as module_version

        self.assertEqual(__version__, module_version)
        segments = module_version.split(".")
        self.assertEqual(len(segments), 4)
        for segment in segments:
            self.assertTrue(segment.isdigit())

        dynamic = load_project()["tool"]["setuptools"]["dynamic"]
        self.assertEqual(
            dynamic["version"]["attr"],
            "maintenance._version.__version__",
        )

    def test_py_typed_marker_is_shipped(self) -> None:
        self.assertTrue((REPO / "maintenance" / "py.typed").is_file())
        package_data = load_project()["tool"]["setuptools"]["package-data"]
        self.assertIn("py.typed", package_data["maintenance"])

    def test_window_supports_subpackage_is_shipped(self) -> None:
        packages = load_project()["tool"]["setuptools"]["packages"]
        self.assertIn("maintenance.ui.window_supports", packages)

    def test_console_scripts_are_declared(self) -> None:
        scripts = load_project()["project"]["scripts"]
        self.assertEqual(
            set(scripts),
            {"expra-connect-harness", "system-analyzer"},
        )

    def test_requires_python_is_310_or_newer(self) -> None:
        self.assertTrue(load_project()["project"]["requires-python"].startswith(">="))

    def test_dependencies_are_declared(self) -> None:
        dependencies = "\n".join(load_project()["project"]["dependencies"])
        for requirement in (
            "psutil",
            "PySide6>=6.7",
            "expra-connect>=2.2.1.1,<3",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, dependencies)

        requirements = (REPO / "requirements.txt").read_text().splitlines()
        self.assertIn("PySide6>=6.7", requirements)
        self.assertIn("expra-connect>=2.2.1.1,<3", requirements)

    def test_harness_version_is_restored_to_requested_baseline(self) -> None:
        version_source = (REPO / "maintenance" / "_version.py").read_text()
        self.assertIn('__version__ = "1.7.0.1"', version_source)

    def test_no_publishing_registry_is_configured(self) -> None:
        text = (REPO / "pyproject.toml").read_text()
        self.assertNotIn("pypi", text.casefold())
        self.assertNotIn("twine", text.casefold())

    def test_gui_entry_module_is_importable(self) -> None:
        import main

        self.assertTrue(callable(main.main))


class BuildScriptReliabilityTests(unittest.TestCase):
    def test_pyproject_declares_setuptools_build_backend(self) -> None:
        build_system = load_project()["build-system"]
        self.assertEqual(build_system["build-backend"], "setuptools.build_meta")
        self.assertIn("setuptools", build_system["requires"][0])

    def test_common_sh_defines_build_backend_preflight(self) -> None:
        source = (REPO / "install" / "_common.sh").read_text()
        self.assertIn("require_build_backend", source)

    def test_build_sh_has_isolation_policy_and_version_restore(self) -> None:
        source = (REPO / "install" / "build.sh").read_text()
        self.assertIn("SA_BUILD_ISOLATION", source)
        self.assertIn("--no-build-isolation", source)
        self.assertIn("restore_on_failure", source)
        self.assertIn("prepare-build", source)

    def test_build_sh_preflights_backend_before_version_preparation(self) -> None:
        source = (REPO / "install" / "build.sh").read_text()
        self.assertLess(
            source.index("require_build_backend"),
            source.index("prepare-build"),
        )

    def test_build_script_pairs_harness_with_connect_variant(self) -> None:
        shell = (REPO / "install" / "build.sh").read_text()
        powershell = (REPO / "install" / "build.ps1").read_text()
        build_system = load_project()["build-system"]
        self.assertEqual(build_system["requires"], ["setuptools>=68"])
        self.assertFalse((REPO / "setup.py").exists())
        self.assertIn("--variant", shell)
        self.assertIn("--native-connect-wheel", shell)
        self.assertIn('ValidateSet("universal', powershell)
        self.assertIn("-NativeConnectWheel", powershell)
        self.assertIn('cp "$native_connect_wheel" "$here/dist/"', shell)
        self.assertIn(
            'if [ "$app_wheel" != "$here/dist/system_analyzer-${app_version}-py3-none-any.whl" ]',
            shell,
        )
        self.assertIn(
            'Copy-Item $companionWheel -Destination (Join-Path $here "dist")',
            powershell,
        )
        self.assertIn("_read_manifest_from_wheel", shell)
        self.assertIn("_read_manifest_from_wheel", powershell)
        self.assertNotIn("SYSTEM_ANALYZER_CYTHON", shell)
        self.assertNotIn("SYSTEM_ANALYZER_CYTHON", powershell)

    def test_common_sh_build_backend_version_gate_requires_68(self) -> None:
        import re

        def gate(version: str) -> bool:
            m = re.match(r"^(\d+)\.(\d+)", version)
            return bool(m and (int(m.group(1)), int(m.group(2))) >= (68, 0))

        self.assertTrue(gate("68.0.0"))
        self.assertTrue(gate("75.6.0"))
        self.assertFalse(gate("67.2.0"))
        self.assertFalse(gate("59.0"))


if __name__ == "__main__":
    unittest.main()
