"""Synthetic media-policy regression tests; no live source or downloader."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive, status
from election26.config import BudgetError, Config, PolicyError
from election26.discovery import discover, feed_references, media_references
from election26.network import PublicHTTP
from election26.transcribe import transcribe


class Response:
    def __init__(self, url, kind="audio/wav", data=b"synthetic audio", length=None, crash=False):
        self.url = url
        self.headers = {"content-type": kind, "last-modified": "Sat, 10 Oct 2026 12:00:00 GMT"}
        if length is not None:
            self.headers["content-length"] = str(length)
        self.data, self.reads, self.crash = data, 0, crash

    def chunks(self):
        self.reads += 1
        yield self.data
        if self.crash:
            raise OSError("synthetic transfer interruption")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Client:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def open(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def policy(**overrides):
    return dict({"download": True, "types": ["audio"], "max_file_bytes": 1024,
                 "max_total_bytes": 4096, "min_free_bytes": 0}, **overrides)


class MediaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        guard = patch("election26.archive.inside_git_checkout", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        self.root = Path(self.tmp.name) / "private"
        self.config = Config(allowed_hosts=["news.example", "cdn.example"], min_free_bytes=0,
                             request_delay_seconds=0)
        self.archive = Archive(self.root, self.config)

    def opt_in(self, **kwargs):
        self.config.media_policies = {"news.example": policy(**kwargs)}

    def seed(self, archive, url="https://cdn.example/audio.wav", **metadata):
        reference = archive.media_reference(url, "https://news.example/story", **metadata)
        permitted, reason = archive.reference_admission(reference)
        archive.media_event(reference, "pending" if permitted else "not_requested", reason)
        return reference

    def test_config_requires_boolean_types_and_explicit_finite_budgets(self):
        for bad in [{"download": "true"}, policy(types=[]), policy(types=["*"]),
                    policy(max_file_bytes=True), policy(max_total_bytes=0), policy(min_free_bytes=-1),
                    {"download": True, "types": ["audio"]}, policy(transport="yt-dlp")]:
            with self.subTest(bad=bad):
                self.assertRaises(PolicyError, Config, allowed_hosts=["news.example"],
                                  media_policies={"news.example": bad})
        self.assertRaises(PolicyError, Config, media_policies={"news.example": policy()})
        self.assertEqual(Config().media_policies, {})

    def test_html_always_records_references_without_downloading_or_inventing_metadata(self):
        url = "https://news.example/story"
        body = b'<video src="https://cdn.example/v.mp4"></video><img src="https://outside.example/i.png">'
        client = Client({url: Response(url, "text/html", body)})
        with self.archive.writer() as archive:
            archive.fetch(client, [url], extract_media=True)
            records = archive.media_records()
            self.assertEqual(len(records), 2)
            self.assertEqual({r["state"] for r in records}, {"not_requested"})
            self.assertIsNone(records[0]["declared_type"])
            self.assertIsNone(records[0]["declared_bytes"])
            self.assertEqual(records[0]["source_url"], url)
            self.assertEqual(archive.object_path(hashlib.sha256(body).hexdigest()).read_bytes(), body)
            self.assertEqual(archive.fetch(client), [])
        self.assertEqual(client.calls, [url])

    def test_direct_known_media_default_makes_no_request(self):
        url = "https://news.example/audio.wav"
        client = Client({})
        with self.archive.writer() as archive:
            result = archive.fetch(client, [url])
            self.assertEqual(result[0]["state"], "not_requested")
            self.assertEqual(archive.media_records()[0]["reason"], "source_not_opted_in")
        self.assertEqual(client.calls, [])

    def test_opaque_manual_and_rss_urls_cannot_bypass_header_gate(self):
        for path, rss in [("story", False), ("feed", True)]:
            with self.subTest(rss=rss):
                url = "https://news.example/" + path
                response = Response(url)
                client = Client({url: response})
                with self.archive.writer() as archive:
                    if rss:
                        self.config.feeds = [url]
                        discover(archive, client, "rss")
                    else:
                        archive.fetch(client, [url])
                    self.assertEqual(response.reads, 0)
                    self.assertEqual(status(self.root)["capture_observations"], 0)
                    self.assertEqual(archive.media_records()[-1]["response_type"], "audio/wav")

    def test_unknown_binary_mime_is_not_assumed_text(self):
        url = "https://news.example/opaque"
        response = Response(url, "application/octet-stream")
        self.opt_in()
        with self.archive.writer() as archive:
            archive.fetch(Client({url: response}), [url])
            self.assertEqual(response.reads, 0)
            self.assertEqual(archive.media_records()[0]["reason"], "type_not_selected")

    def test_rss_enclosure_type_size_and_source_are_preserved(self):
        url = "https://news.example/rss"
        data = b'<rss><item><enclosure url="https://cdn.example/a.wav" type="audio/wav" length="14"/></item></rss>'
        self.config.feeds = [url]
        client = Client({url: Response(url, "application/rss+xml", data)})
        with self.archive.writer() as archive:
            discover(archive, client, "rss")
            record = archive.media_records()[0]
            self.assertEqual((record["declared_type"], record["declared_bytes"]), ("audio/wav", 14))
            self.assertEqual(record["source_url"], url)
            self.assertEqual(record["state"], "not_requested")
        self.assertEqual(client.calls, [url])

    def test_source_opt_in_authorizes_allowlisted_cdn_without_cdn_opt_in(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        response = Response(url)
        with self.archive.writer() as archive:
            self.seed(archive, url)
            result = archive.fetch(Client({url: response}))
            self.assertEqual(result[0]["state"], "done")
            record = archive.media_records()[0]
            self.assertEqual(record["state"], "downloaded")
            self.assertEqual(record["source_url"], "https://news.example/story")
            capture = archive.db.execute("SELECT * FROM captures").fetchone()
            self.assertEqual(capture["source_url"], record["source_url"])
            self.assertEqual(capture["last_modified"], response.headers["last-modified"])

    def test_cdn_opt_in_cannot_authorize_unapproved_source(self):
        self.config.media_policies = {"cdn.example": policy()}
        with self.archive.writer() as archive:
            self.seed(archive)
            client = Client({})
            archive.fetch(client)
            self.assertEqual(client.calls, [])
            self.assertEqual(archive.media_records()[0]["state"], "not_requested")

    def test_selected_type_and_declared_file_size_prevent_requests(self):
        self.opt_in(max_file_bytes=8)
        with self.archive.writer() as archive:
            self.seed(archive, "https://cdn.example/v.mp4")
            self.seed(archive, "https://cdn.example/a.wav", advertised_bytes=9)
            client = Client({})
            archive.fetch(client)
            self.assertEqual(client.calls, [])
            self.assertEqual({r["reason"] for r in archive.media_records()}, {"type_not_selected", "per_file_budget"})

    def test_response_type_and_length_are_rechecked_before_body(self):
        self.opt_in(max_file_bytes=8)
        for path, mime, size in [("wrong.wav", "video/mp4", 2), ("large.wav", "audio/wav", 9)]:
            url = "https://cdn.example/" + path
            response = Response(url, mime, length=size)
            with self.archive.writer() as archive:
                self.seed(archive, url)
                archive.fetch_media(Client({url: response}), 20)
                self.assertEqual(response.reads, 0)

    def test_streaming_size_limit_catches_absent_or_misleading_header(self):
        self.opt_in(max_file_bytes=3)
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            archive.fetch(Client({url: Response(url, data=b"four", length=1)}))
            self.assertEqual(archive.media_records()[0]["state"], "unavailable")
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.assertFalse(list((self.root / "objects").glob("*/*")))

    def test_per_source_total_budget_counts_existing_originals(self):
        self.opt_in(max_total_bytes=5)
        first, second = "https://cdn.example/one.wav", "https://cdn.example/two.wav"
        with self.archive.writer() as archive:
            self.seed(archive, first)
            archive.fetch(Client({first: Response(first, data=b"four")}))
            self.seed(archive, second, advertised_bytes=2)
            client = Client({})
            archive.fetch(client)
            self.assertEqual(client.calls, [])
            self.assertEqual(archive.media_records()[-1]["reason"], "source_disk_budget")
            self.assertEqual(status(self.root)["capture_observations"], 1)

    def test_source_free_space_reserve_blocks_before_body(self):
        self.opt_in(min_free_bytes=10 * 1024 * 1024)
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            with patch("shutil.disk_usage", return_value=type("Disk", (), {"free": 5 * 1024 * 1024})()):
                client = Client({})
                archive.fetch(client)
                self.assertEqual(client.calls, [])
                self.assertEqual(archive.media_records()[0]["reason"], "free_disk_reserve")

    def test_policy_change_admits_existing_reference_and_keeps_all_observations(self):
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            original = (self.root / "manifests/media-1.json").read_bytes()
            self.opt_in()
            archive.fetch(Client({url: Response(url)}))
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")
            self.assertEqual((self.root / "manifests/media-1.json").read_bytes(), original)
            self.config.media_policies = {}
            archive.fetch(Client({}))
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")

    def test_unavailable_and_not_requested_are_distinct_and_download_failure_retries(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            archive.fetch(Client({url: OSError("synthetic missing source")}))
            self.assertEqual(archive.media_records()[0]["state"], "unavailable")
            archive.fetch(Client({url: Response(url)}))
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")

    def test_replay_reports_missing_without_rewriting_capture_or_observation(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            archive.fetch(Client({url: Response(url)}))
            record = archive.media_records()[0]
            manifest = self.root / "manifests" / ("media-" + str(record["id"]) + ".json")
            original = manifest.read_bytes()
            # Simulate operator storage loss only in an isolated synthetic fixture.
            archive.object_path(record["sha256"]).unlink()
            replay = archive.media_records()[0]
            self.assertEqual((replay["state"], replay["recorded_state"]), ("missing", "downloaded"))
            self.assertEqual(manifest.read_bytes(), original)

    def test_capture_checkpoint_and_downloaded_event_commit_together(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            original = archive.media_event
            def fail_downloaded(reference, state, *args, **kwargs):
                if state == "downloaded":
                    raise OSError("synthetic event failure")
                return original(reference, state, *args, **kwargs)
            with patch.object(archive, "media_event", side_effect=fail_downloaded):
                archive.fetch(Client({url: Response(url)}))
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.assertEqual(archive.media_records()[0]["state"], "unavailable")
            self.assertEqual(len(list((self.root / "objects").glob("*/*"))), 1)

    def test_media_sidecar_repair_and_backup_preserve_outcomes(self):
        with self.archive.writer() as archive:
            self.seed(archive)
            path = self.root / "manifests/media-1.json"
            original = path.read_bytes()
            path.unlink()
            archive.repair_manifests()
            self.assertEqual(path.read_bytes(), original)
            archive.backup(Path(self.tmp.name) / "backup")
        with Archive(Path(self.tmp.name) / "backup", self.config).writer() as restored:
            self.assertEqual(restored.media_records()[0]["state"], "not_requested")

    def test_transcription_never_fetches_missing_media(self):
        with self.archive.writer() as archive:
            self.seed(archive)
            with patch.object(PublicHTTP, "open", side_effect=AssertionError("No network")):
                self.assertRaises(PolicyError, transcribe, archive, "a" * 64, "/missing-model")
            self.assertEqual(archive.media_records()[0]["state"], "not_requested")

    def test_reference_window_rotates_and_respects_run_limit(self):
        self.opt_in()
        self.config.max_urls = 1
        a, b = "https://cdn.example/a.wav", "https://cdn.example/b.wav"
        with self.archive.writer() as archive:
            self.seed(archive, a)
            self.seed(archive, b)
            client = Client({a: Response(a), b: Response(b)})
            archive.fetch(client)
            self.assertEqual(len(client.calls), 1)
            archive.fetch(client)
            self.assertEqual(len(client.calls), 2)

    def test_request_budget_defers_media_without_marking_unavailable(self):
        self.opt_in()
        with self.archive.writer() as archive:
            self.seed(archive)
            client = PublicHTTP(self.config)
            client.request_count = self.config.max_requests
            with patch("election26.network.public_address") as resolver:
                result = archive.fetch(client)
            resolver.assert_not_called()
            self.assertEqual(result[0]["state"], "deferred")
            self.assertEqual(archive.media_records()[0]["state"], "pending")

    def test_policy_media_uses_existing_robots_and_redirect_boundaries(self):
        from test_network import Connection, Raw
        self.opt_in()
        url = "https://cdn.example/a.wav"
        Connection.calls = []
        Connection.responses = [Raw(404), Raw(302, headers={"Location": "https://news.example/private.wav"}),
                                Raw(200, b"User-agent: *\nDisallow: /private.wav")]
        with self.archive.writer() as archive, patch("election26.network.public_address", return_value="8.8.8.8"), patch("election26.network.PinnedHTTPS", Connection):
            self.seed(archive, url)
            archive.fetch(PublicHTTP(self.config))
            self.assertEqual(archive.media_records()[0]["state"], "unavailable")
            self.assertNotIn(("news.example", "/private.wav"), [(h, p) for h, p, _, _ in Connection.calls])

    def test_no_downloader_transport_or_implicit_subprocess(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive, patch("subprocess.run", side_effect=AssertionError("No downloader")):
            self.seed(archive, url)
            archive.fetch(Client({url: Response(url)}))
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")


    def test_media_reference_and_extension_never_fall_back_to_text_capture(self):
        self.opt_in()
        for url in ["https://cdn.example/audio.wav", "https://cdn.example/audio%2Ewav", "https://cdn.example/opaque"]:
            with self.subTest(url=url), self.archive.writer() as archive:
                self.seed(archive, url, kind_hint="audio")
                response = Response(url, "text/plain", b"body must not be read")
                archive.fetch_media(Client({url: response}), 20)
                self.assertEqual(response.reads, 0)
                self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_redirect_and_declared_mime_disagreement_fail_closed(self):
        self.opt_in(types=["audio", "video"])
        original = "https://cdn.example/a.wav"
        response = Response("https://cdn.example/v.mp4", "video/mp4")
        with self.archive.writer() as archive:
            self.seed(archive, original, content_type="audio/wav", kind_hint="audio")
            archive.fetch(Client({original: response}))
            self.assertEqual(response.reads, 0)
            self.assertEqual(archive.media_records()[0]["reason"], "type_mismatch")

    def test_global_archive_budget_blocks_even_when_source_budget_is_larger(self):
        self.opt_in()
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            self.config.max_archive_bytes = archive.disk_bytes()
            client = Client({url: Response(url)})
            self.assertRaises(PolicyError, archive.fetch, client)
            self.assertEqual(client.calls, [])
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_malformed_feed_length_preserves_link_with_unknown_size(self):
        refs = feed_references(b'<rss><enclosure url="https://cdn.example/a.wav" length="bad"/></rss>', "https://news.example/rss")
        self.assertEqual(refs[0]["url"], "https://cdn.example/a.wav")
        self.assertIsNone(refs[0]["advertised_bytes"])
        self.assertIsNone(refs[0]["content_type"])

    def test_version_one_upgrade_preserves_bytes_timestamps_and_legacy_sidecars(self):
        self.root.mkdir()
        old_body = b"old audio fixture"
        digest = hashlib.sha256(old_body).hexdigest()
        old_manifest = b'{"original":"legacy sidecar unchanged"}'
        objects = self.root / "objects" / digest[:2]
        objects.mkdir(parents=True)
        (objects / digest).write_bytes(old_body)
        (self.root / "manifests").mkdir()
        (self.root / "manifests/1.json").write_bytes(old_manifest)
        db = sqlite3.connect(self.root / "catalog.sqlite")
        db.executescript('''
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO meta VALUES ('format_version','1');
          CREATE TABLE resources(url TEXT PRIMARY KEY,discovered_at TEXT NOT NULL,via TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
          CREATE TABLE captures(id INTEGER PRIMARY KEY,url TEXT NOT NULL REFERENCES resources(url),
            final_url TEXT NOT NULL,captured_at TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
            content_type TEXT NOT NULL,etag TEXT,last_modified TEXT,content_encoding TEXT);
          INSERT INTO resources(url,discovered_at,via,state) VALUES ('https://news.example/old.wav','original-time','manual','done');
          INSERT INTO resources(url,discovered_at,via) VALUES ('https://cdn.example/queued.wav','original-time','html-media');
        ''')
        db.execute("INSERT INTO captures VALUES (1,?,?,?,?,?,?,?,?,?)", ("https://news.example/old.wav",
            "https://cdn.example/old.wav", "original-capture-time", digest, len(old_body), "audio/wav", None, "original-source-time", None))
        db.commit()
        db.close()
        self.config.media_policies = {"cdn.example": policy()}
        with self.archive.writer() as archive:
            archive.fetch(Client({}))
            capture = archive.db.execute("SELECT * FROM captures").fetchone()
            self.assertEqual(capture["captured_at"], "original-capture-time")
            self.assertEqual(capture["last_modified"], "original-source-time")
            self.assertEqual((objects / digest).read_bytes(), old_body)
            self.assertEqual((self.root / "manifests/1.json").read_bytes(), old_manifest)
            legacy = archive.media_records()[0]
            self.assertIsNone(legacy["source_url"])
            self.assertEqual(legacy["state"], "not_requested")
            self.assertEqual(archive.db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()[0], "2")

    def test_explicit_fetch_keeps_opaque_policy_media_semantics(self):
        url = "https://cdn.example/opaque"
        response = Response(url, "text/plain", b"not an ordinary document")
        with self.archive.writer() as archive:
            self.seed(archive, url, kind_hint="audio")
            archive.enqueue([url], via="policy-media", source_url="https://news.example/story")
            result = archive.fetch(Client({url: response}), [url])
            self.assertEqual(result[0]["state"], "not_requested")
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.opt_in()
            result = archive.fetch(Client({url: response}), [url])
            self.assertEqual(result[0]["state"], "not_requested")
            self.assertEqual(response.reads, 0)
            self.assertEqual(archive.media_records()[0]["reason"], "unexpected_response_type")

    def test_opaque_reference_keeps_media_semantics_when_manually_queued(self):
        url = "https://cdn.example/opaque"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            self.config.media_policies = {"cdn.example": policy()}
            response = Response(url, "text/plain")
            archive.fetch(Client({url: response}), [url])
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_failed_ordinary_url_cannot_starve_pending_media(self):
        self.opt_in()
        self.config.max_urls = 1
        article, media = "https://news.example/broken", "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            archive.enqueue([article])
            self.seed(archive, media)
            client = Client({article: OSError("permanent fixture failure"), media: Response(media)})
            archive.fetch(client)
            self.assertEqual(client.calls, [article])
        with self.archive.writer() as archive:
            archive.fetch(client)
            self.assertEqual(client.calls, [article, media])
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")

    def test_failed_media_cannot_starve_pending_ordinary_url(self):
        self.opt_in()
        self.config.max_urls = 1
        article, media = "https://news.example/story", "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, media)
            archive.fetch(Client({media: OSError("permanent fixture failure")}))
            archive.enqueue([article])
            client = Client({article: Response(article, "text/html"), media: OSError("permanent fixture failure")})
            archive.fetch(client)
            archive.fetch(client)
            self.assertIn(article, client.calls)
            self.assertEqual(archive.db.execute("SELECT state FROM resources WHERE url=?", (article,)).fetchone()[0], "done")

    def make_v1_catalog(self, partial=False):
        self.root.mkdir()
        db = sqlite3.connect(self.root / "catalog.sqlite")
        db.executescript('''
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO meta VALUES ('format_version','1');
          CREATE TABLE resources(url TEXT PRIMARY KEY,discovered_at TEXT NOT NULL,via TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
          CREATE TABLE captures(id INTEGER PRIMARY KEY,url TEXT NOT NULL REFERENCES resources(url),
            final_url TEXT NOT NULL,captured_at TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
            content_type TEXT NOT NULL,etag TEXT,last_modified TEXT,content_encoding TEXT);
          INSERT INTO resources(url,discovered_at,via) VALUES ('https://news.example/a.wav','original-time','manual');
          INSERT INTO resources(url,discovered_at,via) VALUES ('https://cdn.example/old.wav','original-time','html-media');
        ''')
        if partial:
            db.execute("ALTER TABLE resources ADD COLUMN source_url TEXT")
        db.commit()
        db.close()

    def test_partial_v1_column_addition_is_backfilled_on_resume(self):
        self.make_v1_catalog(partial=True)
        self.opt_in()
        url = "https://news.example/a.wav"
        with self.archive.writer() as archive:
            rows = dict(archive.db.execute("SELECT url,source_url FROM resources"))
            self.assertEqual(rows[url], url)
            self.assertIsNone(rows["https://cdn.example/old.wav"])
            archive.fetch(Client({url: Response(url)}), [url])
            self.assertEqual(archive.media_records()[0]["state"], "downloaded")

    def test_interrupted_migration_rolls_back_schema_and_version_together(self):
        self.make_v1_catalog()
        connect = sqlite3.connect
        class Interrupted(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("UPDATE resources SET source_url"):
                    raise OSError("synthetic interruption after ALTER")
                return super().execute(sql, *args, **kwargs)
        with patch("election26.archive.sqlite3.connect", side_effect=lambda *a, **k: connect(*a, **k, factory=Interrupted)):
            with self.assertRaises(OSError):
                with self.archive.writer():
                    pass
        db = connect(self.root / "catalog.sqlite")
        try:
            self.assertNotIn("source_url", {r[1] for r in db.execute("PRAGMA table_info(resources)")})
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()[0], "1")
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='media_references'").fetchone())
        finally:
            db.close()
        with self.archive.writer() as archive:
            self.assertEqual(archive.db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()[0], "2")

    def test_migration_reserves_disk_before_alter_or_backfill(self):
        self.make_v1_catalog()
        self.config.max_archive_bytes = (self.root / "catalog.sqlite").stat().st_size + 1024
        with self.assertRaises(PolicyError):
            with self.archive.writer():
                pass
        db = sqlite3.connect(self.root / "catalog.sqlite")
        try:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()[0], "1")
            self.assertNotIn("source_url", {r[1] for r in db.execute("PRAGMA table_info(resources)")})
        finally:
            db.close()

    def test_policy_change_is_pending_when_budget_defers_request(self):
        url = "https://cdn.example/a.wav"
        with self.archive.writer() as archive:
            self.seed(archive, url)
            original = (self.root / "manifests/media-1.json").read_bytes()
            self.opt_in()
            result = archive.fetch(Client({url: BudgetError("synthetic exhausted pass")}))
            self.assertEqual(result[0]["state"], "deferred")
            record = archive.media_records()[0]
            self.assertEqual(record["state"], "pending")
            self.assertEqual(record["source_policy"], self.config.media_policies["news.example"])
            self.assertEqual((self.root / "manifests/media-1.json").read_bytes(), original)

    def test_shared_capture_gate_recognizes_existing_opaque_media_reference(self):
        url = "https://news.example/opaque"
        with self.archive.writer() as archive:
            self.seed(archive, url, kind_hint="audio")
            archive.enqueue([url])
            response = Response(url, "text/plain")
            record = archive.capture(url, response)
            self.assertEqual(record["state"], "not_requested")
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.opt_in()
            record = archive.capture(url, response)
            self.assertEqual(record["state"], "not_requested")
            self.assertEqual(response.reads, 0)

    def test_rss_capture_keeps_known_media_semantics_even_when_response_is_xml(self):
        url = "https://news.example/opaque"
        self.config.feeds = [url]
        with self.archive.writer() as archive:
            self.seed(archive, url, kind_hint="audio")
            response = Response(url, "application/rss+xml", b"<rss/>")
            discover(archive, Client({url: response}), "rss")
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)
            self.opt_in()
            discover(archive, Client({url: response}), "rss")
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_redirect_to_known_opaque_media_cannot_become_text_capture(self):
        url, final = "https://news.example/article", "https://cdn.example/opaque"
        with self.archive.writer() as archive:
            self.seed(archive, final, kind_hint="audio")
            archive.enqueue([url])
            response = Response(final, "text/plain")
            self.assertEqual(archive.capture(url, response)["state"], "not_requested")
            self.assertEqual(response.reads, 0)
            self.assertEqual(status(self.root)["capture_observations"], 0)


if __name__ == "__main__":
    unittest.main()
