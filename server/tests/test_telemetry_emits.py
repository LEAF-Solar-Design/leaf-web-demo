"""Regression coverage for the wave-B emit helpers (review #426 round-1
warn 6): classification, attribution, transition gating, and label
sanitization — the logic a refactor could silently break. The sink itself is
covered by test_telemetry.py; here it is captured, never exercised."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from fastapi.responses import JSONResponse

import guest_uploads
import telemetry_sink
from routers import sessions as sessions_router
from routers import tenant as tenant_router
from routers import uploads as uploads_router


@pytest.fixture()
def captured(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_emit(name, **kw):
        calls.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", fake_emit)
    return calls


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"error_code": code, "message": message,
                                           "retryable": False}})


# --------------------------------------------------------------------------- #
# uploads: attribution + classification (round-1 blocker 2 / warn 4)
# --------------------------------------------------------------------------- #

def test_upload_rejection_after_resolution_carries_the_real_tenant(captured):
    ident = {"tenant": "acme", "kind": "account", "minted": False}
    uploads_router._emit_upload_event(
        _err(413, "BAD_PARAMS", "file exceeds the 100 byte upload cap"), ident)
    assert captured[-1]["name"] == "drawing.upload_rejected"
    assert captured[-1]["tenant_id"] == "acme"
    assert captured[-1]["tenant_kind"] == "account"
    assert captured[-1]["labels"]["reason"] == "size"


def test_upload_rejection_before_resolution_is_anon(captured):
    uploads_router._emit_upload_event(
        _err(503, "INTERNAL", "guest drawing uploads are disabled on this deployment"), {})
    assert captured[-1]["tenant_id"] == "anon"
    assert captured[-1]["labels"]["reason"] == "disabled"


def test_upload_rejection_reason_classification(captured):
    cases = [
        (_err(429, "quota_exceeded", "guest upload quota reached"), "quota"),
        (_err(400, "BAD_PARAMS", "unsupported file extension"), "validation"),
        (_err(503, "INTERNAL", "storage cutover in progress"), "disabled"),
    ]
    for resp, want in cases:
        uploads_router._emit_upload_event(resp, {"tenant": "t1", "kind": "guest"})
        assert captured[-1]["labels"]["reason"] == want


def test_upload_success_minted_flag_comes_from_the_resolver_not_the_token(captured):
    body = {"tenant_id": "guest-abc", "tenant_kind": "guest",
            "drawing_id": "u-1", "guest_session": None, "status": "extracting"}
    resp = JSONResponse(status_code=202, content=body)
    # Auth-off anonymous upload: a guest tenant WAS minted but no token is
    # returned; the resolver's flag is the truth (round-1 warn 4).
    uploads_router._emit_upload_event(resp, {"tenant": "guest-abc", "kind": "guest",
                                             "minted": True})
    assert captured[-1]["name"] == "drawing.uploaded"
    assert captured[-1]["labels"]["minted_guest"] is True


# --------------------------------------------------------------------------- #
# extraction: transition gating (round-1 blocker 1)
# --------------------------------------------------------------------------- #

def test_mark_failed_emits_only_when_the_failure_actually_wrote(monkeypatch, captured):
    for written, expect_emit in ((False, 0), (True, 1)):
        captured.clear()
        monkeypatch.setattr(guest_uploads, "_mark_failed_committed",
                            lambda *a, **k: written)
        out = guest_uploads._mark_failed(
            object(), "guest-t", "u-9", {"attempt": "a1"},
            "TIMEOUT", "engine timed out", True)
        assert out is written
        assert len(captured) == expect_emit
        if expect_emit:
            assert captured[0]["name"] == "drawing.extraction_finished"
            assert captured[0]["labels"]["ok"] is False
            assert captured[0]["labels"]["error_code"] == "TIMEOUT"


# --------------------------------------------------------------------------- #
# grants: kind allowlist (round-1 blocker 3)
# --------------------------------------------------------------------------- #

def test_grant_kind_is_allowlisted_never_raw(captured):
    # Bogus requested kind + no harness kind -> label omitted entirely.
    tenant_router._emit_grant_event("grant.linked", "acme", {}, "sk-SECRET-LOOKING-STRING")
    assert captured[-1]["labels"] is None
    # Harness-reported kind wins when valid.
    tenant_router._emit_grant_event("grant.linked", "acme", {"kind": "oauth"}, "garbage")
    assert captured[-1]["labels"] == {"kind": "oauth"}
    # Valid requested kind is accepted when the harness reports none.
    tenant_router._emit_grant_event("grant.linked", "acme", {}, "api_key")
    assert captured[-1]["labels"] == {"kind": "api_key"}


# --------------------------------------------------------------------------- #
# agent walls: kind mapping incl. the two non-TurnRejected walls (warn 5)
# --------------------------------------------------------------------------- #

def test_agent_wall_kind_mapping_and_direct_sites(captured):
    sessions_router._emit_agent_wall_kind("acme", "s1", "busy", 409, "turn_in_progress")
    assert captured[-1]["labels"]["wall_kind"] == "busy"
    sessions_router._emit_agent_wall_kind("acme", "s1", "entitlement", 403,
                                          "ENTITLEMENT_REQUIRED")
    assert captured[-1]["labels"]["wall_kind"] == "entitlement"

    class _Exc:
        error_code = "GRANT_REQUIRED"
        status_code = 401

    sessions_router._emit_agent_wall(_Exc(), "acme", "s1")
    assert captured[-1]["labels"]["wall_kind"] == "grant"
    # Identity-less calls emit nothing.
    captured.clear()
    sessions_router._emit_agent_wall_kind(None, "s1", "busy", 409, "turn_in_progress")
    assert captured == []


def test_upload_wrapper_never_raises_on_garbage_response(captured):
    uploads_router._emit_upload_event(object(), {})
    uploads_router._emit_upload_event(JSONResponse(status_code=500, content={}), {})
    # No exception escaped; whatever emitted carried anon identity at worst.
    for call in captured:
        assert call["tenant_id"] in ("anon",)


# --------------------------------------------------------------------------- #
# WIRING tests (review #426 round-2 warn): the corrected paths themselves,
# not just the helpers — removing the ident threading, a wall call site, or
# the pg ready emit must fail one of these.
# --------------------------------------------------------------------------- #

def test_upload_route_threads_resolved_identity_into_rejection(monkeypatch, captured):
    """Through the REAL route: an oversize rejection AFTER identity
    resolution is attributed to the resolved tenant, not anon."""
    import contextlib

    import entitlements
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.setattr(uploads_router.write_loop,
                        "upload_import_mutations_enabled", lambda: True)

    @contextlib.contextmanager
    def _guard():
        yield True

    monkeypatch.setattr(uploads_router.write_loop,
                        "upload_mutation_commit_guard", _guard)
    monkeypatch.setattr(entitlements, "resolve_tier", lambda t: "pro")
    monkeypatch.setattr(entitlements, "resolve_roles", lambda t: ((), False))
    monkeypatch.setattr(entitlements, "entitlements_for",
                        lambda tier, roles, elevated: {"upload": True})
    monkeypatch.setattr(guest_uploads, "max_upload_bytes", lambda: 8)

    app = FastAPI()
    app.include_router(uploads_router.router)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        "/api/drawings/upload",
        headers={"X-Tenant-Id": "acme"},
        files={"file": ("plan.dxf", b"0" * 64, "application/octet-stream")},
    )
    assert resp.status_code == 413
    reject = [c for c in captured if c["name"] == "drawing.upload_rejected"]
    assert reject and reject[-1]["tenant_id"] == "acme"
    assert reject[-1]["labels"]["reason"] == "size"


def test_pg_ready_finalize_emits_after_commit(monkeypatch, captured):
    """_finalize_pg_ready_attempt emits ok=True exactly once, AFTER the
    serializable commit returns."""
    import hashlib

    data = b"intake-bytes"
    digest = hashlib.sha256(data).hexdigest()
    key = "cache/k1"
    marker = {"status": "ready", "attempt": "a1",
              "intake_ref": key, "intake_sha256": digest}

    order = []

    class _Db:
        def run_transaction(self, op, isolation=None):
            order.append("commit")

    def order_recording_emit(name, **kw):
        if name == "drawing.extraction_finished":
            order.append("emit")
        captured.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", order_recording_emit)

    class _Backend:
        def put_if_absent_or_verify(self, k, d):
            pass

        def get(self, k):
            return data

        def put(self, k, d):
            order.append("sentinel")

    monkeypatch.setattr(guest_uploads, "_drawing_db", lambda: _Db())
    monkeypatch.setitem(__import__("sys").modules, "psycopg.types.json",
                        __import__("types").SimpleNamespace(Jsonb=lambda v: v))
    monkeypatch.setitem(__import__("sys").modules, "psycopg.types",
                        __import__("types").SimpleNamespace(
                            json=__import__("sys").modules["psycopg.types.json"]))
    monkeypatch.setitem(__import__("sys").modules, "psycopg",
                        __import__("types").SimpleNamespace(
                            types=__import__("sys").modules["psycopg.types"]))

    guest_uploads._finalize_pg_ready_attempt(
        _Backend(), "guest-t", "u-7", marker, "owner-1", 3, key, data)

    emits = [c for c in captured if c["name"] == "drawing.extraction_finished"]
    assert len(emits) == 1
    assert emits[0]["labels"]["ok"] is True
    # The full sequence, not just the first element (review #426 round-3
    # carried warn): the emit follows the serializable commit, and the legacy
    # sentinel write follows the emit.
    assert order == ["commit", "emit", "sentinel"]
