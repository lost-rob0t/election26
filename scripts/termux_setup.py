#!/usr/bin/env python3
"""Private paths and managed venv setup; no networking, cron, or capture."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

MARKER = "election26-managed-venv-v1\n"


def private_path(home, relative):
    home = Path(home).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Private path must be relative to HOME")
    path = home / relative
    for item in [path, *path.parents]:
        if item == home:
            break
        if item.is_symlink():
            raise ValueError("Refusing a symlink in a private application path")
    return path


def create_only(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def prepare(home, source, create_venv=True):
    home = Path(home).resolve()
    source = Path(source).resolve()
    app = private_path(home, ".local/share/election26")
    archive = private_path(home, ".local/share/election26/archive")
    config = private_path(home, ".config/election26/config.json")
    venv = private_path(home, ".local/share/election26/venv")
    launcher = private_path(home, ".local/bin/election26")
    for path in (app, archive):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    template = json.loads((source / "config/us-2026.example.json").read_text())
    created = create_only(config, json.dumps(template, indent=2) + "\n")
    marker = venv / ".election26-managed"
    if venv.exists() and (not marker.is_file() or marker.is_symlink() or marker.read_text() != MARKER):
        raise ValueError("Refusing to modify an existing unmanaged virtual environment")
    if create_venv:
        # Claim only a new directory before setup, so an interrupted managed
        # venv can be repaired in-place on retry (venvs cannot be renamed).
        if not venv.exists():
            venv.mkdir(mode=0o700)
            create_only(marker, MARKER)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    target = venv / "bin/election26"
    text = "#!/data/data/com.termux/files/usr/bin/sh\n# election26 managed launcher v1\nexec " + shlex.quote(str(target)) + ' "$@"\n'
    if launcher.exists() and launcher.read_text() != text:
        raise ValueError("Refusing to replace a preexisting unrelated launcher")
    create_only(launcher, text, mode=0o700)
    return {"config_created": created, "archive": str(archive), "launcher": str(launcher)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.home, args.source)))


if __name__ == "__main__":
    main()
