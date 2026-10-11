"""Optional, local-only Whisper. Models must already exist on disk."""
import importlib.metadata
import json
import wave
from pathlib import Path

from .archive import atomic_json, now, sha256_file
from .config import PolicyError


MAX_AUDIO_SECONDS = 600
MAX_PCM_BYTES = MAX_AUDIO_SECONDS * 16000 * 2


def pcm_samples(source):
    """Narrow, network-free first-slice decoder: no FFmpeg or media playlists."""
    with wave.open(str(source), "rb") as audio:
        if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, 16000, 2, "NONE"):
            raise PolicyError("Transcription requires mono 16 kHz 16-bit PCM WAV")
        frames = audio.getnframes()
        if not 0 < frames <= MAX_AUDIO_SECONDS * 16000:
            raise PolicyError("Transcription is limited to complete WAV recordings of at most ten minutes")
        data = audio.readframes(frames)
        if len(data) != frames * 2 or len(data) > MAX_PCM_BYTES:
            raise PolicyError("WAV is truncated or exceeds the decoded-audio budget")
    return data


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
    if archive.root == model_path or archive.root in model_path.parents:
        raise PolicyError("Model checkpoint must come from a trusted location outside the archive")
    if model_path.stat().st_size > 160 * 1024 * 1024:
        raise PolicyError("First-slice CPU transcription supports trusted tiny/base-sized models up to 160 MiB")
    existing = archive.db.execute("SELECT * FROM transcripts WHERE source_sha256=?", (digest,)).fetchone()
    if existing:
        path = archive.safe_path(Path("transcripts") / (digest + ".json"))
        if not path.is_file() or sha256_file(path) != existing["transcript_sha256"]:
            raise PolicyError("Existing transcript is corrupt; preserve it for manual recovery")
        return {"state": "already_transcribed", "source_sha256": digest}
    pcm = pcm_samples(source)
    import numpy as np
    import whisper
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    model = whisper.load_model(str(model_path), device="cpu")
    result = model.transcribe(samples, language=language, fp16=False)
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
    archive.transaction_budget(len(json.dumps(payload).encode("utf-8")) + 4096)
    relative = "transcripts/" + digest + ".json"
    path = archive.safe_path(relative)
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
    path = archive.safe_path(Path("transcripts") / (digest + ".json"))
    if sha256_file(path) != row["transcript_sha256"] or sha256_file(archive.object_path(digest)) != digest:
        raise PolicyError("Source or transcript integrity verification failed")
    if json.loads(path.read_text())["source_sha256"] != digest:
        raise PolicyError("Transcript provenance does not match")
    with archive.db:
        archive.db.execute("UPDATE transcripts SET human_verified=1 WHERE source_sha256=?", (digest,))
    return {"source_sha256": digest, "human_verified": True}
