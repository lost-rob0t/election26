"""Runs only in the optional-SDK CI job, against the real pinned dependency."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive, status
from election26.config import Config
from election26.normalize import normalize, sdk


@unittest.skipUnless(os.environ.get("ELECTION26_TEST_STARINTEL") == "1", "optional SDK integration job only")
class PinnedSDKTests(unittest.TestCase):
    def test_real_sdk_builds_valid_canonical_metadata_and_keeps_raw(self):
        from test_archive import Client
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            root = Path(tmp) / "private-fixture"
            with Archive(root, Config(allowed_hosts=["news.example"], min_free_bytes=0)).writer() as archive:
                capture = archive.fetch(Client(), ["https://news.example/story"])[0]
                result = normalize(archive, 1)
                adapter = sdk()
                document = adapter.parse_json((root / "canonical/1.json").read_text())
                adapter.validate_document(document)
                self.assertEqual(document["schemaVersion"], "0.10.1")
                self.assertEqual(document["dtype"], "http-transaction")
                self.assertEqual(document["responseBodyHash"], capture["sha256"])
                self.assertIn("observedAt", document)
                for field in ("startedAt", "endedAt", "challengeStatus"):
                    self.assertNotIn(field, document)
                self.assertEqual(result["source_sha256"], capture["sha256"])
                self.assertTrue(archive.object_path(capture["sha256"]).exists())
                self.assertEqual(status(root)["canonical_documents"], 1)
                self.assertEqual(normalize(archive, 1)["state"], "already_normalized")
