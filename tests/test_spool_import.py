"""Locally generated receipts/bodies only; never load a user's spool or network."""
import contextlib
import fcntl
import hashlib
import io
import itertools
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive, gc_plan, status
from election26.cli import main
from election26.config import Config, PolicyError, BusyError, BudgetError
from election26.spool import FORMAT, import_spool, initialize, normalization_context

IDENTITY = {"format": FORMAT, "spool_id": "a" * 32, "local": True}
OBSERVED = "2026-06-01T12:00:00.123456Z"
COMPLETED = "2026-06-01T12:00:01.654321Z"


def fixture(root, sequence=1, data=b' [ {"created_at":"2020-01-02T03:04:05Z", "unknown":false} ]\n', *, local=True, **changes):
    root.mkdir(exist_ok=True)
    (root / "captures").mkdir(exist_ok=True)
    (root / ".writer.lock").touch(exist_ok=True)
    (root / "spool.json").write_text(json.dumps({**IDENTITY, "local": local}) + "\n")
    stem = str(sequence).zfill(20)
    receipt = {**IDENTITY, "capture_id": IDENTITY["spool_id"] + ":" + str(sequence),
               "sequence": sequence, "observed_at": OBSERVED, "completed_at": COMPLETED,
               "source": "https://fixture.example",
               "url": "https://fixture.example/api/v1/timelines/public?" + ("local=true&" if local else "") + "limit=40",
               "status": 200, "headers": {"content-type": "application/json"},
               "body": "captures/" + stem + ".body", "sha256": hashlib.sha256(data).hexdigest(),
               "bytes": len(data), "complete": True, "error": "", "cursor_status": "cursor_read",
               "cursor_before": "", "cursor_after": "5", "next_poll_at": 1780315261.654321,
               "failures": 0}
    receipt.pop("local")
    receipt.update(changes)
    (root / "captures" / (stem + ".body")).write_bytes(data)
    (root / "captures" / (stem + ".json")).write_text(json.dumps(receipt) + "\n")
    return receipt


