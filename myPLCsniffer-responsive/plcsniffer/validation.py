"""Reusable validation for UI, service, and protocol boundaries."""

from __future__ import annotations

from plcsniffer.config import (
    VALID_PARITIES,
    VALID_STOP_BITS,
    SerialSettings,
)
from plcsniffer.exceptions import ConfigurationError


def validate_port(port: str) -> str:
    """Validate and normalize a serial port identifier.

    Args:
        port: User-provided operating-system serial port identifier.

    Returns:
        The stripped port identifier.

    Raises:
        ConfigurationError: If the identifier is blank.
    """
    normalized = str(port).strip()
    if not normalized:
        raise ConfigurationError("A serial port is required.")
    return normalized


def validate_baudrate(baudrate: int) -> int:
    """Validate a positive serial baud rate."""
    try:
        normalized = int(baudrate)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Baud rate must be an integer.") from error
    if normalized <= 0:
        raise ConfigurationError("Baud rate must be greater than zero.")
    return normalized


def validate_parity(parity: str) -> str:
    """Validate and normalize a PySerial parity code."""
    normalized = str(parity).strip().upper()
    if normalized not in VALID_PARITIES:
        raise ConfigurationError(f"Unsupported serial parity: {parity!r}.")
    return normalized


def validate_stopbits(stopbits: float) -> float:
    """Validate a supported serial stop-bit value."""
    try:
        normalized = float(stopbits)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Stop bits must be numeric.") from error
    if normalized not in VALID_STOP_BITS:
        raise ConfigurationError(f"Unsupported stop-bit setting: {stopbits!r}.")
    return normalized


def validate_serial_settings(settings: SerialSettings) -> SerialSettings:
    """Return a normalized copy of validated serial settings."""
    if settings.bytesize not in {5, 6, 7, 8}:
        raise ConfigurationError("Serial data bits must be between 5 and 8.")
    return SerialSettings(
        port=validate_port(settings.port),
        baudrate=validate_baudrate(settings.baudrate),
        parity=validate_parity(settings.parity),
        stopbits=validate_stopbits(settings.stopbits),
        bytesize=int(settings.bytesize),
    )


def validate_register_address(address: int) -> int:
    """Validate a zero-based 16-bit Modbus address."""
    try:
        normalized = int(address)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Register address must be an integer.") from error
    if not 0 <= normalized <= 65_535:
        raise ConfigurationError("Register address must be between 0 and 65535.")
    return normalized
