"""Application-specific exception hierarchy."""


class PLCSnifferError(Exception):
    """Base exception for recoverable PLC sniffer failures."""


class ConfigurationError(PLCSnifferError, ValueError):
    """Raised when application or protocol configuration is invalid."""


class ProtocolError(PLCSnifferError):
    """Raised when a protocol operation cannot be completed."""


class ProtocolOperationUnsupportedError(ProtocolError):
    """Raised when a protocol adapter does not support an operation."""


class SerialConnectionError(PLCSnifferError):
    """Raised when a serial port cannot be opened or configured."""
