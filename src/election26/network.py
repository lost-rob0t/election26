"""Public-address-only HTTP with exact host allowlists and bounded redirects."""
import contextlib
import http.client
import ipaddress
import socket
import ssl
import time
import threading
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser

from .config import PolicyError, BudgetError, canonical_url


class HTTPStatusError(PolicyError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def public_address(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise PolicyError("Host has no usable address")
    # Reject mixed public/private answers too; pin the checked address at connect.
    ips = [info[4][0] for info in addresses]
    if not all(ipaddress.ip_address(ip).is_global and not ipaddress.ip_address(ip).is_multicast
               and not ipaddress.ip_address(ip).is_reserved for ip in ips):
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


class RequestDeadline:
    """Interrupt socket reads, including headers and slow-drip bodies."""
    def __init__(self, connection, seconds=300):
        self.connection = connection
        self.sock = None
        self.expired = False
        self.timer = threading.Timer(seconds, self.expire)
        self.timer.daemon = True
        self.timer.start()

    def expire(self):
        self.expired = True
        sock = self.sock or getattr(self.connection, "sock", None)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def cancel(self):
        self.timer.cancel()


class Response:
    def __init__(self, response, connection, url, deadline=None):
        self.response = response
        self.connection = connection
        self.deadline = deadline
        self.url = url
        self.status = response.status
        raw_headers = response.getheaders()
        self.headers = {k.lower(): v for k, v in raw_headers}
        # Multiple enforcing CSP fields are an intersection, not last-wins.
        policies = [value for name, value in raw_headers if name.lower() == 'content-security-policy']
        if policies:
            self.headers['content-security-policy'] = ', '.join(policies)

    def chunks(self):
        total = 0
        deadline = time.monotonic() + 300
        expected = self.headers.get("content-length")
        expected = int(expected) if expected else None
        while True:
            if time.monotonic() > deadline:
                raise PolicyError("Response exceeded the five-minute transfer deadline")
            chunk = self.response.read(64 * 1024)
            if self.deadline and self.deadline.expired:
                raise PolicyError("Response exceeded the five-minute per-hop deadline")
            if not chunk:
                break
            total += len(chunk)
            yield chunk
        if expected is not None and total != expected:
            raise PolicyError("Response length does not match Content-Length")

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
        if self.deadline:
            self.deadline.cancel()
        self.response.close()
        self.connection.close()


class PublicHTTP:
    def __init__(self, config, *, reject_redirects=False):
        self.config = config
        self.reject_redirects = reject_redirects
        self.robots = {}
        self.last_request = {}
        self.request_count = 0
        self.run_deadline = time.monotonic() + config.max_run_seconds

    def request(self, url, headers=None, allowed_hosts=None, method="GET", body=None, before_request=None):
        """Low-level bounded request; provider calls use separate fixed host policy."""
        original_host = urlsplit(url).hostname
        for _ in range(6):
            url = canonical_url(url)
            parts = urlsplit(url)
            allowed = self.config.allowed_hosts if allowed_hosts is None else allowed_hosts
            if parts.hostname not in allowed:
                raise PolicyError("Redirect or URL host is not explicitly allowed")
            if before_request:
                before_request(url)
            # Never carry provider secrets or POST bodies across redirects.
            if parts.hostname != original_host and (headers or body):
                raise PolicyError("Authenticated cross-host redirects are blocked")
            remaining = self.run_deadline - time.monotonic()
            if self.request_count >= self.config.max_requests or remaining <= 0:
                raise BudgetError("Run request/time budget exhausted; resume on the next invocation")
            delay = self.config.request_delay_seconds - (time.monotonic() - self.last_request.get(parts.hostname, 0))
            if delay >= remaining:
                raise BudgetError("Run time budget cannot admit another delayed request")
            if delay > 0:
                time.sleep(delay)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            address = public_address(parts.hostname, port)
            cls = PinnedHTTPS if parts.scheme == "https" else PinnedHTTP
            connection = cls(parts.hostname, port, address)
            remaining = self.run_deadline - time.monotonic()
            if remaining <= 0:
                raise BudgetError("Run time budget exhausted during host resolution")
            deadline = RequestDeadline(connection, seconds=min(300, remaining))
            self.request_count += 1
            request_headers = {"User-Agent": self.config.user_agent, "Accept-Encoding": "identity"}
            request_headers.update(headers or {})
            try:
                connection.request(method, parts.path + ("?" + parts.query if parts.query else ""), body=body, headers=request_headers)
                deadline.sock = getattr(connection, "sock", None)
                raw = connection.getresponse()
                if deadline.expired:
                    raise PolicyError("Request exceeded the five-minute per-hop deadline")
                self.last_request[parts.hostname] = time.monotonic()
                response = Response(raw, connection, url, deadline)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    response.__exit__()
                    if self.reject_redirects:
                        raise HTTPStatusError(response.status, "Browser redirects are not supported")
                    if not location or method != "GET" or headers:
                        raise PolicyError("Provider redirects and missing redirect targets are blocked")
                    url = urljoin(url, location)
                    continue
                return response
            except BaseException:
                deadline.cancel()
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
        if crawl_delay > 60:
            raise PolicyError("Robots crawl delay exceeds the bounded worker wait")
        if crawl_delay:
            elapsed = time.monotonic() - self.last_request.get(parts.hostname, 0)
            if elapsed < crawl_delay:
                if crawl_delay - elapsed >= self.run_deadline - time.monotonic():
                    raise BudgetError("Robots delay exceeds remaining run budget")
                time.sleep(crawl_delay - elapsed)
        return parser.can_fetch(self.config.user_agent, url)

    @contextlib.contextmanager
    def open(self, url):
        url = self.config.permit(url)
        def require_robots(target):
            if not self.allowed_by_robots(target):
                raise PolicyError("Source robots policy disallows this URL")
        with self.request(url, before_request=require_robots) as response:
            if response.status != 200:
                raise HTTPStatusError(response.status, "Source did not return HTTP 200")
            length = response.headers.get("content-length")
            if length and int(length) > self.config.max_file_bytes:
                raise PolicyError("Source exceeds per-file size limit")
            yield response
