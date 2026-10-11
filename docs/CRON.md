# Cron is the deployment model

Run short-lived, bounded invocations from a local crontab. There is no resident
daemon. Persistent SQLite checkpoints, immutable blobs, and URL deduplication
survive process exit; subsequent invocations continue pending work.

```sh
/path/to/venv/bin/election26 --config /private/config.local.json --archive /private/election26 run --mode rss
```

Each pass respects `max_urls` (default 100), `max_requests` (default 200),
`max_run_seconds` (default 300), per-file bytes, archive budget, disk reserve,
robots rules, and an exclusive process lock. Use a private local filesystem;
network/NFS filesystem locking/durability is not certified. DNS resolution is
subject to the operating system's resolver timeout; the socket watchdog applies
to HTTP connection/header/body I/O. No new request begins after the run deadline.
An overlapping writer exits 75; no second collector runs concurrently.

Exit codes: 0 successful pass (including no new work), 1 one or more fetches
failed, 2 invalid configuration/other error, 75 temporary overlap or exhausted
run budget. Logs are JSON counters/result summaries and sanitized error classes.
Cron has a minimal environment: use absolute paths and a securely provisioned
credential reference; never put secrets in the cron command.

## Preview or install a crontab block

```sh
python scripts/cron.py \
  --executable /absolute/venv/bin/election26 \
  --config /private/config.local.json \
  --archive /private/election26 \
  --log /private/logs/election26.log \
  --mode rss
```

The default only prints a block. Create the private log directory yourself and
arrange log rotation. Inspect the output and add it with `crontab -e`, or rerun
with `--install` to append it to the current user's readable crontab. The installer
preserves existing entries and refuses duplicate managed blocks. If no readable
crontab exists, it stops and asks the operator to initialize it rather than
silently replacing anything. This repository never installs cron automatically. The generated block does not
grant media download permission. Keep `media_policies` empty for links-first
collection; adding an explicit source policy allows its selected bounded media
requests on subsequent ticks. See [media policy](MEDIA.md) and the [Termux guide](TERMUX.md) for Android
process-lifetime limits before relying on a phone schedule.

The example runs collection every five minutes and includes a commented
15-minute `stats --send` line. Enable that line only after the Discord destination,
permission and local secret are configured. Statistics use committed counters,
mention suppression, a persisted interval limit, and uncertain-send protection.

`run --mode none` drains the existing queue only. `run --mode manual` queues the
configured URL list. `run --mode rss` captures feed originals and queues allowed
candidate URLs. `--refresh` explicitly revisits completed URLs; without it,
completed items are skipped. A limited pass may leave queued media for the next
cron tick. Work beyond a cap is deferred, not silently treated as complete.

Discovery/fetch starting order rotates durably between passes, so a feed cannot
consume every tick's request allowance while queued articles starve. One RSS feed
is visited per discovery pass, with a checkpointed rotating feed position.
Already queued links do not consume the new-candidate allowance, and individual
feed failures are isolated, and untouched
pending URLs are attempted before retrying failed items. Retry attempts are
ordered fairly rather than repeatedly selecting only the oldest broken URL.
