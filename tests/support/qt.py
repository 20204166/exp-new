"""Qt application support for unittest-based widget tests."""

import os

from PySide6.QtWidgets import QApplication

_application: QApplication | None = None


def get_qapplication() -> QApplication:
    """Return the process-wide Qt application, creating it offscreen if needed."""
    global _application

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    application = QApplication.instance()
    if application is not None and not isinstance(application, QApplication):
        raise RuntimeError("A non-GUI Qt application already exists")
    if application is None:
        application = QApplication([])
    _application = application
    return application
