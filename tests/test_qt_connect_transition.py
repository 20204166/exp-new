"""Qt copy and decision tests for the one-time identity transition prompt."""

from __future__ import annotations

import unittest
from typing import ClassVar
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from maintenance.connect_runtime import IDENTITY_TRANSITION_NOTICE
from maintenance.ui.qt_connect_transition import confirm_connect_identity_transition
from tests.support.qt import get_qapplication


class QtConnectTransitionTests(unittest.TestCase):
    app: ClassVar[QApplication]

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = get_qapplication()

    def test_prompt_explains_repair_and_recovery_then_defaults_to_cancel(self) -> None:
        observed: dict[str, object] = {}

        def inspect_dialog(dialog: QMessageBox) -> QMessageBox.StandardButton:
            observed["text"] = dialog.text()
            observed["informative"] = dialog.informativeText()
            observed["default"] = dialog.defaultButton().text()
            observed["continue"] = dialog.button(QMessageBox.StandardButton.Yes).text()
            observed["cancel"] = dialog.button(QMessageBox.StandardButton.No).text()
            return QMessageBox.StandardButton.No

        with patch.object(QMessageBox, "exec", inspect_dialog):
            result = confirm_connect_identity_transition(None)

        self.assertFalse(result)
        self.assertIn("new identity", str(observed["text"]))
        self.assertIn("must explicitly pair again", str(observed["informative"]))
        self.assertIn("kept unchanged for recovery", str(observed["informative"]))
        self.assertEqual(observed["default"], "Not now")
        self.assertEqual(observed["continue"], "Set up new identity")
        self.assertEqual(observed["cancel"], "Not now")

    def test_explicit_continue_is_required_to_accept_transition(self) -> None:
        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
        ):
            self.assertTrue(confirm_connect_identity_transition(None))

        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.No
        ):
            self.assertFalse(confirm_connect_identity_transition(None))

    def test_notice_is_specific_and_does_not_claim_trust_was_migrated(self) -> None:
        self.assertIn("must explicitly pair again", IDENTITY_TRANSITION_NOTICE)
        self.assertIn("kept unchanged for recovery", IDENTITY_TRANSITION_NOTICE)
        self.assertIn("membership must be re-established", IDENTITY_TRANSITION_NOTICE)


if __name__ == "__main__":
    unittest.main()
