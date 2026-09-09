from __future__ import annotations

import csv
from collections import deque
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from plcsniffer.config import (
    CAPTURE_TABLE_MINIMUM_HEIGHT,
    DEFAULT_BAUD_RATES,
    FILTER_DEBOUNCE_MS,
)
from plcsniffer.config import MAX_PAUSED_PACKETS as DEFAULT_MAX_PAUSED_PACKETS
from plcsniffer.config import (
    AutoDetectionSettings,
    SerialSettings,
)
from plcsniffer.exceptions import ConfigurationError
from plcsniffer.modbus import CapturedModbusFrame, REGISTER_TYPE_NOUNS
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.ui.responsive import METRICS, ResponsiveMode, apply_layout_spacing
from plcsniffer.ui.style import STATUS_STYLES

# message_table's floor per density — NORMAL keeps CAPTURE_TABLE_MINIMUM_
# HEIGHT itself (tests/test_architecture.py pins a >=500 floor there);
# Compact/Ultra-compact trade fewer guaranteed-visible rows for actually
# fitting a short viewport without the whole tab needing to scroll just to
# reach the table.
_CAPTURE_TABLE_MINIMUM_HEIGHT_BY_MODE = {
    ResponsiveMode.NORMAL: CAPTURE_TABLE_MINIMUM_HEIGHT,
    ResponsiveMode.COMPACT: 320,
    ResponsiveMode.ULTRA_COMPACT: 220,
}


class CheckableComboBox(QComboBox):
    """Closed combo box whose dropdown lists independently checkable items.

    Used for Auto mode's baud-rate/parity/stop-bit candidate pickers, so the
    sweep only tries what the user actually selects instead of always
    trying every possibility. The closed box shows a short summary (e.g.
    "9600, 19200" or "3 selected") instead of one value, and the dropdown
    stays open while checking/unchecking items so picking several doesn't
    mean reopening it each time.
    """

    selection_changed = Signal()
    def wheelEvent(self, event):
        event.ignore()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setFocusPolicy(Qt.NoFocus)
        self.setInsertPolicy(QComboBox.NoInsert)
        self._item_model = QStandardItemModel(self)
        self.setModel(self._item_model)
        # Intercept clicks inside the popup ourselves (and consume the
        # event) so toggling a checkbox doesn't also trigger QComboBox's
        # normal "select this item and close the popup" behavior.
        self.view().viewport().installEventFilter(self)
        self._refresh_display_text()

    def add_item(self, text: str, data, *, checked: bool = False) -> None:
        item = QStandardItem(text)
        item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        item.setData(data, Qt.UserRole)
        self._item_model.appendRow(item)
        self._refresh_display_text()

    def checked_values(self) -> list:
        return [
            self._item_model.item(row).data(Qt.UserRole)
            for row in range(self._item_model.rowCount())
            if self._item_model.item(row).checkState() == Qt.Checked
        ]

    def set_checked(self, data, checked: bool = True) -> None:
        """Check or uncheck the existing item whose data equals *data*.

        No-op if no item matches. Lets callers (tests, saved-selection
        restore) toggle a specific candidate without reaching into the
        private item model.
        """
        for row in range(self._item_model.rowCount()):
            item = self._item_model.item(row)
            if item.data(Qt.UserRole) == data:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                self._refresh_display_text()
                return

    def eventFilter(self, watched, event) -> bool:
        if (
            watched is self.view().viewport()
            and event.type() == QEvent.MouseButtonRelease
        ):
            index = self.view().indexAt(event.pos())
            if index.isValid():
                item = self._item_model.item(index.row())
                item.setCheckState(
                    Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
                )
                self._refresh_display_text()
                self.selection_changed.emit()
            return True
        return super().eventFilter(watched, event)

    def _refresh_display_text(self) -> None:
        checked = [
            self._item_model.item(row).text()
            for row in range(self._item_model.rowCount())
            if self._item_model.item(row).checkState() == Qt.Checked
        ]
        if not checked:
            text = "None selected"
        elif len(checked) <= 3:
            text = ", ".join(checked)
        else:
            text = f"{len(checked)} selected"
        self.lineEdit().setText(text)


