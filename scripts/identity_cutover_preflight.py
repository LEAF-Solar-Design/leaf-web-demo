"""Refuse the identity cutover until it is safe to take.

Issue #304. Moving the authored-tool lifecycle to the active platform binding
changes the tenant string every route derives. Two things must be true at the
moment of deploy, and neither is checkable after the fact:

  1. NO JOB IS IN FLIGHT. A job row created before the cutover carries the raw
     claim as its tenant_id. Restart recovery resubmits that identity and the
     broker still executes it against the old checkout, but polling, streaming
     and close now authorise only the resolved id, so the submitter gets 404 and
     cannot cancel. A live APS run would keep billing with no way to stop it
     from the product. Draining is the cheapest correct answer: let the queue
     empty, then cut over.

  2. NO BROKER RECORD IS KEYED BY A CLAIM. _provisioned_tier returns None for an
     unknown key and _tenant_tier falls back to DEFAULT_TIER, which grants every
     capability except platform_customize. A record that TIGHTENS a tenant would
     stop applying, so re-keying silently WIDENS access. Run
     scripts/rekey_broker_tenants.py first; this only verifies the result.

Exit 0 means READY. Any other exit means NOT-READY and names why. Nothing here
mutates anything.

    python scripts/identity_cutover_preflight.py
    python scripts/identity_cutover_preflight.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

def _server_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "server"


def check_jobs_drained() -> Dict[str, Any]:
    """No job may be in flight when the identity under it changes.

    Liveness is "not terminal", taken from jobs.TERMINAL rather than a list of
    state names written here, so a new non-terminal state cannot quietly read as
    drained.
    """
    sys.path.insert(0, str(_server_dir()))
    try:
        import jobs  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"name": "jobs drained", "ready": False,
                "detail": f"job store unreadable, cannot prove the queue is "
                          f"empty: {exc}"}
    page = 200  # list_jobs clamps to 200
    try:
        rows = jobs.list_jobs(limit=page)
    except Exception as exc:  # noqa: BLE001 - unreadable is NOT drained
        return {"name": "jobs drained", "ready": False,
                "detail": f"could not enumerate jobs: {exc}"}
    terminal = {str(state).lower() for state in getattr(jobs, "TERMINAL", ())}
    if not terminal:
        return {"name": "jobs drained", "ready": False,
                "detail": "jobs.TERMINAL is empty; refusing to guess which "
                          "states count as finished"}
    live = [{"job_id": row.get("job_id"), "tenant_id": row.get("tenant_id"),
             "status": str(row.get("status") or "").lower()}
            for row in rows
            if str(row.get("status") or "").lower() not in terminal]
    truncated = len(rows) >= page
    if not live and truncated:
        return {"name": "jobs drained", "ready": False,
                "detail": f"the newest {page} jobs are terminal but the listing "
                          "was truncated, so an older live job cannot be ruled "
                          "out; drain and re-run"}
    return {
        "name": "jobs drained",
        "ready": not live,
        "detail": ("the queue is empty" if not live else
                   f"{len(live)} job(s) still in flight; their tenant_id is the "
                   "pre-cutover identity, so after the cutover the submitter "
                   "could neither poll nor cancel them"),
        "live_jobs": live[:20],
    }


def _broker_mode() -> str:
    return os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower()


def _check_broker_postgres() -> Dict[str, Any]:
    """Every row in the broker_tenants TABLE must be keyed by a platform id.

    The JSON file is only one of two authorities. With LEAF_BROKER_STORE
    =postgres the broker reads disables and tiers from broker_tenants
    (server/broker_pg_store.py), and an earlier version of this gate reported
    READY for that configuration purely because the JSON file was absent. A
    claim-keyed disable or restricted tier would have survived the cutover while
    the gate said it was safe, which is worse than having no gate.
    """
    sys.path.insert(0, str(_server_dir()))
    try:
        import broker_pg_store  # noqa: PLC0415
        import platform_link  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"name": "broker records re-keyed (postgres)", "ready": False,
                "detail": f"postgres broker authority is selected but "
                          f"unreadable, so it cannot be proven re-keyed: {exc}"}
    try:
        store = broker_pg_store.store(platform_link.platform_db())
        rows = store.tenant_ids()
    except AttributeError:
        return {"name": "broker records re-keyed (postgres)", "ready": False,
                "detail": "cannot enumerate broker_tenants from this build; "
                          "check it by hand before cutting over rather than "
                          "treating unknown as safe"}
    except Exception as exc:  # noqa: BLE001 - unreadable is NOT re-keyed
        return {"name": "broker records re-keyed (postgres)", "ready": False,
                "detail": f"could not read broker_tenants: {exc}"}
    stale = [key for key in rows if not _UUID.match(str(key).strip())]
    return {
        "name": "broker records re-keyed (postgres)",
        "ready": not stale,
        "detail": ("every broker_tenants row is keyed by a platform id"
                   if not stale else
                   f"{len(stale)} broker_tenants row(s) still keyed by a claim"),
        "stale_keys": stale[:20],
    }


def check_broker_records_rekeyed() -> Dict[str, Any]:
    """Every broker tenant record must already be keyed by a platform id.

    Checks the authority the broker ACTUALLY reads. Under postgres the JSON file
    is irrelevant and its absence proves nothing.
    """
    if _broker_mode() == "postgres":
        return _check_broker_postgres()
    if _broker_mode() != "legacy":
        return {"name": "broker records re-keyed", "ready": False,
                "detail": f"unrecognised LEAF_BROKER_STORE={_broker_mode()!r}; "
                          "refusing to guess which authority to check"}
    raw = os.environ.get("BROKER_TENANTS", "").strip()
    path = Path(raw) if raw else _server_dir() / "broker_tenants.json"
    if not path.is_file():
        return {"name": "broker records re-keyed", "ready": True,
                "detail": f"legacy mode and no broker tenants file at {path}; "
                          "nothing is keyed by a claim"}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"name": "broker records re-keyed", "ready": False,
                "detail": f"unreadable, cannot prove it is re-keyed: {exc}"}
    if not isinstance(records, dict):
        return {"name": "broker records re-keyed", "ready": False,
                "detail": "unexpected shape; expected an object keyed by tenant"}
    stale = [key for key in records if not _UUID.match(str(key).strip())]
    return {
        "name": "broker records re-keyed",
        "ready": not stale,
        "detail": ("every record is keyed by a platform id" if not stale else
                   f"{len(stale)} record(s) still keyed by a claim; a tenant "
                   "whose record TIGHTENS its tier would silently fall back to "
                   "DEFAULT_TIER, which grants nearly everything"),
        "stale_keys": stale[:20],
    }


def check_producers_stopped() -> Dict[str, Any]:
    """Draining is only meaningful once nothing can submit.

    A single listing proves the queue was empty at one instant, not that it
    stays empty: /api/run can commit a new raw-claim job immediately afterwards,
    and an active turn can submit one too. This gate cannot verify that traffic
    is stopped, so it refuses to certify it and says so, rather than letting a
    READY line imply a guarantee it never checked.
    """
    acknowledged = os.environ.get(
        "LEAF_CUTOVER_PRODUCERS_STOPPED", "").strip() == "1"
    return {
        "name": "producers stopped",
        "ready": acknowledged,
        "detail": ("operator asserts submission is stopped "
                   "(LEAF_CUTOVER_PRODUCERS_STOPPED=1)" if acknowledged else
                   "not asserted. Stop /api/run traffic and session turns FIRST, "
                   "then re-run: a drained snapshot is not a drained queue, "
                   "because a job submitted after the check is in flight with "
                   "the pre-cutover identity"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine output only")
    args = parser.parse_args()

    checks = [check_producers_stopped(), check_jobs_drained(),
              check_broker_records_rekeyed()]
    ready = all(check["ready"] for check in checks)
    report = {"ready": ready, "verdict": "READY" if ready else "NOT-READY",
              "checks": checks}

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(report["verdict"])
        for check in checks:
            mark = "ok  " if check["ready"] else "BLOCK"
            print(f"  {mark} {check['name']}: {check['detail']}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
