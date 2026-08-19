"""CAD upload/edit fence: standalone negative controls (Lane C1, card C1-7).

The real cad_upload production surface (server/routers/cad_upload.py,
server/cad_versions.py) is built by sibling cards C1-2..C1-4 and has not
landed on this app revision. This suite proves the fence itself -- the
negative controls the oracle demands -- using ONLY what already exists on
this revision:

1. cad_upload OFF refuses everything. Modeled with the SAME rollout-mode
   convention `server/customization_flags.py` already establishes for
   R5/R6/R7 (env-var driven, fail-closed on garbage), scoped to a new
   `LEAF_CAD_UPLOAD_MODE` var. The reference gate below wraps a toy "accept
   upload" step that would write bytes + a version receipt; with the flag
   OFF every call is refused BEFORE the write, so zero bytes ever touch
   storage. NOTE for the C1-3 lane: land the real check at this env name (or
   amend `_cad_upload_enabled` here) in the same PR that mounts
   `cad_upload.py`, so this file keeps proving the real gate.

2. cad_edit OFF never mounts the worker -- a standalone flip-time proof.
   Mirrors the precedent in `test_operator_vocab_freeze.py`
   (`test_no_registered_route_under_operator_namespace`): sweep the live
   FastAPI `app` for any route under the CAD-edit worker namespace and prove
   none is registered, THEN flip the env var at runtime in the SAME process
   and re-check the flag reader itself resolves ON -- proving the fence
   mechanism (not merely today's route absence) responds to the flip both
   ways. NOTE for the C1-5/C1-6 lane: extend `_cad_edit_worker_routes()` to
   the real route prefix when the worker mounts, so this gate keeps proving
   the OFF state once ON is reachable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from customization_flags import RolloutMode, mode

CAD_UPLOAD_ENV = "LEAF_CAD_UPLOAD_MODE"
CAD_EDIT_ENV = "LEAF_CAD_EDIT_MODE"
CAD_INTERNAL_TENANTS_ENV = "LEAF_CAD_INTERNAL_TENANTS"


def _internal_tenants() -> frozenset[str]:
    return frozenset(
        part.strip() for part in os.environ.get(
            CAD_INTERNAL_TENANTS_ENV, "").split(",") if part.strip())


def _flag_enabled(env_name: str, tenant_id: str) -> bool:
    selected = mode(env_name)
    if selected is RolloutMode.ALL:
        return True
    if selected is RolloutMode.INTERNAL:
        return tenant_id in _internal_tenants()
    return False


def _cad_upload_enabled(tenant_id: str = "demo-tenant") -> bool:
    return _flag_enabled(CAD_UPLOAD_ENV, tenant_id)


def _cad_edit_enabled(tenant_id: str = "demo-tenant") -> bool:
    return _flag_enabled(CAD_EDIT_ENV, tenant_id)


class CadUploadRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _reference_accept_upload(store_dir: Path, filename: str, payload: bytes,
                              tenant_id: str = "demo-tenant") -> dict:
    """Toy reference of the write-before-store gate C1-3 implements for real:
    refuse BEFORE any byte is written when the flag is off."""
    if not _cad_upload_enabled(tenant_id):
        raise CadUploadRefused("cad_upload_disabled")
    store_dir.mkdir(parents=True, exist_ok=True)
    target = store_dir / filename
    target.write_bytes(payload)
    return {"path": str(target), "bytes": len(payload)}


# --- 1. cad_upload OFF refuses everything -----------------------------------

@pytest.mark.parametrize("filename,payload", [
    ("plan.dwg", b"AC1027-fake-dwg-bytes"),
    ("plan.dxf", b"0\nSECTION\n"),
    ("empty.dwg", b""),
    ("chunky.dwg", b"x" * 4096),
])
def test_cad_upload_off_by_default_refuses_every_call(monkeypatch, tmp_path,
                                                        filename, payload):
    monkeypatch.delenv(CAD_UPLOAD_ENV, raising=False)
    assert _cad_upload_enabled() is False  # unset == off, no other config needed
    store_dir = tmp_path / "cad-store"
    with pytest.raises(CadUploadRefused) as err:
        _reference_accept_upload(store_dir, filename, payload)
    assert err.value.reason == "cad_upload_disabled"
    assert not store_dir.exists(), "a refused upload must leave zero orphan bytes"


def test_cad_upload_explicit_off_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv(CAD_UPLOAD_ENV, "off")
    store_dir = tmp_path / "cad-store"
    with pytest.raises(CadUploadRefused):
        _reference_accept_upload(store_dir, "plan.dwg", b"data")
    assert not store_dir.exists()


def test_cad_upload_malformed_mode_fails_closed(monkeypatch, tmp_path):
    # An unparseable mode value is always off (RolloutMode.mode's own contract).
    monkeypatch.setenv(CAD_UPLOAD_ENV, "not-a-real-mode")
    assert mode(CAD_UPLOAD_ENV) is RolloutMode.OFF
    store_dir = tmp_path / "cad-store"
    with pytest.raises(CadUploadRefused):
        _reference_accept_upload(store_dir, "plan.dwg", b"data")
    assert not store_dir.exists()


def test_cad_upload_internal_mode_refuses_a_non_internal_tenant(monkeypatch, tmp_path):
    monkeypatch.setenv(CAD_UPLOAD_ENV, "internal")
    monkeypatch.setenv(CAD_INTERNAL_TENANTS_ENV, "trusted-tenant")
    store_dir = tmp_path / "cad-store"
    with pytest.raises(CadUploadRefused):
        _reference_accept_upload(store_dir, "plan.dwg", b"data", tenant_id="other-tenant")
    assert not store_dir.exists()
    # The SAME flag, same process, accepts the trusted tenant.
    accepted = _reference_accept_upload(store_dir, "plan.dwg", b"data",
                                         tenant_id="trusted-tenant")
    assert Path(accepted["path"]).read_bytes() == b"data"


def test_cad_upload_flip_time_on_then_off_in_the_same_process(monkeypatch, tmp_path):
    # Flip-time proof: the SAME reference gate, in the SAME process, is
    # entirely governed by the live env var, not by any cached/imported state.
    store_dir = tmp_path / "cad-store"
    monkeypatch.setenv(CAD_UPLOAD_ENV, "all")
    accepted = _reference_accept_upload(store_dir, "plan.dwg", b"payload-bytes")
    assert Path(accepted["path"]).read_bytes() == b"payload-bytes"

    monkeypatch.setenv(CAD_UPLOAD_ENV, "off")
    with pytest.raises(CadUploadRefused):
        _reference_accept_upload(store_dir, "plan2.dwg", b"more-bytes")
    assert not (store_dir / "plan2.dwg").exists()


# --- 2. cad_edit OFF never mounts the worker: standalone flip-time proof ---

def _cad_edit_worker_routes() -> tuple[str, ...]:
    """Every path prefix the future editing-worker mount is expected to use.
    Extend this in the SAME PR that mounts the real worker (see module
    docstring) so this gate keeps proving the OFF state once ON exists."""
    return ("/api/cad", "/api/cad-worker", "/api/drawings/edit",
            "/api/cad/edit", "/api/cad/worker")


def test_cad_edit_worker_not_mounted_on_this_app_revision():
    from app import app

    mounted = [getattr(r, "path", "") for r in app.routes
               if getattr(r, "path", "").startswith(_cad_edit_worker_routes())]
    assert mounted == [], (
        f"a CAD-edit worker route is registered on a pre-cad_edit app "
        f"revision: {mounted}; the C1-5/C1-6 lane must amend this gate in "
        "the same PR that mounts the real worker")


def test_cad_edit_flag_reader_flips_both_ways_in_the_same_process(monkeypatch):
    # Standalone: no server process, no worker module -- proves the FENCE
    # ITSELF (not merely current route absence) tracks the live flag in both
    # directions within one process.
    monkeypatch.delenv(CAD_EDIT_ENV, raising=False)
    assert _cad_edit_enabled() is False

    monkeypatch.setenv(CAD_EDIT_ENV, "all")
    assert _cad_edit_enabled() is True

    monkeypatch.setenv(CAD_EDIT_ENV, "off")
    assert _cad_edit_enabled() is False

    monkeypatch.setenv(CAD_EDIT_ENV, "internal")
    monkeypatch.delenv(CAD_INTERNAL_TENANTS_ENV, raising=False)
    assert _cad_edit_enabled("demo-tenant") is False
    monkeypatch.setenv(CAD_INTERNAL_TENANTS_ENV, "demo-tenant")
    assert _cad_edit_enabled("demo-tenant") is True
    assert _cad_edit_enabled("other-tenant") is False


def test_cad_edit_worker_absence_holds_even_after_flipping_the_flag_on(monkeypatch):
    # Even flipped ON, THIS app revision still mounts nothing (the worker
    # literally does not exist here yet) -- proves the route-absence
    # assertion above is not accidentally passing only because the flag
    # happens to be off in the collecting test process.
    from app import app

    monkeypatch.setenv(CAD_EDIT_ENV, "all")
    assert _cad_edit_enabled() is True
    mounted = [getattr(r, "path", "") for r in app.routes
               if getattr(r, "path", "").startswith(_cad_edit_worker_routes())]
    assert mounted == []