class PassiveSniffingWidget(QWidget):
    """Receive-only Modbus RTU capture UI with manual and automatic setup."""

    connection_state_changed = Signal(bool)
    packet_inspection_requested = Signal(object)
    frame_observed = Signal(object)
    profile_captured = Signal(dict)

    BAUDRATES = DEFAULT_BAUD_RATES
    MAX_PAUSED_PACKETS = DEFAULT_MAX_PAUSED_PACKETS

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.capture_service = PassiveCaptureService(self)
        self._paused = False
        self._pending_frames: deque[CapturedModbusFrame] = deque(
            maxlen=self.MAX_PAUSED_PACKETS
        )
        self.last_frame: CapturedModbusFrame | None = None
        self._build_ui()
        self._connect_capture_service()
        self.refresh_ports()

    @property
    def worker(self):
        """Return the active worker for backwards-compatible diagnostics."""
        return self.capture_service.worker

    def _connect_capture_service(self) -> None:
        self.capture_service.frame_received.connect(self._on_frame)
        self.capture_service.status_changed.connect(self._set_status_message)
        self.capture_service.error_occurred.connect(self._on_error)
        self.capture_service.statistics_changed.connect(self._on_statistics)
        self.capture_service.configuration_detected.connect(self._on_auto_configuration)
        self.capture_service.running_changed.connect(self._on_running_changed)

    def _set_status_message(self, message: str, kind: str = "info") -> None:
        self.status_label.setStyleSheet(STATUS_STYLES.get(kind, STATUS_STYLES["info"]))
        self.status_label.setText(message)

    def current_profile_data(self) -> dict | None:
        """Package the live capture table into one multi-slave PLC profile.

        One physical RS-485 capture can see several Modbus slave IDs
        (several devices, or one PLC that answers on more than one unit
        ID) — this app can't tell those cases apart from traffic alone
        (see the module-level "one Save as Profile -> one PLC profile"
        product rule), so rather than rejecting a capture with more than
        one slave ID, every slave ID with usable response data becomes its
        own entry inside one top-level profile. Frames are grouped by
        slave_id first, then each slave's own register list is built
        independently — an address is only ever deduplicated against that
        same slave's other frames, never across slaves, so e.g. slave 1's
        register 10 and slave 2's register 10 both survive as distinct
        rows.
        """
        if self.message_table.rowCount() == 0:
            self._set_status_message("Need to sniff something first.", "warning")
            return None

        rows = [
            self._frame_for_row(row)
            for row in range(self.message_table.rowCount())
            if self._frame_for_row(row) is not None
        ]
        if not rows:
            self._set_status_message("Need to sniff something first.", "warning")
            return None

        response_frames = [
            frame
            for frame in rows
            if frame.frame_type.startswith("Response") or frame.direction == "Slave → Master"
        ]
        if not response_frames:
            self._set_status_message(
                "Need to sniff a response before saving a profile.", "warning"
            )
            return None

        frames_by_slave: dict[int, list[CapturedModbusFrame]] = {}
        for frame in response_frames:
            frames_by_slave.setdefault(frame.slave_id, []).append(frame)

        slaves: list[dict] = []
        for slave_id in sorted(frames_by_slave):
            register_rows: dict[int, dict] = {}
            for frame in frames_by_slave[slave_id]:
                if frame.address is None:
                    continue
                for offset, value in enumerate(frame.values):
                    address = frame.address + offset
                    if address in register_rows:
                        continue
                    register_type = REGISTER_TYPE_NOUNS.get(frame.function_code, "Register")
                    register_rows[address] = {
                        "name": f"{register_type} {address}",
                        "description": "",
                        "address": address,
                        "multiplier": 1.0,
                        "raw_hex": f"0x{value:04X}",
                        "raw_value": value,
                        "parsed_value": str(value),
                        "unit": "",
                        "function_code": frame.function_code,
                        # Every register from a capture starts as a plain
                        # 16-bit reading; a user who knows two adjacent
                        # addresses here are actually one wider value can
                        # combine them via the Profile tab's own Format
                        # column after saving.
                        "data_type": "uint16",
                        "byte_order": "big_endian",
                    }

            if not register_rows:
                # A slave ID that only ever showed up in requests (or whose
                # responses never carried decodable values) contributes no
                # usable data — omit it rather than saving an empty slave
                # entry.
                continue

            addresses = sorted(register_rows)
            slaves.append(
                {
                    "slave_id": slave_id,
                    "start_address": addresses[0],
                    "count": len(register_rows),
                    "registers": [register_rows[address] for address in addresses],
                }
            )

        if not slaves:
            self._set_status_message(
                "Need response register values to build a profile.", "warning"
            )
            return None

        total_registers = sum(len(slave["registers"]) for slave in slaves)
        # Deliberately doesn't restate the slave count here — ProfileTab's
        # own profile list already appends "— N slaves" to whichever name
        # is picked once a profile has more than one (see
        # ProfileTab._refresh_profile_list), so doing it here too would
        # just double up ("... — 3 slaves — 3 slaves").
        return {
            "id": None,
            "name": f"PLC Profile — {total_registers} register(s)",
            "baudrate": int(self.baud_combo.currentText()),
            "parity": str(self.parity_combo.currentData()),
            "stop_bits": float(self.stopbits_combo.currentData()),
            "slaves": slaves,
        }

    def save_as_profile(self) -> None:
        """Package the current capture into a profile and hand it to the Profile tab."""
        data = self.current_profile_data()
        if data is None:
            return
        self.profile_captured.emit(data)
        slave_count = len(data["slaves"])
        total_registers = sum(len(slave["registers"]) for slave in data["slaves"])
        slave_word = "slave" if slave_count == 1 else "slaves"
        self._set_status_message(
            f"Captured {total_registers} register(s) across {slave_count} {slave_word}. "
            "Review and save it on the Profile tab.",
            "saved",
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self._root_layout = layout

        notice = QLabel(
            "Receive-only passive sniffing: this application never transmits Modbus "
            "requests. Double-click any captured packet to open Packet Inspector."
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "padding: 8px; background: #ecfdf5; border: 1px solid #86d3b0;"
        )
        layout.addWidget(notice)

        settings_group = QGroupBox("Passive Capture Setup")
        settings = QGridLayout(settings_group)
        settings.setColumnStretch(1, 1)
        settings.setColumnStretch(3, 1)
        self._settings_grid = settings

        self.capture_mode_combo = QComboBox()
        self.capture_mode_combo.addItem("Config mode — fixed serial settings", "config")
        self.capture_mode_combo.addItem("Auto mode — detect serial settings", "auto")
        self.capture_mode_combo.currentIndexChanged.connect(self._update_mode_ui)
        # By default a QComboBox reserves enough width to show its longest
        # item in full ("Config mode — fixed serial settings" here), which
        # alone was forcing this whole tab past 800px wide. Sizing from a
        # character count instead lets it shrink on a small screen (eliding
        # the current selection with "...") while still growing to show the
        # full text when there's room; the dropdown itself is never elided.
        self.capture_mode_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.capture_mode_combo.setMinimumContentsLength(20)

        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(245)
        self.refresh_btn = QPushButton("Refresh Ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)

        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(str(value) for value in self.BAUDRATES)
        self.baud_combo.setCurrentText("9600")
        self.parity_combo = QComboBox()
        for label, value in (("None", "N"), ("Even", "E"), ("Odd", "O")):
            self.parity_combo.addItem(label, value)
        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItem("1", 1.0)
        self.stopbits_combo.addItem("1.5", 1.5)
        self.stopbits_combo.addItem("2", 2.0)

        # Auto mode's own settings row: which baud rates/parities/stop bits
        # to actually sweep (checkable — nothing checked by default, so a
        # sweep is never started without the user deliberately picking
        # candidates) and how long to listen on each combination before
        # moving to the next. Too short and traffic sent less often than
        # this window can be missed entirely — the sweep moves on before a
        # message ever arrives. If a full sweep of every checked combination
        # finds nothing, PassiveAutoDetectThread automatically adds 1 second
        # to this for the next sweep (see its _window_growth_s), so sparser
        # traffic eventually gets caught without the user re-guessing.
        self.auto_baud_combo = CheckableComboBox()
        for rate in self.BAUDRATES:
            self.auto_baud_combo.add_item(str(rate), rate)
        self.auto_baud_combo.setMinimumWidth(150)
        self.auto_baud_combo.setToolTip("Baud rates Auto mode will try.")
        self.auto_baud_combo.selection_changed.connect(self._update_mode_ui)

        self.auto_parity_combo = CheckableComboBox()
        for label, value in (("None", "N"), ("Even", "E"), ("Odd", "O")):
            self.auto_parity_combo.add_item(label, value)
        self.auto_parity_combo.setMinimumWidth(120)
        self.auto_parity_combo.setToolTip("Parity settings Auto mode will try.")
        self.auto_parity_combo.selection_changed.connect(self._update_mode_ui)

        self.auto_stopbits_combo = CheckableComboBox()
        for label, value in (("1", 1.0), ("1.5", 1.5), ("2", 2.0)):
            self.auto_stopbits_combo.add_item(label, value)
        self.auto_stopbits_combo.setMinimumWidth(120)
        self.auto_stopbits_combo.setToolTip("Stop-bit settings Auto mode will try.")
        self.auto_stopbits_combo.selection_changed.connect(self._update_mode_ui)

        self.auto_window_spin = QSpinBox()
        self.auto_window_spin.setRange(1, 60)
        self.auto_window_spin.setValue(1)
        self.auto_window_spin.setSuffix(" s")
        self.auto_window_spin.setToolTip(
            "How long Auto mode listens on each combination before trying "
            "the next. If a full pass finds nothing, the next pass "
            "automatically adds 1 second to this for every combination."
        )

        self.start_btn = QPushButton("Start Passive Sniffing")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.clicked.connect(self.toggle_monitor)

        # Kept as attributes (not local QLabels) purely so
        # _reflow_setup_row() can move them between grid cells later —
        # nothing about their own behavior needs it.
        self._setup_mode_label = QLabel("Setup mode")
        self._serial_port_label = QLabel("Serial port")
        self._reflow_setup_row(compact=False)

        # Config mode's and Auto mode's settings rows occupy the same grid
        # slot via a stacked widget — see _update_mode_ui(), which switches
        # pages — so switching modes swaps the whole row cleanly instead of
        # leaving disabled leftovers from the other mode in view.
        config_page = QWidget()
        config_row = QHBoxLayout(config_page)
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.addWidget(QLabel("Baud rate"))
        self.baud_combo.setMinimumWidth(100)
        config_row.addWidget(self.baud_combo)
        config_row.addWidget(QLabel("Parity"))
        self.parity_combo.setMinimumWidth(100)
        config_row.addWidget(self.parity_combo)
        config_row.addWidget(QLabel("Stop bits"))
        self.stopbits_combo.setMinimumWidth(100)
        config_row.addWidget(self.stopbits_combo)
        config_row.addStretch()

        auto_page = QWidget()
        auto_page_layout = QVBoxLayout(auto_page)
        auto_page_layout.setContentsMargins(0, 0, 0, 0)
        auto_page_layout.setSpacing(4)

        # Two rows of two label/field pairs rather than one row of four —
        # one long row of all four candidate pickers needs roughly 900px
        # just for itself, which alone forced this whole tab wider than an
        # 800px-class display. Two shorter rows need about half that width
        # while still fitting comfortably on a normal desktop window.
        auto_row = QGridLayout()
        auto_row.setHorizontalSpacing(8)
        auto_row.addWidget(QLabel("Baud rates to try"), 0, 0)
        auto_row.addWidget(self.auto_baud_combo, 0, 1)
        auto_row.addWidget(QLabel("Parity to try"), 0, 2)
        auto_row.addWidget(self.auto_parity_combo, 0, 3)
        auto_row.addWidget(QLabel("Stop bits to try"), 1, 0)
        auto_row.addWidget(self.auto_stopbits_combo, 1, 1)
        auto_row.addWidget(QLabel("Seconds per setting"), 1, 2)
        auto_row.addWidget(self.auto_window_spin, 1, 3)
        auto_row.setColumnStretch(4, 1)
        auto_page_layout.addLayout(auto_row)

        # Nothing is checked by default (deliberate — avoids accidentally
        # sweeping every combination) but that also means Start silently
        # does nothing useful until the user notices. This makes the
        # requirement visible up front instead of only after a blocked
        # Start click's status-bar message.
        self.auto_selection_hint = QLabel()
        self.auto_selection_hint.setWordWrap(True)
        # "editing" (amber) rather than "warning" (red) — this is a heads-up
        # reminder before Start is even clicked, not an error condition.
        self.auto_selection_hint.setStyleSheet(STATUS_STYLES["editing"])
        auto_page_layout.addWidget(self.auto_selection_hint)

        self.mode_settings_stack = QStackedWidget()
        self.mode_settings_stack.addWidget(config_page)
        self.mode_settings_stack.addWidget(auto_page)
        # Row numbers 10+ (not 1/2/3): _reflow_setup_row() below moves the
        # setup row between one row (0) and two (0-1) depending on density,
        # and an empty gap of unused row numbers costs a QGridLayout
        # nothing — only rows that actually contain an item take space — so
        # parking these well above any row the setup row could ever use
        # means the two never have to coordinate row numbers.
        settings.addWidget(self.mode_settings_stack, 10, 0, 1, 8)
        settings.addWidget(self.start_btn, 11, 0, 1, 8)

        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color: #526174; padding-top: 2px;")
        settings.addWidget(self.mode_hint, 12, 0, 1, 8)
        layout.addWidget(settings_group)

        filters_group = QGroupBox("Packet Filters")
        filters = QGridLayout(filters_group)
        filters.setColumnStretch(1, 1)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(
            "Search address, value, description, or raw RTU bytes..."
        )
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(self._schedule_filter)

        self.direction_filter = QComboBox()
        self.direction_filter.addItem("All directions", None)
        self.direction_filter.addItem("Master → Slave", "Master → Slave")
        self.direction_filter.addItem("Slave → Master", "Slave → Master")
        self.direction_filter.addItem("Unknown", "Unknown")
        self.direction_filter.currentIndexChanged.connect(self._apply_filter)

        self.frame_type_filter = QComboBox()
        self.frame_type_filter.addItem("All frame types", None)
        for frame_type in (
            "Request",
            "Response",
            "Exception response",
            "Unmatched frame",
            "CRC Error",
        ):
            self.frame_type_filter.addItem(frame_type, frame_type)
        self.frame_type_filter.currentIndexChanged.connect(self._apply_filter)

        self.slave_filter = QComboBox()
        self.slave_filter.addItem("All slave IDs", None)
        self.slave_filter.currentIndexChanged.connect(self._apply_filter)
        self.function_filter = QComboBox()
        self.function_filter.addItem("All functions", None)
        self.function_filter.currentIndexChanged.connect(self._apply_filter)

        # These four grow further items at runtime (e.g. "16 — Write
        # Multiple Registers" once that function is captured), and a
        # QComboBox's default size policy reserves enough width to show its
        # single longest item without eliding — so left alone, whichever
        # item happens to be longest silently forces this whole row (and the
        # tab) wider. Sizing from a character count keeps a compact, steady
        # width instead; the dropdown list itself always shows full text.
        for combo in (
            self.direction_filter,
            self.frame_type_filter,
            self.slave_filter,
            self.function_filter,
        ):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(14)

        self.reset_filters_btn = QPushButton("Reset Filters")
        self.reset_filters_btn.clicked.connect(self.reset_filters)
        self.filter_count_label = QLabel("Showing 0 of 0 packets")
        self.filter_count_label.setStyleSheet("color: #526174; font-weight: 600;")

        # Two rows of three filters rather than one row of six — halves the
        # width this group needs on a small screen without hiding anything.
        filters.addWidget(QLabel("Search"), 0, 0)
        filters.addWidget(self.search, 0, 1, 1, 2)
        filters.addWidget(self.direction_filter, 1, 0)
        filters.addWidget(self.frame_type_filter, 1, 1)
        filters.addWidget(self.reset_filters_btn, 1, 2)
        filters.addWidget(self.slave_filter, 2, 0)
        filters.addWidget(self.function_filter, 2, 1)
        filters.addWidget(self.filter_count_label, 2, 2)
        layout.addWidget(filters_group)

        # A QGridLayout (not QHBoxLayout) even in its one-row Normal form —
        # _reflow_actions_row() below repositions these same widgets into
        # two rows in Compact/Ultra-compact, the same technique
        # _reflow_setup_row() already uses. This row's buttons are
        # text-driven widths that barely respond to margin/spacing/padding
        # reduction alone, so — unlike the other rows — reflow is what
        # actually keeps this one from setting the tab's minimum width.
        actions = QGridLayout()
        self._actions_grid = actions
        self.pause_btn = QPushButton("Pause Display")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._set_paused)
        self.clear_btn = QPushButton("Clear Packets")
        self.clear_btn.clicked.connect(self.clear_messages)
        # Less-frequent actions tucked behind one menu button instead of
        # sitting in the row at equal weight to Pause/Clear — same slots as
        # before, just relocated.
        self.more_btn = QToolButton()
        self.more_btn.setText("More ▾")
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_btn)
        more_menu.setToolTipsVisible(True)
        self.copy_action = more_menu.addAction("Copy Selected")
        self.copy_action.setToolTip("Copy the selected captured rows to the clipboard.")
        self.copy_action.triggered.connect(self.copy_selected)
        self.export_action = more_menu.addAction("Export CSV")
        self.export_action.setToolTip("Export the captured packets to a CSV file.")
        self.export_action.triggered.connect(self.export_csv_dialog)
        self.more_btn.setMenu(more_menu)
        self.save_profile_btn = QPushButton("Save as Profile")
        self.save_profile_btn.setProperty("role", "primary")
        self.save_profile_btn.setToolTip(
            "Build a PLC profile from this capture: keeps the serial "
            "configuration and lists every register address/value seen in "
            "response packets, grouped separately by slave ID."
        )
        self.save_profile_btn.clicked.connect(self.save_as_profile)

        # Small "what actually gets saved" affordance, separate from the
        # button's own short tooltip — the details here (slave ID limit,
        # responses-only, duplicate-address handling, live-table-only scope)
        # are easy to get wrong assumptions about, so they get their own
        # always-visible icon instead of being buried in one long tooltip.
        self.save_profile_info_icon = QLabel("?")
        self.save_profile_info_icon.setFixedSize(18, 18)
        self.save_profile_info_icon.setAlignment(Qt.AlignCenter)
        self.save_profile_info_icon.setStyleSheet(
            "background: #e9edf2; color: #475569; border: 1px solid #9ca8b8;"
            "border-radius: 9px; font-weight: 600; font-size: 9pt;"
        )
        self.save_profile_info_icon.setToolTip(
            "<b>What gets saved:</b><br>"
            "&bull; <b>Every range you captured is combined</b> — not just the first. Poll "
            "0–9, then 50–59, then 200–205, and all three end up in one profile.<br>"
            "&bull; <b>Every slave ID becomes its own group</b> — one PLC profile is "
            "saved, but each slave ID seen gets its own register list; the same "
            "address on two different slaves is never merged.<br>"
            "&bull; <b>Responses only</b> — request values aren't used.<br>"
            "&bull; <b>Repeated addresses:</b> within the same slave, the first value "
            "seen wins; later repeats are ignored.<br>"
            "&bull; <b>Live table only</b> — clearing packets or pausing the display drops "
            "anything not currently shown."
        )

        # Purely a hint, not a control — the first thing dropped in
        # Compact/Ultra-compact (see _reflow_actions_row), matching the
        # task's priority of collapsing secondary/non-critical information
        # before anything that's actually interactive.
        self._double_click_tip_label = QLabel(
            "Tip: double-click a row for full inspection"
        )
        self._reflow_actions_row(compact=False)
        layout.addLayout(actions)

        footer = QHBoxLayout()
        self.status_label = QLabel("Ready. No serial port is open.")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(STATUS_STYLES["info"])
        footer.addWidget(self.status_label, stretch=1)
        self.stats_label = QLabel("Frames 0 · Requests 0 · Responses 0 · Errors 0")
        footer.addWidget(self.stats_label)
        self.auto_scroll = QCheckBox("Auto-scroll")
        self.auto_scroll.setChecked(True)
        footer.addWidget(self.auto_scroll)
        layout.addLayout(footer)

        self.message_table = QTableWidget(0, 8)
        self.message_table.setHorizontalHeaderLabels(
            [
                "Timestamp",
                "Direction",
                "Slave",
                "Function",
                "Frame Type",
                "Start Address",
                "Count",
                "Raw RTU Frame",
            ]
        )
        self.message_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.message_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.message_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.message_table.setAlternatingRowColors(True)
        self.message_table.verticalHeader().setVisible(False)
        self.message_table.verticalHeader().setDefaultSectionSize(24)
        self.message_table.setWordWrap(False)
        self.message_table.setMinimumHeight(CAPTURE_TABLE_MINIMUM_HEIGHT)
        header = self.message_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(7, QHeaderView.Stretch)
        for column, width in enumerate((105, 135, 65, 220, 125, 100, 70)):
            self.message_table.setColumnWidth(column, width)
        self.message_table.cellDoubleClicked.connect(self._inspect_row)
        layout.addWidget(self.message_table, stretch=1)

        self._update_mode_ui()

    def _reflow_setup_row(self, *, compact: bool) -> None:
        """Arrange the Setup mode / Serial port / Refresh row for the density.

        Normal: one row, [Setup mode][combo][Serial port][combo][Refresh] —
        the layout this tab has always used. Compact: two rows,
        [Setup mode][combo] / [Serial port][combo][Refresh], matching the
        task's own reflow example. Re-adding an already-placed widget to a
        QGridLayout moves it (Qt drops the old cell, no leaked/duplicate
        item — verified directly), so this is safe to call on every density
        change without accumulating stale cells.
        """
        grid = self._settings_grid
        if compact:
            grid.addWidget(self._setup_mode_label, 0, 0)
            grid.addWidget(self.capture_mode_combo, 0, 1, 1, 3)
            grid.addWidget(self._serial_port_label, 1, 0)
            grid.addWidget(self.port_combo, 1, 1, 1, 3)
            grid.addWidget(self.refresh_btn, 1, 4)
        else:
            grid.addWidget(self._setup_mode_label, 0, 0)
            grid.addWidget(self.capture_mode_combo, 0, 1)
            grid.addWidget(self._serial_port_label, 0, 2)
            grid.addWidget(self.port_combo, 0, 3)
            grid.addWidget(self.refresh_btn, 0, 4)

    def _reflow_actions_row(self, *, compact: bool) -> None:
        """Arrange the Pause/Clear/More/Save-as-Profile row for the density.

        Normal: one row, all five controls plus the trailing hint label —
        the layout this tab has always used. Compact/Ultra-compact: two
        rows (Pause/Clear/More, then Save as Profile/info icon) and the
        hint label is dropped entirely rather than reflowed — it's the
        least essential thing in this row (a nice-to-know, not a control),
        so it's the first thing to go per the task's priority of collapsing
        secondary information before anything interactive.
        """
        grid = self._actions_grid
        self._double_click_tip_label.setVisible(not compact)
        if compact:
            grid.addWidget(self.pause_btn, 0, 0)
            grid.addWidget(self.clear_btn, 0, 1)
            grid.addWidget(self.more_btn, 0, 2)
            grid.addWidget(self.save_profile_btn, 1, 0)
            grid.addWidget(self.save_profile_info_icon, 1, 1)
            grid.setColumnStretch(2, 0)
            grid.setColumnStretch(6, 1)
        else:
            grid.addWidget(self.pause_btn, 0, 0)
            grid.addWidget(self.clear_btn, 0, 1)
            grid.addWidget(self.more_btn, 0, 2)
            grid.addWidget(self.save_profile_btn, 0, 3)
            grid.addWidget(self.save_profile_info_icon, 0, 4)
            grid.addWidget(self._double_click_tip_label, 0, 6)
            grid.setColumnStretch(2, 0)
            grid.setColumnStretch(5, 1)

    def apply_responsive_mode(self, mode: ResponsiveMode) -> None:
        """Densify this tab's own layouts and reflow the Setup/Actions rows.

        message_table already has stretch=1 in the root layout (see
        _build_ui) and no fixed/oversized height beyond
        CAPTURE_TABLE_MINIMUM_HEIGHT, so it already claims whatever space
        these chrome rows don't need — shrinking their margins/spacing here
        is what actually grows the table's share on a short window.
        """
        metrics = METRICS[mode]
        margin = metrics.layout_margin
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        # One recursive call densifies every nested row/grid in this tab —
        # the settings/filters group boxes, the Config/Auto stacked pages,
        # the actions/footer rows — without wiring each one up individually.
        apply_layout_spacing(self._root_layout, metrics.layout_spacing)
        self.message_table.verticalHeader().setDefaultSectionSize(
            metrics.table_row_height
        )
        self.message_table.setMinimumHeight(
            _CAPTURE_TABLE_MINIMUM_HEIGHT_BY_MODE[mode]
        )

        compact = mode is not ResponsiveMode.NORMAL
        self._reflow_setup_row(compact=compact)
        self._reflow_actions_row(compact=compact)

    def refresh_ports(self) -> None:
        previous = (
            self.port_combo.currentData() or self.port_combo.currentText().strip()
        )
        self.port_combo.clear()
        try:
            ports = sorted(
                self.capture_service.available_ports(), key=lambda item: item.device
            )
        except Exception as error:
            self._set_status_message(f"Could not list serial ports: {error}", "warning")
            return
        for port in ports:
            self.port_combo.addItem(
                f"{port.device} — {port.description or 'Serial adapter'}", port.device
            )
        index = self.port_combo.findData(previous)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        elif previous:
            self.port_combo.setEditText(str(previous))

    def _selected_port(self) -> str:
        return (
            str(self.port_combo.currentData() or self.port_combo.currentText())
            .split(" — ", 1)[0]
            .strip()
        )

    def _update_mode_ui(self, *_args) -> None:
        running = self.capture_service.worker is not None
        auto_mode = self.capture_mode_combo.currentData() == "auto"
        self.capture_mode_combo.setEnabled(not running)
        self.port_combo.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.mode_settings_stack.setCurrentIndex(1 if auto_mode else 0)
        for widget in (self.baud_combo, self.parity_combo, self.stopbits_combo):
            widget.setEnabled(not running)
        for widget in (
            self.auto_baud_combo,
            self.auto_parity_combo,
            self.auto_stopbits_combo,
            self.auto_window_spin,
        ):
            widget.setEnabled(not running)
        if auto_mode:
            combination_count = (
                len(self.auto_baud_combo.checked_values())
                * len(self.auto_parity_combo.checked_values())
                * len(self.auto_stopbits_combo.checked_values())
            )
            self.mode_hint.setText(
                "Auto mode remains receive-only. It cycles the baud rate / parity / "
                "stop-bit combinations checked above "
                f"({combination_count} combination(s) selected) until a CRC-valid "
                "Modbus RTU packet is observed. Each combination gets the configured "
                "number of seconds to listen; if a full pass finds nothing, the next "
                "pass listens 1 second longer on every combination."
            )
            self.auto_selection_hint.setVisible(combination_count == 0)
            if combination_count == 0:
                self.auto_selection_hint.setText(
                    "Nothing is checked above yet — check at least one baud rate, "
                    "parity, and stop-bit option before clicking Start Passive "
                    "Sniffing."
                )
        else:
            self.mode_hint.setText(
                "Config mode listens with the exact baud rate, parity, and stop bits "
                "configured above."
            )
            self.auto_selection_hint.setVisible(False)

    def toggle_monitor(self) -> None:
        if self.capture_service.worker is not None:
            self.stop_monitor()
        else:
            self.start_monitor()

    def start_monitor(self) -> None:
        self.last_frame = None
        try:
            port = self._selected_port()
            if self.capture_mode_combo.currentData() == "auto":
                baudrates = self.auto_baud_combo.checked_values()
                parities = self.auto_parity_combo.checked_values()
                stop_bits_options = self.auto_stopbits_combo.checked_values()
                if not baudrates or not parities or not stop_bits_options:
                    self._set_status_message(
                        "Check at least one baud rate, parity, and stop-bit "
                        "setting for Auto mode to try.",
                        "warning",
                    )
                    return
                self.capture_service.start_automatic(
                    AutoDetectionSettings(
                        port=port,
                        baudrates=tuple(baudrates),
                        parities=tuple(parities),
                        stop_bits_options=tuple(stop_bits_options),
                        minimum_window_seconds=float(self.auto_window_spin.value()),
                    )
                )
            else:
                self.capture_service.start_configured(
                    SerialSettings(
                        port=port,
                        baudrate=int(self.baud_combo.currentText().strip()),
                        parity=str(self.parity_combo.currentData()),
                        stopbits=float(self.stopbits_combo.currentData()),
                    )
                )
        except (ConfigurationError, TypeError, ValueError) as error:
            self._set_status_message(f"Invalid passive capture setting: {error}", "warning")
            return

        self._set_status_message("Opening the serial port in receive-only mode...")

    def stop_monitor(self) -> None:
        if self.capture_service.worker is None:
            return
        self.start_btn.setText("Stopping...")
        self.start_btn.setEnabled(False)
        self.capture_service.stop()

    def _on_running_changed(self, running: bool) -> None:
        self._update_mode_ui()
        self.start_btn.setEnabled(True)
        self.start_btn.setText(
            "Stop Passive Sniffing" if running else "Start Passive Sniffing"
        )
        self.connection_state_changed.emit(running)

    def _on_auto_configuration(self, configuration: dict) -> None:
        baudrate = int(configuration["baudrate"])
        parity = str(configuration["parity"])
        stopbits = float(configuration["stopbits"])
        self.baud_combo.setCurrentText(str(baudrate))
        parity_index = self.parity_combo.findData(parity)
        if parity_index >= 0:
            self.parity_combo.setCurrentIndex(parity_index)
        stopbits_index = self.stopbits_combo.findData(stopbits)
        if stopbits_index >= 0:
            self.stopbits_combo.setCurrentIndex(stopbits_index)
        tested_rates = configuration.get("tested_baudrates")
        tested_text = (
            f" Tested baud rates: {', '.join(str(value) for value in tested_rates)}."
            if tested_rates
            else ""
        )
        self._set_status_message(
            f"Auto mode locked onto {baudrate} baud, parity {parity}, "
            f"{stopbits:g} stop bit(s). Listening only.{tested_text}"
        )

    def _on_error(self, message: str) -> None:
        self._set_status_message(message, "warning")

    def _schedule_filter(self, *_args) -> None:
        self._filter_timer.start()

    def _on_statistics(self, counts: dict) -> None:
        errors = int(counts.get("crc_errors", 0)) + int(counts.get("unmatched", 0))
        self.stats_label.setText(
            f"Frames {counts.get('frames', 0)} · "
            f"Requests {counts.get('requests', 0)} · "
            f"Responses {counts.get('responses', 0)} · Errors {errors}"
        )

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_btn.setText("Resume Display" if paused else "Pause Display")
        if not paused and self._pending_frames:
            queued = list(self._pending_frames)
            self._pending_frames.clear()
            self.message_table.setUpdatesEnabled(False)
            try:
                for frame in queued:
                    self._append_frame(frame, apply_filter=False)
            finally:
                self.message_table.setUpdatesEnabled(True)
            self._apply_filter()
            if self.auto_scroll.isChecked():
                self.message_table.scrollToBottom()
            self._set_status_message(
                f"Displayed {len(queued)} packet(s) received while paused."
            )

    def _on_frame(self, frame: CapturedModbusFrame) -> None:
        self.last_frame = frame
        self.frame_observed.emit(frame)
        if self._paused:
            self._pending_frames.append(frame)
            self._set_status_message(
                f"Display paused; {len(self._pending_frames)} packet(s) waiting."
            )
            return
        self._append_frame(frame)

    def _append_frame(
        self, frame: CapturedModbusFrame, *, apply_filter: bool = True
    ) -> None:
        self.last_frame = frame
        row = self.message_table.rowCount()
        self.message_table.insertRow(row)
        values = (
            frame.timestamp_text,
            frame.direction,
            str(frame.slave_id),
            frame.function_text,
            frame.frame_type,
            "—" if frame.address is None else str(frame.address),
            "—" if frame.quantity is None else str(frame.quantity),
            frame.raw_hex,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(frame.description)
            if column == 0:
                item.setData(Qt.UserRole, frame)
            self.message_table.setItem(row, column, item)

        if self.slave_filter.findData(frame.slave_id) < 0:
            self.slave_filter.addItem(f"Slave {frame.slave_id}", frame.slave_id)
        if self.function_filter.findData(frame.function_code) < 0:
            self.function_filter.addItem(
                f"{frame.function_code:02d} — {frame.function_name}",
                frame.function_code,
            )
        if apply_filter:
            if self._filters_are_active():
                self._apply_filter()
            else:
                total = self.message_table.rowCount()
                self.message_table.setRowHidden(row, False)
                self.filter_count_label.setText(f"Showing {total} of {total} packets")
        if (
            apply_filter
            and self.auto_scroll.isChecked()
            and not self.message_table.isRowHidden(row)
        ):
            self.message_table.scrollToBottom()

    def _frame_for_row(self, row: int) -> CapturedModbusFrame | None:
        item = self.message_table.item(row, 0)
        if item is None:
            return None
        packet = item.data(Qt.UserRole)
        return packet if isinstance(packet, CapturedModbusFrame) else None

    def _inspect_row(self, row: int, _column: int) -> None:
        packet = self._frame_for_row(row)
        if packet is not None:
            self.packet_inspection_requested.emit(packet)

    def _filters_are_active(self) -> bool:
        return bool(self.search.text().strip()) or any(
            combo.currentData() is not None
            for combo in (
                self.direction_filter,
                self.frame_type_filter,
                self.slave_filter,
                self.function_filter,
            )
        )

    def _apply_filter(self, *_args) -> None:
        query = self.search.text().strip().lower()
        direction = self.direction_filter.currentData()
        frame_type = self.frame_type_filter.currentData()
        slave_id = self.slave_filter.currentData()
        function_code = self.function_filter.currentData()
        visible = 0

        for row in range(self.message_table.rowCount()):
            packet = self._frame_for_row(row)
            if packet is None:
                self.message_table.setRowHidden(row, True)
                continue
            searchable = " ".join(
                self.message_table.item(row, column).text()
                for column in range(self.message_table.columnCount())
                if self.message_table.item(row, column) is not None
            )
            searchable = f"{searchable} {packet.description}".lower()
            matches = (
                (not query or query in searchable)
                and (direction is None or packet.direction == direction)
                and (frame_type is None or packet.frame_type == frame_type)
                and (slave_id is None or packet.slave_id == slave_id)
                and (function_code is None or packet.function_code == function_code)
            )
            self.message_table.setRowHidden(row, not matches)
            visible += int(matches)

        total = self.message_table.rowCount()
        self.filter_count_label.setText(f"Showing {visible} of {total} packets")
        filters_active = any(
            value is not None
            for value in (direction, frame_type, slave_id, function_code)
        ) or bool(query)
        self.reset_filters_btn.setEnabled(filters_active)

    def reset_filters(self) -> None:
        self.search.clear()
        for combo in (
            self.direction_filter,
            self.frame_type_filter,
            self.slave_filter,
            self.function_filter,
        ):
            combo.setCurrentIndex(0)
        self._apply_filter()

    def clear_messages(self) -> None:
        self.message_table.setRowCount(0)
        self._pending_frames.clear()
        self.slave_filter.clear()
        self.slave_filter.addItem("All slave IDs", None)
        self.function_filter.clear()
        self.function_filter.addItem("All functions", None)
        self._apply_filter()
        # Reset the footer counter to match the now-empty table. If a
        self.stats_label.setText("Frames 0 · Requests 0 · Responses 0 · Errors 0")
        self.capture_service.reset_statistics()
        self.last_frame = None

    def copy_selected(self) -> None:
        rows = sorted({index.row() for index in self.message_table.selectedIndexes()})
        if not rows:
            self._set_status_message("Select one or more captured rows to copy.", "warning")
            return
        lines = []
        for row in rows:
            lines.append(
                "\t".join(
                    self.message_table.item(row, column).text()
                    for column in range(self.message_table.columnCount())
                )
            )
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._set_status_message(f"Copied {len(rows)} captured row(s).", "saved")

    def export_csv_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Passive Modbus Capture",
            "modbus_capture.csv",
            "CSV (*.csv)",
        )
        if path:
            self.export_csv(Path(path))

    def export_csv(self, path: Path) -> None:
        try:
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    self.message_table.horizontalHeaderItem(column).text()
                    for column in range(self.message_table.columnCount())
                )
                for row in range(self.message_table.rowCount()):
                    writer.writerow(
                        self.message_table.item(row, column).text()
                        for column in range(self.message_table.columnCount())
                    )
        except OSError as error:
            self._set_status_message(f"Could not export capture: {error}", "warning")
            return
        self._set_status_message(
            f"Exported {self.message_table.rowCount()} frames to {path}.", "saved"
        )

    def shutdown(self, timeout_ms: int = 3_000) -> bool:
        return self.capture_service.shutdown(timeout_ms)


# Backwards-compatible name for integrations that imported the earlier widget class.
PassiveMonitorWidget = PassiveSniffingWidget
