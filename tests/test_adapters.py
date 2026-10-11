import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from election26.archive import Archive, status
from election26.config import Config, PolicyError
from election26.discovery import discover
from election26.reporting import publish
from election26.transcribe import transcribe, verify_transcript


class Response:
    def __init__(self, data, url="https://news.example/rss", content_type="application/xml", code=200):
        self.data = data
        self.url = url
        self.status = code
        self.headers = {"content-type": content_type}

    def read_bounded(self, limit):
        if len(self.data) > limit:
            raise PolicyError("Response too large")
        return self.data

    def chunks(self):
        yield self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, url):
        self.calls.append({"url": url})
        return self.response

    def request(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class AdapterTests(unittest.TestCase):
    def setUp(self):
        # The execution harness may put /tmp itself under Git; these are
        # synthetic fixtures only. The production path guard has separate tests.
        guard = patch("election26.archive.inside_git_checkout", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "private"
        self.config = Config(allowed_hosts=["news.example"], min_free_bytes=0)
        self.archive = Archive(self.root, self.config)

    def test_rss_keeps_original_and_queues_only_allowlist(self):
        data = b'<rss><item><link>https://news.example/story</link></item><item><link>https://other.example/story</link></item></rss>'
        self.config.feeds = ["https://news.example/rss"]
        with self.archive.writer() as archive:
            result = discover(archive, Client(Response(data)), "rss")
            self.assertEqual(result["queued"], 1)
            self.assertEqual(result["outside_allowlist_or_invalid"], 1)
            self.assertEqual(status(self.root)["capture_observations"], 1)
            self.assertTrue(archive.object_path(hashlib.sha256(data).hexdigest()).exists())

    def test_rss_and_atom_enclosures_stay_raw_and_are_not_queued(self):
        data = b'''<rss><item><link>https://news.example/story</link>
            <enclosure url="https://news.example/audio.mp3" type="audio/mpeg"/>
            <link rel="enclosure" href="https://news.example/video.mp4"/>
            </item></rss>'''
        self.config.feeds = ["https://news.example/rss"]
        client = Client(Response(data))
        with self.archive.writer() as archive:
            result = discover(archive, client, "rss")
            self.assertEqual(result["queued"], 1)
            queued = {r[0] for r in archive.db.execute("SELECT url FROM resources")}
            self.assertEqual(queued, {"https://news.example/rss", "https://news.example/story"})
            self.assertEqual(archive.object_path(hashlib.sha256(data).hexdigest()).read_bytes(), data)
            self.assertEqual(len(client.calls), 1)

    def test_brave_opt_in_shape_and_allowlist(self):
        client = Client(Response(b'{"web":{"results":[{"url":"https://news.example/story"}]}}'))
        with self.archive.writer() as archive:
            self.assertRaises(PolicyError, discover, archive, client, "brave", "US election 2026")
            self.config.brave_enabled = True
            with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "synthetic-key"}):
                self.assertEqual(discover(archive, client, "brave", "US election 2026")["queued"], 1)
            call = client.calls[0]
            self.assertTrue(call["url"].startswith("https://api.search.brave.com/res/v1/web/search?"))
            self.assertNotIn("synthetic-key", call["url"])
            self.assertEqual(call["headers"]["X-Subscription-Token"], "synthetic-key")
            self.assertIn("country=US", call["url"])

    def test_discord_disabled_then_receipt_and_rate_limit(self):
        client = Client(Response(b'{"id":"12345"}'))
        with self.archive.writer() as archive:
            self.assertFalse(publish(archive, client)["sent"])
            self.assertEqual(client.calls, [])
            self.assertRaises(PolicyError, publish, archive, client, True)
            self.config.discord_enabled = True
            with patch.dict(os.environ, {"ELECTION26_DISCORD_WEBHOOK": "https://discord.com/api/webhooks/123/fixture-token"}):
                self.assertTrue(publish(archive, client, True)["sent"])
                self.assertFalse(publish(archive, client, True)["sent"])
            self.assertEqual(len(client.calls), 1)
            self.assertTrue(client.calls[0]["url"].endswith("?wait=true"))
            self.assertNotIn("fixture-token", client.calls[0]["body"].decode())

    def test_discord_uncertain_send_does_not_repeat(self):
        client = Client(Response(b"{}", code=500))
        self.config.discord_enabled = True
        with self.archive.writer() as archive, patch.dict(os.environ, {"ELECTION26_DISCORD_WEBHOOK": "https://discord.com/api/webhooks/123/fixture-token"}):
            self.assertRaises(PolicyError, publish, archive, client, True)
            self.assertRaises(PolicyError, publish, archive, client, True)
            self.assertEqual(len(client.calls), 1)

    def test_local_transcription_provenance_and_verification(self):
        source = b"synthetic audio bytes"
        digest = hashlib.sha256(source).hexdigest()
        model = Path(self.temp.name) / "trusted-model.pt"
        model.write_bytes(b"synthetic model")
        fake = types.SimpleNamespace(load_model=lambda *a, **k: types.SimpleNamespace(transcribe=lambda *a, **k: {"text": "Synthetic transcript", "language": "en", "segments": []}))
        with self.archive.writer() as archive:
            archive.enqueue(["https://news.example/audio"])
            archive.capture("https://news.example/audio", Response(source, "https://news.example/audio", "audio/wav"))
            numpy = types.SimpleNamespace(frombuffer=lambda *a, **k: types.SimpleNamespace(astype=lambda *a: 0), float32="float32")
            with patch.dict("sys.modules", {"whisper": fake, "numpy": numpy}), patch("importlib.metadata.version", return_value="fixture"), patch("election26.transcribe.pcm_samples", return_value=b"\0\0"):
                result = transcribe(archive, digest, model)
                self.assertFalse(result["human_verified"])
                self.assertEqual(transcribe(archive, digest, model)["state"], "already_transcribed")
            self.assertTrue(verify_transcript(archive, digest)["human_verified"])
            self.assertTrue(archive.object_path(digest).exists())

    def test_failed_transcription_keeps_original(self):
        source = b"audio fixture"
        digest = hashlib.sha256(source).hexdigest()
        with self.archive.writer() as archive:
            archive.enqueue(["https://news.example/audio"])
            archive.capture("https://news.example/audio", Response(source, "https://news.example/audio", "audio/wav"))
            self.assertRaises(PolicyError, transcribe, archive, digest, "/missing-model")
            self.assertEqual(archive.object_path(digest).read_bytes(), source)
            self.assertEqual(status(self.root)["capture_observations"], 1)
