"""Regression tests for the Profile tab's Format/Byte Order register support."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from plcsniffer.modbus import CapturedModbusFrame
from plcsniffer.ui.profile_tab import (
    _BYTE_ORDER_COLUMN,
    _BYTE_ORDER_OPTIONS_BY_WORD_COUNT,
    _FORMAT_COLUMN,
    _FORMAT_OPTIONS,
    _REGISTER_COLUMN,
    ProfileTab,
)

APP = QApplication.instance() or QApplication([])


def words_from_letters(letters: str) -> list[int]:
    """"ABCDEFGH" -> [0x4142, 0x4344, 0x4546, 0x4748].

    Matches the sequential-alphabet reference convention: each pair of
    letters is one register's two bytes, so reading a combined result back
    through the same helper turns it straight back into the letter string
    the reference tables use (e.g. "CDAB"), rather than a hex literal that
    has to be hand-verified against them.
    """
    return [
        (ord(letters[i]) << 8) | ord(letters[i + 1])
        for i in range(0, len(letters), 2)
    ]


def letters_from_combined(combined: int, byte_count: int) -> str:
    return combined.to_bytes(byte_count, "big").decode("ascii")


def frame(
    *,
    slave_id: int = 80,
    function_code: int = 3,
    address: int = 100,
    values: tuple[int, ...],
) -> CapturedModbusFrame:
    return CapturedModbusFrame(
        timestamp=time.time(),
        direction="Slave → Master",
        slave_id=slave_id,
        function_code=function_code,
        function_name="Read Holding Registers",
        frame_type="Response",
        address=address,
        quantity=len(values),
        values=values,
        raw=b"",
        description="test frame",
    )


class ProfileTabTestCase(unittest.TestCase):
    """Builds a ProfileTab backed by a throwaway profile.json.

    ProfileTab always persists to application_data_directory()/profile.json,
    which in an unpackaged dev/test run resolves to the real project-root
    profile.json — patching application_data_directory for the constructor
    call keeps these tests from ever touching that real file.
    """

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        with patch(
            "plcsniffer.ui.profile_tab.application_data_directory",
            return_value=Path(self._tempdir.name),
        ):
            self.tab = ProfileTab()

    def tearDown(self) -> None:
        self.assertTrue(self.tab.shutdown(100))
        self.tab.close()
        self.tab.deleteLater()
        APP.processEvents()
        self._tempdir.cleanup()

    def _add_profile_with_register(
        self, data_type: str, byte_order: str, address: int = 100
    ) -> dict:
        self.tab.add_profile()  # leaves the tab in edit mode on a fresh profile
        profile = self.tab._current_profile()
        # Through the real widget, like a user would set it — _sync_config_
        # fields() (wired to slave_id_spin.valueChanged) is the actual
        # source of truth for profile["slave_id"], so setting the dict
        # directly here would just get overwritten the next time any config
        # field changes.
        self.tab.slave_id_spin.setValue(80)
        register = {
            "name": "combined",
            "description": "",
            "address": address,
            "multiplier": 1.0,
            "raw_hex": "",
            "raw_value": None,
            "parsed_value": "",
            "unit": "",
            "function_code": 3,
            "data_type": data_type,
            "byte_order": byte_order,
        }
        profile.setdefault("registers", []).append(register)
        # Goes through the same _ignore_changes-guarded path production code
        # uses (_load_current_profile), rather than calling
        # _insert_register_row directly — that fires cellChanged per cell
        # with no guard here, which would immediately reset the raw
        # reading this test is about to feed in.
        self.tab._populate_register_table(profile["registers"])
        return profile


class FormatAndByteOrderDisplayTests(unittest.TestCase):
    def test_every_format_option_round_trips(self) -> None:
        for data_type, _word_count, label in _FORMAT_OPTIONS:
            self.assertEqual(ProfileTab._format_display(data_type), label)
            self.assertEqual(ProfileTab._parse_format_display(label), data_type)

    def test_every_byte_order_option_round_trips_at_every_word_count(self) -> None:
        for word_count, options in _BYTE_ORDER_OPTIONS_BY_WORD_COUNT.items():
            for byte_order, label, _letters in options:
                self.assertEqual(
                    ProfileTab._byte_order_display(byte_order, word_count), label
                )
                self.assertEqual(
                    ProfileTab._parse_byte_order_display(label, word_count), byte_order
                )

    def test_option_count_matches_reference_per_word_count(self) -> None:
        # 16-bit: Big-Endian, Little-Endian only.
        # 32-bit: + Word-Swapped, Byte-Swapped.
        # 64-bit: + Double-Word Swapped, Byte-and-Word Swapped.
        self.assertEqual(len(ProfileTab._byte_order_options_for_word_count(1)), 2)
        self.assertEqual(len(ProfileTab._byte_order_options_for_word_count(2)), 4)
        self.assertEqual(len(ProfileTab._byte_order_options_for_word_count(4)), 5)

    def test_same_operation_can_have_a_different_label_per_word_count(self) -> None:
        # "byte_swapped" is officially "Byte-Swapped" at 32-bit but
        # "Byte-and-Word Swapped" at 64-bit — same underlying math, vendor
        # naming differs by width.
        self.assertEqual(
            ProfileTab._byte_order_display("byte_swapped", 2), "Byte-Swapped"
        )
        self.assertEqual(
            ProfileTab._byte_order_display("byte_swapped", 4),
            "Byte-and-Word Swapped",
        )

    def test_missing_or_unknown_values_default_sensibly(self) -> None:
        self.assertEqual(ProfileTab._format_display(None), "16-bit Unsigned")
        self.assertEqual(ProfileTab._parse_format_display("garbage"), "uint16")
        self.assertEqual(ProfileTab._byte_order_display(None, 2), "Big-Endian")
        self.assertEqual(
            ProfileTab._parse_byte_order_display("garbage", 2), "big_endian"
        )

    def test_letter_mapping_tooltip_matches_reference(self) -> None:
        self.assertEqual(ProfileTab._byte_order_tooltip("little_endian", 1), "B A")
        self.assertEqual(
            ProfileTab._byte_order_tooltip("word_swapped", 2), "C D A B"
        )
        self.assertEqual(
            ProfileTab._byte_order_tooltip("double_word_swapped", 4),
            "E F G H A B C D",
        )


class CombineRegisterValuesTests(unittest.TestCase):
    """Verified directly against the reference tables' letter mappings —
    each case builds words from a sequential A B C D... run and checks the
    combined result reads back as the exact letter string those tables show,
    rather than a hand-derived hex literal.
    """

    def test_16_bit_big_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("AB"), "big_endian"
        )
        self.assertEqual(letters_from_combined(combined, 2), "AB")

    def test_16_bit_little_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("AB"), "little_endian"
        )
        self.assertEqual(letters_from_combined(combined, 2), "BA")

    def test_32_bit_big_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCD"), "big_endian"
        )
        self.assertEqual(letters_from_combined(combined, 4), "ABCD")

    def test_32_bit_little_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCD"), "little_endian"
        )
        self.assertEqual(letters_from_combined(combined, 4), "DCBA")

    def test_32_bit_word_swapped(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCD"), "word_swapped"
        )
        self.assertEqual(letters_from_combined(combined, 4), "CDAB")

    def test_32_bit_byte_swapped(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCD"), "byte_swapped"
        )
        self.assertEqual(letters_from_combined(combined, 4), "BADC")

    def test_64_bit_big_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCDEFGH"), "big_endian"
        )
        self.assertEqual(letters_from_combined(combined, 8), "ABCDEFGH")

    def test_64_bit_little_endian(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCDEFGH"), "little_endian"
        )
        self.assertEqual(letters_from_combined(combined, 8), "HGFEDCBA")

    def test_64_bit_word_swapped(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCDEFGH"), "word_swapped"
        )
        self.assertEqual(letters_from_combined(combined, 8), "GHEFCDAB")

    def test_64_bit_double_word_swapped(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCDEFGH"), "double_word_swapped"
        )
        self.assertEqual(letters_from_combined(combined, 8), "EFGHABCD")

    def test_64_bit_byte_swapped(self) -> None:
        combined = ProfileTab._combine_register_values(
            words_from_letters("ABCDEFGH"), "byte_swapped"
        )
        self.assertEqual(letters_from_combined(combined, 8), "BADCFEHG")


class InterpretCombinedValueTests(unittest.TestCase):
    def test_uint16(self) -> None:
        self.assertEqual(ProfileTab._interpret_combined_value(0xFFFF, "uint16"), 0xFFFF)

    def test_int16_negative(self) -> None:
        self.assertEqual(ProfileTab._interpret_combined_value(0xFFFF, "int16"), -1)

    def test_uint32(self) -> None:
        self.assertEqual(
            ProfileTab._interpret_combined_value(0xFFFFFFFF, "uint32"), 0xFFFFFFFF
        )

    def test_int32_negative(self) -> None:
        self.assertEqual(ProfileTab._interpret_combined_value(0xFFFFFFFF, "int32"), -1)

    def test_float32_known_bit_pattern(self) -> None:
        self.assertAlmostEqual(
            ProfileTab._interpret_combined_value(0x41BC0000, "float32"), 23.5
        )

    def test_uint64(self) -> None:
        self.assertEqual(
            ProfileTab._interpret_combined_value(0xFFFFFFFFFFFFFFFF, "uint64"),
            0xFFFFFFFFFFFFFFFF,
        )

    def test_int64_negative(self) -> None:
        self.assertEqual(
            ProfileTab._interpret_combined_value(0xFFFFFFFFFFFFFFFF, "int64"), -1
        )

    def test_float64_known_bit_pattern(self) -> None:
        # IEEE-754 double precision 23.5 == 0x4037800000000000
        # (struct.pack(">d", 23.5) — verified directly, not hand-derived).
        self.assertAlmostEqual(
            ProfileTab._interpret_combined_value(0x4037800000000000, "float64"), 23.5
        )


class RegisterCellDisplayTests(ProfileTabTestCase):
    def test_single_word_format_shows_plain_address(self) -> None:
        self.assertEqual(self.tab._register_range_text(48, "uint16"), "48")
        self.assertEqual(
            self.tab._register_tooltip(48, 3, "uint16"), "Address: 40049"
        )

    def test_two_word_format_shows_range(self) -> None:
        self.assertEqual(self.tab._register_range_text(48, "uint32"), "48-49")
        self.assertEqual(
            self.tab._register_tooltip(48, 3, "uint32"), "Address: 40049-40050"
        )

    def test_four_word_format_shows_range(self) -> None:
        self.assertEqual(self.tab._register_range_text(48, "uint64"), "48-51")
        self.assertEqual(
            self.tab._register_tooltip(48, 3, "uint64"), "Address: 40049-40052"
        )

    def test_blank_without_address_or_function_code(self) -> None:
        self.assertEqual(self.tab._register_range_text(None, "uint16"), "")
        self.assertEqual(self.tab._register_tooltip(None, 3, "uint16"), "")
        self.assertEqual(self.tab._register_tooltip(48, None, "uint16"), "")


class ApplyFrameToRegistersTests(ProfileTabTestCase):
    def test_uint32_register_combines_two_consecutive_addresses(self) -> None:
        self._add_profile_with_register("uint32", "big_endian")
        self.tab.set_latest_frame(frame(values=(0x0001, 0x0002)))

        profile = self.tab._current_profile()
        register = profile["registers"][0]
        self.assertEqual(register["raw_value"], 0x00010002)
        self.assertEqual(register["raw_hex"], "0x00010002")
        self.assertEqual(register["parsed_value"], str(0x00010002))
        self.assertEqual(
            self.tab.register_table.item(0, _FORMAT_COLUMN).text(), "32-bit Unsigned"
        )
        self.assertEqual(self.tab.register_table.item(0, _REGISTER_COLUMN).text(), "100-101")
        self.assertEqual(
            self.tab.register_table.item(0, _REGISTER_COLUMN).toolTip(),
            "Address: 40101-40102",
        )

    def test_uint64_register_combines_four_consecutive_addresses(self) -> None:
        self._add_profile_with_register("uint64", "big_endian", address=300)
        self.tab.set_latest_frame(
            frame(address=300, values=(0x0001, 0x0002, 0x0003, 0x0004))
        )

        register = self.tab._current_profile()["registers"][0]
        self.assertEqual(register["raw_value"], 0x0001000200030004)
        self.assertEqual(register["raw_hex"], "0x0001000200030004")

    def test_int64_register_decodes_negative_value(self) -> None:
        self._add_profile_with_register("int64", "big_endian", address=300)
        self.tab.set_latest_frame(
            frame(address=300, values=(0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF))
        )

        register = self.tab._current_profile()["registers"][0]
        self.assertEqual(register["raw_value"], -1)

    def test_float64_register_decodes_ieee754_value(self) -> None:
        self._add_profile_with_register("float64", "big_endian", address=300)
        # 100.25 == 0x4059100000000000 (struct.pack(">d", 100.25) — verified).
        self.tab.set_latest_frame(
            frame(address=300, values=(0x4059, 0x1000, 0x0000, 0x0000))
        )

        register = self.tab._current_profile()["registers"][0]
        self.assertAlmostEqual(register["raw_value"], 100.25)
        self.assertEqual(register["raw_hex"], "0x4059100000000000")

    def test_float32_register_decodes_ieee754_value(self) -> None:
        self._add_profile_with_register("float32", "big_endian")
        # 23.5 == 0x41BC0000
        self.tab.set_latest_frame(frame(values=(0x41BC, 0x0000)))

        register = self.tab._current_profile()["registers"][0]
        self.assertAlmostEqual(register["raw_value"], 23.5)
        self.assertEqual(register["parsed_value"], "23.5")

    def test_little_endian_byte_order_produces_different_value(self) -> None:
        self._add_profile_with_register("uint32", "little_endian")
        self.tab.set_latest_frame(frame(values=(0x0001, 0x0002)))

        register = self.tab._current_profile()["registers"][0]
        self.assertEqual(register["raw_value"], 0x02000100)

    def test_int16_negative_value(self) -> None:
        self._add_profile_with_register("int16", "big_endian")
        self.tab.set_latest_frame(frame(values=(0xFFFF,)))

        register = self.tab._current_profile()["registers"][0]
        self.assertEqual(register["raw_value"], -1)
        self.assertEqual(
            self.tab.register_table.item(0, _REGISTER_COLUMN).text(), "100"
        )

    def test_partial_span_in_one_frame_does_not_update(self) -> None:
        self._add_profile_with_register("uint32", "big_endian")
        # Only address 100 is present; address 101 (the other half) isn't
        # covered by this response, so nothing should be decoded yet.
        self.tab.set_latest_frame(frame(address=100, values=(0x0001,)))

        register = self.tab._current_profile()["registers"][0]
        self.assertIsNone(register["raw_value"])
        self.assertEqual(register["raw_hex"], "")

    def test_coil_function_code_is_never_combined(self) -> None:
        self._add_profile_with_register("uint32", "big_endian")
        # Read Coils (function 1) returns single-bit values; even if two
        # "registers" worth of bit values are present, they must never be
        # combined into a wider reading.
        self.tab.set_latest_frame(frame(function_code=1, values=(1, 0)))

        register = self.tab._current_profile()["registers"][0]
        self.assertIsNone(register["raw_value"])

    def test_multiplier_change_after_load_uses_stored_raw_value(self) -> None:
        self._add_profile_with_register("uint32", "big_endian")  # already in edit mode
        self.tab.set_latest_frame(frame(values=(0x0000, 0x0064)))  # 100

        register = self.tab._current_profile()["registers"][0]
        register["multiplier"] = 2.5
        self.tab._recompute_parsed_value(0, register)
        self.assertEqual(register["parsed_value"], "250")


class BackwardCompatibilityTests(ProfileTabTestCase):
    def test_old_profile_without_raw_value_field_still_recomputes(self) -> None:
        """Profiles saved before this feature existed have no raw_value key."""
        self.tab.add_profile()
        profile = self.tab._current_profile()
        old_style_register = {
            "name": "legacy",
            "description": "",
            "address": 5,
            "multiplier": 1.0,
            "raw_hex": "0x0010",  # 16
            "parsed_value": "16",
            "unit": "",
            "function_code": 3,
            # No "data_type", "byte_order", or "raw_value" keys at all.
        }
        profile.setdefault("registers", []).append(old_style_register)
        self.tab._populate_register_table(profile["registers"])

        old_style_register["multiplier"] = 2.0
        self.tab._recompute_parsed_value(0, old_style_register)
        self.assertEqual(old_style_register["parsed_value"], "32")

    def test_old_register_renders_as_16_bit_unsigned_big_endian(self) -> None:
        self.tab.add_profile()
        profile = self.tab._current_profile()
        old_style_register = {
            "name": "legacy",
            "description": "",
            "address": 5,
            "multiplier": 1.0,
            "raw_hex": "0x0010",
            "parsed_value": "16",
            "unit": "",
            "function_code": 3,
        }
        profile.setdefault("registers", []).append(old_style_register)
        self.tab._populate_register_table(profile["registers"])

        self.assertEqual(
            self.tab.register_table.item(0, _FORMAT_COLUMN).text(), "16-bit Unsigned"
        )
        self.assertEqual(
            self.tab.register_table.item(0, _REGISTER_COLUMN).text(), "5"
        )


class RegisterTableShapeTests(ProfileTabTestCase):
    def test_expected_columns_exist(self) -> None:
        self.assertEqual(self.tab.register_table.columnCount(), 12)
        labels = [
            self.tab.register_table.horizontalHeaderItem(column).text()
            for column in range(self.tab.register_table.columnCount())
        ]
        self.assertEqual(
            labels,
            [
                "Name",
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
            ],
        )

    def test_new_register_defaults_to_16_bit_unsigned_big_endian(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self.assertEqual(
            self.tab.register_table.item(0, _FORMAT_COLUMN).text(), "16-bit Unsigned"
        )
        profile = self.tab._current_profile()
        register = profile["registers"][0]
        self.assertEqual(register["data_type"], "uint16")
        self.assertEqual(register["byte_order"], "big_endian")


class ByteOrderFormatInteractionTests(ProfileTabTestCase):
    """The Byte Order column's valid options depend on the row's own
    Format, so editing Format has to keep them in sync.
    """

    def _set_format(self, row: int, label: str) -> None:
        """Edit a row's Format cell the way the real delegate commits one —
        fires cellChanged, which is what production code reacts to.
        """
        self.tab.register_table.item(row, _FORMAT_COLUMN).setText(label)

    def test_word_count_for_row_reads_the_row_own_format(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self._set_format(0, "32-bit Unsigned")

        word_count = ProfileTab._word_count_for_row(self.tab.register_table.model(), 0)
        self.assertEqual(word_count, 2)

    def test_changing_format_keeps_a_still_valid_byte_order(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self._set_format(0, "32-bit Unsigned")
        register = self.tab._current_profile()["registers"][0]
        register["byte_order"] = "little_endian"  # valid at every width

        self._set_format(0, "64-bit Unsigned")

        self.assertEqual(register["byte_order"], "little_endian")

    def test_changing_format_resets_a_no_longer_valid_byte_order(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self._set_format(0, "64-bit Unsigned")
        register = self.tab._current_profile()["registers"][0]
        register["byte_order"] = "double_word_swapped"  # only exists at 64-bit

        self._set_format(0, "16-bit Unsigned")  # only has Big/Little-Endian

        self.assertEqual(register["byte_order"], "big_endian")

    def test_byte_order_cell_label_updates_when_format_width_changes(self) -> None:
        # Same "byte_swapped" operation, different official label per width.
        self.tab.add_profile()
        self.tab.add_register()
        self._set_format(0, "32-bit Unsigned")
        register = self.tab._current_profile()["registers"][0]
        register["byte_order"] = "byte_swapped"
        self.tab._refresh_byte_order_cell(0, register)
        APP.processEvents()
        self.assertEqual(
            self.tab.register_table.item(0, _BYTE_ORDER_COLUMN).text(), "Byte-Swapped"
        )

        self._set_format(0, "64-bit Unsigned")
        register["byte_order"] = "byte_swapped"  # still valid at 64-bit
        self.tab._refresh_byte_order_cell(0, register)
        APP.processEvents()
        self.assertEqual(
            self.tab.register_table.item(0, _BYTE_ORDER_COLUMN).text(),
            "Byte-and-Word Swapped",
        )


class RegisterAddressValidationTests(ProfileTabTestCase):
    """_parse_register_address wires validate_register_address in, so an
    out-of-range or negative address is rejected the same way unparseable
    text already was, instead of being silently stored.
    """

    def test_valid_address_in_range(self) -> None:
        self.assertEqual(self.tab._parse_register_address("48"), 48)

    def test_boundary_addresses_accepted(self) -> None:
        self.assertEqual(self.tab._parse_register_address("0"), 0)
        self.assertEqual(self.tab._parse_register_address("65535"), 65535)

    def test_negative_address_rejected(self) -> None:
        self.assertIsNone(self.tab._parse_register_address("-5"))

    def test_too_large_address_rejected(self) -> None:
        self.assertIsNone(self.tab._parse_register_address("70000"))

    def test_non_numeric_text_rejected(self) -> None:
        self.assertIsNone(self.tab._parse_register_address("abc"))

    def test_editing_register_cell_with_out_of_range_address_stores_none(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        item = self.tab.register_table.item(0, _REGISTER_COLUMN)
        item.setText("-5")  # fires cellChanged -> _on_cell_changed

        register = self.tab._current_profile()["registers"][0]
        self.assertIsNone(register["address"])


class ProfileCrudWorkflowTests(ProfileTabTestCase):
    def test_add_profile_creates_untitled_profile_in_edit_mode(self) -> None:
        before = len(self.tab.profiles)
        self.tab.add_profile()

        self.assertEqual(len(self.tab.profiles), before + 1)
        self.assertTrue(self.tab._editing)
        self.assertEqual(self.tab._current_profile()["name"], "New profile")
        self.assertTrue(self.tab.config_group.isEnabled())

    def test_enter_edit_mode_snapshots_and_unlocks_controls(self) -> None:
        self.tab.add_profile()
        self.tab.save_and_exit_edit_mode()
        self.assertFalse(self.tab._editing)
        self.assertFalse(self.tab.config_group.isEnabled())

        self.tab.enter_edit_mode()
        self.assertTrue(self.tab._editing)
        self.assertTrue(self.tab.config_group.isEnabled())
        self.assertIsNotNone(self.tab._edit_snapshot)

    def test_save_and_exit_edit_mode_persists_name_and_leaves_edit_mode(self) -> None:
        self.tab.add_profile()
        self.tab.profile_name_edit.setText("Renamed profile")
        self.tab._on_profile_name_changed("Renamed profile")
        self.tab.slave_id_spin.setValue(42)

        self.tab.save_and_exit_edit_mode()

        self.assertFalse(self.tab._editing)
        profile = self.tab._current_profile()
        self.assertEqual(profile["name"], "Renamed profile")
        self.assertEqual(profile["slave_id"], 42)

    def test_discard_changes_reverts_to_snapshot(self) -> None:
        self.tab.add_profile()
        self.tab.save_and_exit_edit_mode()
        original_name = self.tab._current_profile()["name"]

        self.tab.enter_edit_mode()
        self.tab._on_profile_name_changed("Changed but not saved")
        self.tab.discard_changes()

        self.assertFalse(self.tab._editing)
        self.assertEqual(self.tab._current_profile()["name"], original_name)

    def test_add_register_appends_blank_row(self) -> None:
        self.tab.add_profile()
        self.assertEqual(self.tab.register_table.rowCount(), 0)

        self.tab.add_register()

        self.assertEqual(self.tab.register_table.rowCount(), 1)
        self.assertEqual(len(self.tab._current_profile()["registers"]), 1)

    def test_add_register_does_nothing_outside_edit_mode(self) -> None:
        self.tab.add_profile()
        self.tab.save_and_exit_edit_mode()  # leaves edit mode

        self.tab.add_register()

        self.assertEqual(self.tab.register_table.rowCount(), 0)

    def test_remove_register_removes_selected_row(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self.tab.add_register()
        self.tab.register_table.selectRow(0)

        self.tab.remove_register()

        self.assertEqual(self.tab.register_table.rowCount(), 1)
        self.assertEqual(len(self.tab._current_profile()["registers"]), 1)

    def test_remove_register_does_nothing_without_selection(self) -> None:
        self.tab.add_profile()
        self.tab.add_register()
        self.tab.register_table.clearSelection()

        self.tab.remove_register()

        self.assertEqual(self.tab.register_table.rowCount(), 1)

    def test_remove_profile_confirmed_deletes_it(self) -> None:
        self.tab.add_profile()
        self.tab.save_and_exit_edit_mode()
        profile_id = self.tab._current_profile()["id"]
        before = len(self.tab.profiles)

        with patch(
            "plcsniffer.ui.profile_tab.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ):
            self.tab.remove_profile()

        self.assertEqual(len(self.tab.profiles), before - 1)
        self.assertFalse(any(p["id"] == profile_id for p in self.tab.profiles))

    def test_remove_profile_cancelled_keeps_it(self) -> None:
        self.tab.add_profile()
        self.tab.save_and_exit_edit_mode()
        before = len(self.tab.profiles)

        with patch(
            "plcsniffer.ui.profile_tab.QMessageBox.question",
            return_value=QMessageBox.No,
        ):
            self.tab.remove_profile()

        self.assertEqual(len(self.tab.profiles), before)

    def test_reset_filters_clears_search_and_function_filter(self) -> None:
        self.tab.add_profile()
        self.tab.search_edit.setText("something")
        self.tab.function_code_filter.setCurrentIndex(1)

        self.tab.reset_filters()

        self.assertEqual(self.tab.search_edit.text(), "")
        self.assertEqual(self.tab.function_code_filter.currentIndex(), 0)

    def test_toggle_nav_panel_hides_and_restores_sidebar(self) -> None:
        # isVisible()/isHidden() only reflect real Qt visibility state once
        # the widget has actually been shown at least once.
        self.tab.show()
        APP.processEvents()
        self.assertTrue(self.tab.left_panel.isVisible())

        self.tab._toggle_nav_panel()
        self.assertFalse(self.tab.left_panel.isVisible())
        self.assertIn("Show Profiles", self.tab.toggle_nav_btn.text())

        self.tab._toggle_nav_panel()
        self.assertTrue(self.tab.left_panel.isVisible())
        self.assertIn("Hide Profiles", self.tab.toggle_nav_btn.text())

    def test_add_profile_from_data_enters_edit_mode_with_given_registers(self) -> None:
        before = len(self.tab.profiles)
        captured = {
            "name": "Slave 5 — 1 register(s)",
            "slave_id": 5,
            "registers": [
                {
                    "name": "Holding Register 0",
                    "address": 0,
                    "function_code": 3,
                    "multiplier": 1.0,
                    "raw_hex": "0x0001",
                    "parsed_value": "1",
                    "unit": "",
                    "description": "",
                }
            ],
        }

        self.tab.add_profile_from_data(captured)

        self.assertEqual(len(self.tab.profiles), before + 1)
        self.assertTrue(self.tab._editing)
        self.assertEqual(self.tab.register_table.rowCount(), 1)
        self.assertIsNotNone(self.tab._current_profile()["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
