"""Application construction and process entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from plcsniffer.config import DEFAULT_ICON_PATH, PROJECT_ROOT
from plcsniffer.ui.main_window import MainWindow


def resource_path(relative_path: str | Path) -> Path:
    """Resolve a resource in source and PyInstaller environments.

    Args:
        relative_path: Resource path relative to the project/package root.

    Returns:
        Absolute path to the resource.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    base_directory = Path(frozen_root) if frozen_root else PROJECT_ROOT
    return (base_directory / relative_path).resolve()


def create_main_window(
    arguments: Sequence[str] | None = None,
) -> tuple[QApplication, MainWindow]:
    """Create the Qt application and configured main window.

    Args:
        arguments: Optional process-style command-line arguments.

    Returns:
        The QApplication and main-window pair.
    """
    existing_application = QApplication.instance()
    if isinstance(existing_application, QApplication):
        application = existing_application
    else:
        application = QApplication(
            list(arguments) if arguments is not None else sys.argv
        )
    icon_path = resource_path(DEFAULT_ICON_PATH.name)
    if not icon_path.exists():
        icon_path = resource_path(Path("assets") / DEFAULT_ICON_PATH.name)
    icon = QIcon(str(icon_path))
    application.setWindowIcon(icon)

    window = MainWindow()
    window.setWindowIcon(icon)
    return application, window


def run(arguments: Sequence[str] | None = None) -> int:
    """Launch the desktop application and return its process exit code."""
    application, window = create_main_window(arguments)
    window.showMaximized()
    return application.exec()
