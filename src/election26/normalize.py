"""Optional verified SDK boundary; never reinterpret operational catalog schema."""
import hashlib
import importlib.metadata
import json
from datetime import datetime
from pathlib import Path

from .archive import sha256_file
from .config import PolicyError

SDK_COMMIT = "6aa8051dc7ea560943a60a25e1d600467f2778bc"
SPEC_VERSION = "0.10.1"
STARLANG_COMMIT = "9198370f7a6f3e2a5ea00af3efdb6d705e102650"


def sdk():
    distribution = importlib.metadata.distribution("starintel_doc")
    if distribution.version != SPEC_VERSION:
        raise PolicyError("Optional StarIntel SDK must be version 0.10.1")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct.get("vcs_info", {}).get("commit_id") != SDK_COMMIT:
        raise PolicyError("Install the exact pinned StarIntel SDK commit")
    import starintel_doc
    if starintel_doc.SPEC_VERSION != SPEC_VERSION:
        raise PolicyError("Canonical StarIntel schema version mismatch")
    return starintel_doc


def normalize(archive, capture_id):
    """Build only the upstream's canonical HTTP transaction, not inferred entities."""
    row = archive.db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
    if not row:
        raise PolicyError("Unknown capture ID")
    source = archive.object_path(row["sha256"])
    if not source.is_file() or sha256_file(source) != row["sha256"]:
        raise PolicyError("Original source bytes must be intact before normalization")
    existing = archive.db.execute("SELECT * FROM normalizations WHERE capture_id=?", (capture_id,)).fetchone()
    path = archive.safe_path(Path("canonical") / (str(capture_id) + ".json"))
    if existing:
        if not path.exists() or sha256_file(path) != existing["document_sha256"]:
            raise PolicyError("Existing canonical document is missing or corrupt")
        return {"state": "already_normalized", "capture_id": capture_id}
    from .spool import normalization_context
    imported = normalization_context(archive, row)
    observed = imported["observed_at"] if imported else row["captured_at"]
    status = imported["status"] if imported else 200
    source_record = imported["source_record_id"] if imported else "election26-observation:" + hashlib.sha256(
        (row["url"] + row["captured_at"] + row["sha256"]).encode("utf-8")).hexdigest()
    adapter = sdk()
    dataset = f"election26-{archive.config.country.lower()}-{archive.config.year}"
    identity = {}
    if imported:
        # The producer sequence disambiguates identical bodies/timestamps. Use
        # the pinned SDK's existing ID function, not a new wire ID convention.
        from starintel_doc.network_capture import stable_capture_id
        identity["transaction_id"] = stable_capture_id(
            "http-transaction", dataset, {"source_capture_id": source_record})
    headers = {"content-type": row["content_type"]}
    if row["etag"]:
        headers["etag"] = row["etag"]
    if row["last_modified"]:
        headers["last-modified"] = row["last_modified"]
    document = adapter.build_http_transaction(
        dataset=dataset, **identity,
        method="GET", url=row["final_url"], response_status=status,
        response_headers=headers, observed_at=observed,
        fields={
            "observedAt": int(datetime.fromisoformat(observed).timestamp()),
            "sourceUrls": list(dict.fromkeys([row["url"], row["final_url"]])),
            "sourceRecordIds": [source_record],
            "responseBodyHash": row["sha256"], "responseBodySize": row["bytes"],
            "responseBodyArtifactUri": "urn:sha256:" + row["sha256"],
        })
    # The builder supplies defaults that were not measured by this collector.
    # Omit them rather than invent request timing or CAPTCHA absence evidence.
    for name in ("startedAt", "endedAt", "challengeStatus", "requestHeaders"):
        document.pop(name, None)
    adapter.validate_document(document)
    serialized = adapter.stringify_json(document)
    # Round-trip with the SDK's exact parser; stdlib floats never touch this wire.
    adapter.validate_document(adapter.parse_json(serialized))
    payload = serialized.encode("utf-8")
    archive.transaction_budget(len(payload) + 4096)
    if path.exists():
        raise PolicyError("Uncatalogued canonical file exists; preserve it for manual recovery")
    # No raw source content is embedded. The local catalog is the provenance join.
    from .archive import sync_dir
    import os
    import tempfile
    fd, temp = tempfile.mkstemp(prefix="canonical-", dir=archive.root / "tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        Path(temp).unlink(missing_ok=True)
    checksum = hashlib.sha256(payload).hexdigest()
    with archive.db:
        archive.db.execute("INSERT INTO normalizations VALUES (?,?,?,?,?)",
                           (capture_id, document["id"], checksum, str(path.relative_to(archive.root)), SDK_COMMIT))
    return {"state": "normalized", "capture_id": capture_id, "source_sha256": row["sha256"],
            "document_id": document["id"], "document_sha256": checksum, "schema_version": SPEC_VERSION}
