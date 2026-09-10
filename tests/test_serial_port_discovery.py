"""Regression tests for onboard/GPIO UART discovery (e.g. Raspberry Pi HATs).

pyserial's own Linux comports() scanner globs /dev/ttyAMA* but then drops
any device whose sysfs "subsystem" is "platform" — which is exactly how a
Pi's GPIO UART (PL011, ttyAMA0) is registered, so it's silently absent even
when a receive-only RS-485 HAT is genuinely wired to the header instead of
USB. plcsniffer.capture.list_serial_ports() supplements pyserial's list
with those onboard UARTs; these tests cover that supplement in isolation
(via mocked glob/os.path calls, since this sandbox has no real serial
hardware) rather than relying on real /dev entries.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from serial.tools.list_ports_common import ListPortInfo

from plcsniffer.capture import (
    _ONBOARD_SERIAL_GLOBS,
    _onboard_serial_ports,
    list_serial_ports,
)


def _fake_glob(patterns_to_devices: dict[str, list[str]]):
    """A glob.glob stand-in returning canned devices for known patterns."""

    def _glob(pattern: str) -> list[str]:
        return list(patterns_to_devices.get(pattern, []))

    return _glob


class OnboardSerialPortsTests(unittest.TestCase):
    def test_reports_a_real_onboard_uart_the_pyserial_filter_would_hide(self) -> None:
        with patch(
            "plcsniffer.capture.glob.glob",
            side_effect=_fake_glob({"/dev/ttyAMA*": ["/dev/ttyAMA0"]}),
        ), patch(
            "plcsniffer.capture.os.path.realpath", side_effect=lambda p: p
        ):
            ports = _onboard_serial_ports()

        self.assertEqual([port.device for port in ports], ["/dev/ttyAMA0"])

    def test_reports_a_ttyamc_hat_pyserial_never_globs_for_at_all(self) -> None:
        """Field-confirmed on real hardware: a HAT that enumerates as
        /dev/ttyAMC0 rather than /dev/ttyAMA0 — a name pyserial's own
        comports() glob list never looks for, filter or no filter."""
        with patch(
            "plcsniffer.capture.glob.glob",
            side_effect=_fake_glob({"/dev/ttyAMC*": ["/dev/ttyAMC0"]}),
        ), patch(
            "plcsniffer.capture.os.path.realpath", side_effect=lambda p: p
        ):
            ports = _onboard_serial_ports()

        self.assertEqual([port.device for port in ports], ["/dev/ttyAMC0"])
        self.assertEqual(ports[0].description, "Onboard/GPIO UART")

    def test_prefers_the_stable_serial0_alias_over_the_raw_device_name(self) -> None:
        """/dev/serial0 and /dev/ttyAMA0 are typically the same physical
        UART (Raspberry Pi OS symlinks serial0 to whichever UART is wired
        to the header) — only the friendly alias should be reported."""
        with patch(
            "plcsniffer.capture.glob.glob",
            side_effect=_fake_glob(
                {
                    "/dev/serial0": ["/dev/serial0"],
                    "/dev/ttyAMA*": ["/dev/ttyAMA0"],
                }
            ),
        ), patch(
            "plcsniffer.capture.os.path.realpath",
            side_effect=lambda p: "/dev/ttyAMA0",
        ):
            ports = _onboard_serial_ports()

        self.assertEqual([port.device for port in ports], ["/dev/serial0"])

    def test_reports_nothing_when_no_onboard_uart_exists(self) -> None:
        with patch(
            "plcsniffer.capture.glob.glob", side_effect=_fake_glob({})
        ):
            self.assertEqual(_onboard_serial_ports(), [])

    def test_never_reports_generic_legacy_ttys_devices(self) -> None:
        """Deliberate scope limit: plain /dev/ttyS0-31 (x86 ISA UART
        compat nodes) exist on most desktop/laptop Linux systems whether
        or not real hardware is attached — re-adding those would flood
        the full desktop edition's port list with phantom entries, the
        exact failure pyserial's own filter exists to avoid elsewhere."""
        self.assertNotIn("/dev/ttyS*", _ONBOARD_SERIAL_GLOBS)


class ListSerialPortsTests(unittest.TestCase):
    def test_supplements_pyserial_with_onboard_uarts_it_missed(self) -> None:
        usb_port = ListPortInfo("/dev/ttyUSB0")
        usb_port.description = "USB-Serial Adapter"
        with patch(
            "plcsniffer.capture.list_ports.comports", return_value=[usb_port]
        ), patch(
            "plcsniffer.capture.glob.glob",
            side_effect=_fake_glob({"/dev/ttyAMA*": ["/dev/ttyAMA0"]}),
        ), patch(
            "plcsniffer.capture.os.path.realpath", side_effect=lambda p: p
        ):
            ports = list_serial_ports()

        self.assertEqual(
            sorted(port.device for port in ports),
            ["/dev/ttyAMA0", "/dev/ttyUSB0"],
        )

    def test_does_not_duplicate_a_port_pyserial_already_found(self) -> None:
        """If a future pyserial version (or a different kernel config)
        already reports the onboard UART itself, it must not be listed
        twice."""
        already_found = ListPortInfo("/dev/ttyAMA0")
        with patch(
            "plcsniffer.capture.list_ports.comports",
            return_value=[already_found],
        ), patch(
            "plcsniffer.capture.glob.glob",
            side_effect=_fake_glob({"/dev/ttyAMA*": ["/dev/ttyAMA0"]}),
        ), patch(
            "plcsniffer.capture.os.path.realpath", side_effect=lambda p: p
        ):
            ports = list_serial_ports()

        self.assertEqual([port.device for port in ports], ["/dev/ttyAMA0"])

    def test_real_environment_does_not_raise(self) -> None:
        """Smoke test against the actual filesystem/pyserial — this
        sandbox has no serial hardware, so the only real assertion is
        that nothing raises and a list comes back."""
        self.assertIsInstance(list_serial_ports(), list)


if __name__ == "__main__":
    unittest.main()
