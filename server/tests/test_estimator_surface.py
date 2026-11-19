"""Route boundary proofs with no network, deployment or live KMS dependency."""
import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
import estimator_handoff as handoff
import platform_link
from routers import estimator_surface as surface
from test_estimator_handoff import CHAN, KID, ORIGIN, PROJECT, ROW, TENANT, StubKms, decode

REAL_PROJECT_ACCESS = platform_link.require_project_access


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("LEAF_ESTIMATOR_SURFACE_ENABLED", "true")
    monkeypatch.setenv("LEAF_ESTIMATOR_BINDINGS", json.dumps([ROW]))
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(surface, "lab_posture", lambda: "staging")
    monkeypatch.setattr(handoff, "limiter", handoff.MintLimiter())
    kms = StubKms()
    monkeypatch.setattr(handoff, "_signer", handoff.KmsRs256Signer("alias/test", kms))
    tenant = deps.TenantContext(TENANT, subject="auth0|human", authority_resolved=True)
    state = SimpleNamespace(tenant=tenant, checks=[], denied=set(), kms=kms)
    binding = object()
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda _: binding)

    def access(tenant, project_id, *, write, binding=None):
        state.checks.append((tenant, project_id, write, binding))
        assert write is True
        if str(tenant) != TENANT or project_id in state.denied:
            raise platform_link.ProjectSessionForbidden("private reason")
        return str(tenant)

    monkeypatch.setattr(platform_link, "require_project_access", access)
    app = FastAPI()
    app.include_router(surface.router)
    app.dependency_overrides[deps.require_tenant] = lambda: state.tenant
    state.client = TestClient(app)
    state.path = f"/api/projects/{PROJECT}/estimator/handoff"
    return state


def test_mint_exact_response_and_no_token_or_channel_in_logs(harness, caplog):
    with caplog.at_level(logging.INFO):
        response = harness.client.post(harness.path, json={"chan": CHAN})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "estimator_origin", "subh", "expires_in"}
    assert response.headers["cache-control"] == "no-store"
    claims = json.loads(decode(body["token"].split(".")[1]))
    assert json.loads(decode(body["token"].split(".")[0]))["kid"] == KID
    assert body["subh"] == claims["sub"] and body["expires_in"] == 60
    assert body["estimator_origin"] == ORIGIN and claims["tenant"] == TENANT
    assert body["token"] not in caplog.text and CHAN not in caplog.text
    assert claims["jti"] in caplog.text and claims["sub"] in caplog.text


def test_status_lists_only_accessible_tenant_bindings_with_one_resolved_binding(harness, monkeypatch):
    rows = [ROW, dict(ROW, studio_project_id=TENANT, deployment_id="dev-local", estimator_origin="http://localhost:8080")]
    monkeypatch.setenv("LEAF_ESTIMATOR_BINDINGS", json.dumps(rows))
    monkeypatch.setattr(surface, "lab_posture", lambda: "local")
    harness.denied.add(TENANT)
    response = harness.client.get("/api/estimator/status")
    assert response.json() == {"state": "on", "bindings": [{"project_id": PROJECT, "estimator_origin": ORIGIN}]}
    assert len(harness.checks) == 2
    assert harness.checks[0][3] is harness.checks[1][3] and harness.checks[0][3] is not None
    harness.denied.add(PROJECT)
    assert harness.client.get("/api/estimator/status").status_code == 404


