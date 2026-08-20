"""Acceptance tests for card TEL-4: write-path product events (templates,
project edit, CAD upload).

Every test drives the REAL router handler (never a stand-in), monkeypatching
only ``telemetry_sink.emit`` to capture calls -- so a "mutation" that deletes,
misfires, or double-fires the real ``_emit_*`` call in the router under test
breaks the assertion here (mutation-red), not just the label wiring.

Acceptance oracle (frozen, TEL-4):
  - Events: template.pinned/template.applied {template_id, version};
    project.edit_applied {files_changed, bytes_class} - project edit is the
    ONE write path and must be observable; cad.upload_received /
    cad.upload_rejected {reason, size_class, format}.
  - Acceptance: exactly one event per accepted/rejected request, tested;
    mutation-red proven; no payload contents in labels.

Run:  cd server/tests && python -m pytest test_write_path_telemetry.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

PLATFORM_DIR = SERVER_DIR.parent / "platform"
if "leaf_platform" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "leaf_platform", PLATFORM_DIR / "__init__.py",
        submodule_search_locations=[str(PLATFORM_DIR)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["leaf_platform"] = _module
    assert _spec.loader is not None
    _spec.loader.exec_module(_module)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import deps  # noqa: E402
import telemetry_sink  # noqa: E402
import templates  # noqa: E402
from routers import cad_upload as cad_upload_router  # noqa: E402
from routers import project_repository_edit as edit_router  # noqa: E402
from routers import templates as templates_router  # noqa: E402


@pytest.fixture()
def captured(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_emit(name, **kw):
        calls.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", fake_emit)
    return calls


def _no_payload_contents(labels: Dict[str, Any], forbidden_substrings: List[str]) -> None:
    """Every label VALUE, stringified, must be free of any forbidden
    substring (filenames, digests, changed-path text, receipt content)."""
    for value in labels.values():
        text = str(value)
        for bad in forbidden_substrings:
            assert bad not in text, f"payload content {bad!r} leaked into label {value!r}"


# --------------------------------------------------------------------------- #
# templates: template.pinned (read) / template.applied (project clone)
# --------------------------------------------------------------------------- #

TEMPLATE_ID = "rooftop-standard-string"
TENANT = "tenant-tel4"
PROJECT = "project-tel4"


@pytest.fixture(autouse=True)
def _template_state(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOG", {})
    monkeypatch.setattr(templates, "_CLONE_UNDO_LOG", {})
    yield


def _ctx(role: str) -> deps.TenantContext:
    roles = () if role == "" else (role,)
    return deps.TenantContext(TENANT, subject="author", roles=roles)


def test_read_template_emits_exactly_one_pinned_event(captured):
    result = templates_router.read_template(TEMPLATE_ID, version="1.0.0", tenant=_ctx("viewer"))
    assert not isinstance(result, type(None))
    events = [c for c in captured if c["name"] == "template.pinned"]
    assert len(events) == 1
    ev = events[0]
    assert set(ev["labels"].keys()) == {"template_id", "version"}
    assert ev["labels"]["template_id"] == TEMPLATE_ID
    assert ev["labels"]["version"] == "1.0.0"
    assert ev["tenant_id"] == TENANT
    _no_payload_contents(ev["labels"], ["content", "receipt", "digest"])


def test_read_template_denied_emits_nothing(captured):
    from fastapi.responses import JSONResponse

    resp = templates_router.read_template(TEMPLATE_ID, version="1.0.0", tenant=_ctx(""))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 403
    assert [c for c in captured if c["name"] == "template.pinned"] == []


def test_read_template_not_found_emits_nothing(captured):
    from fastapi.responses import JSONResponse

    resp = templates_router.read_template("no-such-template", tenant=_ctx("viewer"))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 404
    assert captured == []


def test_clone_into_project_emits_exactly_one_applied_event(captured):
    req = templates_router.ProjectCloneRequest(project_id=PROJECT, version="1.0.0")
    resp = templates_router.clone_template_into_project(
        TEMPLATE_ID, req, tenant=_ctx("editor"))
    from fastapi.responses import JSONResponse

    assert isinstance(resp, JSONResponse) and resp.status_code == 201
    events = [c for c in captured if c["name"] == "template.applied"]
    assert len(events) == 1
    ev = events[0]
    assert set(ev["labels"].keys()) == {"template_id", "version"}
    assert ev["labels"]["template_id"] == TEMPLATE_ID
    assert ev["labels"]["version"] == "1.0.0"
    # Reading never fires "applied", and cloning never fires "pinned".
    assert [c for c in captured if c["name"] == "template.pinned"] == []
    _no_payload_contents(ev["labels"], ["content", "receipt", PROJECT])


def test_clone_into_project_denied_emits_nothing(captured):
    req = templates_router.ProjectCloneRequest(project_id=PROJECT, version="1.0.0")
    resp = templates_router.clone_template_into_project(TEMPLATE_ID, req, tenant=_ctx("viewer"))
    from fastapi.responses import JSONResponse

    assert isinstance(resp, JSONResponse) and resp.status_code == 403
    assert captured == []


# --------------------------------------------------------------------------- #
# cad_upload: cad.upload_received / cad.upload_rejected
# --------------------------------------------------------------------------- #

VALID_DXF = (
    b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n"
    b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
)


@pytest.fixture()
def cad_client(tmp_path, monkeypatch):
    monkeypatch.setenv(cad_upload_router.FLAG_CAD_UPLOAD, "1")
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(cad_upload_router.router)
    return TestClient(app)


def test_accepted_upload_emits_exactly_one_received_event(cad_client, captured):
    resp = cad_client.post(
        "/api/cad/upload",
        files={"file": ("layout.dxf", VALID_DXF, "application/octet-stream")})
    assert resp.status_code == 201
    digest = hashlib.sha256(VALID_DXF).hexdigest()

    events = [c for c in captured if c["name"] == "cad.upload_received"]
    assert len(events) == 1
    rejected = [c for c in captured if c["name"] == "cad.upload_rejected"]
    assert rejected == []
    ev = events[0]
    assert set(ev["labels"].keys()) == {"size_class", "format"}
    assert ev["labels"]["format"] == "dxf"
    assert ev["labels"]["size_class"] != "unknown"
    _no_payload_contents(ev["labels"], ["layout.dxf", digest])


def test_rejected_upload_bad_extension_emits_exactly_one_rejected_event(cad_client, captured):
    resp = cad_client.post(
        "/api/cad/upload",
        files={"file": ("notes.txt", b"hello world", "text/plain")})
    assert resp.status_code == 400

    events = [c for c in captured if c["name"] == "cad.upload_rejected"]
    assert len(events) == 1
    accepted = [c for c in captured if c["name"] == "cad.upload_received"]
    assert accepted == []
    ev = events[0]
    assert set(ev["labels"].keys()) == {"reason", "size_class", "format"}
    assert ev["labels"]["reason"] == "bad_extension"
    assert ev["labels"]["format"] == "unknown"
    _no_payload_contents(ev["labels"], ["notes.txt", "hello world"])


def test_rejected_upload_sniff_failure_emits_exactly_one_rejected_event(cad_client, captured):
    resp = cad_client.post(
        "/api/cad/upload",
        files={"file": ("fake.dxf", b"not really a dxf file", "application/octet-stream")})
    assert resp.status_code == 400

    events = [c for c in captured if c["name"] == "cad.upload_rejected"]
    assert len(events) == 1
    assert events[0]["labels"]["reason"] == "sniff_failed"
    assert events[0]["labels"]["format"] == "dxf"


def test_disabled_flag_emits_exactly_one_rejected_event(monkeypatch, tmp_path, captured):
    monkeypatch.delenv(cad_upload_router.FLAG_CAD_UPLOAD, raising=False)
    monkeypatch.setenv("LEAF_CAD_UPLOAD_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(cad_upload_router.router)
    client = TestClient(app)

    resp = client.post(
        "/api/cad/upload",
        files={"file": ("layout.dxf", VALID_DXF, "application/octet-stream")})
    assert resp.status_code == 503

    events = [c for c in captured if c["name"] == "cad.upload_rejected"]
    assert len(events) == 1
    assert events[0]["labels"] == {"reason": "disabled", "size_class": "unknown", "format": "unknown"}


def test_oversize_upload_emits_exactly_one_rejected_event(cad_client, monkeypatch, captured):
    monkeypatch.setenv("LEAF_CAD_UPLOAD_MAX_BYTES", "16")
    resp = cad_client.post(
        "/api/cad/upload",
        files={"file": ("layout.dxf", VALID_DXF, "application/octet-stream")})
    assert resp.status_code == 413

    events = [c for c in captured if c["name"] == "cad.upload_rejected"]
    assert len(events) == 1
    assert events[0]["labels"]["reason"] == "oversize"


# --------------------------------------------------------------------------- #
# project_repository_edit: project.edit_applied (record-staged is the ONE
# write path -- see routers/project_repository_edit.py's _emit_edit_applied)
# --------------------------------------------------------------------------- #

EDIT_TENANT = "11111111-1111-4111-8111-111111111111"
SECRET = "tel4-test-dispatch-secret"


def _edit_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", SECRET)
    app = FastAPI()
    app.include_router(edit_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _headers() -> Dict[str, str]:
    return {"X-Tenant-Id": EDIT_TENANT, "X-Dispatch-Secret": SECRET}


def test_record_staged_success_emits_exactly_one_applied_event(monkeypatch, captured):
    def handler(state, body):
        return {"contract": "leaf.project-repository-edit-coordination.v1",
                "action": "record_staged", "edit_id": "e-1", "state": "staged", "version": 1}

    monkeypatch.setattr(edit_router, "handle_record_staged", handler)
    body = {
        "action": "record_staged",
        "receipt": {
            "tenant_id": EDIT_TENANT,
            "changed_paths": ["src/a.txt", "src/b.txt", "src/c.txt"],
        },
    }
    resp = _edit_client(monkeypatch).post(
        "/internal/project-repository-edit/record-staged", json=body, headers=_headers())
    assert resp.status_code == 200

    events = [c for c in captured if c["name"] == "project.edit_applied"]
    assert len(events) == 1
    ev = events[0]
    assert set(ev["labels"].keys()) == {"files_changed", "bytes_class"}
    assert ev["labels"]["files_changed"] == 3
    assert ev["labels"]["bytes_class"] != "unknown"
    assert ev["tenant_id"] == EDIT_TENANT
    _no_payload_contents(ev["labels"], ["src/a.txt", "src/b.txt", "src/c.txt"])


def test_record_staged_denial_emits_nothing(monkeypatch, captured):
    called = False

    def handler(state, body):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(edit_router, "handle_record_staged", handler)
    resp = _edit_client(monkeypatch).post(
        "/internal/project-repository-edit/record-staged",
        json={"action": "record_staged", "receipt": {"tenant_id": "22222222-2222-4222-8222-222222222222"}},
        headers=_headers())
    assert resp.status_code == 404
    assert called is False
    assert captured == []


def test_record_staged_coordination_failure_emits_nothing(monkeypatch, captured):
    def handler(state, body):
        raise edit_router.CoordinationError("actor_authority_unavailable")

    monkeypatch.setattr(edit_router, "handle_record_staged", handler)
    body = {"action": "record_staged",
            "receipt": {"tenant_id": EDIT_TENANT, "changed_paths": ["a.txt"]}}
    resp = _edit_client(monkeypatch).post(
        "/internal/project-repository-edit/record-staged", json=body, headers=_headers())
    assert resp.status_code == 404
    assert captured == []


def test_other_coordination_ops_never_emit_project_edit_applied(monkeypatch, captured):
    """authorize/settle/recover-publish only lease/consume/settle an edit
    already recorded by record-staged; they must never emit a SECOND
    project.edit_applied for the same edit."""
    def handler(state, body):
        return {"contract": "leaf.project-repository-edit-coordination.v1", "action": "ok"}

    monkeypatch.setattr(edit_router, "handle_authorize_publish", handler)
    monkeypatch.setattr(edit_router, "handle_settle_publish", handler)
    monkeypatch.setattr(edit_router, "handle_recover_publish", handler)
    client = _edit_client(monkeypatch)
    for path, action in (
        ("authorize-publish", "authorize_publish"),
        ("settle-publish", "settle_publish"),
        ("recover-publish", "recover_publish"),
    ):
        resp = client.post(
            f"/internal/project-repository-edit/{path}",
            json={"action": action, "tenant_id": EDIT_TENANT}, headers=_headers())
        assert resp.status_code == 200
    assert captured == []
