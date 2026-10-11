# Reproducible tools with Nix

This flake packages the local pipeline and optional public dependencies for
`x86_64-linux`. Installing, building, entering a shell, or running checks does
not start collection, install cron, download a Whisper model, configure a secret,
post a message, or deploy a service. The existing [Termux path](TERMUX.md) is
unchanged. Linux Nix results are not Android, ARM64, or macOS Nix evidence.

## Install once, run locally

Use Nix with `nix-command` and `flakes` enabled. Choose and review a full commit
SHA from this repository, then substitute it for `REVIEWED_COMMIT`:

```sh
nix profile install 'github:lost-rob0t/election26/REVIEWED_COMMIT#election26'
election26 --help
election26 --archive "$HOME/.local/share/election26/archive" status
```

The installed command uses its pinned store closure; it needs neither GitHub nor
CI at runtime. Initial source/package downloads need network access. Public-source
collection, if the operator separately starts it, naturally needs its approved
sources. Use `nix run ...#election26 -- --help` for a one-off app invocation; that
may resolve/download the flake first, so a profile install is the offline path.

Create/review configuration outside the checkout, for example at
`$HOME/.config/election26/config.json`. Start from the documented public example,
keep permissions private, and follow [USAGE](../USAGE.md) for explicit commands.
The default package requires only Python's standard library. A status check on a
nonexistent archive does not create it.

## Outputs

- `election26` / `default`: CLI plus `election26-cron` and `election26-fec-target`.
- `election26-starintel`: CLI with the real pinned canonical SDK.
- `election26-keyring`: CLI with the optional keyring import. An unlocked backend
  is separately configured by the operator; no credentials are stored at build.
- `election26-whisper`: CLI with local Whisper and its runtime dependencies.
- `election26-full`: all three Python extras together.
- `cron` and `fec-target`: package aliases for the core tools, with dedicated apps.
- `starintel-doc`, `keyring`, `whisper`: individual upstream public packages.
- `yt-dlp`, `ffmpeg`, `media-tools`: individual or combined explicit media tools.
  They are available to an operator; this does not integrate third-party
  downloaders into the pipeline or bypass its source-host/media policy.
- `devShells.x86_64-linux.default`: core CLI, Python build tools, Git and Bash.
- `devShells.x86_64-linux.full`: full Python profile plus media tools.

Install one CLI variant rather than conflicting variants in the same profile.
For example, `nix profile install <pinned-flake>#election26-full`; add the separate
`media-tools` package only if needed. All variant apps have their package's name.

The cron app previews by default:

```sh
nix run 'github:lost-rob0t/election26/REVIEWED_COMMIT#cron' -- \
  --executable "$HOME/.nix-profile/bin/election26" \
  --config "$HOME/.config/election26/config.json" \
  --archive "$HOME/.local/share/election26/archive" \
  --log "$HOME/.local/state/election26/run.log"
```

No build or check passes `--install`. The existing helper's explicit `--install`
mode remains available only when an operator deliberately invokes it and has
reviewed their existing crontab. Read [CRON.md](CRON.md) before scheduling.

Whisper checks import the engine but never load or fetch a model. Transcription
still requires an already-downloaded trusted local model outside the archive,
and mono 16 kHz 16-bit PCM WAV within the pipeline's bounded limits. Building
FFmpeg does not change that input contract. Model inference is not covered by
these checks; there is no paid transcription API.

## External FEC target interface

No FEC engine is copied into this repository. The public flake never fetches,
builds, embeds, or caches private actor source or a private binary closure.
An operator who already has an approved executable (including one installed from
their own separately managed private package) may opt in at runtime:

```sh
export ELECTION26_FEC_EXECUTABLE=/absolute/path/to/existing/sa-fec
election26-fec-target -- --help
```

The bridge requires an absolute executable path and explicit arguments after
`--`; it preserves those arguments literally, uses no shell, and propagates the
external process's exit status. Missing tools fail clearly. It adds no server,
credentials or options, and never installs or fetches that tool. Keep any private
packaging/source and credential setup separate from this public flake.

