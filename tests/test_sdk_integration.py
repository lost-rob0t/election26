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
    def test_imported_status_time_and_distinct_observations_use_real_contract(self):
        from datetime import datetime
        from test_spool_import import fixture, OBSERVED, COMPLETED
        from election26.spool import import_spool
        with tempfile.TemporaryDirectory() as tmp, patch("election26.archive.inside_git_checkout", return_value=False), patch("election26.spool.inside_git_checkout", return_value=False):
            source, root = Path(tmp).resolve() / "producer", Path(tmp).resolve() / "archive"
            fixture(source, 1, b"synthetic unavailable", status=503, cursor_status="not_parsed", failures=1)
            fixture(source, 2, b"synthetic unavailable", status=503, cursor_status="not_parsed", failures=1)
            with Archive(root, Config(allowed_hosts=["fixture.example"], min_free_bytes=0)).writer() as archive:
                import_spool(archive, source)
                documents = []
                for capture_id in (1, 2):
                    normalize(archive, capture_id)
                    document = sdk().parse_json((root / "canonical" / (str(capture_id) + ".json")).read_text())
                    sdk().validate_document(document)
                    self.assertEqual(document["responseStatus"], 503)
                    self.assertEqual(document["observedAt"], int(datetime.fromisoformat(OBSERVED).timestamp()))
                    self.assertIn("fediwatch:" + "a" * 32 + ":" + str(capture_id), document["sourceRecordIds"])
                    for key in ("startedAt", "endedAt", "challengeStatus"):
                        self.assertNotIn(key, document)
                    documents.append(document)
                self.assertNotEqual(documents[0]["id"], documents[1]["id"])
                self.assertEqual(documents[0]["responseBodyHash"], documents[1]["responseBodyHash"])
                self.assertEqual(archive.db.execute("SELECT captured_at FROM captures WHERE id=1").fetchone()[0], COMPLETED)

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
