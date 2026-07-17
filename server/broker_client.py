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


class BrokerUnreachable(Exception):
    """The broker process could not be reached (connection/timeout)."""


def run_via_broker(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
                   dwg: str, aps_live: bool, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """POST /broker/run -> extended section-3 envelope (ok true OR false)."""
    try:
        resp = requests.post(
            f"{broker_url()}/broker/run",
            json={"tenant_id": tenant_id, "tool": tool, "params": params,
                  "dwg": dwg, "aps_live": bool(aps_live)},
            timeout=timeout_s or 600,
        )
        return resp.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise BrokerUnreachable(f"broker at {broker_url()} unreachable: {exc}") from exc
    except ValueError as exc:  # non-JSON body
        raise BrokerUnreachable(f"broker at {broker_url()} returned non-JSON: {exc}") from exc
