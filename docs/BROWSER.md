# Opt-in isolated browser capture (Linux only)

`election26 browser` captures one explicit allowlisted public URL. It runs no
crawl, login, click, submission, or authenticated-account session. Raw HTTP
acquisition remains available on Termux; this browser backend is **unsupported
on native Android/Termux, PRoot, macOS and Windows**. It requires a non-root Linux
host with working unprivileged user, network, PID, IPC and mount namespaces,
bubblewrap 0.12+, sandboxed Chromium, and an enforced cgroup-v2 envelope
with at most 1 GiB memory, no swap and 256 tasks across the process tree. Unsupported hosts fail closed.

## Local setup and use

Install Python 3.11+ and bubblewrap 0.12+ from trusted official packages. Install
the optional renderer dependency and its official Chromium browser, then create
a dedicated runtime directory **outside any source checkout and archive**:

```sh
python -m pip install '.[browser]'
python -m playwright install --with-deps chromium
python scripts/build-browser-runtime.py "$HOME/.local/share/election26-browser-runtime"

systemd-run --user --scope -p MemoryMax=1G -p MemorySwapMax=0 -p TasksMax=256 \
  election26 --config config.local.json --archive "$HOME/.local/share/election26-archive" \
  browser 'https://explicit-allowlisted.example/page' \
  --runtime "$HOME/.local/share/election26-browser-runtime"
```

Installing dependencies may need ordinary OS package administration. Never
weaken security settings, disable Chromium's sandbox, use privileged containers,
or remove namespace flags to make a failed preflight work. The runtime builder
copies only the installed trusted Python standard library, four named renderer
modules, Chromium's directory and linked shared libraries. It does not install
or download anything. Do not use it on untrusted binaries: `ldd` is part of the
trusted-runtime build. Rebuild into a new directory after renderer updates.
The runtime is public program data, not an archive or arbitrary filesystem root.

The Python dependency is pinned to Playwright 1.62.0; Chromium comes from that
release's official installer. The runtime does not require GitHub or CI. CI
installs the same dependencies and runs synthetic fixtures as validation only.
The mandatory real-browser fixture can also be run locally:

```sh
systemd-run --user --scope -p MemoryMax=1G -p MemorySwapMax=0 -p TasksMax=256 \
  python scripts/ci-browser-fixture.py "$HOME/.local/share/election26-browser-runtime"
```

## Security boundary and transport

A supervised trusted acquisition process uses `PublicHTTP` outside the browser
namespace. It enforces exact host admission, public-only DNS resolution, checked
IP pinning, TLS certificate verification, robots policy, pacing and existing
archive/media budgets. The initial implementation rejects every HTTP redirect,
including robots redirects, before contacting its target. Only HTTP 200 response
bodies become complete captures. Failed requests retain an observed status code
when available; no unavailable status or source publication time is invented.

Browser requests cross bounded anonymous stdin/stdout pipes. No TCP debugging
listener or shared Unix socket is created. Only GET, URL, resource type and the
initial-navigation flag are accepted. Browser-supplied headers never reach the
network. The broker constructs its own fixed User-Agent and identity-encoding
headers; cookies, Authorization, Referer, Origin and response Set-Cookie are not
forwarded. The renderer only fulfills broker-approved bytes. It never uses
`route.continue_`, `route.fetch`, APIRequestContext or browser networking.

Interception is not the security boundary. Bubblewrap creates a separate network
namespace even when a browser path bypasses interception. The only read-only
mount is the dedicated public runtime. HOME, archive data, credentials,
environment secrets and host sockets are absent; private size-limited tmpfs
mounts hold temporary browser state. The PID namespace and process-group
supervisor terminate Chromium and the broker on deadline, including blocked DNS.
The kernel cgroup-v2 memory/swap/tasks limits are verified before acquisition and
again in the broker; an absent or unbounded envelope fails closed. The renderer
cannot access cgroupfs to migrate. V8 heap flags and RSS polling are not treated
as memory enforcement. If an OOM kills the whole CLI, a running session is only
the last committed state, never proof that it is still alive or complete.
A separate lifecycle lock spans session publication and worker ownership; the
next browser invocation or `repair` marks interrupted sessions partial only after
obtaining this lock. The broker uses race-checked Linux parent-death SIGKILL, so
parent termination also stops a broker stuck in DNS and releases its lock.
Chromium's sandbox remains enabled. No weaker fallback exists.

