import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive, gc_plan, sha256_file, status
from election26.config import Config, PolicyError
from election26.discovery import feed_links, media_links
from election26.reporting import stats_payload


class Response:
    url = "https://news.example/story"
    headers = {"content-type": "text/html", "etag": '"version1"'}

    def __init__(self, data=b"synthetic news fixture", crash=False):
        self.data = data
        self.crash = crash

    def chunks(self):
        yield self.data
        if self.crash:
            raise OSError("fixture crash")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Client:
    def __init__(self, response=None):
        self.response = response or Response()

    def open(self, url):
        self.response.url = url
        return self.response


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.git_guard = patch("election26.archive.inside_git_checkout", return_value=False)
        self.git_guard.start()
        self.addCleanup(self.git_guard.stop)
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.tmp.name) / "private-archive"
        self.config = Config(allowed_hosts=["news.example"], min_free_bytes=0)
        self.archive = Archive(self.root, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_dedupe_refresh_and_provenance(self):
        with self.archive.writer() as archive:
            archive.fetch(Client(), ["https://news.example/story"])
            self.assertEqual(archive.fetch(Client()), [])
            archive.fetch(Client(), ["https://news.example/copy"])
            archive.fetch(Client(Response(b"version 2")), ["https://news.example/story"], refresh=True)
        result = status(self.root)
        self.assertEqual(result["capture_observations"], 3)
        self.assertEqual(result["unique_objects"], 2)
        manifest = json.loads((self.root / "manifests/1.json").read_text())
        self.assertEqual(manifest["etag"], '"version1"')
        self.assertEqual(sha256_file(self.archive.object_path(manifest["sha256"])), manifest["sha256"])

    def test_download_failure_is_resumable(self):
        with self.archive.writer() as archive:
            result = archive.fetch(Client(Response(crash=True)), ["https://news.example/story"])
            self.assertEqual(result[0]["state"], "failed")
            self.assertEqual(status(self.root)["capture_observations"], 0)
            result = archive.fetch(Client())
            self.assertEqual(result[0]["state"], "done")

    def test_crashed_running_checkpoint_is_resumed(self):
        with self.archive.writer() as archive:
            archive.enqueue(["https://news.example/story"])
            with archive.db:
                archive.db.execute("UPDATE resources SET state='running'")
        with self.archive.writer() as archive:
            self.assertEqual(archive.fetch(Client())[0]["state"], "done")

    def test_two_writers_fail_closed(self):
        with self.archive.writer():
            with self.assertRaises(PolicyError):
                with Archive(self.root, self.config).writer():
                    pass

    def test_limits_and_no_partial_publish(self):
        self.config.max_file_bytes = 3
        with self.archive.writer() as archive:
            self.assertEqual(archive.fetch(Client(), ["https://news.example/story"])[0]["state"], "failed")
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.assertFalse(list((self.root / "objects").glob("*/*")))

    def test_free_space_floor(self):
        with self.archive.writer() as archive:
            self.config.min_free_bytes = 100
            with patch("election26.archive.shutil.disk_usage", return_value=type("Disk", (), {"free": 50})()):
                self.assertRaises(PolicyError, archive.store, [b"fixture"])

    def test_corrupt_catalog_is_not_reset(self):
        self.root.mkdir()
        (self.root / "catalog.sqlite").write_bytes(b"corrupt fixture")
        with self.assertRaises(sqlite3.DatabaseError):
            with self.archive.writer():
                pass
        self.assertEqual((self.root / "catalog.sqlite").read_bytes(), b"corrupt fixture")

    def test_manifest_repair_and_backup_restore(self):
        with self.archive.writer() as archive:
            archive.fetch(Client(), ["https://news.example/story"])
            (self.root / "manifests/1.json").unlink()
            archive.repair_manifests()
            self.assertTrue((self.root / "manifests/1.json").exists())
            backup = Path(self.tmp.name) / "backup"
            archive.backup(backup)
        self.assertEqual(status(backup), status(self.root))
        with Archive(backup, self.config).writer() as restored:
            self.assertEqual(restored.fetch(Client()), [])

    def test_orphan_gc_never_deletes(self):
        with self.archive.writer() as archive:
            digest, _ = archive.store([b"orphan"])
            plan = gc_plan(archive)
            self.assertTrue(plan["dry_run"])
            self.assertFalse(plan["deletion_supported"])
            self.assertIn(digest, plan["orphan_objects"])
            self.assertTrue(archive.object_path(digest).exists())

    def test_media_and_feed_extractors(self):
        self.assertEqual(media_links('<video src="/a.mp4"></video>', "https://news.example/story"), ["https://news.example/a.mp4"])
        self.assertEqual(feed_links(b'<rss><channel><item><link>https://news.example/item</link></item></channel></rss>'), ["https://news.example/item"])
        with self.assertRaises(PolicyError):
            feed_links(b'<!DOCTYPE a [<!ENTITY x "bad">]><a>&x;</a>')

    def test_discord_payload_does_not_leak_arbitrary_fields(self):
        payload = stats_payload({"capture_observations": 3, "secret": "SUPERSECRET", "url": "https://private.example"})
        self.assertNotIn("SUPERSECRET", json.dumps(payload))
        self.assertNotIn("private.example", json.dumps(payload))
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


if __name__ == "__main__":
    unittest.main()
