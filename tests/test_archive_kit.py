import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from election26 import kit
from election26.archive import Archive, status
from election26.config import BudgetError, Config, PolicyError
from test_adapters import Response

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kit_termux_setup", ROOT / "scripts/termux_setup.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class FixtureHTTP:
    """Only synthetic bytes; no live network or loopback policy bypass."""
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.request_count = 0
        self.run_deadline = time.monotonic() + config.max_run_seconds

    def open(self, url):
        if self.request_count >= self.config.max_requests:
            raise BudgetError("Synthetic request cap")
        self.calls.append(url)
        self.request_count += 1
        if url.endswith("broken"):
            raise OSError("Synthetic unavailable source")
        if "/feed" in url:
            number = url.rsplit("feed", 1)[1]
            return Response(('<rss><item><link>https://news.example/story' + number +
                '</link><enclosure url="https://media.example/clip.mp3" type="audio/mpeg"/></item></rss>').encode(), url)
        return Response(b'<html>synthetic story<img src="https://media.example/picture.jpg"></html>', url, "text/html")


class ArchiveKitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cwd = self.home / "archive kit with spaces"
        self.cwd.mkdir(mode=0o700)
        # Only synthetic fixture directories may sit beneath the harness Git root.
        for name in ("election26.archive.inside_git_checkout", "election26.kit.inside_git_checkout"):
            guard = patch(name, return_value=False)
            guard.start()
            self.addCleanup(guard.stop)
        self.config = Config(allowed_hosts=["news.example"],
            feeds=["https://news.example/feed1", "https://news.example/feed2"],
            manual_urls=["https://news.example/manual"], min_free_bytes=0,
            max_requests=3, max_run_seconds=30, max_urls=10)
        self.events = []

    def report(self, event, **values):
        self.events.append({"event": event, **values})

    def batch(self, config=None):
        config = config or self.config
        client = FixtureHTTP(config)
        with Archive(self.cwd / "archive", config).writer() as archive:
            code = kit.run_batch(archive, client, self.report)
        return code, client

    def test_two_runs_resume_same_archive_and_keep_media_links(self):
        first, client = self.batch()
        self.assertEqual(first, 75)
        self.assertEqual(len(client.calls), 3)
        first_objects = {p: p.read_bytes() for p in (self.cwd / "archive/objects").glob("*/*")}
        second, client = self.batch()
        self.assertEqual(second, 0)
        self.assertEqual(set(client.calls), {"https://news.example/story1", "https://news.example/story2"})
        self.assertTrue(all(p.read_bytes() == value for p, value in first_objects.items()))
        self.assertEqual(status(self.cwd / "archive")["capture_observations"], 5)
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            self.assertGreater(len(archive.media_records()), 0)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures WHERE media_reference_id IS NOT NULL").fetchone()[0], 0)
        self.assertTrue(self.events[-1]["rotation_complete"])

    def test_aggregate_budget_and_feed_rotation_survive_three_invocations(self):
        self.config.max_requests = 1
        self.config.feeds.append("https://news.example/feed3")
        calls = []
        for expected in ("feed1", "feed2", "feed3"):
            code, client = self.batch()
            self.assertEqual(code, 75)
            self.assertEqual(client.request_count, 1)
            self.assertTrue(client.calls[0].endswith(expected))
            calls.extend(client.calls)
        self.assertEqual(len(set(calls)), 3)
        self.assertEqual(self.events[-1]["feeds_left"], 0)

    def test_failed_feed_reports_failure_and_does_not_starve_next_feed(self):
        self.config.feeds = ["https://news.example/broken", "https://news.example/feed2"]
        self.config.max_requests = 20
        code, client = self.batch()
        self.assertEqual(code, 1)
        self.assertIn("https://news.example/feed2", client.calls)
        self.assertGreater(self.events[-1]["source_errors"], 0)

    def test_admitted_partial_feed_is_never_success(self):
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            client = FixtureHTTP(self.config)
            def partial(*args):
                client.request_count += 1
                return {"queued": 0, "errors": [], "deferred": True, "truncated": False}
            with patch.object(kit, "discover", side_effect=partial):
                self.assertEqual(kit.run_batch(archive, client, self.report), 75)
            self.assertGreater(self.events[-1]["source_deferred"], 0)

    def test_config_created_once_and_existing_archive_retained(self):
        source = self.home / "source bundle"
        (source / "config").mkdir(parents=True)
        profile = source / "config/us-2026-public-starter.json"
        profile.write_text(json.dumps(self.config.__dict__))
        archive, config, _ = kit.prepare(self.cwd, self.home, source)
        original = config.read_bytes()
        self.batch()
        before = status(archive)["capture_observations"]
        profile.write_text('{"min_free_bytes": 0}')
        kit.prepare(self.cwd, self.home, source)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(status(archive)["capture_observations"], before)

    def test_paths_reject_symlinks_hardlinks_git_and_shared_storage(self):
        destination = self.home / "elsewhere"
        destination.mkdir()
        (self.cwd / "archive").symlink_to(destination, target_is_directory=True)
        self.assertRaises(PolicyError, kit.paths, self.cwd, self.home)
        (self.cwd / "archive").unlink()
        (self.cwd / "archive").mkdir()
        secret = self.home / "unrelated"
        secret.write_text("leave alone")
        os.link(secret, self.cwd / "archive/catalog.sqlite")
        self.assertRaises(PolicyError, kit.paths, self.cwd, self.home)
        (self.cwd / "archive/catalog.sqlite").unlink()
        self.assertEqual(secret.read_text(), "leave alone")
        self.assertRaises(PolicyError, kit.paths, self.home.parent, self.home)
        with patch.object(kit, "inside_git_checkout", return_value=True):
            self.assertRaises(PolicyError, kit.paths, self.cwd, self.home)

    def test_existing_unknown_archive_and_config_are_not_replaced(self):
        archive = self.cwd / "archive"
        archive.mkdir()
        sentinel = archive / "operator-file.txt"
        sentinel.write_text("preserve")
        config = self.cwd / "archive-config.json"
        config.write_text(json.dumps(self.config.__dict__))
        self.assertRaises(PolicyError, kit.prepare, self.cwd, self.home, ROOT)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertEqual(json.loads(config.read_text())["max_requests"], 3)

    def test_default_never_calls_brave_even_when_key_and_config_are_enabled(self):
        self.config.brave_enabled = True
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "synthetic-do-not-log"}), patch.object(kit, "brave") as search:
            self.batch()
            search.assert_not_called()

    def test_brave_requires_opt_in_and_env_key_and_keeps_key_out_of_logs(self):
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            client = FixtureHTTP(self.config)
            self.assertRaises(PolicyError, kit.brave, archive, client, "synthetic query")
            self.config.brave_enabled = True
            with patch.dict(os.environ, {}, clear=True):
                self.assertRaises(PolicyError, kit.brave, archive, client, "synthetic query")
            output = io.StringIO()
            with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "synthetic-do-not-log"}), patch.object(kit, "discover", return_value={"queued": 0}) as discover, contextlib.redirect_stdout(output):
                self.assertEqual(kit.brave(archive, client, "synthetic query"), 0)
                discover.assert_called_once_with(archive, client, "brave", "synthetic query")
            self.assertNotIn("synthetic-do-not-log", output.getvalue())

    def test_supervisor_stops_real_blocked_child_without_network(self):
        start = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            code = kit.supervise([sys.executable, "-c", "import time; time.sleep(60)"], 0.1)
        self.assertEqual(code, 75)
        self.assertLess(time.monotonic() - start, 5)

    def test_offline_setup_failure_does_not_start_collection(self):
        text = (ROOT / "start-archive.sh").read_text()
        self.assertIn('if ! bash "$root/scripts/install-termux.sh" --install --if-needed', text)
        self.assertIn("Setup failed.", text)
        self.assertNotIn("--with-cron", text)
        # On non-Android, setup refusal occurs before any writes or network.
        result = subprocess.run(["bash", str(ROOT / "start-archive.sh")], cwd=self.cwd,
            env={**os.environ, "HOME": str(self.home), "PREFIX": str(self.home)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_setup_receipt_detects_changed_source_and_uses_exact_installed_bytes(self):
        source = self.home / "bundle"
        package = source / "src/election26"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# synthetic\n")
        (source / "pyproject.toml").write_text("# synthetic metadata\n")
        venv = self.home / ".local/share/election26/venv"
        installed = venv / "lib/election26"
        installed.mkdir(parents=True)
        (venv / ".election26-managed").write_text(setup.MARKER)
        (installed / "__init__.py").write_bytes((package / "__init__.py").read_bytes())
        import types
        with patch.object(setup.importlib.util, "find_spec", return_value=types.SimpleNamespace(origin=str(installed / "__init__.py"))):
            self.assertFalse(setup.install_receipt(self.home, source))
            self.assertTrue(setup.install_receipt(self.home, source, record=True))
            self.assertTrue(setup.install_receipt(self.home, source))
            (package / "__init__.py").write_text("# changed synthetic\n")
            self.assertFalse(setup.install_receipt(self.home, source))


if __name__ == "__main__":
    unittest.main()
