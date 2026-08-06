from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QWidget,
)

from plcsniffer.config import (
    APPLICATION_NAME,
    PROFILE_TAB_MINIMUM_HEIGHT,
    SERIAL_SHUTDOWN_TIMEOUT_MS,
    TAB_CONTENT_MINIMUM_HEIGHT,
    TAB_CONTENT_MINIMUM_WIDTH,
)
from plcsniffer.logging_config import log_event
from plcsniffer.modbus import CapturedModbusFrame
from plcsniffer.ui.log_viewer import LogViewerWidget
from plcsniffer.ui.packet_inspector import PacketInspectorWidget
from plcsniffer.ui.passive_capture import PassiveSniffingWidget
from plcsniffer.ui.profile_tab import ProfileTab

APP_THEME = """
QMainWindow {
    background: #f4f6f8;
}
QWidget {
    color: #1f2937;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QTabWidget::pane {
    background: #f4f6f8;
    border: 1px solid #cbd5e1;
}
QTabBar::tab {
    min-height: 38px;
    min-width: 190px;
    padding: 0 18px;
    color: #475569;
    background: #e7ebf0;
    border: 1px solid #cbd5e1;
    border-bottom: none;
    margin-right: 2px;
}
QTabBar::tab:selected {
    color: #174ea6;
    background: #ffffff;
    border-top: 2px solid #2563eb;
    font-weight: 600;
}
QTabBar::tab:hover:!selected {
    background: #dde3ea;
}
QGroupBox {
    margin-top: 12px;
    padding: 13px 9px 9px 9px;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 4px;
    color: #334155;
}
QLineEdit, QComboBox, QPlainTextEdit {
    min-height: 28px;
    padding: 2px 6px;
    color: #111827;
    background: #ffffff;
    border: 1px solid #aeb8c5;
    border-radius: 3px;
    selection-background-color: #2563eb;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #2563eb;
}
QLineEdit:disabled, QComboBox:disabled {
    color: #64748b;
    background: #e9edf2;
}
QPushButton {
    min-height: 30px;
    padding: 2px 12px;
    color: #27364b;
    background: #ffffff;
    border: 1px solid #9ca8b8;
    border-radius: 4px;
}
QPushButton:hover {
    background: #edf4ff;
    border-color: #4f82c4;
}
QPushButton:pressed {
    background: #dceafe;
}
QPushButton:disabled {
    color: #8a94a3;
    background: #e9edf2;
    border-color: #cbd2dc;
}
QPushButton[role="primary"] {
    color: #ffffff;
    background: #2563eb;
    border-color: #1d4ed8;
    font-weight: 600;
}
QPushButton[role="primary"]:hover {
    background: #1d4ed8;
}
QTableWidget {
    color: #1f2937;
    background: #ffffff;
    alternate-background-color: #f6f8fa;
    border: 1px solid #cbd5e1;
    gridline-color: #dfe4ea;
    selection-color: #17365f;
    selection-background-color: #dbeafe;
}
QHeaderView::section {
    min-height: 28px;
    padding: 3px 6px;
    color: #334155;
    background: #e9edf2;
    border: none;
    border-right: 1px solid #cbd5e1;
    border-bottom: 1px solid #cbd5e1;
    font-weight: 600;
}
QToolTip {
    color: #ffffff;
    background: #334155;
    border: 1px solid #1f2937;
    padding: 4px;
}
"""


class MainWindow(QMainWindow):
    """Four-tab shell for receive-only capture, inspection, logs, and profiles."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APPLICATION_NAME)
        self.setMinimumSize(1000, 700)
        self.resize(1400, 900)
        self.setStyleSheet(APP_THEME)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.passive_page = PassiveSniffingWidget()
        self.packet_inspector = PacketInspectorWidget()
        self.profile_tab = ProfileTab()
        self.logging_page = LogViewerWidget()

        self.passive_tab = self._make_scrollable(self.passive_page)
        self.packet_inspector_tab = self._make_scrollable(self.packet_inspector)
        self.profile_tab_widget = self._make_scrollable(self.profile_tab)
        # The Profile tab's register table already scrolls internally
        # (QTableWidget), so it doesn't need the shared 900px page floor the
        # other tabs use — that floor was forcing the *entire* tab (Edit/Save
        # buttons included) to scroll as one block on any window shorter
        # than 900px. A smaller floor lets the table's own scrollbar absorb
        # a short window instead, keeping the toolbar always visible.
        self.profile_tab.setMinimumHeight(PROFILE_TAB_MINIMUM_HEIGHT)
        self.tabs.addTab(self.passive_tab, "1. Passive Sniffing")
        self.tabs.addTab(self.packet_inspector_tab, "2. Packet Inspector")
        self.tabs.addTab(self.logging_page, "3. Logging")
        self.tabs.addTab(self.profile_tab_widget, "4. Profile")

        self.passive_page.packet_inspection_requested.connect(self.inspect_packet)
        self.passive_page.frame_observed.connect(self.profile_tab.set_latest_frame)
        self.passive_page.profile_captured.connect(self._on_profile_captured)
        log_event(logging.INFO, "application_opened")

    @staticmethod
    def _make_scrollable(content: QWidget) -> QScrollArea:
        """Wrap tab content in a resizable two-axis scroll area.

        Args:
            content: Widget that owns the tab's controls and data tables.

        Returns:
            Configured scroll-area container.
        """
        content.setMinimumSize(
            TAB_CONTENT_MINIMUM_WIDTH,
            TAB_CONTENT_MINIMUM_HEIGHT,
        )
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setWidget(content)
        return scroll_area

    def inspect_packet(self, packet: CapturedModbusFrame) -> None:
        """Render a captured packet and switch to the inspector tab."""
        self.packet_inspector.inspect_packet(packet)
        self.tabs.setCurrentWidget(self.packet_inspector_tab)

    def _on_profile_captured(self, profile: dict) -> None:
        """Hand a profile built from a capture session to the Profile tab.

        The Profile tab adds it (already unlocked for editing) and we jump
        the user there so they can rename it and review the detected
        registers before saving.
        """
        self.profile_tab.add_profile_from_data(profile)
        self.tabs.setCurrentWidget(self.profile_tab_widget)

    def closeEvent(self, event) -> None:
        self.logging_page.stop_refresh()
        # Both calls must run regardless of the first result — each stops
        # and joins its own worker thread, so short-circuiting would leave
        # the second tab's serial port open.
        passive_stopped = self.passive_page.shutdown(SERIAL_SHUTDOWN_TIMEOUT_MS)
        profile_stopped = self.profile_tab.shutdown(SERIAL_SHUTDOWN_TIMEOUT_MS)
        if not (passive_stopped and profile_stopped):
            QMessageBox.warning(
                self,
                "Still closing",
                "The passive serial sniffer is still closing. Please try again in a moment.",
            )
            self.logging_page.start_refresh()
            event.ignore()
            return
        log_event(logging.INFO, "application_closed")
        event.accept()
