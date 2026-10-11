"""Bounded discovery. A candidate is not an archived item or verified fact."""
from html.parser import HTMLParser
import json
from urllib.parse import urljoin, urlencode
import xml.etree.ElementTree as ET

from .config import PolicyError, canonical_url, credential


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
        for url in config.feeds[:config.max_urls]:
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
    for url in candidates[:config.max_urls]:
        try:
            permitted.append(config.permit(url))
        except ValueError:
            rejected += 1
    return {"queued": archive.enqueue(permitted, via=mode), "outside_allowlist_or_invalid": rejected,
            "candidates_considered": min(len(candidates), config.max_urls),
            "truncated": len(candidates) > config.max_urls}
