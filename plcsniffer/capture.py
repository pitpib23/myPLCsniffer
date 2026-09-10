"""Application service for receive-only Modbus RTU capture."""

from __future__ import annotations

import glob
import logging
import os
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

from plcsniffer.config import (
    AUTO_ALL_PARITIES,
    AUTO_ALL_STOP_BITS,
    AUTO_BAUD_RATE_PRIORITY,
    SERIAL_SHUTDOWN_TIMEOUT_MS,
    AutoDetectionSettings,
    SerialSettings,
)
from plcsniffer.exceptions import ConfigurationError
from plcsniffer.logging_config import log_event
from plcsniffer.modbus import (
    CapturedModbusFrame,
    PassiveAutoDetectThread,
    PassiveSerialReaderThread,
)
from plcsniffer.validation import (
    validate_baudrate,
    validate_parity,
    validate_port,
    validate_serial_settings,
    validate_stopbits,
)

# Onboard/GPIO UART device paths (e.g. a receive-only RS-485 HAT wired
# straight to a Raspberry Pi's GPIO header rather than through USB).
#
# /dev/ttyAMA* is the one pyserial's own Linux comports() scanner does
# glob for, but it then drops any of them whose sysfs "subsystem"
# resolves to "platform" (see serial.tools.list_ports_linux.comports/
# SysFS) — which is exactly how the Pi's built-in PL011 UART is
# registered, so it never appears there even when it's genuinely wired up
# and working.
#
# /dev/ttyAMC* is a different gap: pyserial's device-name glob list
# (ttyS*/ttyUSB*/ttyXRUSB*/ttyACM*/ttyAMA*/rfcomm*/ttyAP*) never looks for
# it at all, filter or no filter — confirmed against pyserial's own
# comports() source. Added after field-testing an actual HAT that enumerates
# as /dev/ttyAMC0 rather than /dev/ttyAMA0.
#
# USB-serial HATs (ttyACM*/ttyUSB*) aren't affected by either gap — they're
# on the "usb"/"usb-serial" subsystem and pyserial already globs for them,
# so they show up fine without any of this.
#
# Deliberately narrower than pyserial's own device list: plain
# /dev/ttyS0-ttyS31 is excluded on purpose. On most desktop/laptop Linux
# systems those legacy ISA-UART-compat nodes exist whether or not real
# hardware is attached — the exact "phantom port" case pyserial's filter
# is protecting against elsewhere — so blindly re-adding them would flood
# the full desktop edition's port list with unusable entries. ttyAMA*/
# ttyAMC* don't have that failure mode: both are SoC/HAT-specific UART
# drivers that only ever create a device node for hardware that's
# actually present/enabled.
# /dev/serial0 and /dev/serial1 are Raspberry Pi OS's own stable aliases
# for "whichever UART is actually wired to the header" (this differs by
# Pi model — some route it to ttyAMA0, others to the ttyS0 mini-UART when
# Bluetooth claims ttyAMA0); they only exist when that UART is enabled in
# config.txt, so they're just as trustworthy as a real device node.
_ONBOARD_SERIAL_GLOBS = (
    "/dev/serial0",
    "/dev/serial1",
    "/dev/ttyAMA*",
    "/dev/ttyAMC*",
)


def _onboard_serial_ports() -> list[ListPortInfo]:
    """Onboard/GPIO UARTs pyserial's own comports() never reports.

    Two different gaps, one fix: ttyAMA* is filtered out by pyserial after
    being found (see _ONBOARD_SERIAL_GLOBS above); ttyAMC* (and any future
    HAT-specific name added there) isn't even in pyserial's own device-name
    glob list, filter or no filter. Only ever reports a device that
    genuinely exists in /dev — nothing is invented — so this is exactly as
    conservative as pyserial's own approach, just reaching a couple of
    device names/cases it doesn't. A no-op wherever none of these paths
    exist (any non-Linux platform, or a Linux system with no onboard UART
    enabled).
    """
    ports: list[ListPortInfo] = []
    seen_real_paths: set[str] = set()
    for pattern in _ONBOARD_SERIAL_GLOBS:
        for device in sorted(glob.glob(pattern)):
            try:
                real_path = os.path.realpath(device)
            except OSError:
                continue
            if real_path in seen_real_paths:
                continue
            seen_real_paths.add(real_path)
            port_info = ListPortInfo(device)
            port_info.description = "Onboard/GPIO UART"
            ports.append(port_info)
    return ports


def list_serial_ports() -> list[ListPortInfo]:
    """Return serial ports currently visible to PySerial.

    Supplements pyserial's own list with onboard/GPIO UARTs it filters out
    (see _onboard_serial_ports()), deduplicated against whatever pyserial
    already reported by each device's resolved real path — so a port
    already found (under any name/symlink) is never listed twice.
    """
    ports = list(list_ports.comports())
    known_real_paths: set[str] = set()
    for port in ports:
        try:
            known_real_paths.add(os.path.realpath(port.device))
        except OSError:
            pass
    for onboard_port in _onboard_serial_ports():
        if os.path.realpath(onboard_port.device) not in known_real_paths:
            ports.append(onboard_port)
    return ports


class CaptureMode(str, Enum):
    """Supported receive-only capture setup modes."""

    CONFIGURED = "config"
    AUTOMATIC = "auto"


