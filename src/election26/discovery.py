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
        self.parents = []

    def handle_starttag(self, tag, attrs):
        from .media import url_kind
        attrs = dict(attrs)
        values = []
        hint = {"audio": "audio", "video": "video", "img": "image"}.get(tag)
        if tag in {"audio", "video"}:
            self.parents.append(tag)
        if tag == "source":
            hint = self.parents[-1] if self.parents else None
        if tag in {"audio", "video", "source", "img"}:
            values.append((attrs.get("src"), hint, attrs.get("type")))
            if tag == "video":
                values.append((attrs.get("poster"), "image", None))
        if tag == "a" and url_kind(attrs.get("href", "")):
            values.append((attrs["href"], url_kind(attrs["href"]), attrs.get("type")))
        if tag == "meta" and attrs.get("property") in {"og:video", "og:video:url", "og:audio", "og:image"}:
            values.append((attrs.get("content"), attrs["property"].split(":")[1], None))
        for value, kind, content_type in values:
            if value and len(self.links) < 500:
                try:
                    self.links.append({"url": canonical_url(urljoin(self.base, value)),
                                       "content_type": content_type, "kind_hint": kind})
                except ValueError:
                    continue

    def handle_endtag(self, tag):
        if self.parents and self.parents[-1] == tag:
            self.parents.pop()


def media_references(html, base):
    parser = MediaParser(base)
    parser.feed(html)
    return parser.links


def media_links(html, base):
    return list(dict.fromkeys(item["url"] for item in media_references(html, base)))


def feed_references(data, base):
    from .media import declared_size, url_kind
    # Apply the same entity/DTD guard before either parser.
    feed_links(data)
    links = []
    for item in ET.fromstring(data).iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "enclosure" or (tag == "link" and item.attrib.get("rel") == "enclosure"):
            value = item.attrib.get("url") or item.attrib.get("href")
            if not value:
                continue
            try:
                url = canonical_url(urljoin(base, value))
            except ValueError:
                continue
            try:
                size = declared_size(item.attrib.get("length"))
            except ValueError:
                size = None  # Preserve the link; malformed lengths remain in raw XML.
            links.append({"url": url, "content_type": item.attrib.get("type"),
                          "advertised_bytes": size, "kind_hint": url_kind(url)})
            if len(links) >= 500:
                break
    return links


def feed_links(data):
    # Reject entity/DTD constructs, including XML with UTF-16/32 null bytes.
    if b"\x00" in data or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise PolicyError("DTD/entity feed documents are not supported")
    root = ET.fromstring(data)
    links = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag == "link":
            if item.attrib.get("rel", "alternate") == "alternate":
                value = item.attrib.get("href") or item.text
                if value:
                    links.append(value.strip())
        # Enclosures are recorded separately with their source's media policy.
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
                archive.enqueue([url], via="rss-feed")
                with client.open(url) as response:
                    # Admission precedes every body read, including deceptive feed URLs.
                    record = archive.capture(config.permit(url), response, max_bytes=4 * 1024 * 1024)
                if record.get("sha256") and record["content_encoding"] in (None, "identity"):
                    data = archive.object_path(record["sha256"]).read_bytes()
                    if record["content_type"].split(";", 1)[0].strip().lower() in {
                            "application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}:
                        candidates.extend(feed_links(data))
                        archive.record_media_links(record, feed_references(data, record["final_url"]))
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
