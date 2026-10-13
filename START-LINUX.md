# Desktop Linux archive kit (including Arch Linux)

Extract the ZIP below your own HOME, outside any Git checkout, and enter
`election26-linux-archive-kit`. Keep this directory and its archive on a private
local filesystem. HOME and its path to the launch directory must be owned by you
and not writable by other users. Do not extract an update over existing data:
extract to a new directory and invoke its launcher from your existing archive
working directory.

## Start, status and resume

With Bash and Python 3.11 or newer installed:

```sh
bash ./start-linux-archive.sh
bash ./start-linux-archive.sh --status
bash ./start-linux-archive.sh
```

The third command resumes another bounded pass. Ctrl-C stops the foreground run;
committed raw bytes, provenance and checkpoints remain. Run from the same directory
to resume the same archive. There is no activation, pip install, background service
or cron installation. A private temporary copy of package code under HOME keeps\narchive data outside the running source tree and is cleaned after each run. On Arch, if Python is missing, install it yourself with
`sudo pacman -S python`. The launcher never runs a package manager.

For the pinned Nix runtime on x86_64 Linux, with Nix already installed:

```sh
bash ./start-linux-archive.sh --nix
bash ./start-linux-archive.sh --nix --status
```

This selects Python 3.12 from nixpkgs commit
`c59305bab2065cfecc4944690d9eedbb56f3a9fa`, the same pin as the repository flake.
Initial Nix downloads need internet; the normal Python path needs no setup network.
Nix receives only that remote runtime reference, never this archive directory.
Other architectures are not claimed as Nix-tested.

## Configuration and collection limits

To inspect configuration before any collection:

```sh
bash ./start-linux-archive.sh --prepare-only
```

The shared collector creates literal `./archive` and create-only
`./archive-config.json`. Existing configuration and archive data are preserved.
The original starter manifest is unchanged: seven public feeds and four manual
references, with a shared default budget of 160 requests and 300 seconds per pass.
See [public source scope and coverage gaps](docs/PUBLIC-SOURCES.md).
This is a starter archive, not nationwide completeness or guaranteed source availability.

Media stays links-only by default. Per-source downloads require explicit policy
in your own configuration; see [media instructions](docs/MEDIA.md).
Optional Brave requires `brave_enabled=true`, the configured environment credential,
and a separate explicit invocation:

```sh
bash ./start-linux-archive.sh --brave-query 'your public-source query'
```

Provide `BRAVE_SEARCH_API_KEY` through your own runtime environment only; never
put a key in command arguments, the ZIP or a config file. No credential setup is
performed by this kit. The default command never calls Brave.

Exit 0 means the selected rotation completed, 75 means bounded/deferred/interrupted
work should be resumed, 1 reports acquisition failures, and 2 reports invalid
configuration or setup. Review the JSON status/events; success does not establish
that every source is available or that coverage is complete.

## Verify and reproduce

`SHA256SUMS` records every included source file plus source provenance.
From the extracted root, run `sha256sum -c SHA256SUMS` before first use.
Runtime files are deliberately not included in those checksums.

From the exact commit listed in `SOURCE-PROVENANCE.json`, build outside the source tree:

```sh
python3 scripts/build-source-kit.py --platform linux --commit FULL_SOURCE_COMMIT --output ../election26-linux-archive-kit.zip
```

The shared builder uses the explicit public source allowlist, fixed ZIP metadata
and sorted entries. No runtime data or credentials are packaged.
The original Termux entrypoint and its documentation remain for source compatibility;
desktop users should use this document and `start-linux-archive.sh`.
