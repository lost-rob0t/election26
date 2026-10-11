# Termux archive ZIP: one command

Extract the ZIP into a folder under Termux's private home directory, then open that
extracted folder in Termux. Do not run from Android Downloads/shared storage or a
Git checkout. From the extracted folder, run:

```sh
bash ./start-archive.sh
```

That command installs the existing collector if needed, creates a literal
`./archive` directory relative to **the directory you launched from**, creates
`./archive-config.json` only if absent, and starts one bounded foreground pass.
It uses an absolute managed Python path; no shell profile or PATH edit is needed.
It does not install a cron job, daemon, browser, media downloader or transcription
model. Keep the Termux session open while it runs. Android may stop background apps.

The ZIP is self-contained **source**, not a bundled Python/Android runtime or an
offline package mirror. Initial setup needs internet for official Termux packages
(`python`, `python-pip`, `python-ensurepip-wheels`) and Python build dependencies
from PyPI. Use an official current Termux app; see [Termux installation guidance](https://github.com/termux/termux-app#installation).
The included installer never upgrades system pip. Once the exact bundled collector
is installed, its Python-file hashes and source receipt are checked so reruns do
not reinstall or download build dependencies unnecessarily. Missing system packages
or changed source still require setup. Setup time is separate from collection time.

## What it archives

- Seven selected public RSS feeds: GovInfo bill XML/status XML, Ballotpedia,
  DCCC, NRCC, Libertarian Party and Green Party press releases.
- Four manual reference URLs: FEC announcements, FEC election-dates PDF, FEC
  deadlines, and the Columbus council calendar.
- Original permitted feed/page bytes, checksums, provenance and discovered media
  references, through the existing election26 collector.
- Media links by default. Audio, video, image **and PDF bodies** remain off unless
  your private config explicitly opts in per source. The FEC PDF is therefore a
  recorded document link by default, not a downloaded PDF.

This is a selected starter, not nationwide completeness. Party feeds are partisan
primary statements. Feed windows are small, the Green feed was stale at verification,
and missing or blocked sources are not bypassed. Dates in config guide optional
search; the collector preserves raw feeds/pages without claiming that every item
falls within 2026. See [exact sources and gaps](docs/PUBLIC-SOURCES.md).

## Bounds, progress and resume

The default acquisition command shares one **160-request / 300-second** budget
across the selected source rotation and fetch work. Robots requests and redirects
count toward the request cap. An outer supervisor terminates the child at the time
limit, with up to three seconds of termination grace, including blocked DNS calls.
Preparation, archive validation and initial dependency setup happen before that
acquisition timer. Disk limits default to a 2 GiB archive, 16 MiB per ordinary file,
and a 1 GiB free-space reserve. Feed parsing has the existing 4 MiB cap.

Progress is printed as JSON events: ready paths/limits, each RSS discovery result,
remaining feed turns, then queue and archive counters. The command first queues
manual references and visits rotating feeds, then makes at most one core fetch pass
(up to 120 URLs, further limited by the shared time/request/disk budgets). A large
initial queue will require more than one command. It never loops forever.

Run the **same command from the same directory** to continue. The catalog keeps
the source-rotation checkpoint and pending URLs. Committed original objects are
retained; repeated fetches skip completed URLs. When a selected rotation and its
pending queue finish, the next command starts another feed observation cycle.
Manual URLs already captured are not automatically refreshed. To deliberately
capture a changed manual page, use the existing CLI's explicit `fetch --refresh`
with its exact URL and this archive/config. See [collector usage](USAGE.md).

Exit codes:

- `0`: selected rotation finished with no reported source errors or pending fetches
- `75`: work deferred by bounds, busy archive or interruption; run again
- `1`: one or more sources failed/blocked; inspect printed source/queue counts and
  source policy. Other permitted work may have succeeded. Reruns can retry it.
- `2`: setup, invalid config or unsafe path; correct the reported problem first

Mixed failures and deferred work use exit `1`; the counters still show unfinished
work. A partial feed attempt never turns into a success merely because the cursor
moved on. An exit `0` does not mean every URL on the internet, feed history, manual
link target or linked media body was archived. No command automatically deletes
old captures. Retention is a review/dry-run setting, not automatic deletion.

## Private files and optional commands

Existing `archive-config.json` is read unchanged. The supplied profile is used only
for initial creation. A directory called `archive` containing unrecognized files,
symlinks, unsafe writable paths or hard-linked files is refused without replacing
it. Do not put private config or collected files into the public repository.
The installer also preserves its earlier home-directory config/archive; this kit
explicitly selects the new launch-directory archive instead.

```sh
bash ./start-archive.sh --prepare-only
bash ./start-archive.sh --status
```

Preparation performs setup and creates private config/catalog without collection.
Status performs setup if needed and reports existing aggregate counters without
collection. Custom source URLs and exact hosts belong in your private config;
review them first. The launcher never broadens the allowlist from discovered links.
See [media opt-in policy](docs/MEDIA.md) before enabling downloads.

Brave is optional and may incur provider charges. It is never called by the default
command, even if a key exists or config has Brave enabled. To use it, explicitly set
`brave_enabled` to `true` in your private config, keep its credential reference as
`env:BRAVE_SEARCH_API_KEY`, and supply the key only in that environment variable.
Never put the value in this ZIP, config, source control or command arguments. Then:

```sh
bash ./start-archive.sh --brave-query 'US election 2026 official election dates'
```

That explicit command makes one bounded discovery query (at most 20 provider
results), queues only already-allowlisted URLs, and stops. It does not change the
default source rotation or fetch unreviewed hosts. Run the default command to fetch
permitted queued pages. No key is needed for the default RSS/manual archive.

## Source and verification

The ZIP includes `SOURCE-PROVENANCE.json`, an exact public source commit and
`SHA256SUMS` for its contents. The accompanying `.zip.sha256` checks the complete
ZIP. `source-kit-files.json` is the explicit packaging allowlist; only approved
public source/docs, reference URLs, metadata-only source probes and synthetic tests
are included. There are no collected raw pages/media, private actor packages,
location datasets or credentials inside.

Tests never run live collection. The actual Android/Termux lane installs in a
disposable emulator, runs synthetic collector/rotation fixtures, extracts a source
ZIP, and runs this exact entrypoint twice using an empty synthetic source config.
It checks the literal archive path, paths with spaces, unchanged config, a simulated
offline setup failure, and no automatic cron. This does not establish live source
availability from every phone, ARM64 behavior, Android background reliability, or
national coverage. Public source probes are dated point-in-time metadata checks.
