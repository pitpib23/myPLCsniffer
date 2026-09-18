"""Select an optional, locally built Qt Wayland correction before Qt loads."""
from __future__ import annotations

import hashlib
import json
import os
from importlib.metadata import distribution
from pathlib import Path
import platform
import sys


def prepare_lite_runtime(arguments: list[str]) -> None:
    """Re-exec Lite with the validated local backend, preserving Python flags."""
    if '--lite' not in arguments or sys.platform != 'linux':
        return
    if not os.environ.get('WAYLAND_DISPLAY'):
        return
    backend = os.environ.get('QT_QPA_PLATFORM', 'wayland')
    for index, argument in enumerate(arguments[:-1]):
        if argument in ('-platform', '--platform'):
            backend = arguments[index + 1]
    if backend.split(';')[0].split(':')[0] != 'wayland':
        return
    runtime = Path(__file__).resolve().parents[1] / '.lite-runtime'
    manifest_path = runtime / 'manifest.json'
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        required = ('qt_version', 'machine', 'original_sha256', 'patched_sha256')
        if not isinstance(manifest, dict) or any(not manifest.get(key) for key in required):
            raise ValueError('Incomplete manifest')
    except (OSError, ValueError) as error:
        raise SystemExit('Cannot read the local Lite Qt build manifest; rebuild it.') from error
    package = distribution('PySide6-Essentials')
    if package.version != manifest['qt_version'] or platform.machine() != manifest['machine']:
        raise SystemExit('The local Lite Qt backend needs rebuilding for this Python/Qt installation.')
    qt_lib = Path(package.locate_file('PySide6/Qt/lib')).resolve()
    library = 'libQt6WaylandClient.so.6'
    for path, expected in ((qt_lib / library, manifest['original_sha256']),
                           (runtime / library, manifest['patched_sha256'])):
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit('The local Lite Qt backend does not match its build manifest; rebuild it.')
    prefix = os.pathsep.join((str(runtime), str(qt_lib)))
    existing = os.environ.get('LD_LIBRARY_PATH', '')
    if os.environ.get('PLCSNIFFER_LITE_QT_RUNTIME') == prefix and (
            existing == prefix or existing.startswith(prefix + os.pathsep)):
        return
    environment = os.environ.copy()
    environment['LD_LIBRARY_PATH'] = prefix + (os.pathsep + existing if existing else '')
    environment['PLCSNIFFER_LITE_QT_RUNTIME'] = prefix
    os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], environment)
