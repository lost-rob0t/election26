import contextlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from election26.archive import Archive, status
from election26.config import Config

CONNECT = sqlite3.connect


class SQLiteLifetimeTests(unittest.TestCase):
    def setUp(self):
        guard = patch("election26.archive.inside_git_checkout", return_value=False)
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "archive"
        self.archive = Archive(self.root, Config(min_free_bytes=0))
        with self.archive.writer():
            pass

    @contextlib.contextmanager
    def connections(self):
        opened = []
        def connect(*args, **kwargs):
            db = CONNECT(*args, **kwargs)
            opened.append(db)
            return db
        with patch("election26.archive.sqlite3.connect", side_effect=connect):
            yield opened
        self.assertTrue(opened)
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")

    def test_status_closes_successful_read_snapshot(self):
        with self.connections():
            self.assertEqual(status(self.root)["capture_observations"], 0)

    def test_status_closes_after_query_failure(self):
        with self.archive.writer() as archive, archive.db:
            archive.db.execute("DROP TABLE captures")
        with self.connections():
            with self.assertRaises(sqlite3.OperationalError):
                status(self.root)

    def test_backup_closes_destination_and_remains_readable(self):
        destination = Path(self.tmp.name) / "backup"
        with self.archive.writer() as archive:
            with self.connections():
                archive.backup(destination)
        self.assertEqual(status(destination)["capture_observations"], 0)

    def test_backup_closes_destination_after_copy_failure(self):
        class FailedSource:
            def backup(self, target):
                raise RuntimeError("synthetic backup interruption")
        with self.archive.writer() as archive:
            source = archive.db
            try:
                archive.db = FailedSource()
                with self.connections():
                    with self.assertRaises(RuntimeError):
                        archive.backup(Path(self.tmp.name) / "partial")
            finally:
                archive.db = source
