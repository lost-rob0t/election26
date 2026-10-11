#!/usr/bin/env python3
"""Preview a local cron block; install only with an explicit --install flag."""
import argparse
import pathlib
import shlex
import subprocess

START = "# BEGIN election26 managed cron"
END = "# END election26 managed cron"


def block(executable, config, archive, log, mode):
    for value in (executable, config, archive, log):
        if "\n" in value or "\r" in value or "%" in value:
            raise ValueError("Cron paths must not contain newline or percent characters")
        if not pathlib.Path(value).is_absolute():
            raise ValueError("Cron paths must be absolute")
    base = " ".join(shlex.quote(x) for x in [executable, "--config", config, "--archive", archive])
    logfile = shlex.quote(log)
    return "\n".join([
        START,
        "# Bounded jobs; existing checkpoints/dedup survive process exits.",
        f"*/5 * * * * {base} run --mode {shlex.quote(mode)} --extract-media >>{logfile} 2>&1",
        "# Uncomment only after Discord destination/secrets/permission are configured:",
        f"# */15 * * * * {base} stats --send >>{logfile} 2>&1",
        END,
    ])


def combine(existing, generated):
    if START in existing or END in existing:
        raise ValueError("An election26 managed cron block already exists; review/edit it manually")
    return existing.rstrip() + ("\n\n" if existing.strip() else "") + generated + "\n"


def main():
    parser = argparse.ArgumentParser()
    for name in ("executable", "config", "archive", "log"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", choices=["manual", "rss", "none"], default="rss")
    parser.add_argument("--install", action="store_true", help="Explicitly append this block to the current user's crontab")
    args = parser.parse_args()
    generated = block(args.executable, args.config, args.archive, args.log, args.mode)
    print(generated)
    if args.install:
        current = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=False)
        if current.returncode != 0:
            # Missing crontabs differ across platforms; avoid silently replacing
            # an unreadable table or guessing based on localized error text.
            raise SystemExit("Could not read the existing crontab. Initialize/review it with crontab -e, then retry.")
        updated = combine(current.stdout, generated)
        subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
        print("Cron block installed. Existing entries were preserved.")


if __name__ == "__main__":
    main()
