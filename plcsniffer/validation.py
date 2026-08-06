"""Reusable validation for UI, service, and protocol boundaries."""

from __future__ import annotations

from ipaddress import ip_address

from plcsniffer.config import (
    MODBUS_MAX_UNIT_ID,
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


def validate_ip_address(address: str) -> str:
    """Validate an IPv4 or IPv6 address for future network protocols."""
    normalized = str(address).strip()
    try:
        return str(ip_address(normalized))
    except ValueError as error:
        raise ConfigurationError(f"Invalid IP address: {address!r}.") from error


def validate_timeout(timeout_seconds: float) -> float:
    """Validate a positive operation timeout in seconds."""
    try:

        normalized = float(timeout_seconds)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Timeout must be numeric.") from error
    if normalized <= 0:
        raise ConfigurationError("Timeout must be greater than zero.")
    return normalized


def validate_modbus_unit_id(unit_id: int, *, allow_broadcast: bool = False) -> int:
    """Validate a Modbus unit identifier.

    Args:
        unit_id: Unit/slave identifier.
        allow_broadcast: Permit unit zero for receive-only observations.

    Returns:
        Normalized unit identifier.

    Raises:
        ConfigurationError: If the identifier is outside the allowed range.
    """
    try:
        normalized = int(unit_id)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Modbus unit ID must be an integer.") from error
    minimum = 0 if allow_broadcast else 1
    if not minimum <= normalized <= MODBUS_MAX_UNIT_ID:
        raise ConfigurationError(
            f"Modbus unit ID must be between {minimum} and {MODBUS_MAX_UNIT_ID}."
        )
    return normalized


def validate_register_address(address: int) -> int:
    """Validate a zero-based 16-bit Modbus address."""
    try:
        normalized = int(address)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Register address must be an integer.") from error
    if not 0 <= normalized <= 65_535:
        raise ConfigurationError("Register address must be between 0 and 65535.")
    return normalized


def validate_point_count(count: int, *, bit_points: bool) -> int:
    """Validate a Modbus read count for bit or register points."""
    try:
        normalized = int(count)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("Point count must be an integer.") from error
    maximum = 2_000 if bit_points else 125
    if not 1 <= normalized <= maximum:
        raise ConfigurationError(f"Point count must be between 1 and {maximum}.")
    return normalized
