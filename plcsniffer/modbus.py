from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import product

import serial
from PySide6.QtCore import QThread, Signal

from plcsniffer.config import (
    AUTO_ALL_PARITIES,
    AUTO_ALL_STOP_BITS,
    AUTO_BAUD_RATE_PRIORITY,
    AUTO_FRAME_MARGIN_SECONDS,
    AUTO_FRAMING_PRIORITY,
    AUTO_RETRY_DELAY_SECONDS,
    DEFAULT_AUTO_DETECTION_WINDOW_SECONDS,
    MINIMUM_FRAME_GAP_SECONDS,
    MODBUS_MAX_RTU_FRAME_BYTES,
    MODBUS_MAX_UNIT_ID,
    MODBUS_MIN_UNIT_ID,
    SERIAL_READ_TIMEOUT_SECONDS,
    SerialSettings,
)
from plcsniffer.logging_config import log_event, logger
from plcsniffer.validation import validate_serial_settings

FUNCTION_NAMES = {
    1: "Read Coils",
    2: "Read Discrete Inputs",
    3: "Read Holding Registers",
    4: "Read Input Registers",
    5: "Write Single Coil",
    6: "Write Single Register",
    15: "Write Multiple Coils",
    16: "Write Multiple Registers",
}

# Short noun for the register type a function code operates on, used to
# build a human-readable default register name (e.g. "Holding Register 48")
# instead of a generic "Reg 48" that hides what kind of point it is.
REGISTER_TYPE_NOUNS = {
    1: "Coil",
    2: "Discrete Input",
    3: "Holding Register",
    4: "Input Register",
    5: "Coil",
    6: "Holding Register",
    15: "Coil",
    16: "Holding Register",
}

# Standard Modicon reference-number block offsets, keyed by function code:
# the classic 0xxxx/1xxxx/3xxxx/4xxxx addressing every PLC vendor documents
# registers in. Added to the zero-based wire address and zero-padded to 5
# digits (e.g. FC 03 address 0 -> "40001", FC 01 address 5 -> "00006").
MODICON_BLOCK_OFFSET = {
    1: 1,       # Read Coils              -> 0xxxx
    2: 10001,   # Read Discrete Inputs    -> 1xxxx
    3: 40001,   # Read Holding Registers  -> 4xxxx
    4: 30001,   # Read Input Registers    -> 3xxxx
    5: 1,       # Write Single Coil       -> 0xxxx
    6: 40001,   # Write Single Register   -> 4xxxx
    15: 1,      # Write Multiple Coils    -> 0xxxx
    16: 40001,  # Write Multiple Registers -> 4xxxx
}