class SpoolImportTests(unittest.TestCase):
    def setUp(self):
        for target in ("election26.archive.inside_git_checkout", "election26.spool.inside_git_checkout"):
            guard = patch(target, return_value=False)
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source, self.root = self.base / "source", self.base / "archive"
        self.config = Config(allowed_hosts=["fixture.example"], min_free_bytes=0)
        self.archive = Archive(self.root, self.config)

    def test_exact_bytes_distinct_times_and_no_source_writes(self):
        receipt = fixture(self.source)
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        with self.archive.writer() as archive, patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
            result = import_spool(archive, self.source)
            capture = dict(archive.db.execute("SELECT * FROM captures").fetchone())
            imported = dict(archive.db.execute("SELECT * FROM spool_imports").fetchone())
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(archive.object_path(capture["sha256"]).read_bytes(), before[receipt["body"]])
            self.assertEqual(archive.object_path(imported["receipt_sha256"]).read_bytes(), before[receipt["body"].replace(".body", ".json")])
            self.assertEqual(capture["captured_at"], COMPLETED)
            self.assertEqual(imported["observed_at"], OBSERVED)
            self.assertNotIn(imported["imported_at"], (OBSERVED, COMPLETED))
            self.assertEqual(gc_plan(archive)["orphan_objects"], [])
            self.assertTrue((self.root / "manifests/1.json").is_file())
            manifest = json.loads((self.root / "manifests/import-1.json").read_text())
            self.assertEqual(manifest["receipt"]["capture_id"], receipt["capture_id"])
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})
        self.assertEqual(status(self.root)["imported_observations"], 1)

    def test_repeated_identical_bodies_are_distinct_observations_and_resume(self):
        fixture(self.source, 2)
        fixture(self.source, 1)
        with self.archive.writer() as archive:
            self.assertEqual(import_spool(archive, self.source, 1)["imported"], 1)
        with self.archive.writer() as archive:
            self.assertEqual(import_spool(archive, self.source, 1)["imported"], 1)
            again = import_spool(archive, self.source)
            self.assertEqual((again["imported"], again["already_imported"]), (0, 2))
            self.assertEqual([r[0] for r in archive.db.execute("SELECT sequence FROM spool_imports ORDER BY capture_id")], [1, 2])
        self.assertEqual(status(self.root)["capture_observations"], 2)
        self.assertEqual(status(self.root)["unique_objects"], 1)

    def test_new_receipt_progress_does_not_require_rereading_old_receipts(self):
        from election26.spool import read_metadata
        fixture(self.source, 1)
        fixture(self.source, 2)
        with self.archive.writer() as archive:
            import_spool(archive, self.source)
        fixture(self.source, 3)
        reads = []
        def read(directory, name):
            reads.append(name)
            if name.endswith(".json") and name != "spool.json":
                self.assertEqual(name, "00000000000000000003.json")
            return read_metadata(directory, name)
        with self.archive.writer() as archive, patch("election26.spool.read_metadata", side_effect=read):
            self.assertEqual(import_spool(archive, self.source, 1)["imported"], 1)
        self.assertEqual(reads, ["spool.json", "00000000000000000003.json"])

    def test_global_timeline_omits_false_local_parameter_as_actual_producer(self):
        fixture(self.source, local=False)
        with self.archive.writer() as archive:
            self.assertEqual(import_spool(archive, self.source)["imported"], 1)

    def test_partial_and_error_responses_are_retained_honestly(self):
        fixture(self.source, 1, b"prefix", complete=False, status=200, error="truncated_response")
        fixture(self.source, 2, b"denied", status=403, headers={"content-type": "text/plain"})
        fixture(self.source, 3, b"", complete=False, status=0, error="OSError")
        with self.archive.writer() as archive:
            self.assertEqual(import_spool(archive, self.source)["imported"], 3)
            rows = list(archive.db.execute("SELECT * FROM captures ORDER BY id"))
            for row in (rows[0], rows[2]):
                with self.assertRaises(PolicyError):
                    normalization_context(archive, row)
            self.assertEqual(normalization_context(archive, rows[1])["status"], 403)
        self.assertEqual(status(self.root)["incomplete_imported_observations"], 2)
        manifest = json.loads((self.root / "manifests/1.json").read_text())
        self.assertIs(manifest["local_import"]["complete"], False)

    def test_committed_crash_resumes_without_duplicate_and_repairs_sidecar(self):
        fixture(self.source)
        with self.archive.writer() as archive:
            with patch("election26.spool.atomic_json", side_effect=OSError("fixture sidecar failure")):
                import_spool(archive, self.source)
        with self.archive.writer() as archive:
            self.assertEqual(import_spool(archive, self.source)["already_imported"], 1)
            archive.repair_manifests()
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)
        self.assertTrue((self.root / "manifests/import-1.json").is_file())

    def test_transaction_failure_never_publishes_capture_or_import_checkpoint(self):
        fixture(self.source)
        with self.archive.writer() as archive:
            initialize(archive)
            archive.db.execute("CREATE TRIGGER stop_import BEFORE INSERT ON spool_imports BEGIN SELECT RAISE(ABORT,'fixture'); END")
            archive.db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                import_spool(archive, self.source)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM spool_imports").fetchone()[0], 0)
            self.assertTrue(gc_plan(archive)["orphan_objects"])
            archive.db.execute("DROP TRIGGER stop_import")
            archive.db.commit()
            self.assertEqual(import_spool(archive, self.source)["imported"], 1)

    def test_hash_size_and_byte_budget_fail_before_checkpoint(self):
        for changes in ({"sha256": "0" * 64}, {"bytes": 999}, {"bytes": self.config.max_file_bytes + 1}):
            fixture(self.source, **changes)
            with self.archive.writer() as archive, self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_changed_receipt_and_identity_are_not_overwritten(self):
        fixture(self.source)
        with self.archive.writer() as archive:
            import_spool(archive, self.source)
            fixture(self.source, observed_at="2026-06-01T12:00:02.000000Z")
            with self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            fixture(self.source, local=False)
            with self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)

    def test_unsafe_receipts_fail_closed(self):
        cases = [{"body": "../../outside"}, {"sequence": True}, {"complete": "false"},
                 {"status": 99}, {"headers": {"set-cookie": "fixture"}},
                 {"url": "https://fixture.example/private?token=fixture"},
                 {"observed_at": "not-a-time"}, {"next_poll_at": float("nan")},
                 {"extra": "unapproved-field"}, {"capture_id": "other:1"}]
        for changes in cases:
            with self.subTest(changes=changes):
                fixture(self.source, **changes)
                with self.archive.writer() as archive, self.assertRaises(PolicyError):
                    import_spool(archive, self.source)
                self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_symlink_body_or_directory_is_never_read(self):
        receipt = fixture(self.source)
        outside = self.base / "outside"; outside.write_bytes(b"outside must stay outside")
        body = self.source / receipt["body"]
        body.unlink(); body.symlink_to(outside)
        with self.archive.writer() as archive, self.assertRaises((PolicyError, OSError)):
            import_spool(archive, self.source)
        self.assertEqual(outside.read_bytes(), b"outside must stay outside")
        self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_producer_lock_is_respected_without_writing_it(self):
        fixture(self.source)
        with (self.source / ".writer.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.archive.writer() as archive, self.assertRaises(BusyError):
                import_spool(archive, self.source)

    def test_orphans_do_not_create_observations(self):
        fixture(self.source)
        (self.source / "captures/00000000000000000002.body").write_bytes(b"orphan")
        (self.source / "captures/00000000000000000003.json.part").write_bytes(b"partial")
        with self.archive.writer() as archive:
            result = import_spool(archive, self.source)
            self.assertEqual((result["imported"], result["orphan_files"]), (1, 2))

    def test_backup_restore_preserves_import_receipts(self):
        fixture(self.source)
        backup = self.base / "backup"
        with self.archive.writer() as archive:
            import_spool(archive, self.source)
            archive.backup(backup)
        self.assertEqual(status(self.root), status(backup))
        with Archive(backup, self.config).writer() as archive:
            self.assertEqual(import_spool(archive, self.source)["already_imported"], 1)
            self.assertEqual(gc_plan(archive)["orphan_objects"], [])

    def test_cli_never_constructs_network_client(self):
        fixture(self.source)
        config = self.base / "config.json"; config.write_text(json.dumps({"allowed_hosts": ["fixture.example"], "min_free_bytes": 0}))
        with patch("election26.cli.PublicHTTP", side_effect=AssertionError("network adapter")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(config), "--archive", str(self.root), "import-spool", str(self.source)]), 0)

    def test_nested_destination_rejected_before_creating_source_files(self):
        fixture(self.source)
        nested = self.source / "must-not-create"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--archive", str(nested), "import-spool", str(self.source)]), 2)
        self.assertFalse(nested.exists())

    def test_bounded_snapshot_and_disk_fail_without_import_progress(self):
        fixture(self.source)
        with self.archive.writer() as archive:
            with patch("election26.spool.DIRECTORY_LIMIT", 1), self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            with patch.object(archive, "transaction_budget", side_effect=PolicyError("disk cap")), self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)

    def test_private_spool_inside_git_is_rejected(self):
        fixture(self.source)
        with self.archive.writer() as archive, patch("election26.spool.inside_git_checkout", return_value=True), self.assertRaises(PolicyError):
            import_spool(archive, self.source)

    def test_duplicate_keys_and_oversized_metadata_rejected(self):
        fixture(self.source)
        receipt = self.source / "captures/00000000000000000001.json"
        payload = receipt.read_text()
        for value in ('{"format":"duplicate",' + payload[1:], " " * (64 * 1024 + 1)):
            receipt.write_text(value)
            with self.archive.writer() as archive, self.assertRaises(PolicyError):
                import_spool(archive, self.source)
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_scan_deadline_is_enforced_without_progress(self):
        fixture(self.source)
        self.config.max_run_seconds = 1
        with self.archive.writer() as archive, patch("election26.spool.time.monotonic", side_effect=itertools.chain([0], itertools.repeat(2))), self.assertRaises(BudgetError):
            import_spool(archive, self.source)
        self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_receipt_corruption_blocks_later_normalization(self):
        fixture(self.source)
        with self.archive.writer() as archive:
            import_spool(archive, self.source)
            capture = archive.db.execute("SELECT * FROM captures").fetchone()
            context = normalization_context(archive, capture)
            self.assertEqual(context["observed_at"], OBSERVED)
            row = archive.db.execute("SELECT receipt_sha256 FROM spool_imports").fetchone()
            archive.object_path(row[0]).write_bytes(b"corrupt")
            with self.assertRaises(PolicyError):
                normalization_context(archive, capture)