class PassiveCaptureService(QObject):
    """Own passive reader threads and expose UI-safe Qt signals.

    The service is the only presentation-facing component allowed to create
    serial reader threads. All emitted UI signals are relayed through this
    main-thread ``QObject``.
    """

    frame_received = Signal(object)
    status_changed = Signal(str)
    error_occurred = Signal(str)
    statistics_changed = Signal(dict)
    configuration_detected = Signal(dict)
    running_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: PassiveSerialReaderThread | PassiveAutoDetectThread | None = None

    @property
    def worker(self) -> PassiveSerialReaderThread | PassiveAutoDetectThread | None:
        """Return the active worker for diagnostics and compatibility."""
        return self._worker

    @property
    def is_running(self) -> bool:
        """Return whether a capture worker currently owns the serial resource."""
        return self._worker is not None and self._worker.isRunning()

    @staticmethod
    def available_ports() -> list[ListPortInfo]:
        """Return serial ports available for passive capture.

        Raises:
            OSError: If the operating system cannot enumerate serial ports.
        """
        try:
            return list_serial_ports()
        except (OSError, RuntimeError) as error:
            log_event(
                logging.ERROR,
                "serial_port_enumeration_failed",
                error=str(error),
            )
            raise

    def start_configured(self, settings: SerialSettings) -> None:
        """Start capture with explicit validated serial settings.

        Args:
            settings: Fixed serial framing configuration.

        Raises:
            ConfigurationError: If capture is already active or settings fail
                validation.
        """
        normalized = validate_serial_settings(settings)
        worker = PassiveSerialReaderThread(
            port=normalized.port,
            baudrate=normalized.baudrate,
            parity=normalized.parity,
            stopbits=normalized.stopbits,
            bytesize=normalized.bytesize,
            parent=self,
        )
        self._start_worker(worker, CaptureMode.CONFIGURED)

    def start_automatic(self, settings: AutoDetectionSettings) -> None:
        """Start receive-only serial-format detection on one selected port.

        Args:
            settings: Candidate baud rates, parities, stop bits, and minimum
                observation window per combination.

        Raises:
            ConfigurationError: If capture is already active or settings are
                invalid.
        """
        port = validate_port(settings.port)
        baudrates = tuple(validate_baudrate(value) for value in settings.baudrates)
        if not baudrates:
            baudrates = AUTO_BAUD_RATE_PRIORITY
        parities = tuple(validate_parity(value) for value in settings.parities)
        if not parities:
            parities = AUTO_ALL_PARITIES
        stop_bits_options = tuple(
            validate_stopbits(value) for value in settings.stop_bits_options
        )
        if not stop_bits_options:
            stop_bits_options = AUTO_ALL_STOP_BITS
        if settings.minimum_window_seconds <= 0:
            raise ConfigurationError(
                "Auto-detection observation window must be greater than zero."
            )
        worker = PassiveAutoDetectThread(
            port=port,
            baudrates=baudrates,
            parities=parities,
            stop_bits_options=stop_bits_options,
            parent=self,
            detection_window_s=settings.minimum_window_seconds,
        )
        worker.configuration_detected.connect(self._relay_configuration)
        self._start_worker(worker, CaptureMode.AUTOMATIC)

    def stop(self) -> None:
        """Request prompt cancellation of the active capture worker."""
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        log_event(logging.INFO, "capture_stop_requested")
        self.status_changed.emit("Stopping passive capture...")
        worker.stop()

    def reset_statistics(self) -> None:
        """Zero the active worker's running counters, if any are active.

        Lets the UI clear its packet table and the frame/request/response/
        error counter together, instead of the counter continuing to show
        totals from before the clear.
        """
        worker = self._worker
        if worker is None:
            return
        worker.reset_statistics()

    def shutdown(self, timeout_ms: int = SERIAL_SHUTDOWN_TIMEOUT_MS) -> bool:
        """Stop capture and wait for serial resource cleanup.

        Args:
            timeout_ms: Maximum wait time in milliseconds.

        Returns:
            ``True`` when no worker remains active.
        """
        worker = self._worker
        if worker is None or not worker.isRunning():
            return True
        worker.stop()
        stopped = worker.wait(max(0, int(timeout_ms)))
        if not stopped:
            log_event(
                logging.ERROR,
                "capture_shutdown_timeout",
                timeout_ms=timeout_ms,
            )
        return stopped

    def _start_worker(
        self,
        worker: PassiveSerialReaderThread | PassiveAutoDetectThread,
        mode: CaptureMode,
    ) -> None:
        if self._worker is not None:
            if self._worker.isRunning():
                worker.deleteLater()
                raise ConfigurationError("Passive capture is already running.")
            self._worker.deleteLater()

        self._worker = worker
        worker.frame_received.connect(self._relay_frame)
        worker.status.connect(self.status_changed)
        worker.error.connect(self._relay_error)
        worker.statistics.connect(self.statistics_changed)
        worker.finished.connect(self._on_worker_finished)
        log_event(
            logging.INFO,
            "capture_thread_start",
            mode=mode.value,
            port=worker.port,
        )
        worker.start()
        self.running_changed.emit(True)

    @Slot(object)
    def _relay_frame(self, frame: CapturedModbusFrame) -> None:
        self.frame_received.emit(frame)

    @Slot(str)
    def _relay_error(self, message: str) -> None:
        log_event(
            logging.ERROR,
            "capture_worker_error",
            message=message,
        )
        self.error_occurred.emit(message)

    @Slot(dict)
    def _relay_configuration(self, configuration: dict[str, Any]) -> None:
        self.configuration_detected.emit(configuration)

    @Slot()
    def _on_worker_finished(self) -> None:
        finished_worker = self.sender()
        if finished_worker is not self._worker:
            return
        self._worker = None
        self.running_changed.emit(False)
        log_event(logging.INFO, "capture_thread_stopped")
        finished_worker.deleteLater()
