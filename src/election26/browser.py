"""Opt-in Linux browser capture with an out-of-namespace acquisition broker.

The parent supervises the entire process group, including blocked getaddrinfo.
The browser never has an egress interface, host filesystem, or broker socket.
"""
import base64
import ctypes
import fcntl
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
import json
import multiprocessing
import os
from pathlib import Path
import selectors
import signal
import shutil
import stat
import subprocess
import sys
import time

from .archive import Archive, now
from . import browser_catalog as catalog
from .config import PolicyError, BudgetError
from .media import url_kind
from .network import PublicHTTP
from .browser_resources import envelope


@dataclass(frozen=True)
class BrowserLimits:
    seconds: int = 45
    requests: int = 40
    total_bytes: int = 16 * 1024 * 1024
    body_bytes: int = 2 * 1024 * 1024
    dom_bytes: int = 2 * 1024 * 1024
    dom_nodes: int = 20000
    json_blocks: int = 16
    json_bytes: int = 256 * 1024
    settle_ms: int = 1000
    tmp_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        caps = {'seconds': 120, 'requests': 100, 'total_bytes': 64 * 1024 * 1024,
                'body_bytes': 4 * 1024 * 1024, 'dom_bytes': 4 * 1024 * 1024,
                'dom_nodes': 50000, 'json_blocks': 32, 'json_bytes': 1024 * 1024,
                'settle_ms': 5000, 'tmp_bytes': 512 * 1024 * 1024}
        for name, cap in caps.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise PolicyError('Browser ' + name + ' exceeds its bounded range')

    @property
    def ipc_bytes(self):
        return max(self.body_bytes, self.dom_bytes) * 2 + 16384


def runtime_command(runtime, limits):
    if sys.platform != 'linux' or 'ANDROID_ROOT' in os.environ or os.geteuid() == 0:
        raise PolicyError('Secure browser capture requires non-root Linux; Android/Termux and macOS are unsupported')
    resources = envelope()
    root = Path(runtime).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise PolicyError('Browser runtime must be a dedicated public runtime directory')
    # No symlinks, sockets, devices or FIFOs in the copied public closure.
    for path in root.rglob('*'):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise PolicyError('Browser runtime must contain only regular public runtime files')
    marker = root / 'runtime.json'
    if marker.stat().st_size > 4096:
        raise PolicyError('Oversized browser runtime descriptor')
    spec = json.loads(marker.read_text())
    if set(spec) != {'format_version', 'python', 'python_home', 'chromium', 'public_runtime_only'} or spec['format_version'] != 1 or spec['public_runtime_only'] is not True:
        raise PolicyError('Invalid browser runtime descriptor')
    for name in ('python', 'python_home', 'chromium'):
        path = Path(spec[name])
        if not path.is_absolute() or '..' in path.parts or not (root / str(path).lstrip('/')).exists():
            raise PolicyError('Invalid browser runtime executable path')
    if not (root / 'app/renderer.py').is_file():
        raise PolicyError('Browser renderer is missing from public runtime')
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise PolicyError('Install trusted bubblewrap 0.12 or later for secure browser capture')
    version = subprocess.run([bwrap, '--version'], capture_output=True, text=True, timeout=5)
    try:
        release = tuple(int(v) for v in version.stdout.strip().split()[-1].split('.')[:2])
    except (ValueError, IndexError):
        release = ()
    if version.returncode != 0 or release < (0, 12):
        raise PolicyError('Secure browser capture requires bubblewrap 0.12 or later')
    command = [bwrap, '--unshare-user', '--unshare-net', '--unshare-pid',
               '--unshare-ipc', '--unshare-uts', '--die-with-parent', '--cap-drop', 'ALL',
               '--ro-bind', str(root), '/', '--proc', '/proc', '--dev', '/dev',
               '--size', str(limits.tmp_bytes), '--tmpfs', '/tmp',
               '--size', str(32 * 1024 * 1024), '--tmpfs', '/dev/shm',
               '--clearenv', '--setenv', 'HOME', '/tmp/home', '--setenv', 'TMPDIR', '/tmp',
               '--setenv', 'PATH', '/nonexistent', '--setenv', 'PYTHONHOME', spec['python_home'],
               '--setenv', 'PYTHONDONTWRITEBYTECODE', '1', '--setenv', 'LANG', 'C.UTF-8',
               '--chdir', '/tmp', spec['python'], '/app/renderer.py']
    return command, dict(spec, resource_envelope=resources)


