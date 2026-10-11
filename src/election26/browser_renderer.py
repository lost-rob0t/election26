"""Untrusted-side renderer. Executed only inside the enforced empty-net namespace.

The acquisition broker, credentials, archive, host HOME and host sockets are
outside this process's filesystem and PID namespaces. IPC is bounded JSON lines.
This file deliberately imports no archive/network code.
"""
import base64
import json
import os
import resource
import sys

sys.path.insert(0, '/app/vendor')


def emit(value):
    data = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode() + b'\n'
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('IPC limit')
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def receive(limit):
    line = sys.stdin.buffer.readline(limit + 1)
    if len(line) > limit or not line.endswith(b'\n'):
        raise ValueError('IPC limit')
    return json.loads(line)


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
    config = receive(16384)
    resource.setrlimit(resource.RLIMIT_CPU, (config['seconds'] + 2,) * 2)
    os.makedirs('/tmp/home', mode=0o700, exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=config['chromium'], headless=True,
            chromium_sandbox=True,
            args=['--disable-background-networking', '--disable-extensions',
                  '--js-flags=--max-old-space-size=128'])
        context = browser.new_context(service_workers='block', accept_downloads=False,
                                      permissions=[], java_script_enabled=True)
        context.clear_permissions()
        page = context.new_page()
        context.on('page', lambda popup: popup.close() if popup != page else None)
        page.on('dialog', lambda dialog: dialog.dismiss())
        page.on('download', lambda download: download.cancel())
        context.route_web_socket('**/*', lambda ws: ws.close())
        # EventSource is also rejected by its request type; this avoids retry loops.
        context.add_init_script('''(() => {
          Object.defineProperty(globalThis, 'EventSource', {value: class {
            constructor() { throw new Error('EventSource disabled'); }
          }, configurable: false, writable: false});
        })();''')
        count = 0
        main_seen = False
        def handle(route):
            nonlocal count, main_seen
            request = route.request
            count += 1
            if count > config['requests']:
                route.abort()
                return
            initial = (not main_seen and request.is_navigation_request()
                       and request.frame == page.main_frame and request.url == config['url'])
            if initial:
                main_seen = True
            # No other page/frame navigation, popups, POST, streaming or workers.
            try:
                main_frame = request.frame == page.main_frame
            except Exception:
                main_frame = False
            if (request.method != 'GET' or not main_frame or
                    (request.is_navigation_request() and not initial) or
                    request.resource_type not in {'document', 'script', 'stylesheet', 'xhr', 'fetch', 'image', 'media'}):
                emit({'op': 'blocked', 'reason': 'unsupported_request'})
                route.abort()
                return
            # No browser header (Cookie, Authorization, Origin, Referer, etc.) is sent.
            emit({'op': 'request', 'url': request.url, 'method': request.method,
                  'type': request.resource_type, 'initial': initial})
            answer = receive(config['ipc_bytes'])
            if answer.get('state') != 'fulfilled':
                route.abort()
                return
            body = base64.b64decode(answer['body'], validate=True)
            # This is the only path that supplies page resources. Never use
            # continue_, fetch, APIRequestContext or the browser's network.
            route.fulfill(status=200, body=body, headers=answer['headers'])
        context.route('**/*', handle)
        emit({'op': 'ready'})
        try:
            page.goto(config['url'], wait_until='domcontentloaded', timeout=config['seconds'] * 1000)
            page.wait_for_timeout(config['settle_ms'])
            # Bound inside the page BEFORE serializing over the Playwright pipe.
            # Both string/UTF-8 length and a DOM node count are capped.
            dom = page.evaluate('''limits => {
              const nodes = document.getElementsByTagName('*');
              if (nodes.length > limits.nodes) return null;
              const value = '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;
              if (value.length > limits.bytes) return null;
              if (new TextEncoder().encode(value).length > limits.bytes) return null;
              return value;
            }''', {'nodes': config['dom_nodes'], 'bytes': config['dom_bytes']})
            if dom is None:
                emit({'op': 'failed', 'reason': 'dom_limit'})
            else:
                emit({'op': 'dom', 'body': base64.b64encode(dom.encode()).decode()})
                emit({'op': 'done', 'requests_exhausted': count > config['requests']})
        except Exception:
            emit({'op': 'failed', 'reason': 'renderer_failure'})
        finally:
            browser.close()


if __name__ == '__main__':
    main()
