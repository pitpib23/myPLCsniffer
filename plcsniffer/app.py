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
    *,
    lite: bool = False,
) -> tuple[QApplication, MainWindow]:
    """Create the Qt application and configured main window.

    Args:
        arguments: Optional process-style command-line arguments, passed
            straight through to QApplication. run() below is what strips
            its own --lite flag out of these before they reach here — call
            this function directly (e.g. from a test or REPL) with
            whatever Qt-specific arguments you need, plus lite explicitly.
        lite: True builds the Raspberry Pi 7" touchscreen edition
            (MainWindow(lite=True)) instead of the full desktop edition.

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

    window = MainWindow(lite=lite)
    window.setWindowIcon(icon)
    return application, window


def run(arguments: Sequence[str] | None = None) -> int:
    """Launch the desktop application and return its process exit code.

    Recognizes one flag of its own, ``--lite`` (e.g. ``python main.py
    --lite``), which selects the Raspberry Pi 7" touchscreen edition
    (MainWindow(lite=True)) instead of the full desktop edition — the
    default with no flag. ``--lite`` is consumed here and never forwarded
    to QApplication, which would not recognize it.
    """
    raw_arguments = list(arguments) if arguments is not None else sys.argv
    lite = "--lite" in raw_arguments
    qt_arguments = [value for value in raw_arguments if value != "--lite"]
    application, window = create_main_window(qt_arguments, lite=lite)
    window.showMaximized()
    return application.exec()
