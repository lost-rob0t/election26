import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive
from election26.cli import main
from election26.config import BudgetError, Config
from election26.network import PublicHTTP

spec = importlib.util.spec_from_file_location("cron_example", Path(__file__).resolve().parents[1] / "scripts/cron.py")
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class CronTests(unittest.TestCase):
    def test_preview_quotes_paths_and_preserves_existing_entries(self):
        block = cron.block("/private/my venv/election26", "/private/config.json", "/private/archive", "/private/logs/out.log", "rss")
        self.assertIn("'/private/my venv/election26'", block)
        self.assertIn("*/5 * * * *", block)
        self.assertIn("# */15 * * * *", block)
        combined = cron.combine("0 0 * * * existing-job\n", block)
        self.assertTrue(combined.startswith("0 0 * * * existing-job"))
        self.assertRaises(ValueError, cron.combine, combined, block)
        self.assertRaises(ValueError, cron.block, "/bad%path", "/cfg", "/archive", "/log", "rss")

    def test_single_pass_and_overlap_exit_codes(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            root = str(Path(tmp) / "archive")
            cfg = Path(tmp) / "config.json"
            cfg.write_text('{"min_free_bytes":0}')
            args = ["--archive", root, "--config", str(cfg), "run", "--mode", "none"]
            self.assertEqual(main(args), 0)
            with Archive(root, Config(min_free_bytes=0)).writer():
                self.assertEqual(main(args), 75)
            self.assertEqual(main(args), 0)

    def test_request_budget_blocks_network_before_connection(self):
        client = PublicHTTP(Config(allowed_hosts=["news.example"], max_requests=1, request_delay_seconds=0))
        client.request_count = 1
        with patch("election26.network.public_address") as resolver:
            self.assertRaises(BudgetError, client.request, "https://news.example/fixture")
            resolver.assert_not_called()

    def test_time_budget_blocks_network_before_connection(self):
        client = PublicHTTP(Config(allowed_hosts=["news.example"]))
        client.run_deadline = 0
        with patch("election26.network.public_address") as resolver:
            self.assertRaises(BudgetError, client.request, "https://news.example/fixture")
            resolver.assert_not_called()


class ProgressTests(unittest.TestCase):
    def test_failed_oldest_url_does_not_starve_pending_work(self):
        from test_archive import Response
        class Client:
            def open(self, url):
                if url.endswith("/a"):
                    raise OSError("permanent fixture failure")
                response = Response()
                response.url = url
                return response
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            config = Config(allowed_hosts=["news.example"], min_free_bytes=0)
            with Archive(Path(tmp) / "archive", config).writer() as archive:
                archive.enqueue(["https://news.example/a", "https://news.example/b"])
                config.max_urls = 1
                self.assertEqual(archive.fetch(Client())[0]["state"], "failed")
                self.assertEqual(archive.fetch(Client())[0]["state"], "done")
                self.assertEqual(archive.db.execute("SELECT state FROM resources WHERE url LIKE '%/b'").fetchone()[0], "done")

    def test_phase_rotation_prevents_rss_budget_starvation(self):
        from election26.runner import run_pass
        from test_adapters import Response
        class Client:
            def __init__(self):
                self.request_count = 0
            def open(self, url):
                if self.request_count >= 1:
                    raise BudgetError("fixture request cap")
                self.request_count += 1
                if url.endswith("/rss"):
                    return Response(b'<rss><item><link>https://news.example/story</link></item></rss>')
                return Response(b"news fixture", url, "text/html")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            config = Config(allowed_hosts=["news.example"], feeds=["https://news.example/rss"], min_free_bytes=0)
            with Archive(Path(tmp) / "archive", config).writer() as archive:
                first = run_pass(archive, Client(), mode="rss")
                self.assertEqual(first["fetch"][0]["state"], "deferred")
                second = run_pass(archive, Client(), mode="rss")
                self.assertEqual(second["fetch"][0]["state"], "done")
                self.assertEqual(archive.db.execute("SELECT state FROM resources WHERE url LIKE '%/story'").fetchone()[0], "done")


    def test_rss_cursor_rotates_past_broken_feed(self):
        from election26.discovery import discover
        from test_adapters import Response
        class Client:
            def open(self, url):
                if url.endswith("/broken"):
                    raise OSError("fixture failure")
                return Response(b'<rss><item><link>https://news.example/story</link></item></rss>', url)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            config = Config(allowed_hosts=["news.example"], feeds=["https://news.example/broken", "https://news.example/rss"], min_free_bytes=0)
            with Archive(Path(tmp) / "archive", config).writer() as archive:
                self.assertEqual(discover(archive, Client(), "rss")["errors"], ["OSError"])
                self.assertEqual(discover(archive, Client(), "rss")["queued"], 1)

    def test_previously_queued_links_do_not_consume_discovery_limit(self):
        from election26.discovery import discover
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            config = Config(allowed_hosts=["news.example"], manual_urls=["https://news.example/old", "https://news.example/new"], min_free_bytes=0, max_urls=1)
            with Archive(Path(tmp) / "archive", config).writer() as archive:
                archive.enqueue(["https://news.example/old"])
                result = discover(archive, None, "manual")
                self.assertEqual(result["queued"], 1)
                self.assertIsNotNone(archive.db.execute("SELECT 1 FROM resources WHERE url LIKE '%/new'").fetchone())


    def test_budget_consuming_feed_rotates_but_unstarted_feed_keeps_turn(self):
        from election26.discovery import discover
        from test_adapters import Response
        class Client:
            def __init__(self, exhausted=False):
                self.request_count = 1 if exhausted else 0
                self.exhausted = exhausted
            def open(self, url):
                if self.exhausted:
                    raise BudgetError("no request admitted")
                self.request_count += 1
                if url.endswith("/costly"):
                    raise BudgetError("redirects consumed fixture budget")
                return Response(b'<rss><item><link>https://news.example/story</link></item></rss>', url)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, patch("election26.archive.inside_git_checkout", return_value=False):
            config = Config(allowed_hosts=["news.example"], feeds=["https://news.example/costly", "https://news.example/rss"], min_free_bytes=0)
            with Archive(Path(tmp) / "archive", config).writer() as archive:
                self.assertTrue(discover(archive, Client(), "rss")["deferred"])
                self.assertEqual(archive.db.execute("SELECT value FROM meta WHERE key='rss_cursor'").fetchone()[0], "1")
                self.assertTrue(discover(archive, Client(exhausted=True), "rss")["deferred"])
                self.assertEqual(archive.db.execute("SELECT value FROM meta WHERE key='rss_cursor'").fetchone()[0], "1")
                self.assertEqual(discover(archive, Client(), "rss")["queued"], 1)
