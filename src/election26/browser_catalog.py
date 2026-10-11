"""Operational browser evidence relationships, not canonical StarIntel records."""
import json
import fcntl

from .archive import atomic_json, now, sha256_file
from .config import PolicyError


RENDERER_CSP = "frame-src 'none'; child-src 'none'; worker-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"


def intersect_csp(source):
    return source + ', ' + RENDERER_CSP if source else RENDERER_CSP


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_sessions'").fetchone())


def initialize(archive):
    version = archive.db.execute("SELECT value FROM meta WHERE key='browser_format_version'").fetchone()
    if version and version[0] != '1':
        raise PolicyError('Unsupported browser catalog extension version')
    if version:
        return
    archive.transaction_budget(16384)
    archive.db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS browser_sessions(
        id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, state TEXT NOT NULL, reason TEXT, limits TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS browser_requests(
        id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES browser_sessions(id),
        url TEXT NOT NULL, requested_at TEXT NOT NULL, resource_type TEXT NOT NULL,
        finished_at TEXT, state TEXT NOT NULL, reason TEXT, response_status INTEGER);
      CREATE TABLE IF NOT EXISTS browser_artifacts(
        id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES browser_sessions(id),
        request_id INTEGER REFERENCES browser_requests(id),
        capture_id INTEGER REFERENCES captures(id), parent_artifact_id INTEGER REFERENCES browser_artifacts(id),
        role TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL, metadata TEXT NOT NULL);
      INSERT OR REPLACE INTO meta VALUES ('browser_format_version','1');
      COMMIT;
    ''')


def start(archive, url, limits):
    initialize(archive)
    archive.transaction_budget(8192)
    with archive.db:
        cur = archive.db.execute('INSERT INTO browser_sessions(source_url,started_at,state,limits) VALUES (?,?,?,?)',
                                 (url, now(), 'running', json.dumps(limits)))
    save_manifest(archive, cur.lastrowid)
    return cur.lastrowid


def request_start(archive, session_id, url, kind):
    archive.transaction_budget(len(url.encode()) + 4096)
    with archive.db:
        cur = archive.db.execute('INSERT INTO browser_requests(session_id,url,requested_at,resource_type,state) VALUES (?,?,?,?,?)',
                                 (session_id, url, now(), kind, 'running'))
    return cur.lastrowid


def request_finish(archive, request_id, state, reason=None, status=None):
    archive.transaction_budget(4096)
    with archive.db:
        archive.db.execute('UPDATE browser_requests SET finished_at=?,state=?,reason=?,response_status=? WHERE id=?',
                           (now(), state, reason, status, request_id))


def original(archive, request_id, record, response):
    """Called INSIDE Archive.capture's transaction: no detached originals."""
    row = archive.db.execute('SELECT session_id FROM browser_requests WHERE id=?', (request_id,)).fetchone()
    if not row:
        raise PolicyError('Unknown browser request')
    metadata = {'source_content_security_policy': response.headers.get('content-security-policy'),
                'rendered_content_security_policy': intersect_csp(response.headers.get('content-security-policy')),
                'response_status': 200, 'url': record['url'], 'final_url': record['final_url'],
                'content_type': record['content_type'], 'content_encoding': record['content_encoding'],
                'source_last_modified': record['last_modified'], 'etag': record['etag'],
                'source_timestamp_semantics': 'unverified HTTP Last-Modified header',
                'response_body': 'original received entity bytes; transfer framing removed'}
    archive.db.execute('''INSERT INTO browser_artifacts(session_id,request_id,capture_id,role,sha256,bytes,created_at,metadata)
                          VALUES (?,?,?,?,?,?,?,?)''',
                       (row[0], request_id, record['id'], 'original_response', record['sha256'],
                        record['bytes'], record['captured_at'], json.dumps(metadata)))


def derived(archive, session_id, role, body, *, parent=None, request_id=None, metadata=None):
    checksum, size = archive.store([body])
    archive.transaction_budget(8192)
    with archive.db:
        cur = archive.db.execute('''INSERT INTO browser_artifacts(session_id,request_id,parent_artifact_id,role,sha256,bytes,created_at,metadata)
                                  VALUES (?,?,?,?,?,?,?,?)''',
                                (session_id, request_id, parent, role, checksum, size, now(), json.dumps(metadata or {})))
    return cur.lastrowid


def finish(archive, session_id, state, reason=None):
    archive.transaction_budget(8192)
    with archive.db:
        archive.db.execute('UPDATE browser_sessions SET finished_at=?,state=?,reason=? WHERE id=?',
                           (now(), state, reason, session_id))
        archive.db.execute("UPDATE browser_requests SET finished_at=?,state='interrupted',reason=? WHERE session_id=? AND state='running'",
                           (now(), reason or 'renderer_ended', session_id))
    return save_manifest(archive, session_id)


def manifest(archive, session_id):
    row = archive.db.execute('SELECT * FROM browser_sessions WHERE id=?', (session_id,)).fetchone()
    if not row:
        raise PolicyError('Unknown browser session')
    result = dict(row)
    result['limits'] = json.loads(result['limits'])
    result.update(format_version=1, record_type='browser_capture',
                  source_time=None, source_time_note='No source publication time inferred',
                  requests=[dict(r) for r in archive.db.execute('SELECT * FROM browser_requests WHERE session_id=? ORDER BY id', (session_id,))],
                  artifacts=[])
    for row in archive.db.execute('SELECT * FROM browser_artifacts WHERE session_id=? ORDER BY id', (session_id,)):
        item = dict(row)
        item['metadata'] = json.loads(item['metadata'])
        # Relative to manifests/browser-N.json; no executable HTML index.
        item['object'] = '../objects/' + row['sha256'][:2] + '/' + row['sha256']
        path = archive.object_path(row['sha256'])
        item['integrity'] = 'verified' if path.is_file() and sha256_file(path) == row['sha256'] else 'missing_or_corrupt'
        result['artifacts'].append(item)
    return result


def save_manifest(archive, session_id):
    result = manifest(archive, session_id)
    encoded = json.dumps(result, ensure_ascii=False).encode()
    archive.budget(len(encoded) * 2 + 4096)
    try:
        atomic_json(archive.root / 'manifests' / ('browser-' + str(session_id) + '.json'), result)
    except OSError:
        result['manifest_pending'] = True
    return result


def recover_interrupted(archive):
    """Caller must own the lifecycle lock, not merely the SQLite writer lock."""
    if not exists(archive.db):
        return
    for row in list(archive.db.execute("SELECT id FROM browser_sessions WHERE state='running'")):
        finish(archive, row[0], 'partial', 'supervisor_interrupted')


def repair(archive):
    if exists(archive.db):
        # An active supervisor holds this through its parent/worker lock gap.
        # A dead supervisor's broker is killed by PDEATHSIG, releasing its copy.
        with open(archive.root / '.browser.lock', 'a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                recover_interrupted(archive)
        for row in archive.db.execute('SELECT id FROM browser_sessions'):
            save_manifest(archive, row[0])
