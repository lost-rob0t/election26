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
from election26.browser_resources import envelope
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
    def __init__(self, body, mime, policy=None):
        self.body = io.BytesIO(body)
        self.mime = mime
        self.policy = policy
    def getheaders(self):
        headers = [('Content-Type', self.mime), ('Set-Cookie', 'must-not-leave-broker=fixture')]
        if self.policy:
            headers.append(('Content-Security-Policy', self.policy))
        return headers
    def read(self, n):
        return self.body.read(n)
    def close(self):
        pass


class SourceCSPHTTP:
    def __init__(self, config, **kwargs):
        pass
    @contextlib.contextmanager
    def open(self, url):
        assert url == URL
        body = b'<html><p id="result">original</p><script>document.getElementById("result").textContent="forbidden";</script></html>'
        yield Response(Raw(body, 'text/html', "script-src 'none'"), None, url)


class MemoryPressureHTTP:
    def __init__(self, config, **kwargs):
        pass
    @contextlib.contextmanager
    def open(self, url):
        assert url == URL
        body = b'<html><script>const hold=[]; while(true) {const a=new Uint8Array(16777216); a.fill(7); hold.push(a);}</script></html>'
        yield Response(Raw(body, 'text/html'), None, url)


def memory_events():
    group = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    return dict(line.split() for line in (Path('/sys/fs/cgroup')/group.lstrip('/')/'memory.events').read_text().splitlines())


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


# Exercise the strongest unprivileged remount route available to a compromised
# browser process. No host mount or resource setting is changed by this probe.
CGROUP_PROBE = r'''
import ctypes, errno, json, os
from pathlib import Path
libc = ctypes.CDLL(None, use_errno=True)
libc.unshare.argtypes = [ctypes.c_int]
libc.unshare.restype = ctypes.c_int
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
libc.mount.restype = ctypes.c_int
def checked(result):
 if result != 0:
  code = ctypes.get_errno()
  raise OSError(code, os.strerror(code))
assert Path('/proc/self/cgroup').read_text().strip() == '0::/'
outer = os.readlink('/proc/self/ns/cgroup')
uid, gid = os.getuid(), os.getgid()
checked(libc.unshare(0x10000000))  # CLONE_NEWUSER, then map this ordinary user.
Path('/proc/self/uid_map').write_text('0 %d 1' % uid)
Path('/proc/self/setgroups').write_text('deny')
Path('/proc/self/gid_map').write_text('0 %d 1' % gid)
checked(libc.unshare(0x00020000 | 0x02000000))  # NEWNS | NEWCGROUP
assert os.readlink('/proc/self/ns/cgroup') != outer
assert Path('/proc/self/cgroup').read_text().strip() == '0::/'
mount = Path('/tmp/nested-cgroup'); mount.mkdir()
checked(libc.mount(b'cgroup2', bytes(mount), b'cgroup2', 2 | 4 | 8, None))
# Omitting nsdelegate here cannot turn off the system-wide option from a child
# namespace. The ancestor is outside this mount and cannot be a migration target.
assert not (mount/'..'/'cgroup.procs').exists()
denied = {}
for name in ['memory.max', 'memory.swap.max', 'pids.max']:
 file = mount/name
 value = file.read_text().strip()
 try:
  file.write_text(value)
 except OSError as exc:
  assert exc.errno in (errno.EPERM, errno.EACCES), (name, exc.errno)
  denied[name] = exc.errno
 else:
  raise AssertionError('Namespace root controller was writable: ' + name)
 assert file.read_text().strip() == value
print(json.dumps({'nested_cgroup_remount': True, 'root_cap_writes_denied': denied,
                  'outside_cgroup_paths_absent': True}), flush=True)
'''


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
assert os.readlink('/proc/self/ns/cgroup') != {os.readlink('/proc/self/ns/cgroup')!r}
assert open('/proc/self/cgroup').read().strip() == '0::/'
assert not os.path.exists({str(secret)!r})
assert 'ELECTION26_HOST_ONLY' not in os.environ
assert not os.path.exists('/sys/fs/cgroup'), 'renderer must not migrate cgroups'
with sync_playwright() as p:
 b = p.chromium.launch(executable_path={spec['chromium']!r}, headless=True, chromium_sandbox=True)
 page = b.new_page()
 failed = False
 try:
  page.goto('http://127.0.0.1:{port}/sentinel', timeout=3000)
 except Exception:
  failed = True
 assert failed, 'browser escaped isolated network namespace'
 page.close()
 page = b.new_page()
 page.goto('chrome://sandbox')
 page.wait_for_function("document.querySelectorAll('#sandbox-status tr').length > 0", timeout=3000)
 summary = page.locator('body').inner_text()
 rows = dict(page.locator('#sandbox-status tr').evaluate_all('(rows) => rows.map(r => Array.from(r.cells, c => c.textContent))'))
 print(json.dumps({{'sandbox_rows': rows}}), flush=True)
 assert rows.get('Seccomp-BPF sandbox') == 'Yes', 'Chromium seccomp sandbox evidence missing'
 assert rows.get('Layer 1 Sandbox') == 'Namespace', 'Chromium namespace sandbox evidence missing'
 from pathlib import Path
 renderer_status=[]
 for directory in Path('/proc').glob('[0-9]*'):
  try:
   arguments = (directory/'cmdline').read_bytes().replace(bytes([0]), b' ').split()
   if not arguments or arguments[0] != {spec['chromium'].encode()!r}:
    continue
   print(json.dumps({{'chromium_process_args': [a.decode(errors='replace')[:120] for a in arguments[:6]]}}), flush=True)
   if b'--type=renderer' not in arguments:
    continue
   fields=dict(line.split(':',1) for line in (directory/'status').read_text().splitlines() if ':' in line)
   assert fields.get('Seccomp','').strip() == '2'
   assert fields.get('NoNewPrivs','').strip() == '1'
   renderer_status.append({{'seccomp':2,'no_new_privs':1}})
  except (FileNotFoundError, PermissionError):
   continue
 assert renderer_status, 'No actual sandboxed renderer kernel evidence'
 print(json.dumps({{'renderer_kernel_status': renderer_status}}), flush=True)
 b.close()
