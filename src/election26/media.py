"""Source-attributed, links-first media admission; no downloader transports."""
from pathlib import PurePosixPath
from urllib.parse import urlsplit, unquote

from .config import PolicyError


SUFFIXES = {
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"},
    "video": {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".m3u8", ".mpd"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".heic"},
    "document": {".pdf"},
}


def url_kind(url):
    suffix = PurePosixPath(unquote(urlsplit(url).path).lower()).suffix
    return next((kind for kind, values in SUFFIXES.items() if suffix in values), None)


def content_kind(content_type):
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith(("audio/", "video/", "image/")):
        return mime.split("/", 1)[0]
    if mime == "application/pdf":
        return "document"
    # Unknown/binary formats fail closed; never assume that an opaque URL is text.
    if (mime.startswith("text/") or mime in {"application/json", "application/xml",
            "application/rss+xml", "application/atom+xml", "application/xhtml+xml"}
            or mime.endswith(("+json", "+xml"))):
        return None
    return "unknown"


def declared_size(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 32 or not value.isascii() or not value.isdigit():
        raise PolicyError("Invalid advertised media length")
    return int(value)


def admission(config, source_url, url, content_type=None, size=None, hint=None, response=False):
    """Return policy/reason. Hints classify admission, never fill missing metadata."""
    try:
        config.permit(url)
        host = urlsplit(config.permit(source_url)).hostname if source_url else None
    except ValueError:
        return None, "outside_allowlist"
    policy = config.media_policies.get(host, {})
    if not policy.get("download", False):
        return None, "source_not_opted_in"
    kinds = {kind for kind in (url_kind(url), hint,
             content_kind(content_type) if response or content_type else None) if kind}
    if response and content_kind(content_type) is None:
        return None, "unexpected_response_type"
    if "unknown" in kinds or not kinds.issubset(set(policy["types"])):
        return None, "type_not_selected"
    if len(kinds) > 1:
        return None, "type_mismatch"
    if size is not None and size > min(config.max_file_bytes, policy["max_file_bytes"]):
        return None, "per_file_budget"
    return policy, None


class MediaCatalog:
    """Operational references and append-only observations, separate from StarIntel."""
    def media_reference(self, url, source_url, parent_capture_id=None, content_type=None,
                        advertised_bytes=None, kind_hint=None):
        from .archive import now
        from .config import canonical_url
        url = canonical_url(url)
        source_url = canonical_url(source_url) if source_url else None
        if content_type is not None and (not isinstance(content_type, str) or len(content_type.encode()) > 1024):
            raise PolicyError("Media type metadata exceeds the per-field limit")
        if kind_hint is not None and kind_hint not in SUFFIXES:
            raise PolicyError("Invalid media kind hint")
        if advertised_bytes is not None and (type(advertised_bytes) is not int or advertised_bytes < 0):
            raise PolicyError("Invalid advertised media length")
        # Direct URLs have no parent; repeat attempts reuse their reference.
        row = self.db.execute("SELECT * FROM media_references WHERE url=? AND source_url IS ? "
                              "AND parent_capture_id IS ?", (url, source_url, parent_capture_id)).fetchone()
        if row:
            return row
        self.transaction_budget(len(url.encode()) + len((source_url or "").encode()) + 4096)
        with self.db:
            cursor = self.db.execute("INSERT INTO media_references(url,source_url,parent_capture_id,discovered_at,"
                "declared_type,declared_bytes,kind_hint) VALUES (?,?,?,?,?,?,?)",
                (url, source_url, parent_capture_id, now(), content_type, advertised_bytes, kind_hint))
        return self.db.execute("SELECT * FROM media_references WHERE id=?", (cursor.lastrowid,)).fetchone()

    def known_media_reference(self, url, source_url, final_url=None, force=False):
        """Every entry point retains known-media semantics, even for opaque URLs."""
        reference = self.db.execute("SELECT * FROM media_references WHERE url=? AND source_url IS ? "
                                    "ORDER BY id DESC LIMIT 1", (url, source_url)).fetchone()
        known = reference or self.db.execute("SELECT * FROM media_references WHERE url IN (?,?) LIMIT 1",
                                             (url, final_url or url)).fetchone()
        hint = url_kind(url) or url_kind(final_url or url)
        if reference or known or hint or force:
            return reference or self.media_reference(url, source_url,
                kind_hint=hint or (known["kind_hint"] if known else None))
        return None

    def media_event(self, reference, state, reason=None, capture=None, response=None, transactional=False):
        import json
        from .archive import now, atomic_json
        record = {"format_version": 2, "record_type": "media_observation",
                  "reference_id": reference["id"], "source_url": reference["source_url"],
                  "url": reference["url"], "parent_capture_id": reference["parent_capture_id"],
                  "discovered_at": reference["discovered_at"], "observed_at": now(),
                  "declared_type": reference["declared_type"], "declared_bytes": reference["declared_bytes"],
                  "state": state, "reason": reason, "capture_id": capture["id"] if capture else None,
                  "sha256": capture["sha256"] if capture else None,
                  "source_policy": self.config.media_policies.get(urlsplit(reference["source_url"]).hostname, {})
                                   if reference["source_url"] else {}}
        if response:
            record.update({"final_url": response.url,
                           "response_type": response.headers.get("content-type"),
                           "response_bytes": declared_size(response.headers.get("content-length"))})
        previous = self.db.execute("SELECT id,payload FROM media_observations WHERE reference_id=? ORDER BY id DESC LIMIT 1",
                                   (reference["id"],)).fetchone()
        if previous:
            old = json.loads(previous["payload"])
            if all(old.get(k) == v for k, v in record.items() if k != "observed_at"):
                return dict(old, id=previous["id"])
        payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
        self.transaction_budget(len(payload.encode()) + 4096)
        import contextlib
        with contextlib.nullcontext() if transactional else self.db:
            cursor = self.db.execute("INSERT INTO media_observations(reference_id,payload) VALUES (?,?)",
                                     (reference["id"], payload))
        record["id"] = cursor.lastrowid
        if transactional:
            return record
        try:
            atomic_json(self.root / "manifests" / ("media-" + str(record["id"]) + ".json"), record)
        except OSError:
            record["manifest_pending"] = True
        return record

    def media_records(self):
        """Replay latest outcomes locally. Missing originals never become not-requested."""
        import json
        from .archive import sha256_file
        records = []
        for row in self.db.execute("SELECT id,payload FROM media_observations WHERE id IN "
                                  "(SELECT MAX(id) FROM media_observations GROUP BY reference_id) ORDER BY id"):
            record = dict(json.loads(row["payload"]), id=row["id"])
            if record["state"] == "downloaded":
                path = self.object_path(record["sha256"])
                if not path.is_file() or sha256_file(path) != record["sha256"]:
                    record.update(recorded_state="downloaded", state="missing", reason="original_missing_or_corrupt")
            records.append(record)
        return records

    def media_used(self, source_url):
        host = urlsplit(source_url).hostname
        sizes = {}
        for row in self.db.execute("SELECT sha256,bytes,source_url,content_type,media_reference_id FROM captures"):
            if row["source_url"] and urlsplit(row["source_url"]).hostname == host and (row["media_reference_id"] or content_kind(row["content_type"])):
                sizes[row["sha256"]] = max(sizes.get(row["sha256"], 0), row["bytes"])
        return sum(sizes.values())

    def reference_admission(self, reference, response=None):
        policy, reason = admission(self.config, reference["source_url"],
            response.url if response else reference["url"],
            response.headers.get("content-type") if response else reference["declared_type"],
            declared_size(response.headers.get("content-length")) if response else reference["declared_bytes"],
            reference["kind_hint"], response=response is not None)
        if policy and response and reference["declared_type"]:
            declared = content_kind(reference["declared_type"])
            if declared and declared != content_kind(response.headers.get("content-type")):
                return None, "type_mismatch"
        if policy:
            size = (declared_size(response.headers.get("content-length")) if response else reference["declared_bytes"])
            used = self.media_used(reference["source_url"])
            if used >= policy["max_total_bytes"] or (size is not None and used + size > policy["max_total_bytes"]):
                return None, "source_disk_budget"
            import shutil
            if shutil.disk_usage(self.root).free - (size or 0) - 64 * 1024 < max(
                    self.config.min_free_bytes, policy["min_free_bytes"]):
                return None, "free_disk_reserve"
        return policy, reason

    def record_media_links(self, capture, links):
        for link in links[:self.config.max_urls]:
            reference = self.media_reference(link["url"], capture["source_url"], capture["id"],
                link.get("content_type"), link.get("advertised_bytes"), link.get("kind_hint"))
            policy, reason = self.reference_admission(reference)
            self.media_event(reference, "pending" if policy else "not_requested", reason)

    def fetch_media(self, client, limit, exclude=()):
        import json
        from .config import BudgetError
        if limit <= 0:
            return []
        # A durable rotating window bounds local replay work as well as requests.
        cursor = self.db.execute("SELECT value FROM meta WHERE key='media_cursor'").fetchone()
        start = int(cursor[0]) if cursor else 0
        rows = self.db.execute("SELECT * FROM media_references WHERE id>? ORDER BY id LIMIT ?",
                               (start, limit)).fetchall()
        if len(rows) < limit:
            rows += self.db.execute("SELECT * FROM media_references WHERE id<=? ORDER BY id LIMIT ?",
                                    (start, limit - len(rows))).fetchall()
        results = []
        position = start
        for reference in rows:
            previous_position = position
            position = reference["id"]
            self.transaction_budget(1024)
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('media_cursor',?)", (str(reference["id"]),))
            if reference["id"] in exclude:
                continue
            last = self.db.execute("SELECT payload FROM media_observations WHERE reference_id=? ORDER BY id DESC LIMIT 1",
                                    (reference["id"],)).fetchone()
            if last and json.loads(last[0])["state"] == "downloaded":
                continue
            policy, reason = self.reference_admission(reference)
            if not policy:
                self.media_event(reference, "not_requested", reason)
                continue
            if len(results) >= limit:
                break
            requests_before = getattr(client, "request_count", None)
            try:
                self.media_event(reference, "pending")
                self.enqueue([reference["url"]], via="policy-media", source_url=reference["source_url"])
                with client.open(reference["url"]) as response:
                    record = self.capture(reference["url"], response, source_url=reference["source_url"], reference=reference)
                results.append({"state": "done" if record.get("sha256") else "not_requested",
                                "media_reference_id": reference["id"]})
            except BudgetError:
                if requests_before is not None and getattr(client, "request_count", None) == requests_before:
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('media_cursor',?)", (str(previous_position),))
                results.append({"state": "deferred", "reason": "run_budget_exhausted"})
                break
            except Exception as exc:
                self.media_event(reference, "unavailable", type(exc).__name__)
                results.append({"state": "failed", "media_reference_id": reference["id"], "error": type(exc).__name__})
        return results
