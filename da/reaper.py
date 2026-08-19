"""da/reaper.py — orphaned-WorkItem reaper (so a closed tab can't bill forever).

The risk: /api/run submits a WorkItem and the broker polls it to completion. If
the owning browser tab/session is abandoned (closed, crashed, lease expired), the
WorkItem keeps running and BILLING on APS with nobody watching. This module
sweeps such orphans and cancels them.

DESIGN
------
`sweep(records, cancel_client, now=None)` is PURE and transport-agnostic:
  * `records` is a jobs ledger — a list of dict rows, each carrying at least
    `status`, `workitem_id`, and an orphan signal (`session_closed=True` OR a
    `lease_expires` epoch in the past).
  * a row is an ORPHAN iff status in {submitted, inprogress} AND not already
    reaped AND (session_closed OR lease expired).
  * for each orphan, `cancel_client.cancel(workitem_id)` is called and the row is
    marked `status="reaped"` (+ `reaped=True`). Live (non-orphan) rows are left
    untouched.

The `cancel_client` is INJECTED, which is where liveness lives:
  * tests pass a stub that just records the cancel calls (NO APS).
  * production passes `DACancelClient`, which issues `DELETE /workitems/{id}` via
    da/client — reachable ONLY behind an explicit flag (APS_LIVE + the caller's
    decision), mirroring the dry_run/APS_LIVE discipline elsewhere.

Because only the credential-holding broker can talk to APS, the broker owns the
live cancel path (POST /broker/reap → sweep with DACancelClient); the app/jobs
side only supplies the orphan signal (tab-close / heartbeat-stale).
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

IN_FLIGHT = ("submitted", "inprogress")
REAPED = "reaped"


def is_orphan(record: Dict[str, Any], now: Optional[float] = None) -> bool:
    """True iff `record` is an in-flight job whose owner is gone (closed session
    or expired lease) and which has not already been reaped."""
    now = time.time() if now is None else now
    if record.get("reaped"):
        return False
    if record.get("status") not in IN_FLIGHT:
        return False
    if record.get("session_closed"):
        return True
    exp = record.get("lease_expires")
    return exp is not None and float(exp) < now


class StubCancelClient:
    """Test/default cancel client: records cancels, makes NO APS call."""

    def __init__(self) -> None:
        self.cancelled: List[str] = []

    def cancel(self, workitem_id: str) -> Dict[str, Any]:
        self.cancelled.append(workitem_id)
        return {"workitem_id": workitem_id, "cancelled": True, "live": False}


class DACancelClient:
    """Live cancel client: DELETE /workitems/{id} via da/client. LIVE APS call —
    only construct+use this behind an explicit flag (APS_LIVE / --reap-live)."""

    def __init__(self, da_client=None):
        self._da = da_client  # inject for testing; else lazy-load da/client.py

    def _client(self):
        if self._da is not None:
            return self._da
        import client as _client  # sibling da/client.py (credential-holding)
        return _client

    def cancel(self, workitem_id: str) -> Dict[str, Any]:
        return self._client().cancel_workitem(workitem_id)


def sweep(records: Iterable[Dict[str, Any]], cancel_client=None,
          now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Cancel + mark every orphan in `records`. Returns the reaped rows.

    Mutates each orphan row IN PLACE: status -> "reaped", reaped=True, and records
    reaped_at + cancel outcome. Non-orphan rows are not touched. `cancel_client`
    defaults to a StubCancelClient (no APS) so an accidental call is inert.

    FAILS CLOSED on false success: a row is marked reaped ONLY when a cancel was
    actually issued for a known workitem_id and did not report failure. An
    orphan with no workitem_id (`reap_unresolved`) or whose cancel failed /
    raised (`reap_outcome.cancelled=False`) stays un-reaped so a retry can still
    act on it — previously such rows were marked reaped while the WorkItem kept
    billing. One record's cancel error never aborts the rest of the batch.
    """
    now = time.time() if now is None else now
    client = cancel_client or StubCancelClient()
    reaped: List[Dict[str, Any]] = []
    for rec in records:
        if not is_orphan(rec, now):
            continue
        wid = rec.get("workitem_id")
        if not wid:
            rec["reap_unresolved"] = True
            continue
        try:
            outcome: Any = client.cancel(wid)
        except Exception as exc:  # noqa: BLE001 — one bad cancel must not kill the batch
            rec["reap_outcome"] = {"workitem_id": wid, "cancelled": False,
                                   "error": f"{type(exc).__name__}: {exc}"}
            continue
        if isinstance(outcome, dict) and outcome.get("cancelled") is False:
            rec["reap_outcome"] = outcome
            continue
        rec["status"] = REAPED
        rec["reaped"] = True
        rec["reaped_at"] = now
        rec["reap_outcome"] = outcome
        reaped.append(rec)
    return reaped


def reap_live_enabled() -> bool:
    """Whether the broker may perform LIVE WorkItem cancels. Off unless BOTH
    APS_LIVE=1 AND BROKER_REAP_LIVE=1 — a run can never be cancelled on APS by
    accident/import."""
    return os.environ.get("APS_LIVE") == "1" and os.environ.get("BROKER_REAP_LIVE") == "1"


def cancel_client_for(live: Optional[bool] = None, da_client=None):
    """Return the cancel client the broker should use: DACancelClient when live
    reaping is enabled, else the inert StubCancelClient."""
    use_live = reap_live_enabled() if live is None else bool(live)
    return DACancelClient(da_client=da_client) if use_live else StubCancelClient()
