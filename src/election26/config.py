"""Explicit source boundaries and non-secret configuration."""
import json
import os
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl


class PolicyError(ValueError):
    pass


class BusyError(PolicyError):
    pass


class BudgetError(PolicyError):
    pass


def canonical_url(url):
    if not isinstance(url, str) or len(url.encode("utf-8")) > 4096:
        raise PolicyError("URL must be a string of at most 4096 UTF-8 bytes")
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
    media_policies: dict = field(default_factory=dict)
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
    max_requests: int = 200
    max_run_seconds: int = 300
    request_delay_seconds: float = 1.0
    user_agent: str = "election26/0.1 (bounded public-source archive)"
    brave_enabled: bool = False
    brave_credential: str = "env:BRAVE_SEARCH_API_KEY"
    discord_enabled: bool = False
    discord_credential: str = "env:ELECTION26_DISCORD_WEBHOOK"
    discord_interval_seconds: int = 900
    retention_days: int = 90

    def __post_init__(self):
        for name in ("brave_enabled", "discord_enabled"):
            if type(getattr(self, name)) is not bool:
                raise PolicyError(name + " must be a JSON boolean")
        for name in ("allowed_hosts", "manual_urls", "feeds"):
            values = getattr(self, name)
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise PolicyError(name + " must be an array of strings")
        if not isinstance(self.country, str) or not re.fullmatch(r"[A-Za-z]{2}", self.country):
            raise PolicyError("country must be a two-letter code")
        from datetime import date
        if not isinstance(self.date_from, str) or not isinstance(self.date_to, str):
            raise PolicyError("Date bounds must be ISO date strings")
        if date.fromisoformat(self.date_from) > date.fromisoformat(self.date_to):
            raise PolicyError("date_from must precede date_to")
        for name in ("year", "max_file_bytes", "max_archive_bytes", "max_urls", "max_requests", "max_run_seconds", "retention_days", "discord_interval_seconds"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise PolicyError(name + " must be a positive integer")
        if type(self.min_free_bytes) is not int or self.min_free_bytes < 0:
            raise PolicyError("min_free_bytes must be a nonnegative integer")
        if (type(self.request_delay_seconds) not in (int, float)
                or not math.isfinite(self.request_delay_seconds)
                or not 0 <= self.request_delay_seconds <= 60):
            raise PolicyError("request_delay_seconds must be finite and between 0 and 60")
        if not 1900 <= self.year <= 9999:
            raise PolicyError("year must be between 1900 and 9999")
        if self.max_requests > 2000 or self.max_run_seconds > 3600:
            raise PolicyError("Request/run limits may not exceed 2000 requests or 3600 seconds")
        if self.max_urls > 500:
            raise PolicyError("max_urls must not exceed 500 per invocation")
        if self.discord_interval_seconds < 900:
            raise PolicyError("Discord interval must be at least 900 seconds")
        if not isinstance(self.user_agent, str) or not self.user_agent or len(self.user_agent) > 256 or any(ord(c) < 32 for c in self.user_agent):
            raise PolicyError("Invalid user agent")
        for reference in (self.brave_credential, self.discord_credential):
            if not isinstance(reference, str) or not (re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", reference)
                    or re.fullmatch(r"keyring:[A-Za-z0-9_.-]+/[A-Za-z0-9_.@-]+", reference)):
                raise PolicyError("Credentials must be environment or keyring references")
        self.allowed_hosts = [h.lower() for h in self.allowed_hosts]
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", h) for h in self.allowed_hosts):
            raise PolicyError("Use exact DNS hostnames, without wildcards or ports")

        if not isinstance(self.media_policies, dict):
            raise PolicyError("media_policies must map exact source hosts to policies")
        for host, policy in self.media_policies.items():
            if host not in self.allowed_hosts:
                raise PolicyError("Media policy source hosts must be explicitly allowlisted")
            if not isinstance(policy, dict) or set(policy) - {
                    "download", "types", "max_file_bytes", "max_total_bytes", "min_free_bytes"}:
                raise PolicyError("Invalid source media policy fields")
            if type(policy.get("download", False)) is not bool:
                raise PolicyError("Media download opt-in must be a JSON boolean")
            types = policy.get("types", [])
            if (not isinstance(types, list) or any(type(t) is not str or t not in
                    {"audio", "video", "image", "document"} for t in types)):
                raise PolicyError("Media types must select audio, video, image or document")
            for name in ("max_file_bytes", "max_total_bytes", "min_free_bytes"):
                if name in policy and (type(policy[name]) is not int or policy[name] <
                                       (0 if name == "min_free_bytes" else 1)):
                    raise PolicyError("Media budgets must be explicit finite integers")
            if policy.get("download") and (not types or any(name not in policy for name in
                    ("max_file_bytes", "max_total_bytes", "min_free_bytes"))):
                raise PolicyError("Media opt-in requires selected types and all three budgets")

    @classmethod
    def load(cls, path=None):
        def reject_constant(value):
            raise PolicyError("Non-finite JSON numbers are not supported")
        data = json.loads(Path(path).read_text(), parse_constant=reject_constant) if path else {}
        if not isinstance(data, dict):
            raise PolicyError("Configuration must be a JSON object")
        return cls(**data)

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
