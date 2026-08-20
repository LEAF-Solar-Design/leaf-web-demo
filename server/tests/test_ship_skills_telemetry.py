"""Card TEL-5: ios_ship + skills/checkpoints telemetry.

Acceptance oracle (board/claimed/TEL-5.yaml):
  - Events: ship.requested/ship.completed/ship.failed {stage, reason};
    skill.invoked {skill}; checkpoint.created/restored.
  - Acceptance: the inventory shows the families; failures carry reason
    labels; mutation-red proven.

"Mutation-red proven": every test below fails if the emit call it covers is
deleted from the source, not merely if the response shape changes -- each
assertion reads telemetry_sink.emit's captured arguments directly.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import telemetry_sink  # noqa: E402
from routers import ios_ship  # noqa: E402
from routers import skills  # noqa: E402

SHA = "a" * 64
PROJECT = str(uuid.uuid4())
ORG = str(uuid.uuid4())
APPROVAL = str(uuid.uuid4())


@pytest.fixture()
def captured(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_emit(name, **kw):
        calls.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", fake_emit)
    return calls


# --------------------------------------------------------------------------- #
# ios_ship.py: ship.requested / ship.completed / ship.failed {stage, reason}
# --------------------------------------------------------------------------- #

class _IosShipError(ValueError):
    def __init__(self, code="error", message="error", setup_action=None):
        super().__init__(message)
        self.code = code
        self.setup_action = setup_action


class _GrantInvalid(_IosShipError):
    pass


class _RevisionNotApproved(_IosShipError):
    pass


class _LaunchConflict(_IosShipError):
    pass


class _ProjectUnavailable(_IosShipError):
    pass


class FakeStore:
    IosShipError = _IosShipError
    GrantInvalid = _GrantInvalid
    RevisionNotApproved = _RevisionNotApproved
    LaunchConflict = _LaunchConflict
    ProjectUnavailable = _ProjectUnavailable

    def __init__(self):
        self.launches: List[Any] = []
        self.project_available = True
        self.principal = "binding-1"
        self.raise_on_launch: Any = None

    def project_org(self, _project_id):
        return ORG if self.project_available else None

    def project_accessible(self, org_id, project_id, subject):
        return (self.project_available and str(org_id) == ORG
                and project_id == PROJECT and bool(subject))

    def resolve_ship_principal(self, _org_id, _project_id, _subject):
        return self.principal

    def launch_execution(self, *args, **kwargs):
        if self.raise_on_launch is not None:
            raise self.raise_on_launch
        self.launches.append((args, kwargs))
        return {"execution_id": "e-1", "project_id": PROJECT, "status": "dispatched"}


class VerifiedTenant(str):
    def __new__(cls, tenant_id, org_id, subject):
        value = super().__new__(cls, tenant_id)
        value.tenant_id = tenant_id
        value.org_id = org_id
        value.subject = subject
        return value


def _client(tenant) -> TestClient:
    app = FastAPI()
    app.include_router(ios_ship.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


def _body(**overrides) -> Dict[str, Any]:
    body = {"project_id": PROJECT, "approval_id": APPROVAL, "revision": "r1",
            "source_revision": "83bbde1", "source_sha256": SHA,
            "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
            "build_number": "19"}
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _reset_dispatch():
    ios_ship.set_dispatch(None)
    previous = os.environ.get("LEAF_IOS_SHIP_APP_COLOR")
    os.environ["LEAF_IOS_SHIP_APP_COLOR"] = "primary"
    yield
    ios_ship.set_dispatch(None)
    if previous is None:
        os.environ.pop("LEAF_IOS_SHIP_APP_COLOR", None)
    else:
        os.environ["LEAF_IOS_SHIP_APP_COLOR"] = previous


def _names(calls: List[Dict[str, Any]]) -> List[str]:
    return [c["name"] for c in calls]


def test_ship_requested_and_completed_on_a_successful_launch(monkeypatch, captured):
    store = FakeStore()
    monkeypatch.setattr(ios_ship, "_store", lambda: store)
    ios_ship.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))

    response = client.post("/api/ios-ship/launch", json=_body(),
                           headers={"Idempotency-Key": "k-1"})

    assert response.status_code == 202
    assert _names(captured) == ["ship.requested", "ship.completed"]
    requested, completed = captured
    assert requested["labels"] == {"stage": "received"}
    assert completed["labels"] == {"stage": "dispatched"}
    for call in captured:
        assert call["tenant_id"] == ORG
        assert call["tenant_kind"] == "account"


def test_ship_failed_carries_stage_and_reason_for_missing_idempotency_key(captured):
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))

    response = client.post("/api/ios-ship/launch", json=_body())

    assert response.status_code == 400
    assert _names(captured) == ["ship.requested", "ship.failed"]
    failed = captured[-1]
    assert failed["labels"]["stage"] == "validation"
    assert failed["labels"]["reason"] == "idempotency_key_required"


def test_ship_failed_reason_tracks_each_named_failure_branch(monkeypatch, captured):
    store = FakeStore()
    monkeypatch.setattr(ios_ship, "_store", lambda: store)
    ios_ship.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))

    # secret-shaped field: rejected before the store is ever touched.
    captured.clear()
    resp = client.post("/api/ios-ship/launch", json=_body(apple_password="x"),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 400
    assert captured[-1]["labels"] == {"stage": "validation", "reason": "secret_shaped_field"}

    # dispatch not mounted.
    captured.clear()
    ios_ship.set_dispatch(None)
    resp = client.post("/api/ios-ship/launch", json=_body(),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 503
    assert captured[-1]["labels"] == {"stage": "provisioning", "reason": "dispatch_unavailable"}
    ios_ship.set_dispatch(lambda _: {"status": "dispatched"})

    # app color unavailable.
    captured.clear()
    os.environ.pop("LEAF_IOS_SHIP_APP_COLOR", None)
    resp = client.post("/api/ios-ship/launch", json=_body(),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 503
    assert captured[-1]["labels"] == {"stage": "provisioning", "reason": "app_color_unavailable"}
    os.environ["LEAF_IOS_SHIP_APP_COLOR"] = "primary"

    # unknown project: authorization stage.
    captured.clear()
    store.project_available = False
    resp = client.post("/api/ios-ship/launch", json=_body(),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 404
    assert captured[-1]["labels"] == {"stage": "authorization", "reason": "project_unavailable"}
    store.project_available = True

    # store raises a named ship error: dispatch stage.
    captured.clear()
    store.raise_on_launch = FakeStore.IosShipError("bad_bundle", "bundle id mismatch")
    resp = client.post("/api/ios-ship/launch", json=_body(),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 400
    assert captured[-1]["labels"] == {"stage": "dispatch", "reason": "bad_bundle"}
    store.raise_on_launch = None

    # store raises a grant conflict: grant stage.
    captured.clear()
    store.raise_on_launch = FakeStore.GrantInvalid("grant_expired", "grant has expired")
    resp = client.post("/api/ios-ship/launch", json=_body(),
                       headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 409
    assert captured[-1]["labels"] == {"stage": "grant", "reason": "grant_expired"}


# --------------------------------------------------------------------------- #
# skills.py: skill.invoked {skill}
# --------------------------------------------------------------------------- #

def _skills_client() -> TestClient:
    app = FastAPI()
    app.include_router(skills.router)
    return TestClient(app, raise_server_exceptions=True)


def _bundle(root: Path, tier: str, names: Dict[str, str]) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        '{"leafTier": "%s"}' % tier, encoding="utf-8")
    for name, description in names.items():
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8")
    return root


def test_skill_invoked_fires_once_per_catalog_skill(monkeypatch, tmp_path, captured):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    bundle = _bundle(tmp_path / "tenant", "tenant-safe",
                     {"probe": "Probe skill", "measure": "Measure skill"})
    monkeypatch.setenv("LEAF_SKILLS_BUNDLE_PATH", str(bundle))
    monkeypatch.delenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", raising=False)

    response = _skills_client().get("/api/skills")

    assert response.status_code == 200
    assert _names(captured) == ["skill.invoked", "skill.invoked"]
    assert {c["labels"]["skill"] for c in captured} == {"probe", "measure"}


def test_skill_invoked_is_silent_for_an_empty_catalog(monkeypatch, captured):
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_SKILLS_BUNDLE_PATH", raising=False)
    monkeypatch.delenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", raising=False)

    response = _skills_client().get("/api/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}
    assert captured == []


# --------------------------------------------------------------------------- #
# skills.py: checkpoint.created / checkpoint.restored
# --------------------------------------------------------------------------- #

def test_checkpoint_created_emits_the_family_with_its_identifiers(captured):
    skills.record_checkpoint_created(
        tenant_id="tenant-a", tenant_kind="account", session_id="sess-1",
        checkpoint_id="cp-1", skill="probe")

    assert _names(captured) == ["checkpoint.created"]
    call = captured[0]
    assert call["tenant_id"] == "tenant-a"
    assert call["tenant_kind"] == "account"
    assert call["session_id"] == "sess-1"
    assert call["labels"] == {"checkpoint_id": "cp-1", "skill": "probe"}


def test_checkpoint_restored_emits_the_family_with_its_identifiers(captured):
    skills.record_checkpoint_restored(
        tenant_id="tenant-a", tenant_kind="account", session_id="sess-1",
        checkpoint_id="cp-1", skill="probe")

    assert _names(captured) == ["checkpoint.restored"]
    call = captured[0]
    assert call["labels"] == {"checkpoint_id": "cp-1", "skill": "probe"}


def test_checkpoint_events_omit_skill_label_when_not_supplied(captured):
    skills.record_checkpoint_created(
        tenant_id="tenant-a", tenant_kind="account", session_id="sess-1",
        checkpoint_id="cp-2")
    skills.record_checkpoint_restored(
        tenant_id="tenant-a", tenant_kind="account", session_id="sess-1",
        checkpoint_id="cp-2")

    for call in captured:
        assert call["labels"] == {"checkpoint_id": "cp-2"}
        assert "skill" not in call["labels"]


# --------------------------------------------------------------------------- #
# Inventory: every event family the oracle names is proven reachable
# --------------------------------------------------------------------------- #

EVENT_FAMILIES = (
    "ship.requested", "ship.completed", "ship.failed",
    "skill.invoked",
    "checkpoint.created", "checkpoint.restored",
)


def test_inventory_shows_every_named_family(monkeypatch, tmp_path, captured):
    """One combined run touching every real choke point in this card: the
    inventory of captured event names must be a superset of every family the
    acceptance oracle names."""
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)

    store = FakeStore()
    monkeypatch.setattr(ios_ship, "_store", lambda: store)
    ios_ship.set_dispatch(lambda _: {"status": "dispatched"})
    prev = os.environ.get("LEAF_IOS_SHIP_APP_COLOR")
    os.environ["LEAF_IOS_SHIP_APP_COLOR"] = "primary"
    try:
        ship_client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
        ship_client.post("/api/ios-ship/launch", json=_body(),
                         headers={"Idempotency-Key": "k-1"})
        ship_client.post("/api/ios-ship/launch", json=_body())  # missing key -> failed
    finally:
        ios_ship.set_dispatch(None)
        if prev is None:
            os.environ.pop("LEAF_IOS_SHIP_APP_COLOR", None)
        else:
            os.environ["LEAF_IOS_SHIP_APP_COLOR"] = prev

    bundle = _bundle(tmp_path / "tenant", "tenant-safe", {"probe": "Probe"})
    monkeypatch.setenv("LEAF_SKILLS_BUNDLE_PATH", str(bundle))
    monkeypatch.delenv("LEAF_SKILLS_OPERATOR_BUNDLE_PATH", raising=False)
    _skills_client().get("/api/skills")

    skills.record_checkpoint_created(
        tenant_id="t", tenant_kind="account", session_id="s", checkpoint_id="c1")
    skills.record_checkpoint_restored(
        tenant_id="t", tenant_kind="account", session_id="s", checkpoint_id="c1")

    seen = set(_names(captured))
    missing = set(EVENT_FAMILIES) - seen
    assert not missing, f"acceptance oracle families never emitted: {sorted(missing)}"
