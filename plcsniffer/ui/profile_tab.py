from __future__ import annotations

import copy
import json
import logging
import struct
import uuid

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScroller,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from plcsniffer.capture import PassiveCaptureService
from plcsniffer.config import SERIAL_SHUTDOWN_TIMEOUT_MS, SerialSettings
from plcsniffer.exceptions import ConfigurationError
from plcsniffer.logging_config import application_data_directory, log_event
from plcsniffer.modbus import (
    CapturedModbusFrame,
    FUNCTION_NAMES,
    MODICON_BLOCK_OFFSET,
)
from plcsniffer.ui.responsive import METRICS, ResponsiveMode, apply_layout_spacing
from plcsniffer.ui.style import STATUS_STYLES
from plcsniffer.validation import validate_register_address

# sniff_port_combo's minimum width per density — see _build_sniff_toolbar.
_SNIFF_PORT_MINIMUM_WIDTH_BY_MODE = {
    ResponsiveMode.NORMAL: 350,
    ResponsiveMode.COMPACT: 220,
    ResponsiveMode.ULTRA_COMPACT: 180,
}

# Profile Configuration's parity_combo (see _build_config_form) stores the
# full word ("None"/"Even"/"Odd") rather than the single-letter code
# SerialSettings/pyserial expect ("N"/"E"/"O", per config.VALID_PARITIES).
# Only used when opening a serial port for this profile's own sniffing
# worker — the stored profile value itself is left exactly as-is.
_PROFILE_PARITY_TO_SERIAL_CODE = {"None": "N", "Even": "E", "Odd": "O"}

# register_table column indices; kept as named constants since
# _insert_register_row and several _refresh_* helpers all need to agree on
# them. One shared table holds every slave ID in the current PLC profile at
# once (see the Slave ID column and slave_filter), so Slave ID sits right
# after Name — the same position "which slave does this row belong to"
# would occupy in a vendor Modbus map that covers multiple units.
_SLAVE_ID_COLUMN = 1
_FUNCTION_CODE_COLUMN = 2
_FORMAT_COLUMN = 3
_BYTE_ORDER_COLUMN = 4
_REGISTER_COLUMN = 5
_TIMESTAMP_COLUMN = 11
_STATUS_COLUMN = 12

# Format names/word counts, matching the vocabulary and register-count
# convention used in vendor Modbus maps (e.g. Entes MPR-3/4 series: Address |
# Format | Word Counts | Unit | Remarks | Multiplier). Word count is fixed
# per format rather than a separately editable field — each entry is
# (data_type, word_count, display label).
#
# This set matches what the major Modbus/SCADA tools actually expose (Kepware
# KEPServerEX, Modbus Poll, Ignition's Advanced Modbus module): 16/32-bit
# signed+unsigned+float, and 64-bit unsigned+signed+double. Deliberately
# excludes wider/non-standard types — e.g. an 80-bit "long double" never
# appears in any of those tools' type lists (it's a C/x87-FPU concept with no
# defined wire format, not part of IEEE-754, and nothing in the Modbus
# ecosystem transmits it).
_FORMAT_OPTIONS: tuple[tuple[str, int, str], ...] = (
    ("uint16", 1, "16-bit Unsigned"),
    ("int16", 1, "16-bit Signed"),
    ("uint32", 2, "32-bit Unsigned"),
    ("int32", 2, "32-bit Signed"),
    ("float32", 2, "32-bit Float"),
    ("uint64", 4, "64-bit Unsigned"),
    ("int64", 4, "64-bit Signed"),
    ("float64", 4, "64-bit Float"),
)
_FORMAT_WORD_COUNTS: dict[str, int] = {
    data_type: word_count for data_type, word_count, _ in _FORMAT_OPTIONS
}

# The four standard Modbus multi-register byte orders (the same naming used
# across SCADA/HMI tag configuration, e.g. Kepware/Ignition): word order
# (which register holds the most-significant half) and, independently,
# whether each individual register's own two bytes are swapped. Only
# meaningful — and only applied — when a Format spans more than one
# register; a single 16-bit register's bytes are always big-endian per the
# Modbus RTU wire format itself (see modbus.py), so there's nothing to
# choose there.
#
# Which byte orders are even meaningful depends on how many registers the
# Format spans, so this is keyed by word count rather than being one flat
# list — a single register only has two possible byte arrangements, while
# four registers have five distinct named ones. Each entry is
# (operation, official standard name, example letter mapping showing which
# byte of a sequential A B C D... run ends up where). The same operation can
# have a different official name at a different width — e.g. "byte_swapped"
# is officially "Byte-Swapped" at 32-bit but "Byte-and-Word Swapped" at
# 64-bit — so the label always comes from this per-width table, never from
# the operation key alone.
_BYTE_ORDER_OPTIONS_BY_WORD_COUNT: dict[int, tuple[tuple[str, str, str], ...]] = {
    1: (
        ("big_endian", "Big-Endian", "A B"),
        ("little_endian", "Little-Endian", "B A"),
    ),
    2: (
        ("big_endian", "Big-Endian", "A B C D"),
        ("little_endian", "Little-Endian", "D C B A"),
        ("word_swapped", "Word-Swapped", "C D A B"),
        ("byte_swapped", "Byte-Swapped", "B A D C"),
    ),
    4: (
        ("big_endian", "Big-Endian", "A B C D E F G H"),
        ("little_endian", "Little-Endian", "H G F E D C B A"),
        ("word_swapped", "Word-Swapped", "G H E F C D A B"),
        ("double_word_swapped", "Double-Word Swapped", "E F G H A B C D"),
        ("byte_swapped", "Byte-and-Word Swapped", "B A D C F E H G"),
    ),
}

# Combining registers into one wider value only makes sense for function
# codes that actually read/write 16-bit registers. Coil-oriented codes
# (1, 2, 5, 15) carry single-bit values, so combining them would just
# produce a meaningless number — _apply_frame_to_registers skips the
# combination rather than doing that silently.
_SIXTEEN_BIT_REGISTER_FUNCTION_CODES = frozenset({3, 4, 6, 16})


class FunctionCodeDelegate(QStyledItemDelegate):
    """Combo-box editor for the register table's Function Code column.

    Replaces free-typed entry ("3" / "0x03" / the full display string) with
    picking a named function from a list — no legend needed to know what
    values are valid. Options come from modbus.FUNCTION_NAMES; the display
    text and the text committed back to the cell reuse
    ProfileTab._function_code_display() exactly, so
    ProfileTab._parse_function_code() (used by _on_cell_changed) keeps
    working completely unchanged.
    """

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(False)
        for code, name in sorted(FUNCTION_NAMES.items()):
            combo.addItem(f"0x{code:02X} {name}", code)
        # Combo-box editors don't auto-commit on selection the way a line
        # edit commits on focus-out, so wire it explicitly.
        combo.activated.connect(lambda _index, c=combo: self._commit_and_close(c))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index) -> None:
        text = index.data(Qt.EditRole) or ""
        code = ProfileTab._parse_function_code(text)
        editor.setCurrentIndex(editor.findData(code))

    def setModelData(self, editor: QComboBox, model, index) -> None:
        code = editor.currentData()
        model.setData(index, ProfileTab._function_code_display(code), Qt.EditRole)


class FormatDelegate(QStyledItemDelegate):
    """Combo-box editor for the register table's Format column.

    Same pattern as FunctionCodeDelegate: options come from the module-level
    _FORMAT_OPTIONS, and the display text round-trips through
    ProfileTab._format_display()/_parse_format_display() so
    _on_cell_changed doesn't need to know about this delegate at all.
    """

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(False)
        for data_type, _word_count, label in _FORMAT_OPTIONS:
            combo.addItem(label, data_type)
        combo.activated.connect(lambda _index, c=combo: self._commit_and_close(c))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index) -> None:
        text = index.data(Qt.EditRole) or ""
        data_type = ProfileTab._parse_format_display(text)
        found = editor.findData(data_type)
        editor.setCurrentIndex(found if found >= 0 else 0)

    def setModelData(self, editor: QComboBox, model, index) -> None:
        data_type = editor.currentData()
        model.setData(index, ProfileTab._format_display(data_type), Qt.EditRole)


class ByteOrderDelegate(QStyledItemDelegate):
    """Combo-box editor for the register table's Byte Order column.

    Which options are even offered depends on that row's own Format — a
    16-bit register only has two meaningful byte orders, four registers have
    five — so createEditor reads the Format cell in the same row to build
    the right list, via ProfileTab._byte_order_options_for_word_count().
    Each item's tooltip (Qt.ToolTipRole) shows its example letter mapping,
    so hovering an option in the open dropdown shows how it reorders bytes.
    """

    def createEditor(self, parent, option, index):
        word_count = ProfileTab._word_count_for_row(index.model(), index.row())
        combo = QComboBox(parent)
        combo.setEditable(False)
        for byte_order, label, letters in ProfileTab._byte_order_options_for_word_count(
            word_count
        ):
            combo.addItem(label, byte_order)
            combo.setItemData(combo.count() - 1, letters, Qt.ToolTipRole)
        combo.activated.connect(lambda _index, c=combo: self._commit_and_close(c))
        return combo

    def _commit_and_close(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor: QComboBox, index) -> None:
        text = index.data(Qt.EditRole) or ""
        word_count = ProfileTab._word_count_for_row(index.model(), index.row())
        byte_order = ProfileTab._parse_byte_order_display(text, word_count)
        found = editor.findData(byte_order)
        editor.setCurrentIndex(found if found >= 0 else 0)

    def setModelData(self, editor: QComboBox, model, index) -> None:
        byte_order = editor.currentData()
        word_count = ProfileTab._word_count_for_row(index.model(), index.row())
        model.setData(
            index, ProfileTab._byte_order_display(byte_order, word_count), Qt.EditRole
        )


class RegisterAddressDelegate(QStyledItemDelegate):
    """Plain-text editor for the Register column.

    The cell *displays* a range (e.g. "48-49") once its row's Format spans
    more than one register, but the only thing actually being edited is the
    starting address. Without this, double-clicking a multi-register row
    would pre-fill the editor with the whole range string, and committing it
    unchanged would fail to parse as an address (see ProfileTab._parse_int).
    Uses the default QLineEdit editor — only the text handed to it on open
    needs correcting.
    """

    def setEditorData(self, editor, index) -> None:
        text = index.data(Qt.EditRole) or ""
        leading = text.split("-", 1)[0].strip()
        editor.setText(leading)


class ProfileTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Same writable-location logic logging_config.py already uses for the
        # log file: project root during development, but a proper per-user
        # app-data directory (%LOCALAPPDATA%\myPLCsniffer, or ~/.myPLCsniffer
        # as a fallback) once packaged — a frozen build's own install
        # directory is often read-only, so saved profiles need to live
        # somewhere else to actually persist between runs.
        self.profile_json_path = application_data_directory() / "profile.json"
        self.profiles: list[dict] = []
        self.current_profile_id: str | None = None
        self._ignore_changes = False
        self._latest_frame: CapturedModbusFrame | None = None

        # register_table is one shared table for every slave ID in the
        # current profile (see the Slave ID column) rather than one table
        # per slave, so these two lists are what map each table row back to
        # the underlying (slave dict, register dict) it displays — kept in
        # lockstep with register_table's rows by _insert_register_row/
        # remove_register/_populate_register_table. Both dicts are the
        # actual objects living inside self.profiles, not copies, so
        # mutating register[...] or moving a register between slaves here
        # is exactly the same as mutating the saved profile.
        self._row_slaves: list[dict] = []
        self._row_registers: list[dict] = []

        # Editing workflow state: profiles open read-only; "Edit Profile"
        # unlocks the form and stashes a snapshot so "Discard Changes" can
        # restore it. Nothing is written to disk until "Save Profile".
        self._editing = False
        self._edit_snapshot: dict | None = None

        # Splitter proportions to restore when the sidebar is reopened after
        # being hidden via _toggle_nav_panel(); see that method.
        self._nav_panel_sizes: list[int] | None = None
        # Tracked explicitly rather than read back via left_panel.isVisible()
        # — that reports False for a widget that has simply never been
        # shown yet (e.g. during __init__, before the window is visible),
        # which would misfire the very first _set_sidebar_visible() call.
        self._sidebar_visible = True
        # Once True (the user has manually clicked Hide/Show Profiles even
        # once), apply_responsive_mode() never touches the sidebar again —
        # a density-driven auto-collapse must never fight a choice the user
        # already made explicitly.
        self._sidebar_user_overridden = False

        # Independent receive-only capture for this tab's own Start/Stop
        # Passive Sniffing button. Reuses the exact same QThread worker,
        # CRC/frame decoding, and cleanup lifecycle as the Passive Sniffing
        # tab (see PassiveCaptureService) — this is a second, separate
        # instance so this tab can sniff on its own port/profile settings
        # independently of whatever Tab 1 is doing.
        self.sniff_capture_service = PassiveCaptureService(self)

        self._build_ui()
        self._connect_sniff_service()
        self.load_profiles_from_json()
        self._refresh_profile_list()
        self._set_active_profile(self.current_profile_id)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(self.splitter)

        self._build_left_panel()
        self._build_right_panel()

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)

    def apply_responsive_mode(self, mode: ResponsiveMode) -> None:
        """Densify both splitter panels and auto-collapse the sidebar.

        register_table already has stretch=1 in the right panel (see
        _build_table), so — same as the other tabs — shrinking the chrome
        above it (and, here, the sidebar's own share of the splitter) is
        what actually grows its share of a short or narrow window.
        """
        metrics = METRICS[mode]
        left_margin = metrics.layout_margin
        self._left_layout.setContentsMargins(
            left_margin, left_margin, left_margin, left_margin
        )
        apply_layout_spacing(self._left_layout, metrics.layout_spacing)

        # Right panel's top/bottom margin was always a little tighter than
        # its left/right (10, 8, 10, 8) — same ratio, scaled with density.
        right_horizontal = metrics.layout_margin
        right_vertical = max(metrics.layout_margin - 2, 0)
        self._right_layout.setContentsMargins(
            right_horizontal, right_vertical, right_horizontal, right_vertical
        )
        apply_layout_spacing(self._right_layout, metrics.layout_spacing)

        self.sniff_port_combo.setMinimumWidth(
            _SNIFF_PORT_MINIMUM_WIDTH_BY_MODE[mode]
        )
        self.register_table.verticalHeader().setDefaultSectionSize(
            metrics.table_row_height
        )

        # Give the register table more width on a narrow/short window by
        # collapsing the profile-list sidebar — but only ever as a
        # reversible *default*. The moment the user has manually toggled it
        # even once, _sidebar_user_overridden latches permanently and this
        # branch never touches it again.
        if not self._sidebar_user_overridden:
            self._set_sidebar_visible(
                mode is ResponsiveMode.NORMAL, user_initiated=False
            )

    def _toggle_nav_panel(self) -> None:
        """Hide or restore the profile-list sidebar (the button's click handler).

        Marks the choice as user-initiated, so apply_responsive_mode() will
        never override it again for the rest of this tab's life — see
        _set_sidebar_visible().
        """
        self._set_sidebar_visible(not self._sidebar_visible, user_initiated=True)

    def _set_sidebar_visible(self, visible: bool, *, user_initiated: bool) -> None:
        """Shared implementation for both the manual toggle and density-driven auto-collapse.

        Hiding it makes the splitter give its space straight to the summary
        and register table. The toggle button lives in the right panel (see
        _build_edit_toolbar) so it stays reachable even while the sidebar is
        fully collapsed to zero width.
        """
        if visible == self._sidebar_visible:
            return
        self._sidebar_visible = visible
        if user_initiated:
            self._sidebar_user_overridden = True
        if visible:
            self.left_panel.setVisible(True)
            if self._nav_panel_sizes:
                self.splitter.setSizes(self._nav_panel_sizes)
            self.toggle_nav_btn.setText("« Hide Profiles")
            self.toggle_nav_btn.setToolTip(
                "Hide the profile list to give the summary more room"
            )
        else:
            self._nav_panel_sizes = self.splitter.sizes()
            self.left_panel.setVisible(False)
            self.toggle_nav_btn.setText("» Show Profiles")
            self.toggle_nav_btn.setToolTip("Show the profile list")

    def _build_left_panel(self) -> None:
        # Styled as a distinct nav sidebar (white card, right border, accented
        # selection) rather than a plain list box, and paired with the toggle
        # button in the toolbar so it can be fully collapsed to give the
        # summary/table below more room. See _toggle_nav_panel().
        self.left_panel = QWidget()
        self.left_panel.setObjectName("profileNavPanel")
        self.left_panel.setStyleSheet(
            """
            #profileNavPanel {
                background: #ffffff;
                border-right: 1px solid #cbd5e1;
            }
            """
        )
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(8)
        self._left_layout = left_layout

        title = QLabel("Profiles")
        title.setStyleSheet("font-size: 11pt; font-weight: 700; color: #1f2937;")
        left_layout.addWidget(title)

        button_layout = QHBoxLayout()
        self.add_profile_btn = QPushButton("Add")
        self.add_profile_btn.setToolTip("Add a new profile")
        self.add_profile_btn.setMinimumHeight(44)
        self.add_profile_btn.clicked.connect(self.add_profile)
        self.remove_profile_btn = QPushButton("Remove")
        self.remove_profile_btn.setToolTip("Remove the selected profile")
        self.remove_profile_btn.setMinimumHeight(44)
        self.remove_profile_btn.clicked.connect(self.remove_profile)
        button_layout.addWidget(self.add_profile_btn)
        button_layout.addWidget(self.remove_profile_btn)
        left_layout.addLayout(button_layout)

        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._on_profile_selected)
        self.profile_list.setAlternatingRowColors(True)
        self.profile_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.profile_list.setMinimumWidth(210)
        self.profile_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.profile_list.setSpacing(2)
        self.profile_list.setStyleSheet(
            """
            QListWidget {
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                background: #ffffff;
                outline: none;
            }
            QListWidget::item {
                padding: 7px 8px;
                border-radius: 3px;
                color: #334155;
            }
            QListWidget::item:hover:!selected {
                background: #eef2f7;
            }
            QListWidget::item:selected {
                background: #dbeafe;
                color: #174ea6;
                font-weight: 600;
            }
            """
        )
        left_layout.addWidget(self.profile_list)

    def _build_right_panel(self) -> None:
        self.right_panel = QWidget()
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(10, 8, 10, 8)
        right_layout.setSpacing(6)
        self._right_layout = right_layout

        self._build_edit_toolbar(right_layout)
        self._build_name_row(right_layout)
        self._build_config_form(right_layout)
        self._build_register_controls_row(right_layout)
        self._build_sniff_toolbar(right_layout)
        self._build_table(right_layout)

    def _build_edit_toolbar(self, parent_layout: QVBoxLayout) -> None:
        row = QHBoxLayout()

        # Lives in the right panel (not the sidebar itself) so it stays
        # reachable after the sidebar is fully collapsed — the only way to
        # bring it back otherwise would be to drag a zero-width splitter
        # handle, which isn't discoverable.
        self.toggle_nav_btn = QPushButton("« Hide Profiles")
        self.toggle_nav_btn.setToolTip("Hide the profile list to give the summary more room")
        self.toggle_nav_btn.clicked.connect(self._toggle_nav_panel)
        row.addWidget(self.toggle_nav_btn)

        row.addStretch()

        self.edit_profile_btn = QPushButton("Edit Profile")
        self.edit_profile_btn.setMinimumHeight(44)
        self.edit_profile_btn.clicked.connect(self.enter_edit_mode)
        row.addWidget(self.edit_profile_btn)

        self.save_profile_btn = QPushButton("Save Profile")
        self.save_profile_btn.setProperty("role", "primary")
        self.save_profile_btn.setMinimumHeight(44)
        self.save_profile_btn.clicked.connect(self.save_and_exit_edit_mode)
        row.addWidget(self.save_profile_btn)

        self.discard_changes_btn = QPushButton("Discard Changes")
        self.discard_changes_btn.setMinimumHeight(44)
        self.discard_changes_btn.clicked.connect(self.discard_changes)
        row.addWidget(self.discard_changes_btn)

        parent_layout.addLayout(row)

        self.edit_status_label = QLabel("Select a profile to view its details.")
        self.edit_status_label.setWordWrap(True)
        self.edit_status_label.setStyleSheet(STATUS_STYLES["info"])
        parent_layout.addWidget(self.edit_status_label)

    def _build_name_row(self, parent_layout: QVBoxLayout) -> None:
        name_row = QHBoxLayout()
        name_label = QLabel("Profile name")
        name_label.setStyleSheet("font-weight: 600;")
        name_row.addWidget(name_label)
        self.profile_name_edit = QLineEdit()
        self.profile_name_edit.setPlaceholderText("Profile name")
        self.profile_name_edit.textEdited.connect(self._on_profile_name_changed)
        name_row.addWidget(self.profile_name_edit, 1)
        parent_layout.addLayout(name_row)

    def _build_config_form(self, parent_layout: QVBoxLayout) -> None:
        # Baudrate/parity/stop bits apply to the whole capture (and so the
        # whole PLC profile) — Slave ID moved out to _build_slave_row(),
        # since a profile can now hold more than one.
        self.config_group = QGroupBox("Profile Configuration")
        config_layout = QGridLayout(self.config_group)
        for field_column in (1, 3, 5):
            config_layout.setColumnStretch(field_column, 1)

        self.baudrate_combo = QComboBox()
        for rate in (9600, 19200, 38400, 57600, 115200):
            self.baudrate_combo.addItem(str(rate), rate)
        self.baudrate_combo.currentIndexChanged.connect(self._on_config_field_changed)

        self.parity_combo = QComboBox()
        self.parity_combo.addItem("None", "None")
        self.parity_combo.addItem("Even", "Even")
        self.parity_combo.addItem("Odd", "Odd")
        self.parity_combo.currentIndexChanged.connect(self._on_config_field_changed)

        self.stop_bits_combo = QComboBox()
        self.stop_bits_combo.addItem("1", 1.0)
        self.stop_bits_combo.addItem("1.5", 1.5)
        self.stop_bits_combo.addItem("2", 2.0)
        self.stop_bits_combo.currentIndexChanged.connect(self._on_config_field_changed)

        # One row: all three fields fit comfortably at the tab's minimum
        # width and this alone saves vertical space versus stacking them,
        # leaving more room for the register table below.
        config_layout.addWidget(QLabel("Baudrate"), 0, 0)
        config_layout.addWidget(self.baudrate_combo, 0, 1)
        config_layout.addWidget(QLabel("Parity"), 0, 2)
        config_layout.addWidget(self.parity_combo, 0, 3)
        config_layout.addWidget(QLabel("Stop Bits"), 0, 4)
        config_layout.addWidget(self.stop_bits_combo, 0, 5)

        self.config_group.setEnabled(False)
        parent_layout.addWidget(self.config_group)

        # Styled as a card (matching the Packet Inspector tab's help banner)
        # rather than plain text, so it stays legible as the focal point of
        # this tab once the profile-list sidebar is hidden.
        self.profile_summary_label = QLabel("")
        self.profile_summary_label.setWordWrap(True)
        self.profile_summary_label.setStyleSheet(
            "padding: 9px; background: #eef5ff; border: 1px solid #b8d4f5;"
            "border-radius: 4px; color: #1f2937; font-weight: 600;"
        )
        parent_layout.addWidget(self.profile_summary_label)

    def _build_register_controls_row(self, parent_layout: QVBoxLayout) -> None:
        # No free-text "Search registers..." field on Lite — same reasoning
        # as Passive Sniffing's dropped search box (on-screen-keyboard
        # interaction is poor for field use). Slave/Function dropdowns need
        # no typing, so they're kept — see _apply_filter()/reset_filters(),
        # which no longer reference any text query.
        row = QHBoxLayout()

        # One PLC profile may contain several Modbus slave IDs (see
        # passive_capture.PassiveSniffingWidget.current_profile_data) — all
        # of them share this one register table (see the Slave ID column in
        # _build_table), and this filter is what narrows the shared table
        # down to a single slave's own rows. Its options are rebuilt from
        # the loaded profile's own slaves — see _refresh_slave_filter_options.
        self.slave_filter = QComboBox()
        self.slave_filter.addItem("All slaves", None)
        self.slave_filter.currentIndexChanged.connect(self._apply_filter)
        self.slave_filter.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.slave_filter.setMinimumContentsLength(10)
        row.addWidget(self.slave_filter, 1)

        self.function_code_filter = QComboBox()
        self.function_code_filter.addItem("All functions", None)
        for code, name in sorted(FUNCTION_NAMES.items()):
            self.function_code_filter.addItem(f"{code:02d} — {name}", code)
        self.function_code_filter.currentIndexChanged.connect(self._apply_filter)
        # Left at its default size policy, this combo reserves enough width
        # for its single longest item ("16 — Write Multiple Registers"),
        # which alone was over 400px — sizing from a character count instead
        # keeps it compact on a small screen; the dropdown list is unaffected.
        self.function_code_filter.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.function_code_filter.setMinimumContentsLength(14)
        row.addWidget(self.function_code_filter, 1)

        self.reset_filters_btn = QPushButton("Reset Filters")
        self.reset_filters_btn.clicked.connect(self.reset_filters)
        row.addWidget(self.reset_filters_btn)

        row.addSpacing(16)

        self.add_register_btn = QPushButton("Add Register")
        self.add_register_btn.setMinimumHeight(44)
        self.add_register_btn.clicked.connect(self.add_register)
        row.addWidget(self.add_register_btn)

        self.remove_register_btn = QPushButton("Remove Register")
        self.remove_register_btn.setMinimumHeight(44)
        self.remove_register_btn.clicked.connect(self.remove_register)
        row.addWidget(self.remove_register_btn)

        parent_layout.addLayout(row)

    def _build_sniff_toolbar(self, parent_layout: QVBoxLayout) -> None:
        """Independent receive-only Start/Stop controls for this profile.

        Mirrors the Start/Stop lifecycle on the Passive Sniffing tab (same
        PassiveCaptureService class, same receive-only guarantee — this
        never transmits) but opens its own serial connection using this
        profile's own slave ID/baud/parity/stop bits and only ever updates
        the rows already defined by the selected profile. See
        _apply_frame_to_registers(): the table is a live view of the
        profile's own register list, not a log of every frame observed.
        """
        row = QHBoxLayout()
        row.addWidget(QLabel("Sniff port"))
        self.sniff_port_combo = QComboBox()
        self.sniff_port_combo.setEditable(True)
        self.sniff_port_combo.setMinimumWidth(350)
        row.addWidget(self.sniff_port_combo)

        self.sniff_refresh_btn = QPushButton("Refresh Ports")
        self.sniff_refresh_btn.clicked.connect(self._refresh_sniff_ports)
        row.addWidget(self.sniff_refresh_btn)

        self.sniff_toggle_btn = QPushButton("Start Passive Sniffing")
        self.sniff_toggle_btn.setProperty("role", "primary")
        # Matches Tab 1's start_btn height exactly — same recurring action
        # (Start/Stop Passive Sniffing) gets the same visual weight on both
        # tabs, and both meet Lite's ~44px touch-target floor.
        self.sniff_toggle_btn.setMinimumHeight(44)
        self.sniff_toggle_btn.clicked.connect(self.toggle_sniffing)
        row.addWidget(self.sniff_toggle_btn)
        row.addStretch()
        parent_layout.addLayout(row)

        self.sniff_status_label = QLabel(
            "Not sniffing. Select a profile with registers, choose a port, and click Start."
        )
        self.sniff_status_label.setWordWrap(True)
        self.sniff_status_label.setStyleSheet(STATUS_STYLES["info"])
        parent_layout.addWidget(self.sniff_status_label)

        self._refresh_sniff_ports()

    def _build_table(self, parent_layout: QVBoxLayout) -> None:
        # One shared table for every slave ID in the profile — see the
        # Slave ID column and slave_filter (_build_register_controls_row)
        # rather than a separate table/tab/sidebar per slave, so the
        # existing layout stays exactly this compact regardless of how many
        # slave IDs one PLC profile contains.
        self.register_table = QTableWidget(0, 13)
        self.register_table.setHorizontalHeaderLabels(
            [
                "Name",
                "Slave ID",
                "Function Code",
                "Format",
                "Byte Order",
                "Register",
                "Raw Hex Value",
                "Multiplier",
                "Parsed Value",
                "Unit",
                "Description",
                "Timestamp",
                "Status",
            ]
        )
        self.register_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.register_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.register_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.register_table.setAlternatingRowColors(True)
        # Description stretches to fill remaining space; the two
        # sniffing-status columns appended after it keep fixed widths so
        # adding them doesn't silently move the stretch to the new last
        # column and squeeze Description down to its default width.
        header = self.register_table.horizontalHeader()
        header.setSectionResizeMode(10, QHeaderView.Stretch)
        self.register_table.verticalHeader().setVisible(False)
        self.register_table.setWordWrap(False)
        # Lite's table strategy is scroll-not-shrink: all 13 columns keep
        # their existing readable widths (below) rather than being hidden
        # or compressed to fit 800px — a finger swipe reaches the rest.
        # QScroller's TouchGesture only engages for actual touch input, so
        # mouse-driven cell selection/editing during desktop development is
        # unaffected.
        self.register_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.register_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.register_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.register_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        QScroller.grabGesture(
            self.register_table.viewport(), QScroller.TouchGesture
        )
        self.register_table.setColumnWidth(0, 150)
        self.register_table.setColumnWidth(_SLAVE_ID_COLUMN, 70)  # fits 3-digit slave IDs
        self.register_table.setColumnWidth(_FUNCTION_CODE_COLUMN, 210)  # fits "0x03 Read Holding Registers"
        self.register_table.setColumnWidth(_FORMAT_COLUMN, 150)  # fits "16-bit Unsigned"
        self.register_table.setColumnWidth(_BYTE_ORDER_COLUMN, 190)  # fits "Byte-and-Word Swapped"
        self.register_table.setColumnWidth(_REGISTER_COLUMN, 100)  # fits e.g. "1234-1237"
        self.register_table.setColumnWidth(_TIMESTAMP_COLUMN, 110)
        self.register_table.setColumnWidth(_STATUS_COLUMN, 140)
        # A modest floor, not a target: `stretch=1` below is what actually
        # gives this table the rest of the tab's height on any reasonably
        # sized window. Kept small so it doesn't itself force the whole page
        # to grow past the window on a shorter one — see the matching
        # MainWindow.profile_tab.setMinimumHeight() override, which is what
        # lets this table's own scrollbar handle a short window instead of
        # the whole page scrolling.
        self.register_table.setMinimumHeight(220)
        self.register_table.cellChanged.connect(self._on_cell_changed)

        self.function_code_delegate = FunctionCodeDelegate(self.register_table)
        self.register_table.setItemDelegateForColumn(
            _FUNCTION_CODE_COLUMN, self.function_code_delegate
        )
        self.format_delegate = FormatDelegate(self.register_table)
        self.register_table.setItemDelegateForColumn(_FORMAT_COLUMN, self.format_delegate)
        self.byte_order_delegate = ByteOrderDelegate(self.register_table)
        self.register_table.setItemDelegateForColumn(
            _BYTE_ORDER_COLUMN, self.byte_order_delegate
        )
        self.register_address_delegate = RegisterAddressDelegate(self.register_table)
        self.register_table.setItemDelegateForColumn(
            _REGISTER_COLUMN, self.register_address_delegate
        )

        parent_layout.addWidget(self.register_table, stretch=1)

        header_tooltips = {
            0: "Friendly label for this register, shown throughout the app.",
            _SLAVE_ID_COLUMN: "Which Modbus slave (unit) ID this register belongs "
            "to. Edit to move this register to a different slave within this PLC "
            "profile — registers keep their own values per slave, so the same "
            "address on two different slaves never gets mixed up.",
            _FUNCTION_CODE_COLUMN: "Which Modbus read produced this register, "
            "e.g. 0x03 = Read Holding Registers.",
            _FORMAT_COLUMN: "How to interpret this register's raw bytes, and how many "
            "consecutive registers it spans (16-bit = 1, everything else = 2 or 4). "
            "Only meaningful for register reads/writes (function codes 03, 04, 06, "
            "16); coil-type functions are always single-bit and are never combined.",
            _BYTE_ORDER_COLUMN: "Which official standard byte order to use when "
            "decoding this register — the options offered depend on the selected "
            "Format's register span. Hover an option (open the dropdown, or this "
            "cell when closed) to see its example letter mapping. Vendors disagree "
            "on this, so if a decoded value looks garbled, try another option.",
            _REGISTER_COLUMN: "Raw, zero-based starting register address exactly as "
            "seen on the wire. Shows a range (e.g. \"48-49\") once Format spans more "
            "than one register — edit just the starting number. Hover for the "
            "equivalent Modicon reference (e.g. 40049).",
            6: "Fills in automatically from a matching sniffed response packet.",
            7: "Scale factor applied to the raw value to produce Parsed Value.",
            8: "Fills in automatically: the raw reading × Multiplier.",
            9: "Unit label shown next to the parsed value, e.g. °C or kWh.",
            10: "Free-form notes about this register.",
            _TIMESTAMP_COLUMN: "When this row last matched a sniffed response packet.",
            _STATUS_COLUMN: "Passive-sniffing state for this row: not sniffed yet, "
            "waiting for data, updated, or stopped.",
        }
        for column, tooltip in header_tooltips.items():
            header_item = self.register_table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setToolTip(tooltip)

    def load_profiles_from_json(self) -> None:
        if not self.profile_json_path.exists():
            self.profiles = []
            return
        try:
            with self.profile_json_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (json.JSONDecodeError, OSError):
            self.profiles = []
            return
        self.profiles = [self._normalize_profile(profile) for profile in loaded]

    @staticmethod
    def _normalize_profile(profile: dict) -> dict:
        """Normalize a profile dict into the current {"slaves": [...]} shape.

        One PLC profile may now contain several Modbus slave IDs discovered
        in the same capture (see passive_capture.PassiveSniffingWidget.
        current_profile_data), each owning its own register list. Profiles
        saved before that existed store "slave_id"/"registers" directly on
        the top-level profile dict instead — detected here by the absence
        of a "slaves" key (an *empty* "slaves": [] list, by contrast, is
        already the current shape and is left alone) and wrapped as that
        profile's single slave.

        This only ever changes the in-memory dict; nothing is written back
        to profile.json until the user actually saves, so an older build
        reading the same file afterwards is unaffected by a load that
        never itself writes anything out.
        """
        if "slaves" not in profile:
            profile["slaves"] = [
                {
                    "slave_id": profile.get("slave_id", 0),
                    "registers": profile.get("registers", []),
                }
            ]
            # These lived at the profile level only because a profile used
            # to have exactly one slave; now that the slave itself owns
            # them, keeping stale top-level copies around risks them
            # silently drifting out of sync with the real (slave-level)
            # values the next time this profile is edited.
            profile.pop("slave_id", None)
            profile.pop("registers", None)
            profile.pop("start_address", None)
            profile.pop("count", None)
        for slave in profile["slaves"]:
            slave.setdefault("slave_id", 0)
            slave.setdefault("registers", [])
        return profile

    def save_profiles_to_json(self) -> None:
        try:
            with self.profile_json_path.open("w", encoding="utf-8") as handle:
                json.dump(self.profiles, handle, indent=2)
        except OSError:
            pass

    def _refresh_profile_list(self) -> None:
        self.profile_list.clear()
        for profile in self.profiles:
            item = QListWidgetItem(profile.get("name", "Untitled"))
            item.setData(Qt.UserRole, profile.get("id"))
            self.profile_list.addItem(item)

    def _set_status(self, text: str, kind: str = "info") -> None:
        self.edit_status_label.setStyleSheet(STATUS_STYLES.get(kind, STATUS_STYLES["info"]))
        self.edit_status_label.setText(text)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_profile_selected(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        if self._editing and current is not previous:
            if not self._confirm_discard_if_editing():
                self._reselect_item(previous)
                return
        self.current_profile_id = current.data(Qt.UserRole) if current is not None else None
        self._load_current_profile()

    def _reselect_item(self, item: QListWidgetItem | None) -> None:
        self.profile_list.blockSignals(True)
        if item is not None:
            self.profile_list.setCurrentItem(item)
        self.profile_list.blockSignals(False)

    def _confirm_discard_if_editing(self) -> bool:
        reply = QMessageBox.question(
            self,
            "Discard unsaved changes?",
            "This profile has unsaved changes. Switch profiles and discard them?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return False
        self._revert_to_snapshot()
        return True

    def _set_active_profile(self, profile_id: str | None) -> None:
        if profile_id is None or not self.profiles:
            if self.profile_list.count() > 0:
                self.profile_list.setCurrentRow(0)
            else:
                self.current_profile_id = None
                self._load_current_profile()
            return
        for index in range(self.profile_list.count()):
            item = self.profile_list.item(index)
            if item and item.data(Qt.UserRole) == profile_id:
                self.profile_list.setCurrentItem(item)
                return
        self._set_active_profile(None)

    # ------------------------------------------------------------------
    # Load / display (always read-only until "Edit Profile" is clicked)
    # ------------------------------------------------------------------

    def _load_current_profile(self) -> None:
        profile = self._current_profile()
        self._ignore_changes = True
        self._editing = False
        self._edit_snapshot = None

        if profile is None:
            self.profile_name_edit.clear()
            self.profile_name_edit.setEnabled(False)
            self.baudrate_combo.setCurrentIndex(0)
            self.parity_combo.setCurrentIndex(0)
            self.stop_bits_combo.setCurrentIndex(0)
            self.register_table.setRowCount(0)
            self._row_slaves = []
            self._row_registers = []
            self._refresh_slave_filter_options(None)
            self._set_profile_controls_enabled(False)
            self.edit_profile_btn.setEnabled(False)
            self.save_profile_btn.setEnabled(False)
            self.discard_changes_btn.setEnabled(False)
            self._update_profile_summary(None)
            self._set_status(
                "No profiles yet. Click \"Add\" to create one, or capture a session "
                "on the Passive Sniffing tab and save it as a profile.",
                "info",
            )
            self._ignore_changes = False
            return

        self.profile_name_edit.setEnabled(True)
        self.profile_name_edit.setText(profile.get("name", ""))
        self._select_combo_by_value(self.baudrate_combo, profile.get("baudrate", 9600))
        self._select_combo_by_value(self.parity_combo, profile.get("parity", "None"))
        self._select_combo_by_value(self.stop_bits_combo, profile.get("stop_bits", 1.0))
        self._populate_register_table(profile)
        self._set_profile_controls_enabled(False)
        self.edit_profile_btn.setEnabled(True)
        self.save_profile_btn.setEnabled(False)
        self.discard_changes_btn.setEnabled(False)
        self._update_profile_summary(profile)
        self._set_status(
            "Read-only — click \"Edit Profile\" to change the configuration or registers.",
            "info",
        )
        self._ignore_changes = False

    def _select_combo_by_value(self, combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _refresh_slave_filter_options(self, profile: dict | None) -> None:
        """Rebuild slave_filter's options from *profile*'s own slaves.

        Only slaves that still have at least one register are listed — a
        slave whose last register was removed (or moved to a different
        slave ID) is dropped from the filter instead of lingering there
        with nothing left to actually show. Keeps the current selection
        when that slave ID is still offered; otherwise falls back to
        "All slaves" — e.g. once the slave it was showing no longer has
        any registers, or the profile changed.
        """
        previous = self.slave_filter.currentData()
        self.slave_filter.blockSignals(True)
        try:
            self.slave_filter.clear()
            self.slave_filter.addItem("All slaves", None)
            if profile is not None:
                slave_ids = sorted(
                    {
                        slave.get("slave_id", 0)
                        for slave in profile.get("slaves", [])
                        if slave.get("registers")
                    }
                )
                for slave_id in slave_ids:
                    self.slave_filter.addItem(f"Slave {slave_id}", slave_id)
            index = self.slave_filter.findData(previous)
            self.slave_filter.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.slave_filter.blockSignals(False)

    def _set_profile_controls_enabled(self, enabled: bool) -> None:
        self.profile_name_edit.setReadOnly(not enabled)
        self.config_group.setEnabled(enabled)
        # slave_filter (like function_code_filter) narrows which
        # already-visible rows are shown — that's not an edit action, so it
        # stays usable read-only; the Slave ID column itself is only
        # editable once register_table's own edit triggers are unlocked
        # below, exactly like every other register column.
        self.add_register_btn.setEnabled(enabled)
        self.remove_register_btn.setEnabled(enabled)
        self.register_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            if enabled
            else QAbstractItemView.NoEditTriggers
        )

    def _update_profile_summary(self, profile: dict | None) -> None:
        if not profile:
            self.profile_summary_label.setText("")
            self.profile_summary_label.setVisible(False)
            return
        self.profile_summary_label.setVisible(True)
        slaves = profile.get("slaves", [])
        slave_count = len(slaves)
        total_registers = sum(len(slave.get("registers", [])) for slave in slaves)
        plural = "s" if slave_count != 1 else ""
        header = f"{slave_count} slave{plural} in this profile — "
        if not total_registers:
            self.profile_summary_label.setText(header + "no registers mapped yet.")
            return
        all_addresses = [
            register["address"]
            for slave in slaves
            for register in slave.get("registers", [])
            if register.get("address") is not None
        ]
        if all_addresses:
            all_addresses.sort()
            range_text = (
                f"{all_addresses[0]}–{all_addresses[-1]}"
                if len(all_addresses) > 1
                else str(all_addresses[0])
            )
            self.profile_summary_label.setText(
                header + f"{total_registers} register(s) mapped · register range {range_text}"
            )
        else:
            self.profile_summary_label.setText(
                header + f"{total_registers} register(s), no addresses set yet."
            )

    # ------------------------------------------------------------------
    # Edit / Save / Discard workflow
    # ------------------------------------------------------------------

    def enter_edit_mode(self) -> None:
        profile = self._current_profile()
        if profile is None:
            return
        if self.sniff_capture_service.worker is not None:
            self._set_status(
                "Stop passive sniffing before editing this profile.", "warning"
            )
            return
        self._edit_snapshot = copy.deepcopy(profile)
        self._editing = True
        self._set_profile_controls_enabled(True)
        self.edit_profile_btn.setEnabled(False)
        self.save_profile_btn.setEnabled(True)
        self.discard_changes_btn.setEnabled(True)
        self._set_status(
            "Editing — changes are kept locally until you click \"Save Profile\".",
            "editing",
        )
        self.profile_name_edit.setFocus()

    def save_and_exit_edit_mode(self) -> None:
        if not self._editing:
            return
        profile = self._current_profile()
        if profile is None:
            return
        profile["name"] = self.profile_name_edit.text().strip() or "Untitled"
        self._sync_config_fields(profile)
        self.save_profiles_to_json()
        self._edit_snapshot = None
        self._editing = False
        self._refresh_profile_list()
        self._set_active_profile(profile["id"])
        self._set_status(f"Saved \"{profile['name']}\".", "saved")

    def discard_changes(self) -> None:
        if not self._editing:
            return
        profile_id = self.current_profile_id
        self._revert_to_snapshot()
        self._refresh_profile_list()
        # _refresh_profile_list() clears the list widget, which itself fires a
        # selection-changed signal and would otherwise null out
        # current_profile_id before we get to reload it. Reselect by id
        # explicitly instead of reloading in place.
        self._set_active_profile(profile_id)
        self._set_status("Changes discarded.", "info")

    def _revert_to_snapshot(self) -> None:
        if self._edit_snapshot is not None and self.current_profile_id is not None:
            for index, profile in enumerate(self.profiles):
                if profile.get("id") == self.current_profile_id:
                    self.profiles[index] = self._edit_snapshot
                    break
            self.save_profiles_to_json()
        self._edit_snapshot = None
        self._editing = False

    def _sync_config_fields(self, profile: dict) -> None:
        """Sync the PLC-level (profile-wide) fields only.

        Slave ID lives on each register's own row (the Slave ID column),
        not the profile — edited directly there, via _on_cell_changed.
        """
        profile["baudrate"] = int(self.baudrate_combo.currentData())
        profile["parity"] = str(self.parity_combo.currentData())
        profile["stop_bits"] = float(self.stop_bits_combo.currentData())

    def _sync_slave_summary_fields(self, slave: dict) -> None:
        """Recompute one slave's own start_address/count summary fields.

        Purely informational metadata carried over from the pre-multi-slave
        profile shape (nothing in this app reads it back) — scoped to the
        individual slave now that one profile can hold several, so it
        never mixes one slave's register span with another's.
        """
        addresses = [
            register.get("address")
            for register in slave.get("registers", [])
            if register.get("address") is not None
        ]
        slave["start_address"] = min(addresses) if addresses else 0
        slave["count"] = len(slave.get("registers", []))

    def add_profile(self) -> None:
        new_profile = {
            "id": uuid.uuid4().hex,
            "name": "New profile",
            "baudrate": 9600,
            "parity": "None",
            "stop_bits": 1.0,
            "slaves": [{"slave_id": 0, "start_address": 0, "count": 0, "registers": []}],
        }
        self.profiles.append(new_profile)
        self.save_profiles_to_json()
        self._refresh_profile_list()
        self._set_active_profile(new_profile["id"])
        self.enter_edit_mode()

    def remove_profile(self) -> None:
        current = self.profile_list.currentItem()
        if current is None:
            return
        profile_id = current.data(Qt.UserRole)
        profile = self._current_profile()
        name = profile.get("name", "this profile") if profile else "this profile"
        reply = QMessageBox.question(
            self,
            "Remove profile",
            f'Remove "{name}"? This cannot be undone.',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.profiles = [p for p in self.profiles if p.get("id") != profile_id]
        self._editing = False
        self._edit_snapshot = None
        self.save_profiles_to_json()
        self._refresh_profile_list()
        self._set_active_profile(None)

    def _current_profile(self) -> dict | None:
        if self.current_profile_id is None:
            return None
        for profile in self.profiles:
            if profile.get("id") == self.current_profile_id:
                return profile
        return None

    @staticmethod
    def _find_or_create_slave(profile: dict, slave_id: int) -> dict:
        """The profile's existing slave with this ID, or a new empty one.

        Used by add_register (which slave does a brand-new row join?) and
        by editing a row's own Slave ID cell (moving that register to a
        slave ID the profile doesn't have yet) — either way, one profile
        still ends up with at most one slave entry per slave_id.
        """
        for slave in profile.get("slaves", []):
            if slave.get("slave_id") == slave_id:
                return slave
        new_slave = {"slave_id": slave_id, "start_address": 0, "count": 0, "registers": []}
        profile.setdefault("slaves", []).append(new_slave)
        return new_slave

    @staticmethod
    def _remove_register_from_slave(slave: dict, register: dict) -> None:
        """Drop *register* from *slave*'s own list, matched by identity.

        Matching by identity (``is``), not ``==``/list.remove(), matters
        here: two still-blank registers (or any two with identical field
        values) compare equal by value, so list.remove() could silently
        delete the wrong one.
        """
        registers = slave.get("registers", [])
        for index, candidate in enumerate(registers):
            if candidate is register:
                del registers[index]
                return

    def _on_profile_name_changed(self, text: str) -> None:
        if self._ignore_changes:
            return
        profile = self._current_profile()
        if profile is None:
            return
        # Staged only: the list and disk copy update when Save Profile runs,
        # so the sidebar doesn't rebuild (and steal focus) on every keystroke.
        profile["name"] = text.strip() or "Untitled"

    def _on_config_field_changed(self, *_args) -> None:
        if self._ignore_changes:
            return
        profile = self._current_profile()
        if profile is None:
            return
        self._sync_config_fields(profile)

    def _populate_register_table(self, profile: dict) -> None:
        """(Re)build register_table from every slave in *profile*, flattened.

        One shared table for every slave ID (see the Slave ID column and
        slave_filter) instead of one table per slave — self._row_slaves/
        self._row_registers are what let row-indexed code (editing,
        add/remove, live sniffing) find each row's owning slave and
        register dict again afterwards.
        """
        self._ignore_changes = True
        self.register_table.setRowCount(0)
        self._row_slaves = []
        self._row_registers = []
        for slave in profile.get("slaves", []):
            for register in slave.get("registers", []):
                self._insert_register_row(register, slave)
        self._ignore_changes = False
        self._refresh_slave_filter_options(profile)
        self._apply_filter()

    def _insert_register_row(self, register: dict, slave: dict) -> None:
        row = self.register_table.rowCount()
        self.register_table.insertRow(row)
        self._row_slaves.append(slave)
        self._row_registers.append(register)

        self.register_table.setItem(row, 0, QTableWidgetItem(register.get("name", "")))
        self.register_table.setItem(
            row, _SLAVE_ID_COLUMN, QTableWidgetItem(str(slave.get("slave_id", 0)))
        )
        self.register_table.setItem(
            row,
            _FUNCTION_CODE_COLUMN,
            QTableWidgetItem(self._function_code_display(register.get("function_code"))),
        )

        data_type = register.get("data_type", "uint16")
        word_count = _FORMAT_WORD_COUNTS.get(data_type, 1)
        self.register_table.setItem(
            row, _FORMAT_COLUMN, QTableWidgetItem(self._format_display(data_type))
        )
        byte_order_item = QTableWidgetItem(
            self._byte_order_display(register.get("byte_order"), word_count)
        )
        byte_order_item.setToolTip(
            self._byte_order_tooltip(register.get("byte_order"), word_count)
        )
        self.register_table.setItem(row, _BYTE_ORDER_COLUMN, byte_order_item)

        register_item = QTableWidgetItem(
            self._register_range_text(register.get("address"), data_type)
        )
        register_item.setToolTip(
            self._register_tooltip(
                register.get("address"), register.get("function_code"), data_type
            )
        )
        self.register_table.setItem(row, _REGISTER_COLUMN, register_item)

        raw_item = QTableWidgetItem(register.get("raw_hex", ""))
        raw_item.setFlags(raw_item.flags() & ~Qt.ItemIsEditable)
        self.register_table.setItem(row, 6, raw_item)

        self.register_table.setItem(row, 7, QTableWidgetItem(str(register.get("multiplier", 1.0))))

        parsed_item = QTableWidgetItem(register.get("parsed_value", ""))
        parsed_item.setFlags(parsed_item.flags() & ~Qt.ItemIsEditable)
        self.register_table.setItem(row, 8, parsed_item)

        self.register_table.setItem(row, 9, QTableWidgetItem(register.get("unit", "")))

        # Description doesn't wrap (setWordWrap(False) on the whole table,
        # so every row stays one line tall) and can run longer than the
        # column, so the full text is always available as a hover tooltip —
        # same pattern the Passive Sniffing tab's capture table already uses.
        description_text = register.get("description", "") or ""
        description_item = QTableWidgetItem(description_text)
        description_item.setToolTip(description_text)
        self.register_table.setItem(row, 10, description_item)

        # Sniffing status, not part of the saved profile: reset to a fresh
        # "not sniffed yet" placeholder whenever a row is (re)built, e.g. on
        # profile load or Add Register. Updated live by _refresh_sniff_cells().
        timestamp_item = QTableWidgetItem("—")
        timestamp_item.setFlags(timestamp_item.flags() & ~Qt.ItemIsEditable)
        self.register_table.setItem(row, _TIMESTAMP_COLUMN, timestamp_item)

        status_item = QTableWidgetItem("Not sniffed yet")
        status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
        self.register_table.setItem(row, _STATUS_COLUMN, status_item)

    def add_register(self) -> None:
        if not self._editing:
            return
        profile = self._current_profile()
        if profile is None:
            return
        # New rows join whichever slave the Slave ID filter is currently
        # narrowed to; with "All slaves" selected (or no slaves at all yet)
        # they fall back to the profile's first slave, same as the single-
        # slave case before this feature existed.
        slave_id = self.slave_filter.currentData()
        if slave_id is None:
            slaves = profile.get("slaves", [])
            slave_id = slaves[0].get("slave_id", 0) if slaves else 0
        slave = self._find_or_create_slave(profile, slave_id)
        register = {
            "name": "",
            "description": "",
            "address": None,
            "multiplier": 1.0,
            "raw_hex": "",
            "raw_value": None,
            "parsed_value": "",
            "unit": "",
            "function_code": None,
            "data_type": "uint16",
            "byte_order": "big_endian",
        }
        slave.setdefault("registers", []).append(register)
        self._insert_register_row(register, slave)
        self._sync_slave_summary_fields(slave)
        self._refresh_slave_filter_options(profile)
        self._apply_filter()
        self._update_profile_summary(profile)

    def remove_register(self) -> None:
        if not self._editing:
            return
        selected_rows = sorted({index.row() for index in self.register_table.selectedIndexes()})
        if not selected_rows:
            return
        profile = self._current_profile()
        if profile is None:
            return
        affected_slaves: list[dict] = []
        for row in reversed(selected_rows):
            if 0 <= row < len(self._row_registers):
                slave = self._row_slaves[row]
                register = self._row_registers[row]
                self._remove_register_from_slave(slave, register)
                if not any(existing is slave for existing in affected_slaves):
                    affected_slaves.append(slave)
                del self._row_slaves[row]
                del self._row_registers[row]
            self.register_table.removeRow(row)
        for slave in affected_slaves:
            self._sync_slave_summary_fields(slave)
        # A slave that just lost its last register must drop out of the
        # Slave ID filter's own options too, not just out of the table.
        self._refresh_slave_filter_options(profile)
        self._apply_filter()
        self._update_profile_summary(profile)

    def _on_cell_changed(self, row: int, column: int) -> None:
        if self._ignore_changes:
            return
        profile = self._current_profile()
        if profile is None:
            return
        if row < 0 or row >= len(self._row_registers):
            return
        slave = self._row_slaves[row]
        register = self._row_registers[row]
        item = self.register_table.item(row, column)
        if item is None:
            return
        text = item.text().strip()

        if column == 0:
            register["name"] = text
        elif column == _SLAVE_ID_COLUMN:
            new_slave_id = self._parse_slave_id(text)
            if new_slave_id is None or new_slave_id == slave.get("slave_id"):
                # Invalid, or typed back to the same value — just reassert
                # the current (numeric, normalized) text rather than
                # leaving whatever the user typed sitting in the cell.
                self._refresh_slave_id_cell(row, slave)
                return
            # Moving a register to a different Slave ID must never touch
            # another slave's own registers — find (or create) that slave
            # and physically relocate this one register's dict into it, so
            # e.g. Slave 1/Register 10 and Slave 2/Register 10 stay two
            # entirely separate dicts even if this row now joins Slave 2.
            target_slave = self._find_or_create_slave(profile, new_slave_id)
            self._remove_register_from_slave(slave, register)
            target_slave.setdefault("registers", []).append(register)
            self._row_slaves[row] = target_slave
            self._sync_slave_summary_fields(slave)
            self._sync_slave_summary_fields(target_slave)
            self._refresh_slave_filter_options(profile)
            self._apply_filter()
            self._update_profile_summary(profile)
        elif column == _FUNCTION_CODE_COLUMN:
            register["function_code"] = self._parse_function_code(text)
            register["raw_hex"] = ""
            register["raw_value"] = None
            register["parsed_value"] = ""
            self._refresh_measurement_cells(row, register)
            self._refresh_register_cell(row, register)
            self._refresh_function_code_cell(row, register)
            self._update_profile_summary(profile)
        elif column == _FORMAT_COLUMN:
            data_type = self._parse_format_display(text)
            register["data_type"] = data_type
            # Which Byte Order options are valid depends on the Format's
            # word count — if the previously-set one isn't offered at the
            # new width (e.g. "Double-Word Swapped" going from 64-bit down
            # to 16-bit), fall back to Big-Endian rather than silently
            # keeping a choice that's no longer even shown as an option.
            word_count = _FORMAT_WORD_COUNTS.get(data_type, 1)
            valid_byte_orders = {
                candidate
                for candidate, _label, _letters in self._byte_order_options_for_word_count(
                    word_count
                )
            }
            if register.get("byte_order") not in valid_byte_orders:
                register["byte_order"] = "big_endian"
            # A different format reads a different number of registers with
            # a different interpretation, so any previously-decoded reading
            # no longer applies until the next matching frame.
            register["raw_hex"] = ""
            register["raw_value"] = None
            register["parsed_value"] = ""
            self._refresh_measurement_cells(row, register)
            self._refresh_register_cell(row, register)
            self._refresh_byte_order_cell(row, register)
            self._update_profile_summary(profile)
        elif column == _BYTE_ORDER_COLUMN:
            word_count = _FORMAT_WORD_COUNTS.get(register.get("data_type", "uint16"), 1)
            register["byte_order"] = self._parse_byte_order_display(text, word_count)
            register["raw_hex"] = ""
            register["raw_value"] = None
            register["parsed_value"] = ""
            self._refresh_measurement_cells(row, register)
            self._refresh_byte_order_cell(row, register)
        elif column == _REGISTER_COLUMN:
            register["address"] = self._parse_register_address(text)
            register["raw_hex"] = ""
            register["raw_value"] = None
            register["parsed_value"] = ""
            self._refresh_measurement_cells(row, register)
            self._refresh_register_cell(row, register)
            self._sync_slave_summary_fields(slave)
            self._update_profile_summary(profile)
        elif column == 7:
            register["multiplier"] = self._parse_float(text) or 1.0
            self._recompute_parsed_value(row, register)
        elif column == 9:
            register["unit"] = text
        elif column == 10:
            register["description"] = text
            item.setToolTip(text)

    def _recompute_parsed_value(self, row: int, register: dict) -> None:
        """Re-derive Parsed Value from the stored raw reading and multiplier.

        Without this, changing the multiplier on an already-detected register
        left the Parsed Value column stuck at whatever it showed when the
        raw value was first captured.
        """
        raw_value = register.get("raw_value")
        if raw_value is None:
            # Backward compatible with profiles saved before per-register
            # data types existed, where raw_hex was always a plain uint16
            # hex value and there was no separate raw_value field yet.
            raw_hex = register.get("raw_hex")
            if not raw_hex:
                return
            try:
                raw_value = int(raw_hex, 16)
            except ValueError:
                return
        multiplier = float(register.get("multiplier", 1.0))
        register["parsed_value"] = self._format_parsed_value(raw_value * multiplier)
        self._refresh_measurement_cells(row, register)

    def _refresh_measurement_cells(self, row: int, register: dict) -> None:
        """Redisplay Raw Hex Value / Parsed Value from the register dict.

        Never touches the Function Code, Register, or Address columns, so
        it's safe to call synchronously from inside a cellChanged handler
        for this row.
        """
        raw_item = self.register_table.item(row, 6)
        parsed_item = self.register_table.item(row, 8)
        if raw_item is None or parsed_item is None:
            return
        self.register_table.blockSignals(True)
        try:
            raw_item.setText(register.get("raw_hex", ""))
            parsed_item.setText(register.get("parsed_value", ""))
        finally:
            self.register_table.blockSignals(False)

    def _refresh_register_cell(self, row: int, register: dict) -> None:
        """Recompute the Register cell's range text and address tooltip.

        Deferred like _refresh_function_code_cell below: this can run from
        inside the Register column's own cellChanged signal (editing the
        address itself), and Qt does not tolerate a QTableWidgetItem
        text/tooltip change nested inside its own change notification. It's
        also called from Function Code and Format edits, where that hazard
        doesn't apply — deferring unconditionally is simpler than tracking
        which callers need it.
        """

        def _apply() -> None:
            if row >= self.register_table.rowCount():
                return
            item = self.register_table.item(row, _REGISTER_COLUMN)
            if item is None:
                return
            data_type = register.get("data_type", "uint16")
            self.register_table.blockSignals(True)
            try:
                item.setText(self._register_range_text(register.get("address"), data_type))
                item.setToolTip(
                    self._register_tooltip(
                        register.get("address"), register.get("function_code"), data_type
                    )
                )
            finally:
                self.register_table.blockSignals(False)

        QTimer.singleShot(0, _apply)

    def _refresh_byte_order_cell(self, row: int, register: dict) -> None:
        """Recompute the Byte Order cell's label and letter-mapping tooltip.

        Needed after a Format edit even when the stored operation key is
        unchanged, since the *label* for the same operation can differ by
        word count (e.g. "byte_swapped" reads "Byte-Swapped" at 32-bit but
        "Byte-and-Word Swapped" at 64-bit) — and after a Byte Order edit,
        since that's this cell's own column (see _refresh_register_cell's
        docstring for why this is always deferred).
        """

        def _apply() -> None:
            if row >= self.register_table.rowCount():
                return
            item = self.register_table.item(row, _BYTE_ORDER_COLUMN)
            if item is None:
                return
            word_count = _FORMAT_WORD_COUNTS.get(register.get("data_type", "uint16"), 1)
            byte_order = register.get("byte_order")
            self.register_table.blockSignals(True)
            try:
                item.setText(self._byte_order_display(byte_order, word_count))
                item.setToolTip(self._byte_order_tooltip(byte_order, word_count))
            finally:
                self.register_table.blockSignals(False)

        QTimer.singleShot(0, _apply)

    def _refresh_function_code_cell(self, row: int, register: dict) -> None:
        """Reformat the Function Code cell to its "0xNN Name" display.

        Deferred to the next event-loop tick: this runs from inside the
        Function Code column's own cellChanged signal (the edit that just
        set this very cell), and Qt does not tolerate a
        QTableWidgetItem.setText() call nested inside its own change
        notification — so this waits until that signal has fully unwound.
        """

        def _apply() -> None:
            if row >= self.register_table.rowCount():
                return
            item = self.register_table.item(row, _FUNCTION_CODE_COLUMN)
            if item is None:
                return
            self.register_table.blockSignals(True)
            try:
                item.setText(self._function_code_display(register.get("function_code")))
            finally:
                self.register_table.blockSignals(False)

        QTimer.singleShot(0, _apply)

    def _refresh_slave_id_cell(self, row: int, slave: dict) -> None:
        """Reassert the Slave ID cell's own (numeric, normalized) text.

        Called after an edit is rejected (invalid text) or is a no-op (typed
        back to the same value) so the cell never keeps showing whatever the
        user actually typed. Deferred for the same reason as
        _refresh_register_cell/_refresh_byte_order_cell/_refresh_function_
        code_cell above: this runs from inside the Slave ID column's own
        cellChanged signal.
        """

        def _apply() -> None:
            if row >= self.register_table.rowCount():
                return
            item = self.register_table.item(row, _SLAVE_ID_COLUMN)
            if item is None:
                return
            self.register_table.blockSignals(True)
            try:
                item.setText(str(slave.get("slave_id", 0)))
            finally:
                self.register_table.blockSignals(False)

        QTimer.singleShot(0, _apply)

    def _parse_int(self, value: str) -> int | None:
        try:
            return int(value)
        except ValueError:
            return None

    def _parse_register_address(self, value: str) -> int | None:
        """Parse the Register column's text into a valid 16-bit address.

        Returns None (same as an unparseable value) for anything outside
        the valid 0-65535 range, so a stray negative or oversized address
        never gets silently stored — it's treated exactly like invalid text.
        """
        candidate = self._parse_int(value)
        if candidate is None:
            return None
        try:
            return validate_register_address(candidate)
        except ConfigurationError:
            return None

    def _parse_slave_id(self, value: str) -> int | None:
        """Parse the Slave ID column's text into a valid Modbus unit ID (0-247)."""
        candidate = self._parse_int(value)
        if candidate is None or not (0 <= candidate <= 247):
            return None
        return candidate

    def _parse_float(self, value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    def _format_parsed_value(self, value: float) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{value:.6g}"

    def _register_range_text(self, address: int | None, data_type: str) -> str:
        """The Register cell's own displayed text: a single address, or a
        range once Format spans more than one register (e.g. "48-49").

        RegisterAddressDelegate.setEditorData() is what strips this back
        down to just the starting number when the cell is actually edited.
        """
        if address is None:
            return ""
        word_count = _FORMAT_WORD_COUNTS.get(data_type, 1)
        if word_count <= 1:
            return str(address)
        return f"{address}-{address + word_count - 1}"

    def _register_tooltip(
        self, address: int | None, function_code: int | None, data_type: str
    ) -> str:
        """Hover tooltip for the Register cell: the standard Modicon reference.

        Zero-padded to 5 digits (e.g. FC 03 address 0 -> "40001", FC 01
        address 5 -> "00006"), matching how every PLC vendor documents
        registers. Blank until both the register and its function code are
        known. Shown as a range (e.g. "40049-40050") when Format spans more
        than one register.
        """
        if address is None:
            return ""
        offset = MODICON_BLOCK_OFFSET.get(function_code)
        if offset is None:
            return ""
        start = offset + address
        word_count = _FORMAT_WORD_COUNTS.get(data_type, 1)
        if word_count <= 1:
            return f"Address: {start:05d}"
        return f"Address: {start:05d}-{start + word_count - 1:05d}"

    @staticmethod
    def _function_code_display(function_code: int | None) -> str:
        if function_code is None:
            return ""
        name = FUNCTION_NAMES.get(function_code)
        return f"0x{function_code:02X} {name}" if name else f"0x{function_code:02X}"

    @staticmethod
    def _parse_function_code(text: str) -> int | None:
        text = text.strip()
        if not text:
            return None
        # Accept a bare decimal ("3"), a hex code ("0x03"), or the full
        # descriptive display ("0x03 Read Holding Registers") by reading
        # only its leading token, so re-editing an unchanged cell round-trips.
        token = text.split(maxsplit=1)[0]
        try:
            return int(token, 16) if token.lower().startswith("0x") else int(token)
        except ValueError:
            return None

    @staticmethod
    def _format_display(data_type: str | None) -> str:
        data_type = data_type or "uint16"
        for candidate_type, _word_count, label in _FORMAT_OPTIONS:
            if candidate_type == data_type:
                return label
        return "16-bit Unsigned"

    @staticmethod
    def _parse_format_display(text: str) -> str:
        text = text.strip()
        for data_type, _word_count, label in _FORMAT_OPTIONS:
            if label == text:
                return data_type
        return "uint16"

    @staticmethod
    def _byte_order_options_for_word_count(
        word_count: int,
    ) -> tuple[tuple[str, str, str], ...]:
        """The (operation, official name, example letters) rows valid for a
        given register span. Falls back to the 1-register set (just
        Big-Endian/Little-Endian) for any word count without its own entry.
        """
        return _BYTE_ORDER_OPTIONS_BY_WORD_COUNT.get(
            word_count, _BYTE_ORDER_OPTIONS_BY_WORD_COUNT[1]
        )

    @staticmethod
    def _byte_order_display(byte_order: str | None, word_count: int) -> str:
        byte_order = byte_order or "big_endian"
        for candidate, label, _letters in ProfileTab._byte_order_options_for_word_count(
            word_count
        ):
            if candidate == byte_order:
                return label
        return "Big-Endian"

    @staticmethod
    def _byte_order_tooltip(byte_order: str | None, word_count: int) -> str:
        """Example letter mapping for the current Format+Byte Order, e.g.
        "C D A B" — shown as the Byte Order cell's resting tooltip so the
        active choice's effect is visible without opening the dropdown.
        """
        byte_order = byte_order or "big_endian"
        for candidate, _label, letters in ProfileTab._byte_order_options_for_word_count(
            word_count
        ):
            if candidate == byte_order:
                return letters
        return ""

    @staticmethod
    def _parse_byte_order_display(text: str, word_count: int) -> str:
        text = text.strip()
        for byte_order, label, _letters in ProfileTab._byte_order_options_for_word_count(
            word_count
        ):
            if label == text:
                return byte_order
        return "big_endian"

    @staticmethod
    def _word_count_for_row(model, row: int) -> int:
        """Read the Format cell in `row` and return its word count.

        Used by ByteOrderDelegate and _refresh_byte_order_cell, both of
        which need to know a row's Format before they can know which Byte
        Order options are even valid for it.
        """
        format_text = model.index(row, _FORMAT_COLUMN).data(Qt.EditRole) or ""
        data_type = ProfileTab._parse_format_display(format_text)
        return _FORMAT_WORD_COUNTS.get(data_type, 1)

    @staticmethod
    def _combine_register_values(values: list[int], byte_order: str) -> int:
        """Combine consecutive 16-bit register values into one unsigned integer.

        Args:
            values: The register's own words in address order — values[0]
                is the value at the register's configured starting address,
                values[1] is address + 1, and so on. Given a run of bytes
                A B C D... in that order (A,B = values[0]'s own two bytes,
                C,D = values[1]'s, and so on — each register's own bytes are
                always big-endian per the Modbus wire format), each
                operation reorders them like so:
            byte_order: "big_endian" -> A B C D (no change); "little_endian"
                -> D C B A (fully reversed); "word_swapped" -> reverse the
                *register* order only, e.g. C D A B for two registers;
                "byte_swapped" -> swap each register's own two bytes only,
                word order unchanged, e.g. B A D C; "double_word_swapped"
                (four registers only) -> swap the first and second *pairs*
                of registers, e.g. E F G H A B C D.

        Returns:
            The combined unsigned integer, len(values) * 16 bits wide.
        """
        words = list(values)
        word_count = len(words)

        def byte_swap(word: int) -> int:
            return ((word & 0xFF) << 8) | ((word >> 8) & 0xFF)

        if byte_order == "big_endian":
            pass
        elif byte_order == "little_endian":
            words = [byte_swap(word) for word in reversed(words)]
        elif byte_order == "word_swapped":
            words.reverse()
        elif byte_order == "byte_swapped":
            words = [byte_swap(word) for word in words]
        elif byte_order == "double_word_swapped":
            half = word_count // 2
            words = words[half:] + words[:half]
        else:
            raise ValueError(f"Unsupported byte order: {byte_order!r}")

        combined = 0
        for word in words:
            combined = (combined << 16) | (word & 0xFFFF)
        return combined

    @staticmethod
    def _interpret_combined_value(combined: int, data_type: str) -> int | float:
        """Reinterpret a combined unsigned bit pattern per the register's Format.

        `combined` is always the plain unsigned integer _combine_register_
        values() produced (or, for a single-register uint16/int16 Format,
        just that one register's own value) — this only handles sign and
        float reinterpretation, never word/byte ordering.
        """
        if data_type in ("uint16", "uint32", "uint64"):
            return combined
        if data_type == "int16":
            return combined - 0x1_0000 if combined >= 0x8000 else combined
        if data_type == "int32":
            return combined - 0x1_0000_0000 if combined >= 0x8000_0000 else combined
        if data_type == "int64":
            return (
                combined - 0x1_0000_0000_0000_0000
                if combined >= 0x8000_0000_0000_0000
                else combined
            )
        if data_type == "float32":
            return struct.unpack(">f", combined.to_bytes(4, "big"))[0]
        if data_type == "float64":
            return struct.unpack(">d", combined.to_bytes(8, "big"))[0]
        raise ValueError(f"Unsupported format: {data_type!r}")

    def _find_value_for_address(
        self, address: int, frame: CapturedModbusFrame
    ) -> int | None:
        if frame.address is None or frame.quantity is None:
            return None
        offset = address - frame.address
        if offset < 0 or offset >= len(frame.values):
            return None
        return frame.values[offset]

    def set_latest_frame(self, frame: CapturedModbusFrame) -> None:
        """Receive one decoded, CRC-valid frame from any passive source.

        Two sources feed this: frames forwarded from the Passive Sniffing
        tab (Tab 1, via MainWindow) and, independently, this tab's own
        Start/Stop Passive Sniffing worker (see _connect_sniff_service()).
        Either way, keeps the most recent frame for reference and, when it
        is a response matching the selected profile's slave ID, fills in
        that profile's Raw Hex Value / Parsed Value / Timestamp / Status
        columns live.
        """
        self._latest_frame = frame
        self._apply_frame_to_registers(frame)

    def _apply_frame_to_registers(self, frame: CapturedModbusFrame) -> None:
        """Match one decoded frame against every row in the shared register table.

        register_table holds every slave in the current profile at once
        (see the Slave ID column/slave_filter), so this walks
        self._row_slaves/self._row_registers directly rather than going
        back through profile["slaves"] itself — each row's owning slave is
        checked (slave.get("slave_id") == frame.slave_id) *before* its
        register is touched at all, so a frame from one slave ID can never
        update another slave's register even when they share the same
        address (Slave 1/Register 10 and Slave 2/Register 10 are two
        separate rows/dicts here, each only ever matched against its own
        slave's traffic). Deliberately register-driven, not frame-driven,
        same as before this feature existed: only addresses a row's own
        register already lists are ever looked up or written — this never
        grows new rows or shows values the profile didn't ask for.
        """
        if not self._row_registers:
            return
        profile = self._current_profile()
        if profile is None:
            return

        self._ignore_changes = True
        updated = False
        for row, (slave, register) in enumerate(zip(self._row_slaves, self._row_registers)):
            if slave.get("slave_id") != frame.slave_id:
                continue
            address = register.get("address")
            if address is None:
                continue
            function_code = register.get("function_code")
            if function_code is not None and function_code != frame.function_code:
                continue
            data_type = register.get("data_type", "uint16")
            word_count = _FORMAT_WORD_COUNTS.get(data_type, 1)

            if word_count > 1 and frame.function_code not in _SIXTEEN_BIT_REGISTER_FUNCTION_CODES:
                # A multi-register reading only makes sense combining 16-bit
                # register words — this frame's function returns something
                # else (coils, etc.), so there's nothing valid to combine.
                continue

            values: list[int] = []
            for offset in range(word_count):
                value = self._find_value_for_address(address + offset, frame)
                if value is None:
                    break
                values.append(value)
            if len(values) != word_count:
                # Every register in the span has to land in the same
                # response to combine them; a frame that only covers part
                # of the span doesn't tell us the rest of the reading.
                continue

            # Applies even at word_count == 1: a single register can still
            # be "Little-Endian" (its own two bytes swapped) for a device
            # that packs that one register non-compliantly — see
            # _combine_register_values and the 16-bit row of
            # _BYTE_ORDER_OPTIONS_BY_WORD_COUNT.
            byte_order = register.get("byte_order", "big_endian")
            combined = self._combine_register_values(values, byte_order)
            raw_value = self._interpret_combined_value(combined, data_type)
            raw_hex = f"0x{combined:0{word_count * 4}X}"

            register["raw_hex"] = raw_hex
            register["raw_value"] = raw_value
            multiplier = float(register.get("multiplier", 1.0))
            register["parsed_value"] = self._format_parsed_value(raw_value * multiplier)
            self._refresh_measurement_cells(row, register)
            self._refresh_sniff_cells(row, frame.timestamp_text, "Updated")
            log_event(
                logging.INFO,
                "profile_register_decoded",
                profile_id=profile.get("id"),
                slave_id=slave.get("slave_id"),
                register_address=address,
                data_type=data_type,
                function_code=frame.function_code,
                raw_hex=register["raw_hex"],
                parsed_value=register["parsed_value"],
            )
            updated = True

        self._ignore_changes = False
        if updated:
            self.save_profiles_to_json()

    # ------------------------------------------------------------------
    # Passive sniffing: independent Start/Stop capture for this tab
    # ------------------------------------------------------------------

    def _connect_sniff_service(self) -> None:
        self.sniff_capture_service.frame_received.connect(self.set_latest_frame)
        self.sniff_capture_service.status_changed.connect(self._on_sniff_status)
        self.sniff_capture_service.error_occurred.connect(self._on_sniff_error)
        self.sniff_capture_service.running_changed.connect(self._on_sniff_running_changed)

    def _refresh_sniff_ports(self) -> None:
        previous = (
            self.sniff_port_combo.currentData()
            or self.sniff_port_combo.currentText().strip()
        )
        self.sniff_port_combo.clear()
        try:
            ports = sorted(
                self.sniff_capture_service.available_ports(),
                key=lambda item: item.device,
            )
        except OSError as error:
            self._set_sniff_status(f"Could not list serial ports: {error}", "warning")
            return
        for port in ports:
            self.sniff_port_combo.addItem(
                f"{port.device} — {port.description or 'Serial adapter'}", port.device
            )
        index = self.sniff_port_combo.findData(previous)
        if index >= 0:
            self.sniff_port_combo.setCurrentIndex(index)
        elif previous:
            self.sniff_port_combo.setEditText(str(previous))

    def _selected_sniff_port(self) -> str:
        return (
            str(self.sniff_port_combo.currentData() or self.sniff_port_combo.currentText())
            .split(" — ", 1)[0]
            .strip()
        )

    def toggle_sniffing(self) -> None:
        if self.sniff_capture_service.worker is not None:
            self.stop_sniffing()
        else:
            self.start_sniffing()

    def start_sniffing(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self._set_sniff_status(
                "Select a profile before starting passive sniffing.", "warning"
            )
            return
        if not any(slave.get("registers") for slave in profile.get("slaves", [])):
            self._set_sniff_status(
                "This profile has no registers to sniff yet.", "warning"
            )
            return
        if self._editing:
            self._set_sniff_status(
                "Finish editing (Save or Discard) before starting passive sniffing.",
                "warning",
            )
            return

        port = self._selected_sniff_port()
        parity_word = str(profile.get("parity", "None"))
        try:
            self.sniff_capture_service.start_configured(
                SerialSettings(
                    port=port,
                    baudrate=int(profile.get("baudrate", 9600)),
                    parity=_PROFILE_PARITY_TO_SERIAL_CODE.get(parity_word, "N"),
                    stopbits=float(profile.get("stop_bits", 1.0)),
                )
            )
        except (ConfigurationError, TypeError, ValueError) as error:
            self._set_sniff_status(f"Invalid passive sniffing setting: {error}", "warning")
            return

        log_event(
            logging.INFO,
            "profile_sniffing_started",
            profile_id=profile.get("id"),
            profile_name=profile.get("name"),
            port=port,
        )
        self._mark_all_rows_sniff_status("Waiting for data...")
        self._set_sniff_status(
            f"Opening {port} in receive-only mode for \"{profile.get('name', 'this profile')}\"...",
            "info",
        )

    def stop_sniffing(self) -> None:
        if self.sniff_capture_service.worker is None:
            return
        self.sniff_toggle_btn.setText("Stopping...")
        self.sniff_toggle_btn.setEnabled(False)
        self.sniff_capture_service.stop()

    def _on_sniff_running_changed(self, running: bool) -> None:
        self.sniff_toggle_btn.setEnabled(True)
        self.sniff_toggle_btn.setText(
            "Stop Passive Sniffing" if running else "Start Passive Sniffing"
        )
        self.sniff_port_combo.setEnabled(not running)
        self.sniff_refresh_btn.setEnabled(not running)
        self._set_profile_management_enabled(not running)
        if not running:
            self._mark_all_rows_sniff_status("Sniffing stopped")
            profile = self._current_profile()
            log_event(
                logging.INFO,
                "profile_sniffing_stopped",
                profile_id=profile.get("id") if profile else None,
            )

    def _on_sniff_status(self, message: str) -> None:
        self._set_sniff_status(message, "info")

    def _on_sniff_error(self, message: str) -> None:
        # Already logged distinctly by PassiveCaptureService itself
        # (capture_worker_error); this only needs to surface it in the UI.
        self._set_sniff_status(message, "warning")

    def _set_sniff_status(self, text: str, kind: str = "info") -> None:
        self.sniff_status_label.setStyleSheet(STATUS_STYLES.get(kind, STATUS_STYLES["info"]))
        self.sniff_status_label.setText(text)

    def _set_profile_management_enabled(self, enabled: bool) -> None:
        """Lock profile selection/editing while this profile is being sniffed.

        Switching or editing the active profile mid-capture would leave the
        running worker matching frames against a profile it was never
        configured for. Re-enabling on stop restores exactly the buttons
        _load_current_profile() would already enable for the current
        profile/no-profile state.
        """
        self.profile_list.setEnabled(enabled)
        self.add_profile_btn.setEnabled(enabled)
        self.remove_profile_btn.setEnabled(enabled)
        self.edit_profile_btn.setEnabled(enabled and self._current_profile() is not None)

    def _refresh_sniff_cells(self, row: int, timestamp_text: str, status_text: str) -> None:
        """Update only the Timestamp/Status columns for one row, in place."""
        timestamp_item = self.register_table.item(row, _TIMESTAMP_COLUMN)
        status_item = self.register_table.item(row, _STATUS_COLUMN)
        if timestamp_item is None or status_item is None:
            return
        self.register_table.blockSignals(True)
        try:
            timestamp_item.setText(timestamp_text)
            status_item.setText(status_text)
        finally:
            self.register_table.blockSignals(False)

    def _mark_all_rows_sniff_status(self, status_text: str) -> None:
        """Set the Status column for every row without touching anything else."""
        self.register_table.blockSignals(True)
        try:
            for row in range(self.register_table.rowCount()):
                item = self.register_table.item(row, _STATUS_COLUMN)
                if item is not None:
                    item.setText(status_text)
        finally:
            self.register_table.blockSignals(False)

    def shutdown(self, timeout_ms: int = SERIAL_SHUTDOWN_TIMEOUT_MS) -> bool:
        """Stop this tab's own sniffing worker and wait for cleanup.

        Mirrors PassiveSniffingWidget.shutdown() so MainWindow.closeEvent
        can stop both tabs' capture workers the same way.
        """
        return self.sniff_capture_service.shutdown(timeout_ms)

    def _apply_filter(self, *_args) -> None:
        slave_id = self.slave_filter.currentData()
        function_code = self.function_code_filter.currentData()

        for row in range(self.register_table.rowCount()):
            item_name = self.register_table.item(row, 0)
            if item_name is None:
                self.register_table.setRowHidden(row, False)
                continue
            matches = True
            if slave_id is not None and row < len(self._row_slaves):
                matches = matches and self._row_slaves[row].get("slave_id") == slave_id
            if function_code is not None and row < len(self._row_registers):
                matches = matches and self._row_registers[row].get("function_code") == function_code
            self.register_table.setRowHidden(row, not matches)

    def reset_filters(self) -> None:
        self.slave_filter.setCurrentIndex(0)
        self.function_code_filter.setCurrentIndex(0)
        self._apply_filter()

    def add_profile_from_data(self, profile_dict: dict) -> None:
        """Add one already-built PLC profile (e.g. from a passive capture).

        Accepts either the current {"slaves": [...]} shape or a legacy
        single-slave dict — _normalize_profile() wraps the latter the same
        way loading an old profile.json does, so a captured profile and a
        profile loaded from disk are never treated differently.
        """
        if not profile_dict.get("id"):
            profile_dict["id"] = uuid.uuid4().hex
        profile_dict = self._normalize_profile(profile_dict)
        self.profiles.append(profile_dict)
        self.save_profiles_to_json()
        self._refresh_profile_list()
        self._set_active_profile(profile_dict["id"])
        self.enter_edit_mode()
