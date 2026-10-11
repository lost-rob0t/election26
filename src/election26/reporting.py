"""Optional aggregate-only Discord publisher. No source URLs or text leave disk."""
import json
import re
import time
from urllib.parse import urlsplit

from .archive import status
from .config import PolicyError, credential


def stats_payload(snapshot):
    # Allowlist numeric fields rather than attempting to redact arbitrary text.
    names = ("capture_observations", "unique_objects", "original_bytes", "stored_object_bytes",
             "canonical_documents", "transcripts", "errors")
    clean = {key: int(snapshot.get(key, 0)) for key in names}
    return {"content": "election26 committed archive stats\n" + "\n".join(f"{k}: {v}" for k, v in clean.items()),
            "allowed_mentions": {"parse": []}}


def publish(archive, client, send=False):
    payload = stats_payload(status(archive.root))
    if not send:
        return {"sent": False, "enabled": archive.config.discord_enabled, "payload": payload}
    if not archive.config.discord_enabled:
        raise PolicyError("Discord publishing is disabled")
    key = "discord_last_success"
    row = archive.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    stamp = time.time()
    if row and stamp - float(row[0]) < archive.config.discord_interval_seconds:
        return {"sent": False, "reason": "interval_not_elapsed"}
    pending = archive.db.execute("SELECT value FROM meta WHERE key='discord_pending'").fetchone()
    if pending:
        # Network failure may still have reached Discord. Do not retry blindly.
        raise PolicyError("Previous Discord send is uncertain; inspect destination before clearing pending state")
    webhook = credential(archive.config.discord_credential)
    parts = urlsplit(webhook)
    if (parts.scheme != "https" or parts.hostname != "discord.com" or parts.port not in (None, 443)
            or parts.query or parts.fragment or parts.username or parts.password
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+", parts.path)):
        raise PolicyError("Discord webhook must be a standard discord.com HTTPS webhook URL")
    body = json.dumps(payload).encode("utf-8")
    with archive.db:
        archive.db.execute("INSERT INTO meta VALUES ('discord_pending',?)", (str(stamp),))
    with client.request(webhook + "?wait=true", method="POST", body=body,
                        headers={"Content-Type": "application/json"}, allowed_hosts=["discord.com"]) as response:
        if response.status != 200:
            raise PolicyError("Discord send failed or is uncertain; no automatic retry")
        receipt = json.loads(response.read_bounded(16 * 1024))
        if not isinstance(receipt.get("id"), str) or not receipt["id"].isdigit():
            raise PolicyError("Discord response lacks a confirmed message ID")
    with archive.db:
        archive.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(stamp)))
        archive.db.execute("DELETE FROM meta WHERE key='discord_pending'")
    return {"sent": True}