def modbus_crc(data: bytes) -> int:
    """Return the Modbus RTU CRC-16 for *data*."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def crc_is_valid(frame: bytes) -> bool:
    """Return True when a complete RTU frame has a valid little-endian CRC."""
    return len(frame) >= 4 and int.from_bytes(frame[-2:], "little") == modbus_crc(
        frame[:-2]
    )


@dataclass(frozen=True)
class PendingRequest:
    """Request metadata retained while waiting for a matching response."""

    slave_id: int
    function_code: int
    start_address: int | None
    quantity: int | None
    timestamp: float


@dataclass(frozen=True)
class CapturedModbusFrame:
    """One CRC-valid Modbus RTU frame observed by the passive reader."""

    timestamp: float
    direction: str
    slave_id: int
    function_code: int
    function_name: str
    frame_type: str
    address: int | None
    quantity: int | None
    values: tuple[int, ...]
    raw: bytes
    description: str
    request_timestamp: float | None = None
    exception_code: int | None = None

    @property
    def timestamp_text(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S.%f")[:-3]

    @property
    def function_text(self) -> str:
        return f"{self.function_code:02d} - {self.function_name}"

    @property
    def raw_hex(self) -> str:
        return self.raw.hex(" ").upper()

    @property
    def values_text(self) -> str:
        return ", ".join(str(value) for value in self.values) if self.values else "—"

    @property
    def response_time_ms(self) -> float | None:
        if self.request_timestamp is None:
            return None
        return max(0.0, (self.timestamp - self.request_timestamp) * 1000.0)


class ModbusRTUDecoder:
    """Match observed Modbus requests and responses without transmitting data."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, int], PendingRequest] = {}

    def decode(
        self, frame: bytes, timestamp: float | None = None
    ) -> CapturedModbusFrame:
        if not crc_is_valid(frame):
            raise ValueError("Invalid or incomplete Modbus RTU frame CRC.")
        timestamp = time.time() if timestamp is None else timestamp
        slave_id = frame[0]
        raw_function = frame[1]
        function_code = raw_function & 0x7F
        function_name = FUNCTION_NAMES.get(function_code, "Unknown Function")
        if function_code not in FUNCTION_NAMES:
            log_event(
                logging.WARNING,
                "unsupported_function_code",
                slave_id=slave_id,
                function_code=function_code,
            )
        key = (slave_id, function_code)
        pending = self._pending.get(key)

        if raw_function & 0x80:
            exception = frame[2] if len(frame) >= 5 else 0
            self._pending.pop(key, None)
            request_quantity = (
                None
                if pending is not None and function_code in (5, 6)
                else pending.quantity if pending else None
            )
            request_values = (
                (pending.quantity,)
                if pending is not None and function_code in (5, 6)
                else ()
            )
            return CapturedModbusFrame(
                timestamp,
                "Slave → Master",
                slave_id,
                function_code,
                function_name,
                "Exception response",
                pending.start_address if pending else None,
                request_quantity,
                request_values,
                frame,
                f"Exception code {exception}",
                request_timestamp=pending.timestamp if pending else None,
                exception_code=exception,
            )

        if pending is not None and self._looks_like_response(frame, function_code):
            self._pending.pop(key, None)
            values = self._decode_response_values(frame, pending)
            response_quantity = None if function_code in (5, 6) else pending.quantity
            return CapturedModbusFrame(
                timestamp,
                "Slave → Master",
                slave_id,
                function_code,
                function_name,
                "Response",
                pending.start_address,
                response_quantity,
                values,
                frame,
                "Matched response",
                request_timestamp=pending.timestamp,
            )

        request = self._decode_request(frame, timestamp)
        if request is not None:
            self._pending[key] = request
            request_quantity = None if function_code in (5, 6) else request.quantity
            request_values = self._decode_request_values(frame, request)
            return CapturedModbusFrame(
                timestamp,
                "Master → Slave",
                slave_id,
                function_code,
                function_name,
                "Request",
                request.start_address,
                request_quantity,
                request_values,
                frame,
                "Observed request",
            )

        return CapturedModbusFrame(
            timestamp,
            "Unknown",
            slave_id,
            function_code,
            function_name,
            "Unmatched frame",
            None,
            None,
            (),
            frame,
            "Valid CRC but no matching request was observed",
        )

    @staticmethod
    def _decode_request(frame: bytes, timestamp: float) -> PendingRequest | None:
        function_code = frame[1]
        if function_code in (1, 2, 3, 4, 5, 6) and len(frame) == 8:
            value = int.from_bytes(frame[4:6], "big")
            if function_code in (1, 2) and not 1 <= value <= 2000:
                return None
            if function_code in (3, 4) and not 1 <= value <= 125:
                return None
            if function_code == 5 and value not in (0x0000, 0xFF00):
                return None
            return PendingRequest(
                frame[0],
                function_code,
                int.from_bytes(frame[2:4], "big"),
                value,
                timestamp,
            )
        if function_code in (15, 16) and len(frame) >= 9:
            byte_count = frame[6]
            quantity = int.from_bytes(frame[4:6], "big")
            expected_bytes = (
                (quantity + 7) // 8 if function_code == 15 else quantity * 2
            )
            maximum = 1968 if function_code == 15 else 123
            if (
                1 <= quantity <= maximum
                and byte_count == expected_bytes
                and len(frame) == 9 + byte_count
            ):
                return PendingRequest(
                    frame[0],
                    function_code,
                    int.from_bytes(frame[2:4], "big"),
                    quantity,
                    timestamp,
                )
        return None

    @staticmethod
    def _decode_request_values(
        frame: bytes, request: PendingRequest
    ) -> tuple[int, ...]:
        if request.function_code in (5, 6):
            return (request.quantity,) if request.quantity is not None else ()
        if request.function_code == 15 and request.quantity is not None:
            bits = tuple((byte >> bit) & 1 for byte in frame[7:-2] for bit in range(8))
            return bits[: request.quantity]
        if request.function_code == 16:
            data = frame[7:-2]
            return tuple(
                int.from_bytes(data[index : index + 2], "big")
                for index in range(0, len(data), 2)
            )
        return ()

    @staticmethod
    def _looks_like_response(frame: bytes, function_code: int) -> bool:
        if function_code in (1, 2, 3, 4):
            return len(frame) >= 5 and len(frame) == 5 + frame[2]
        return function_code in (5, 6, 15, 16) and len(frame) == 8

    @staticmethod
    def _decode_response_values(
        frame: bytes, request: PendingRequest
    ) -> tuple[int, ...]:
        if request.function_code in (3, 4):
            data = frame[3:-2]
            if len(data) % 2:
                return ()
            return tuple(
                int.from_bytes(data[index : index + 2], "big")
                for index in range(0, len(data), 2)
            )
        if request.function_code in (1, 2):
            bits = tuple((byte >> bit) & 1 for byte in frame[3:-2] for bit in range(8))
            return bits[: request.quantity]
        if request.function_code in (5, 6):
            return (int.from_bytes(frame[4:6], "big"),)
        return ()

    def candidate_lengths(self, data: bytes) -> list[int]:
        """Return likely lengths for a frame beginning at data[0]."""
        if len(data) < 2:
            return []
        slave_id, raw_function = data[0], data[1]
        function_code = raw_function & 0x7F
        if raw_function & 0x80:
            return [5]
        pending = (slave_id, function_code) in self._pending
        candidates: list[int] = []
        if function_code in (1, 2, 3, 4):
            if pending and len(data) >= 3:
                candidates.append(5 + data[2])
            candidates.append(8)
            if not pending and len(data) >= 3:
                candidates.append(5 + data[2])
        elif function_code in (5, 6):
            candidates.append(8)
        elif function_code in (15, 16):
            if pending:
                candidates.append(8)
            if len(data) >= 7:
                candidates.append(9 + data[6])
            candidates.append(8)
        return list(
            dict.fromkeys(
                length
                for length in candidates
                if 4 <= length <= MODBUS_MAX_RTU_FRAME_BYTES
            )
        )

    def split_frames(self, data: bytes) -> tuple[list[bytes], bytes]:
        """Extract CRC-valid RTU frames, preserving an incomplete trailing frame."""
        frames: list[bytes] = []
        offset = 0
        while len(data) - offset >= 4:
            remaining = data[offset:]
            match = None
            candidates = self.candidate_lengths(remaining)
            for length in candidates:
                if len(remaining) >= length and crc_is_valid(remaining[:length]):
                    match = remaining[:length]
                    break
            if match is None:
                if any(length > len(remaining) for length in candidates):
                    break
                if len(remaining) < 8:
                    break
                offset += 1
                continue
            frames.append(match)
            offset += len(match)
        return frames, data[offset:]


