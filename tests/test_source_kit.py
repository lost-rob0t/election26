import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_kit_builder", ROOT / "scripts/build-source-kit.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class SourceKitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="source kit fixture ")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        (self.source / "hello.py").write_text("# synthetic public code\n")
        (self.source / "source-kit-files.json").write_text('["hello.py", "source-kit-files.json"]')

    def test_deterministic_allowlisted_zip_and_checksums(self):
        # Unknown private/runtime artifacts are never added by a recursive glob.
        (self.source / "operator-data.sqlite").write_bytes(b"synthetic excluded fixture")
        one, two = self.work / "one.zip", self.work / "two.zip"
        builder.build(self.source, one, "0" * 40)
        builder.build(self.source, two, "0" * 40)
        self.assertEqual(one.read_bytes(), two.read_bytes())
        self.assertTrue(one.with_suffix(".zip.sha256").read_text().startswith(hashlib.sha256(one.read_bytes()).hexdigest()))
        with zipfile.ZipFile(one) as bundle:
            names = bundle.namelist()
            self.assertEqual(len(names), 4)
            self.assertFalse(any("operator-data" in name for name in names))
            prefix = "election26-termux-archive-kit/"
            for line in bundle.read(prefix + "SHA256SUMS").decode().splitlines():
                expected, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(bundle.read(prefix + name)).hexdigest(), expected)

    def test_relative_parent_output_is_supported(self):
        cwd = Path.cwd()
        try:
            os.chdir(self.source)
            builder.build(self.source, "../relative.zip", "0" * 40)
        finally:
            os.chdir(cwd)
        self.assertTrue((self.work / "relative.zip").is_file())

    def test_existing_zip_or_checksum_is_never_overwritten(self):
        output = self.work / "existing.zip"
        output.write_text("keep")
        self.assertRaises(ValueError, builder.build, self.source, output, "0" * 40)
        self.assertEqual(output.read_text(), "keep")
        checksum = self.work / "other.zip.sha256"
        checksum.write_text("keep checksum")
        self.assertRaises(ValueError, builder.build, self.source, self.work / "other.zip", "0" * 40)
        self.assertEqual(checksum.read_text(), "keep checksum")

    def test_raw_paths_symlinks_and_parent_traversal_are_rejected(self):
        for name in ("private/raw.json", "../outside.py", "payload.sqlite"):
            (self.source / "source-kit-files.json").write_text(json.dumps([name]))
            self.assertRaises(ValueError, builder.build, self.source, self.work / "bad.zip", "0" * 40)
        (self.source / "source-kit-files.json").write_text('["link.py"]')
        (self.source / "link.py").symlink_to(self.source / "hello.py")
        self.assertRaises(ValueError, builder.build, self.source, self.work / "bad.zip", "0" * 40)


if __name__ == "__main__":
    unittest.main()
