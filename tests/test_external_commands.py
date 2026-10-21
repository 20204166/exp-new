import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from maintenance.external_commands import (
    COMMAND_TIMEOUT_SECONDS,
    run_json_command,
    run_text_command,
)


class RunTextCommandTests(unittest.TestCase):
    def test_decode_error_is_reported_as_command_failure(self) -> None:
        def failing_runner(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

        stdout, error = run_text_command(["cmd"], runner=failing_runner)

        self.assertEqual(stdout, "")
        self.assertIn("invalid byte", error or "")
        # Command name must appear so the error is self-contained when propagated.
        self.assertIn("cmd", error or "")

    def test_successful_command_returns_stdout(self) -> None:
        stdout, error = run_text_command(
            [sys.executable, "-c", "print('probe-ok')"],
        )

        self.assertIsNone(error)
        self.assertIn("probe-ok", stdout)

    def test_missing_command_reports_error(self) -> None:
        stdout, error = run_text_command(["definitely-missing-command-xyz"])

        self.assertEqual(stdout, "")
        self.assertIn("No such file or directory", error or "")

    def test_permission_error_reports_error(self) -> None:
        with patch(
            "maintenance.external_commands.subprocess.run",
            side_effect=PermissionError("access denied"),
        ):
            stdout, error = run_text_command(["restricted-command"])

        self.assertEqual(stdout, "")
        self.assertIn("access denied", error or "")

    def test_timeout_reports_error(self) -> None:
        stdout, error = run_text_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout_seconds=0.1,
        )

        self.assertEqual(stdout, "")
        self.assertIn("timed out", error or "")

    def test_non_zero_exit_reports_error(self) -> None:
        stdout, error = run_text_command(
            [sys.executable, "-c", "raise SystemExit(3)"],
        )

        self.assertEqual(stdout, "")
        self.assertIn("exit status 3", error or "")

    def test_default_runner_resolves_late_for_patch_seams(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="out")
        with patch(
            "maintenance.external_commands.subprocess.run",
            return_value=fake,
        ) as runner:
            stdout, error = run_text_command(["cmd"])

        runner.assert_called_once_with(
            ["cmd"],
            check=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=0,
        )
        self.assertIsNone(error)
        self.assertEqual(stdout, "out")

    def test_custom_runner_is_used_and_error_string_is_returned(self) -> None:
        def failing_runner(*args: Any, **kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=["cmd"], timeout=7)

        stdout, error = run_text_command(
            ["cmd"],
            timeout_seconds=7,
            runner=failing_runner,
        )

        self.assertEqual(stdout, "")
        self.assertIn("timed out", error or "")

    def test_custom_runner_success(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="parsed later")
        stdout, error = run_text_command(["cmd"], runner=lambda *a, **k: fake)

        self.assertIsNone(error)
        self.assertEqual(stdout, "parsed later")

    def test_run_text_command_forwards_creationflags(self) -> None:
        seen: dict[str, Any] = {}

        def fake(command, **kwargs) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(stdout="out", returncode=0)

        stdout, error = run_text_command(["cmd"], runner=fake, creationflags=0x08000000)
        self.assertEqual(stdout, "out")
        self.assertIsNone(error)
        self.assertEqual(seen.get("creationflags"), 0x08000000)


class RunJsonCommandTests(unittest.TestCase):
    def test_valid_json_payload_is_returned(self) -> None:
        fake = subprocess.CompletedProcess(
            ["cmd"], returncode=0, stdout=json.dumps({"Name": "AMD"})
        )

        payload, error = run_json_command(["cmd"], runner=lambda *a, **k: fake)

        self.assertIsNone(error)
        self.assertEqual(payload, {"Name": "AMD"})

    def test_command_error_short_circuits_before_decoding(self) -> None:
        def failing_runner(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory")

        payload, error = run_json_command(
            ["cmd"], runner=failing_runner, empty_stdout_fallback="[]"
        )

        self.assertIsNone(payload)
        self.assertIn("No such file or directory", error or "")

    def test_malformed_json_reports_decode_error(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="{not-json")

        payload, error = run_json_command(["cmd"], runner=lambda *a, **k: fake)

        self.assertIsNone(payload)
        self.assertIn("Expecting", error or "")
        # Command name must appear so the error traces back to its source.
        self.assertIn("cmd", error or "")

    def test_empty_stdout_parses_fallback_when_supplied(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="")

        payload, error = run_json_command(
            ["cmd"], runner=lambda *a, **k: fake, empty_stdout_fallback="[]"
        )

        self.assertIsNone(error)
        self.assertEqual(payload, [])

    def test_empty_stdout_fails_normally_without_fallback(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="")

        payload, error = run_json_command(["cmd"], runner=lambda *a, **k: fake)

        self.assertIsNone(payload)
        self.assertIn("Expecting", error or "")

    def test_runner_seam_still_reaches_decoding(self) -> None:
        fake = subprocess.CompletedProcess(["cmd"], returncode=0, stdout="[1, 2]")
        with patch(
            "maintenance.external_commands.subprocess.run", return_value=fake
        ) as runner:
            payload, error = run_json_command(["cmd"])

        runner.assert_called_once()
        self.assertIsNone(error)
        self.assertEqual(payload, [1, 2])

    def test_run_json_command_forwards_creationflags(self) -> None:
        seen: dict[str, Any] = {}

        def fake(command, **kwargs) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(stdout='{"ok": true}', returncode=0)

        payload, error = run_json_command(
            ["cmd"], runner=fake, creationflags=0x08000000
        )
        self.assertEqual(payload, {"ok": True})
        self.assertIsNone(error)
        self.assertEqual(seen.get("creationflags"), 0x08000000)


if __name__ == "__main__":
    unittest.main()
