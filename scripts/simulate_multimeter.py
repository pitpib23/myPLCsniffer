"""Standalone traffic simulator for testing myPLCsniffer without real hardware.

Plays back a realistic "master polls a multimeter" Modbus RTU exchange over
a serial port, on a loop. Point this at one side of a virtual null-modem
pair (e.g. com0com's COM10<->COM11) and point myPLCsniffer's Tab 1 at the
other side. See TESTING_WITHOUT_HARDWARE.md for the full walkthrough.

This writes to a real serial port — it is NOT part of the sniffer app and
is not receive-only. Only ever point it at a virtual/loopback port, never
at a port connected to real equipment.

Usage:
    python scripts/simulate_multimeter.py COM10 [--baud 9600]
"""

from __future__ import annotations

import argparse
import random
import time

import serial

SLAVE_ID = 1
FUNCTION_READ_HOLDING_REGISTERS = 3
START_ADDRESS = 0
REGISTER_COUNT = 2  # two 16-bit registers: e.g. voltage, current


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(payload: bytes) -> bytes:
    return payload + modbus_crc(payload).to_bytes(2, "little")


def build_request() -> bytes:
    payload = bytes(
        [
            SLAVE_ID,
            FUNCTION_READ_HOLDING_REGISTERS,
            *START_ADDRESS.to_bytes(2, "big"),
            *REGISTER_COUNT.to_bytes(2, "big"),
        ]
    )
    return with_crc(payload)


def build_response(voltage_mv: int, current_ma: int) -> bytes:
    byte_count = REGISTER_COUNT * 2
    payload = bytes(
        [SLAVE_ID, FUNCTION_READ_HOLDING_REGISTERS, byte_count]
    ) + voltage_mv.to_bytes(2, "big") + current_ma.to_bytes(2, "big")
    return with_crc(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="Virtual COM port to write to, e.g. COM10")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between polls (default 2.0, matching a slow real device)",
    )
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        print(f"Writing simulated multimeter traffic to {args.port} "
              f"at {args.baud} baud, every {args.interval}s. Ctrl+C to stop.")
        poll = 0
        while True:
            poll += 1
            # Simulate a slowly drifting real-world reading.
            voltage_mv = 11800 + random.randint(-50, 50)
            current_ma = 250 + random.randint(-10, 10)

            request = build_request()
            ser.write(request)
            time.sleep(0.05)  # small gap, like real request/response timing

            response = build_response(voltage_mv, current_ma)
            ser.write(response)

            print(
                f"[poll {poll}] request {request.hex(' ')} -> "
                f"response {response.hex(' ')} "
                f"(voltage={voltage_mv/1000:.2f}V, current={current_ma/1000:.3f}A)"
            )
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
