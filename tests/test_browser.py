import base64
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from election26.archive import Archive, gc_plan, status
from election26 import browser_catalog as catalog
from election26.browser import Broker, BrowserLimits, EmbeddedJSON, IPCReader, runtime_command, capture_browser
from election26.config import Config, PolicyError, BudgetError
from election26.network import PublicHTTP, Response
from test_network import Connection, Raw

HTML = b'<html><body>original<script type="application/json">{"fixture":1}</script></body></html>'
DOM = b'<html><body>rendered<script type="application/json">{"fixture":2}</script></body></html>'
URL = 'https://fixture.example/index'


class FixtureClient:
    def __init__(self):
        self.calls = []
        self.bodies = {URL: ('text/html', HTML), 'https://fixture.example/api': ('application/json', b'{"value":2}')}

    @contextlib.contextmanager
    def open(self, url):
        self.calls.append(url)
        mime, body = self.bodies[url]
        yield Response(Raw(body=body, headers={'Content-Type': mime, 'Set-Cookie': 'secret', 'Last-Modified': 'source header'}), None, url)


def request(url=URL, initial=True, kind='document', **extra):
    return dict(op='request', url=url, method='GET', type=kind, initial=initial, **extra)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        guard = patch('election26.archive.inside_git_checkout', return_value=False)
        guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'archive'
        self.config = Config(allowed_hosts=['fixture.example'], min_free_bytes=0, request_delay_seconds=0)
        self.archive = Archive(self.root, self.config)
        self.writer = self.archive.writer(); self.writer.__enter__(); self.addCleanup(self.writer.__exit__, None, None, None)
        self.limits = BrowserLimits()
        self.session = catalog.start(self.archive, URL, {})
        self.client = FixtureClient()
        self.broker = Broker(self.archive, self.session, URL, self.limits, self.client)

    def test_distinct_original_dom_json_and_embedded_relationships(self):
        answer = self.broker.request(request())
        self.assertEqual(base64.b64decode(answer['body']), HTML)
        self.assertNotIn('set-cookie', answer['headers'])
        self.broker.request(request('https://fixture.example/api', False, 'fetch'))
        self.broker.receive_dom({'op': 'dom', 'body': base64.b64encode(DOM).decode()})
        manifest = catalog.finish(self.archive, self.session, 'complete')
        roles = [x['role'] for x in manifest['artifacts']]
        self.assertEqual(roles, ['original_response', 'embedded_json_original', 'original_response',
                                 'observed_json_response', 'rendered_dom', 'embedded_json_rendered'])
        originals = [x for x in manifest['artifacts'] if x['role'] == 'original_response']
        self.assertEqual(self.archive.object_path(originals[0]['sha256']).read_bytes(), HTML)
        self.assertEqual(len(manifest['requests']), 2)
        self.assertTrue(all(x['integrity'] == 'verified' for x in manifest['artifacts']))
        self.assertTrue(all(x['requested_at'] and x['finished_at'] for x in manifest['requests']))
        self.assertIsNone(manifest['source_time'])
        self.assertEqual(gc_plan(self.archive)['orphan_objects'], [])
        stats = status(self.root)
        self.assertEqual(stats['browser_sessions'], 1)
        self.assertEqual(stats['browser_artifacts'], 6)
        # Repair and backup retain every relationship and derived object.
        path = self.root / 'manifests' / ('browser-' + str(self.session) + '.json')
        path.unlink(); self.archive.repair_manifests()
        self.assertEqual(json.loads(path.read_text())['artifacts'], manifest['artifacts'])
        backup = Path(self.temp.name) / 'backup'
        self.archive.backup(backup)
        self.assertEqual(status(backup)['browser_artifacts'], 6)

    def test_original_relation_is_in_capture_transaction(self):
        with patch('election26.browser_catalog.original', side_effect=RuntimeError('synthetic crash')):
            self.assertEqual(self.broker.request(request()), {'state': 'blocked'})
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 0)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM browser_artifacts').fetchone()[0], 0)

    def test_media_links_first_no_connection_then_optin(self):
        self.broker.request(request())
        url = 'https://fixture.example/picture.png'
        self.client.bodies[url] = ('image/png', b'fixture image')
        self.assertEqual(self.broker.request(request(url, False, 'image')), {'state': 'blocked'})
        self.assertNotIn(url, self.client.calls)
        self.config.media_policies = {'fixture.example': {'download': True, 'types': ['image'],
            'max_file_bytes': 1000, 'max_total_bytes': 1000, 'min_free_bytes': 0}}
        self.assertEqual(self.broker.request(request(url, False, 'image'))['state'], 'fulfilled')

    def test_no_headers_post_frames_extra_document_or_disallowed_hosts(self):
        for msg in [request(headers={'Authorization': 'secret'}), dict(request(), method='POST'),
                    request('https://denied.example'), request(URL, False, 'document'), request(URL, False, 'eventsource')]:
            with self.subTest(msg=msg), self.assertRaises(PolicyError):
                self.broker.request(msg)
        self.assertEqual(self.client.calls, [])

    def test_limits_body_dom_requests_and_embedded(self):
        broker = Broker(self.archive, self.session, URL, BrowserLimits(body_bytes=8), self.client)
        self.assertEqual(broker.request(request()), {'state': 'blocked'})
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 0)
        self.broker.request(request())
        self.broker.limits = BrowserLimits(dom_bytes=2)
        with self.assertRaises(PolicyError):
            self.broker.receive_dom({'op': 'dom', 'body': base64.b64encode(DOM).decode()})
        self.broker.limits = BrowserLimits(requests=1)
        with self.assertRaises(BudgetError):
            self.broker.request(request('https://fixture.example/api', False, 'fetch'))
        parser = EmbeddedJSON(BrowserLimits(json_bytes=4, json_blocks=1))
        parser.feed('<script type="application/json">[1]</script><script type="application/json">[2]</script>')
        self.assertEqual(parser.blocks, [b'[1]']); self.assertEqual(parser.omitted, 1)

    def test_original_survives_renderer_and_disk_failure(self):
        self.broker.request(request())
        original = self.archive.db.execute('SELECT sha256 FROM captures').fetchone()[0]
        with patch.object(self.archive, 'store', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                self.broker.receive_dom({'op': 'dom', 'body': base64.b64encode(DOM).decode()})
        manifest = catalog.finish(self.archive, self.session, 'partial', 'disk_failure')
        self.assertEqual(self.archive.object_path(original).read_bytes(), HTML)
        self.assertEqual(manifest['state'], 'partial')
        self.assertNotIn('rendered_dom', [a['role'] for a in manifest['artifacts']])

    def test_missing_derived_blob_is_truthful(self):
        self.broker.request(request())
        self.broker.receive_dom({'op': 'dom', 'body': base64.b64encode(DOM).decode()})
        row = self.archive.db.execute("SELECT sha256 FROM browser_artifacts WHERE role='rendered_dom'").fetchone()
        self.archive.object_path(row[0]).unlink()
        items = catalog.manifest(self.archive, self.session)['artifacts']
        self.assertEqual(next(a for a in items if a['role'] == 'rendered_dom')['integrity'], 'missing_or_corrupt')

    def test_redirect_robots_and_private_dns_never_connect_to_target(self):
        Connection.calls, Connection.responses = [], [Raw(404), Raw(302, headers={'Location': 'https://fixture.example/target'})]
        self.broker.client = PublicHTTP(self.config, reject_redirects=True)
        with patch('election26.network.PinnedHTTPS', Connection), patch('election26.network.public_address', return_value='8.8.8.8'):
            self.assertEqual(self.broker.request(request()), {'state': 'blocked'})
        self.assertNotIn('/target', [x[1] for x in Connection.calls])
        Connection.calls, Connection.responses = [], [Raw(200, b'User-agent: *\nDisallow: /index')]
        self.broker.client = PublicHTTP(self.config, reject_redirects=True)
        with patch('election26.network.PinnedHTTPS', Connection), patch('election26.network.public_address', return_value='8.8.8.8'):
            self.broker.request(request())
        self.assertNotIn('/index', [x[1] for x in Connection.calls])
        Connection.calls = []
        self.broker.client = PublicHTTP(self.config, reject_redirects=True)
        with patch('election26.network.PinnedHTTPS', Connection), patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            self.broker.request(request())
        self.assertEqual(Connection.calls, [])

    def test_fixed_headers_only_and_javascript_mime_admitted(self):
        Connection.calls, Connection.responses = [], [Raw(404), Raw(200, HTML, {'Content-Type': 'text/html', 'Set-Cookie': 'secret'})]
        self.broker.client = PublicHTTP(self.config, reject_redirects=True)
        with patch('election26.network.PinnedHTTPS', Connection), patch('election26.network.public_address', return_value='8.8.8.8'):
            answer = self.broker.request(request())
        self.assertEqual(set(Connection.calls[-1][3]), {'User-Agent', 'Accept-Encoding'})
        self.assertNotIn('Set-Cookie', str(answer))
        from election26.media import content_kind
        self.assertIsNone(content_kind('application/javascript'))
        self.assertEqual(content_kind('application/octet-stream'), 'unknown')


class IPCAndIsolationTests(unittest.TestCase):
    def test_messages_are_bounded_and_coalesced(self):
        r, w = os.pipe()
        with os.fdopen(r, 'rb', buffering=0) as reader, os.fdopen(w, 'wb', buffering=0) as writer:
            writer.write(b'{"op":"one"}\n{"op":"two"}\n')
            ipc = IPCReader(reader)
            self.assertEqual(ipc.read(100, time.monotonic()+1)['op'], 'one')
            self.assertEqual(ipc.read(100, time.monotonic()+1)['op'], 'two')
            writer.write(b'x' * 10)
            with self.assertRaises(PolicyError):
                ipc.read(10, time.monotonic()+1)

    def test_partial_line_deadline(self):
        r, w = os.pipe()
        with os.fdopen(r, 'rb', buffering=0) as reader, os.fdopen(w, 'wb', buffering=0) as writer:
            writer.write(b'{')
            with self.assertRaises(BudgetError):
                IPCReader(reader).read(100, time.monotonic()+0.02)

    def test_unsupported_systems_fail_closed(self):
        for platform in ['darwin', 'win32']:
            with patch('election26.browser.sys.platform', platform), self.assertRaises(PolicyError):
                runtime_command('/missing', BrowserLimits())
        with patch.dict(os.environ, {'ANDROID_ROOT': '/system'}), self.assertRaises(PolicyError):
            runtime_command('/missing', BrowserLimits())

    def test_namespace_flags_have_no_weaker_fallback_or_host_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ['runtime/python', 'runtime/chromium', 'app/renderer.py']:
                p = root/path; p.parent.mkdir(exist_ok=True); p.touch()
            (root/'runtime.json').write_text(json.dumps({'format_version': 1, 'python': '/runtime/python',
                'python_home': '/runtime', 'chromium': '/runtime/chromium', 'public_runtime_only': True}))
            with patch('os.geteuid', return_value=1000), patch('election26.browser.envelope', return_value={}), patch('shutil.which', return_value='/usr/bin/bwrap'), patch('subprocess.run') as proc:
                proc.return_value.returncode = 0
                proc.return_value.stdout = 'bubblewrap 0.12.0'
                cmd, _ = runtime_command(tmp, BrowserLimits())
            for option in ['--unshare-net', '--unshare-user', '--unshare-pid', '--clearenv', '--die-with-parent']:
                self.assertIn(option, cmd)
            self.assertNotIn('--share-net', cmd)
            self.assertNotIn('--unshare-user-try', cmd)
            self.assertNotIn('--no-sandbox', cmd)
            self.assertEqual(cmd.count('--ro-bind'), 1)
            self.assertEqual(cmd[cmd.index('--ro-bind')+1:cmd.index('--ro-bind')+3], [tmp, '/'])

    def test_limits_cannot_be_disabled(self):
        for value in [0, -1, True, float('inf'), 121]:
            with self.assertRaises(PolicyError):
                BrowserLimits(seconds=value)

class ResourceEnvelopeTests(unittest.TestCase):
    def setUp(self):
        from election26.browser_resources import envelope
        self.envelope = envelope
        self.files = {
            '/proc/self/mountinfo': '1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw',
            '/proc/self/cgroup': '0::/scope/leaf\n',
            '/sys/fs/cgroup/scope/leaf/cgroup.procs': str(os.getpid()),
            '/sys/fs/cgroup/scope/leaf/memory.max': 'max',
            '/sys/fs/cgroup/scope/leaf/memory.swap.max': 'max',
            '/sys/fs/cgroup/scope/leaf/pids.max': 'max',
            '/sys/fs/cgroup/scope/memory.max': '1073741824',
            '/sys/fs/cgroup/scope/memory.swap.max': '0',
            '/sys/fs/cgroup/scope/pids.max': '256',
        }
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Path, 'read_text', lambda path, *a, **kw: self.files[str(path)]))
        self.stack.enter_context(patch.object(Path, 'exists', lambda path: str(path) in self.files))
        self.stack.enter_context(patch.object(Path, 'is_dir', return_value=True))
        self.stack.enter_context(patch.object(Path, 'is_symlink', return_value=False))

    def test_effective_ancestor_limits_and_current_membership(self):
        self.assertEqual(self.envelope(), {'memory.max': 1073741824, 'memory.swap.max': 0, 'pids.max': 256})
        self.files['/sys/fs/cgroup/scope/leaf/pids.max'] = '128'
        self.assertEqual(self.envelope()['pids.max'], 128)
        self.files['/sys/fs/cgroup/scope/leaf/cgroup.procs'] = '99999999'
        self.assertRaises(PolicyError, self.envelope)

    def test_missing_unbounded_and_unmapped_envelopes_fail_closed(self):
        for key, value in [('/sys/fs/cgroup/scope/memory.max', 'max'),
                           ('/sys/fs/cgroup/scope/memory.swap.max', '1'),
                           ('/sys/fs/cgroup/scope/pids.max', '257'),
                           ('/proc/self/mountinfo', '1 0 0:1 /container /sys/fs/cgroup rw - cgroup2 cgroup rw')]:
            old = self.files[key]; self.files[key] = value
            self.assertRaises(PolicyError, self.envelope)
            self.files[key] = old


