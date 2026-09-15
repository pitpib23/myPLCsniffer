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
from plcsniffer.ui.responsive import METRICS, ResponsiveMode, apply_layout_spacing

# Compact/ultra-compact floors for byte_table, well below
# INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT's normal-mode floor — this table is
# now QSizePolicy.Expanding (see _build_ui), so these only matter as an
# absolute lower bound; tests/test_architecture.py pins the NORMAL-mode
# floor at >=300, which INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT alone satisfies.
_BYTE_TABLE_MINIMUM_HEIGHT_BY_MODE = {
    ResponsiveMode.NORMAL: INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT,
    ResponsiveMode.COMPACT: 220,
    ResponsiveMode.ULTRA_COMPACT: 150,
}
_SUMMARY_TABLE_MINIMUM_HEIGHT_BY_MODE = {
    ResponsiveMode.NORMAL: INSPECTOR_SUMMARY_MINIMUM_HEIGHT,
    ResponsiveMode.COMPACT: 220,
    ResponsiveMode.ULTRA_COMPACT: 160,
}


class PacketInspectorWidget(QWidget):
    """Show a concise packet summary with optional byte-level information."""

    def __init__(self, parent=None, *, lite: bool = False) -> None:
        super().__init__(parent)
        self._lite = lite
        self.current_packet: CapturedModbusFrame | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        # This widget is intended to live inside the tab's QScrollArea, but
        # only as a last-resort fallback now: byte_table below is
        # Expanding+stretch, so it's the one that actually claims whatever
        # space this tab is given, shrinking to its own floor (and falling
        # back to its own internal scrollbar for extra rows) well before the
        # outer QScrollArea would ever need to engage.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        self._root_layout = layout

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
        # Important information, so it stays exactly the height its content
        # needs (see _fit_table_to_rows) rather than competing for space —
        # byte_table is the one that gives ground on a short screen.
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
        if self._lite:
            self.show_all_btn.setMinimumHeight(44)
        self.show_all_btn.toggled.connect(self._toggle_all_information)
        summary_layout.addWidget(self.show_all_btn)

        self.summary_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.summary_group)

        # Expanded information appears directly below the button/first
        # group. Secondary/detailed info (per the task's stated priority),
        # so this group — specifically byte_table inside it — is the one
        # that actually expands into whatever space remains: the group
        # itself must be Expanding too, not just byte_table, since a Fixed-
        # policy parent would clamp to its children's natural height
        # regardless of any stretch factor given to it below.
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
        # Keep the header visible by scrolling inside this table. Lite
        # additionally enables the horizontal scrollbar (see the Meaning
        # column below, which no longer stretches to fill whatever width
        # is left in that mode) — the full edition keeps it off, unchanged.
        self.byte_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.byte_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if self._lite else Qt.ScrollBarAlwaysOff
        )
        self.byte_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        if self._lite:
            self.byte_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.byte_table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        # Expanding in both directions (was Expanding/Fixed with a
        # setFixedHeight computed for up to 16 rows): this table now claims
        # whatever vertical space the tab actually has, growing past 16
        # rows on a tall window and shrinking — using its own internal
        # scrollbar for whatever doesn't fit — on a short one, rather than
        # always reserving room for exactly 16 regardless of the window.
        self.byte_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.byte_table.setMinimumHeight(INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT)

        byte_header = self.byte_table.horizontalHeader()
        for column in range(4):
            byte_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        if self._lite:
            # Meaning is Interactive with a readable floor width rather
            # than plain Stretch — on a narrow screen, Stretch would
            # compress it down to whatever's left over (or unreadably
            # narrow once the tab is itself narrower than the other four
            # columns need); scroll, don't shrink, is what the horizontal
            # scrollbar enabled above is for. setStretchLastSection still
            # lets it grow to fill genuine extra width on a wide/desktop
            # window, it just never shrinks it below the floor set here.
            byte_header.setSectionResizeMode(4, QHeaderView.Interactive)
            self.byte_table.setColumnWidth(4, 260)
            byte_header.setStretchLastSection(True)
        else:
            byte_header.setSectionResizeMode(4, QHeaderView.Stretch)
        all_layout.addWidget(self.byte_table, stretch=1)

        self.all_information_group.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self.all_information_group.setVisible(False)
        layout.addWidget(self.all_information_group, stretch=1)

    def apply_responsive_mode(self, mode: ResponsiveMode) -> None:
        """Densify margins/spacing and lower the idle-state table floors.

        summary_table/full_details_table stay exactly fit to their content
        (see _fit_table_to_rows) at every mode — that's already
        right-sized, not oversized. byte_table's floor is what actually
        shrinks, since it's the Expanding one that's meant to give ground.
        """
        metrics = METRICS[mode]
        margin = metrics.layout_margin
        self._root_layout.setContentsMargins(margin, margin, margin, margin)
        # Reaches summary_layout and all_layout too — both are owned by a
        # QGroupBox reachable from this root layout.
        apply_layout_spacing(self._root_layout, metrics.layout_spacing)

        self.summary_table.setMinimumHeight(_SUMMARY_TABLE_MINIMUM_HEIGHT_BY_MODE[mode])
        self.byte_table.setMinimumHeight(_BYTE_TABLE_MINIMUM_HEIGHT_BY_MODE[mode])

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

    def _fit_all_tables(self) -> None:
        self._fit_table_to_rows(self.summary_table)
        self._fit_table_to_rows(self.full_details_table)
        # byte_table is Expanding+stretch (see _build_ui), so it claims
        # whatever space the layout actually gives it rather than being
        # fixed to a row count — only its per-row heights need recomputing
        # here, for wrapped "Meaning" cell text.
        self.byte_table.resizeRowsToContents()

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