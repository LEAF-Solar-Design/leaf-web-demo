"""Tier-branching entitlement enforcement for the platform jobs lane.

The orgs table has carried the cadwalk-studio-mirrored ``tier`` column since
migration 0001 — stored and validated but never branched on. This module is the
branch: POST /api/projects/{id}/jobs resolves the caller org's tier through the
server lane's fail-closed policy (server/entitlements.py + entitlements.json,
overridable via ``LEAF_ENTITLEMENTS_FILE``) so both lanes enforce the ONE
operator-tunable policy file instead of growing a second platform-only policy.

Job kinds map onto the server capability vocabulary (JOB_KIND_CAPABILITY).
Fail-closed rules:

  * org row missing         -> deny  (an unprovisioned org runs nothing)
  * org.status != "active"  -> deny  (offboarding/deleted orgs run nothing)
  * unknown/blank tier      -> the "restricted" policy entry (never "demo")
  * unmapped job kind       -> deny  (cannot classify => cannot grant)
  * policy seam unavailable -> 503   (enforcement never degrades to allow)

The server module is loaded lazily by file path (the same seam deps.py uses for
server/auth.py) so this package stays importable without the server tree on
sys.path; the server directory is appended at load time because
server/entitlements.py imports its sibling ``envelopes``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

from fastapi.responses import JSONResponse

from .models import Org

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_DIR = _PROJECT_ROOT / "server"
_SERVER_ENTITLEMENTS_FILE = _SERVER_DIR / "entitlements.py"

# Which server-lane capability each job kind consumes. "solve"/"run" execute
# hosted compute that mutates drawing state => run_write; "extract" is
# read-only; "build" is authoring. A kind absent from this map grants nothing.
JOB_KIND_CAPABILITY = {
    "solve": "run_write",
    "run": "run_write",
    "build": "build",
    "extract": "run_read",
}


def _server_entitlements():
    """server/entitlements.py, loaded by explicit file path and cached."""
    mod = sys.modules.get("leaf_server_entitlements")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "leaf_server_entitlements", _SERVER_ENTITLEMENTS_FILE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"server entitlements module unavailable at {_SERVER_ENTITLEMENTS_FILE}")
    mod = importlib.util.module_from_spec(spec)
    server_dir = str(_SERVER_DIR)
    if server_dir not in sys.path:
        # Appended (not prepended) so nothing already resolvable — the stdlib,
        # this package — can be shadowed by server/ siblings.
        sys.path.append(server_dir)
    spec.loader.exec_module(mod)
    sys.modules["leaf_server_entitlements"] = mod
    return mod


def job_entitlement_denial(org: Optional[Org], kind: str) -> Optional[JSONResponse]:
    """None when ``org`` may run ``kind``; otherwise the 403/503 response.

    Returned rather than raised so api.py hands back the SAME top-level
    envelope the server lane's ``entitlement_denied_response`` produces — an
    HTTPException would wrap the body under ``detail`` and change the shape
    the console's classifyAgentError already keys on.
    """
    try:
        ents = _server_entitlements()
    except Exception:
        # The policy could not load: enforcement must never degrade to allow.
        return JSONResponse(status_code=503, content={
            "entitlement_required": True,
            "error": {
                "error_code": "ENTITLEMENT_POLICY_UNAVAILABLE",
                "message": "entitlement policy unavailable; job refused (fail closed)",
                "retryable": True,
            },
        })

    tier = (org.tier if org is not None and isinstance(org.tier, str) and org.tier.strip()
            else ents.RESTRICTED_TIER)

    if org is None or org.status != "active":
        status_label = "missing" if org is None else org.status
        return JSONResponse(status_code=403, content={
            "entitlement_required": True,
            "required": "org_active",
            "tier": tier,
            "error": ents.error_obj(
                ents.ErrorCode.ENTITLEMENT_REQUIRED,
                f"organization is not active (status={status_label}); jobs are refused.",
                retryable=False,
            ),
            "degraded_mode": False,
        })

    cap = JOB_KIND_CAPABILITY.get(kind)
    if cap is None:
        # api.py validates kind against JOB_KINDS first; reaching here means the
        # vocabularies drifted — grant nothing rather than guess.
        return JSONResponse(status_code=403, content={
            "entitlement_required": True,
            "required": kind,
            "tier": tier,
            "error": ents.error_obj(
                ents.ErrorCode.ENTITLEMENT_REQUIRED,
                f"job kind '{kind}' has no entitlement mapping; refused (fail closed).",
                retryable=False,
            ),
            "degraded_mode": False,
        })

    if not ents.entitlements_for(tier).get(cap, False):
        return ents.entitlement_denied_response(cap, tier)
    return None
