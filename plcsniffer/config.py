"""Central application configuration and immutable defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIRECTORY = PROJECT_ROOT / "assets"
DEFAULT_ICON_PATH = ASSETS_DIRECTORY / "myplcsniffer.ico"

APPLICATION_NAME = "Passive PLC Sniffer"
APPLICATION_LOGGER_NAME = "PLCSniffer"
LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "plcsniffer.log"
LOG_BACKUP_COUNT = 5
LOG_REFRESH_INTERVAL_MS = 1_000
LOG_VIEW_INITIAL_TAIL_BYTES = 2 * 1024 * 1024

DEFAULT_BAUD_RATES = (
    1_200,
    2_400,
    4_800,
    9_600,
    19_200,
    38_400,
    57_600,
    115_200,
)
AUTO_BAUD_RATE_PRIORITY = (
    9_600,
    19_200,
    38_400,
    115_200,
    57_600,
    4_800,
    2_400,
    1_200,
)
VALID_PARITIES = frozenset({"N", "E", "O", "M", "S"})
VALID_STOP_BITS = frozenset({1.0, 1.5, 2.0})
MODBUS_MIN_UNIT_ID = 0
MODBUS_MAX_UNIT_ID = 247
MODBUS_MAX_RTU_FRAME_BYTES = 260
SERIAL_READ_TIMEOUT_SECONDS = 0.02
MINIMUM_FRAME_GAP_SECONDS = 0.004
DEFAULT_AUTO_DETECTION_WINDOW_SECONDS = 0.65
AUTO_FRAME_MARGIN_SECONDS = 0.05
AUTO_RETRY_DELAY_SECONDS = 0.05

CAPTURE_TABLE_MINIMUM_HEIGHT = 560
INSPECTOR_SUMMARY_MINIMUM_HEIGHT = 400
INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT = 340
TAB_CONTENT_MINIMUM_WIDTH = 1_100
TAB_CONTENT_MINIMUM_HEIGHT = 900
# Smaller than TAB_CONTENT_MINIMUM_HEIGHT on purpose: the Profile tab's
# register table scrolls internally, so the tab only needs enough floor for
# its toolbar/config controls plus a handful of table rows — not a full
# page's worth — before its own QTableWidget scrollbar takes over.
PROFILE_TAB_MINIMUM_HEIGHT = 620
FILTER_DEBOUNCE_MS = 150
MAX_PAUSED_PACKETS = 10_000
SERIAL_SHUTDOWN_TIMEOUT_MS = 3_000


@dataclass(frozen=True)
class SerialSettings:
    """Validated serial framing settings.

    Attributes:
        port: Operating-system serial port name.
        baudrate: Serial bit rate.
        parity: PySerial parity code.
        stopbits: Number of serial stop bits.
        bytesize: Number of data bits.
    """

    port: str
    baudrate: int
    parity: str = "N"
    stopbits: float = 1.0
    bytesize: int = 8


@dataclass(frozen=True)
class AutoDetectionSettings:
    """Settings used by receive-only serial-format detection."""

    port: str
    baudrates: tuple[int, ...] = AUTO_BAUD_RATE_PRIORITY
    minimum_window_seconds: float = DEFAULT_AUTO_DETECTION_WINDOW_SECONDS
