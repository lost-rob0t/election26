# Offline FediWatch raw-spool import

This reuses the existing public Nim collector. It does not add another Mastodon
HTTP client. The accepted producer is
[`lost-rob0t/fediWatch@ba0cf3b896620c50d0a92daacaa57af6cf0558dd`](https://github.com/lost-rob0t/fediWatch/tree/ba0cf3b896620c50d0a92daacaa57af6cf0558dd),
whose [`RAW.md`](https://github.com/lost-rob0t/fediWatch/blob/ba0cf3b896620c50d0a92daacaa57af6cf0558dd/RAW.md)
defines the `fediwatch.raw.v1` receipt/body contract. Its raw package has no
StarRouter or canonical ingestion runtime dependency.

## Producer and importer are separate explicit commands

The producer's existing Nix output is `packages.x86_64-linux.raw`, with the
corresponding `raw` app. To install that immutable public producer:

```sh
nix profile install 'github:lost-rob0t/fediWatch/ba0cf3b896620c50d0a92daacaa57af6cf0558dd#raw'
```

Review the producer's configuration example and instructions before running it.
An operator must explicitly choose instance DNS hostnames, private output path,
request/time/response/disk limits and local/broader-public timeline scope.
Anonymous API access varies by instance. A software package cannot grant access.
The raw producer fetches no attachments or thumbnails. Original response JSON
retains its media links and source timestamps. No live source is selected by
the import command or by installation.

After an explicitly configured bounded producer run finishes:

```sh
fediWatchRaw --raw-config "$HOME/.config/fediwatch/raw.json"
election26 --config "$HOME/.config/election26/config.json" \
  --archive "$HOME/.local/share/election26/archive" \
  import-spool "$HOME/.local/share/fediwatch/spool" --limit 100
election26 --archive "$HOME/.local/share/election26/archive" status
```

The Election26 configuration must explicitly allowlist the instance hostnames
present in that spool. The source and archive must be separate, non-nested
directories outside every Git checkout. The source must be an existing producer
spool; this command does not create or repair a producer spool.

Import is entirely offline. It does not instantiate an HTTP client, resolve DNS,
fetch media, run normalization, send to a server, install a schedule, or change
source files. It takes a shared nonblocking lock on the producer's existing
`.writer.lock`; a running writer yields exit 75 so a later bounded invocation can
retry. Both tools continue to run locally without GitHub or CI.

## Preserved evidence and resume

- Exact response entity-body bytes and exact original receipt JSON bytes become
  content-addressed objects. Unknown post fields, JSON whitespace, source post
  timestamps and attachment links are not reserialized or inferred.
- The producer's `capture_id`, spool identity, numeric sequence, `observed_at`
  request-start time and `completed_at` time remain unchanged. `captured_at` in
  the base capture row is the known completion time. A separate `imported_at`
  records local import; it is not source time or later Star Server ingestion.
- Repeated equal bodies remain distinct observations. Receipt identity is unique
  per spool/sequence. Restart skips committed identical receipts, rejects changed
  receipts/identity when checked and never overwrites old evidence. New receipts
  take priority over rechecking committed receipts, so a growing history cannot
  consume every pass before new work. Already-imported bodies are not reread;
  opportunistic receipt checks are not a full spool audit.
- The importer verifies a new body against its receipt's length and SHA-256
  before the capture and import checkpoint commit together. Blob/receipt writes
  precede that transaction. A crash may leave an unreferenced object, never a
  falsely committed observation. GC remains reporting-only and preserves it.
- Orphan bodies and `.part` files do not create observations. Partial/error
  receipts are imported as explicit partial/error observations, including empty
  prefixes. They cannot masquerade as successful complete captures.

`manifests/<capture-id>.json` contains a versioned `local_import` extension with
completion/status/times and a link to `manifests/import-<capture-id>.json`. The
import manifest links the exact raw receipt and response object. These are JSON
manifests, not an executable HTML browsing interface. Never publish these local
files, private posts/metadata, WiGLE/KML/KMZ inputs, or route-bearing derivatives.

`status` includes imported and incomplete-import counts and import-metadata
bytes. Original/raw object counters still describe response bodies; operational
receipt/identity blobs are counted separately. `repair` reconstructs missing
capture/import manifests from the catalog. `backup` retains all imported objects,
receipts, identities and checkpoints. Imported metadata objects are referenced
in the dry-run GC inventory.

## Optional normalization and later replay

Explicit `normalize <capture-id>` uses the same exact pinned StarIntel SDK as
other captures. For complete imported HTTP responses it preserves the measured
HTTP status and request-start observation time. Subsecond timestamps remain
verbatim in raw receipts even where canonical UnixTime is integer seconds.
The SDK's existing ID builder incorporates the external capture identity, so two
responses with the same URL/body/timestamp remain distinct canonical documents.
Partial bodies and transport errors stay raw and fail closed at normalization.

This slice does not implement timed replay, a Star Server transport, server
acknowledgement, or downstream ingestion timestamps. Those remain issue #8 work:
explicit time slices, deterministic time/spool/sequence ordering, virtual-clock
timing, and checkpoint advancement only after persisted server readback. Import
success, HTTP acceptance and broker publication are not persistence receipts.

## Bounds and validation limits

The default new-observation limit is `max_urls` (at most 500); `--limit` can only
lower it. Global archive/free-space/per-file limits still apply. New body intake
is at most 64 MiB and may be lower under the archive policy. Receipt/identity JSON
is at most 64 KiB, with strict keys, types, finite numbers, source-route/header
allowlists, relative filenames and duplicate-key rejection. Symlinks, directories
in place of files, FIFOs and source/destination nesting fail closed.

A snapshot may contain at most 100,000 capture-directory entries. Larger or
unscannable snapshots fail explicitly rather than claiming a complete import.
The run budget is checked during directory scans, between receipts and between
body chunks; local filesystem operations/fsync may outlast it if the OS stalls.
This is not a hard real-time filesystem deadline. No producer file is deleted or
split automatically to satisfy a bound.

Tests use locally generated synthetic receipts and bodies based on the pinned
producer contract, including its omitted `local` parameter for broader public
timelines. They cover bytes/timestamps, duplicate observations, locks, corruption,
symlinks, transaction failure, sidecar repair, backup, budgets and no-network CLI
operation. The optional SDK job must exercise the actual generated contract for
status/time/ID mapping. A full installed producer-to-importer round trip and each
platform's exact hosted checks must be reported separately; fixture construction
or source inspection alone is not that runtime proof.
