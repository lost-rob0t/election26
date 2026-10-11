"""Fair, bounded cron passes with durable phase rotation."""
from .archive import status
from .config import BudgetError
from .discovery import discover


def run_pass(archive, client, mode="manual", refresh=False, extract_media=False):
    row = archive.db.execute("SELECT value FROM meta WHERE key='next_phase'").fetchone()
    first = row[0] if row and row[0] in {"discover", "fetch"} else "discover"
    phases = [first, "fetch" if first == "discover" else "discover"]
    # Persist the next starting phase before work, including interrupted passes.
    archive.transaction_budget(1024)
    with archive.db:
        archive.db.execute("INSERT OR REPLACE INTO meta VALUES ('next_phase',?)", (phases[1],))
    result = {"discovery": {"queued": 0}, "fetch": []}
    for phase in phases:
        try:
            if phase == "discover" and mode != "none":
                result["discovery"] = discover(archive, client, mode)
            elif phase == "fetch":
                result["fetch"] = archive.fetch(client, refresh=refresh, extract_media=extract_media)
        except BudgetError:
            result["deferred"] = True
            break
    result.update({"requests": client.request_count, "status": status(archive.root)})
    return result
