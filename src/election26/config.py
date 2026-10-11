"""Explicit source boundaries and non-secret configuration."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl


class PolicyError(ValueError):
    pass


def canonical_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PolicyError("Only absolute public HTTP(S) URLs are supported")
    if parts.username or parts.password:
        raise PolicyError("Credential-bearing URLs are not supported")
    if any(k.lower() in {"token", "access_token", "api_key", "apikey", "key", "signature", "sig"}
           or k.lower().startswith("x-amz-") for k, _ in parse_qsl(parts.query)):
        raise PolicyError("Credential or signed URLs are not supported")
    host = parts.hostname.lower().encode("idna").decode("ascii")
    try:
        port = parts.port
    except ValueError as exc:
        raise PolicyError("Invalid URL port") from exc
    if port not in (None, 80, 443):
        raise PolicyError("Nonstandard ports are not supported")
    if ":" in host:
        host = "[" + host + "]"
    if port and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        host += ":" + str(port)
    if any(ord(c) < 32 for c in url) or "\\" in url:
        raise PolicyError("Invalid URL characters")
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


@dataclass
class Config:
    allowed_hosts: list[str] = field(default_factory=list)
    manual_urls: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    country: str = "US"
    year: int = 2026
    date_from: str = "2026-01-01"
    date_to: str = "2026-12-31"
    max_file_bytes: int = 256 * 1024 * 1024
    max_archive_bytes: int = 10 * 1024 * 1024 * 1024
    min_free_bytes: int = 1024 * 1024 * 1024
    max_urls: int = 100
    request_delay_seconds: float = 1.0
    user_agent: str = "election26/0.1 (bounded public-source archive)"
    brave_enabled: bool = False
    brave_credential: str = "env:BRAVE_SEARCH_API_KEY"
    discord_enabled: bool = False
    discord_credential: str = "env:ELECTION26_DISCORD_WEBHOOK"
    discord_interval_seconds: int = 900
    retention_days: int = 90

    @classmethod
    def load(cls, path=None):
        data = json.loads(Path(path).read_text()) if path else {}
        config = cls(**data)
        if not config.country.isalpha() or len(config.country) != 2:
            raise PolicyError("country must be a two-letter code")
        from datetime import date
        if date.fromisoformat(config.date_from) > date.fromisoformat(config.date_to):
            raise PolicyError("date_from must precede date_to")
        for key in ("max_file_bytes", "max_archive_bytes", "max_urls", "retention_days"):
            if not isinstance(getattr(config, key), int) or getattr(config, key) <= 0:
                raise PolicyError(key + " must be a positive integer")
        if config.min_free_bytes < 0 or config.request_delay_seconds < 0:
            raise PolicyError("Disk reserve and delay must not be negative")
        if config.discord_interval_seconds < 900:
            raise PolicyError("Discord interval must be at least 900 seconds")
        config.allowed_hosts = [h.lower() for h in config.allowed_hosts]
        if any("/" in h or "*" in h or ":" in h for h in config.allowed_hosts):
            raise PolicyError("Use exact DNS hostnames, without wildcards or ports")
        return config

    def permit(self, url):
        url = canonical_url(url)
        if urlsplit(url).hostname not in self.allowed_hosts:
            raise PolicyError("URL host is not on the explicit source allowlist")
        return url


def credential(reference):
    """Resolve only references; never put credential values in config or logs."""
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:])
    elif reference.startswith("keyring:"):
        import keyring
        service, username = reference[8:].split("/", 1)
        value = keyring.get_password(service, username)
    else:
        raise PolicyError("Use an env:NAME or keyring:SERVICE/USERNAME reference")
    if not value:
        raise PolicyError("Required credential is not configured")
    return value