class IPCReader:
    def __init__(self, stream):
        self.stream = stream
        self.pending = bytearray()

    def read(self, limit, deadline):
        # Avoid readline's partial-line stall and preserve coalesced messages.
        with selectors.DefaultSelector() as selector:
            selector.register(self.stream, selectors.EVENT_READ)
            while True:
                end = self.pending.find(b"\n")
                if end >= 0:
                    if end + 1 > limit:
                        raise PolicyError('Renderer IPC message exceeds its limit')
                    line = self.pending[:end + 1]
                    del self.pending[:end + 1]
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise PolicyError('Invalid renderer IPC message')
                    return value
                if len(self.pending) >= limit:
                    raise PolicyError('Renderer IPC message exceeds its limit')
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise BudgetError('Browser IPC deadline exhausted')
                chunk = os.read(self.stream.fileno(), min(65536, limit - len(self.pending)))
                if not chunk:
                    raise PolicyError('Renderer exited before capture completion')
                self.pending.extend(chunk)


def send_message(stream, value, limit):
    data = json.dumps(value, separators=(',', ':')).encode() + b'\n'
    if len(data) > limit:
        raise PolicyError('Broker IPC response exceeds its limit')
    stream.write(data)
    stream.flush()


def reject_json_constant(value):
    raise ValueError('Nonfinite JSON constant')


class EmbeddedJSON(HTMLParser):
    def __init__(self, limits):
        super().__init__(convert_charrefs=False)
        self.limits = limits
        self.blocks = []
        self.active = None
        self.omitted = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        mime = attrs.get('type', '').split(';', 1)[0].strip().lower()
        if tag == 'script' and (mime in {'application/json', 'application/ld+json'} or mime.endswith('+json')):
            self.active = [] if len(self.blocks) < self.limits.json_blocks else None
            if self.active is None:
                self.omitted += 1

    def handle_data(self, data):
        if self.active is not None:
            if sum(len(part.encode()) for part in self.active) + len(data.encode()) > self.limits.json_bytes:
                self.active = None
                self.omitted += 1
            else:
                self.active.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active is not None:
            text = ''.join(self.active)
            self.active = None
            try:
                json.loads(text, parse_constant=reject_json_constant)
            except (ValueError, RecursionError):
                self.omitted += 1
                return
            self.blocks.append(text.encode())


def embedded(archive, session_id, parent, body, role, limits):
    parser = EmbeddedJSON(limits)
    try:
        parser.feed(body.decode('utf-8'))
        parser.close()
    except (UnicodeError, ValueError, RecursionError):
        return 1
    for index, block in enumerate(parser.blocks):
        catalog.derived(archive, session_id, role, block, parent=parent,
                        metadata={'block_index': index, 'extraction': 'script text, UTF-8 only; JSON syntax verified'})
    return parser.omitted


