"""Tests for application-level Qt startup and lifecycle ownership."""

from __future__ import annotations

import unittest
from typing import ClassVar
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMainWindow

from maintenance.ui.qt_application import create_application, run_application
from tests.support.qt import get_qapplication


class QtApplicationTests(unittest.TestCase):
    app: ClassVar[QApplication]

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = get_qapplication()

    def test_create_application_reuses_instance_and_sets_metadata(self) -> None:
        app = create_application(["system-analyzer"])

        self.assertIs(app, QApplication.instance())
        self.assertEqual(app.applicationName(), "Expra Connect")
        self.assertEqual(app.organizationName(), "Expra Connect")

    def test_run_application_shows_window_and_returns_qt_exit_code(self) -> None:
        window = QMainWindow()
        with (
            patch(
                "maintenance.ui.qt_application.create_application",
                return_value=self.app,
            ),
            patch.object(self.app, "exec", return_value=17) as execute,
        ):
            result = run_application(["system-analyzer"], lambda: window)

        self.assertEqual(result, 17)
        self.assertTrue(window.isVisible())
        execute.assert_called_once_with()
        window.close()

    def test_window_factory_failure_does_not_enter_event_loop(self) -> None:
        with (
            patch(
                "maintenance.ui.qt_application.create_application",
                return_value=self.app,
            ),
            patch.object(self.app, "exec") as execute,
            self.assertRaisesRegex(RuntimeError, "construction failed"),
        ):
            run_application(
                ["system-analyzer"],
                lambda: (_ for _ in ()).throw(RuntimeError("construction failed")),
            )

        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
