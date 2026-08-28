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
# Every parity/stop-bit combination Auto mode is willing to try, in the
# priority order it tries them: 1 stop bit across all parities first (most
# common framing), then 2, then 1.5. AutoDetectionSettings lets a caller
# narrow which combinations actually get swept — see
# PassiveAutoDetectThread._candidate_settings(), which filters this list
# down to the requested parities/stop-bit options rather than recomputing
# the order itself.
AUTO_FRAMING_PRIORITY = (
    ("N", 1.0), ("E", 1.0), ("O", 1.0),
    ("N", 2.0), ("E", 2.0), ("O", 2.0),
    ("N", 1.5), ("E", 1.5), ("O", 1.5),
)
AUTO_ALL_PARITIES = ("N", "E", "O")
AUTO_ALL_STOP_BITS = (1.0, 2.0, 1.5)
MODBUS_MIN_UNIT_ID = 0
MODBUS_MAX_UNIT_ID = 247
MODBUS_MAX_RTU_FRAME_BYTES = 260
SERIAL_READ_TIMEOUT_SECONDS = 0.02
MINIMUM_FRAME_GAP_SECONDS = 0.004
DEFAULT_AUTO_DETECTION_WINDOW_SECONDS = 0.5
AUTO_FRAME_MARGIN_SECONDS = 0.05
AUTO_RETRY_DELAY_SECONDS = 0.05

# A trimmed-down floor (was 560) so the capture table still shows a useful
# number of rows without by itself exceeding the height budget of an
# 800x480 display. tests/test_architecture.py pins a >=500 floor.
CAPTURE_TABLE_MINIMUM_HEIGHT = 500
INSPECTOR_SUMMARY_MINIMUM_HEIGHT = 400
INSPECTOR_BYTE_TABLE_MINIMUM_HEIGHT = 340
# Window floor, not a per-tab content floor: each tab's own content sizes
# itself from its children (table/button/combo minimums below), so this only
# has to be small enough to fit a Raspberry Pi-class display. 760px wide
# leaves slack under the 800px baseline for window-manager chrome; the
# window is free to grow well past this on a normal desktop.
MAIN_WINDOW_MINIMUM_WIDTH = 760
# Low enough that the window can actually reach
# responsive.ULTRA_COMPACT_SEVERE_HEIGHT_THRESHOLD (the on-screen-keyboard
# escape hatch) by resizing alone — a floor at or above that threshold would
# make that mode's severe-height branch unreachable in practice.
MAIN_WINDOW_MINIMUM_HEIGHT = 320
# How long a resize burst must go quiet before MainWindow recomputes its
# responsive mode — short enough to feel immediate once a resize/keyboard
# transition settles, long enough that a drag-resize (many resizeEvents in
# quick succession) triggers one recompute instead of dozens.
RESIZE_DEBOUNCE_MS = 120
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
    """Settings used by receive-only serial-format detection.

    Attributes:
        port: Operating-system serial port name.
        baudrates: Candidate baud rates to sweep.
        parities: Candidate parity codes to sweep.
        stop_bits_options: Candidate stop-bit counts to sweep.
        minimum_window_seconds: Seconds to listen on each combination before
            moving to the next.
    """

    port: str
    baudrates: tuple[int, ...] = AUTO_BAUD_RATE_PRIORITY
    parities: tuple[str, ...] = AUTO_ALL_PARITIES
    stop_bits_options: tuple[float, ...] = AUTO_ALL_STOP_BITS
    minimum_window_seconds: float = DEFAULT_AUTO_DETECTION_WINDOW_SECONDS
