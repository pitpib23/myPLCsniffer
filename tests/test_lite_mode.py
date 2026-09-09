"""Regression tests for the --lite CLI flag / MainWindow(lite=...) mode.

The full desktop edition (lite=False, the default — no flag) must stay
byte-identical in behavior to how this app worked before Lite existed; the
Lite edition (lite=True, `python main.py --lite`) is a presentation-only
Raspberry Pi 7" touchscreen variant of the same backend. These tests pin
both the differences and the fact that everything else is shared.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QScroller

import plcsniffer.app as app_module
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.logging_config import log_event
from plcsniffer.ui.main_window import MainWindow
from plcsniffer.ui.packet_inspector import PacketInspectorWidget
from plcsniffer.ui.passive_capture import PassiveSniffingWidget
from plcsniffer.ui.profile_tab import _FORMAT_COLUMN, ProfileTab

APP = QApplication.instance() or QApplication([])


class RunFlagParsingTests(unittest.TestCase):
    """run() is the one place --lite is recognized and stripped.

    Every test here spies on create_main_window to also capture the real
    MainWindow it builds (run() never closes it itself — that's normally
    QApplication.exec()'s job) and explicitly tears it down afterward:
    left running, its log-refresh QTimer (full edition only) keeps firing
    across later tests and can raise once other widgets are torn down.
    """

    def _run_and_capture(self, argv: list[str]) -> dict:
        captured: dict = {}
        original_create = app_module.create_main_window

        def spy_create(arguments=None, *, lite=False):
            captured["arguments"] = list(arguments) if arguments is not None else None
            captured["lite"] = lite
            application, window = original_create(arguments, lite=lite)
            captured["window"] = window
            return application, window

        with patch.object(app_module, "create_main_window", spy_create), patch.object(
            QApplication, "exec", return_value=0
        ):
            captured["exit_code"] = app_module.run(argv)
        return captured

    def _close_window(self, window) -> None:
        if hasattr(window, "logging_page"):
            window.logging_page.stop_refresh()
        window.close()
        window.deleteLater()
        APP.processEvents()

    def test_lite_flag_selects_lite_and_is_not_forwarded_to_qapplication(self) -> None:
        captured = self._run_and_capture(["main.py", "--lite"])
        try:
            self.assertEqual(captured["exit_code"], 0)
            self.assertTrue(captured["lite"])
            self.assertNotIn("--lite", captured["arguments"])
        finally:
            self._close_window(captured["window"])

    def test_no_flag_defaults_to_full_edition(self) -> None:
        captured = self._run_and_capture(["main.py"])
        try:
            self.assertFalse(captured["lite"])
        finally:
            self._close_window(captured["window"])

    def test_other_qt_arguments_pass_through_alongside_lite(self) -> None:
        captured = self._run_and_capture(["main.py", "--lite", "-style=fusion"])
        try:
            self.assertEqual(captured["arguments"], ["main.py", "-style=fusion"])
        finally:
            self._close_window(captured["window"])


class MainWindowLiteModeTests(unittest.TestCase):
    def test_full_edition_is_unchanged_from_before_lite_existed(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow()  # lite defaults to False
        try:
            self.assertEqual(window.tabs.count(), 4)
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(4)],
                [
                    "1. Passive Sniffing",
                    "2. Packet Inspector",
                    "3. Logging",
                    "4. Profile",
                ],
            )
            self.assertTrue(hasattr(window, "logging_page"))
            self.assertEqual(window.size().width(), 1400)
            self.assertEqual(window.size().height(), 900)
        finally:
            window.logging_page.stop_refresh()
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_lite_edition_exposes_exactly_sniff_inspect_profile(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=True)
        try:
            self.assertEqual(window.tabs.count(), 3)
            self.assertEqual(
                [window.tabs.tabText(index) for index in range(3)],
                ["Sniff", "Inspect", "Profile"],
            )
            self.assertFalse(hasattr(window, "logging_page"))
            self.assertEqual(len(window._responsive_tabs), 3)
            self.assertEqual(window.size().width(), 800)
            self.assertEqual(window.size().height(), 480)
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_lite_close_event_does_not_touch_logging_page(self) -> None:
        """Regression guard: closeEvent must not assume logging_page exists."""
        from PySide6.QtGui import QCloseEvent

        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=True)
        try:
            event = QCloseEvent()
            window.closeEvent(event)  # must not raise AttributeError
            self.assertTrue(event.isAccepted())
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_backend_logging_still_writes_in_both_modes(self) -> None:
        try:
            log_event(__import__("logging").INFO, "lite_mode_test_event")
        except Exception as error:  # pragma: no cover - defensive
            self.fail(f"log_event() must keep working regardless of lite: {error}")


class PassiveSniffingWidgetLiteModeTests(unittest.TestCase):
    def test_full_edition_keeps_the_search_field_and_tip_label(self) -> None:
        widget = PassiveSniffingWidget(lite=False)
        try:
            self.assertTrue(hasattr(widget, "search"))
            self.assertTrue(hasattr(widget, "_double_click_tip_label"))
            self.assertFalse(hasattr(widget, "inspect_btn"))
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_edition_drops_search_tip_and_direction_type_filters(self) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            self.assertFalse(hasattr(widget, "search"))
            self.assertFalse(hasattr(widget, "_double_click_tip_label"))
            self.assertFalse(hasattr(widget, "inspect_btn"))
            # Direction/Frame Type filtering and Reset Filters are gone —
            # only Slave and Function remain.
            self.assertFalse(hasattr(widget, "direction_filter"))
            self.assertFalse(hasattr(widget, "frame_type_filter"))
            self.assertFalse(hasattr(widget, "reset_filters_btn"))
            self.assertTrue(hasattr(widget, "slave_filter"))
            self.assertTrue(hasattr(widget, "function_filter"))
            # No More▾ menu — Export CSV is a standalone button instead;
            # Copy Selected (the menu's other action) is dropped entirely.
            self.assertFalse(hasattr(widget, "more_btn"))
            self.assertFalse(hasattr(widget, "copy_action"))
            self.assertTrue(hasattr(widget, "export_csv_btn"))
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_table_visible_columns_are_not_squeezed(self) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            table = widget.message_table
            visible_width = sum(
                table.columnWidth(column)
                for column in range(table.columnCount())
                if not table.isColumnHidden(column)
            )
            # Whatever remains visible still keeps its full, readable width
            # (not compressed to fit) — horizontal scroll (still enabled;
            # see _build_ui) is what reaches anything beyond it.
            self.assertGreaterEqual(visible_width, 725)
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_hides_direction_and_count_keeps_the_rest(self) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            table = widget.message_table
            hidden = {column for column in range(8) if table.isColumnHidden(column)}
            self.assertEqual(hidden, {1, 6})  # Direction, Count
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_function_column_shows_only_the_code(self) -> None:
        from plcsniffer.modbus import CapturedModbusFrame

        widget = PassiveSniffingWidget(lite=True)
        try:
            frame = CapturedModbusFrame(
                timestamp=1.0,
                direction="Master → Slave",
                slave_id=1,
                function_code=3,
                function_name="Read Holding Registers",
                frame_type="Request",
                address=0,
                quantity=1,
                values=(),
                raw=b"\x01\x03\x00\x00\x00\x01\x84\x0a",
                description="a",
            )
            widget._on_frame(frame)
            self.assertEqual(widget.message_table.item(0, 3).text(), "03")
            self.assertIn(
                "Read Holding Registers", widget.message_table.item(0, 3).toolTip()
            )
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_full_edition_still_shows_direction_count_and_full_function_name(
        self,
    ) -> None:
        from plcsniffer.modbus import CapturedModbusFrame

        widget = PassiveSniffingWidget(lite=False)
        try:
            table = widget.message_table
            for column in range(table.columnCount()):
                self.assertFalse(table.isColumnHidden(column))
            frame = CapturedModbusFrame(
                timestamp=1.0,
                direction="Master → Slave",
                slave_id=1,
                function_code=3,
                function_name="Read Holding Registers",
                frame_type="Request",
                address=0,
                quantity=1,
                values=(),
                raw=b"\x01\x03\x00\x00\x00\x01\x84\x0a",
                description="a",
            )
            widget._on_frame(frame)
            self.assertEqual(table.item(0, 3).text(), "03 - Read Holding Registers")
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_actions_row_puts_export_and_save_on_the_right_of_pause_clear(
        self,
    ) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            grid = widget._actions_grid
            pause_row, pause_col, _, _ = grid.getItemPosition(
                grid.indexOf(widget.pause_btn)
            )
            clear_row, clear_col, _, _ = grid.getItemPosition(
                grid.indexOf(widget.clear_btn)
            )
            export_row, export_col, _, _ = grid.getItemPosition(
                grid.indexOf(widget.export_csv_btn)
            )
            save_row, save_col, _, _ = grid.getItemPosition(
                grid.indexOf(widget.save_profile_btn)
            )
            info_row, info_col, _, _ = grid.getItemPosition(
                grid.indexOf(widget.save_profile_info_icon)
            )
            # All one row.
            self.assertEqual(
                {pause_row, clear_row, export_row, save_row, info_row}, {0}
            )
            # Pause/Clear on the left, Export CSV/Save as Profile/info icon
            # to the right of them, in that order.
            self.assertLess(pause_col, clear_col)
            self.assertLess(clear_col, export_col)
            self.assertLess(export_col, save_col)
            self.assertLess(save_col, info_col)
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_slave_and_function_filters_still_work(self) -> None:
        from plcsniffer.modbus import CapturedModbusFrame

        widget = PassiveSniffingWidget(lite=True)
        try:
            frame_a = CapturedModbusFrame(
                timestamp=1.0,
                direction="Master → Slave",
                slave_id=1,
                function_code=3,
                function_name="Read Holding Registers",
                frame_type="Request",
                address=0,
                quantity=1,
                values=(),
                raw=b"\x01\x03\x00\x00\x00\x01\x84\x0a",
                description="a",
            )
            frame_b = CapturedModbusFrame(
                timestamp=2.0,
                direction="Master → Slave",
                slave_id=2,
                function_code=4,
                function_name="Read Input Registers",
                frame_type="Request",
                address=0,
                quantity=1,
                values=(),
                raw=b"\x02\x04\x00\x00\x00\x01\x71\xca",
                description="b",
            )
            widget._on_frame(frame_a)
            widget._on_frame(frame_b)

            widget.slave_filter.setCurrentIndex(widget.slave_filter.findData(1))
            widget._apply_filter()
            self.assertFalse(widget.message_table.isRowHidden(0))
            self.assertTrue(widget.message_table.isRowHidden(1))

            widget.reset_filters()
            self.assertFalse(widget.message_table.isRowHidden(0))
            self.assertFalse(widget.message_table.isRowHidden(1))
        finally:
            widget.deleteLater()
            APP.processEvents()


class PacketInspectorLiteModeTests(unittest.TestCase):
    def test_full_edition_keeps_byte_table_horizontal_scroll_off(self) -> None:
        from PySide6.QtCore import Qt

        widget = PacketInspectorWidget(lite=False)
        try:
            self.assertEqual(
                widget.byte_table.horizontalScrollBarPolicy(),
                Qt.ScrollBarAlwaysOff,
            )
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_edition_enables_byte_table_horizontal_scroll(self) -> None:
        from PySide6.QtCore import Qt

        widget = PacketInspectorWidget(lite=True)
        try:
            self.assertEqual(
                widget.byte_table.horizontalScrollBarPolicy(),
                Qt.ScrollBarAsNeeded,
            )
        finally:
            widget.deleteLater()
            APP.processEvents()


class ProfileTabLiteModeTests(unittest.TestCase):
    def _make_tab(self, *, lite: bool) -> ProfileTab:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        with patch(
            "plcsniffer.ui.profile_tab.application_data_directory",
            return_value=Path(tempdir.name),
        ):
            return ProfileTab(lite=lite)

    def test_full_edition_keeps_the_search_field(self) -> None:
        tab = self._make_tab(lite=False)
        try:
            self.assertTrue(hasattr(tab, "search_edit"))
        finally:
            tab.deleteLater()
            APP.processEvents()

    def test_lite_edition_drops_the_search_field(self) -> None:
        tab = self._make_tab(lite=True)
        try:
            self.assertFalse(hasattr(tab, "search_edit"))
            self.assertTrue(hasattr(tab, "slave_filter"))
            self.assertTrue(hasattr(tab, "function_code_filter"))
            # Keyboard input required to actually author profile data is
            # NOT removed — only the keyboard-heavy convenience search is.
            self.assertTrue(hasattr(tab, "profile_name_edit"))
        finally:
            tab.deleteLater()
            APP.processEvents()

    def test_lite_edition_defaults_to_showing_every_column(self) -> None:
        """Lite shows all 13 register_table columns by default ("Show all
        columns" starts checked); unchecking switches to a denser identity
        + value only view (Name/Slave ID/Register/Parsed Value/Unit/
        Status) — either way nothing is ever removed, only hidden."""
        tab = self._make_tab(lite=True)
        try:
            self.assertEqual(tab.register_table.columnCount(), 13)
            self.assertTrue(tab.show_all_columns_checkbox.isChecked())
            for column in range(13):
                self.assertFalse(tab.register_table.isColumnHidden(column))

            tab.show_all_columns_checkbox.setChecked(False)
            visible = {
                column
                for column in range(13)
                if not tab.register_table.isColumnHidden(column)
            }
            self.assertEqual(visible, {0, 1, 5, 8, 9, 12})

            tab.show_all_columns_checkbox.setChecked(True)
            for column in range(13):
                self.assertFalse(tab.register_table.isColumnHidden(column))
        finally:
            tab.deleteLater()
            APP.processEvents()

    def test_full_edition_shows_all_13_register_columns_with_no_toggle(self) -> None:
        tab = self._make_tab(lite=False)
        try:
            self.assertEqual(tab.register_table.columnCount(), 13)
            for column in range(13):
                self.assertFalse(tab.register_table.isColumnHidden(column))
            self.assertFalse(hasattr(tab, "show_all_columns_checkbox"))
        finally:
            tab.deleteLater()
            APP.processEvents()

    def test_hiding_a_column_never_touches_its_underlying_register_data(self) -> None:
        """Hidden columns are a view-only change — editing/decoding still
        works on data in columns the user currently can't see, exactly the
        way it already works while scrolled off-screen."""
        tab = self._make_tab(lite=True)
        try:
            tab.add_profile()  # enters edit mode on a fresh profile
            tab.add_register()
            profile = tab._current_profile()
            register = profile["slaves"][0]["registers"][0]
            self.assertEqual(register["data_type"], "uint16")
            self.assertEqual(register["byte_order"], "big_endian")
            self.assertEqual(register["multiplier"], 1.0)
            # Format (hidden by default) still holds the real stored value.
            format_item = tab.register_table.item(0, _FORMAT_COLUMN)
            self.assertEqual(format_item.text(), "16-bit Unsigned")
        finally:
            tab.deleteLater()
            APP.processEvents()


class ProfileSidebarToggleRegressionTests(unittest.TestCase):
    """Regression guard: expanding the sidebar must never claim 100% of the
    splitter's width.

    Lite's Sniff/Inspect/Profile tabs auto-collapse this sidebar as part of
    MainWindow's very first apply_responsive_mode() call, which runs before
    the window is ever shown — the splitter isn't laid out yet, so its
    sizes() at that point is degenerate ([0, 0]). Recording that as the
    "restore to this" split and later replaying it via setSizes() handed
    the sidebar the entire splitter and squeezed the register table/summary
    to zero width the first time a user reopened it.
    """

    def _open_profile_tab(self, *, lite: bool):
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=lite)
        window.show()
        APP.processEvents()
        window.tabs.setCurrentWidget(window.profile_tab_widget)
        APP.processEvents()
        return window

    def test_lite_reopening_the_sidebar_leaves_the_register_table_usable(self) -> None:
        window = self._open_profile_tab(lite=True)
        try:
            profile_tab = window.profile_tab
            # Lite starts with the sidebar auto-collapsed (COMPACT at
            # 800x480) — reopening it must give the right panel a real,
            # usable share of the width, not zero.
            self.assertFalse(profile_tab._sidebar_visible)
            profile_tab.toggle_nav_btn.click()
            APP.processEvents()
            self.assertTrue(profile_tab._sidebar_visible)
            self.assertGreater(profile_tab.right_panel.width(), 300)
            self.assertGreater(profile_tab.left_panel.width(), 0)
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_lite_sidebar_survives_repeated_toggling(self) -> None:
        window = self._open_profile_tab(lite=True)
        try:
            profile_tab = window.profile_tab
            for _ in range(4):
                profile_tab.toggle_nav_btn.click()
                APP.processEvents()
                if profile_tab._sidebar_visible:
                    self.assertGreater(profile_tab.right_panel.width(), 300)
                else:
                    self.assertEqual(profile_tab.left_panel.width(), 0)
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_full_edition_toggle_still_restores_its_real_captured_sizes(self) -> None:
        """Regression guard for the full edition's existing behavior, which
        already worked correctly (the sidebar starts visible at the full
        edition's 1400x900 default, so a real split is always captured
        before the first hide) — must not be disturbed by the Lite fix."""
        window = self._open_profile_tab(lite=False)
        try:
            profile_tab = window.profile_tab
            self.assertTrue(profile_tab._sidebar_visible)
            original_sizes = profile_tab.splitter.sizes()

            profile_tab.toggle_nav_btn.click()  # hide
            APP.processEvents()
            profile_tab.toggle_nav_btn.click()  # show again
            APP.processEvents()

            self.assertEqual(profile_tab.splitter.sizes(), original_sizes)
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()


class DragToScrollTests(unittest.TestCase):
    """Lite must be draggable like a phone screen — both when the touch
    panel reports genuine multi-touch (QScroller.TouchGesture) and when it
    reports single-touch as plain mouse events instead
    (QScroller.LeftMouseButtonGesture), which is common on Linux/X11
    without a touch protocol registered. Full edition must never grab
    either, so desktop mouse drag-to-select stays exactly as it was."""

    def test_lite_grabs_both_gesture_types_on_every_scrollable_surface(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=True)
        try:
            surfaces = [
                window.passive_tab.viewport(),
                window.packet_inspector_tab.viewport(),
                window.profile_tab_widget.viewport(),
                window.passive_page.message_table.viewport(),
                window.packet_inspector.byte_table.viewport(),
                window.profile_tab.profile_list.viewport(),
            ]
            for surface in surfaces:
                self.assertTrue(
                    QScroller.hasScroller(surface),
                    f"expected a QScroller grabbed on {surface!r}",
                )
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_lite_profile_register_table_has_no_scroller_of_its_own(self) -> None:
        """One scrollable layer, not two: register_table has no internal
        scrollbars/QScroller — it's sized to its full content instead (see
        ProfileTab._fit_register_table_to_content) so the outer
        profile_tab_widget QScrollArea is the only thing that scrolls."""
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=True)
        try:
            from PySide6.QtCore import Qt

            table = window.profile_tab.register_table
            self.assertFalse(QScroller.hasScroller(table.viewport()))
            self.assertEqual(table.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(table.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
            self.assertEqual(table.minimumHeight(), table.maximumHeight())
            self.assertEqual(table.minimumWidth(), table.maximumWidth())
        finally:
            window.close()
            window.deleteLater()
            APP.processEvents()

    def test_full_edition_grabs_no_scrollers_anywhere(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow(lite=False)
        try:
            surfaces = [
                window.passive_tab.viewport(),
                window.packet_inspector_tab.viewport(),
                window.profile_tab_widget.viewport(),
                window.passive_page.message_table.viewport(),
                window.packet_inspector.byte_table.viewport(),
                window.profile_tab.register_table.viewport(),
                window.profile_tab.profile_list.viewport(),
            ]
            for surface in surfaces:
                self.assertFalse(
                    QScroller.hasScroller(surface),
                    f"did not expect a QScroller grabbed on {surface!r}",
                )
        finally:
            window.logging_page.stop_refresh()
            window.close()
            window.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