class PassiveSerialReaderThread(QThread):
    """Read an existing serial bus in the background; this class never writes."""

    frame_received = Signal(object)
    status = Signal(str)
    error = Signal(str)
    statistics = Signal(dict)

    def __init__(
        self,
        port: str,
        baudrate: int,
        parity: str = "N",
        stopbits: float = 1,
        bytesize: int = 8,
        parent=None,
    ) -> None:
        super().__init__(parent)
        settings = validate_serial_settings(
            SerialSettings(port, baudrate, parity, stopbits, bytesize)
        )
        self.port = settings.port
        self.baudrate = settings.baudrate
        self.parity = settings.parity
        self.stopbits = settings.stopbits
        self.bytesize = settings.bytesize
        self._stop_event = threading.Event()
        self._serial: serial.Serial | None = None
        self._decoder = ModbusRTUDecoder()
        self._counts = {
            "frames": 0,
            "requests": 0,
            "responses": 0,
            "crc_errors": 0,
            "unmatched": 0,
        }

    def stop(self) -> None:
        self._stop_event.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.cancel_read()
            except (AttributeError, OSError, serial.SerialException) as error:
                log_event(
                    logging.DEBUG,
                    "serial_cancel_read_failed",
                    port=self.port,
                    error=str(error),
                )

    def reset_statistics(self) -> None:
        """Zero the running frame/request/response/error counters.

        Called from the main thread (see PassiveCaptureService.reset_statistics)
        when the displayed packet table is cleared, so the footer count matches
        the now-empty table instead of continuing to show the prior total.
        """
        self._counts = {
            "frames": 0,
            "requests": 0,
            "responses": 0,
            "crc_errors": 0,
            "unmatched": 0,
        }
        self.statistics.emit(dict(self._counts))

    def _publish(self, frame: CapturedModbusFrame) -> None:
        self._counts["frames"] += 1
        if frame.frame_type == "Request":
            self._counts["requests"] += 1
        elif frame.frame_type in {"Response", "Exception response"}:
            self._counts["responses"] += 1
        else:
            self._counts["unmatched"] += 1
        self.frame_received.emit(frame)
        self.statistics.emit(dict(self._counts))

    def _decode_and_publish(self, raw: bytes) -> None:
        try:
            self._publish(self._decoder.decode(raw))
            log_event(
                logging.INFO,
                "passive_modbus_frame_received",
                port=self.port,
                byte_count=len(raw),
                raw_hex=raw.hex(" ").upper(),
            )
        except (IndexError, ValueError) as error:
            self._counts["crc_errors"] += 1
            log_event(
                logging.WARNING,
                "passive_modbus_frame_rejected",
                port=self.port,
                error=str(error),
            )

    def _consume(self, buffer: bytes, final: bool = False) -> bytes:
        frames, remainder = self._decoder.split_frames(buffer)
        for raw in frames:
            self._decode_and_publish(raw)
        if final and remainder:
            self._publish_crc_error(remainder)
            return b""
        return remainder

    def _publish_crc_error(self, raw: bytes) -> None:
        """Surface bytes that never resolved into a valid CRC as a flagged row.

        Kept as a real, timestamped table entry (frame_type "CRC Error")
        instead of only a status-bar message, so a stray bad byte isn't
        silently dropped — the row itself is the permanent record. Reported
        through the `status` signal, not `error`: the capture is still
        running fine at this point (more frames keep arriving after this),
        so it shouldn't paint the status bar red — `error` stays reserved
        for failures that actually stop capture (port disconnected, etc.).
        """
        self._counts["frames"] += 1
        self._counts["crc_errors"] += 1
        frame = CapturedModbusFrame(
            timestamp=time.time(),
            direction="Unknown",
            slave_id=raw[0] if raw else 0,
            function_code=raw[1] if len(raw) >= 2 else 0,
            function_name="CRC error",
            frame_type="CRC Error",
            address=None,
            quantity=None,
            values=(),
            raw=raw,
            description=(
                f"Ignored {len(raw)} byte(s) without a valid Modbus CRC. Check "
                "baud rate, parity, stop bits, and A/B polarity."
            ),
        )
        self.frame_received.emit(frame)
        self.statistics.emit(dict(self._counts))
        self.status.emit(
            f"Ignored {len(raw)} byte(s) without a valid Modbus CRC — flagged "
            "in the table below."
        )
        log_event(
            logging.WARNING,
            "passive_modbus_frame_rejected",
            port=self.port,
            byte_count=len(raw),
            raw_hex=raw.hex(" ").upper(),
        )

    def _capture_loop(self, serial_port, initial_buffer: bytes = b"") -> None:
        buffer = initial_buffer
        last_data = time.monotonic()
        character_bits = (
            1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits
        )
        frame_gap = max(MINIMUM_FRAME_GAP_SECONDS, 3.5 * character_bits / self.baudrate)
        while not self._stop_event.is_set():
            chunk = serial_port.read(serial_port.in_waiting or 1)
            now = time.monotonic()
            if chunk:
                buffer += chunk
                last_data = now
                buffer = self._consume(buffer)
            elif buffer and now - last_data >= frame_gap:
                buffer = self._consume(buffer, final=True)

    def _open_serial(self):
        return serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=SERIAL_READ_TIMEOUT_SECONDS,
            write_timeout=0,
        )

    def run(self) -> None:
        try:
            self._serial = self._open_serial()
            self.status.emit(
                f"Listening only on {self.port} at {self.baudrate} baud; "
                "no data will be transmitted."
            )
            log_event(
                logging.INFO,
                "serial_port_opened",
                port=self.port,
                baudrate=self.baudrate,
                parity=self.parity,
                stopbits=self.stopbits,
            )
            self._capture_loop(self._serial)
        except (OSError, serial.SerialException) as error:
            if not self._stop_event.is_set():
                self.error.emit(
                    f"Could not monitor {self.port}: {error}. Close other serial "
                    "programs and verify the adapter and permissions."
                )
                log_event(
                    logging.ERROR,
                    "serial_connection_failed",
                    port=self.port,
                    error=str(error),
                )
        except Exception as error:
            if not self._stop_event.is_set():
                self.error.emit(f"Passive monitor stopped unexpectedly: {error}")
                logger.exception(
                    "Passive Modbus monitor failed unexpectedly on port %s",
                    self.port,
                )
        finally:
            self._close_serial()
            log_event(logging.INFO, "serial_port_closed", port=self.port)
            if self._stop_event.is_set():
                self.status.emit("Passive monitor stopped.")

    def _close_serial(self) -> None:
        serial_port = self._serial
        self._serial = None
        if serial_port is not None:
            try:
                serial_port.close()
            except (OSError, serial.SerialException) as error:
                log_event(
                    logging.WARNING,
                    "serial_port_close_failed",
                    port=self.port,
                    error=str(error),
                )


