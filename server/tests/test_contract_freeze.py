"""
Contract-freeze guard for the agent-spine waves (plan verdict 3 / wire §8).

The §12 deterministic prompt lane is the documented DEGRADED-MODE FLOOR: with
the harness stopped, this surface IS the product. These tests pin the frozen
surfaces so no spine wave can drift them:

  * `nl_router.classify` still returns the documented five-key shape and
    routes "count panels per layer" to the run lane / count-by-layer;
  * POST /api/nl-prompt answers with EXACTLY the §12+§10 envelope fields —
    no new required fields, no tenant requirement (global, stateless);
  * ErrorCode keeps every pre-existing member AND gains exactly the four §18
    codes (guards against renames — the frontend keys on these strings).

Run:  cd server && python -m pytest tests/test_contract_freeze.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="freeze-jobs-")) / "jobs.db"))

import nl_router  # noqa: E402
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode  # noqa: E402

# The documented classify() shape — the §12 five keys plus `alternatives`
# (added pre-spine by CONTRACT-ADDENDUM §D "Contract 4"). Frozen as shipped.
CLASSIFY_KEYS = {"lane", "tool", "params", "confidence", "rationale", "alternatives"}

# The known-tool fixture from the §12 acceptance suite (test_nl_router.py) —
# grouping intent must keep beating the shallower panel-name match.
KNOWN_TOOLS = [
    {"name": "count-by-layer", "engine_op": "count_by_layer",
     "description": "Counts every model-space entity per layer.",
     "params": {"type": "object", "properties": {}}},
    {"name": "count-panels", "engine_op": "count_by_layer",
     "description": "count panels on layer Panels",
     "params": {"type": "object", "properties": {"layer": {"type": "string"}}}},
]


# --------------------------------------------------------------------------- #
# nl_router.classify — documented shape + lane, untouched
# --------------------------------------------------------------------------- #
def test_classify_shape_and_lane_frozen():
    r = nl_router.classify("count panels per layer", KNOWN_TOOLS)
    assert set(r) == CLASSIFY_KEYS, "classify() shape is §12-frozen — no new/renamed keys"
    assert r["lane"] == nl_router.LANE_RUN
    assert r["tool"] == "count-by-layer"
    assert 0.8 <= r["confidence"] <= 1.0
    assert isinstance(r["params"], dict)
    assert isinstance(r["rationale"], str) and r["rationale"]
    assert isinstance(r["alternatives"], list)


# --------------------------------------------------------------------------- #
# POST /api/nl-prompt — exact envelope, global + stateless (no tenant needed)
# --------------------------------------------------------------------------- #
def test_nl_prompt_envelope_frozen_no_tenant_required():
    from fastapi.testclient import TestClient

    from app import app
    c = TestClient(app, raise_server_exceptions=False)
    # deliberately NO X-Tenant-Id and no auth: §12 is global and stateless.
    r = c.post("/api/nl-prompt", json={"text": "count panels per layer"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) == CLASSIFY_KEYS | {"error", "degraded_mode"}, (
        "the §12 response body is frozen: classify keys + the §10 envelope pair only"
    )
    assert b["error"] is None
    assert b["degraded_mode"] is False
    assert b["lane"] == "run"
    assert b["tool"] == "count-by-layer"


# --------------------------------------------------------------------------- #
# ErrorCode enum — every pre-existing member AND the four §18 additions
# --------------------------------------------------------------------------- #
# name -> wire value, exactly as shipped before the agent spine (frozen for the
# platform frontend; QUOTA_EXCEEDED's lowercase promotion predates the spine).
PREEXISTING_CODES = {
    "UNKNOWN_TOOL": "UNKNOWN_TOOL",
    "BAD_PARAMS": "BAD_PARAMS",
    "APS_UNAVAILABLE": "APS_UNAVAILABLE",
    "BROKER_UNREACHABLE": "BROKER_UNREACHABLE",
    "WORKITEM_FAILED": "WORKITEM_FAILED",
    "TIMEOUT": "TIMEOUT",
    "TENANT_DISABLED": "TENANT_DISABLED",
    "GRANT_REQUIRED": "GRANT_REQUIRED",
    "ENTITLEMENT_REQUIRED": "ENTITLEMENT_REQUIRED",
    "QUOTA_EXCEEDED": "quota_exceeded",
    "INTERNAL": "INTERNAL",
}

# The §18 additions — wire values lowercase like quota_exceeded (wire §8).
NEW_CODES = {
    "LLM_QUOTA_EXHAUSTED": "llm_quota_exhausted",
    "LLM_RATE_LIMITED": "llm_rate_limited",
    "TURN_IN_PROGRESS": "turn_in_progress",
    "SESSION_NOT_FOUND": "session_not_found",
}


def test_error_code_members_frozen_plus_four_new():
    for name, value in {**PREEXISTING_CODES, **NEW_CODES}.items():
        assert hasattr(ErrorCode, name), f"ErrorCode.{name} was renamed/removed"
        assert getattr(ErrorCode, name) == value, (
            f"ErrorCode.{name} wire value drifted from {value!r}")
        assert value in ErrorCode.ALL, f"{value!r} missing from ErrorCode.ALL"


def test_new_codes_default_http_status():
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_QUOTA_EXHAUSTED] == 429
    assert DEFAULT_HTTP_STATUS[ErrorCode.LLM_RATE_LIMITED] == 429
    assert DEFAULT_HTTP_STATUS[ErrorCode.TURN_IN_PROGRESS] == 409
    assert DEFAULT_HTTP_STATUS[ErrorCode.SESSION_NOT_FOUND] == 404


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
