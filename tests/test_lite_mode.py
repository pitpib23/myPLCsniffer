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

from PySide6.QtWidgets import QApplication

import plcsniffer.app as app_module
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.logging_config import log_event
from plcsniffer.ui.main_window import MainWindow
from plcsniffer.ui.packet_inspector import PacketInspectorWidget
from plcsniffer.ui.passive_capture import PassiveSniffingWidget
from plcsniffer.ui.profile_tab import ProfileTab

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

    def test_lite_edition_drops_search_and_tip_adds_inspect_button(self) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            self.assertFalse(hasattr(widget, "search"))
            self.assertFalse(hasattr(widget, "_double_click_tip_label"))
            self.assertTrue(hasattr(widget, "inspect_btn"))
            # Retained touch-friendly dropdown filters.
            for attribute in (
                "direction_filter",
                "frame_type_filter",
                "slave_filter",
                "function_filter",
            ):
                self.assertTrue(hasattr(widget, attribute))
        finally:
            widget.deleteLater()
            APP.processEvents()

    def test_lite_table_columns_scroll_rather_than_shrink(self) -> None:
        widget = PassiveSniffingWidget(lite=True)
        try:
            table = widget.message_table
            total_width = sum(
                table.columnWidth(column) for column in range(table.columnCount())
            )
            self.assertGreater(total_width, 800)
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

    def test_lite_edition_keeps_all_13_register_columns(self) -> None:
        tab = self._make_tab(lite=True)
        try:
            self.assertEqual(tab.register_table.columnCount(), 13)
            for column in range(13):
                self.assertFalse(tab.register_table.isColumnHidden(column))
        finally:
            tab.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
