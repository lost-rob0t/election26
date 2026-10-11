#!/usr/bin/env python3
"""Actual Termux entrypoint/extracted-ZIP smoke, with empty synthetic sources only."""
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
    if not Path("/system/bin/getprop").exists():
        raise SystemExit("This entrypoint verification requires real Android Termux")
    with tempfile.TemporaryDirectory(prefix="kit extraction with spaces ", dir=Path.home()) as tmp:
        work = Path(tmp)
        bundle = work / "fixture-source.zip"
        # Provenance is explicitly synthetic for this build/extraction fixture.
        builder.build(ROOT, bundle, "0" * 40)
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(work)
        extracted = work / "election26-termux-archive-kit"
        for line in (extracted / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            assert hashlib.sha256((extracted / name).read_bytes()).hexdigest() == expected
        config = extracted / "archive-config.json"
        config.write_text(json.dumps({"min_free_bytes": 0, "manual_urls": [], "feeds": [],
            "allowed_hosts": [], "max_requests": 2, "max_run_seconds": 10}))
        original = config.read_bytes()
        command = ["bash", "./start-archive.sh"]
        # The exact delivered command is run twice from the extracted source root.
        # Empty allowlists/sources prove setup/launch/resume without live acquisition.
        for _ in range(2):
            result = subprocess.run(command, cwd=extracted, capture_output=True, text=True, timeout=60)
            print(result.stdout, end="")
            assert result.returncode == 0, result.stderr
            assert '"rotation_complete": true' in result.stdout
            assert config.read_bytes() == original
            assert (extracted / "archive/catalog.sqlite").is_file()
        # Separately prove synthetic acquisition/resume from the extracted kit.
        # Transport is an in-memory fixture; no loopback or production allowlist bypass.
        sys.path.insert(0, str(extracted / "tests"))
        from test_archive_kit import FixtureHTTP
        from election26 import kit
        from election26.archive import Archive, status
        acquisition = work / "synthetic acquisition with spaces"
        acquisition.mkdir(mode=0o700)
        synthetic = {"allowed_hosts": ["news.example"], "min_free_bytes": 0,
            "feeds": ["https://news.example/feed1", "https://news.example/feed2"],
            "manual_urls": ["https://news.example/manual"], "max_requests": 3,
            "max_run_seconds": 30, "max_urls": 10}
        private_config = acquisition / "archive-config.json"
        private_config.write_text(json.dumps(synthetic))
        preserved = private_config.read_bytes()
        archive_path, _, loaded = kit.prepare(acquisition, Path.home(), extracted)
        with Archive(archive_path, loaded).writer() as archive:
            assert kit.run_batch(archive, FixtureHTTP(loaded)) == 75
        originals = {p: p.read_bytes() for p in (archive_path / "objects").glob("*/*")}
        with Archive(archive_path, loaded).writer() as archive:
            assert kit.run_batch(archive, FixtureHTTP(loaded)) == 0
            assert len(archive.media_records()) > 0
            assert archive.db.execute("SELECT COUNT(*) FROM captures WHERE media_reference_id IS NOT NULL").fetchone()[0] == 0
        assert status(archive_path)["capture_observations"] == 5
        assert all(p.read_bytes() == value for p, value in originals.items())
        assert private_config.read_bytes() == preserved
        print("ANDROID_TERMUX_SOURCE_KIT_ACQUISITION_PASSED: extracted fixtures, five synthetic originals, two-run resume, media links only")
        # Force a synthetic offline setup failure, never contact a package server.
        (extracted / "scripts/install-termux.sh").write_text("#!/bin/sh\nexit 2\n")
        result = subprocess.run(command, cwd=extracted, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2 and "Setup failed." in result.stderr
        assert '"ready"' not in result.stdout and config.read_bytes() == original
        assert (extracted / "archive/catalog.sqlite").is_file()
        print("ANDROID_TERMUX_SOURCE_KIT_PASSED: ZIP extraction, exact command twice, literal ./archive, spaces, create-only config, offline failure; no live sources")


if __name__ == "__main__":
    main()
