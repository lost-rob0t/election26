#!/usr/bin/env python3
"""Extracted Linux ZIP proof. Synthetic sources only; no live collection."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_builder", ROOT / "scripts/build-source-kit.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nix", action="store_true")
    parser.add_argument("--zip", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="linux kit spaces ", dir=Path.home()) as tmp:
        work = Path(tmp)
        bundle = args.zip
        if bundle is None:
            bundle = work / "synthetic.zip"
            builder.build(ROOT, bundle, "0" * 40, "linux")
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(work)
        extracted = work / "election26-linux-archive-kit"
        for line in (extracted / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256((extracted / name).read_bytes()).hexdigest() == expected
        command = ["bash", "./start-linux-archive.sh"] + (["--nix"] if args.nix else [])
        def run(*options, expected=0):
            result = subprocess.run(command + list(options), cwd=extracted,
                capture_output=True, text=True, timeout=600)
            print(result.stdout, end="")
            assert result.returncode == expected, (result.returncode, result.stderr)
            return result
        # Real prepare uses the unchanged starter but cannot contact sources.
        run("--prepare-only")
        config = extracted / "archive-config.json"
        assert config.read_bytes() == (extracted / "config/us-2026-public-starter.json").read_bytes()
        # Empty synthetic sources prove the exact shell command, worker and resume.
        config.write_text(json.dumps({"min_free_bytes": 0, "feeds": [], "manual_urls": [],
            "allowed_hosts": [], "max_requests": 2, "max_run_seconds": 10}))
        preserved = config.read_bytes()
        for _ in range(2):
            assert '"rotation_complete": true' in run().stdout
            assert config.read_bytes() == preserved
        assert '"capture_observations": 0' in run("--status").stdout
        assert (extracted / "archive/catalog.sqlite").is_file()
        # Invoke the extracted implementation in a fresh process for acquisition.
        # Patch only the in-memory transport, never the public-address policy.
        code = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_archive_kit import FixtureHTTP
from election26 import kit
from election26.archive import Archive, status
os.umask(0o077)
target = Path.cwd().parent / "synthetic acquisition"
target.mkdir(mode=0o700)
config = target / "archive-config.json"
config.write_text(json.dumps({"allowed_hosts": ["news.example"], "min_free_bytes": 0,
    "feeds": ["https://news.example/feed1", "https://news.example/feed2"],
    "manual_urls": ["https://news.example/manual"], "max_requests": 3,
    "max_run_seconds": 30, "max_urls": 10}))
original_config = config.read_bytes()
root, _, loaded = kit.prepare(target, Path.home(), Path.cwd())
with Archive(root, loaded).writer() as archive:
    assert kit.run_batch(archive, FixtureHTTP(loaded)) == 75
originals = {p: p.read_bytes() for p in (root / "objects").glob("*/*")}
with Archive(root, loaded).writer() as archive:
    assert kit.run_batch(archive, FixtureHTTP(loaded)) == 0
    assert len(archive.media_records()) > 0
    assert archive.db.execute("SELECT COUNT(*) FROM captures WHERE media_reference_id IS NOT NULL").fetchone()[0] == 0
assert status(root)["capture_observations"] == 5
assert all(p.read_bytes() == data for p, data in originals.items())
assert config.read_bytes() == original_config
print("LINUX_ACQUISITION_RESUME_PASSED: five synthetic observations; originals/config retained; media links only")
"""
        env = {**os.environ, "PYTHONPATH": str(extracted / "src"), "PYTHONSAFEPATH": "1",
               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-P", "-s", "-c", code], cwd=extracted,
            env=env, capture_output=True, text=True, timeout=60)
        print(result.stdout, end="")
        assert result.returncode == 0, result.stderr
        # Unsupported/unrecognized archive contents are preserved, not overwritten.
        sentinel = extracted / "archive/operator-file.txt"
        sentinel.write_text("synthetic preserve")
        run(expected=2)
        assert sentinel.read_text() == "synthetic preserve"
        assert config.read_bytes() == preserved
        print("LINUX_EXTRACTED_ZIP_PASSED: prepare/start/resume/status/spaces/preservation; nix=" + str(args.nix))


if __name__ == "__main__":
    main()
