"""Public-address-only HTTP with exact host allowlists and bounded redirects."""
import contextlib
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser

from .config import PolicyError, canonical_url


def public_address(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise PolicyError("Host has no usable address")
    # Reject mixed public/private answers too; pin the checked address at connect.
    ips = [info[4][0] for info in addresses]
    if not all(ipaddress.ip_address(ip).is_global for ip in ips):
        raise PolicyError("Private, loopback, and reserved network addresses are blocked")
    return ips[0]


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port=port, timeout=30)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class PinnedHTTPS(PinnedHTTP):
    def connect(self):
        super().connect()
        self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


class Response:
    def __init__(self, response, connection, url):
        self.response = response
        self.connection = connection
        self.url = url
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.getheaders()}

    def chunks(self):
        yield from iter(lambda: self.response.read(64 * 1024), b"")

    def read_bounded(self, limit):
        parts = []
        total = 0
        for chunk in self.chunks():
            total += len(chunk)
            if total > limit:
                raise PolicyError("Response exceeds parsing limit")
            parts.append(chunk)
        return b"".join(parts)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.response.close()
        self.connection.close()


class PublicHTTP:
    def __init__(self, config):
        self.config = config
        self.robots = {}
        self.last_request = {}

    def request(self, url, headers=None, allowed_hosts=None, method="GET", body=None):
        """Low-level bounded request; provider calls use separate fixed host policy."""
        original_host = urlsplit(url).hostname
        for _ in range(6):
            url = canonical_url(url)
            parts = urlsplit(url)
            allowed = self.config.allowed_hosts if allowed_hosts is None else allowed_hosts
            if parts.hostname not in allowed:
                raise PolicyError("Redirect or URL host is not explicitly allowed")
            # Never carry provider secrets or POST bodies across redirects.
            if parts.hostname != original_host and (headers or body):
                raise PolicyError("Authenticated cross-host redirects are blocked")
            delay = self.config.request_delay_seconds - (time.monotonic() - self.last_request.get(parts.hostname, 0))
            if delay > 0:
                time.sleep(delay)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            address = public_address(parts.hostname, port)
            cls = PinnedHTTPS if parts.scheme == "https" else PinnedHTTP
            connection = cls(parts.hostname, port, address)
            request_headers = {"User-Agent": self.config.user_agent, "Accept-Encoding": "identity"}
            request_headers.update(headers or {})
            try:
                connection.request(method, parts.path + ("?" + parts.query if parts.query else ""), body=body, headers=request_headers)
                raw = connection.getresponse()
                self.last_request[parts.hostname] = time.monotonic()
                response = Response(raw, connection, url)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    response.__exit__()
                    if not location or method != "GET" or headers:
                        raise PolicyError("Provider redirects and missing redirect targets are blocked")
                    url = urljoin(url, location)
                    continue
                return response
            except BaseException:
                connection.close()
                raise
        raise PolicyError("Too many redirects")

    def allowed_by_robots(self, url):
        parts = urlsplit(url)
        origin = parts.scheme + "://" + parts.netloc
        if origin not in self.robots:
            with self.request(origin + "/robots.txt") as response:
                parser = RobotFileParser()
                if response.status == 404:
                    parser.parse([])
                elif response.status == 200:
                    parser.parse(response.read_bounded(1024 * 1024).decode("utf-8", errors="replace").splitlines())
                else:
                    raise PolicyError("Cannot verify robots policy; access is blocked")
                self.robots[origin] = parser
        parser = self.robots[origin]
        crawl_delay = parser.crawl_delay(self.config.user_agent) or parser.crawl_delay("*") or 0
        if crawl_delay:
            elapsed = time.monotonic() - self.last_request.get(parts.hostname, 0)
            if elapsed < crawl_delay:
                time.sleep(crawl_delay - elapsed)
        return parser.can_fetch(self.config.user_agent, url)

    @contextlib.contextmanager
    def open(self, url):
        url = self.config.permit(url)
        if not self.allowed_by_robots(url):
            raise PolicyError("Source robots policy disallows this URL")
        with self.request(url) as response:
            if response.url != url and not self.allowed_by_robots(response.url):
                raise PolicyError("Redirect target robots policy disallows this URL")
            if response.status != 200:
                raise PolicyError("Source did not return HTTP 200")
            length = response.headers.get("content-length")
            if length and int(length) > self.config.max_file_bytes:
                raise PolicyError("Source exceeds per-file size limit")
            yield response
