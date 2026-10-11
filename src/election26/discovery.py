"""Bounded discovery. A candidate is not an archived item or verified fact."""
from html.parser import HTMLParser
import json
from urllib.parse import urljoin, urlencode
import xml.etree.ElementTree as ET

from .config import PolicyError, BudgetError, canonical_url, credential


class MediaParser(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        values = []
        if tag in {"audio", "video", "source", "img"}:
            values.append(attrs.get("src"))
            if tag == "video":
                values.append(attrs.get("poster"))
        if tag == "a" and attrs.get("href", "").split("?")[0].lower().endswith((".mp4", ".mp3", ".wav", ".m4a", ".ogg", ".webm", ".pdf", ".jpg", ".png")):
            values.append(attrs["href"])
        if tag == "meta" and attrs.get("property") in {"og:video", "og:video:url", "og:audio", "og:image"}:
            values.append(attrs.get("content"))
        for value in values:
            if value:
                try:
                    self.links.append(canonical_url(urljoin(self.base, value)))
                except ValueError:
                    continue


def media_links(html, base):
    parser = MediaParser(base)
    parser.feed(html)
    return list(dict.fromkeys(parser.links))


def feed_links(data):
    # Reject entity/DTD constructs, including XML with UTF-16/32 null bytes.
    if b"\x00" in data or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise PolicyError("DTD/entity feed documents are not supported")
    root = ET.fromstring(data)
    links = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "link":
            if item.attrib.get("rel", "alternate") in {"alternate", "enclosure"}:
                value = item.attrib.get("href") or item.text
                if value:
                    links.append(value.strip())
        if tag == "enclosure" and item.attrib.get("url"):
            links.append(item.attrib["url"])
    return list(dict.fromkeys(links))


def discover(archive, client, mode, query=None):
    config = archive.config
    if mode == "manual":
        candidates = config.manual_urls
    elif mode == "rss":
        candidates = []
        errors = []
        deferred = False
        cursor_row = archive.db.execute("SELECT value FROM meta WHERE key='rss_cursor'").fetchone()
        start = int(cursor_row[0]) % len(config.feeds) if cursor_row and config.feeds else 0
        indexes = [start] if config.feeds else []  # One rotating feed bounds discovery memory and work.
        for index in indexes:
            url = config.feeds[index]
            requests_before = getattr(client, "request_count", 0)
            try:
                with client.open(url) as response:
                    # Archive original feed bytes before interpreting them.
                    data = response.read_bounded(min(config.max_file_bytes, 4 * 1024 * 1024))
                    archive.enqueue([url], via="rss-feed")
                    class FeedResponse:
                        pass
                    original = FeedResponse()
                    original.url = response.url
                    original.headers = response.headers
                    original.chunks = lambda: iter([data])
                    archive.capture(config.permit(url), original)
                    candidates.extend(feed_links(data))
            except BudgetError:
                deferred = True
                # A costly feed must not monopolize every future discovery tick.
                # If no request was admitted, retain its turn for the next pass.
                if getattr(client, "request_count", 0) > requests_before:
                    with archive.db:
                        archive.db.execute("INSERT OR REPLACE INTO meta VALUES ('rss_cursor',?)", (str((index + 1) % len(config.feeds)),))
                break
            except Exception as exc:
                errors.append(type(exc).__name__)  # Never expose provider error text.
            with archive.db:
                archive.db.execute("INSERT OR REPLACE INTO meta VALUES ('rss_cursor',?)", (str((index + 1) % len(config.feeds)),))
    elif mode == "brave":
        if not config.brave_enabled:
            raise PolicyError("Brave is disabled; enabling it may incur API charges")
        if not query:
            raise PolicyError("Brave discovery needs an explicit query")
        params = {"q": query, "country": config.country.upper(), "count": min(config.max_urls, 20),
                  "freshness": config.date_from + "to" + config.date_to}
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode(params)
        headers = {"X-Subscription-Token": credential(config.brave_credential), "Accept": "application/json"}
        with client.request(url, headers=headers, allowed_hosts=["api.search.brave.com"]) as response:
            if response.status != 200:
                raise PolicyError("Brave search failed")
            result = json.loads(response.read_bounded(4 * 1024 * 1024))
        candidates = [r["url"] for r in result.get("web", {}).get("results", []) if "url" in r]
    else:
        raise PolicyError("Unknown discovery mode")
    permitted = []
    rejected = 0
    considered = 0
    unseen = set()
    for url in candidates:
        considered += 1
        try:
            allowed = config.permit(url)
        except ValueError:
            rejected += 1
            continue
        if allowed in unseen or archive.db.execute("SELECT 1 FROM resources WHERE url=?", (allowed,)).fetchone():
            continue
        unseen.add(allowed)
        permitted.append(allowed)
        if len(permitted) >= config.max_urls:
            break
    result = {"queued": archive.enqueue(permitted, via=mode), "outside_allowlist_or_invalid": rejected,
              "candidates_considered": considered,
              "truncated": considered < len(candidates)}
    if mode == "rss":
        result.update({"errors": errors, "deferred": deferred})
    return result
