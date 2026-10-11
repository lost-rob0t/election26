#!/usr/bin/env python3
"""Exercise installed yt-dlp on one self-generated file; no remote media."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import wave


def main():
    with tempfile.TemporaryDirectory(prefix="election26-media-") as tmp:
        root = Path(tmp)
        source = root / "synthetic.wav"
        target = root / "download.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 1600)
        subprocess.run(["yt-dlp", "--version"], check=True, timeout=30)
        # File URLs are explicitly enabled only for this generated fixture.
        # No user config, plugins, playlist, proxy, external downloader or auth.
        subprocess.run(["yt-dlp", "--ignore-config", "--no-plugin-dirs", "--enable-file-urls",
                        "--no-playlist", "--no-part", "--max-filesize", "1M", "--socket-timeout", "10",
                        "--retries", "0", "--output", str(target), source.as_uri()], check=True, timeout=30)
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
        print("YTDLP_SYNTHETIC_BYTE_IDENTITY_PASSED")


if __name__ == "__main__":
    main()
