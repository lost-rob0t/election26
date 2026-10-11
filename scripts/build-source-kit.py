#!/usr/bin/env python3
"""Build a deterministic source-only ZIP from the explicit reviewed allowlist."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_tree", ROOT / "scripts/check-public-tree.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def build(root, output, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Provide the exact reviewed 40-character source commit")
    root, output = Path(root).resolve(), Path(output).absolute()
    if output == root or root in output.parents or output.exists() or output.is_symlink():
        raise ValueError("Output must be a new ZIP outside the source tree")
    paths = json.loads((root / "source-kit-files.json").read_text())
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise ValueError("Invalid source allowlist")
    files = {}
    for name in sorted(paths):
        item = PurePosixPath(name)
        if item.is_absolute() or ".." in item.parts or "\\" in name or ".git" in item.parts:
            raise ValueError("Unsafe source path")
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError("Missing or symlinked source entry: " + name)
        data = path.read_bytes()
        reason = guard.rejection(name, data)
        if reason:
            raise ValueError("Rejected source entry " + name + ": " + reason)
        files[name] = data
    files["SOURCE-PROVENANCE.json"] = (json.dumps({
        "repository": "https://github.com/lost-rob0t/election26", "source_commit": commit,
        "content": "Public code, docs, reference URLs, metadata-only source probes and synthetic tests. No collected archive.",
        "command_after_extraction": "bash ./start-archive.sh",
        "dependencies": "Termux Android; official Termux Python packages and initial Python build dependencies require internet.",
    }, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                                    for name, data in sorted(files.items())).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo("election26-termux-archive-kit/" + name, (2026, 10, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n")
    return {"zip": str(output), "sha256": digest, "entries": len(files), "source_commit": commit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output, args.commit), sort_keys=True))
