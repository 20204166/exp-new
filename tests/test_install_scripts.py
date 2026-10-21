import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from maintenance import _release

ROOT = Path(__file__).resolve().parents[1]


class PowerShellInstallerTests(unittest.TestCase):
    def test_build_scripts_preflight_and_publish_companion_wheel(self) -> None:
        bash = (ROOT / "install" / "build.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("verify-companion-wheel", bash)
        self.assertIn("expra_connect-", bash)
        self.assertIn("py3-none-any.whl", bash)
        self.assertIn("verify-companion-wheel", powershell)
        self.assertIn("expra_connect-", powershell)
        self.assertIn("py3-none-any.whl", powershell)

    def test_user_installer_uses_powershell_quote_escaping(self) -> None:
        script = (ROOT / "install" / "install-user.ps1").read_text(encoding="utf-8")

        self.assertNotIn('setx PATH \\"', script)
        self.assertIn('`"$binDir;%PATH%`"', script)

    def test_online_installer_verifies_imported_release_after_install(self) -> None:
        script = (ROOT / "install" / "install-online.ps1").read_text(encoding="utf-8")

        self.assertIn("installed version", script)
        self.assertIn("maintenance.__file__", script)
        self.assertIn("main.__file__", script)
        self.assertNotIn("import window", script)
        self.assertIn("$driveRoot", script)
        self.assertIn("Active virtual environment", script)

    def test_installed_verification_imports_shipped_entry_module(self) -> None:
        for filename in (
            "_common.sh",
            "_common.ps1",
            "install-online.sh",
            "install-online.ps1",
        ):
            script = (ROOT / "install" / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("import main", script)
                self.assertIn("main.__file__", script)
                self.assertNotIn("import window", script)

    def test_online_installers_download_and_verify_both_wheels(self) -> None:
        shell = (ROOT / "install" / "install-online.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install" / "install-online.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("expra_connect-", shell)
        self.assertIn("expra_connect-", powershell)
        self.assertIn("SHA256SUMS", shell)
        self.assertIn("SHA256SUMS", powershell)

    def test_online_installers_target_exp_new_and_resolve_compatible_connect_wheels(
        self,
    ) -> None:
        shell = (ROOT / "install" / "install-online.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install" / "install-online.ps1").read_text(
            encoding="utf-8"
        )
        for script in (shell, powershell):
            self.assertIn("20204166/exp-new/main/dist", script)
            self.assertIn("expra_connect-", script)
            self.assertIn("--find-links", script)
            self.assertIn("expra-connect", script)
        self.assertNotIn("20204166/exp/main/dist", shell)
        self.assertNotIn("20204166/exp/main/dist", powershell)

    def test_local_installers_validate_before_mutation_and_install_both_wheels(
        self,
    ) -> None:
        common = (ROOT / "install" / "_common.ps1").read_text(encoding="utf-8")
        self.assertIn("function Verify-ReleaseArtifacts", common)
        self.assertIn("function Install-ReleaseWheels", common)
        self.assertIn("Get-CompanionWheelPath", common)
        for filename in ("install.ps1", "upgrade.ps1"):
            with self.subTest(filename=filename):
                script = (ROOT / "install" / filename).read_text(encoding="utf-8")
                self.assertLess(
                    script.index("Verify-ReleaseArtifacts"),
                    script.index("Remove-InstalledPackage"),
                )
                self.assertIn("Install-ReleaseWheels", script)
        rollback = (ROOT / "install" / "rollback.ps1").read_text(encoding="utf-8")
        self.assertLess(
            rollback.index("Verify-ReleaseArtifacts"),
            rollback.index("Install-ReleaseWheels"),
        )

    def test_local_installers_resolve_connect_from_offline_wheelhouse(self) -> None:
        shell = (ROOT / "install" / "_common.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install" / "_common.ps1").read_text(encoding="utf-8")
        user_shell = (ROOT / "install" / "install-user.sh").read_text(encoding="utf-8")
        user_powershell = (ROOT / "install" / "install-user.ps1").read_text(
            encoding="utf-8"
        )
        for script in (shell, powershell, user_shell, user_powershell):
            self.assertIn("--find-links", script)
            self.assertIn("expra-connect==2.2.1.1", script)
            self.assertNotIn("--no-index", script)

    def test_user_installers_validate_and_pass_both_wheels(self) -> None:
        bash = (ROOT / "install" / "install-user.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install" / "install-user.ps1").read_text(encoding="utf-8")
        self.assertIn("validate_release_artifacts", bash)
        self.assertIn('"expra-connect==2.2.1.1"', bash)
        self.assertIn("Verify-ReleaseArtifacts", powershell)
        self.assertIn("Install-UserWheels", powershell)

    def test_unix_installers_validate_artifacts_before_removing_current_app(
        self,
    ) -> None:
        for filename in ("install.sh", "upgrade.sh", "rollback.sh"):
            with self.subTest(filename=filename):
                script = (ROOT / "install" / filename).read_text(encoding="utf-8")
                self.assertLess(
                    script.index("validate_release_artifacts"),
                    script.index("clean_installed_package"),
                )


class UnixInstallerArtifactTests(unittest.TestCase):
    def _make_package(self, root: Path) -> tuple[Path, Path, Path]:
        install_dir = root / "install"
        dist_dir = root / "dist"
        install_dir.mkdir(parents=True)
        dist_dir.mkdir()
        common = ROOT / "install" / "_common.sh"
        (install_dir / "_common.sh").write_bytes(common.read_bytes())

        app_wheel = dist_dir / "system_analyzer-1.0.0.0-py3-none-any.whl"
        companion_wheel = dist_dir / _release._COMPANION_WHEEL_NAME
        with zipfile.ZipFile(app_wheel, "w") as archive:
            for name in (
                "maintenance/__init__.py",
                "maintenance/_version.py",
                "main.py",
                "window.py",
                "algo.py",
                "system_analyzer-1.0.0.0.dist-info/entry_points.txt",
                "system_analyzer-1.0.0.0.dist-info/WHEEL",
            ):
                contents = (
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
                    if name.endswith(".dist-info/WHEEL")
                    else "[console_scripts]\nsystem-analyzer = main:main\n"
                )
                archive.writestr(name, contents)
        source_companion = ROOT / "dist" / _release._COMPANION_WHEEL_NAME
        shutil.copyfile(source_companion, companion_wheel)
        manifest = dist_dir / "SHA256SUMS"
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                for path in (app_wheel, companion_wheel)
            ),
            encoding="utf-8",
        )
        return app_wheel, companion_wheel, manifest

    def _run_shell_helper(
        self,
        root: Path,
        app_wheel: Path,
        log: Path,
    ) -> subprocess.CompletedProcess[str]:
        fake_python = root / "fake-python"
        fake_python.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *verify-release-artifacts*) printf \'%s\\n\' "$*" >> "$CALL_LOG"; exec "$REAL_PY" "$@" ;;\n'
            "  *'-m pip install'*) printf '%s\\n' \"$*\" >> \"$CALL_LOG\" ;;\n"
            '  *) printf \'%s\\n\' "$*" >> "$CALL_LOG" ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        command = (
            'source "$COMMON"; '
            'validate_release_artifacts "$PY" "$APP" && '
            'install_release_wheels "$PY" "$APP"'
        )
        environment = os.environ | {
            "APP": str(app_wheel),
            "CALL_LOG": str(log),
            "COMMON": str(root / "install" / "_common.sh"),
            "PY": str(fake_python),
            "PYTHONPATH": str(ROOT),
            "REAL_PY": sys.executable,
        }
        return subprocess.run(
            ["bash", "-c", command],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_local_installer_verifies_artifacts_then_resolves_from_wheelhouse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, _, _ = self._make_package(root)
            log = root / "calls.log"

            result = self._run_shell_helper(root, app, log)

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            installs = [line for line in calls if line.startswith("-m pip install")]
            self.assertEqual(len(installs), 1, calls)
            self.assertIn("--find-links", installs[0])
            self.assertNotIn("--no-index", installs[0])
            self.assertIn(str(app), installs[0])
            self.assertIn("expra-connect==2.2.1.1", installs[0])
            self.assertLess(
                calls.index(
                    next(line for line in calls if "verify-release-artifacts" in line)
                ),
                calls.index(installs[0]),
            )

    def test_corrupt_companion_is_rejected_before_any_pip_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, companion, _ = self._make_package(root)
            companion.write_bytes(b"corrupted companion")
            log = root / "calls.log"

            result = self._run_shell_helper(root, app, log)

            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            self.assertFalse(any(line.startswith("-m pip install") for line in calls))

    def test_missing_companion_is_rejected_before_any_pip_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, companion, _ = self._make_package(root)
            companion.unlink()
            log = root / "calls.log"

            result = self._run_shell_helper(root, app, log)

            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            self.assertFalse(any(line.startswith("-m pip install") for line in calls))


if __name__ == "__main__":
    unittest.main()
