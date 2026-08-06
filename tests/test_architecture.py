"""Regression tests for capture ownership and responsive tab layout."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QScrollArea

from plcsniffer.config import SerialSettings
from plcsniffer.modbus import (
    PassiveAutoDetectThread,
    PassiveSerialReaderThread,
    modbus_crc,
)
from plcsniffer.capture import PassiveCaptureService
from plcsniffer.ui.main_window import MainWindow

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
            self.assertGreaterEqual(
                window.passive_page.message_table.minimumHeight(),
                500,
            )
            self.assertGreaterEqual(
                window.packet_inspector.byte_table.minimumHeight(),
                300,
            )
        finally:
            window.logging_page.stop_refresh()
            window.close()
            window.deleteLater()
            APPLICATION.processEvents()


if __name__ == "__main__":
    unittest.main()
