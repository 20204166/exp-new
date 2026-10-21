"""Qt confirmation for adopting a new Connect identity."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from maintenance.connect_runtime import IDENTITY_TRANSITION_NOTICE


def confirm_connect_identity_transition(parent: QWidget | None) -> bool:
    """Ask before replacing legacy network authority with a new identity."""

    dialog = QMessageBox(parent)
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setWindowTitle("Set up Expra Connect")
    dialog.setText("Expra Connect will use a new identity for this installation.")
    dialog.setInformativeText(IDENTITY_TRANSITION_NOTICE)
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    dialog.button(QMessageBox.StandardButton.Yes).setText("Set up new identity")
    dialog.button(QMessageBox.StandardButton.No).setText("Not now")
    dialog.setDefaultButton(QMessageBox.StandardButton.No)
    dialog.setEscapeButton(QMessageBox.StandardButton.No)
    dialog.setAccessibleName("Expra Connect identity transition")
    return dialog.exec() == QMessageBox.StandardButton.Yes
