#!/usr/bin/env python3
"""Synthetic real Chromium + namespace gate. No live source collection.

The fixture substitutes only the trusted broker's transport. There is no
production localhost switch. The omission probe uses real browser networking
WITHOUT interception and must not reach a host-namespace TCP sentinel.
"""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from election26.archive import Archive, gc_plan, status
from election26.browser import BrowserLimits, capture_browser, runtime_command
from election26.config import Config
from election26.network import Response

URL = 'https://fixture.example/index'
HTML = b'''<!doctype html><html><head><script src="/app.js"></script></head>
<body><p id="result">original</p><script type="application/json">{"embedded":"original"}</script></body></html>'''
JS = b'''addEventListener('DOMContentLoaded', async () => {
 const data = await (await fetch('/data.json')).json();
 document.getElementById('result').textContent = data.message;
 const block = document.createElement('script'); block.type = 'application/json';
 block.textContent = JSON.stringify({embedded: 'rendered'}); document.body.append(block);
});'''


class Raw:
    status = 200
    def __init__(self, body, mime):
        self.body = io.BytesIO(body)
        self.mime = mime
    def getheaders(self):
        return [('Content-Type', self.mime), ('Set-Cookie', 'must-not-leave-broker=fixture')]
    def read(self, n):
        return self.body.read(n)
    def close(self):
        pass


class SyntheticHTTP:
    def __init__(self, config, **kwargs):
        self.config = config
    @contextlib.contextmanager
    def open(self, url):
        fixtures = {URL: (HTML, 'text/html'), 'https://fixture.example/app.js': (JS, 'application/javascript'),
                    'https://fixture.example/data.json': (b'{"message":"rendered by JSON"}', 'application/json')}
        assert url in fixtures, 'unexpected synthetic URL'
        body, mime = fixtures[url]
        yield Response(Raw(body, mime), None, url)


def main(runtime):
    limits = BrowserLimits(seconds=30)
    command, spec = runtime_command(runtime, limits)
    with tempfile.TemporaryDirectory(prefix='election26-browser-gate-') as tmp:
        root = Path(tmp)
        config = Config(allowed_hosts=['fixture.example'], min_free_bytes=0, request_delay_seconds=0)
        # A real outside-netns sentinel; the browser has no matching loopback service.
        with socket.socket() as sentinel:
            sentinel.bind(('127.0.0.1', 0)); sentinel.listen(); sentinel.settimeout(0.1)
            port = sentinel.getsockname()[1]
            secret = root / 'host-only-secret.txt'; secret.write_text('synthetic not-mounted sentinel')
            probe = f'''
import os, sys, json
sys.path.insert(0, '/app/vendor')
from playwright.sync_api import sync_playwright
assert os.readlink('/proc/self/ns/net') != {os.readlink('/proc/self/ns/net')!r}
assert not os.path.exists({str(secret)!r})
assert 'ELECTION26_HOST_ONLY' not in os.environ
with sync_playwright() as p:
 b = p.chromium.launch(executable_path={spec['chromium']!r}, headless=True, chromium_sandbox=True)
 page = b.new_page()
 failed = False
 try:
  page.goto('http://127.0.0.1:{port}/sentinel', timeout=3000)
 except Exception:
  failed = True
 assert failed, 'browser escaped isolated network namespace'
 page.goto('chrome://sandbox')
 summary = page.locator('body').inner_text()
 assert 'Seccomp-BPF' in summary and 'Yes' in summary, 'Chromium sandbox evidence missing'
 b.close()
print(json.dumps({{'outside_sentinel_unreachable': True, 'host_files_hidden': True, 'host_env_hidden': True, 'chromium_sandbox': summary}}))
'''
            proc = subprocess.run(command[:-1] + ['-c', probe], capture_output=True, text=True, timeout=20,
                                  env={'PATH': os.environ['PATH'], 'ELECTION26_HOST_ONLY': 'fixture'})
            if proc.returncode:
                # Only trusted runtime diagnostics, no acquired content or credentials.
                print(proc.stderr[-3000:], file=sys.stderr)
                raise AssertionError('Mandatory namespace/browser probe failed; no fallback permitted')
            try:
                sentinel.accept()
            except socket.timeout:
                pass
            else:
                raise AssertionError('Host sentinel was reached')
            print(proc.stdout)
        with patch('election26.browser.PublicHTTP', SyntheticHTTP):
            result = capture_browser(root/'archive', config, URL, runtime, limits)
        assert result['state'] == 'complete', result
        roles = [a['role'] for a in result['artifacts']]
        assert 'observed_json_response' in roles and 'embedded_json_original' in roles and 'embedded_json_rendered' in roles
        with Archive(root/'archive', config).writer() as archive:
            first = next(a for a in result['artifacts'] if a['role'] == 'original_response')
            assert archive.object_path(first['sha256']).read_bytes() == HTML
            dom = next(a for a in result['artifacts'] if a['role'] == 'rendered_dom')
            assert b'rendered by JSON' in archive.object_path(dom['sha256']).read_bytes()
            assert gc_plan(archive)['orphan_objects'] == []
            assert all(a['integrity'] == 'verified' for a in result['artifacts'])
        # Real renderer DOM limit: completed originals remain when extraction fails.
        with patch('election26.browser.PublicHTTP', SyntheticHTTP):
            partial = capture_browser(root/'small-dom', config, URL, runtime, BrowserLimits(seconds=30, dom_bytes=10))
        assert partial['state'] == 'partial'
        assert any(a['role'] == 'original_response' for a in partial['artifacts'])
        assert not any(a['role'] == 'rendered_dom' for a in partial['artifacts'])
        print(json.dumps({'secure_browser_fixture': 'passed', 'original_unchanged': True,
                          'javascript_and_json_rendered': True, 'derived_relationships': len(roles),
                          'oversize_dom_retains_original': True}))


if __name__ == '__main__':
    main(sys.argv[1])
