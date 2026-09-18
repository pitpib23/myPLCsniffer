"""Build the pinned Qt text-input-v3 fix locally, without modifying the venv.

Raspberry Pi OS arm64 only. Requires git, curl, 7z, ninja, g++, pkg-config,
apt-get and dpkg-deb. Downloaded development packages are extracted, not installed.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

VERSION = '6.11.2'
COMMIT = '8f376c89b21fe554e0b26c497025bce32943a4f7'
ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    'source.tar.gz': (
        f'https://codeload.github.com/qt/qtbase/tar.gz/refs/tags/v{VERSION}',
        '06cd7aa6b3ab19cb5a1664a2891ef0df960f78c97fa31b80b1d6d7ee70688414'),
    'sdk.7z': (
        'https://download.qt.io/online/qtsdkrepository/linux_arm64/desktop/'
        'qt6_6112/qt6_6112/qt.qt6.6112.linux_gcc_arm64/'
        '6.11.2-0-202608131018qtbase-Linux-Ubuntu_24_04-GCC-Linux-Ubuntu_24_04-AARCH64.7z',
        '2f9991bc4e5660fd36d70df04239fadc17acf135f201041cdc1bf6c82ae43aa4'),
    'text-input.patch': (
        f'https://github.com/qt/qtbase/commit/{COMMIT}.patch',
        '0c83c278b152aee9db14b1796c03ed832ece48901c860ae1dc34820eda72eb78'),
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*arguments, **kwargs):
    subprocess.run([str(arg) for arg in arguments], check=True, **kwargs)


def install_runtime(library, qt_lib, source):
    """Publish the manifest last so an incomplete build cannot be selected."""
    runtime = ROOT / '.lite-runtime'
    runtime.mkdir(exist_ok=True)
    name = 'libQt6WaylandClient.so.6'
    staged = runtime / (name + '.new')
    shutil.copy2(library, staged)
    run('strip', '--strip-unneeded', staged)
    manifest = {
        'qt_version': VERSION, 'machine': platform.machine(),
        'upstream_commit': COMMIT,
        'original_sha256': digest(qt_lib / name),
        'patched_sha256': digest(staged),
        'inputs': {key: {'url': url, 'sha256': sha} for key, (url, sha) in INPUTS.items()},
    }
    shutil.copytree(source / 'LICENSES', runtime / 'LICENSES', dirs_exist_ok=True)
    staged.replace(runtime / name)
    temporary = runtime / 'manifest.json.new'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    temporary.replace(runtime / 'manifest.json')
    print(f'Installed Lite-only backend in {runtime}. Full mode and the venv are unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, help='Empty directory; defaults to a new /tmp directory')
    args = parser.parse_args()
    package = distribution('PySide6-Essentials')
    if sys.platform != 'linux' or platform.machine() != 'aarch64' or package.version != VERSION:
        parser.error(f'This pinned build requires Linux aarch64 and PySide6 {VERSION}.')
    for tool in ('git', 'curl', '7z', 'ninja', 'g++', 'pkg-config', 'apt-get', 'dpkg-deb', 'tar', 'strip'):
        if shutil.which(tool) is None:
            parser.error(f'Missing build tool: {tool}')
    work = args.work_dir.resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix='plcsniffer-qt-'))
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        parser.error('--work-dir must be empty; build inputs are never overwritten.')
    print(f'Build files: {work}', flush=True)
    qt_lib = Path(package.locate_file('PySide6/Qt/lib')).resolve()
    for name, (url, expected) in INPUTS.items():
        output = work / name
        run('curl', '--fail', '--location', '--retry', '3', '--output', output, url)
        if digest(output) != expected:
            raise SystemExit(f'Checksum mismatch: {name}')
    source = work / f'qtbase-{VERSION}'
    members = ['src/plugins/platforms/wayland', 'src/3rdparty/wayland',
               'src/tools/qtwaylandscanner', 'cmake', '.cmake.conf', 'LICENSES']
    run('tar', '-xzf', work / 'source.tar.gz', '-C', work,
        *(f'qtbase-{VERSION}/{member}' for member in members))
    sdk = work / 'sdk'
    # These two optional linker symlinks point to SDK libraries not in this archive.
    run('7z', 'x', '-y', f'-o{sdk}', '-x!lib/libQt6DBus.so',
        '-x!lib/libQt6EglFsKmsSupport.so', work / 'sdk.7z')
    run('git', 'apply', work / 'text-input.patch', cwd=source)
    run('git', 'apply', ROOT / 'scripts/qt-wayland/backend-only.patch', cwd=source)
    shutil.copy2(ROOT / 'scripts/qt-wayland/CMakeLists.txt', source / 'CMakeLists.txt')

    deps = work / 'deps'
    deps.mkdir()
    run('apt-get', 'download', 'libwayland-dev', 'libwayland-bin', 'libxkbcommon-dev',
        'libegl-dev', 'libgl-dev', 'libglx-dev', 'libgles-dev', 'libvulkan-dev', cwd=deps)
    sysroot = deps / 'root'
    for archive in deps.glob('*.deb'):
        run('dpkg-deb', '-x', archive, sysroot)
    native_lib = sysroot / 'usr/lib/aarch64-linux-gnu'
    for link in native_lib.glob('*.so'):
        if link.is_symlink() and not link.exists():
            target = link.parent / os.readlink(link)
            installed = Path('/usr/lib/aarch64-linux-gnu') / target.name
            if not installed.exists():
                raise SystemExit(f'Missing native runtime dependency: {installed}')
            target.symlink_to(installed)

    build_tools = work / 'tools'
    run(sys.executable, '-m', 'pip', 'install', '--no-cache-dir', '--target', build_tools, 'cmake==4.4.3')
    cmake = build_tools / 'cmake/data/bin/cmake'
    environment = os.environ.copy()
    environment.update(LD_LIBRARY_PATH=str(qt_lib), PKG_CONFIG_SYSROOT_DIR=str(sysroot),
                       PKG_CONFIG_LIBDIR=str(native_lib / 'pkgconfig'))
    build = work / 'build'
    run(cmake, '-S', source, '-B', build, '-G', 'Ninja',
        f'-DCMAKE_PREFIX_PATH={sdk};{sysroot / "usr"}',
        f'-DCMAKE_INSTALL_PREFIX={work / "install"}', '-DCMAKE_BUILD_TYPE=Release',
        '-DCMAKE_SKIP_RPATH=ON',
        '-DQT_BUILD_TESTS=OFF', '-DQT_BUILD_EXAMPLES=OFF',
        '-DQT_BUILD_EXAMPLES_BY_DEFAULT=OFF', '-DQT_GENERATE_SBOM=OFF', env=environment)
    run(cmake, '--build', build, '--target', 'WaylandClient', '--parallel', '1', env=environment)
    install_runtime(build / f'lib/libQt6WaylandClient.so.{VERSION}', qt_lib, source)
    print(f'Keep {work} for the exact source/build inputs. See docs/lite-input-method.md for acceptance.')


if __name__ == '__main__':
    main()
