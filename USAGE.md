# Running the archive pipeline

This repository contains code and source references. **Keep raw originals, source
manifests, transcripts, credentials, and all generated data on private local
storage outside Git.** No live collection, API call, or Discord post runs on
installation, import, or CI. The repository owner's original README is preserved.

For Android installation, see [the Termux guide](docs/TERMUX.md). All runtime
commands work locally without GitHub or CI. For continuous collection, see
[the cron deployment guide](docs/CRON.md).
Each scheduled invocation is bounded and resumes from durable state.

## First run

Python 3.11+ is required (`fcntl` provides crash-safe writer locks). The desktop
path targets Linux/macOS; Android uses the separate Termux installer and emulator
verification described in the Termux guide.
The base pipeline uses only Python's standard library.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
cp config/us-2026.example.json config.local.json
```

Edit `config.local.json`: add exact approved public source hosts to
`allowed_hosts`, and approved URLs to `manual_urls` or RSS/Atom URLs to `feeds`.
An empty allowlist intentionally collects nothing. See `docs/SOURCES.md` for a bounded official U.S. starting profile and news-feed candidates. Each redirected/media host
must also be explicitly allowed. Configure a private local archive directory and
disk budgets appropriate to your machine; the example 10 GiB cap is a starter
limit, not an estimate of nationwide coverage.

```sh
export ARCHIVE="$HOME/.local/share/election26/us-2026"
election26 --config config.local.json --archive "$ARCHIVE" discover --mode manual
election26 --config config.local.json --archive "$ARCHIVE" fetch
election26 --archive "$ARCHIVE" status
election26 --archive "$ARCHIVE" gc
```

Media links and available type/length metadata are recorded by default. Raw HTML,
JSON and feed originals preserve their original references. Raw media requires an
explicit source-host policy with selected types and file/storage budgets. This
also gates ordinary/manual/article/feed URLs that return media; unknown binary
responses fail closed before their bodies are read. Known media suffixes are
recorded without requesting them when not opted in.

The legacy `--extract-media` flag is accepted for compatibility, but cannot grant
download permission. HTML references are now recorded regardless of that flag.
`fetch URL` processes a specific allowlisted URL. `fetch --refresh URL` records a
new observation and preserves previous versions. Each invocation processes at
most `max_urls` items, with a rotating bounded media-reference window. Failed or
pending requests resume on later invocations. Downloaded references are skipped;
previously not-requested links can be admitted after an explicit config change.
Interruptions resume at the item boundary, not at a byte offset.

See [media policy and replay](docs/MEDIA.md) before opting in. The empty default
`media_policies` map is suitable for links-first collection on a phone.

## Discovery and scope

The initial profile is nationwide United States, election year 2026, with public
news/event and protest coverage as a priority. This is a raw acquisition layer:
HTML, JSON and optionally selected media, with derived transcription when
explicitly requested and supported. Later aggregation belongs in Star Server.
No people dossiers, facial recognition, biometrics, or private account content.

Operator-provided WiGLE files are a separate planned local input; there is no
WiGLE importer in this slice. Preserve originals privately and keep sanitized
exports separate. Never Git-track or commit WiGLE KML/KMZ files or route-bearing
derivatives, even for a private simulation comparison. Use
`python scripts/check-public-tree.py --staged` before any commit; CI also checks
the committed tree. Ignore rules and this guard are defense in depth, not
proof that arbitrary renamed/encoded data is safe. No real KML fixtures belong
in this public repository. Wireless observations must not be used here to infer identities,
political affiliation, or protest attendance.
The date bounds are sent to Brave when used; RSS/manual archival does not infer
publication dates or silently discard originals based on uncertain dates.
Country/year/date fields support later explicitly selected profiles. They do not
claim completeness or activate other countries.

```sh
election26 --config config.local.json --archive "$ARCHIVE" discover --mode rss
# Optional: enable brave_enabled only after authorizing your API plan/usage.
# Provide BRAVE_SEARCH_API_KEY through your environment; never commit its value.
election26 --config config.local.json --archive "$ARCHIVE" discover --mode brave --query 'United States election 2026 public protest reporting'
```

Brave makes one bounded request per command, up to 20 results, and may incur
charges. There is no automatic pagination or paid transcription. An optional
`keyring:SERVICE/USERNAME` credential reference is supported after installing
`.[keyring]`. Neither credential values nor returned article text enter stats.

## Storage and recovery

- Immutable originals: `objects/<first-two-sha256-chars>/<sha256>`.
- `catalog.sqlite`: operational URL queue, capture observations, checkpoints,
  transcript provenance, and notification state. **Not a StarIntel schema.**
- `manifests/<capture-id>.json`: source/final URL, UTC capture time, SHA-256,
  byte count, response type/encoding, ETag, and Last-Modified.
- `manifests/media-<observation-id>.json`: versioned media reference, source
  attribution, declared/response metadata, policy snapshot, and acquisition state.
- `transcripts/<source-sha256>.json`: local derived result with source/model hash.

Writes stream to a temporary file, hash, fsync, atomically rename, then commit
observation + checkpoint in one SQLite transaction. A process lock excludes a
second writer. URLs and retained header fields have byte limits. Disk admission
conservatively reserves catalog rollback-journal, sidecar, and metadata space;
this may stop before the nominal cap. Model files and backups have separate
capacity needs. Archive descendants may not be symlinks. Status reads committed transactions. Duplicate bytes share one
object while each capture observation retains its provenance.

A crash between blob and catalog commits may leave an orphan object; `gc` lists
it without deleting it. A crash after catalog commit can leave a missing JSON
sidecar; `repair` reconstructs missing manifests from the catalog. Corrupt
catalogs/objects fail closed rather than resetting or overwriting evidence.

```sh
election26 --config config.local.json --archive "$ARCHIVE" repair
election26 --config config.local.json --archive "$ARCHIVE" backup "$HOME/election26-backup-2026-10-11"
```

Backup uses SQLite's backup API while holding the archive lock and copies
originals/manifests/transcripts to a new destination. Point `--archive` at that
copy to restore/use it. The backup must fit its destination disk; test restores
before relying on a backup. A single disk is not a disaster-recovery plan.

## Optional local transcription

```sh
python -m pip install '.[whisper]'
# Acquire a trusted official tiny/base Whisper model checkpoint separately.
# This command never downloads a model or invokes FFmpeg.
election26 --config config.local.json --archive "$ARCHIVE" transcribe SOURCE_SHA256 --model-path /private/models/base.pt --language en
# After reading and checking accuracy yourself:
election26 --config config.local.json --archive "$ARCHIVE" transcribe SOURCE_SHA256 --verify
```

Whisper is optional and local-only. The first slice accepts complete mono
16 kHz signed 16-bit PCM WAV recordings of at most ten minutes and trusted model
checkpoints up to 160 MiB. Other media formats remain archived but fail closed
for transcription. It decodes WAV with the standard library and gives Whisper
a bounded sample array, never an untrusted filename or playlist. No raw media
is uploaded to a provider.
Transcripts can contain errors; a successful checksum is not an accuracy review.
Human verification is explicit. Model/dependency installation can consume
substantial storage, separate from the archive disk budget.

`gc` is always a dry-run plan: it can identify raw objects older than the
retention window with a human-reviewed, intact transcript and intact original.
**There is no permanent deletion or automatic eviction command.** All originals,
transcripts, hashes, and provenance remain. Retention planning does not treat
an image, video, or audio original as equivalent to its transcript.

## Optional canonical StarIntel metadata

```sh
python -m pip install '.[starintel]'
election26 --config config.local.json --archive "$ARCHIVE" normalize 1
```

The optional dependency is pinned to StarIntel SDK 0.10.1 at immutable commit
`6aa8051dc7ea560943a60a25e1d600467f2778bc`. Its canonical lock pins StarLang
`9198370f7a6f3e2a5ea00af3efdb6d705e102650`. The adapter verifies the installed
VCS commit and schema version, calls the SDK's `build_http_transaction`, and
validates/serializes through its exact-number-aware API. It does not create
people, relationships, election claims, or a new domain schema.

`normalize CAPTURE_ID` creates local canonical HTTP metadata, joined to the raw
hash and source observation through the operational catalog. Original bytes and
historical captures never change when normalization fails or is absent. Output
stays private in `canonical/`. No upstream/private code is copied into this repo.
See `docs/STARINTEL.md` for verified APIs and licensing caveat.

## Optional Discord aggregate status

```sh
election26 --config config.local.json --archive "$ARCHIVE" stats
# Only after destination/permission and secret configuration are complete:
election26 --config config.local.json --archive "$ARCHIVE" stats --send
```

Disabled by default. `stats` previews only fixed aggregate counters. Set
`discord_enabled` and reference a webhook stored in the local environment or
keyring only after authorizing the destination. `--send` sends once; a persisted
15-minute minimum interval limits repeats. `wait=true` requires a confirmed
Discord message ID before marking success. No scheduler is installed. An
uncertain send leaves a pending marker and blocks automatic retries; inspect
the destination before an operator clears that marker. Mentions are disabled.
The webhook URL is a secret and must not appear in Git, CLI arguments, or logs.

## Validation and current limits

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src
```

