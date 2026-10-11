#!/usr/bin/env python3
"""Offline installed-command smoke checks. Only ephemeral synthetic fixtures."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run(command, **kwargs):
    return subprocess.run(command, text=True, capture_output=True, check=True, **kwargs).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--with-sdk", action="store_true")
    parser.add_argument("--with-keyring", action="store_true")
    parser.add_argument("--with-whisper", action="store_true")
    parser.add_argument("--media-bin", type=Path)
    parser.add_argument("--keyring-bin", type=Path)
    parser.add_argument("--whisper-bin", type=Path)
    args = parser.parse_args()
    commands = {name: str(args.bin / name) for name in ("election26", "election26-cron", "election26-fec-target")}
    # Run away from the source tree; imports must come from the installed package.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "ELECTION26_FEC_EXECUTABLE"}}
        for command in commands.values():
            assert "usage:" in run([command, "--help"], cwd=root, env=env).lower()
        archive = root / "status-only"
        result = json.loads(run([commands["election26"], "--archive", str(archive), "status"], cwd=root, env=env))
        assert result["capture_observations"] == 0 and not archive.exists()
        preview = run([commands["election26-cron"], "--executable", commands["election26"], "--config", str(root / "config.json"), "--archive", str(archive), "--log", str(root / "run.log")], cwd=root, env=env)
        assert "BEGIN election26 managed cron" in preview and "--extract-media" not in preview
        assert list(root.iterdir()) == []
        missing = subprocess.run([commands["election26-fec-target"], "--", "--help"], cwd=root, env=env, text=True, capture_output=True)
        assert missing.returncode == 2 and "not bundled" in missing.stderr
        fake = root / "synthetic external tool"
        fake.write_text(f"#!{args.python}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7 if sys.argv[1:] == ['fail'] else 0)\n")
        fake.chmod(0o700)
        env["ELECTION26_FEC_EXECUTABLE"] = str(fake)
        forwarded = ["literal ; $(no-shell)", "--server", "https://example.invalid"]
        output = run([commands["election26-fec-target"], "--", *forwarded], cwd=root, env=env)
        assert json.loads(output) == forwarded
        failed = subprocess.run([commands["election26-fec-target"], "--", "fail"], cwd=root, env=env, capture_output=True)
        assert failed.returncode == 7
        imports = ["import election26; import election26.browser, election26.browser_catalog, election26.browser_renderer, election26.browser_resources, election26.spool; print(election26.__file__)"]
        if args.with_sdk:
            imports.append("from election26.normalize import sdk; sdk()")
        if args.with_keyring:
            imports.append("import keyring")
        if args.with_whisper:
            # Import/startup only: no model fetch, load, inference or source media.
            imports.append("import whisper, torch; import importlib.metadata as m; assert m.version('openai-whisper') == '20250625'; assert m.version('torch') == '2.13.0+cpu'; assert torch.version.cuda is None; assert torch.tensor([1, 2, 3]).sum().item() == 6")
        print(run([args.python, "-c", "; ".join(imports)], cwd=root, env=env).strip())
        for directory, command in ((args.keyring_bin, "keyring"), (args.whisper_bin, "whisper")):
            if directory:
                assert "usage:" in run([str(directory / command), "--help"], cwd=root, env=env).lower()
        if args.media_bin:
            assert run([str(args.media_bin / "yt-dlp"), "--version"], cwd=root, env=env).strip()
            assert "ffmpeg version" in run([str(args.media_bin / "ffmpeg"), "-version"], cwd=root, env=env)
    print("Installed commands passed: help, non-mutating status/cron preview, synthetic FEC forwarding and selected imports/tools.")


if __name__ == "__main__":
    main()
