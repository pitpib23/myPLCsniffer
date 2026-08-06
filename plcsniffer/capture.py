"""Application service for receive-only Modbus RTU capture."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from serial.tools import list_ports
from serial.tools.list_ports_common import ListPortInfo

from plcsniffer.config import (
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
    validate_port,
    validate_serial_settings,
)


def list_serial_ports() -> list[ListPortInfo]:
    """Return serial ports currently visible to PySerial."""
    return list(list_ports.comports())


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
            settings: Candidate baud rates and minimum observation window.

        Raises:
            ConfigurationError: If capture is already active or settings are
                invalid.
        """
        port = validate_port(settings.port)
        baudrates = tuple(validate_baudrate(value) for value in settings.baudrates)
        if not baudrates:
            baudrates = AUTO_BAUD_RATE_PRIORITY
        if settings.minimum_window_seconds <= 0:
            raise ConfigurationError(
                "Auto-detection observation window must be greater than zero."
            )
        worker = PassiveAutoDetectThread(
            port=port,
            baudrates=baudrates,
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
