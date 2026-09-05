"""server/test_hardening_1f.py — F15 (input bounds) + F9-residue (fail-closed tiers).

F15  oversized inputs are REJECTED before processing:
       * POST /api/nl-prompt  `text`        -> pydantic max_length (validation layer)
       * POST /api/author     `description`  -> pydantic max_length (validation layer)
       * POST /api/run        `params`       -> jobs.submit_job byte cap -> HTTP 400
F9   entitlements_for FAILS CLOSED: unknown/absent tier -> "restricted" (read-only),
     per-key omission -> False; the auth-off "demo" tier stays full access.

Hermetic in-process (FastAPI TestClient); the jobs DB is routed to a throwaway file.
Run:  cd C:/tmp/leaf-web-demo/server && python -m pytest test_hardening_1f.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs`/`app` import (mirrors wave4).
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="hard1f-jobs-")) / "jobs.db"))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import entitlements  # noqa: E402
from _test_run_confirmation import confirmed_client_payload  # noqa: E402


def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _h(tenant: str = "demo-tenant") -> dict:
    return {"X-Tenant-Id": tenant}


# =========================================================================== #
# F15 — oversized inputs rejected (never processed)
# =========================================================================== #
def test_nl_prompt_oversized_text_rejected():
    c = _client()
    # a valid short prompt is accepted (control) ...
    ok = c.post("/api/nl-prompt", json={"text": "count panels per layer"}, headers=_h())
    assert ok.status_code == 200, ok.text
    # ... an over-cap body is rejected at the validation layer, never classified.
    big = "x" * 8_001
    r = c.post("/api/nl-prompt", json={"text": big}, headers=_h())
    assert r.status_code in (400, 422), r.text          # pydantic max_length -> 422 in this app
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_author_oversized_description_rejected():
    c = _client()
    big = "x" * 8_001
    r = c.post("/api/author", json={"description": big}, headers=_h())
    assert r.status_code in (400, 422), r.text
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"


def test_run_oversized_params_rejected_400():
    c = _client()
    # count-by-layer is a real read tool in the catalog; demo tier grants run_read.
    huge = {"blob": "x" * 70_000}                        # > 64 KiB serialised
    r = c.post("/api/run", json=confirmed_client_payload(
        c, "count-by-layer", huge, headers=_h()), headers=_h())
    assert r.status_code == 400, r.text
    assert r.json()["error"] is not None                 # structured error envelope
    # a normally-sized run on the same tool is still accepted (control).
    ok = c.post("/api/run", json=confirmed_client_payload(
        c, "count-by-layer", headers=_h()), headers=_h())
    assert ok.status_code == 202, ok.text


def test_run_params_cap_is_defense_in_depth_at_submit():
    """The bound lives in jobs.submit_job, so it holds for ANY caller of the spine."""
    import jobs
    from fastapi import HTTPException
    tool = {"name": "count-by-layer", "capabilities": ["drawing.read"]}
    with pytest.raises(HTTPException) as ei:
        jobs.submit_job("demo-tenant", tool, {"blob": "x" * 70_000}, "rooftop_demo", aps_live=False)
    assert ei.value.status_code == 400
    # a within-cap submission does NOT raise on the bound (returns a job_id).
    jid = jobs.submit_job("demo-tenant", tool, {"ok": 1}, "rooftop_demo", aps_live=False)
    assert isinstance(jid, str) and jid


# =========================================================================== #
# F9 residue — entitlements_for fails closed
# =========================================================================== #
def test_unknown_tier_falls_to_restricted_not_demo():
    ent = entitlements.entitlements_for("totally-unknown-tier-xyz")
    assert ent == {
        "run_read": True, "run_write": False, "build": False,
        "solve": False, "converse": False, "agent_write_autopilot": False,
        "deploy": False, "platform_customize": False,
        "upload": False, "link_service": False,
    }
    # the load-bearing property: an unrecognised tier does NOT get write/build.
    assert ent["run_write"] is False and ent["build"] is False


def test_per_key_omission_defaults_false(monkeypatch, tmp_path):
    """A PARTIAL policy entry no longer fails open: omitted keys default False."""
    pf = tmp_path / "entitlements.json"
    pf.write_text('{"partialtier": {"run_read": true}}', encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(pf))
    ent = entitlements.entitlements_for("partialtier")
    assert ent == {
        "run_read": True, "run_write": False, "build": False,
        "solve": False, "converse": False, "agent_write_autopilot": False,
        "deploy": False, "platform_customize": False,
        "upload": False, "link_service": False,
    }


def test_demo_tier_stays_full_access():
    """The auth-off / demo path is unchanged — demo is a known tier granting everything."""
    assert entitlements.entitlements_for("demo") == {
        "run_read": True, "run_write": True, "build": True,
        "solve": True, "converse": True, "agent_write_autopilot": True,
        "deploy": True, "platform_customize": False,
        "upload": True, "link_service": False,
    }


def test_resolve_tier_unchanged_offauth_demo_and_empty_restricted():
    # plain-str (auth-off) tenant -> demo (friction-free full access preserved).
    assert entitlements.resolve_tier("demo-tenant") == "demo"

    class _Ctx:  # an authenticated tenant whose Auth0 claim carried NO tier
        tier = ""

    assert entitlements.resolve_tier(_Ctx()) == "restricted"
