# Source-specific media policy

Links and available metadata are the default. No model/transcription command,
legacy `--extract-media` flag, optional yt-dlp installation, or host allowlist is
permission to acquire media bytes. This policy applies at the shared archive
capture boundary, including direct/manual URLs and RSS feed responses.

## Explicit opt-in

Edit a private configuration outside Git. Example values below are illustrative;
choose limits suitable for the actual storage device before enabling downloads.

```json
{
  "allowed_hosts": ["news.example", "cdn.example"],
  "media_policies": {
    "news.example": {
      "download": true,
      "types": ["audio"],
      "max_file_bytes": 16777216,
      "max_total_bytes": 268435456,
      "min_free_bytes": 1073741824
    }
  }
}
```

- Keys are exact source hostnames from `allowed_hosts`, without wildcards.
  This first slice shares a policy across that host; path/board selectors are
  not supported. A discovered reference retains the source page/feed identity.
- Each media destination and every redirected host must also be allowlisted.
  A CDN allowlist entry grants network reachability only. A CDN's own download
  policy does not authorize a different source's media.
- `download` must be an explicit boolean. An enabled policy requires a nonempty
  subset of `audio`, `video`, `image`, `document` (PDF), plus all three budgets.
- Per-file admission is the smaller of the source and global `max_file_bytes`.
  Advertised sizes can reject a request early; streaming byte accounting remains
  authoritative when headers are absent, inaccurate, or a transfer is truncated.
- `max_total_bytes` bounds unique cataloged media-object bytes attributed to this
  source host. Historical known media captures count. New requests conservatively
  reserve their whole size before any deduplication benefit is known.
- Global `max_archive_bytes` still includes all archive data and metadata.
  The higher source/global free-space reserve applies. Transaction/journal/sidecar
  reserves can stop work before the nominal data cap. Incomplete/orphan objects
  continue to consume the global disk budget; nothing is evicted.

Source-host permission does not override robots rules, pinned public-only DNS,
redirect policy, request/run deadlines, or the single-writer lock. All downloads
use the existing bounded HTTP client. There is no yt-dlp transport in the
collector, no segment/playlist expansion, login, browser media fetch, paid API
fallback, or implicit transcription download.

## Metadata and admission

HTML media elements/Open Graph/direct media links and RSS/Atom enclosures record
resolved public HTTP(S) URLs. Missing declared MIME/length fields remain null;
filename/tag hints guide admission but never fill in missing metadata. Invalid
feed lengths remain unknown, with the original XML retained. Credential/signed
URLs are rejected rather than persisted. Raw HTML/JSON/XML preserves additional
references beyond the bounded structured extraction window (at most `max_urls`
and never more than 500 per captured document).

Headers are reported as response declarations, not signature-verified content.
Media URL/type/element disagreements, unexpected text responses to media links,
and unknown binary MIME types fail closed. A previously unknown ordinary URL can
require a bounded header request to discover its type, but a disallowed media
body is never read or stored. Normal text/HTML/JSON/XML source bodies remain raw
originals. RSS bodies are additionally bounded at 4 MiB before parsing.

A future adapter should first archive its source JSON/HTML, then call
`archive.record_media_links(capture, links)`. A link contains `url` and optional
`content_type`, `advertised_bytes`, `kind_hint`. `capture.source_url` identifies the
source, even when media is hosted elsewhere. `archive.fetch_media(client, limit)`
uses the shared admission path; adapters must not fetch media on their own.

## Versioned outcomes and local replay

```sh
election26 --config /private/config.local.json --archive /private/archive media
```

This command reads latest reference outcomes and verifies any downloaded object
locally. It makes no network request. Outcomes are:

- `not_requested`: no body acquisition was authorized/admitted; includes source
  opt-out, type, allowlist, and preflight budget exclusions.
- `pending`: permitted reference awaiting a bounded attempt, or a deferred run.
- `downloaded`: a fully hashed/fsynced original and capture checkpoint committed
  together with the media observation.
- `unavailable`: an attempted acquisition failed, including network rejection,
  missing remote content, interrupted body, or a streaming-budget rejection.
- `missing`: local replay found a previously downloaded object missing/corrupt.
  `recorded_state` stays `downloaded`; replay never rewrites historical evidence
  or silently fetches a replacement.

Version-2 `media-<id>.json` sidecars and SQLite observations are append-only. They
include source URL, parent capture ID, discovery/observation times, declared
metadata, response metadata when available, policy snapshot, and capture/hash
joins after a successful download. Source capture time and Last-Modified remain
unchanged. Existing capture sidecars/originals are never overwritten by migration.
Version-1 catalogs migrate additively in a restart-safe transaction after reserving
journal/schema/URL-backfill space; unattributed legacy `html-media` queue
entries fail closed until an operator identifies their source. Existing archived
media remains accessible regardless of current acquisition policy.

`repair` reconstructs missing sidecars from SQLite. Backup includes references,
all observations and originals. `gc` remains dry-run-only. Media observations are
an operational archive contract, not a new canonical StarIntel document schema.
Full raw-import/derived-artifact replay remains a separate feature.
