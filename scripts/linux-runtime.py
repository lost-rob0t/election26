#!/usr/bin/env python3
"""Run the bundled stdlib-only collector from a private disposable code directory."""
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required")
    os.umask(0o077)
    source = Path(__file__).resolve().parents[1]
    # Preserve the core's source/archive separation without pip, activation or
    # weakening Archive's guard. Copy only package code, never config or data.
    with tempfile.TemporaryDirectory(prefix=".election26-linux-runtime-", dir=Path.home()) as tmp:
        runtime = Path(tmp)
        package = runtime / "src/election26"
        package.mkdir(parents=True, mode=0o700)
        for path in sorted((source / "src/election26").iterdir()):
            if path.suffix != ".py":
                continue
            if path.is_symlink() or not path.is_file():
                raise SystemExit("Package code must be ordinary files")
            shutil.copyfile(path, package / path.name)
        sys.path.insert(0, str(package.parent))
        os.environ["PYTHONPATH"] = str(package.parent)
        os.environ["PYTHONSAFEPATH"] = "1"
        os.environ["PYTHONNOUSERSITE"] = "1"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        from election26.kit import main as kit_main
        return kit_main(["--source", str(source), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
