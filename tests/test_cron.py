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
