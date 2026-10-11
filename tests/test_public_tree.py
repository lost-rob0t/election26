import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_tree_guard", ROOT / "scripts/check-public-tree.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicTreeTests(unittest.TestCase):
    def test_location_and_raw_paths_are_rejected_without_real_files(self):
        for name in ("walk.KML", "track.kMz", "input.geojson", "data/copy.json", "WIGLE/export.txt",
                     "private/foo", "routes/preview.svg", "renamed.csv", "copy.kml.txt", "photo.png"):
            self.assertIsNotNone(guard.rejection(name), name)

    def test_structured_location_derivatives_are_rejected(self):
        for data in (b'{"features":[{"geometry":{"coordinates":[0,0]}}]}',
                     b'{"sample":{"LAT":0,"LON":0}}', b'{"BSSID":"synthetic"}'):
            self.assertIsNotNone(guard.rejection("renamed.json", data))
        self.assertIsNone(guard.rejection("config/profile.json", b'{"country":"US","allowed_hosts":[]}'))
        self.assertIsNone(guard.rejection("src/parser.py", b'latitude = "code"'))

    def test_staged_guard_reads_index_not_worktree(self):
        with mock.patch.object(guard.subprocess, "check_output", side_effect=[b"sample.json\0", b"21", b'{"coordinates":[0,0]}']) as run:
            self.assertEqual(guard.main(["--staged"]), 1)
        self.assertEqual(run.call_args_list[2].args[0], ["git", "show", ":sample.json"])

    def test_oversized_json_is_not_loaded(self):
        with mock.patch.object(guard.subprocess, "check_output", side_effect=[b"large.json\0", b"99999999"]) as run:
            self.assertEqual(guard.main(["--staged"]), 1)
        self.assertEqual(run.call_count, 2)
