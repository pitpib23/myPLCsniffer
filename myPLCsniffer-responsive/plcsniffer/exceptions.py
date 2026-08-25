"""Application-specific exception hierarchy."""


class PLCSnifferError(Exception):
    """Base exception for recoverable PLC sniffer failures."""


class ConfigurationError(PLCSnifferError, ValueError):
    """Raised when application or protocol configuration is invalid."""
