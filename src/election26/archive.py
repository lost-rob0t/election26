"""Transactional operational catalog, immutable originals, and item-level resume."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

from .config import PolicyError, BusyError, BudgetError
from .media import MediaCatalog, content_kind, url_kind, declared_size


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path):
    """Persist every newly created ancestor before any checkpoint can commit."""
    missing = []
    cursor = Path(path)
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise PolicyError("Archive directory creation encountered an unsafe path")
        sync_dir(directory)
        sync_dir(directory.parent)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def inside_git_checkout(path):
    return any((p / ".git").exists() for p in [path, *path.parents])


class Archive(MediaCatalog):
    def __init__(self, root, config):
        self.root = Path(root).expanduser().resolve()
        self.config = config
        # A public code checkout is never an archive destination.
        code_root = Path(__file__).resolve().parents[2]
        if self.root == code_root or code_root in self.root.parents:
            raise PolicyError("Archive must be outside the source checkout")
        if inside_git_checkout(self.root):
            raise PolicyError("Archive must be outside any Git checkout")
        self.db = None

    def safe_path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise PolicyError("Archive path must be relative without parent traversal")
        path = self.root / relative
        if path != self.root and self.root not in path.parents:
            raise PolicyError("Archive path escapes its root")
        for item in [path, *path.parents]:
            if item == self.root:
                break
            if item.is_symlink():
                raise PolicyError("Archive symlink paths are not supported")
        return path

    @contextlib.contextmanager
    def writer(self):
        if self.db is not None:
            raise PolicyError("Archive writer is already active")
        durable_mkdir(self.root)
        allowed_names = {".writer.lock", ".browser.lock", "catalog.sqlite", "catalog.sqlite-journal", "objects", "tmp", "transcripts", "manifests", "canonical", "backup.json"}
        if any(p.name not in allowed_names or p.is_symlink() for p in self.root.iterdir()):
            raise PolicyError("Archive directory contains unrecognized files or symlinks")
        if any(p.is_symlink() for p in self.root.rglob("*")):
            raise PolicyError("Archive symlink descendants are not supported")
        lock = open(self.root / ".writer.lock", "a+")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError("Another archive writer is active") from exc
            for folder in ("objects", "tmp", "transcripts", "manifests", "canonical"):
                (self.root / folder).mkdir(exist_ok=True, mode=0o700)
            sync_dir(self.root)
            sync_dir(self.root.parent)
            self.db = sqlite3.connect(self.root / "catalog.sqlite", timeout=0)
            self.db.row_factory = sqlite3.Row
            os.chmod(self.root / "catalog.sqlite", 0o600)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            # Check existing format before any catalog schema changes.
            existing_version = None
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
                existing_version = self.db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()
                if existing_version and existing_version[0] not in {"1", "2"}:
                    raise PolicyError("Unsupported archive catalog version")
            migrating = not existing_version or existing_version[0] == "1"
            if migrating:
                # Reserve URL-column duplication, schema growth and the complete
                # rollback journal before any upgrade writes. A partial old
                # migration is repaired using its version, not column presence.
                growth = 64 * 1024
                for table in ("resources", "captures"):
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                        growth += self.db.execute("SELECT COALESCE(SUM(length(CAST(url AS BLOB))),0) FROM " + table).fetchone()[0]
                self.transaction_budget(growth)
            self.db.executescript('''
              BEGIN IMMEDIATE;
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS resources(
                url TEXT PRIMARY KEY, discovered_at TEXT NOT NULL,
                via TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
              CREATE TABLE IF NOT EXISTS captures(
                id INTEGER PRIMARY KEY, url TEXT NOT NULL REFERENCES resources(url),
                final_url TEXT NOT NULL, captured_at TEXT NOT NULL,
                sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
                content_type TEXT NOT NULL, etag TEXT, last_modified TEXT, content_encoding TEXT);
              CREATE TABLE IF NOT EXISTS normalizations(
                capture_id INTEGER PRIMARY KEY REFERENCES captures(id), document_id TEXT NOT NULL,
                document_sha256 TEXT NOT NULL, path TEXT NOT NULL, sdk_commit TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS media_references(
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, source_url TEXT,
                parent_capture_id INTEGER REFERENCES captures(id), discovered_at TEXT NOT NULL,
                declared_type TEXT, declared_bytes INTEGER, kind_hint TEXT);
              CREATE TABLE IF NOT EXISTS media_observations(
                id INTEGER PRIMARY KEY, reference_id INTEGER NOT NULL REFERENCES media_references(id),
                payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS transcripts(
                source_sha256 TEXT PRIMARY KEY, transcript_sha256 TEXT NOT NULL,
                path TEXT NOT NULL, created_at TEXT NOT NULL, engine TEXT NOT NULL,
                human_verified INTEGER NOT NULL DEFAULT 0);
            ''')
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(captures)")}
            if "content_encoding" not in columns:
                self.db.execute("ALTER TABLE captures ADD COLUMN content_encoding TEXT")
            resource_columns = {row[1] for row in self.db.execute("PRAGMA table_info(resources)")}
            if "source_url" not in resource_columns:
                self.db.execute("ALTER TABLE resources ADD COLUMN source_url TEXT")
            if migrating:
                self.db.execute("UPDATE resources SET source_url=url WHERE source_url IS NULL AND via!='html-media'")
            for name, declaration in (("source_url", "TEXT"), ("media_reference_id", "INTEGER"),
                                      ("format_version", "INTEGER NOT NULL DEFAULT 1")):
                if name not in columns:
                    self.db.execute("ALTER TABLE captures ADD COLUMN " + name + " " + declaration)
            if migrating:
                self.db.execute("UPDATE captures SET source_url=url WHERE source_url IS NULL AND format_version=1")
            version = self.db.execute("SELECT value FROM meta WHERE key='format_version'").fetchone()
            if version and version[0] not in {"1", "2"}:
                raise PolicyError("Unsupported archive catalog version")
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('format_version','2')")
                self.db.execute("UPDATE resources SET state='pending' WHERE state='running'")
            # Interrupted downloads are never represented as complete captures.
            # Keep their bytes for inspection; gc reports them without deletion.
            yield self
        finally:
            if self.db:
                self.db.close()
                self.db = None
            lock.close()

    def enqueue(self, urls, via="manual", source_url=None):
        added = 0
        self.transaction_budget(sum(len(str(u).encode("utf-8")) + 1024 for u in urls[:self.config.max_urls]))
        with self.db:
            for raw in urls[:self.config.max_urls]:
                url = self.config.permit(raw)
                added += self.db.execute(
                    "INSERT OR IGNORE INTO resources(url,discovered_at,via,source_url) VALUES (?,?,?,?)",
                    (url, now(), via, source_url or url)).rowcount
        return added

    def disk_bytes(self):
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def budget(self, added, used=None):
        used = self.disk_bytes() if used is None else used
        if used + added > self.config.max_archive_bytes:
            raise PolicyError("Archive size budget exceeded")
        if shutil.disk_usage(self.root).free - added < self.config.min_free_bytes:
            raise PolicyError("Free disk reserve would be exceeded")

    def transaction_budget(self, metadata_bytes=0):
        # SQLite rollback journals can copy every touched page. Reserve the full
        # current catalog plus conservative B-tree/sidecar growth before writes.
        database_bytes = (self.root / "catalog.sqlite").stat().st_size
        self.budget(database_bytes + metadata_bytes * 4 + 256 * 1024)

    def object_path(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PolicyError("Invalid SHA-256")
        return self.safe_path(Path("objects") / digest[:2] / digest)

    def store(self, chunks, policy=None, source_url=None, max_bytes=None):
        used = self.disk_bytes()
        # Reserve space for a catalog transaction and provenance sidecar.
        self.budget(64 * 1024, used)
        fd, name = tempfile.mkstemp(suffix=".part", dir=self.root / "tmp")
        temp = Path(name)
        digest = hashlib.sha256()
        size = 0
        media_used = self.media_used(source_url) if policy else 0
        limit = min(self.config.max_file_bytes, max_bytes or self.config.max_file_bytes,
                    policy["max_file_bytes"] if policy else self.config.max_file_bytes)
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise PolicyError("Per-file size limit exceeded")
                    if policy and media_used + size > policy["max_total_bytes"]:
                        raise PolicyError("Source media disk budget exceeded")
                    if policy and shutil.disk_usage(self.root).free - size - 64 * 1024 < policy["min_free_bytes"]:
                        raise PolicyError("Source media free disk reserve would be exceeded")
                    self.budget(size + 64 * 1024, used)
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            checksum = digest.hexdigest()
            final = self.object_path(checksum)
            final.parent.mkdir(exist_ok=True, mode=0o700)
            sync_dir(final.parent.parent)  # Persist a newly created shard first.
            if final.exists():
                if sha256_file(final) != checksum:
                    raise PolicyError("Existing archive object is corrupt")
            else:
                os.replace(temp, final)
                sync_dir(final.parent)
            return checksum, size
        finally:
            temp.unlink(missing_ok=True)

    def capture(self, url, response, source_url=None, reference=None, max_bytes=None, browser_request_id=None):
        url = self.config.permit(url)
        final_url = self.config.permit(response.url)
        source_url = source_url or (reference["source_url"] if reference else url)
        if source_url:
            source_url = self.config.permit(source_url)
        reference = reference or self.known_media_reference(url, source_url, final_url)
        for name in ("content-type", "etag", "last-modified", "content-encoding", "content-length"):
            if len(response.headers.get(name, "").encode("utf-8")) > 1024:
                raise PolicyError("Source metadata exceeds the per-field limit")
        if browser_request_id is not None:
            csp = response.headers.get('content-security-policy', '')
            if len(csp.encode()) > 8192 or any(ord(c) < 32 and c != '\t' for c in csp):
                raise PolicyError('Source content security policy exceeds browser metadata limits')
        policy = None
        if reference or content_kind(response.headers.get("content-type")) or url_kind(url) or url_kind(final_url):
            reference = reference or self.media_reference(url, source_url, kind_hint=url_kind(url))
            policy, reason = self.reference_admission(reference, response)
            if not policy:
                event = self.media_event(reference, "not_requested", reason, response=response)
                with self.db:
                    self.db.execute("UPDATE resources SET state='not_requested',error=NULL WHERE url=?", (url,))
                return event
            self.media_event(reference, "pending", response=response)
        checksum, size = self.store(response.chunks(), policy=policy, source_url=source_url, max_bytes=max_bytes)
        record = {
            "format_version": 2, "source_url": source_url,
            "media_reference_id": reference["id"] if reference else None,
            "url": url, "final_url": final_url, "captured_at": now(),
            "sha256": checksum, "bytes": size,
            "content_type": response.headers.get("content-type", "application/octet-stream"),
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "content_encoding": response.headers.get("content-encoding"),
        }
        self.transaction_budget(len(json.dumps(record, ensure_ascii=False).encode("utf-8")))
        # One SQLite commit publishes both observation and resume checkpoint.
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO captures(url,final_url,captured_at,sha256,bytes,content_type,etag,last_modified,content_encoding,source_url,media_reference_id,format_version) "
                "VALUES (:url,:final_url,:captured_at,:sha256,:bytes,:content_type,:etag,:last_modified,:content_encoding,:source_url,:media_reference_id,:format_version)", record)
            self.db.execute("UPDATE resources SET state='done',error=NULL WHERE url=?", (url,))
            record["id"] = cursor.lastrowid
            if browser_request_id is not None:
                from .browser_catalog import original
                original(self, browser_request_id, record, response)
            event = self.media_event(reference, "downloaded", capture=record, response=response, transactional=True) if reference else None
        try:
            atomic_json(self.root / "manifests" / (str(record["id"]) + ".json"), record)
        except OSError:
            # Capture/checkpoint are committed. Repair can reconstruct this sidecar.
            record["manifest_pending"] = True
        if event:
            try:
                atomic_json(self.root / "manifests" / ("media-" + str(event["id"]) + ".json"), event)
            except OSError:
                record["manifest_pending"] = True
        if "html" in record["content_type"].lower() and record["content_encoding"] in (None, "identity") and record["bytes"] <= 4 * 1024 * 1024:
            try:
                from .discovery import media_references
                self.record_media_links(record, media_references(self.object_path(checksum).read_text(errors="replace"), final_url))
            except Exception as exc:
                record["postprocess_error"] = type(exc).__name__
        return record

    def fetch(self, client, urls=None, refresh=False, extract_media=False):
        if urls:
            self.enqueue(urls)
        results = []
        attempted_media = set()
        media_first = False
        requests_before = getattr(client, "request_count", None)
        def retain_unstarted_turn():
            if requests_before is not None and getattr(client, "request_count", None) == requests_before:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('next_fetch_phase',?)",
                                    ("media" if media_first else "ordinary",))
        if not urls:
            row = self.db.execute("SELECT value FROM meta WHERE key='next_fetch_phase'").fetchone()
            media_first = bool(row and row[0] == "media")
            self.transaction_budget(1024)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('next_fetch_phase',?)",
                                ("ordinary" if media_first else "media",))
            if media_first:
                results = self.fetch_media(client, self.config.max_urls)
                if any(item["state"] == "deferred" for item in results):
                    retain_unstarted_turn()
                    return results
        remaining = self.config.max_urls - len(results)
        if remaining <= 0:
            return results
        query = "SELECT url,source_url,via FROM resources"
        conditions = [] if urls else ["via NOT IN ('policy-media','browser')"]
        params = []
        if not refresh:
            conditions.append("state IN ('pending','failed','not_requested')" if urls else "state IN ('pending','failed')")
        if urls:
            selected = list(dict.fromkeys(self.config.permit(u) for u in urls[:self.config.max_urls]))
            conditions.append("url IN (" + ",".join("?" for _ in selected) + ")")
            params.extend(selected)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY CASE WHEN state='pending' THEN 0 ELSE 1 END,attempts,discovered_at,url LIMIT ?"
        params.append(remaining)
        pending = list(self.db.execute(query, params))
        for item in pending:
            url = item["url"]
            reference = self.known_media_reference(url, item["source_url"],
                                                   force=item["via"] in {"html-media", "policy-media"})
            if reference:
                attempted_media.add(reference["id"])
                policy, reason = self.reference_admission(reference)
                if not policy:
                    self.media_event(reference, "not_requested", reason)
                    with self.db:
                        self.db.execute("UPDATE resources SET state='not_requested' WHERE url=?", (url,))
                    results.append({"state": "not_requested", "media_reference_id": reference["id"]})
                    continue
                self.media_event(reference, "pending")
            self.transaction_budget(len(url.encode("utf-8")) + 1024)
            with self.db:
                self.db.execute("UPDATE resources SET state='running',attempts=attempts+1 WHERE url=?", (url,))
            try:
                with client.open(url) as response:
                    record = self.capture(url, response, source_url=item["source_url"], reference=reference)
                if not record.get("sha256"):
                    results.append({"state": "not_requested", "media_reference_id": record["reference_id"]})
                    continue
                results.append({"sha256": record["sha256"], "state": "done"})
                if record.get("postprocess_error"):
                    results[-1]["postprocess_error"] = record["postprocess_error"]
                if record.get("media_reference_id"):
                    attempted_media.add(record["media_reference_id"])
            except BudgetError:
                with self.db:
                    self.db.execute("UPDATE resources SET state='pending' WHERE url=?", (url,))
                results.append({"state": "deferred", "reason": "run_budget_exhausted"})
                break
            except Exception as exc:
                # Post-capture extraction must never downgrade a committed original.
                if results and results[-1].get("state") == "done" and self.db.execute(
                        "SELECT state FROM resources WHERE url=?", (url,)).fetchone()[0] == "done":
                    results[-1]["postprocess_error"] = type(exc).__name__
                    continue
                # Exception messages may contain URLs, headers, or provider secrets.
                with self.db:
                    self.db.execute("UPDATE resources SET state='failed',error=? WHERE url=?", (type(exc).__name__, url))
                if reference:
                    self.media_event(reference, "unavailable", type(exc).__name__)
                else:
                    direct = self.db.execute("SELECT * FROM media_references WHERE url=? AND parent_capture_id IS NULL",
                                             (url,)).fetchone()
                    if direct:
                        self.media_event(direct, "unavailable", type(exc).__name__)
                results.append({"state": "failed", "error": type(exc).__name__})
        if not urls and not media_first and not any(r["state"] == "deferred" for r in results):
            results.extend(self.fetch_media(client, max(0, self.config.max_urls - len(results)), exclude=attempted_media))
        if not urls and any(r["state"] == "deferred" for r in results):
            retain_unstarted_turn()
        return results

    def repair_manifests(self):
        from .browser_catalog import repair
        repair(self)
        for row in self.db.execute("SELECT * FROM captures"):
            path = self.root / "manifests" / (str(row["id"]) + ".json")
            if not path.exists():
                self.budget(len(json.dumps(dict(row)).encode("utf-8")) + 4096)
                atomic_json(path, dict(row))

        for row in self.db.execute("SELECT id,payload FROM media_observations"):
            path = self.root / "manifests" / ("media-" + str(row["id"]) + ".json")
            if not path.exists():
                self.budget(len(row["payload"].encode()) + 4096)
                atomic_json(path, dict(json.loads(row["payload"]), id=row["id"]))

    def backup(self, path):
        path = Path(path).expanduser().resolve()
        Archive(path, self.config)  # Apply the same private/outside-Git destination policy.
        if path.exists():
            raise PolicyError("Backup destination already exists")
        if path == self.root or self.root in path.parents:
            raise PolicyError("Backup must be outside the archive")
        path.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(path.parent).free < self.disk_bytes() + self.config.min_free_bytes:
            raise PolicyError("Backup destination has insufficient free space")
        path.mkdir(mode=0o700)
        # Caller holds the writer lock, so blobs and catalog share a stable view.
        with contextlib.closing(sqlite3.connect(path / "catalog.sqlite")) as target, target:
            self.db.backup(target)
        for name in ("objects", "manifests", "transcripts", "canonical"):
            shutil.copytree(self.root / name, path / name)
        atomic_json(path / "backup.json", {"created_at": now(), "format_version": 2})
        return {"backup_created": True}


def status(root):
    root = Path(root).expanduser().resolve()
    path = root / "catalog.sqlite"
    if not path.exists():
        return {"initialized": False, "capture_observations": 0, "unique_objects": 0,
                "original_bytes": 0, "stored_object_bytes": 0, "canonical_documents": 0,
                "transcripts": 0, "errors": 0, "queue": {}}
    if path.is_symlink():
        raise PolicyError("Archive catalog symlinks are not supported")
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db, db:
        db.execute("BEGIN")  # All counters come from the same committed snapshot.
        rows = db.execute("SELECT sha256, MAX(bytes) FROM captures GROUP BY sha256").fetchall()
        browser = db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_artifacts'").fetchone()
        all_objects = dict(rows)
        if browser:
            for digest, size in db.execute("SELECT sha256,MAX(bytes) FROM browser_artifacts GROUP BY sha256"):
                all_objects[digest] = max(all_objects.get(digest, 0), size)
        return {
            "browser_sessions": db.execute("SELECT COUNT(*) FROM browser_sessions").fetchone()[0] if browser else 0,
            "browser_artifacts": db.execute("SELECT COUNT(*) FROM browser_artifacts").fetchone()[0] if browser else 0,
            "initialized": True,
            "capture_observations": db.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
            "unique_objects": len(all_objects),
            "original_bytes": db.execute("SELECT COALESCE(SUM(bytes),0) FROM captures").fetchone()[0],
            "stored_object_bytes": sum(all_objects.values()),
            "canonical_documents": db.execute("SELECT COUNT(*) FROM normalizations").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='normalizations'").fetchone() else 0,
            "transcripts": db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0],
            "errors": db.execute("SELECT COUNT(*) FROM resources WHERE state='failed'").fetchone()[0],
            "queue": dict(db.execute("SELECT state,COUNT(*) FROM resources GROUP BY state")),
        }


def gc_plan(archive):
    """Report only. No raw data, transcript, or provenance is deleted or moved."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=archive.config.retention_days)).isoformat()
    candidates = []
    for row in archive.db.execute('''
        SELECT c.sha256,MAX(c.captured_at) AS newest,t.path,t.transcript_sha256
        FROM captures c JOIN transcripts t ON t.source_sha256=c.sha256
        WHERE t.human_verified=1 GROUP BY c.sha256 HAVING MAX(c.captured_at) < ?
    ''', (cutoff,)):
        transcript = archive.safe_path(Path("transcripts") / (row["sha256"] + ".json"))
        source = archive.object_path(row["sha256"])
        if (transcript.is_file() and source.is_file()
                and sha256_file(transcript) == row["transcript_sha256"]
                and sha256_file(source) == row["sha256"]):
            candidates.append(row["sha256"])
    known = {r[0] for r in archive.db.execute("SELECT DISTINCT sha256 FROM captures")}
    from .browser_catalog import exists
    if exists(archive.db):
        known.update(r[0] for r in archive.db.execute("SELECT DISTINCT sha256 FROM browser_artifacts"))
    orphans = [p.name for p in (archive.root / "objects").glob("*/*") if p.is_file() and p.name not in known]
    return {"dry_run": True, "deletion_supported": False,
            "raw_candidates_after_human_review": candidates, "orphan_objects": orphans,
            "incomplete_downloads": len(list((archive.root / "tmp").glob("*.part"))),
            "preserved": ["all originals", "all transcripts", "all provenance", "all hashes"]}