class Broker:
    def __init__(self, archive, session_id, url, limits, client=None):
        self.archive, self.session_id, self.url, self.limits = archive, session_id, url, limits
        self.client = client or PublicHTTP(archive.config, reject_redirects=True)
        self.requests = self.bytes = self.blocked = 0
        self.initial = None
        self.dom = None
        self.omissions = 0

    def request(self, message):
        self.requests += 1
        if self.requests > self.limits.requests:
            raise BudgetError('Browser request budget exhausted')
        if set(message) != {'op', 'url', 'method', 'type', 'initial'}:
            raise PolicyError('Unexpected browser request fields')
        # Browser-supplied headers and credentials are never forwarded or stored.
        url = self.archive.config.permit(message['url'])
        if (message['method'] != 'GET' or type(message['initial']) is not bool
                or message['type'] not in {'document', 'script', 'stylesheet', 'fetch', 'xhr', 'image', 'media'}):
            raise PolicyError('Unsupported browser request')
        if message['initial']:
            if self.initial is not None or url != self.url or message['type'] != 'document':
                raise PolicyError('Only the explicit initial document may navigate')
        elif message['type'] == 'document' or self.initial is None:
            raise PolicyError('Additional documents and frames are disabled')
        request_id = catalog.request_start(self.archive, self.session_id, url, message['type'])
        try:
            reference = self.archive.known_media_reference(url, self.url,
                         force=message['type'] in {'image', 'media'} or bool(url_kind(url)))
            if reference:
                policy, reason = self.archive.reference_admission(reference)
                if not policy:
                    self.archive.media_event(reference, 'not_requested', reason)
                    catalog.request_finish(self.archive, request_id, 'not_requested', reason)
                    self.blocked += 1
                    return {'state': 'blocked'}
            if self.bytes >= self.limits.total_bytes:
                raise BudgetError('Browser body budget exhausted')
            self.archive.enqueue([url], via='browser', source_url=self.url)
            with self.client.open(url) as response:
                # Broker constructs fixed PublicHTTP headers. Neither browser request
                # headers nor Set-Cookie/WWW-Authenticate reach the renderer/archive.
                record = self.archive.capture(url, response, source_url=self.url, reference=reference,
                    max_bytes=min(self.limits.body_bytes, self.limits.total_bytes - self.bytes),
                    browser_request_id=request_id)
            if not record.get('sha256'):
                self.blocked += 1
                catalog.request_finish(self.archive, request_id, 'not_requested', 'media_policy')
                return {'state': 'blocked'}
            self.bytes += record['bytes']
            artifact = self.archive.db.execute('SELECT id FROM browser_artifacts WHERE request_id=? AND capture_id=?',
                                               (request_id, record['id'])).fetchone()[0]
            if message['initial']:
                self.initial = artifact
            body = self.archive.object_path(record['sha256']).read_bytes()
            mime = record['content_type'].split(';', 1)[0].strip().lower()
            if mime == 'application/json' or mime.endswith('+json'):
                # Distinct observation role referencing the SAME immutable bytes.
                catalog.derived(self.archive, self.session_id, 'observed_json_response', body,
                                parent=artifact, request_id=request_id,
                                metadata={'url': url, 'content_type': record['content_type']})
            if message['initial'] and mime in {'text/html', 'application/xhtml+xml'} and record['content_encoding'] in (None, 'identity'):
                self.omissions += embedded(self.archive, self.session_id, artifact, body,
                                           'embedded_json_original', self.limits)
            if record['content_encoding'] not in (None, 'identity') or mime == 'text/event-stream':
                catalog.request_finish(self.archive, request_id, 'preserved_not_rendered', 'unsupported_encoding_or_stream')
                self.blocked += 1
                return {'state': 'blocked'}
            if message['initial'] and mime not in {'text/html', 'application/xhtml+xml'}:
                catalog.request_finish(self.archive, request_id, 'preserved_not_rendered', 'not_html')
                self.blocked += 1
                return {'state': 'blocked'}
            catalog.request_finish(self.archive, request_id, 'captured', status=200)
            # Security restrictions alter execution, never the archived original.
            headers = {'content-type': record['content_type'],
                       'content-security-policy': catalog.intersect_csp(response.headers.get('content-security-policy'))}
            return {'state': 'fulfilled', 'body': base64.b64encode(body).decode(), 'headers': headers}
        except Exception as exc:
            self.blocked += 1
            catalog.request_finish(self.archive, request_id, 'failed', type(exc).__name__, getattr(exc, 'status', None))
            if isinstance(exc, BudgetError):
                raise
            return {'state': 'blocked'}

    def receive_dom(self, message):
        if set(message) != {'op', 'body'} or self.initial is None or self.dom is not None:
            raise PolicyError('Invalid rendered DOM observation')
        body = base64.b64decode(message['body'], validate=True)
        if len(body) > self.limits.dom_bytes:
            raise PolicyError('Rendered DOM exceeds limit')
        self.dom = catalog.derived(self.archive, self.session_id, 'rendered_dom', body, parent=self.initial,
                                   metadata={'url': self.url, 'derived': True, 'renderer': 'Playwright Chromium',
                                             'policy': 'no frames/workers/popups/downloads/POST/WebSocket/EventSource; broker-only resources',
                                             'forwarded_response_headers': ['content-type', 'content-security-policy'],
                                             'omitted_response_headers': 'all others, including nosniff, CORS, COOP, COEP, CORP and Content-Disposition',
                                             'fidelity': 'bounded broker replay; header omissions can change execution compared with a source browser'})
        self.omissions += embedded(self.archive, self.session_id, self.dom, body,
                                   'embedded_json_rendered', self.limits)


def bind_parent_lifetime(expected_parent):
    # Linux PR_SET_PDEATHSIG: parent exit kills a broker even in blocked DNS.
    # Check the expected PID AFTER setting the signal to close the fork race.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise PolicyError('Cannot bind broker lifetime to its supervisor')
    if os.getppid() != expected_parent:
        os._exit(70)