print(json.dumps({{'outside_sentinel_unreachable': True, 'host_files_hidden': True, 'host_env_hidden': True, 'chromium_sandbox': summary}}))
'''
            proc = subprocess.run(command[:-1] + ['-c', probe], capture_output=True, text=True, timeout=20,
                                  env={'PATH': os.environ['PATH'], 'ELECTION26_HOST_ONLY': 'fixture'})
            if proc.returncode:
                # Only trusted runtime diagnostics, no acquired content or credentials.
                print(proc.stdout[-3000:], file=sys.stderr)
                print(proc.stderr[-3000:], file=sys.stderr)
                raise AssertionError('Mandatory namespace/browser probe failed; no fallback permitted')
            try:
                sentinel.accept()
            except socket.timeout:
                pass
            else:
                raise AssertionError('Host sentinel was reached')
            print(proc.stdout)
        before_caps = envelope()
        nested = subprocess.run(command[:-1] + ['-c', CGROUP_PROBE], capture_output=True, text=True, timeout=15,
                                env={'PATH': os.environ['PATH']})
        assert nested.returncode == 0, nested.stderr[-2000:]
        assert envelope() == before_caps
        print(nested.stdout)
        # Exercise the kernel pids controller, not a mocked process-count check.
        task_probe = '''
import os, signal, json, errno
children=[]
limited=False
try:
 for _ in range(300):
  try:
   pid=os.fork()
  except OSError as e:
   assert e.errno == errno.EAGAIN
   limited=True
   break
  if pid == 0:
   signal.pause()
   os._exit(0)
  children.append(pid)
 assert limited and len(children) < 256
 print(json.dumps({'kernel_tasks_limit': True, 'children_before_denial': len(children)}), flush=True)
finally:
 for pid in children:
  os.kill(pid, signal.SIGKILL)
 for pid in children:
  os.waitpid(pid, 0)
'''
        task_result = subprocess.run(command[:-1] + ['-c', task_probe], capture_output=True, text=True, timeout=15,
                                     env={'PATH': os.environ['PATH']})
        assert task_result.returncode == 0, task_result.stderr[-1000:]
        print(task_result.stdout)
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
        # Source restrictions must stay effective after broker fulfillment.
        with patch('election26.browser.PublicHTTP', SourceCSPHTTP):
            restricted = capture_browser(root/'source-csp', config, URL, runtime, BrowserLimits(seconds=15))
        assert restricted['state'] == 'complete'
        with Archive(root/'source-csp', config).writer() as archive:
            dom = next(a for a in restricted['artifacts'] if a['role'] == 'rendered_dom')
            assert b'<p id="result">original</p>' in archive.object_path(dom['sha256']).read_bytes()
            original = next(a for a in restricted['artifacts'] if a['role'] == 'original_response')
            assert original['metadata']['source_content_security_policy'] == "script-src 'none'"
        # ArrayBuffer allocations are outside the V8 old-space heap cap. Prove
        # the cgroup kills their renderer while committed originals survive.
        before_oom = int(memory_events()['oom_kill'])
        with patch('election26.browser.PublicHTTP', MemoryPressureHTTP):
            pressured = capture_browser(root/'memory-pressure', config, URL, runtime, BrowserLimits(seconds=15))
        assert pressured['state'] == 'partial'
        assert any(a['role'] == 'original_response' for a in pressured['artifacts'])
        assert int(memory_events()['oom_kill']) > before_oom, 'No kernel memory enforcement evidence'
        print(json.dumps({'secure_browser_fixture': 'passed', 'original_unchanged': True,
                          'javascript_and_json_rendered': True, 'derived_relationships': len(roles),
                          'oversize_dom_retains_original': True, 'kernel_memory_limit': True, 'kernel_tasks_limit': True, 'source_csp_enforced': True}))


if __name__ == '__main__':
    main(sys.argv[1])
