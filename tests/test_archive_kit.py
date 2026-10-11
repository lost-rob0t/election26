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
        # Match the production launcher's private umask, including on Termux
        # where the ambient interactive-shell umask may allow group writes.
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
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
        catalog = self.cwd / "archive/catalog.sqlite"
        if hasattr(os, "link"):
            os.link(secret, catalog)
            self.assertRaises(PolicyError, kit.paths, self.cwd, self.home)
        else:
            # Android Python may omit os.link. Exercise the same nlink guard
            # with synthetic stat metadata instead of skipping the safety check.
            catalog.write_text("synthetic catalog")
            real_stat = Path.stat
            def linked_stat(path, *args, **kwargs):
                value = real_stat(path, *args, **kwargs)
                if path == catalog:
                    fields = list(value)
                    fields[3] = 2  # st_nlink
                    return os.stat_result(fields)
                return value
            with patch.object(Path, "stat", linked_stat):
                self.assertRaises(PolicyError, kit.paths, self.cwd, self.home)
        catalog.unlink()
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

    def test_supervisor_terminal_signals_do_not_orphan_child(self):
        # Real processes, synthetic sleeping child. No sockets or live collection.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            marker = self.cwd / ("child-" + str(signum))
            child_code = "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(60)"
            supervisor_code = "import sys; from election26.kit import supervise; sys.exit(supervise([sys.executable,'-c'," + repr(child_code) + "],1))"
            supervisor = subprocess.Popen([sys.executable, "-c", supervisor_code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            child_pid = int(marker.read_text())
            try:
                supervisor.send_signal(signum)
                supervisor.communicate(timeout=5)
                self.assertEqual(supervisor.returncode, 75)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child_pid, signal.SIGKILL)

    def test_unattempted_opt_in_media_reports_deferred_until_next_run(self):
        self.config.feeds = []
        self.config.manual_urls = ["https://news.example/page"]
        self.config.allowed_hosts.append("media.example")
        self.config.max_urls = 1
        self.config.media_policies = {"news.example": {"download": True, "types": ["image"],
            "max_file_bytes": 1000, "max_total_bytes": 10000, "min_free_bytes": 0}}
        class MediaHTTP(FixtureHTTP):
            def open(self, url):
                response = super().open(url)
                return Response(b"synthetic image", url, "image/jpeg") if url.endswith(".jpg") else response
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            self.assertEqual(kit.run_batch(archive, MediaHTTP(self.config), self.report), 75)
            self.assertEqual(self.events[-1]["pending_media"], 1)
            self.assertEqual(kit.run_batch(archive, MediaHTTP(self.config), self.report), 0)
            self.assertEqual(self.events[-1]["pending_media"], 0)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures WHERE media_reference_id IS NOT NULL").fetchone()[0], 1)

    def test_failed_opt_in_media_does_not_starve_future_feed_rotations(self):
        self.config.feeds = ["https://news.example/feed1"]
        self.config.manual_urls = ["https://news.example/page"]
        self.config.allowed_hosts.append("media.example")
        self.config.max_urls = 1
        self.config.max_requests = 20
        self.config.media_policies = {"news.example": {"download": True, "types": ["image"],
            "max_file_bytes": 1000, "max_total_bytes": 10000, "min_free_bytes": 0}}
        class UnavailableMediaHTTP(FixtureHTTP):
            def open(self, url):
                response = super().open(url)
                if url.endswith(".jpg"):
                    raise OSError("Synthetic unavailable media")
                return Response(b"<rss/>", url) if "/feed" in url else response
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            self.assertEqual(kit.run_batch(archive, UnavailableMediaHTTP(self.config), self.report), 75)
            self.assertEqual(kit.run_batch(archive, UnavailableMediaHTTP(self.config), self.report), 1)
            self.assertTrue(self.events[-1]["rotation_complete"])
            self.assertEqual(self.events[-1]["failed_media"], 1)
            client = UnavailableMediaHTTP(self.config)
            self.assertEqual(kit.run_batch(archive, client, self.report), 1)
            self.assertIn("https://news.example/feed1", client.calls)

    def test_response_policy_refusal_does_not_hold_rotation_forever(self):
        self.config.feeds = []
        self.config.manual_urls = ["https://news.example/page"]
        self.config.allowed_hosts.append("media.example")
        self.config.max_urls = 1
        self.config.media_policies = {"news.example": {"download": True, "types": ["image"],
            "max_file_bytes": 1000, "max_total_bytes": 10000, "min_free_bytes": 0}}
        class MismatchedHTTP(FixtureHTTP):
            def open(self, url):
                response = super().open(url)
                if url.endswith(".jpg"):
                    response = Response(b"must never read", url, "application/pdf")
                    response.chunks = lambda: self.fail("Rejected media body was read")
                return response
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            self.assertEqual(kit.run_batch(archive, MismatchedHTTP(self.config), self.report), 75)
            for _ in range(2):
                self.assertEqual(kit.run_batch(archive, MismatchedHTTP(self.config), self.report), 1)
                self.assertTrue(self.events[-1]["rotation_complete"])
                self.assertEqual(self.events[-1]["pending_media"], 0)
                self.assertEqual(self.events[-1]["blocked_media"], 1)
            self.assertEqual(status(archive.root)["capture_observations"], 1)

    def test_later_media_opt_in_admits_previously_link_only_reference(self):
        self.config.feeds = []
        self.config.manual_urls = ["https://news.example/page"]
        self.config.allowed_hosts.append("media.example")
        self.config.max_urls = 1
        class ImageHTTP(FixtureHTTP):
            def open(self, url):
                response = super().open(url)
                return Response(b"synthetic image", url, "image/jpeg") if url.endswith(".jpg") else response
        with Archive(self.cwd / "archive", self.config).writer() as archive:
            self.assertEqual(kit.run_batch(archive, ImageHTTP(self.config), self.report), 0)
            self.config.media_policies = {"news.example": {"download": True, "types": ["image"],
                "max_file_bytes": 1000, "max_total_bytes": 10000, "min_free_bytes": 0}}
            self.assertEqual(kit.unfinished_media(archive), (1, 0, 0))
            self.assertEqual(kit.run_batch(archive, ImageHTTP(self.config), self.report), 0)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures WHERE media_reference_id IS NOT NULL").fetchone()[0], 1)

    def test_supervisor_signals_owned_group_before_reaping(self):
        import types
        event = types.SimpleNamespace(si_status=0, si_code=os.CLD_EXITED)
        with patch("election26.browser.wait_unreaped"), patch.object(kit.os, "waitid", return_value=event), \
                patch.object(kit.subprocess, "Popen") as popen, patch.object(kit.os, "killpg") as killpg:
            child = popen.return_value
            child.pid = 12345
            sequence = []
            killpg.side_effect = lambda *args: sequence.append("signal")
            child.wait.side_effect = lambda: sequence.append("reap")
            self.assertEqual(kit.supervise(["synthetic"], 1), 0)
            self.assertEqual(sequence, ["signal", "reap"])
            child.poll.assert_not_called()

    def test_supervisor_never_signals_after_external_reap(self):
        with patch.object(kit.os, "waitid", side_effect=ChildProcessError), \
                patch.object(kit.subprocess, "Popen") as popen, patch.object(kit.os, "killpg") as killpg, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kit.supervise(["synthetic"], 1), 75)
            killpg.assert_not_called()
            popen.return_value.wait.assert_not_called()

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
