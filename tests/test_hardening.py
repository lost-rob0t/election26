import hashlib
import http.client
import io
import json
import socket
from pathlib import Path
import tempfile
import types
import unittest
import wave
from unittest.mock import patch

from election26.archive import Archive, status, sync_dir
from election26.config import Config, PolicyError
from election26.network import RequestDeadline, Response
from election26.normalize import normalize
from election26.transcribe import pcm_samples


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        guard = patch("election26.archive.inside_git_checkout", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        self.root = Path(self.tmp.name) / "archive"
        self.config = Config(allowed_hosts=["news.example"], min_free_bytes=0)
        self.archive = Archive(self.root, self.config)

    def test_opt_ins_and_numeric_bounds_are_strict(self):
        for field, value in [("brave_enabled", "false"), ("discord_enabled", 1), ("max_urls", True),
                             ("min_free_bytes", float("nan")), ("request_delay_seconds", float("nan")),
                             ("discord_interval_seconds", False)]:
            with self.subTest(field=field):
                self.assertRaises(PolicyError, Config, **{field: value})
        config = Path(self.tmp.name) / "bad.json"
        config.write_text('{"request_delay_seconds":NaN}')
        self.assertRaises(PolicyError, Config.load, config)

    def test_nested_symlink_and_parent_paths_are_rejected(self):
        body = b"fixture"
        digest = hashlib.sha256(body).hexdigest()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        with self.archive.writer() as archive:
            (self.root / "objects" / digest[:2]).symlink_to(outside, target_is_directory=True)
            self.assertRaises(PolicyError, archive.store, [body])
            self.assertFalse(list(outside.iterdir()))
            self.assertRaises(PolicyError, archive.safe_path, "../outside")
            self.assertRaises(PolicyError, archive.safe_path, str(outside))
        with self.assertRaises(PolicyError):
            with self.archive.writer():
                pass

    def test_git_archive_and_backup_guards(self):
        with patch("election26.archive.inside_git_checkout", return_value=True):
            self.assertRaises(PolicyError, Archive, self.root, self.config)
        with self.archive.writer() as archive, patch("election26.archive.inside_git_checkout", return_value=True):
            self.assertRaises(PolicyError, archive.backup, Path(self.tmp.name) / "public-repo-backup")

    def test_new_shard_parent_is_synced_before_capture_commit(self):
        from test_archive import Client
        with self.archive.writer() as archive:
            events = []
            archive.db.set_trace_callback(lambda sql: events.append("COMMIT") if sql == "COMMIT" else None)
            def sync(path):
                events.append(str(path))
                sync_dir(path)
            with patch("election26.archive.sync_dir", side_effect=sync):
                archive.fetch(Client(), ["https://news.example/story"])
            object_sync = events.index(str(self.root / "objects"))
            later_commits = [i for i, event in enumerate(events) if event == "COMMIT" and i > object_sync]
            self.assertTrue(later_commits)

    def test_large_metadata_is_not_committed(self):
        from test_archive import Client, Response
        response = Response(b"fixture")
        response.headers = {"content-type": "x" * 60000, "etag": "x" * 60000}
        with self.archive.writer() as archive:
            self.assertEqual(archive.fetch(Client(response), ["https://news.example/story"])[0]["state"], "failed")
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_committed_capture_survives_sidecar_failure(self):
        from test_archive import Client
        with self.archive.writer() as archive:
            with patch("election26.archive.atomic_json", side_effect=OSError("fixture failure")):
                self.assertEqual(archive.fetch(Client(), ["https://news.example/story"])[0]["state"], "done")
            self.assertEqual(status(self.root)["queue"], {"done": 1})
            archive.repair_manifests()
            self.assertTrue((self.root / "manifests/1.json").exists())

    def test_explicit_url_is_selected_before_queue_limit(self):
        from test_archive import Client
        with self.archive.writer() as archive:
            archive.enqueue(["https://news.example/a", "https://news.example/b"])
            self.config.max_urls = 1
            archive.fetch(Client(), ["https://news.example/z"])
            self.assertEqual(archive.db.execute("SELECT state FROM resources WHERE url='https://news.example/z'").fetchone()[0], "done")

    def test_blocked_socket_body_is_interrupted(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
        raw = http.client.HTTPResponse(left)
        raw.begin()
        deadline = RequestDeadline(types.SimpleNamespace(sock=left), seconds=0.02)
        deadline.sock = left
        response = Response(raw, types.SimpleNamespace(close=lambda: None), "https://news.example/fixture", deadline)
        with response:
            self.assertRaises(PolicyError, lambda: list(response.chunks()))

    def test_blocked_socket_headers_are_interrupted(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        raw = http.client.HTTPResponse(left)
        deadline = RequestDeadline(types.SimpleNamespace(sock=left), seconds=0.02)
        try:
            self.assertRaises(http.client.RemoteDisconnected, raw.begin)
        finally:
            deadline.cancel()
            raw.close()

    def test_pcm_decoder_rejects_playlists_truncation_and_long_audio(self):
        path = Path(self.tmp.name) / "audio"
        path.write_bytes(b"#EXTM3U\nhttp://127.0.0.1/private\n")
        self.assertRaises(wave.Error, pcm_samples, path)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16)
        self.assertEqual(len(pcm_samples(path)), 32)
        data = bytearray(path.read_bytes())
        data[40:44] = (16000 * 601 * 2).to_bytes(4, "little")
        path.write_bytes(data)
        self.assertRaises(PolicyError, pcm_samples, path)
        data[40:44] = (64).to_bytes(4, "little")
        path.write_bytes(data)
        self.assertRaises(PolicyError, pcm_samples, path)

    def test_normalization_failure_never_discards_original(self):
        from test_archive import Client
        with self.archive.writer() as archive:
            result = archive.fetch(Client(), ["https://news.example/story"])
            digest = result[0]["sha256"]
            with patch("election26.normalize.sdk", side_effect=PolicyError("SDK missing")):
                self.assertRaises(PolicyError, normalize, archive, 1)
            self.assertTrue(archive.object_path(digest).exists())
            self.assertEqual(status(self.root)["capture_observations"], 1)


    def test_multilevel_archive_creation_syncs_every_new_ancestor(self):
        root = Path(self.tmp.name) / "new-parent" / "nested" / "archive"
        seen = []
        def sync(path):
            seen.append(path)
            sync_dir(path)
        with patch("election26.archive.sync_dir", side_effect=sync):
            with Archive(root, self.config).writer():
                pass
        for directory in [Path(self.tmp.name), root.parent.parent, root.parent, root]:
            self.assertIn(directory, seen)
        self.assertLess(seen.index(Path(self.tmp.name)), seen.index(root))
