#!/usr/bin/env python3
"""Fail before publication when staged/committed paths look like private data.

Defense in depth, not a proof that arbitrary renamed/encoded bytes are safe.
The operator must keep every real archive and location derivative outside Git.
"""
import argparse
import json
from pathlib import PurePosixPath
import subprocess
import sys

PRIVATE_DIRS = {"objects", "transcripts", "canonical", "manifests", "archives", "archive", "data",
                "private", "captures", "spool", "exports", "routes", "wigle"}
PRIVATE_SUFFIXES = {".kml", ".kmz", ".gpx", ".geojson", ".gpkg", ".pcap", ".pcapng", ".csv",
                    ".sqlite", ".sqlite3", ".part", ".zip", ".gz", ".7z", ".rar", ".tar",
                    ".wav", ".mp3", ".mp4", ".webm", ".jpg", ".jpeg", ".png", ".gif", ".pdf"}
LOCATION_KEYS = {"latitude", "longitude", "lat", "lon", "lng", "coordinates", "bssid", "ssid",
                 "gps", "gpslatitude", "gpslongitude", "route", "trackpoints"}


def has_location_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in LOCATION_KEYS or has_location_fields(item):
                return True
    elif isinstance(value, list):
        return any(has_location_fields(item) for item in value)
    return False


def rejection(path, data=None):
    p = PurePosixPath(path.lower())
    if PRIVATE_DIRS.intersection(p.parts[:-1]):
        return "private archive/input/export directory"
    if p.name.startswith(".env") or PRIVATE_SUFFIXES.intersection(p.suffixes) or any(s.startswith(".sqlite") for s in p.suffixes):
        return "credential, raw capture, location or archive format"
    if p.suffix in {".json", ".jsonl", ".ndjson"} and data is not None:
        if len(data) > 4 * 1024 * 1024:
            return "oversized structured data needs explicit publication review"
        try:
            values = [json.loads(data)] if p.suffix == ".json" else [json.loads(line) for line in data.splitlines() if line.strip()]
            if any(has_location_fields(value) for value in values):
                return "location/wireless-bearing structured data"
        except (ValueError, UnicodeError, RecursionError):
            return "uninspectable structured data"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect staged Git index bytes before commit; default inspects HEAD")
    args = parser.parse_args(argv)
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if args.staged else ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"]
    paths = subprocess.check_output(command).decode("utf-8", errors="surrogateescape").split("\0")
    failures = []
    for path in filter(None, paths):
        reason = rejection(path)
        if reason is None and PurePosixPath(path.lower()).suffix in {".json", ".jsonl", ".ndjson"}:
            ref = ":" + path if args.staged else "HEAD:" + path
            size = int(subprocess.check_output(["git", "cat-file", "-s", ref]))
            if size > 4 * 1024 * 1024:
                reason = "oversized structured data needs explicit publication review"
            else:
                data = subprocess.check_output(["git", "show", ref])
                reason = rejection(path, data)
        if reason:
            failures.append((path, reason))
    for path, reason in failures:
        # Report only the filename/category, never private body bytes.
        print(f"Refusing public tree entry {path!r}: {reason}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
