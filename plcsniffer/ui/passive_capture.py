from __future__ import annotations

import csv
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from plcsniffer.config import (
    CAPTURE_TABLE_MINIMUM_HEIGHT,
    DEFAULT_AUTO_DETECTION_WINDOW_SECONDS,
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
        self.capture_service.status_changed.connect(self.status_label.setText)
        self.capture_service.error_occurred.connect(self._on_error)
        self.capture_service.statistics_changed.connect(self._on_statistics)
        self.capture_service.configuration_detected.connect(self._on_auto_configuration)
        self.capture_service.running_changed.connect(self._on_running_changed)

    def _set_status_message(self, message: str) -> None:
        self.status_label.setText(message)

    def current_profile_data(self) -> dict | None:
        if self.message_table.rowCount() == 0:
            self._set_status_message("Need to sniff something first.")
            return None

        rows = [
            self._frame_for_row(row)
            for row in range(self.message_table.rowCount())
            if self._frame_for_row(row) is not None
        ]
        if not rows:
            self._set_status_message("Need to sniff something first.")
            return None

        slave_ids = {frame.slave_id for frame in rows}
        if len(slave_ids) > 1:
            self._set_status_message("Need to have only one slave id when save.")
            return None

        response_frames = [
            frame
            for frame in rows
            if frame.frame_type.startswith("Response") or frame.direction == "Slave → Master"
        ]
        if not response_frames:
            self._set_status_message("Need to sniff a response before saving a profile.")
            return None

        slave_id = next(iter(slave_ids))
        register_rows: dict[int, dict] = {}
        for frame in response_frames:
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
                    "parsed_value": str(value),
                    "unit": "",
                    "function_code": frame.function_code,
                }

        if not register_rows:
            self._set_status_message("Need response register values to build a profile.")
            return None

        addresses = sorted(register_rows)
        packet = response_frames[-1]
        return {
            "id": None,
            "name": f"Slave {slave_id} — {len(register_rows)} register(s)",
            "slave_id": slave_id,
            "start_address": addresses[0],
            "count": len(register_rows),
            "baudrate": int(self.baud_combo.currentText()),
            "parity": str(self.parity_combo.currentData()),
            "stop_bits": float(self.stopbits_combo.currentData()),
            "registers": [register_rows[address] for address in addresses],
        }

    def save_as_profile(self) -> None:
        """Package the current capture into a profile and hand it to the Profile tab."""
        data = self.current_profile_data()
        if data is None:
            return
        self.profile_captured.emit(data)
        self._set_status_message(
            f"Captured {len(data['registers'])} register(s) for slave {data['slave_id']}. "
            "Review and save it on the Profile tab."
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

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

        self.capture_mode_combo = QComboBox()
        self.capture_mode_combo.addItem("Config mode — fixed serial settings", "config")
        self.capture_mode_combo.addItem("Auto mode — detect serial settings", "auto")
        self.capture_mode_combo.currentIndexChanged.connect(self._update_mode_ui)
        self.capture_mode_combo.setMinimumWidth(270)

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

        self.start_btn = QPushButton("Start Passive Sniffing")
        self.start_btn.setProperty("role", "primary")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.clicked.connect(self.toggle_monitor)

        settings.addWidget(QLabel("Setup mode"), 0, 0)
        settings.addWidget(self.capture_mode_combo, 0, 1)
        settings.addWidget(QLabel("Serial port"), 0, 2)
        settings.addWidget(self.port_combo, 0, 3)
        settings.addWidget(self.refresh_btn, 0, 4)
        settings.addWidget(QLabel("Baud rate"), 1, 0)
        settings.addWidget(self.baud_combo, 1, 1)
        settings.addWidget(QLabel("Parity"), 1, 2)
        settings.addWidget(self.parity_combo, 1, 3)
        settings.addWidget(QLabel("Stop bits"), 1, 4)
        settings.addWidget(self.stopbits_combo, 1, 5)
        settings.addWidget(self.start_btn, 2, 0, 1, 6)

        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color: #526174; padding-top: 2px;")
        settings.addWidget(self.mode_hint, 3, 0, 1, 6)
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
        ):
            self.frame_type_filter.addItem(frame_type, frame_type)
        self.frame_type_filter.currentIndexChanged.connect(self._apply_filter)

        self.slave_filter = QComboBox()
        self.slave_filter.addItem("All slave IDs", None)
        self.slave_filter.currentIndexChanged.connect(self._apply_filter)
        self.function_filter = QComboBox()
        self.function_filter.addItem("All functions", None)
        self.function_filter.currentIndexChanged.connect(self._apply_filter)

        self.reset_filters_btn = QPushButton("Reset Filters")
        self.reset_filters_btn.clicked.connect(self.reset_filters)
        self.filter_count_label = QLabel("Showing 0 of 0 packets")
        self.filter_count_label.setStyleSheet("color: #526174; font-weight: 600;")

        filters.addWidget(QLabel("Search"), 0, 0)
        filters.addWidget(self.search, 0, 1, 1, 5)
        filters.addWidget(self.direction_filter, 1, 0)
        filters.addWidget(self.frame_type_filter, 1, 1)
        filters.addWidget(self.slave_filter, 1, 2)
        filters.addWidget(self.function_filter, 1, 3)
        filters.addWidget(self.reset_filters_btn, 1, 4)
        filters.addWidget(self.filter_count_label, 1, 5)
        layout.addWidget(filters_group)

        actions = QHBoxLayout()
        self.pause_btn = QPushButton("Pause Display")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._set_paused)
        actions.addWidget(self.pause_btn)
        self.clear_btn = QPushButton("Clear Packets")
        self.clear_btn.clicked.connect(self.clear_messages)
        actions.addWidget(self.clear_btn)
        self.copy_btn = QPushButton("Copy Selected")
        self.copy_btn.clicked.connect(self.copy_selected)
        actions.addWidget(self.copy_btn)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export_csv_dialog)
        actions.addWidget(self.export_btn)
        self.save_profile_btn = QPushButton("Save as Profile")
        self.save_profile_btn.setProperty("role", "primary")
        self.save_profile_btn.setToolTip(
            "Build a profile from this capture: keeps the serial configuration "
            "and lists every register address/value seen in response packets "
            "from a single slave ID."
        )
        self.save_profile_btn.clicked.connect(self.save_as_profile)
        actions.addWidget(self.save_profile_btn)
        actions.addStretch()
        actions.addWidget(QLabel("Tip: double-click a row for full inspection"))
        layout.addLayout(actions)

        self.message_table = QTableWidget(0, 9)
        self.message_table.setHorizontalHeaderLabels(
            [
                "Timestamp",
                "Direction",
                "Slave",
                "Function",
                "Frame Type",
                "Start Address",
                "Count",
                "Values",
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
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        for column, width in enumerate((105, 135, 65, 220, 125, 100, 70, 230)):
            self.message_table.setColumnWidth(column, width)
        self.message_table.cellDoubleClicked.connect(self._inspect_row)
        layout.addWidget(self.message_table, stretch=1)

        footer = QHBoxLayout()
        self.status_label = QLabel("Ready. No serial port is open.")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, stretch=1)
        self.stats_label = QLabel("Frames 0 · Requests 0 · Responses 0 · Errors 0")
        footer.addWidget(self.stats_label)
        self.auto_scroll = QCheckBox("Auto-scroll")
        self.auto_scroll.setChecked(True)
        footer.addWidget(self.auto_scroll)
        layout.addLayout(footer)
        self._update_mode_ui()

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
            self.status_label.setText(f"Could not list serial ports: {error}")
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
        for widget in (self.baud_combo, self.parity_combo, self.stopbits_combo):
            widget.setEnabled(not running and not auto_mode)
        if auto_mode:
            self.mode_hint.setText(
                "Auto mode remains receive-only. It cycles common baud, parity, and "
                "stop-bit settings until a CRC-valid Modbus RTU packet is observed."
            )
        else:
            self.mode_hint.setText(
                "Config mode listens with the exact baud rate, parity, and stop bits "
                "configured above."
            )

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
                self.capture_service.start_automatic(
                    AutoDetectionSettings(
                        port=port,
                        baudrates=self.BAUDRATES,
                        minimum_window_seconds=(DEFAULT_AUTO_DETECTION_WINDOW_SECONDS),
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
            self.status_label.setText(f"Invalid passive capture setting: {error}")
            return

        self.status_label.setText("Opening the serial port in receive-only mode...")

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
        self.status_label.setText(
            f"Auto mode locked onto {baudrate} baud, parity {parity}, "
            f"{stopbits:g} stop bit(s). Listening only.{tested_text}"
        )

    def _on_error(self, message: str) -> None:
        self.status_label.setText(message)

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
            self.status_label.setText(
                f"Displayed {len(queued)} packet(s) received while paused."
            )

    def _on_frame(self, frame: CapturedModbusFrame) -> None:
        self.last_frame = frame
        self.frame_observed.emit(frame)
        if self._paused:
            self._pending_frames.append(frame)
            self.status_label.setText(
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
            frame.values_text,
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
        # capture is still running, also zero the worker's own running
        # totals — otherwise the next frame would make the counter jump
        # right back up to the pre-clear numbers.
        self.stats_label.setText("Frames 0 · Requests 0 · Responses 0 · Errors 0")
        self.capture_service.reset_statistics()
        self.last_frame = None

    def copy_selected(self) -> None:
        rows = sorted({index.row() for index in self.message_table.selectedIndexes()})
        if not rows:
            self.status_label.setText("Select one or more captured rows to copy.")
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
        self.status_label.setText(f"Copied {len(rows)} captured row(s).")

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
            self.status_label.setText(f"Could not export capture: {error}")
            return
        self.status_label.setText(
            f"Exported {self.message_table.rowCount()} frames to {path}."
        )

    def shutdown(self, timeout_ms: int = 3_000) -> bool:
        return self.capture_service.shutdown(timeout_ms)


# Backwards-compatible name for integrations that imported the earlier widget class.
PassiveMonitorWidget = PassiveSniffingWidget
