"""Qt application startup helpers, independent of the application window."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtWidgets import QApplication, QMainWindow


def create_application(arguments: Sequence[str] | None = None) -> QApplication:
    """Return the process QApplication, creating it once when needed."""

    existing = QApplication.instance()
    if existing is not None and not isinstance(existing, QApplication):
        raise RuntimeError("A QCoreApplication already exists without Qt Widgets")
    application = existing
    if application is None:
        application = QApplication(list(arguments or ()))
    application.setApplicationName("Expra Connect")
    application.setOrganizationName("Expra Connect")
    return application


def run_application(
    arguments: Sequence[str],
    window_factory: Callable[[], QMainWindow],
) -> int:
    """Create and show the main window, then enter Qt's sole event loop."""

    application = create_application(arguments)
    window = window_factory()
    window.show()
    return application.exec()