The current external `sa-fec` interface constructs Star Server targets: without
`--server` it prints a request; with a server it submits asynchronous work and
prints a receipt. Neither result is proof of downloaded raw FEC records. Review
the external tool's help and authorize its network/submission behavior separately.
Tests use a fake executable and synthetic arguments, never the real producer.

## Inputs, schema and licensing

`flake.lock` records hashes generated by Nix for immutable public inputs:

- Nixpkgs `c59305bab2065cfecc4944690d9eedbb56f3a9fa`; Python 3.12, keyring 25.7.0,
  OpenAI Whisper 20250625, and yt-dlp 2026.08.19 come from this snapshot.
- `lost-rob0t/starintel-doc` `6aa8051dc7ea560943a60a25e1d600467f2778bc`, package
  0.10.1; its Nixpkgs input follows this flake's Nixpkgs. The existing public
  upstream derivation is reused with one packaging-only provenance receipt. The upstream schema pin (`9198370`)
  and vendored contracts are unchanged; no schema is copied or hand-edited here.

Nix's wheel installer does not emit pip VCS `direct_url.json`. The upstream
package override adds `election26_nix_source.json` to its distribution metadata,
using the actual flake input revision and NAR hash. The adapter accepts that
separate receipt only under `/nix/store` with the exact repository/revision and
expected format; a conflicting pip commit still fails closed. Existing pip
installs must retain the exact VCS commit. This is packaging provenance, not a
signature against a malicious local package/store administrator; no pip VCS
installation claim is fabricated.

The SDK has MIT metadata/classifiers but lacks an explicit root license file at
that revision. This flake consumes the pinned package; it does not resolve that
upstream licensing caveat or assign a license to this repository. Review the
upstream terms before redistribution. Public source availability alone is not a
license grant.

## Store and source boundaries

Nix store paths are normally readable by other local users and can be cached.
Never put credentials, runtime config, raw originals, transcripts, models,
WiGLE/KML/KMZ files, routes, or generated archives into a Nix expression or its
source directory. Never pass a secret as a Nix string, build argument or store
path. Use runtime environment/keyring references and private paths instead.

`nix/source-files.json` explicitly allowlists the files passed to the Python
build. Only code, tests, helper scripts, packaging metadata and the known public
example config needed by synthetic installer tests are included. Arbitrary
extra Python files, local config, raw directories, caches and symlinks are
excluded. A Nix check verifies that copied files exactly match the allowlist and
that representative private paths/symlinks fail closed. When adding public code
or tests, review and update this list deliberately.

**This filter cannot protect a contaminated flake root:** Nix copies a flake's
source before evaluating derivations. Tracked private files in a Git flake, or
files in a non-Git/path flake, can enter the store before this filter runs. Keep
all private material outside the checkout even if Git ignores it. Prefer the
immutable remote-flake install above and run `scripts/check-public-tree.py`
before publishing. Neither the allowlist nor the Git guard can detect every
secret hidden in otherwise approved source bytes.

## Verification

From a clean public-only checkout with a committed lock:

```sh
nix flake check --no-write-lock-file --print-build-logs
nix build --no-write-lock-file --no-link \
  .#election26 .#election26-starintel .#election26-keyring \
  .#election26-whisper .#election26-full .#cron .#fec-target \
  .#starintel-doc .#keyring .#whisper .#yt-dlp .#ffmpeg .#media-tools
nix develop --no-write-lock-file --command python -c 'import election26'
```

The hosted Nix job builds these outputs, runs the synthetic unit suite for each
CLI variant (including the real SDK contract in SDK profiles), verifies installed
commands from outside the source checkout, imports selected extras, exercises
safe status/cron preview, and invokes only version/help for standalone keyring,
Whisper and media tools. Shell startup has no hook. Existing Python/SDK and real Android Termux
jobs remain independent gates. Check the exact commit's Actions results before
claiming a build passed; source review or Linux Python tests alone are not a Nix
build. No Android Nix, ARM64, live collection, real FEC submission, keyring backend
unlock, or Whisper inference is claimed here.
