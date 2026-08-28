"""Shared status-label color states, reused by any tab with a status line.

Kept separate from plcsniffer/config.py deliberately: config.py is imported
by non-UI modules (capture.py, modbus.py) and currently has zero PySide
imports of its own — these QSS fragments are a UI concern, not a general
application setting, so they belong beside the other plcsniffer/ui/*.py
files instead of introducing the first Qt-flavored constant into an
otherwise Qt-agnostic module.
"""

from __future__ import annotations

STATUS_STYLES = {
    "info": "color: #526174;",
    "editing": "color: #b45309; font-weight: 600;",
    "saved": "color: #166534; font-weight: 600;",
    "warning": "color: #b91c1c; font-weight: 600;",
}
