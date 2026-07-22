"""
App-side thin client for the APS broker (CONTRACT-ADDENDUM section 8).

This is the ONLY way the app/jobs process reaches APS-capable execution: an
HTTP call to the broker process. No da.* import, no APS credential, ever.
"""
from __future__ import annotations

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
    secret = os.environ.get("LEAF_BROKER_SECRET")
    return {"X-Broker-Secret": secret} if secret else {}


def harness_headers() -> Dict[str, str]:
    """F5 app->harness caller-auth: ``X-Harness-Secret`` from ``LEAF_HARNESS_SECRET``
    (same env the harness reads; Codex injects at deploy). Empty when unset so the
    off-live demo stays byte-identical."""
    secret = os.environ.get("LEAF_HARNESS_SECRET")
    return {"X-Harness-Secret": secret} if secret else {}


class BrokerUnreachable(Exception):
    """The broker process could not be reached (connection/timeout)."""


def run_via_broker(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
                   dwg: str, aps_live: bool, timeout_s: Optional[float] = None,
                   dwg_version: Optional[int] = None) -> Dict[str, Any]:
    """POST /broker/run -> extended section-3 envelope (ok true OR false).

    ``dwg_version`` (None -> head, unchanged behaviour) pins the run to a specific
    immutable drawing version; carried straight through as an extra JSON field so
    an older broker (that ignores unknown fields) stays compatible.
    """
    try:
        resp = requests.post(
            f"{broker_url()}/broker/run",
            json={"tenant_id": tenant_id, "tool": tool, "params": params,
                  "dwg": dwg, "aps_live": bool(aps_live), "dwg_version": dwg_version},
            headers=broker_headers(),
            timeout=timeout_s or 600,
        )
        return resp.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise BrokerUnreachable(f"broker at {broker_url()} unreachable: {exc}") from exc
    except ValueError as exc:  # non-JSON body
        raise BrokerUnreachable(f"broker at {broker_url()} returned non-JSON: {exc}") from exc
