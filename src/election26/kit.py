"""Foreground source-kit orchestration over the existing collector; no new transport."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from .archive import Archive, inside_git_checkout, status, sync_dir
from .config import BudgetError, BusyError, Config, PolicyError
from .discovery import discover
from .network import PublicHTTP

STATE_KEY = "source_kit_rotation_v1"


def emit(event, **values):
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def safe_path(path):
    """Reject aliases before resolve; existing descendants never become write targets."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PolicyError("Symlink paths are not supported by the archive kit")
    return path


def paths(cwd, home):
    cwd, home = safe_path(cwd), safe_path(home)
    if cwd != home and home not in cwd.parents:
        raise PolicyError("Run under private HOME, outside shared storage")
    if inside_git_checkout(cwd) or any((part / ".git").is_symlink() for part in (cwd, *cwd.parents)):
        raise PolicyError("Run outside any Git checkout; extract the source ZIP instead")
    # Private HOME and the launch directory must be owned and not writable by others.
    directories = [cwd]
    while directories[-1] != home:
        directories.append(directories[-1].parent)
    for directory in directories:
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PolicyError("HOME and launch directory must be owned and not group/world writable")
    archive, config = safe_path(cwd / "archive"), safe_path(cwd / "archive-config.json")
    if config.exists() and (not config.is_file() or config.stat().st_nlink != 1):
        raise PolicyError("Config must be an ordinary unlinked file")
    if config.exists() and (config.stat().st_uid != os.getuid() or config.stat().st_mode & 0o022):
        raise PolicyError("Config must be owned and not group/world writable")
    if archive.exists():
        if not archive.is_dir():
            raise PolicyError("Existing ./archive is not a directory; it was preserved")
        # The core also checks recognized names and symlinks before opening a writer.
        for item in (archive, *archive.rglob("*")):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise PolicyError("Archive contains symlinks or special files; it was preserved")
            if info.st_uid != os.getuid() or info.st_mode & 0o022 or (item.is_file() and info.st_nlink != 1):
                raise PolicyError("Archive contains unsafe ownership, permissions or hard links")
    return archive, config


def validate_config(config):
    if len(config.feeds) > 64 or len(config.manual_urls) > 500:
        raise PolicyError("Kit selection is limited to 64 feeds and 500 manual URLs")
    for url in config.feeds + config.manual_urls:
        config.permit(url)


def prepare(cwd, home, source):
    archive, config_path = paths(cwd, home)
    if not config_path.exists():
        template = safe_path(Path(source) / "config/us-2026-public-starter.json")
        data = template.read_bytes()
        config = Config.load(template)
        validate_config(config)
        try:
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # Another creator won; validate its complete file below, never replace.
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            sync_dir(config_path.parent)
    config = Config.load(config_path)
    validate_config(config)
    # Initializes only recognized empty/existing archives; no network.
    with Archive(archive, config).writer():
        pass
    return archive, config_path, config


def save_state(archive, state):
    archive.transaction_budget(4096)
    with archive.db:
        archive.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (STATE_KEY, json.dumps(state)))


def unfinished_media(archive):
    """Count admitted work not necessarily present in the ordinary URL queue."""
    pending = failed = 0
    if not any(policy.get("download") for policy in archive.config.media_policies.values()):
        return pending, failed
    for reference in archive.db.execute("SELECT * FROM media_references ORDER BY id"):
        previous = archive.db.execute("SELECT payload FROM media_observations WHERE reference_id=? ORDER BY id DESC LIMIT 1",
                                      (reference["id"],)).fetchone()
        outcome = json.loads(previous[0])["state"] if previous else None
        if outcome == "downloaded":
            continue
        policy, _ = archive.reference_admission(reference)
        if policy:
            if outcome == "unavailable":
                failed += 1
            else:
                pending += 1
    return pending, failed


