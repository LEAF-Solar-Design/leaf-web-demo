"""
App-side thin client for the APS broker (CONTRACT-ADDENDUM section 8).

This is the ONLY way the app/jobs process reaches APS-capable execution: an
HTTP call to the broker process. No da.* import, no APS credential, ever.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional

import requests


def broker_url() -> str:
    return os.environ.get("BROKER_URL", "http://127.0.0.1:8140").rstrip("/")


def broker_headers() -> Dict[str, str]:
    """F4 caller-auth: the header the app→broker senders MUST attach. Sends
    ``X-Broker-Secret`` from ``LEAF_BROKER_SECRET`` (the SAME env the broker reads;
    Codex injects the value at deploy). Empty dict when the env is unset (off-live
    demo) so the local demo stays byte-identical to today. EVERY app-side POST to a
    protected ``/broker/*`` route must include these headers (``/broker/run`` here;
    the extract sender in ``routers/session.py`` and the disable/enable proxy in
    ``routers/ops.py`` must adopt this helper too — one-line change per site)."""
    secret = os.environ.get("LEAF_BROKER_SECRET", "").strip()
    return {"X-Broker-Secret": secret} if secret else {}


def harness_headers() -> Dict[str, str]:
    """F5 app->harness caller-auth: ``X-Harness-Secret`` from ``LEAF_HARNESS_SECRET``
    (same env the harness reads; Codex injects at deploy). Empty when unset so the
    off-live demo stays byte-identical."""
    secret = os.environ.get("LEAF_HARNESS_SECRET", "").strip()
    return {"X-Harness-Secret": secret} if secret else {}


class BrokerUnreachable(Exception):
    """The broker process could not be reached (connection/timeout)."""


class BrokerReapRejected(Exception):
    """The broker was reached but refused the reap (auth, 5xx, malformed body).

    Distinct from BrokerUnreachable only for readability: both mean the cancel
    did NOT happen, and both must leave the caller's retry queue intact.
    """


def extract_event_key(
    tenant_id: str,
    dwg: str,
    *,
    upload: bool = False,
    attempt: Optional[str] = None,
) -> str:
    """Stable identity for retries of one immutable drawing extraction."""
    if upload and not str(attempt or "").strip():
        raise ValueError("upload extraction event keys require an attempt")
    identity = {
        "tenant_id": str(tenant_id),
        "dwg": str(dwg),
        "upload": bool(upload),
    }
    if upload:
        identity["attempt"] = str(attempt)
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"extract:{digest}"


def run_via_broker(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
                   dwg: str, aps_live: bool, timeout_s: Optional[float] = None,
                   dwg_version: Optional[int] = None,
                   ledger_event_key: Optional[str] = None,
                   job_id: Optional[str] = None) -> Dict[str, Any]:
    """POST /broker/run -> extended section-3 envelope (ok true OR false).

    ``dwg_version`` (None -> head, unchanged behaviour) pins the run to a specific
    immutable drawing version; carried straight through as an extra JSON field so
    an older broker (that ignores unknown fields) stays compatible.
    ``ledger_event_key`` is the durable job-execution identity used by a
    PostgreSQL broker to prevent duplicate paid execution across task retries.
    ``job_id`` (None -> unchanged behaviour) lets the broker correlate this run's
    live WorkItem with the job row, so ``reap_via_broker`` can cancel it when the
    owning browser tab closes. An older broker ignores the unknown field.
    """
    try:
        resp = requests.post(
            f"{broker_url()}/broker/run",
            json={"tenant_id": tenant_id, "tool": tool, "params": params,
                  "dwg": dwg, "aps_live": bool(aps_live), "dwg_version": dwg_version,
                  "ledger_event_key": ledger_event_key, "job_id": job_id},
            headers=broker_headers(),
            timeout=timeout_s or 600,
        )
        return resp.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise BrokerUnreachable(f"broker at {broker_url()} unreachable: {exc}") from exc
    except ValueError as exc:  # non-JSON body
        raise BrokerUnreachable(f"broker at {broker_url()} returned non-JSON: {exc}") from exc


def reap_via_broker(records: list, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """POST /broker/reap -> cancel the orphaned WorkItems named by ``records``.

    Each record is an orphan signal the app side owns: ``job_id``, ``tenant_id``,
    and ``session_closed``/``lease_expires``. The app never knows the APS
    WorkItem id (it holds no credential); the broker resolves it from ``job_id``.
    Whether a LIVE DA cancel is issued stays the broker's decision, gated on
    APS_LIVE + BROKER_REAP_LIVE.

    Raises BrokerUnreachable on transport failure, like run_via_broker. Callers
    reaping on the tab-close path must treat that as non-fatal: the local job row
    is already terminal, and failing to cancel remote compute must not un-finish
    it.
    """
    try:
        resp = requests.post(
            f"{broker_url()}/broker/reap",
            json={"records": records},
            headers=broker_headers(),
            timeout=timeout_s or 30,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise BrokerUnreachable(f"broker at {broker_url()} unreachable: {exc}") from exc
    # A 401/500/503 still carries a JSON body. Returning it would report a
    # cancel that never happened, and the caller would drop the job from its
    # retry queue while APS keeps billing.
    if resp.status_code != 200:
        raise BrokerReapRejected(
            f"broker at {broker_url()} refused the reap: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError as exc:  # non-JSON body
        raise BrokerUnreachable(f"broker at {broker_url()} returned non-JSON: {exc}") from exc
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise BrokerReapRejected(f"broker at {broker_url()} returned a non-ok reap body")
    return body
