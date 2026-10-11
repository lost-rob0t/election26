"""Optional, local-only Whisper. Models must already exist on disk."""
import importlib.metadata
import json
from pathlib import Path

from .archive import atomic_json, now, sha256_file
from .config import PolicyError


def transcribe(archive, digest, model_path, language=None):
    source = archive.object_path(digest)
    if not source.is_file() or sha256_file(source) != digest:
        raise PolicyError("Source object is missing or corrupt")
    rows = archive.db.execute("SELECT content_type FROM captures WHERE sha256=?", (digest,)).fetchall()
    if not rows or not any(row[0].startswith(("audio/", "video/")) for row in rows):
        raise PolicyError("Only archived audio/video captures may be transcribed")
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_file():
        raise PolicyError("Provide an existing trusted local Whisper model file; no automatic model download")
    existing = archive.db.execute("SELECT * FROM transcripts WHERE source_sha256=?", (digest,)).fetchone()
    if existing:
        path = archive.root / existing["path"]
        if not path.is_file() or sha256_file(path) != existing["transcript_sha256"]:
            raise PolicyError("Existing transcript is corrupt; preserve it for manual recovery")
        return {"state": "already_transcribed", "source_sha256": digest}
    import whisper
    model = whisper.load_model(str(model_path), device="cpu")
    result = model.transcribe(str(source), language=language, fp16=False)
    payload = {
        "source_sha256": digest,
        "engine": "openai-whisper",
        "engine_version": importlib.metadata.version("openai-whisper"),
        "model_sha256": sha256_file(model_path),
        "created_at": now(),
        "text": result["text"],
        "language": result.get("language"),
        "segments": result.get("segments", []),
        "accuracy_reviewed": False,
    }
    # A transcript remains a derived local artifact, never a replacement for raw.
    archive.budget(len(json.dumps(payload).encode("utf-8")) + 64 * 1024)
    relative = "transcripts/" + digest + ".json"
    path = archive.root / relative
    if path.exists():
        # Orphan transcript from an interrupted transaction: preserve for review.
        raise PolicyError("Uncatalogued transcript exists; inspect before retrying")
    atomic_json(path, payload)
    checksum = sha256_file(path)
    if json.loads(path.read_text())["source_sha256"] != digest:
        raise PolicyError("Transcript source verification failed")
    with archive.db:
        archive.db.execute(
            "INSERT INTO transcripts(source_sha256,transcript_sha256,path,created_at,engine) VALUES (?,?,?,?,?)",
            (digest, checksum, relative, payload["created_at"], "openai-whisper"))
    return {"state": "transcribed", "source_sha256": digest, "transcript_sha256": checksum,
            "human_verified": False}


def verify_transcript(archive, digest):
    """Caller explicitly asserts they reviewed accuracy; also verify file integrity."""
    archive.object_path(digest)  # Validate digest shape before SQL/path access.
    row = archive.db.execute("SELECT * FROM transcripts WHERE source_sha256=?", (digest,)).fetchone()
    if not row:
        raise PolicyError("No transcript exists for that object")
    path = archive.root / row["path"]
    if sha256_file(path) != row["transcript_sha256"] or sha256_file(archive.object_path(digest)) != digest:
        raise PolicyError("Source or transcript integrity verification failed")
    if json.loads(path.read_text())["source_sha256"] != digest:
        raise PolicyError("Transcript provenance does not match")
    with archive.db:
        archive.db.execute("UPDATE transcripts SET human_verified=1 WHERE source_sha256=?", (digest,))
    return {"source_sha256": digest, "human_verified": True}
