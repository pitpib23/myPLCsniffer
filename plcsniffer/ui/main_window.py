from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
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
    MAIN_WINDOW_MINIMUM_HEIGHT,
    MAIN_WINDOW_MINIMUM_WIDTH,
    RESIZE_DEBOUNCE_MS,
    SERIAL_SHUTDOWN_TIMEOUT_MS,
)
from plcsniffer.logging_config import log_event
from plcsniffer.modbus import CapturedModbusFrame
from plcsniffer.ui.log_viewer import LogViewerWidget
from plcsniffer.ui.lite_scroll import LiteScrollOwner
from plcsniffer.ui.packet_inspector import PacketInspectorWidget
from plcsniffer.ui.passive_capture import PassiveSniffingWidget
from plcsniffer.ui.profile_tab import ProfileTab
from plcsniffer.ui.responsive import METRICS, ResponsiveMetrics, ResponsiveMode, compute_mode


def _build_theme(metrics: ResponsiveMetrics) -> str:
    """Render the app's one QSS theme for a given density tier.

    Everything mode-dependent here is widget *chrome* — padding, minimum
    height, font size — the things a Qt stylesheet can actually express.
    Layout margins/spacing are a different mechanism (QLayout properties
    aren't stylesheet-able), applied per-tab instead; see
    MainWindow._recompute_responsive_mode().
    """
    top, right, bottom, left = metrics.group_padding
    return f"""
QMainWindow {{
    background: #f4f6f8;
}}
QWidget {{
    color: #1f2937;
    font-family: "Segoe UI";
    font-size: {metrics.base_font_pt}pt;
}}
QTabWidget::pane {{
    background: #f4f6f8;
    border: 1px solid #cbd5e1;
}}
QTabBar::tab {{
    min-height: {metrics.tab_min_height}px;
    min-width: {metrics.tab_min_width}px;
    padding: {metrics.tab_padding};
    color: #475569;
    background: #e7ebf0;
    border: 1px solid #cbd5e1;
    border-bottom: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    color: #174ea6;
    background: #ffffff;
    border-top: 2px solid #2563eb;
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    background: #dde3ea;
}}
QGroupBox {{
    margin-top: {metrics.group_margin_top}px;
    padding: {top}px {right}px {bottom}px {left}px;
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 4px;
    color: #334155;
}}
QLineEdit, QComboBox, QPlainTextEdit {{
    min-height: {metrics.field_min_height}px;
    padding: 2px 6px;
    color: #111827;
    background: #ffffff;
    border: 1px solid #aeb8c5;
    border-radius: 3px;
    selection-background-color: #2563eb;
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid #2563eb;
}}
QLineEdit:disabled, QComboBox:disabled {{
    color: #64748b;
    background: #e9edf2;
}}
QPushButton {{
    min-height: {metrics.control_min_height}px;
    padding: {metrics.control_padding};
    color: #27364b;
    background: #ffffff;
    border: 1px solid #9ca8b8;
    border-radius: 4px;
}}
QPushButton:hover {{
    background: #edf4ff;
    border-color: #4f82c4;
}}
QPushButton:pressed {{
    background: #dceafe;
}}
QPushButton:disabled {{
    color: #8a94a3;
    background: #e9edf2;
    border-color: #cbd2dc;
}}
QPushButton[role="primary"] {{
    color: #ffffff;
    background: #2563eb;
    border-color: #1d4ed8;
    font-weight: 600;
}}
QPushButton[role="primary"]:hover {{
    background: #1d4ed8;
}}
QToolButton {{
    min-height: {metrics.control_min_height}px;
    padding: {metrics.control_padding};
    color: #27364b;
    background: #ffffff;
    border: 1px solid #9ca8b8;
    border-radius: 4px;
}}
QToolButton:hover {{
    background: #edf4ff;
    border-color: #4f82c4;
}}
QToolButton:pressed {{
    background: #dceafe;
}}
QToolButton::menu-indicator {{
    image: none;
    width: 0px;
}}
QTableWidget {{
    color: #1f2937;
    background: #ffffff;
    alternate-background-color: #f6f8fa;
    border: 1px solid #cbd5e1;
    gridline-color: #dfe4ea;
    selection-color: #17365f;
    selection-background-color: #dbeafe;
}}
QHeaderView::section {{
    min-height: {metrics.header_min_height}px;
    padding: 3px 6px;
    color: #334155;
    background: #e9edf2;
    border: none;
    border-right: 1px solid #cbd5e1;
    border-bottom: 1px solid #cbd5e1;
    font-weight: 600;
}}
QToolTip {{
    color: #ffffff;
    background: #334155;
    border: 1px solid #1f2937;
    padding: 4px;
}}
"""