def run_batch(archive, client, report=emit):
    """Discover one complete selected rotation, then drain one bounded fetch pass.

    The same client, request count and deadline cover every source. Checkpoints live
    in the existing transactional catalog; interrupted captures use core recovery.
    """
    config = archive.config
    signature = hashlib.sha256(json.dumps([config.manual_urls, config.feeds]).encode()).hexdigest()
    row = archive.db.execute("SELECT value FROM meta WHERE key=?", (STATE_KEY,)).fetchone()
    state = json.loads(row[0]) if row else {}
    if state.get("signature") != signature or state.get("complete"):
        state = {"signature": signature, "manual_done": False, "feeds_left": len(config.feeds),
                 "source_errors": 0, "source_deferred": 0, "complete": False}
        save_state(archive, state)
    deferred = False
    if not state["manual_done"]:
        result = discover(archive, client, "manual")
        state["manual_done"] = not result["truncated"]
        save_state(archive, state)
        report("manual_discovery", **result)
        deferred = not state["manual_done"]
    while state["manual_done"] and state["feeds_left"]:
        if client.request_count >= config.max_requests or time.monotonic() >= client.run_deadline:
            deferred = True
            break
        before = client.request_count
        result = discover(archive, client, "rss")
        # The core rotates after admission/error; an unadmitted deferred turn stays pending.
        if not result.get("deferred") or client.request_count > before:
            state["feeds_left"] -= 1
            state["source_deferred"] += bool(result.get("deferred"))
        state["source_errors"] += len(result.get("errors", []))
        save_state(archive, state)
        report("rss_discovery", feeds_left=state["feeds_left"], requests=client.request_count, **result)
        if result.get("deferred"):
            deferred = True
            break
    # At most one core fetch call per command, so failed sources are not hot-looped.
    fetched = archive.fetch(client)
    summary = status(archive.root)
    # Core media fetch keeps its URL row pending after an unavailable observation;
    # the media outcome, not that row, decides retry/error semantics. Browser work
    # is also outside the ordinary fetch queue this kit can drain.
    pending = archive.db.execute("SELECT COUNT(*) FROM resources WHERE state IN ('pending','running') "
                                 "AND via NOT IN ('policy-media','browser')").fetchone()[0]
    pending_media, failed_media = unfinished_media(archive)
    deferred = deferred or bool(pending or pending_media) or any(item["state"] == "deferred" for item in fetched)
    failures = state["source_errors"] + summary["errors"] + failed_media + sum(item["state"] == "failed" for item in fetched)
    # A failed source is an attempted rotation, not a successful archive. Permit a
    # fresh discovery cycle next time once this cycle's pending queue is drained.
    state["complete"] = state["manual_done"] and state["feeds_left"] == 0 and not deferred
    save_state(archive, state)
    code = 1 if failures else 75 if not state["complete"] or state["source_deferred"] else 0
    report("batch_finished", exit_code=code, rotation_complete=state["complete"],
           source_errors=state["source_errors"], requests=client.request_count,
           source_deferred=state["source_deferred"],
           pending_media=pending_media, failed_media=failed_media,
           feeds_left=state["feeds_left"], status=summary)
    return code


def brave(archive, client, query):
    if not archive.config.brave_enabled:
        raise PolicyError("Brave requires explicit brave_enabled=true in archive-config.json")
    if archive.config.brave_credential != "env:BRAVE_SEARCH_API_KEY" or not os.environ.get("BRAVE_SEARCH_API_KEY"):
        raise PolicyError("Kit Brave uses only BRAVE_SEARCH_API_KEY from the environment")
    emit("brave_opt_in", notice="One explicit query may incur provider charges; only exact allowlisted results are queued")
    result = discover(archive, client, "brave", query)
    emit("brave_discovery", **result)
    # Keep provider requests separate from automatic public-source rotation.
    return 0


def worker(archive_path, config_path, query=None):
    config = Config.load(config_path)
    validate_config(config)
    with Archive(archive_path, config).writer() as archive:
        client = PublicHTTP(config)
        return brave(archive, client, query) if query is not None else run_batch(archive, client)


def supervise(command, seconds):
    from .browser import wait_unreaped
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        # Do not raise through Popen's fork/exec window before child is assigned.
        stopped = True
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    child = None
    owned = True
    outcome = "interrupted"
    result = 75
    try:
        for signum in signals:
            signal.signal(signum, stop)
        child = subprocess.Popen(command, start_new_session=True)
        deadline = time.monotonic() + seconds
        while not stopped:
            event = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if event is not None:
                outcome = "finished"
                result = event.si_status if event.si_code == os.CLD_EXITED else 75
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "deadline"
                break
            time.sleep(min(0.05, remaining))
    except ChildProcessError:
        # An external SIGCHLD handler reaped it. Never signal a recycled PGID.
        owned = False
    finally:
        # Repeated terminal signals cannot interrupt cleanup. Retain the process
        # leader until every group signal has been sent, then reap exactly once.
        for signum in signals:
            signal.signal(signum, signal.SIG_IGN)
        if child is not None and owned:
            if outcome != "finished":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    wait_unreaped(child.pid, 3)
                except ChildProcessError:
                    owned = False
            # Kill remaining descendants even after the leader exited normally.
        if child is not None and owned:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if outcome != "finished":
        emit(outcome, exit_code=75, notice="Committed data retained. Rerun the same command to resume.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Extracted public source bundle")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-only", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--brave-query")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        cwd = Path.cwd()
        archive_path, config_path = paths(cwd, Path.home())
        if args.worker:
            return worker(archive_path, config_path, args.brave_query)
        if args.status:
            emit("status", archive=str(archive_path), status=status(archive_path))
            return 0
        archive_path, config_path, config = prepare(cwd, Path.home(), args.source)
        emit("ready", archive=str(archive_path), config=str(config_path),
             feeds=len(config.feeds), manual_urls=len(config.manual_urls),
             requests_limit=config.max_requests, acquisition_seconds=config.max_run_seconds)
        if args.prepare_only:
            return 0
        if args.brave_query is not None and (not config.brave_enabled or
                config.brave_credential != "env:BRAVE_SEARCH_API_KEY" or not os.environ.get("BRAVE_SEARCH_API_KEY")):
            raise PolicyError("Enable Brave explicitly and provide BRAVE_SEARCH_API_KEY only in the environment")
        command = [sys.executable, "-m", "election26.kit", "--source", args.source, "--worker"]
        if args.brave_query is not None:
            command += ["--brave-query", args.brave_query]
        return supervise(command, config.max_run_seconds)
    except Exception as exc:
        emit("error", error=str(exc) if isinstance(exc, PolicyError) else type(exc).__name__)
        return 75 if isinstance(exc, (BusyError, BudgetError)) else 2


if __name__ == "__main__":
    sys.exit(main())