class PassiveAutoDetectThread(PassiveSerialReaderThread):
    """Detect common serial formats from traffic, then capture without writing."""

    configuration_detected = Signal(dict)

    DEFAULT_BAUDRATES = AUTO_BAUD_RATE_PRIORITY

    def __init__(
        self,
        port: str,
        baudrates=DEFAULT_BAUDRATES,
        parent=None,
        *,
        parities=AUTO_ALL_PARITIES,
        stop_bits_options=AUTO_ALL_STOP_BITS,
        detection_window_s: float = DEFAULT_AUTO_DETECTION_WINDOW_SECONDS,
    ) -> None:
        normalized = tuple(dict.fromkeys(int(value) for value in baudrates))
        if not normalized or any(value <= 0 for value in normalized):
            raise ValueError(
                "At least one positive auto-detection baud rate is required."
            )
        normalized_parities = tuple(dict.fromkeys(str(value) for value in parities))
        if not normalized_parities:
            raise ValueError("At least one auto-detection parity is required.")
        normalized_stop_bits = tuple(
            dict.fromkeys(float(value) for value in stop_bits_options)
        )
        if not normalized_stop_bits:
            raise ValueError("At least one auto-detection stop-bit option is required.")
        preferred = tuple(
            value for value in self.DEFAULT_BAUDRATES if value in normalized
        ) + tuple(value for value in normalized if value not in self.DEFAULT_BAUDRATES)
        super().__init__(port, preferred[0], parent=parent)
        self.baudrates = preferred
        self.parities = normalized_parities
        self.stop_bits_options = normalized_stop_bits
        self._tested_baudrates: list[int] = []
        self.detection_window_s = max(0.1, float(detection_window_s))
        # Grows by one second every time a full sweep across every candidate
        # setting finds nothing (see run()'s retry branch), so traffic that's
        # sent less often than the base window — e.g. once every 2s against a
        # sub-second default — eventually gets caught instead of the sweep
        # moving on before a message ever arrives. Reset per Start click,
        # since each Start creates a fresh thread instance.
        self._window_growth_s: float = 0.0

    def _candidate_settings(self):
        # Filters the shared priority-ordered list down to only the parity/
        # stop-bit combinations actually requested (see AutoDetectionSettings),
        # instead of always sweeping all nine — narrower selections mean fewer
        # combinations per baud rate and a faster sweep.
        framing = tuple(
            (parity, stopbits)
            for parity, stopbits in AUTO_FRAMING_PRIORITY
            if parity in self.parities and stopbits in self.stop_bits_options
        )
        return product(self.baudrates, framing)

    def _trial_window_seconds(self) -> float:
        character_bits = (
            1 + self.bytesize + (0 if self.parity == "N" else 1) + self.stopbits
        )
        longest_frame_time = MODBUS_MAX_RTU_FRAME_BYTES * character_bits / self.baudrate
        effective_window = self.detection_window_s + self._window_growth_s
        return max(effective_window, longest_frame_time + AUTO_FRAME_MARGIN_SECONDS)

    @staticmethod
    def _is_plausible_modbus_frame(raw: bytes) -> bool:
        if len(raw) < 4 or not MODBUS_MIN_UNIT_ID <= raw[0] <= MODBUS_MAX_UNIT_ID:
            return False
        return (raw[1] & 0x7F) in FUNCTION_NAMES and crc_is_valid(raw)

    def _detect_on_open_port(
        self, serial_port
    ) -> tuple[list[bytes], bytes, ModbusRTUDecoder]:
        decoder = ModbusRTUDecoder()
        buffer = b""
        deadline = time.monotonic() + self._trial_window_seconds()
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            chunk = serial_port.read(serial_port.in_waiting or 1)
            if not chunk:
                continue
            buffer += chunk
            frames, remainder = decoder.split_frames(buffer)
            valid_frames = [
                raw for raw in frames if self._is_plausible_modbus_frame(raw)
            ]
            if valid_frames:
                return valid_frames, remainder, decoder
            buffer = remainder
        return [], b"", decoder

    def _configure_open_serial(
        self,
        serial_port,
        baudrate: int,
        parity: str,
        stopbits: float,
    ) -> bool:
        """Apply one candidate without closing and reopening the adapter."""
        try:
            serial_port.baudrate = baudrate
            serial_port.parity = parity
            serial_port.stopbits = stopbits
            reset_input_buffer = getattr(serial_port, "reset_input_buffer", None)
            if callable(reset_input_buffer):
                reset_input_buffer()
        except (OSError, ValueError, serial.SerialException) as error:
            log_event(
                logging.WARNING,
                "auto_candidate_configuration_failed",
                port=self.port,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                error=str(error),
            )
            return False
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        return True

    def run(self) -> None:
        locked = False
        try:
            self._serial = self._open_serial()
            log_event(logging.INFO, "auto_detection_started", port=self.port)
            while not self._stop_event.is_set() and not locked:
                successful_trials = 0
                for baudrate, (parity, stopbits) in self._candidate_settings():
                    if self._stop_event.is_set():
                        break
                    if not self._configure_open_serial(
                        self._serial, baudrate, parity, stopbits
                    ):
                        continue
                    if baudrate not in self._tested_baudrates:
                        self._tested_baudrates.append(baudrate)
                    successful_trials += 1
                    self.status.emit(
                        f"Auto mode testing {baudrate} baud, parity {parity}, "
                        f"{stopbits:g} stop bit(s) — receive-only."
                    )
                    frames, remainder, decoder = self._detect_on_open_port(self._serial)
                    if not frames:
                        continue

                    locked = True
                    self._decoder = decoder
                    configuration = {
                        "port": self.port,
                        "baudrate": self.baudrate,
                        "parity": self.parity,
                        "stopbits": self.stopbits,
                        "bytesize": self.bytesize,
                        "tested_baudrates": tuple(self._tested_baudrates),
                    }
                    self.configuration_detected.emit(configuration)
                    tested_rates = ", ".join(str(value) for value in self._tested_baudrates)
                    self.status.emit(
                        f"Auto mode detected {self.baudrate} baud, "
                        f"parity {self.parity}, {self.stopbits:g} stop bit(s). "
                        f"Tested baud rates: {tested_rates}. Listening only."
                    )
                    log_event(
                        logging.INFO,
                        "auto_detection_locked",
                        **configuration,
                    )
                    for raw in frames:
                        self._decode_and_publish(raw)
                    self._capture_loop(self._serial, remainder)
                    break

                if successful_trials == 0 and not self._stop_event.is_set():
                    self.error.emit(
                        f"Could not configure {self.port} for any supported "
                        "auto-detection setting."
                    )
                    return
                if not locked and not self._stop_event.is_set():
                    self._window_growth_s += 1.0
                    next_window = self.detection_window_s + self._window_growth_s
                    self.status.emit(
                        "No valid Modbus packet found in this auto-detection pass; "
                        f"increasing listen time to {next_window:g}s per setting "
                        "and retrying."
                    )
                    log_event(
                        logging.INFO,
                        "auto_detection_window_increased",
                        port=self.port,
                        window_seconds=next_window,
                    )
                    self._stop_event.wait(AUTO_RETRY_DELAY_SECONDS)
        except (OSError, serial.SerialException) as error:
            if not self._stop_event.is_set():
                self.error.emit(
                    f"Could not open {self.port} for auto detection: {error}. "
                    "Close other serial programs and verify the adapter."
                )
                log_event(
                    logging.ERROR,
                    "auto_detection_connection_failed",
                    port=self.port,
                    error=str(error),
                )
        except Exception as error:
            if not self._stop_event.is_set():
                self.error.emit(f"Passive auto mode stopped unexpectedly: {error}")
                log_event(
                    logging.ERROR,
                    "auto_detection_unexpected_failure",
                    port=self.port,
                    error=str(error),
                )
                logger.exception("Auto detection traceback for %s", self.port)
        finally:
            self._close_serial()
            log_event(logging.INFO, "auto_detection_stopped", port=self.port)
            if self._stop_event.is_set():
                self.status.emit("Passive monitor stopped.")
