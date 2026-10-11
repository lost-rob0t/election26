# Termux: private local installation

Install Termux from a source listed by the [official Termux project](https://github.com/termux/termux-app#installation), then put this public source checkout inside Termux's private `$HOME`. Do not use shared Android storage for executable code or raw archives. Review scripts before applying them.

```sh
cd "$HOME/election26"
bash scripts/install-termux.sh                    # preview only
bash scripts/install-termux.sh --install          # base collector
# Optional tools, only if wanted:
bash scripts/install-termux.sh --install --with-yt-dlp
# --with-ffmpeg and --with-cron install those packages; neither is the default.
```

The installer uses official configured Termux repositories, installs only missing packages, and installs this source into a managed virtual environment. It never upgrades Termux's global pip. Interrupted managed-venv setup can be retried. Re-running preserves the existing config, archive and unrelated files; an unmanaged preexisting venv/launcher or symlink path is refused.

Paths:
- CLI: `$HOME/.local/bin/election26`
- Config: `$HOME/.config/election26/config.json` (created once, mode 0600)
- Raw archive: `$HOME/.local/share/election26/archive` (private, outside Git)
- Venv: `$HOME/.local/share/election26/venv`

The initial host allowlist is empty. Review disk/free-space budgets and configure only selected public sources, then run locally:

```sh
"$HOME/.local/bin/election26" \
  --config "$HOME/.config/election26/config.json" \
  --archive "$HOME/.local/share/election26/archive" run --mode manual
"$HOME/.local/bin/election26" \
  --archive "$HOME/.local/share/election26/archive" status
```

No GitHub account, CI service, central server, paid API, or canonical SDK is required at runtime. Raw capture runs before optional normalization. HTML/JSON keeps the source's embedded media links by default. Bulk media downloads are not part of phone setup; per-source type/size policy is a separate planned slice. Transcription never triggers an implicit download.

## Optional yt-dlp and scheduling

`--with-yt-dlp` installs the official [`python-yt-dlp` Termux package](https://github.com/termux/termux-packages/blob/master/packages/python-yt-dlp/build.sh). It installs a tool; it does not wire unrestricted yt-dlp requests into the archive. The collector's exact-host, robots, deadline, disk and provenance checks remain in force for its own HTTP path. A future downloader adapter must preserve these boundaries before being exposed. Segmented streams, site-specific extraction, login and anti-bot bypass are not enabled here.

`--with-cron` installs the official [`cronie` package](https://github.com/termux/termux-packages/blob/master/packages/cronie/build.sh) only. It does not start a daemon, modify a crontab, enable boot startup, acquire a wake lock, or change battery/storage permissions. Use the existing [cron preview/opt-in guide](CRON.md) with absolute private paths if scheduling is wanted. Android may kill background processes or suspend them during sleep; emulator fixture success does not establish all-day physical-phone reliability. Durable checkpoints let a later invocation resume.

## What CI proves

`.github/workflows/termux.yml` launches Android API 28 x86_64 on a GitHub-hosted Intel macOS emulator host. Tests execute **inside the Android Termux app**, not in a Linux container or on the macOS host. The job:
1. Downloads the versioned official Termux GitHub debug APK (v0.118.3), verifies it against that release's SHA-256 manifest, and logs the hash.
2. Uses `adb run-as` in the disposable debuggable emulator app, without enabling external-app access or touching a phone.
3. Installs twice, verifies unchanged config and no default cron installation, then runs the fixture suite with Android SDK/ABI/Python evidence.
4. Runs yt-dlp against one generated 0.1-second WAV file, with user config/plugins disabled, an explicit fixture-only file-URL opt-in, 30-second timeout and size bound, and verifies exact bytes.

The APK checksum manifest is fetched from the same official versioned release; it is a download-integrity check, not an independent signing trust root. GitHub debug APKs use a public test signing key and are used here only for disposable CI. Use the official installation guidance for your real device, not this CI APK harness.

Only a successful exact-head Android job is evidence of that tested configuration. Linux tests alone are not Android verification. Physical phones, ARM64, newer Android versions, background scheduling endurance, real site downloads and Whisper inference remain separate validation targets. CI makes no live source collection and deploys nothing to a user device.

References: [yt-dlp Android installation](https://github.com/yt-dlp/yt-dlp/wiki/Installation#android), [Termux release](https://github.com/termux/termux-app/releases/tag/v0.118.3), [emulator runner](https://github.com/ReactiveCircus/android-emulator-runner), [GitHub runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