@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1", " true", "true "])
def test_surface_off_is_404_for_both_routes(harness, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("LEAF_ESTIMATOR_SURFACE_ENABLED")
    else:
        monkeypatch.setenv("LEAF_ESTIMATOR_SURFACE_ENABLED", flag)
    assert harness.client.get("/api/estimator/status").status_code == 404
    assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == 404
    assert not harness.checks and harness.kms.sign_calls == 0


def test_production_always_off_and_malformed_bindings_fail_closed(harness, monkeypatch):
    monkeypatch.setattr(surface, "lab_posture", lambda: "production")
    assert harness.client.get("/api/estimator/status").status_code == 404
    assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == 404
    monkeypatch.setattr(surface, "lab_posture", lambda: "staging")
    monkeypatch.setenv("LEAF_ESTIMATOR_BINDINGS", "malformed")
    assert harness.client.get("/api/estimator/status").status_code == 404


@pytest.mark.parametrize("role", ["reviewer", "read_only", "no-role", "cross-tenant", "unbound", "missing"])
def test_no_access_cross_tenant_and_unbound_have_identical_404(harness, monkeypatch, role):
    if role == "cross-tenant":
        harness.tenant = deps.TenantContext(PROJECT, subject="auth0|human", authority_resolved=True)
    elif role == "unbound":
        monkeypatch.setenv("LEAF_ESTIMATOR_BINDINGS", json.dumps([dict(ROW, studio_project_id=TENANT)]))
    elif role == "missing":
        def absent(*args, **kwargs):
            raise LookupError("private reason")
        monkeypatch.setattr(platform_link, "require_project_access", absent)
    else:
        harness.denied.add(PROJECT)
    response = harness.client.post(harness.path, json={"chan": CHAN})
    assert response.status_code == 404 and response.json() == {"detail": "estimator_unavailable"}
    assert response.headers["cache-control"] == "no-store" and harness.kms.sign_calls == 0


@pytest.mark.parametrize("subject,attributes", [
    (None, {}), ("machine@clients", {}), (handoff.SUBJECT_PREFIX + "staging", {}),
    ("human", {"backedge": True}), ("human", {"verified_demo": True}),
    ("human", {"authority_resolved": False}),
])
def test_nonhuman_cannot_probe_or_mint(harness, subject, attributes):
    harness.tenant = deps.TenantContext(TENANT, **{"subject": subject, "authority_resolved": True, **attributes})
    assert harness.client.get("/api/estimator/status").status_code == 403
    assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == 403
    assert not harness.checks


@pytest.mark.parametrize("body", [{}, {"chan": CHAN, "tenant": TENANT}, {"chan": True},
                                   {"chan": CHAN + "\n"}, {"chan": "=" * 43}, [], {"chan": "x" * 2000}])
def test_invalid_body_is_400(harness, body):
    assert harness.client.post(harness.path, json=body).status_code == 400
    assert harness.kms.sign_calls == 0


def test_duplicate_json_media_type_and_uuid_are_refused(harness):
    assert harness.client.post(harness.path, content='{"chan":"a","chan":"b"}',
                               headers={"Content-Type": "application/json"}).status_code == 400
    assert harness.client.post(harness.path, content=json.dumps({"chan": CHAN})).status_code == 400
    assert harness.client.post(harness.path.replace(PROJECT, PROJECT.replace("-", "")), json={"chan": CHAN}).status_code == 400


def test_rate_limit_has_retry_after(harness):
    for _ in range(20):
        assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == 200
    response = harness.client.post(harness.path, json={"chan": CHAN})
    assert response.status_code == 429 and 1 <= int(response.headers["retry-after"]) <= 300


@pytest.mark.parametrize("operation", ["get_public_key", "sign"])
def test_kms_errors_are_uniform_and_redacted(harness, monkeypatch, caplog, operation):
    def fail(**kwargs):
        raise TimeoutError(CHAN + " private KMS response")
    monkeypatch.setattr(harness.kms, operation, fail)
    response = harness.client.post(harness.path, json={"chan": CHAN})
    assert response.status_code == 503 and response.json() == {"detail": "estimator_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert CHAN not in caplog.text and "private KMS response" not in caplog.text


def test_optional_manifest_preserves_required_reader_shape():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "deploy/required-config.app.json").read_text())
    names = {"LEAF_ESTIMATOR_SURFACE_ENABLED", "LEAF_ESTIMATOR_HANDOFF_KMS_KEY_ID", "LEAF_ESTIMATOR_BINDINGS"}
    assert names <= set(manifest["optional"]["environment"])
    assert not names.intersection(manifest["required"]["environment"])
    assert manifest["defaults"]["LEAF_ESTIMATOR_SURFACE_ENABLED"] == "false"
    assert set(manifest["required"]) == {"environment", "mountPaths", "secrets"}


@pytest.mark.parametrize("role,expected", [("owner", 200), ("editor", 200), ("reviewer", 404), ("read_only", 404), (None, 404)])
def test_real_project_access_and_role_policy_gate_both_routes(harness, monkeypatch, role, expected):
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle as lifecycle

    binding = SimpleNamespace(platform_tenant_id=uuid.UUID(TENANT), binding_id=uuid.UUID(PROJECT))
    monkeypatch.setattr(platform_link, "require_project_access", REAL_PROJECT_ACCESS)
    monkeypatch.setattr(platform_link, "resolve_caller_binding", lambda _: binding)
    # Replace only persistence reads: the production role predicate and the
    # project's tenant binding checks remain load-bearing in this proof.
    monkeypatch.setattr(lifecycle, "_actor_tenant_role", lambda *a, **k: "owner")
    monkeypatch.setattr(lifecycle, "_project_membership_role", lambda *a, **k: role)
    monkeypatch.setattr(lifecycle, "require_project_role", lambda org, project, actor, *, write:
                        lifecycle._require_project_role(None, org, project, actor, write=write))
    assert harness.client.get("/api/estimator/status").status_code == expected
    assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == expected
    # Re-entry always reads the current membership, including a revocation.
    monkeypatch.setattr(lifecycle, "_project_membership_role", lambda *a, **k: None)
    assert harness.client.post(harness.path, json={"chan": CHAN}).status_code == 404
