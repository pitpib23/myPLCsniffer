"""Regression tests for capture ownership and responsive tab layout."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QScrollArea

from plcsniffer.config import (
    MAIN_WINDOW_MINIMUM_HEIGHT,
    MAIN_WINDOW_MINIMUM_WIDTH,
    SerialSettings,
)
from plcsniffer.modbus import (
    PassiveAutoDetectThread,
    PassiveSerialReaderThread,
    modbus_crc,
)
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.logging_config import log_event
from plcsniffer.ui import main_window as main_window_module
from plcsniffer.ui.main_window import MainWindow
from plcsniffer.ui.profile_tab import ProfileTab

APPLICATION = QApplication.instance() or QApplication([])


class AutomaticCaptureTests(unittest.TestCase):
    """Verify Auto mode remains passive and retains one open handle."""

    def test_auto_mode_reconfigures_one_handle_and_never_writes(self) -> None:
        raw_frame = b"\x01\x03\x00\x00\x00\x01"
        raw_frame += modbus_crc(raw_frame).to_bytes(2, "little")

        class FakeSerial:
            instances: list["FakeSerial"] = []
            write_calls = 0

            def __init__(self, **configuration) -> None:
                type(self).instances.append(self)
                self.configuration = configuration
                self.data = raw_frame
                self.closed = False

            @property
            def in_waiting(self) -> int:
                return len(self.data)

            def read(self, _size: int) -> bytes:
                data, self.data = self.data, b""
                return data

            def reset_input_buffer(self) -> None:
                return None

            def cancel_read(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            def write(self, _data: bytes) -> None:
                type(self).write_calls += 1
                raise AssertionError("Auto sniffing must never transmit")

        worker = PassiveAutoDetectThread(
            "COM-TEST",
            baudrates=(9_600,),
            detection_window_s=0.1,
        )
        frames = []
        configurations: list[dict] = []
        worker.frame_received.connect(
            lambda frame: (frames.append(frame), worker.stop())
        )
        worker.configuration_detected.connect(configurations.append)

        with patch(
            "plcsniffer.modbus.serial.Serial",
            FakeSerial,
        ):
            worker.run()
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(FakeSerial.instances), 1)
        self.assertEqual(FakeSerial.write_calls, 0)
        self.assertTrue(FakeSerial.instances[0].closed)
        self.assertIsNone(worker._serial)
        self.assertEqual(len(configurations), 1)
        self.assertEqual(configurations[0]["tested_baudrates"], (9600,))

    def test_configured_mode_starts_without_a_logging_name_error(self) -> None:
        service = PassiveCaptureService()
        running_states: list[bool] = []
        service.running_changed.connect(running_states.append)

        with patch.object(PassiveSerialReaderThread, "start") as start:
            service.start_configured(
                SerialSettings(
                    port="COM-TEST",
                    baudrate=9_600,
                    parity="N",
                    stopbits=1,
                )
            )

        start.assert_called_once_with()
        self.assertIsInstance(service.worker, PassiveSerialReaderThread)
        self.assertEqual(running_states, [True])
        service.worker.deleteLater()


class ResponsiveLayoutTests(unittest.TestCase):
    """Verify data-heavy tabs scroll and retain useful table heights."""

    def test_capture_and_inspector_tabs_are_scrollable(self) -> None:
        with patch.object(
            PassiveCaptureService,
            "available_ports",
            return_value=[],
        ):
            window = MainWindow()
        try:
            self.assertIsInstance(window.passive_tab, QScrollArea)
            self.assertIsInstance(window.packet_inspector_tab, QScrollArea)
            # Lite's default construction size (800x480) computes as
            # COMPACT (see responsive.compute_mode) — verify its own,
            # still-readable floor here rather than the desktop/NORMAL one.
            self.assertGreaterEqual(
                window.passive_page.message_table.minimumHeight(),
                300,
            )
            self.assertGreaterEqual(
                window.packet_inspector.byte_table.minimumHeight(),
                150,
            )

            # The original desktop-class floor must still apply once the
            # window is actually NORMAL-sized (e.g. Lite run on a desktop
            # during development, or a Pi with a larger attached monitor).
            window.resize(1400, 900)
            APPLICATION.processEvents()
            window._resize_debounce_timer.stop()
            window._recompute_responsive_mode()
            self.assertGreaterEqual(
                window.passive_page.message_table.minimumHeight(),
                500,
            )
            self.assertGreaterEqual(
                window.packet_inspector.byte_table.minimumHeight(),
                300,
            )
        finally:
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()

    def test_main_window_shrinks_to_the_small_screen_floor(self) -> None:
        """Regression guard for the Raspberry Pi-class minimum size.

        Without this, a future edit could silently raise the floor back
        toward desktop-only sizes (as plcsniffer/ui/main_window.py's own
        history already did once) with no test catching it.
        """
        with patch.object(
            PassiveCaptureService,
            "available_ports",
            return_value=[],
        ):
            window = MainWindow()
        try:
            self.assertEqual(window.minimumWidth(), MAIN_WINDOW_MINIMUM_WIDTH)
            self.assertEqual(window.minimumHeight(), MAIN_WINDOW_MINIMUM_HEIGHT)
            self.assertLessEqual(MAIN_WINDOW_MINIMUM_WIDTH, 800)
            self.assertLessEqual(MAIN_WINDOW_MINIMUM_HEIGHT, 480)

            window.resize(800, 480)
            APPLICATION.processEvents()
            self.assertEqual(window.size().width(), 800)
            self.assertEqual(window.size().height(), 480)
        finally:
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()


class LiteUiSurfaceTests(unittest.TestCase):
    """Regression guards for the Lite edition's specific removals/retentions.

    These pin the task's own checklist: no Logging tab/LogViewerWidget in
    Lite's navigation (backend logging keeps working regardless), no
    keyboard-driven free-text search, and the "scroll, don't shrink" table
    strategy for Passive Sniffing/Packet Inspector/Profile.
    """

    def test_log_viewer_widget_is_never_imported_by_main_window(self) -> None:
        """Removing the Logging tab also means avoiding constructing
        LogViewerWidget at all — this guards against a future edit
        reintroducing the import (and the tab) by accident."""
        self.assertFalse(hasattr(main_window_module, "LogViewerWidget"))

    def test_main_window_has_no_logging_page(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow()
        try:
            self.assertFalse(hasattr(window, "logging_page"))
            self.assertEqual(len(window._responsive_tabs), 3)
        finally:
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()

    def test_backend_logging_still_writes_regardless_of_the_missing_tab(self) -> None:
        """Removing the Logging tab must never disable the logging backend
        itself — log_event() must keep working exactly as it does on the
        `pi` branch."""
        try:
            log_event(__import__("logging").INFO, "lite_ui_surface_test_event")
        except Exception as error:  # pragma: no cover - defensive
            self.fail(f"log_event() must keep working without the Logging tab: {error}")

    def test_passive_sniffing_has_no_free_text_search_widget(self) -> None:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow()
        try:
            self.assertFalse(hasattr(window.passive_page, "search"))
            # Retained touch-friendly dropdown filters must still exist.
            for attribute in (
                "direction_filter",
                "frame_type_filter",
                "slave_filter",
                "function_filter",
            ):
                self.assertTrue(hasattr(window.passive_page, attribute))
        finally:
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()

    def test_profile_tab_has_no_free_text_search_widget(self) -> None:
        tab = ProfileTab()
        try:
            self.assertFalse(hasattr(tab, "search_edit"))
            # Retained touch-friendly dropdown filters must still exist.
            self.assertTrue(hasattr(tab, "slave_filter"))
            self.assertTrue(hasattr(tab, "function_code_filter"))
            # Keyboard input required to actually author profile data is
            # NOT removed — only the keyboard-heavy convenience search is.
            self.assertTrue(hasattr(tab, "profile_name_edit"))
        finally:
            tab.deleteLater()
            APPLICATION.processEvents()

    def test_passive_table_columns_scroll_rather_than_shrink(self) -> None:
        """All 8 Passive Sniffing columns keep their existing readable
        widths (including Raw RTU Frame, no longer stretched to fill
        whatever's left) — their sum naturally exceeds an 800px viewport,
        which is what makes the horizontal scrollbar the actual mechanism
        rather than a fallback that never engages."""
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow()
        try:
            table = window.passive_page.message_table
            total_width = sum(
                table.columnWidth(column) for column in range(table.columnCount())
            )
            self.assertGreater(total_width, 800)
        finally:
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()

    def test_profile_register_table_keeps_all_13_columns(self) -> None:
        """Lite's decision (see profile_tab.py) is to keep every column
        visible and rely on horizontal scrolling, rather than hiding
        secondary columns and risking an inaccessible edit workflow."""
        tab = ProfileTab()
        try:
            self.assertEqual(tab.register_table.columnCount(), 13)
            for column in range(13):
                self.assertFalse(tab.register_table.isColumnHidden(column))
        finally:
            tab.deleteLater()
            APPLICATION.processEvents()


if __name__ == "__main__":
    unittest.main()
