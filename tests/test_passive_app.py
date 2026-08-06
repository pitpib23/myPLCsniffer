import csv
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from plcsniffer.ui.main_window import MainWindow
from plcsniffer.ui.log_viewer import LogViewerWidget
from plcsniffer.ui.packet_inspector import PacketInspectorWidget
from plcsniffer.ui.passive_capture import PassiveSniffingWidget
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.modbus import (
    CapturedModbusFrame,
    ModbusRTUDecoder,
    PassiveAutoDetectThread,
    crc_is_valid,
    modbus_crc,
)
from plcsniffer.logging_config import LOG_FILE_PATH, configure_logging, logger

APP = QApplication.instance() or QApplication([])


def rtu(payload: bytes) -> bytes:
    return payload + modbus_crc(payload).to_bytes(2, "little")


def request_frame(
    *,
    timestamp: float = 1.0,
    slave_id: int = 1,
    function_code: int = 3,
    function_name: str = "Read Holding Registers",
    address: int = 10,
    quantity: int = 2,
    description: str = "Observed request",
) -> CapturedModbusFrame:
    payload = (
        bytes((slave_id, function_code))
        + address.to_bytes(2, "big")
        + quantity.to_bytes(2, "big")
    )
    return CapturedModbusFrame(
        timestamp=timestamp,
        direction="Master \u2192 Slave",
        slave_id=slave_id,
        function_code=function_code,
        function_name=function_name,
        frame_type="Request",
        address=address,
        quantity=quantity,
        values=(),
        raw=rtu(payload),
        description=description,
    )


def table_values(table) -> dict[str, str]:
    return {
        table.item(row, 0).text(): table.item(row, 1).text()
        for row in range(table.rowCount())
    }


class QtWidgetTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        APP.processEvents()

    def close_widget(self, widget) -> None:
        if hasattr(widget, "stop_refresh"):
            widget.stop_refresh()
        if hasattr(widget, "shutdown"):
            self.assertTrue(widget.shutdown(100))
        widget.close()
        widget.deleteLater()
        APP.processEvents()


class DecoderTests(unittest.TestCase):
    def test_crc_matching_response_time_and_exception_code(self):
        decoder = ModbusRTUDecoder()
        request_raw = rtu(b"\x01\x03\x00\x0a\x00\x02")
        response_raw = rtu(b"\x01\x03\x04\x00\x0b\x00\x16")

        self.assertTrue(crc_is_valid(request_raw))
        request = decoder.decode(request_raw, timestamp=10.0)
        response = decoder.decode(response_raw, timestamp=10.125)

        self.assertEqual(request.frame_type, "Request")
        self.assertIsNone(request.response_time_ms)
        self.assertEqual(response.frame_type, "Response")
        self.assertEqual(response.address, 10)
        self.assertEqual(response.quantity, 2)
        self.assertEqual(response.values, (11, 22))
        self.assertAlmostEqual(response.response_time_ms, 125.0)

        decoder.decode(request_raw, timestamp=20.0)
        exception_raw = rtu(b"\x01\x83\x02")
        exception = decoder.decode(exception_raw, timestamp=20.05)
        self.assertEqual(exception.frame_type, "Exception response")
        self.assertEqual(exception.exception_code, 2)
        self.assertEqual(exception.address, 10)
        self.assertAlmostEqual(exception.response_time_ms, 50.0)

        bad_crc = response_raw[:-1] + bytes((response_raw[-1] ^ 0xFF,))
        self.assertFalse(crc_is_valid(bad_crc))
        with self.assertRaisesRegex(ValueError, "CRC"):
            decoder.decode(bad_crc)


class AutoDetectionTests(unittest.TestCase):
    def test_crc_lock_is_receive_only_and_low_baud_window_is_long_enough(self):
        request_raw = rtu(b"\x01\x03\x00\x00\x00\x01")

        class FakeSerial:
            write_calls = 0

            def __init__(self, **configuration):
                self.configuration = configuration
                self.data = request_raw

                self.closed = False

            @property
            def in_waiting(self):
                return len(self.data)

            def read(self, _size):
                data, self.data = self.data, b""
                return data

            def cancel_read(self):
                return None

            def close(self):
                self.closed = True

            def write(self, _data):
                type(self).write_calls += 1
                raise AssertionError("Passive auto detection must never write")

        worker = PassiveAutoDetectThread(
            "COM-TEST", baudrates=(9600,), detection_window_s=0.1
        )
        packets = []
        configurations = []
        worker.frame_received.connect(
            lambda packet: (packets.append(packet), worker.stop())
        )
        worker.configuration_detected.connect(configurations.append)
        with patch("plcsniffer.modbus.serial.Serial", FakeSerial):
            worker.run()

        self.assertEqual(len(packets), 1)
        self.assertEqual(configurations[0]["baudrate"], 9600)
        self.assertEqual(configurations[0]["parity"], "N")
        self.assertEqual(FakeSerial.write_calls, 0)

        worker.baudrate = 1200
        worker.parity = "N"
        worker.stopbits = 1.0
        self.assertGreater(worker._trial_window_seconds(), 2.0)


