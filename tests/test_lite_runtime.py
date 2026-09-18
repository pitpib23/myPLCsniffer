"""The optional backend must never alter Full or non-Wayland startup."""
import unittest
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

from plcsniffer.lite_runtime import prepare_lite_runtime


class LiteRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.runtime = root / '.lite-runtime'
        self.runtime.mkdir()
        self.qt = root / 'qt'
        self.qt.mkdir()
        library = 'libQt6WaylandClient.so.6'
        (self.qt / library).write_bytes(b'original')
        (self.runtime / library).write_bytes(b'patched')
        self.manifest = {
            'qt_version': '6.11.2', 'machine': 'aarch64',
            'original_sha256': hashlib.sha256(b'original').hexdigest(),
            'patched_sha256': hashlib.sha256(b'patched').hexdigest(),
        }
        self.write_manifest()
        package = Mock(version='6.11.2')
        package.locate_file.return_value = self.qt
        for context in (
            patch('plcsniffer.lite_runtime.__file__', str(root / 'plcsniffer/lite_runtime.py')),
            patch('plcsniffer.lite_runtime.distribution', return_value=package),
            patch('plcsniffer.lite_runtime.platform.machine', return_value='aarch64'),
            patch('plcsniffer.lite_runtime.sys.platform', 'linux'),
            patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}, clear=True),
        ):
            context.start()
            self.addCleanup(context.stop)

    def write_manifest(self):
        (self.runtime / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_full_does_not_inspect_or_change_runtime(self):
        with patch('plcsniffer.lite_runtime.distribution') as package, patch('plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py'])
            package.assert_not_called()
            execute.assert_not_called()

    def test_explicit_offscreen_keeps_normal_runtime(self):
        with patch.dict('os.environ', {'WAYLAND_DISPLAY': 'wayland-0', 'QT_QPA_PLATFORM': 'offscreen'}), patch('plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite'])
            execute.assert_not_called()

    def test_missing_wayland_keeps_normal_runtime(self):
        with patch.dict('os.environ', {}, clear=True), patch('plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite'])
            execute.assert_not_called()

    def test_command_line_platform_override(self):
        with patch('plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite', '-platform', 'xcb'])
            execute.assert_not_called()

    def test_absent_optional_runtime(self):
        (self.runtime / 'manifest.json').unlink()
        with patch('plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite'])
            execute.assert_not_called()

    def test_reexec_preserves_flags_arguments_and_library_path(self):
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/existing'}), patch(
                'plcsniffer.lite_runtime.sys.orig_argv', ['python', '-B', 'main.py', '--lite']), patch(
                'plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite'])
            executable, arguments, environment = execute.call_args.args
            self.assertEqual(executable, sys.executable)
            self.assertEqual(arguments, [sys.executable, '-B', 'main.py', '--lite'])
            prefix = os.pathsep.join((str(self.runtime), str(self.qt)))
            self.assertEqual(environment['LD_LIBRARY_PATH'], prefix + ':/existing')
            self.assertEqual(environment['PLCSNIFFER_LITE_QT_RUNTIME'], prefix)
            self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/existing')

    def test_reexec_happens_only_once(self):
        prefix = os.pathsep.join((str(self.runtime), str(self.qt)))
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': prefix + ':/existing',
                                   'PLCSNIFFER_LITE_QT_RUNTIME': prefix}), patch(
                'plcsniffer.lite_runtime.os.execve') as execute:
            prepare_lite_runtime(['main.py', '--lite'])
            execute.assert_not_called()

    def test_rejects_toolkit_or_architecture_mismatch(self):
        for key in ('qt_version', 'machine'):
            with self.subTest(key=key), patch('plcsniffer.lite_runtime.os.execve') as execute:
                original = self.manifest[key]
                self.manifest[key] = 'incompatible'
                self.write_manifest()
                with self.assertRaisesRegex(SystemExit, 'needs rebuilding'):
                    prepare_lite_runtime(['main.py', '--lite'])
                execute.assert_not_called()
                self.manifest[key] = original

    def test_rejects_changed_or_missing_libraries(self):
        for directory in (self.qt, self.runtime):
            with self.subTest(directory=directory), patch('plcsniffer.lite_runtime.os.execve') as execute:
                library = directory / 'libQt6WaylandClient.so.6'
                original = library.read_bytes()
                library.write_bytes(b'changed')
                with self.assertRaisesRegex(SystemExit, 'does not match'):
                    prepare_lite_runtime(['main.py', '--lite'])
                library.unlink()
                with self.assertRaisesRegex(SystemExit, 'does not match'):
                    prepare_lite_runtime(['main.py', '--lite'])
                execute.assert_not_called()
                library.write_bytes(original)

    def test_rejects_invalid_manifest(self):
        for contents in ('invalid json', '{}', '[]'):
            with self.subTest(contents=contents):
                (self.runtime / 'manifest.json').write_text(contents, encoding='utf-8')
                with self.assertRaisesRegex(SystemExit, 'Cannot read'):
                    prepare_lite_runtime(['main.py', '--lite'])