The Linux CI matrix tests synthetic fixtures across Python 3.11–3.13. The
separate Android workflow executes inside a real Termux app on an Android emulator;
its exact run must pass before Android support is claimed. No API credentials,
Discord destination, live collection, optional Whisper model execution, or
user-computer deployment is configured by this bootstrap. Direct HTTP media
is supported; JavaScript-rendered sites, segmented streaming, authenticated
content, social network APIs, and third-party downloaders are not yet wired.
There is no network service or dashboard. Optional StarIntel metadata normalization uses the pinned SDK; adapter boundary
tests run with fixtures, the canonical SDK job also installs the exact real SDK and validates the generated
contract. Whisper model inference remains untested. Raw originals always survive adapter failures.

## Local FediWatch spools

`import-spool` verifies and imports an existing `fediwatch.raw.v1` producer spool
without network activity, retaining exact bodies/receipts, source observation
times and sequence IDs. See [the producer/import workflow](docs/FEDIWATCH.md).
Timed Star Server replay remains a separate pending integration.

## Official references

- [Brave Web Search request contract](https://api-dashboard.search.brave.com/api-reference/web/search/get)
- [OpenAI Whisper](https://github.com/openai/whisper)
- [Discord webhook execution](https://discord.com/developers/docs/resources/webhook#execute-webhook)
- [Python SQLite backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)

## Optional isolated browser capture

Linux operators can opt into one-URL rendered capture with separately preserved
original HTML, rendered DOM and observed JSON. See [BROWSER.md](docs/BROWSER.md)
for the mandatory namespace/sandbox requirements, runtime build and limits.
Native Termux and macOS browser capture are unsupported; ordinary raw HTTP is unchanged.
