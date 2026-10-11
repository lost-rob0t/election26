#!/usr/bin/env python3
"""Copy an installed, trusted Linux renderer runtime into a NEW private directory.

No installation or download. Input binaries/packages must already come from
trusted official OS/Python/Playwright packages. Do not use this on unknown code.
Only this output root is mounted in the browser; no host library trees are bound.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=True)
    os.chmod(target, target.stat().st_mode & 0o755)  # Never retain setuid/setgid.


def build(destination, chromium):
    if sys.platform != 'linux':
        raise SystemExit('The isolated renderer runtime is Linux-only')
    root = destination.expanduser().absolute()
    source = Path(__file__).resolve().parents[1]
    if root.exists() or source == root or source in root.parents:
        raise SystemExit('Choose a new runtime directory outside the checkout')
    if not chromium.is_file():
        raise SystemExit('Install trusted Playwright Chromium before building the runtime')
    root.mkdir(mode=0o700, parents=True)
    # Preserve the base interpreter layout so stdlib/extension discovery works.
    python = Path(sys.executable).resolve()
    base = Path(sys.base_prefix).resolve()
    stdlib = Path(sysconfig.get_path('stdlib')).resolve()
    copy(python, root / str(python).lstrip('/'))
    shutil.copytree(stdlib, root / str(stdlib).lstrip('/'),
                    ignore=shutil.ignore_patterns('site-packages', 'dist-packages', '__pycache__', '*.pyc', '_tkinter*', 'tkinter', 'idlelib', 'turtledemo', 'ensurepip', 'test', 'tests'))
    # Python installations can require a libpython next to the interpreter.
    libdir = Path(sysconfig.get_config_var('LIBDIR') or base / 'lib')
    for library in libdir.glob('libpython*.so*'):
        copy(library, root / str(library).lstrip('/'))
    vendor = root / 'app/vendor'
    vendor.mkdir(parents=True)
    for package in ('playwright', 'greenlet', 'pyee', 'typing_extensions'):
        spec = importlib.util.find_spec(package)
        if not spec or not spec.origin:
            raise SystemExit('Missing installed renderer dependency: ' + package)
        path = Path(spec.origin)
        if spec.submodule_search_locations:
            shutil.copytree(path.parent, vendor / package,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        else:
            copy(path, vendor / path.name)
    browser_dir = root / 'runtime/chromium'
    shutil.copytree(chromium.parent, browser_dir, symlinks=False)
    copy(source / 'src/election26/browser_renderer.py', root / 'app/renderer.py')
    # ldd is used only on the already trusted installed runtime, never webpage data.
    # Every referenced library is copied, not broadly bind-mounted from the host.
    pending = list(root.rglob('*'))
    checked = set()
    while pending:
        path = pending.pop()
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        # Copying flattens links; remove executable privilege bits everywhere.
        os.chmod(path, path.stat().st_mode & 0o755)
        proc = subprocess.run(['ldd', str(path)], capture_output=True, text=True, timeout=10,
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if 'not found' in proc.stdout:
            raise SystemExit('A renderer shared library is missing: ' + str(path))
        for name in re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)', proc.stdout, re.M):
            dependency = Path(name)
            if dependency in checked or str(dependency).startswith(str(root) + '/'):
                continue
            checked.add(dependency)
            target = root / name.lstrip('/')
            if not target.exists():
                copy(dependency, target)
                pending.append(target)
    for directory in ('tmp', 'proc', 'dev'):
        (root / directory).mkdir(exist_ok=True)
    manifest = {'format_version': 1, 'python': str(python), 'python_home': str(base),
                'chromium': '/runtime/chromium/' + chromium.name, 'public_runtime_only': True}
    (root / 'runtime.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'runtime_created': str(root), 'files': sum(p.is_file() for p in root.rglob('*'))}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--chromium', type=Path, help='Trusted installed Chromium binary; defaults to the Playwright-managed binary')
    args = parser.parse_args()
    if args.chromium is None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            args.chromium = Path(p.chromium.executable_path)
    build(args.destination, args.chromium.resolve())
