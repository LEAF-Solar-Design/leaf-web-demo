"""
Registry-promotion groundwork (operator ruling R-A, 2026-07-22): "stringing"
capability availability, emitted from the REAL server catalog only.

R-A: the heuristic keeps the name ``autofill-string-targets``; the future real
optimizer will register as ``string-autofill-opt``; both belong to one
"stringing" capability family (``server/capability_families.json``).

This router is STANDALONE and deliberately NOT wired into app.py — the platform
lane (`auth0-identity-signup` sibling / R2, uncommitted at time of writing) is
independently building an authenticated, org/project-scoped
``GET /api/platform/capabilities`` that emits the same
``ServerCapabilityAvailability`` shape from LIVE canonical-worker health
(``server/routers/jobs.py``, ``platform/``). That is the authoritative runtime
signal once it merges. THIS router answers a narrower, catalog-only question
that needs no platform DB, no org/project context, and no live worker: is
``autofill-string-targets`` present in the tool catalog THIS process can see
right now? It fails closed — a tool absent from the catalog is reported
"locked", never fabricated as implemented.

Field names deliberately mirror the shape used in server/routers/jobs.py's
``GET /api/platform/capabilities`` (contractVersion/authority/productCapability/
evidence[]) so the two payloads read the same way, but the STATE VOCABULARY
here is catalog-only (registered/locked) — it does not claim runtimeState
values (connected/degraded/...) that require live worker health this router
does not have. ``authority`` is deliberately a DIFFERENT string
("leaf-server-catalog" vs. jobs.py's "leaf-platform-registry") so a client can
tell which authority answered. Reconciling the two into one contract is a
rebase-time decision for whoever owns the platform lane, not this router.

Run:  cd server && python -m pytest tests/test_capabilities_promotion.py -q
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import deps  # noqa: E402  (import order matches routers/capabilities.py — deps first
#   puts PROJECT_ROOT/SERVER_DIR on sys.path for the plain `import catalog` below)
import catalog  # noqa: E402
from envelopes import with_envelope_fields

router = APIRouter()

CONTRACT_VERSION = "leaf.platform.v1alpha1"
AUTHORITY = "leaf-server-catalog"
FAMILIES_FILE = catalog.FAMILIES_FILE
DATA_DIR = deps.PROJECT_ROOT / "data"

# The one capability this groundwork router emits (R-A two-name ruling).
STRINGING_TOOL = "autofill-string-targets"
STRINGING_PRODUCT_CAPABILITY = "drawing.solve.strings"


def _family_id_for(tool_name: str) -> Optional[str]:
    """Static family assignment straight from capability_families.json's
    tool_families map — config truth, independent of whether the tool is
    actually registered in the catalog yet."""
    cfg = json.loads(FAMILIES_FILE.read_text(encoding="utf-8"))
    return cfg.get("tool_families", {}).get(tool_name)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt_evidence(tool_name: str) -> List[Dict[str, Any]]:
    """Scan data/*.json for a receipt whose recorded tool_name (top-level or
    nested under "tool") matches tool_name. Real path + sha256 of the exact
    file bytes — never a fabricated digest. No match -> [] (this is expected
    today: no receipt in this repo yet proves autofill-string-targets ran
    through this catalog)."""
    evidence: List[Dict[str, Any]] = []
    if not DATA_DIR.is_dir():
        return evidence
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("tool_name")
        if not name and isinstance(data.get("tool"), dict):
            name = data["tool"].get("name")
        if name != tool_name:
            continue
        rel = path.relative_to(deps.PROJECT_ROOT).as_posix()
        evidence.append({
            "kind": "receipt",
            "uri": f"file:{rel}",
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
            "digest": {"algorithm": "sha256", "value": _sha256_file(path)},
        })
    return evidence


def availability_for(tool_name: str) -> Dict[str, Any]:
    """Build one ServerCapabilityAvailability-shaped entry from the REAL
    catalog. Fail closed: a tool absent from deps.all_tools() is "locked" —
    never reported implemented/registered on config presence alone."""
    now = datetime.now(timezone.utc).isoformat()
    tool = deps.find_tool(tool_name)
    family_id = _family_id_for(tool_name)
    # Evidence only for REGISTERED tools: a locked entry carrying receipts would
    # dress an unreachable capability in proof (fail-open by implication).
    evidence = _receipt_evidence(tool_name) if tool is not None else []
    if tool is not None:
        state = "registered"
        implementation_state = "implemented"
        reason_code = None
    else:
        state = "locked"
        implementation_state = "not_registered"
        reason_code = "not_registered_in_catalog"
    return {
        "contractVersion": CONTRACT_VERSION,
        "authority": AUTHORITY,
        "toolName": tool_name,
        "familyId": family_id,
        "productCapability": STRINGING_PRODUCT_CAPABILITY,
        "implementationState": implementation_state,
        "state": state,
        "observedAt": now,
        "reasonCode": reason_code,
        "evidence": evidence,
    }


@router.get("/api/capabilities/promotion/stringing")
def stringing_availability() -> Dict[str, Any]:
    """ServerCapabilityAvailability for the R-A "stringing" family's heuristic
    name (autofill-string-targets), catalog-only authority (no auth/tenant
    context needed — this reads the same process-global engine registry
    /api/capabilities does)."""
    return with_envelope_fields({"availability": availability_for(STRINGING_TOOL)})