class PassiveSniffingWidgetTests(QtWidgetTestCase):
    def make_widget(self) -> PassiveSniffingWidget:
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            return PassiveSniffingWidget()

    def test_config_and_auto_mode_ui_state(self):
        widget = self.make_widget()
        try:
            self.assertEqual(widget.capture_mode_combo.currentData(), "config")
            self.assertTrue(widget.baud_combo.isEnabled())
            self.assertTrue(widget.parity_combo.isEnabled())
            self.assertTrue(widget.stopbits_combo.isEnabled())

            widget.capture_mode_combo.setCurrentIndex(
                widget.capture_mode_combo.findData("auto")
            )
            self.assertTrue(widget.port_combo.isEnabled())
            self.assertFalse(widget.baud_combo.isEnabled())
            self.assertFalse(widget.parity_combo.isEnabled())
            self.assertFalse(widget.stopbits_combo.isEnabled())
            self.assertIn("CRC-valid", widget.mode_hint.text())

            widget._on_auto_configuration(
                {"baudrate": 19200, "parity": "E", "stopbits": 2.0}
            )
            self.assertEqual(widget.baud_combo.currentText(), "19200")
            self.assertEqual(widget.parity_combo.currentData(), "E")
            self.assertEqual(widget.stopbits_combo.currentData(), 2.0)

            class RunningWorker:
                @staticmethod
                def isRunning() -> bool:
                    return True

            widget.capture_service._worker = RunningWorker()
            widget._update_mode_ui()
            self.assertFalse(widget.capture_mode_combo.isEnabled())
            self.assertFalse(widget.port_combo.isEnabled())
            widget.capture_service._worker = None
        finally:
            self.close_widget(widget)

    def test_combined_filters_user_role_and_inspection_signal(self):
        widget = self.make_widget()
        try:
            matching = request_frame(description="Temperature process value")
            wrong_direction = CapturedModbusFrame(
                timestamp=2.0,
                direction="Slave \u2192 Master",
                slave_id=1,
                function_code=3,
                function_name="Read Holding Registers",
                frame_type="Response",
                address=10,
                quantity=2,
                values=(11, 22),
                raw=rtu(b"\x01\x03\x04\x00\x0b\x00\x16"),
                description="Temperature response",
                request_timestamp=1.0,
            )
            wrong_slave = request_frame(
                timestamp=3.0,
                slave_id=2,
                function_code=4,
                function_name="Read Input Registers",
                description="Pressure process value",
            )
            for frame in (matching, wrong_direction, wrong_slave):
                widget._on_frame(frame)

            stored = widget.message_table.item(0, 0).data(Qt.UserRole)
            self.assertIs(stored, matching)

            widget.search.setText("temperature")
            widget.direction_filter.setCurrentIndex(
                widget.direction_filter.findData("Master \u2192 Slave")
            )
            widget.frame_type_filter.setCurrentIndex(
                widget.frame_type_filter.findData("Request")
            )
            widget.slave_filter.setCurrentIndex(widget.slave_filter.findData(1))
            widget.function_filter.setCurrentIndex(widget.function_filter.findData(3))
            widget._apply_filter()

            self.assertFalse(widget.message_table.isRowHidden(0))
            self.assertTrue(widget.message_table.isRowHidden(1))
            self.assertTrue(widget.message_table.isRowHidden(2))
            self.assertEqual(widget.filter_count_label.text(), "Showing 1 of 3 packets")
            self.assertTrue(widget.reset_filters_btn.isEnabled())

            inspected = []
            widget.packet_inspection_requested.connect(inspected.append)
            widget.message_table.cellDoubleClicked.emit(0, 8)
            self.assertEqual(inspected, [matching])

            widget.reset_filters()
            self.assertEqual(widget.filter_count_label.text(), "Showing 3 of 3 packets")
            self.assertFalse(widget.reset_filters_btn.isEnabled())
            self.assertFalse(
                any(
                    widget.message_table.isRowHidden(row)
                    for row in range(widget.message_table.rowCount())
                )
            )
        finally:
            self.close_widget(widget)

    def test_pause_queue_and_csv_export(self):
        widget = self.make_widget()
        try:
            first = request_frame(description="First queued packet")
            second = request_frame(
                timestamp=2.0,
                slave_id=2,
                description="Second queued packet",
            )

            widget.pause_btn.setChecked(True)
            widget._on_frame(first)
            widget._on_frame(second)
            self.assertEqual(widget.message_table.rowCount(), 0)
            self.assertEqual(list(widget._pending_frames), [first, second])
            self.assertIn("2 packet(s) waiting", widget.status_label.text())

            widget.pause_btn.setChecked(False)
            self.assertEqual(widget.message_table.rowCount(), 2)
            self.assertEqual(list(widget._pending_frames), [])

            with tempfile.TemporaryDirectory() as directory:
                export_path = Path(directory) / "capture.csv"
                widget.export_csv(export_path)
                with export_path.open(newline="", encoding="utf-8-sig") as export_file:
                    rows = list(csv.reader(export_file))

            self.assertEqual(len(rows), 3)
            self.assertEqual(
                rows[0][0:4], ["Timestamp", "Direction", "Slave", "Function"]
            )
            self.assertIn("Read Holding Registers", rows[1][3])
            self.assertEqual(rows[1][8], first.raw_hex)
        finally:
            self.close_widget(widget)