@unittest.skipUnless(sys.platform == 'linux', 'Linux broker lifecycle')
class BrokerLifecycleTests(unittest.TestCase):
    def test_supervisor_bounds_stalled_dns_and_preserves_original(self):
        import textwrap
        # Synthetic protocol process ONLY, not a browser bypass in production.
        protocol = textwrap.dedent('''
            import sys,json
            json.loads(sys.stdin.readline())
            print(json.dumps({'op':'ready'}),flush=True)
            print(json.dumps({'op':'request','url':'https://fixture.example/index','method':'GET','type':'document','initial':True}),flush=True)
            json.loads(sys.stdin.readline())
            print(json.dumps({'op':'request','url':'https://fixture.example/slow','method':'GET','type':'fetch','initial':False}),flush=True)
            json.loads(sys.stdin.readline())
        ''')
        class SlowDNS(FixtureClient):
            @contextlib.contextmanager
            def open(self, url):
                if url.endswith('/slow'):
                    time.sleep(30)
                with super().open(url) as response:
                    yield response
        with tempfile.TemporaryDirectory() as tmp, patch('election26.archive.inside_git_checkout', return_value=False), \
                patch('election26.browser.runtime_command', return_value=([sys.executable,'-u','-c',protocol], {'chromium':'fixture','resource_envelope':{}})), \
                patch('election26.browser.envelope', return_value={}), \
                patch('election26.browser.PublicHTTP', side_effect=lambda *a, **k: SlowDNS()):
            config = Config(allowed_hosts=['fixture.example'], min_free_bytes=0, request_delay_seconds=0)
            start = time.monotonic()
            result = capture_browser(Path(tmp)/'archive', config, URL, '/unused-fixture', BrowserLimits(seconds=1))
            self.assertLess(time.monotonic()-start, 7)
            self.assertEqual(result['state'], 'partial')
            self.assertEqual(result['reason'], 'process_deadline_or_crash')
            self.assertEqual(result['requests'][-1]['state'], 'interrupted')
            self.assertTrue(any(a['role'] == 'original_response' for a in result['artifacts']))

    def test_parent_death_kills_blocked_broker(self):
        import multiprocessing
        import signal
        from election26.browser import bind_parent_lifetime
        receiver, sender = multiprocessing.get_context('fork').Pipe(duplex=False)
        # Use a standalone supervisor process to avoid terminating the test runner.
        def actual_supervisor():
            parent_pid = os.getpid()
            pid = os.fork()
            if pid == 0:
                bind_parent_lifetime(parent_pid)
                sender.send(os.getpid())
                time.sleep(30)
                os._exit(0)
            time.sleep(30)
        process = multiprocessing.get_context('fork').Process(target=actual_supervisor)
        process.start(); sender.close()
        try:
            self.assertTrue(receiver.poll(3))
            broker_pid = receiver.recv()
            process.kill(); process.join(3)
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                path = Path('/proc')/str(broker_pid)/'stat'
                if not path.exists() or path.read_text().split()[2] == 'Z':
                    break
                time.sleep(0.02)
            else:
                self.fail('orphaned broker kept running after supervisor death')
        finally:
            receiver.close()
            if process.is_alive():
                process.kill(); process.join()

    def test_repair_does_not_race_live_lifecycle_but_recovers_dead_owner(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, patch('election26.archive.inside_git_checkout', return_value=False):
            root = Path(tmp)/'archive'
            config = Config(min_free_bytes=0)
            with Archive(root, config).writer() as archive:
                session = catalog.start(archive, URL, {})
                lock = open(root/'.browser.lock', 'a+')
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Models the parent-start/worker-acquire gap while parent is alive.
                catalog.repair(archive)
                self.assertEqual(catalog.manifest(archive, session)['state'], 'running')
                lock.close()
                # No lifecycle owner now; repair can mark a crashed observation.
                catalog.repair(archive)
                recovered = catalog.manifest(archive, session)
                self.assertEqual(recovered['state'], 'partial')
                self.assertEqual(recovered['reason'], 'supervisor_interrupted')
