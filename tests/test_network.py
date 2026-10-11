import io
import socket
import unittest
from unittest.mock import patch

from election26.config import Config, PolicyError, canonical_url
from election26.network import PublicHTTP, Response, public_address


class Raw:
    def __init__(self, code=200, body=b"fixture", headers=None):
        self.status = code
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def getheaders(self):
        return list(self.headers.items())

    def read(self, amount):
        return self.body.read(amount)

    def close(self):
        pass


class Connection:
    responses = []
    calls = []

    def __init__(self, host, port, address):
        self.host = host
        self.address = address

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.host, path, method, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        pass


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(allowed_hosts=["news.example", "media.example"], request_delay_seconds=0)
        self.client = PublicHTTP(self.config)
        Connection.calls = []
        Connection.responses = []

    def test_nonpublic_addresses_rejected(self):
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"]:
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]):
                self.assertRaises(PolicyError, public_address, "news.example", 443)

    def test_mixed_public_private_dns_rejected(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]):
            self.assertRaises(PolicyError, public_address, "news.example", 443)

    def test_urls_without_credentials_and_exact_allowlist(self):
        self.assertEqual(canonical_url("https://NEWS.example/a#section"), "https://news.example/a")
        for url in ["file:///etc/passwd", "https://u:p@news.example/a", "https://news.example/?api_key=secret", "https://news.example:8443/a"]:
            self.assertRaises(PolicyError, canonical_url, url)
        self.assertRaises(PolicyError, self.config.permit, "https://news.example.attacker.test")

    @patch("election26.network.public_address", return_value="8.8.8.8")
    @patch("election26.network.PinnedHTTPS", Connection)
    def test_disallowed_redirect_blocked_before_connect(self, _):
        Connection.responses = [Raw(302, headers={"Location": "https://evil.example/x"})]
        with self.assertRaises(PolicyError):
            self.client.request("https://news.example/a")
        self.assertEqual(len(Connection.calls), 1)

    @patch("election26.network.public_address", return_value="8.8.8.8")
    @patch("election26.network.PinnedHTTPS", Connection)
    def test_redirect_robots_checked_before_target_body(self, _):
        Connection.responses = [Raw(404), Raw(302, headers={"Location": "https://media.example/private"}),
                                Raw(200, b"User-agent: *\nDisallow: /private")]
        with self.assertRaises(PolicyError):
            with self.client.open("https://news.example/a"):
                pass
        self.assertNotIn(("media.example", "/private"), [(h, p) for h, p, _, _ in Connection.calls])

    @patch("election26.network.public_address", return_value="8.8.8.8")
    @patch("election26.network.PinnedHTTPS", Connection)
    def test_provider_redirect_never_forwards_secret(self, _):
        Connection.responses = [Raw(302, headers={"Location": "https://media.example/steal"})]
        with self.assertRaises(PolicyError):
            self.client.request("https://news.example/search", headers={"Authorization": "secret"})
        self.assertEqual(len(Connection.calls), 1)

    def test_truncated_content_never_looks_complete(self):
        response = Response(Raw(body=b"short", headers={"Content-Length": "100"}), None, "https://news.example/a")
        self.assertRaises(PolicyError, lambda: list(response.chunks()))

    def test_transfer_deadline(self):
        response = Response(Raw(), None, "https://news.example/a")
        with patch("election26.network.time.monotonic", side_effect=[0, 301]):
            self.assertRaises(PolicyError, lambda: list(response.chunks()))
