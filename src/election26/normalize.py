"""Optional verified SDK boundary; never reinterpret operational catalog schema."""
import hashlib
import importlib.metadata
import json
import re
from datetime import datetime
from pathlib import Path

from .archive import sha256_file
from .config import PolicyError

SDK_COMMIT = "6aa8051dc7ea560943a60a25e1d600467f2778bc"
SPEC_VERSION = "0.10.1"
STARLANG_COMMIT = "9198370f7a6f3e2a5ea00af3efdb6d705e102650"


def verify_sdk_source(distribution):
    """Verify pip VCS metadata or the separate locked-Nix build receipt.

    This is installation provenance, not a signature against a malicious local
    package/store administrator. Never relabel a Nix wheel as a pip VCS install.
    """
    def metadata(text):
        try:
            value = json.loads(text or "{}")
        except (ValueError, TypeError):
            raise PolicyError("Invalid optional StarIntel SDK source metadata") from None
        if not isinstance(value, dict):
            raise PolicyError("Invalid optional StarIntel SDK source metadata")
        return value

    direct_text = distribution.read_text("direct_url.json")
    if direct_text is not None:
        direct = metadata(direct_text)
        vcs = direct.get("vcs_info", {})
        if not isinstance(vcs, dict) or vcs.get("commit_id") != SDK_COMMIT:
            raise PolicyError("Install the exact pinned StarIntel SDK commit")
        return
    receipt = metadata(distribution.read_text("election26_nix_source.json"))
    expected = {
        "format": "election26-nix-source-v1",
        "source_url": "github:lost-rob0t/starintel-doc",
        "source_rev": SDK_COMMIT,
    }
    location = Path(distribution.locate_file("")).resolve()
    digest = receipt.get("source_nar_hash", "")
    if (not location.is_relative_to(Path("/nix/store"))
            or set(receipt) != {*expected, "source_nar_hash"}
            or any(receipt.get(key) != value for key, value in expected.items())
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", digest) is None):
        raise PolicyError("Install the exact pinned StarIntel SDK commit")


def sdk():
    distribution = importlib.metadata.distribution("starintel_doc")
    if distribution.version != SPEC_VERSION:
        raise PolicyError("Optional StarIntel SDK must be version 0.10.1")
    verify_sdk_source(distribution)
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
    adapter = sdk()
    headers = {"content-type": row["content_type"]}
    if row["etag"]:
        headers["etag"] = row["etag"]
    if row["last_modified"]:
        headers["last-modified"] = row["last_modified"]
    document = adapter.build_http_transaction(
        dataset=f"election26-{archive.config.country.lower()}-{archive.config.year}",
        method="GET", url=row["final_url"], response_status=200,
        response_headers=headers, observed_at=row["captured_at"],
        fields={
            "observedAt": int(datetime.fromisoformat(row["captured_at"]).timestamp()),
            "sourceUrls": list(dict.fromkeys([row["url"], row["final_url"]])),
            "sourceRecordIds": ["election26-observation:" + hashlib.sha256(
                (row["url"] + row["captured_at"] + row["sha256"]).encode("utf-8")).hexdigest()],
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