# Kept for anything (tests, a REPL) that still wants "the" theme string —
# equivalent to _build_theme(METRICS[ResponsiveMode.NORMAL]), which is what
# MainWindow itself applies at startup before its first size is known.
APP_THEME = _build_theme(METRICS[ResponsiveMode.NORMAL])


class MainWindow(QMainWindow):
    """Shell for receive-only capture, inspection, logs, and profiles.

    Two presentation modes share this one implementation:

    - Full (default, ``lite=False``): the original four-tab desktop
      layout — Passive Sniffing / Packet Inspector / Logging / Profile —
      unchanged from before Lite existed.
    - Lite (``lite=True``, ``python main.py --lite``): a three-tab
      Raspberry Pi 7" touchscreen edition (Sniff / Inspect / Profile) —
      the Logging tab/LogViewerWidget is dropped from navigation (backend
      Python logging keeps writing to disk regardless), and the retained
      tabs get touchscreen-appropriate presentation. See PassiveSniffingWidget,
      PacketInspectorWidget, and ProfileTab's own ``lite`` handling for the
      per-tab differences; backend capture/parsing/profile behavior is
      identical in both modes.
    """

    def __init__(self, lite: bool = False) -> None:
        super().__init__()
        self._lite = lite
        self.setWindowTitle(APPLICATION_NAME)
        self.setMinimumSize(MAIN_WINDOW_MINIMUM_WIDTH, MAIN_WINDOW_MINIMUM_HEIGHT)
        self.resize(*(self._initial_size() if lite else (1400, 900)))
        # None (not a real mode) until _recompute_responsive_mode()'s first
        # call at the end of __init__ — that first call must always apply
        # to every tab, even when the computed mode happens to be NORMAL,
        # so this can't reuse NORMAL itself as the "nothing applied yet"
        # sentinel.
        self._responsive_mode: ResponsiveMode | None = None
        self.setStyleSheet(_build_theme(METRICS[ResponsiveMode.NORMAL]))

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.passive_page = PassiveSniffingWidget(lite=lite)
        self.packet_inspector = PacketInspectorWidget(lite=lite)
        self.profile_tab = ProfileTab(lite=lite)
        # Every tab implements apply_responsive_mode(mode) for the parts a
        # shared stylesheet can't reach — see _recompute_responsive_mode().
        self._responsive_tabs = (
            self.passive_page,
            self.packet_inspector,
            self.profile_tab,
        )

        self.passive_tab = self._make_scrollable(self.passive_page, lite=lite)
        self.packet_inspector_tab = self._make_scrollable(self.packet_inspector, lite=lite)
        self.profile_tab_widget = self._make_scrollable(self.profile_tab, lite=lite)

        if lite:
            LiteScrollOwner.install(self, (
                self.passive_tab, self.packet_inspector_tab, self.profile_tab_widget
            ))
            # Short, touch-friendly labels, no numeric prefixes, and no
            # Logging tab in navigation — see the class docstring.
            self.tabs.addTab(self.passive_tab, "Sniff")
            self.tabs.addTab(self.packet_inspector_tab, "Inspect")
            self.tabs.addTab(self.profile_tab_widget, "Profile")
        else:
            self.logging_page = LogViewerWidget()
            self._responsive_tabs = self._responsive_tabs + (self.logging_page,)
            self.tabs.addTab(self.passive_tab, "1. Passive Sniffing")
            self.tabs.addTab(self.packet_inspector_tab, "2. Packet Inspector")
            self.tabs.addTab(self.logging_page, "3. Logging")
            self.tabs.addTab(self.profile_tab_widget, "4. Profile")

        self.passive_page.packet_inspection_requested.connect(self.inspect_packet)
        self.passive_page.frame_observed.connect(self.profile_tab.set_latest_frame)
        self.passive_page.profile_captured.connect(self._on_profile_captured)

        # Debounced resize handling: a drag-resize fires many resizeEvents
        # per second, but recomputing density and reapplying a stylesheet on
        # every single one would be wasted work on a Raspberry Pi (and can
        # visibly stutter). A single-shot timer restarted on every
        # resizeEvent coalesces a burst of events into one recompute shortly
        # after resizing settles, per the task's "avoid resize-event loops
        # and excessive layout churn... debounce appropriately."
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(RESIZE_DEBOUNCE_MS)
        self._resize_debounce_timer.timeout.connect(self._recompute_responsive_mode)

        # Best-effort second trigger for the on-screen-keyboard case: how a
        # keyboard affects window size is window-manager-dependent (it may
        # resize this window directly, in which case resizeEvent alone
        # already covers it, or it may only shrink the screen's available
        # area). Reacting to both costs nothing extra — the debounce timer
        # coalesces whichever fires.
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            screen.availableGeometryChanged.connect(self._schedule_responsive_recompute)

        # Apply the mode implied by the initial size once everything exists,
        # so a window that *starts* small (or maximized on a small display)
        # is correctly densified from the first frame, not just after a
        # later resize.
        self._recompute_responsive_mode()
        log_event(logging.INFO, "application_opened")

    @staticmethod
    def _initial_size() -> tuple[int, int]:
        """Lite's startup window size, targeting a 7" 800x480 Pi touchscreen.

        Uses the primary screen's *available* geometry (excludes panels/
        window-manager chrome) rather than assuming the full 800x480 is
        free — never relies on a 900px-tall window (the full edition's
        default) and never requests more than the target 800x480 either,
        so a genuinely small panel isn't asked to grow a maximized window
        past its own size.
        """
        target_width, target_height = 800, 480
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return target_width, target_height
        available = screen.availableGeometry()
        return (
            max(MAIN_WINDOW_MINIMUM_WIDTH, min(target_width, available.width())),
            max(MAIN_WINDOW_MINIMUM_HEIGHT, min(target_height, available.height())),
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_responsive_recompute()

    def _schedule_responsive_recompute(self, *_args) -> None:
        self._resize_debounce_timer.start()

    def _recompute_responsive_mode(self) -> None:
        """Re-derive density from the current window size and apply it.

        Two independent mechanisms, both reversible and neither rebuilding
        widgets: the QSS theme (control chrome — padding, min-height, font)
        is regenerated and reapplied in one setStyleSheet() call, which Qt
        cascades to the whole window; each tab's own
        apply_responsive_mode() then handles what a stylesheet can't
        (layout margins/spacing, and structural reflow like Tab 1's Setup
        row or the Profile sidebar). A no-op when the mode hasn't actually
        changed, so idle resizes that stay within one tier cost nothing.
        """
        size = self.size()
        mode = compute_mode(size.width(), size.height())
        if mode is self._responsive_mode:
            return
        self._responsive_mode = mode
        self.setStyleSheet(_build_theme(METRICS[mode]))
        for tab in self._responsive_tabs:
            tab.apply_responsive_mode(mode)

    @staticmethod
    def _make_scrollable(content: QWidget, *, lite: bool = False) -> QScrollArea:
        """Wrap tab content in a resizable two-axis scroll area.

        Deliberately does *not* impose an artificial minimum size on
        ``content``: each tab's natural floor already comes from its own
        children (table minimum heights, button/combo minimum widths), so
        the scroll area only grows scrollbars when a window genuinely can't
        fit that content — not for every window smaller than some
        desktop-sized constant. That's what lets the main window shrink
        close to Raspberry Pi-class displays (see
        MAIN_WINDOW_MINIMUM_WIDTH/HEIGHT) without every tab requiring
        two-axis scrolling just to reach its own primary controls.

        Args:
            content: Widget that owns the tab's controls and data tables.
            lite: Retained for callers; the Lite shell installs one input
                owner after all pages have been wrapped.

        Returns:
            Configured scroll-area container.
        """
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
        if not self._lite:
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
            if not self._lite:
                self.logging_page.start_refresh()
            event.ignore()
            return
        log_event(logging.INFO, "application_closed")
        event.accept()
