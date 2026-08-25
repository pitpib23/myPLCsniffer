from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from plcsniffer.config import (
    INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT,
    INSPECTOR_SUMMARY_MINIMUM_HEIGHT,
)
from plcsniffer.modbus import (
    CapturedModbusFrame,
    crc_is_valid,
    modbus_crc,
)


class PacketInspectorWidget(QWidget):
    """Show a concise packet summary with optional byte-level information."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_packet: CapturedModbusFrame | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        # This widget is intended to live inside the tab's QScrollArea.
        # The first two tables grow to show every row. The byte table shows
        # up to 16 rows and scrolls internally when a frame is longer.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)

        self.help_label = QLabel(
            "Double-click a packet on the Passive Sniffing tab to inspect it here. "
            "The summary contains the fields normally needed for troubleshooting."
        )
        self.help_label.setWordWrap(True)
        self.help_label.setStyleSheet(
            "padding: 9px; background: #eef5ff; border: 1px solid #b8d4f5;"
        )
        self.help_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.help_label)

        # First group: summary table, then the toggle button at the bottom.
        self.summary_group = QGroupBox("Important Packet Information")
        summary_layout = QVBoxLayout(self.summary_group)
        summary_layout.setContentsMargins(12, 18, 12, 12)
        summary_layout.setSpacing(8)

        self.summary_table = QTableWidget(0, 2)
        self.summary_table.setHorizontalHeaderLabels(["Field", "Value"])
        self._configure_property_table(self.summary_table)
        self.summary_table.setMinimumHeight(INSPECTOR_SUMMARY_MINIMUM_HEIGHT)
        summary_layout.addWidget(self.summary_table)

        self.show_all_btn = QPushButton("Show All Packet Information")
        self.show_all_btn.setCheckable(True)
        self.show_all_btn.setEnabled(False)
        self.show_all_btn.toggled.connect(self._toggle_all_information)
        summary_layout.addWidget(self.show_all_btn)

        self.summary_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.summary_group)

        # Expanded information appears directly below the button/first group.
        self.all_information_group = QGroupBox("Complete Byte-Level Information")
        all_layout = QVBoxLayout(self.all_information_group)
        all_layout.setContentsMargins(12, 18, 12, 12)
        all_layout.setSpacing(10)

        self.full_details_table = QTableWidget(0, 2)
        self.full_details_table.setHorizontalHeaderLabels(["Property", "Exact value"])
        self._configure_property_table(self.full_details_table)
        all_layout.addWidget(self.full_details_table)

        self.byte_table = QTableWidget(0, 5)
        self.byte_table.setHorizontalHeaderLabels(
            ["Offset", "Hex", "Decimal", "Binary", "Meaning"]
        )
        self.byte_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.byte_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.byte_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.byte_table.setAlternatingRowColors(True)
        self.byte_table.setWordWrap(True)
        self.byte_table.verticalHeader().setVisible(False)
        # Keep the header visible by scrolling inside this table.
        self.byte_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.byte_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.byte_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.byte_table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.byte_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.byte_table.setMinimumHeight(INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT)

        byte_header = self.byte_table.horizontalHeader()
        for column in range(4):
            byte_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        byte_header.setSectionResizeMode(4, QHeaderView.Stretch)
        all_layout.addWidget(self.byte_table)

        self.all_information_group.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        self.all_information_group.setVisible(False)
        layout.addWidget(self.all_information_group)

    @staticmethod
    def _configure_property_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(True)
        table.verticalHeader().setVisible(False)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)

    @staticmethod
    def _fit_table_to_rows(table: QTableWidget) -> None:
        """Make a table exactly tall enough for its header and every row."""
        table.resizeRowsToContents()

        height = table.horizontalHeader().height()
        height += sum(table.rowHeight(row) for row in range(table.rowCount()))
        height += table.frameWidth() * 2 + 2

        table.setFixedHeight(height)

    @staticmethod
    def _fit_table_to_visible_rows(
        table: QTableWidget, maximum_visible_rows: int = 16
    ) -> None:
        """Show at most ``maximum_visible_rows`` and scroll additional rows."""
        table.resizeRowsToContents()

        visible_rows = min(table.rowCount(), maximum_visible_rows)
        height = table.horizontalHeader().height()
        height += sum(table.rowHeight(row) for row in range(visible_rows))
        height += table.frameWidth() * 2 + 2

        table.setFixedHeight(height)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _fit_all_tables(self) -> None:
        self._fit_table_to_rows(self.summary_table)
        self._fit_table_to_rows(self.full_details_table)
        self._fit_table_to_visible_rows(self.byte_table, maximum_visible_rows=16)

        # Recalculate group-box size hints after table heights change.
        self.summary_group.adjustSize()
        if self.all_information_group.isVisible():
            self.all_information_group.adjustSize()
        self.updateGeometry()

    def inspect_packet(self, packet: CapturedModbusFrame) -> None:
        """Replace the current inspector contents with one captured packet.

        Args:
            packet: CRC-valid or diagnostic packet record to render.
        """
        self.current_packet = packet
        received_crc = (
            int.from_bytes(packet.raw[-2:], "little") if len(packet.raw) >= 2 else None
        )
        calculated_crc = modbus_crc(packet.raw[:-2]) if len(packet.raw) >= 2 else None
        response_time = getattr(packet, "response_time_ms", None)
        if callable(response_time):
            response_time = response_time()

        summary = [
            (
                "Timestamp",
                datetime.fromtimestamp(packet.timestamp).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )[:-3],
            ),
            ("Direction", packet.direction),
            ("Slave / Unit ID", f"{packet.slave_id} (0x{packet.slave_id:02X})"),
            (
                "Function",
                f"{packet.function_code} (0x{packet.function_code:02X}) — "
                f"{packet.function_name}",
            ),
            ("Frame type", packet.frame_type),
            ("Start address", self._display(packet.address)),
            ("Quantity", self._display(packet.quantity)),
            ("Raw RTU frame", packet.raw_hex),
            ("Frame length", f"{len(packet.raw)} bytes"),
            ("CRC status", "Valid" if crc_is_valid(packet.raw) else "Invalid"),
            (
                "CRC-16",
                (
                    "—"
                    if received_crc is None or calculated_crc is None
                    else f"received 0x{received_crc:04X}; calculated 0x{calculated_crc:04X}"
                ),
            ),
            (
                "Response time",
                "—" if response_time is None else f"{float(response_time):.3f} ms",
            ),
            ("Decoder note", packet.description),
        ]
        self._fill_property_table(self.summary_table, summary)

        details = [
            ("Unix timestamp", f"{packet.timestamp:.6f}"),
            ("Direction", packet.direction),
            ("Slave / Unit ID", str(packet.slave_id)),
            ("Function code", str(packet.function_code)),
            ("Function name", packet.function_name),
            ("Frame type", packet.frame_type),
            ("Start address", self._display(packet.address)),
            ("Quantity", self._display(packet.quantity)),
            ("Description", packet.description),
            ("Raw bytes", repr(packet.raw)),
            ("Raw hexadecimal", packet.raw_hex),
            ("PDU hexadecimal", packet.raw[1:-2].hex(" ").upper()),
            ("Data hexadecimal", packet.raw[2:-2].hex(" ").upper() or "—"),
            (
                "CRC wire bytes",
                packet.raw[-2:].hex(" ").upper() if len(packet.raw) >= 2 else "—",
            ),
            ("CRC valid", str(crc_is_valid(packet.raw))),
        ]
        request_timestamp = getattr(packet, "request_timestamp", None)
        if request_timestamp is not None:
            details.append(("Matched request timestamp", f"{request_timestamp:.6f}"))
        exception_code = getattr(packet, "exception_code", None)
        if exception_code is not None:
            details.append(
                ("Modbus exception code", f"{exception_code} (0x{exception_code:02X})")
            )
        self._fill_property_table(self.full_details_table, details)
        self._fill_byte_table(packet)

        # Wait until Qt has applied column widths, then calculate wrapped row heights.
        QTimer.singleShot(0, self._fit_all_tables)

        self.show_all_btn.setEnabled(True)
        self.help_label.setText(
            f"Inspecting {packet.frame_type.lower()} from slave {packet.slave_id}, "
            f"function {packet.function_code}."
        )

    @staticmethod
    def _display(value) -> str:
        return "—" if value is None else str(value)

    @staticmethod
    def _fill_property_table(table: QTableWidget, rows: list[tuple[str, str]]) -> None:
        table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            name_item = QTableWidgetItem(name)
            value_item = QTableWidgetItem(str(value))
            value_item.setToolTip(str(value))
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, value_item)

    def _fill_byte_table(self, packet: CapturedModbusFrame) -> None:
        meanings = self._byte_meanings(packet)
        self.byte_table.setRowCount(len(packet.raw))
        for row, value in enumerate(packet.raw):
            cells = (
                str(row),
                f"0x{value:02X}",
                str(value),
                f"{value:08b}",
                meanings.get(row, "Data / payload byte"),
            )
            for column, text in enumerate(cells):
                self.byte_table.setItem(row, column, QTableWidgetItem(text))

    @staticmethod
    def _byte_meanings(packet: CapturedModbusFrame) -> dict[int, str]:
        length = len(packet.raw)
        meanings: dict[int, str] = {}
        if length >= 1:
            meanings[0] = "Slave / unit identifier"
        if length >= 2:
            exception_label = " with exception flag" if packet.raw[1] & 0x80 else ""
            meanings[1] = f"Function code{exception_label}"
        if length >= 2:
            meanings[length - 2] = "CRC-16 low byte (wire order)"
            meanings[length - 1] = "CRC-16 high byte (wire order)"

        function = packet.function_code
        if packet.frame_type == "Exception response" and length >= 5:
            meanings[2] = "Modbus exception code"
        elif packet.frame_type == "Request":
            if function in (1, 2, 3, 4, 5, 6) and length >= 8:
                meanings[2] = "Start address high byte"
                meanings[3] = "Start address low byte"
                label = "Value" if function in (5, 6) else "Quantity"
                meanings[4] = f"{label} high byte"
                meanings[5] = f"{label} low byte"
            elif function in (15, 16) and length >= 9:
                meanings.update(
                    {
                        2: "Start address high byte",
                        3: "Start address low byte",
                        4: "Quantity high byte",
                        5: "Quantity low byte",
                        6: "Following data byte count",
                    }
                )
        elif packet.frame_type == "Response":
            if function in (1, 2, 3, 4) and length >= 5:
                meanings[2] = "Following data byte count"
            elif function in (5, 6, 15, 16) and length >= 8:
                meanings[2] = "Echoed address high byte"
                meanings[3] = "Echoed address low byte"
                meanings[4] = "Echoed value / quantity high byte"
                meanings[5] = "Echoed value / quantity low byte"
        return meanings

    def _toggle_all_information(self, checked: bool) -> None:
        self.all_information_group.setVisible(checked)
        self.show_all_btn.setText(
            "Show Important Information Only"
            if checked
            else "Show All Packet Information"
        )

        # Showing a hidden widget changes available widths and wrapped row heights.
        QTimer.singleShot(0, self._fit_all_tables)