"""Run the passive PLC sniffer desktop application."""

if __name__ == "__main__":
    import sys

    from plcsniffer.lite_runtime import prepare_lite_runtime

    prepare_lite_runtime(sys.argv)

from plcsniffer.app import create_main_window, resource_path, run

__all__ = ["create_main_window", "resource_path", "run"]


if __name__ == "__main__":
    raise SystemExit(run())