def _run(archive_root, config, url, limits, session_id, command, spec, ready, expected_parent, client_factory=None):
    # New process group is established before the parent is allowed to kill it.
    bind_parent_lifetime(expected_parent)
    os.setsid()
    ready.send(True)
    ready.close()
    os.umask(0o077)
    renderer = None
    with Archive(archive_root, config).writer() as archive:
        try:
            envelope()  # Verify the child inherited the same enforced envelope.
            broker = Broker(archive, session_id, url, limits, client_factory(config) if client_factory else None)
            renderer = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0, close_fds=True,
                                        env={'PATH': '/usr/bin:/bin'}, cwd='/')
            send_message(renderer.stdin, dict(asdict(limits), ipc_bytes=limits.ipc_bytes,
                                              url=url, chromium=spec['chromium']), 16384)
            deadline = time.monotonic() + min(limits.seconds, config.max_run_seconds)
            reader = IPCReader(renderer.stdout)
            ready_message = reader.read(16384, deadline)
            if ready_message != {'op': 'ready'}:
                raise PolicyError('Isolated renderer did not become ready')
            messages = 0
            while True:
                message = reader.read(limits.ipc_bytes, deadline)
                messages += 1
                if messages > limits.requests * 2 + 4:
                    raise BudgetError('Browser event count exhausted')
                op = message.get('op')
                if op == 'request':
                    answer = broker.request(message)
                    send_message(renderer.stdin, answer, limits.ipc_bytes)
                elif op == 'blocked':
                    broker.blocked += 1
                elif op == 'dom':
                    broker.receive_dom(message)
                elif op == 'done' and broker.dom is not None:
                    partial = broker.blocked or broker.omissions or message.get('requests_exhausted')
                    catalog.finish(archive, session_id, 'partial' if partial else 'complete',
                                   'bounded_resources_or_extractions' if partial else None)
                    return
                else:
                    raise PolicyError('Renderer failed or returned an invalid event')
        except Exception as exc:
            catalog.finish(archive, session_id, 'partial', type(exc).__name__)
        finally:
            if renderer:
                renderer.kill()
                renderer.wait(timeout=3)


def capture_browser(archive_root, config, url, runtime, limits=None):
    limits = limits or BrowserLimits()
    url = config.permit(url)
    command, spec = runtime_command(runtime, limits)
    lifecycle = None
    try:
        with Archive(archive_root, config).writer() as archive:
            lifecycle = open(archive.root / '.browser.lock', 'a+')
            try:
                fcntl.flock(lifecycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                from .config import BusyError
                raise BusyError('Another browser capture owns the lifecycle') from exc
            catalog.recover_interrupted(archive)
            session_id = catalog.start(archive, url, dict(asdict(limits), enforced_resources=spec['resource_envelope']))
        return _supervise(archive_root, config, url, limits, session_id, command, spec)
    finally:
        if lifecycle:
            lifecycle.close()


def _supervise(archive_root, config, url, limits, session_id, command, spec):
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_run, args=(archive_root, config, url, limits, session_id, command, spec, sender, os.getpid()))
    worker.start()
    sender.close()
    try:
        established = receiver.poll(5) and receiver.recv() is True
    except EOFError:
        established = False
    finally:
        receiver.close()
    owned = True
    try:
        if established:
            wait_unreaped(worker.pid, min(limits.seconds, config.max_run_seconds) + 3)
    except ChildProcessError:
        # An external SIGCHLD handler reaped it; never signal a recycled number.
        owned = False
    finally:
        if owned and established:
            # Leader is still alive or a retained zombie, so its PID/PGID cannot
            # be recycled between observing exit and signaling our process group.
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif owned:
            try:
                os.kill(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        worker.join(5)  # Reap ONLY AFTER owned group termination.
    with Archive(archive_root, config).writer() as archive:
        state = archive.db.execute('SELECT state FROM browser_sessions WHERE id=?', (session_id,)).fetchone()[0]
        if state == 'running':
            return catalog.finish(archive, session_id, 'partial', 'process_deadline_or_crash')
        return catalog.save_manifest(archive, session_id)


def wait_unreaped(pid, seconds):
    """Wait for our child without releasing its PID before group cleanup."""
    deadline = time.monotonic() + seconds
    while True:
        event = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if event is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))