Service workers, other workers, child frames, extra navigations, popups,
downloads, permissions, POST, WebSocket and EventSource are disabled or rejected.
The source's bounded enforcing CSP headers are intersected with an additional
restrictive CSP only for renderer fulfillment; their source/applied policies are
recorded with original-response provenance. Archived body bytes are unchanged. Only Content-Type and the intersected enforcing CSP are supplied to the
renderer. All other response headers, including nosniff, CORS, COOP/COEP/CORP
and Content-Disposition, are omitted, as recorded on the DOM artifact. These
omissions can change execution, not merely block it; the snapshot is a bounded
broker replay rather than a claim of source-browser equivalence. Completion means the bounded observation
ended, not that all content or future network activity was captured.

## Evidence and limits

`manifests/browser-N.json` is a navigable operational manifest with relative
content-addressed object paths. Do not open captured untrusted HTML as trusted
local code. The manifest keeps separate roles for:

- Original response entity bytes, with HTTP capture ID, requested/completed
  capture times, URL, hash, MIME and observed Last-Modified/ETag metadata
- A separately labeled derived rendered DOM snapshot
- Observed JSON response bodies, linked to their original response artifact
- Bounded valid-JSON script text extracted separately from original HTML and DOM

Embedded JSON extraction is UTF-8 only. Its script text is an extraction product,
not a byte-identical copy of the enclosing HTML response. HTTP Last-Modified is
unverified source metadata, not a publication timestamp. A later replay import
must preserve these semantics rather than invent a new acquisition time.

Defaults are 45 seconds, 40 browser requests, 16 MiB total body bytes, 2 MiB per
body and DOM, 20,000 DOM elements, 16 embedded blocks per representation, 256 KiB
per embedded block, a 1-second settle window and 256 MiB temporary browser disk.
The request/time/file/disk caps in the ordinary configuration also apply. CLI
`--seconds` and `--requests` may lower or raise these only within hard caps of
120 seconds and 100 requests. API limits are validated with hard finite caps.
Process cleanup has a bounded grace period of at most 3 seconds after run time.

Raw media remains links and metadata by default. Browser images/audio/video use
the existing source-specific media opt-in and its selected types and budgets.
`application/javascript` and `application/ecmascript` are admitted as narrow
textual runtime formats; unknown/binary MIME remains blocked. No model download,
private input, live fixture capture or canonical schema is bundled in the repo.

Sessions and artifact relationships are stored in the transactional catalog
(`browser_format_version=1` is an independent operational extension; core
version 2 is unchanged). Original-response relationships commit atomically with
the capture. `status`, `repair`, `backup` and dry-run `gc` account for derived
blobs. A deadline, crash, missing object, blocked request, extraction limit or
renderer failure is reported honestly. Completed originals survive renderer
failure; sidecars can be rebuilt from the catalog.

## Verification boundaries

The hosted Linux gate requires actual Chromium JavaScript mutation + JSON fetch,
unchanged originals, derived relationships, and preservation after oversized DOM.
A separate real browser deliberately runs without request interception and must
fail to reach a TCP sentinel outside its network namespace. It also checks the
absence of a host file/environment value and Chromium sandbox evidence. It
also exhausts the kernel tasks limit and triggers a real ArrayBuffer OOM under
the cgroup memory ceiling, requiring preserved originals and a partial result. There is
no production localhost-admission option.

Unit tests cover denied/private/robots/redirect targets, fixed headers, links-first
media, IPC/request/body/DOM/extraction limits, disk failure and catalog integrity.
On restricted executors, bubblewrap may fail with `NETLINK_ROUTE ... Operation
not permitted`; this is a fail-closed result, not successful browser validation.
Actual Android/Termux tests still validate only the supported base raw-HTTP path.

Official implementation references:

- https://playwright.dev/python/docs/network
- https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-option-chromium-sandbox
- https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket
- https://github.com/containers/bubblewrap
- https://github.com/containers/bubblewrap/releases/tag/v0.12.0
- https://github.com/termux/termux-packages/blob/master/x11-packages/chromium/build.sh
- https://github.com/termux/proot-distro

Resource-control references: https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html and https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html
