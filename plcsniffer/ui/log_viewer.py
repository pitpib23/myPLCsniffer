from __future__ import annotations

import codecs
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from plcsniffer.config import (
    LOG_REFRESH_INTERVAL_MS,
    LOG_VIEW_INITIAL_TAIL_BYTES,
)
from plcsniffer.logging_config import LOG_FILE_PATH


class LogViewerWidget(QWidget):
    """Read-only, automatically refreshed view of the active application log."""

    INITIAL_TAIL_BYTES = LOG_VIEW_INITIAL_TAIL_BYTES

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        log_path: str | Path = LOG_FILE_PATH,
        timer_interval_ms: int = LOG_REFRESH_INTERVAL_MS,
        auto_start: bool = True,
    ) -> None:
        super().__init__(parent)
        self.log_path = Path(log_path).expanduser().resolve()
        self._file_identity: tuple[int, int] | None = None
        self._position = 0
        self._last_mtime_ns: int | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(max(1, int(timer_interval_ms)))
        self.timer.timeout.connect(self.refresh_log)

        self.refresh_log()
        if auto_start:
            self.timer.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        details = QHBoxLayout()
        details.addWidget(QLabel("Current log file:"))
        self.path_label = QLabel(str(self.log_path))
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setToolTip(str(self.log_path))
        details.addWidget(self.path_label, stretch=1)
        self.status_label = QLabel("Checking log file...")
        details.addWidget(self.status_label)
        layout.addLayout(details)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(10_000)
        self.log_view.setPlaceholderText("Log entries will appear here.")
        self.log_view.setStyleSheet(
            'font-family: "Cascadia Mono", "Consolas", monospace; font-size: 10.5pt;'
        )
        layout.addWidget(self.log_view, stretch=1)

    @staticmethod
    def _identity(stat_result: os.stat_result) -> tuple[int, int]:
        return int(stat_result.st_dev), int(stat_result.st_ino)

    def _reset_reader(self, *, clear_view: bool) -> None:
        self._file_identity = None
        self._position = 0
        self._last_mtime_ns = None
        self._decoder.reset()
        if clear_view:
            self.log_view.clear()

    def _append_text(self, text: str) -> None:
        if not text:
            return
        scroll_bar = self.log_view.verticalScrollBar()
        was_at_end = scroll_bar.value() >= scroll_bar.maximum() - 1
        cursor = QTextCursor(self.log_view.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        if was_at_end:
            scroll_bar.setValue(scroll_bar.maximum())

    def refresh_log(self) -> None:
        """Read new bytes and detect creation, replacement, or truncation."""
        try:
            with self.log_path.open("rb") as handle:
                stat_result = os.fstat(handle.fileno())
                identity = self._identity(stat_result)
                replaced = (
                    self._file_identity is not None and identity != self._file_identity
                )
                truncated = stat_result.st_size < self._position
                same_size_rewrite = (
                    self._last_mtime_ns is not None
                    and stat_result.st_size == self._position
                    and stat_result.st_mtime_ns != self._last_mtime_ns
                )
                if replaced or truncated or same_size_rewrite:
                    self._reset_reader(clear_view=True)

                self._file_identity = identity
                if (
                    self._position == 0
                    and stat_result.st_size > self.INITIAL_TAIL_BYTES
                ):
                    handle.seek(stat_result.st_size - self.INITIAL_TAIL_BYTES)
                    handle.readline()
                    self._position = handle.tell()
                else:
                    handle.seek(self._position)

                payload = handle.read()
                self._position = handle.tell()
                latest_stat = os.fstat(handle.fileno())
                self._last_mtime_ns = latest_stat.st_mtime_ns
        except FileNotFoundError:
            self._reset_reader(clear_view=True)
            self.status_label.setText("Waiting for the log file to be created.")
            return
        except OSError as error:
            self.status_label.setText(f"Unable to read log file: {error}")
            return

        self._append_text(self._decoder.decode(payload, final=False))
        self.status_label.setText(f"Live — {self._position:,} bytes")

    def start_refresh(self) -> None:
        """Start periodic log-file refreshes."""
        self.timer.start()

    def stop_refresh(self) -> None:
        """Stop periodic log-file refreshes."""
        self.timer.stop()
