"""Run the passive PLC sniffer desktop application."""

from plcsniffer.app import create_main_window, resource_path, run

__all__ = ["create_main_window", "resource_path", "run"]


if __name__ == "__main__":
    raise SystemExit(run())
