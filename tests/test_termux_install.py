import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("termux_setup", ROOT / "scripts/termux_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class TermuxInstallerTests(unittest.TestCase):
    def test_dry_run_is_non_mutating_and_optional_tools_are_explicit(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/install-termux.sh"), "--with-yt-dlp"], text=True, capture_output=True, check=True)
        self.assertIn("Preview only", result.stdout)
        self.assertIn("python-yt-dlp", result.stdout)
        self.assertNotIn("Official Termux packages: python python-pip python-yt-dlp cronie", result.stdout)

    def test_existing_config_is_preserved_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = setup.prepare(tmp, ROOT, create_venv=False)
            config = Path(tmp) / ".config/election26/config.json"
            config.write_text('{"operator":"keep me"}\n')
            second = setup.prepare(tmp, ROOT, create_venv=False)
            self.assertTrue(first["config_created"])
            self.assertFalse(second["config_created"])
            self.assertEqual(json.loads(config.read_text()), {"operator": "keep me"})
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_symlink_and_unmanaged_venv_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".config").symlink_to(home / "other", target_is_directory=True)
            self.assertRaises(ValueError, setup.prepare, home, ROOT, False)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".local/share/election26/venv").mkdir(parents=True)
            self.assertRaises(ValueError, setup.prepare, tmp, ROOT, False)

    def test_interrupted_managed_venv_retries_without_replacing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(setup.subprocess, "run", side_effect=RuntimeError("interrupted")):
                self.assertRaises(RuntimeError, setup.prepare, tmp, ROOT)
            marker = Path(tmp) / ".local/share/election26/venv/.election26-managed"
            self.assertEqual(marker.read_text(), setup.MARKER)
            with mock.patch.object(setup.subprocess, "run") as run:
                result = setup.prepare(tmp, ROOT)
                run.assert_called_once()
                self.assertFalse(result["config_created"])

    def test_cron_preview_does_not_enable_media_downloads(self):
        result = subprocess.run(["python", str(ROOT / "scripts/cron.py"),
            "--executable", "/private/bin/election26", "--config", "/private/config.json",
            "--archive", "/private/archive", "--log", "/private/run.log"],
            text=True, capture_output=True, check=True)
        self.assertNotIn("--extract-media", result.stdout)