class PacketInspectorTests(QtWidgetTestCase):
    def test_important_information_and_show_all_toggle(self):
        decoder = ModbusRTUDecoder()
        decoder.decode(rtu(b"\x07\x03\x00\x20\x00\x01"), timestamp=4.0)
        packet = decoder.decode(rtu(b"\x07\x03\x02\x12\x34"), timestamp=4.25)
        inspector = PacketInspectorWidget()
        try:
            self.assertFalse(inspector.show_all_btn.isEnabled())
            self.assertTrue(inspector.all_information_group.isHidden())

            inspector.inspect_packet(packet)
            important = table_values(inspector.summary_table)
            self.assertEqual(important["Frame type"], "Response")
            self.assertEqual(important["Start address"], "32")
            self.assertEqual(important["Decoded values"], "4660")
            self.assertEqual(important["CRC status"], "Valid")
            self.assertEqual(important["Response time"], "250.000 ms")
            self.assertEqual(important["Raw RTU frame"], packet.raw_hex)

            inspector.show_all_btn.setChecked(True)
            self.assertFalse(inspector.all_information_group.isHidden())
            self.assertEqual(
                inspector.show_all_btn.text(), "Show Important Information Only"
            )
            complete = table_values(inspector.full_details_table)
            self.assertEqual(complete["CRC valid"], "True")
            self.assertEqual(complete["Matched request timestamp"], "4.000000")
            self.assertEqual(inspector.byte_table.rowCount(), len(packet.raw))
            self.assertIn("Slave", inspector.byte_table.item(0, 4).text())

            inspector.show_all_btn.setChecked(False)
            self.assertTrue(inspector.all_information_group.isHidden())
            self.assertEqual(
                inspector.show_all_btn.text(), "Show All Packet Information"
            )
        finally:
            self.close_widget(inspector)


class MainWindowTests(QtWidgetTestCase):
    def test_exact_tabs_and_double_click_routes_to_inspector(self):
        with patch.object(PassiveCaptureService, "available_ports", return_value=[]):
            window = MainWindow()
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
            self.assertEqual(window.tabs.currentIndex(), 0)

            packet = request_frame(slave_id=9, description="Route this packet")
            window.passive_page._on_frame(packet)
            window.passive_page.message_table.cellDoubleClicked.emit(0, 0)
            self.assertEqual(window.tabs.currentIndex(), 1)
            self.assertIs(window.packet_inspector.current_packet, packet)
            self.assertIn("slave 9", window.packet_inspector.help_label.text())
        finally:
            window.logging_page.stop_refresh()
            window.close()
            window.deleteLater()
            APP.processEvents()


class LoggingConfigurationTests(unittest.TestCase):
    def test_midnight_rotation_and_five_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary_log = Path(directory) / "test.log"
            try:
                handler = configure_logging(temporary_log)
                self.assertIsInstance(
                    handler, logging.handlers.TimedRotatingFileHandler
                )
                self.assertEqual(handler.when, "MIDNIGHT")
                self.assertEqual(handler.interval, 24 * 60 * 60)
                self.assertEqual(handler.backupCount, 5)
                self.assertFalse(handler.utc)
                self.assertEqual(Path(handler.baseFilename), temporary_log.resolve())
                self.assertIs(configure_logging(temporary_log), handler)
                self.assertEqual(logger.handlers.count(handler), 1)

                logger.info("rotation configuration test")
                handler.flush()
                self.assertIn(
                    "rotation configuration test",
                    temporary_log.read_text(encoding="utf-8"),
                )
            finally:
                configure_logging(LOG_FILE_PATH)


class LogViewerTests(QtWidgetTestCase):
    def test_append_truncate_and_replace_without_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "current.log"
            log_path.write_text("first line\n", encoding="utf-8")
            viewer = LogViewerWidget(log_path=log_path, auto_start=False)
            try:
                self.assertFalse(viewer.timer.isActive())
                self.assertEqual(viewer.log_view.toPlainText(), "first line\n")

                with log_path.open("ab") as handle:
                    handle.write("second line \u03c0\n".encode("utf-8"))
                viewer.refresh_log()
                self.assertEqual(
                    viewer.log_view.toPlainText(),
                    "first line\nsecond line \u03c0\n",
                )

                log_path.write_text("short\n", encoding="utf-8")
                viewer.refresh_log()
                self.assertEqual(viewer.log_view.toPlainText(), "short\n")

                replacement = Path(directory) / "replacement.log"
                replacement.write_text("replacement generation\n", encoding="utf-8")
                os.replace(replacement, log_path)
                viewer.refresh_log()
                self.assertEqual(
                    viewer.log_view.toPlainText(), "replacement generation\n"
                )
                self.assertIn("Live", viewer.status_label.text())
            finally:
                self.close_widget(viewer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
