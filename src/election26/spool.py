"""Offline import of the existing fediwatch.raw.v1 receipt/body contract.

The receipt is evidence of a producer commit, not a signature of authenticity.
Nothing in this module resolves a host, requests a URL, or modifies the spool.
"""
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from .archive import atomic_json, inside_git_checkout, now, sha256_file
from .config import PolicyError, BusyError, BudgetError

FORMAT = "fediwatch.raw.v1"
CONTRACT_COMMIT = "ba0cf3b896620c50d0a92daacaa57af6cf0558dd"
METADATA_LIMIT = 64 * 1024
DIRECTORY_LIMIT = 100000
HEADERS = {"content-type", "content-length", "content-encoding", "etag",
           "last-modified", "transfer-encoding", "retry-after", "x-ratelimit-limit",
           "x-ratelimit-remaining", "x-ratelimit-reset"}
FIELDS = {"format", "spool_id", "capture_id", "sequence", "observed_at", "completed_at",
          "source", "url", "status", "headers", "body", "sha256", "bytes", "complete",
          "error", "cursor_status", "cursor_before", "cursor_after", "next_poll_at", "failures"}


def strict_json(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PolicyError("Duplicate receipt JSON keys are unsupported")
            result[key] = value
        return result
    def nonfinite(_):
        raise PolicyError("Non-finite receipt JSON numbers are unsupported")
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PolicyError("Invalid spool JSON metadata") from exc


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", value):
        raise PolicyError("Unsupported producer timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError("Invalid producer timestamp") from exc


def separate_paths(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise PolicyError("Import source and archive must be separate directories")


@contextlib.contextmanager
def regular(directory, name):
    # O_NONBLOCK avoids blocking on a malicious FIFO before fstat rejects it.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PolicyError("Spool entries must be regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


def read_metadata(directory, name):
    with regular(directory, name) as handle:
        if os.fstat(handle.fileno()).st_size > METADATA_LIMIT:
            raise PolicyError("Spool metadata exceeds the size limit")
        payload = handle.read(METADATA_LIMIT + 1)
        if len(payload) > METADATA_LIMIT:
            raise PolicyError("Spool metadata exceeds the size limit")
        return payload, strict_json(payload)


@contextlib.contextmanager
def snapshot(path):
    root = Path(path).expanduser().absolute()
    if any(item.is_symlink() for item in (root, *root.parents)):
        raise PolicyError("Spool paths must not contain symlinks")
    if inside_git_checkout(root):
        raise PolicyError("Private spools must remain outside Git checkouts")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name not in {".writer.lock", "spool.json", "spool.json.part", "captures"} or entry.is_symlink():
                    raise PolicyError("Unrecognized or unsafe spool root entry")
        with regular(descriptor, ".writer.lock") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError("FediWatch writer is active; retry after its bounded run") from exc
            payload, identity = read_metadata(descriptor, "spool.json")
            if (not isinstance(identity, dict) or set(identity) != {"format", "spool_id", "local"}
                    or identity["format"] != FORMAT or type(identity["local"]) is not bool
                    or not isinstance(identity["spool_id"], str)
                    or re.fullmatch(r"[0-9a-f]{32}", identity["spool_id"]) is None):
                raise PolicyError("Invalid FediWatch spool identity")
            captures = os.open("captures", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                yield root, captures, payload, identity
            finally:
                os.close(captures)
    finally:
        os.close(descriptor)


def validate(receipt, identity, stem, config):
    if not isinstance(receipt, dict) or set(receipt) != FIELDS:
        raise PolicyError("Unsupported FediWatch receipt fields")
    number = int(stem)
    if (receipt["format"] != FORMAT or receipt["spool_id"] != identity["spool_id"]
            or not integer(number, 1, 2**63 - 1) or type(receipt["sequence"]) is not int
            or receipt["sequence"] != number
            or receipt["capture_id"] != identity["spool_id"] + ":" + str(number)
            or receipt["body"] != "captures/" + stem + ".body"):
        raise PolicyError("Spool receipt identity or body path mismatch")
    # Both original timestamps are preserved verbatim. A backwards wall-clock
    # adjustment is possible; do not invent a monotonic relationship between them.
    timestamp(receipt["observed_at"])
    timestamp(receipt["completed_at"])
    source, url = receipt["source"], receipt["url"]
    if not isinstance(source, str) or not isinstance(url, str):
        raise PolicyError("Invalid spool source URL")
    normalized = config.permit(url)
    parts = urlsplit(url)
    if (normalized != url or parts.scheme != "https" or parts.netloc != parts.hostname
            or source != "https://" + parts.netloc or parts.path != "/api/v1/timelines/public"
            or parts.fragment or len(parts.hostname) > 253 or "." not in parts.hostname
            or parts.hostname.startswith(".") or parts.hostname.endswith(".") or ".." in parts.hostname
            or parts.hostname.endswith((".localhost", ".local", ".internal"))):
        raise PolicyError("Spool URL is not an explicit public timeline route")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise PolicyError("Spool source must use the producer's DNS-hostname contract")
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = dict(query)
    if (len(values) != len(query) or set(values) - {"local", "limit", "min_id"}
            or values.get("local") != ("true" if identity["local"] else None)
            or not values.get("limit", "").isascii() or not values["limit"].isdigit()
            or not 1 <= int(values["limit"]) <= 40
            or ("min_id" in values and not re.fullmatch(r"(?:0|[1-9][0-9]{0,127})", values["min_id"]))):
        raise PolicyError("Unsupported public timeline query")
    if (not integer(receipt["status"], 0, 599) or (0 < receipt["status"] < 100)
            or type(receipt["complete"]) is not bool
            or (receipt["complete"] and receipt["status"] == 0)
            or not integer(receipt["bytes"], 0, min(config.max_file_bytes, 64 * 1024 * 1024))
            or not isinstance(receipt["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]) is None):
        raise PolicyError("Invalid spool response metadata")
    headers = receipt["headers"]
    if (not isinstance(headers, dict) or set(headers) - HEADERS
            or any(not isinstance(value, str) or len(value.encode()) > 1024
                   or any(ord(char) < 32 and char != "\t" for char in value) for value in headers.values())):
        raise PolicyError("Unsupported spool response headers")
    for name in ("error", "cursor_status", "cursor_before", "cursor_after"):
        value = receipt[name]
        if not isinstance(value, str) or len(value.encode()) > 256 or any(ord(char) < 32 for char in value):
            raise PolicyError("Invalid spool producer checkpoint metadata")
    for name in ("cursor_before", "cursor_after"):
        if receipt[name] and re.fullmatch(r"(?:0|[1-9][0-9]{0,127})", receipt[name]) is None:
            raise PolicyError("Invalid spool producer cursor")
    if (type(receipt["next_poll_at"]) not in (int, float) or not math.isfinite(receipt["next_poll_at"])
            or receipt["next_poll_at"] < 0 or not integer(receipt["failures"], 0, 10)):
        raise PolicyError("Invalid spool producer backoff metadata")
    validate_completeness(receipt)
    return receipt


def validate_completeness(receipt):
    """Verify implications of the pinned producer's fetchRaw completion flag."""
    if not receipt["complete"]:
        return
    headers = receipt["headers"]
    expected = headers.get("content-length", "")
    if (receipt["error"] or headers.get("transfer-encoding", "") not in ("", "chunked")
            or (expected and (re.fullmatch(r"[+-]?[0-9]+", expected) is None
                              or int(expected) != receipt["bytes"]))):
        raise PolicyError("Complete spool receipt contradicts its transport metadata")


def initialize(archive):
    version = archive.db.execute("SELECT value FROM meta WHERE key='spool_import_version'").fetchone()
    if version:
        if version[0] != "1":
            raise PolicyError("Unsupported spool import catalog version")
        return
    archive.transaction_budget(32768)
    archive.db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE spool_sources(
        spool_id TEXT PRIMARY KEY, identity_sha256 TEXT NOT NULL, identity_bytes INTEGER NOT NULL);
      CREATE TABLE spool_imports(
        external_capture_id TEXT PRIMARY KEY, spool_id TEXT NOT NULL REFERENCES spool_sources(spool_id),
        sequence INTEGER NOT NULL, capture_id INTEGER UNIQUE NOT NULL REFERENCES captures(id),
        receipt_sha256 TEXT NOT NULL, receipt_bytes INTEGER NOT NULL,
        observed_at TEXT NOT NULL, completed_at TEXT NOT NULL, imported_at TEXT NOT NULL,
        http_status INTEGER NOT NULL, complete INTEGER NOT NULL, receipt TEXT NOT NULL,
        UNIQUE(spool_id, sequence));
      INSERT INTO meta VALUES ('spool_import_version','1');
      COMMIT;
    ''')


def present(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='spool_imports'").fetchone())


def import_one(archive, captures, payload, receipt, deadline):
    expected = hashlib.sha256(payload).hexdigest()
    old = archive.db.execute("SELECT * FROM spool_imports WHERE external_capture_id=?", (receipt["capture_id"],)).fetchone()
    if old:
        if old["receipt_sha256"] != expected:
            raise PolicyError("An imported receipt changed; history cannot be overwritten")
        return False
    body_name = receipt["body"].split("/")[-1]
    with regular(captures, body_name) as handle:
        if os.fstat(handle.fileno()).st_size != receipt["bytes"]:
            raise PolicyError("Spool body size does not match its receipt")
        def chunks():
            while True:
                if time.monotonic() >= deadline:
                    raise BudgetError("Spool import run budget exhausted")
                chunk = handle.read(65536)
                if not chunk:
                    break
                yield chunk
        checksum, size = archive.store(chunks(), max_bytes=64 * 1024 * 1024)
    if checksum != receipt["sha256"] or size != receipt["bytes"]:
        raise PolicyError("Spool body hash does not match its receipt")
    receipt_hash, receipt_bytes = archive.store([payload])
    headers = receipt["headers"]
    record = {"url": receipt["url"], "final_url": receipt["url"],
              "captured_at": receipt["completed_at"], "sha256": checksum, "bytes": size,
              "content_type": headers.get("content-type", "application/octet-stream"),
              "etag": headers.get("etag"), "last_modified": headers.get("last-modified"),
              "content_encoding": headers.get("content-encoding"), "source_url": receipt["source"],
              "media_reference_id": None, "format_version": 2}
    archive.transaction_budget(len(payload) + len(json.dumps(record).encode()) + 8192)
    imported = now()
    with archive.db:
        archive.db.execute("INSERT OR IGNORE INTO resources(url,discovered_at,via,source_url) VALUES (?,?,?,?)",
                           (receipt["url"], receipt["observed_at"], "fediwatch-import", receipt["source"]))
        cursor = archive.db.execute('''INSERT INTO captures(url,final_url,captured_at,sha256,bytes,content_type,etag,
                            last_modified,content_encoding,source_url,media_reference_id,format_version)
                            VALUES (:url,:final_url,:captured_at,:sha256,:bytes,:content_type,:etag,
                            :last_modified,:content_encoding,:source_url,:media_reference_id,:format_version)''', record)
        archive.db.execute("UPDATE resources SET state='done',error=NULL WHERE url=?", (receipt["url"],))
        archive.db.execute("INSERT INTO spool_imports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (receipt["capture_id"], receipt["spool_id"], receipt["sequence"], cursor.lastrowid,
                            receipt_hash, receipt_bytes, receipt["observed_at"], receipt["completed_at"], imported,
                            receipt["status"], int(receipt["complete"]), payload.decode("utf-8")))
    save_manifest(archive, cursor.lastrowid)
    return True


def import_spool(archive, path, limit=None):
    limit = archive.config.max_urls if limit is None else limit
    if not integer(limit, 1, archive.config.max_urls):
        raise PolicyError("Import limit must be within the configured item budget")
    separate_paths(path, archive.root)
    deadline = time.monotonic() + archive.config.max_run_seconds
    result = {"imported": 0, "already_imported": 0, "orphan_files": 0, "network_requests": 0, "deferred": False}
    with snapshot(path) as (root, directory, identity_bytes, identity):
        if root == archive.root or root in archive.root.parents or archive.root in root.parents:
            raise PolicyError("Import source and archive must be separate directories")
        # Snapshot under the producer's shared lock. Bound directory work/memory;
        # never infer progress from an orphan or skip unseen smaller sequences.
        names, other_names = [], set()
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > DIRECTORY_LIMIT:
                    raise PolicyError("Spool snapshot exceeds the bounded directory-entry limit")
                if time.monotonic() >= deadline:
                    raise BudgetError("Spool directory scan exhausted the run budget")
                if not entry.is_file(follow_symlinks=False) or not re.fullmatch(r"[0-9]{20}\.(?:json|body|part|json\.part|body\.part)", entry.name):
                    raise PolicyError("Unrecognized or unsafe spool capture entry")
                if entry.name.endswith(".json"):
                    names.append(entry.name)
                else:
                    other_names.add(entry.name)
        initialize(archive)
        identity_hash = hashlib.sha256(identity_bytes).hexdigest()
        existing = archive.db.execute("SELECT identity_sha256 FROM spool_sources WHERE spool_id=?", (identity["spool_id"],)).fetchone()
        if existing and existing[0] != identity_hash:
            raise PolicyError("Imported spool identity changed; use a new producer spool")
        if not existing:
            checksum, size = archive.store([identity_bytes])
            archive.transaction_budget(8192)
            with archive.db:
                archive.db.execute("INSERT INTO spool_sources VALUES (?,?,?)", (identity["spool_id"], checksum, size))
        pending, committed = [], []
        for name in sorted(names):
            if time.monotonic() >= deadline:
                raise BudgetError("Spool admission scan exhausted the run budget")
            known = archive.db.execute(
                "SELECT 1 FROM spool_imports WHERE spool_id=? AND sequence=?",
                (identity["spool_id"], int(name[:-5]))).fetchone()
            (committed if known else pending).append(name)
        # New receipts must not sit behind repeated full reads of old metadata.
        # Numeric ordering is deterministic within each class. The known class
        # is a bounded opportunistic consistency check, not a complete audit.
        for name in pending + committed:
            if time.monotonic() >= deadline or result["imported"] >= limit:
                result["deferred"] = True
                break
            payload, receipt = read_metadata(directory, name)
            receipt = validate(receipt, identity, name[:-5], archive.config)
            if import_one(archive, directory, payload, receipt, deadline):
                result["imported"] += 1
            else:
                result["already_imported"] += 1
        # Complete body/receipt pairs are not orphans; parts and unpaired bodies are.
        result["orphan_files"] = len(other_names - {name[:-5] + ".body" for name in names})
    return result


def save_manifest(archive, capture_id):
    row = archive.db.execute("SELECT * FROM spool_imports WHERE capture_id=?", (capture_id,)).fetchone()
    value = dict(row)
    value["receipt"] = strict_json(value["receipt"])
    value.update(record_type="local_spool_import", format_version=1, contract_commit=CONTRACT_COMMIT)
    value["receipt_object"] = "../objects/" + row["receipt_sha256"][:2] + "/" + row["receipt_sha256"]
    value["body_object"] = "../objects/" + value["receipt"]["sha256"][:2] + "/" + value["receipt"]["sha256"]
    value["capture_manifest"] = str(capture_id) + ".json"
    capture = dict(archive.db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone())
    capture["local_import"] = {"format_version": 1, "manifest": "import-" + str(capture_id) + ".json",
                              "complete": bool(row["complete"]), "http_status": row["http_status"],
                              "external_capture_id": row["external_capture_id"],
                              "observed_at": row["observed_at"], "completed_at": row["completed_at"],
                              "imported_at": row["imported_at"]}
    archive.budget((len(json.dumps(value).encode()) + len(json.dumps(capture).encode())) * 2 + 4096)
    for name, payload in ((str(capture_id) + ".json", capture), ("import-" + str(capture_id) + ".json", value)):
        try:
            atomic_json(archive.root / "manifests" / name, payload)
        except OSError:
            pass  # The catalog is authoritative; repair reconstructs either sidecar.


def repair(archive):
    if present(archive.db):
        for row in archive.db.execute("SELECT capture_id FROM spool_imports"):
            save_manifest(archive, row[0])


def normalization_context(archive, capture):
    """Return measured imported HTTP metadata, never reinterpret partial bodies."""
    if not present(archive.db):
        return None
    row = archive.db.execute("SELECT * FROM spool_imports WHERE capture_id=?", (capture["id"],)).fetchone()
    if row is None:
        return None
    path = archive.object_path(row["receipt_sha256"])
    if (not integer(row["receipt_bytes"], 0, METADATA_LIMIT)
            or not path.is_file() or path.stat().st_size != row["receipt_bytes"]
            or sha256_file(path) != row["receipt_sha256"]):
        raise PolicyError("Imported receipt must remain intact before normalization")
    receipt = strict_json(path.read_bytes())
    if (receipt["capture_id"] != row["external_capture_id"] or receipt["sha256"] != capture["sha256"]
            or receipt["bytes"] != capture["bytes"] or receipt["observed_at"] != row["observed_at"]
            or receipt["completed_at"] != capture["captured_at"]):
        raise PolicyError("Imported provenance no longer matches its archived capture")
    validate_completeness(receipt)
    if not receipt["complete"] or not 100 <= receipt["status"] <= 599:
        raise PolicyError("Partial or transport-error imports remain raw and cannot be normalized")
    return {"observed_at": receipt["observed_at"], "status": receipt["status"],
            "source_record_id": "fediwatch:" + receipt["capture_id"]}
