"""Command line entry point. No schedules or network activity run on import."""
import argparse
import json
import sys

from .archive import Archive, gc_plan, status
from .config import Config
from .discovery import discover
from .network import PublicHTTP


def parser():
    cli = argparse.ArgumentParser(prog="election26")
    cli.add_argument("--config", help="JSON config containing references, never secrets")
    cli.add_argument("--archive", required=True, help="Private archive directory outside any Git checkout")
    commands = cli.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="One bounded cron-friendly discovery/fetch pass")
    run.add_argument("--mode", choices=["manual", "rss", "none"], default="manual")
    run.add_argument("--refresh", action="store_true")
    run.add_argument("--extract-media", action="store_true")
    discovery = commands.add_parser("discover", help="Queue bounded allowlisted candidates")
    discovery.add_argument("--mode", choices=["manual", "rss", "brave"], default="manual")
    discovery.add_argument("--query", help="Explicit Brave query; config must also enable API usage")
    fetch = commands.add_parser("fetch", help="Archive pending URLs, or explicit URLs")
    fetch.add_argument("urls", nargs="*")
    fetch.add_argument("--refresh", action="store_true", help="Capture new observations, retaining prior versions")
    fetch.add_argument("--extract-media", action="store_true", help="Queue allowlisted direct media links from archived HTML")
    normal = commands.add_parser("normalize", help="Optional canonical StarIntel HTTP metadata only")
    normal.add_argument("capture_id", type=int)
    commands.add_parser("status", help="Read committed aggregate counters")
    commands.add_parser("gc", help="Dry-run retention plan only; never deletes data")
    commands.add_parser("repair", help="Rebuild missing JSON manifests from the transactional catalog")
    backup = commands.add_parser("backup", help="Copy a consistent local archive snapshot to a new directory")
    backup.add_argument("destination")
    trans = commands.add_parser("transcribe", help="Local Whisper only; no source media is uploaded")
    trans.add_argument("sha256")
    trans.add_argument("--model-path", help="Pre-downloaded trusted Whisper model checkpoint")
    trans.add_argument("--language")
    trans.add_argument("--verify", action="store_true", help="Assert you reviewed transcript accuracy and verify checksums")
    stats = commands.add_parser("stats", help="Preview aggregate Discord payload")
    stats.add_argument("--send", action="store_true", help="Send once only when explicitly enabled and configured")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = Config.load(args.config)
        if args.command == "status":
            result = status(args.archive)
        else:
            with Archive(args.archive, config).writer() as archive:
                client = PublicHTTP(config)
                if args.command == "run":
                    from .runner import run_pass
                    result = run_pass(archive, client, args.mode, args.refresh, args.extract_media)
                elif args.command == "discover":
                    result = discover(archive, client, args.mode, args.query)
                elif args.command == "fetch":
                    result = archive.fetch(client, args.urls, args.refresh, args.extract_media)
                elif args.command == "normalize":
                    from .normalize import normalize
                    result = normalize(archive, args.capture_id)
                elif args.command == "gc":
                    result = gc_plan(archive)
                elif args.command == "repair":
                    archive.repair_manifests()
                    result = {"manifests_repaired": True}
                elif args.command == "backup":
                    result = archive.backup(args.destination)
                elif args.command == "transcribe":
                    from .transcribe import transcribe, verify_transcript
                    if args.verify:
                        result = verify_transcript(archive, args.sha256)
                    elif not args.model_path:
                        raise ValueError("Provide --model-path or use --verify after reviewing an existing transcript")
                    else:
                        result = transcribe(archive, args.sha256, args.model_path, args.language)
                elif args.command == "stats":
                    from .reporting import publish
                    result = publish(archive, client, args.send)
        print(json.dumps(result, indent=2, sort_keys=True))
        items = result if isinstance(result, list) else result.get("fetch", [])
        discovery_result = result.get("discovery", {}) if isinstance(result, dict) else {}
        if any(item.get("state") == "failed" for item in items) or discovery_result.get("errors") or (isinstance(result, dict) and result.get("errors")):
            return 1
        if any(item.get("state") == "deferred" for item in items) or discovery_result.get("deferred") or (isinstance(result, dict) and result.get("deferred")):
            return 75
        return 0
    except Exception as exc:
        # Exception strings from third-party libraries can contain credentials.
        # Known local policy messages are safe, but never print provider exceptions.
        from .config import PolicyError, BusyError, BudgetError
        message = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
        print(json.dumps({"error": message}), file=sys.stderr)
        return 75 if isinstance(exc, (BusyError, BudgetError)) else 2


if __name__ == "__main__":
    sys.exit(main())
