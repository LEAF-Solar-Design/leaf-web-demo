"""Tenant publish signatures, review rewriting, denial, and revoke boundaries."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from branch_tools import online_signer, tenant_catalog, tenant_publish
from branch_tools.package_check import check_branch_package
import customization_flags
import customization_service
from customization_authority import TenantBinding
from customization_models import ChangeState
from customization_service import CustomizationService
from customization_store import SQLiteCustomizationStore
import deps
from routers import author, tools

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NAME = "panel-gap-check"
VERSION = "1.2.0"
NOW = datetime(2026, 11, 22, 12, 0, 0, 123456, tzinfo=timezone.utc)
BASE = "/api/tool-envelopes/tenant"
REVOKE = f"/api/author/branch-tools/{NAME}/{VERSION}/revoke"
FIXTURE = Path(__file__).parent / "fixtures/branch_tools/reference-package" / NAME


def package():
    return {p.relative_to(FIXTURE).as_posix(): p.read_bytes()
            for p in FIXTURE.rglob("*") if p.is_file()}


@pytest.fixture
def signer():
    return online_signer.LocalKeySigner(ec.generate_private_key(ec.SECP256R1()))


def verify(raw, signer):
    value = json.loads(raw)
    signature = bytes.fromhex(value.pop("signature"))
    signer._private_key.public_key().verify(
        signature, online_signer.canonical_json(value), ec.ECDSA(hashes.SHA256()))
    return value


@pytest.fixture
def publishing(tmp_path, monkeypatch, signer):
    root = tmp_path / "tenant-tools"
    for key, value in {
        "LEAF_PLUGIN_BUILDER": "all", "LEAF_TENANT_TOOLS_DIR": str(root),
        "LEAF_CUSTOMIZATION_R5_MODE": "all", "LEAF_CUSTOMIZATION_R6_MODE": "all",
        "LEAF_STUDIO_INLINE_REVIEW": "all", "LEAF_CUSTOMIZATION_INTERNAL_TENANTS": "",
        "LEAF_CUSTOMIZATION_CONFIRMATION_KEY": "tenant-publish-test-key",
    }.items():
        monkeypatch.setenv(key, value)
    store = SQLiteCustomizationStore(tmp_path / "changes.db")
    store.initialize()
    created = store.create_change_set(
        tenant_id=TENANT, idempotency_key="create", base_commit="a" * 40,
        desired_platform_release="release-a", workspace_contract_digest="d" * 64,
        author_subject="auth0|author", tool_target="branch")
    staging = store.transition(
        tenant_id=TENANT, change_set_id=created.change_set_id, next_state=ChangeState.STAGING,
        expected_version=created.version, idempotency_key="staging")
    change = store.record_staged(
        tenant_id=TENANT, change_set_id=created.change_set_id, expected_version=staging.version,
        idempotency_key="staged", staged_commit="b" * 40, catalog_digest="c" * 64,
        platform_release="release-a", workspace_contract_digest="d" * 64)
    service = CustomizationService(store)
    state = SimpleNamespace(root=root, store=store, service=service, change=change,
                            files=package(), tenant=TENANT, role="owner", subject="auth0|author")
    monkeypatch.setattr(customization_service, "_binding", lambda tenant: TenantBinding(
        str(tenant), state.subject, state.role, True))
    monkeypatch.setattr(service, "_branch_package_files", lambda change: (state.files, NAME))
    monkeypatch.setattr(CustomizationService, "_staged_tool", staticmethod(
        lambda change: json.loads(state.files["tool.json"])))
    monkeypatch.setattr(service, "_verify_catalog", lambda *args: None)
    monkeypatch.setattr(service, "_verified_record_fields", lambda *args: None)
    monkeypatch.setattr(service, "_publication_policy_state", lambda tenant: (True, True))
    monkeypatch.setattr(online_signer, "configured_signer", lambda: signer)
    monkeypatch.setattr(author, "_review_calls", OrderedDict())
    app = FastAPI()
    app.include_router(author.router)
    app.include_router(tools.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
        state.tenant, subject=state.subject)
    state.client = TestClient(app)
    state.approve = lambda: service.decide_review(
        tenant=deps.TenantContext(TENANT, subject="auth0|author"), change_set_id=change.change_set_id,
        staged_commit=change.staged_commit, catalog_digest=change.catalog_digest,
        decision="approve", comment=None)
    return state


def test_self_approval_publishes_verified_reviewed_tenant_package(publishing, signer):
    state = publishing
    result = state.approve()
    assert result["status"] == "published" and result["approval_kind"] == "self"
    change = state.store.get_change_set(tenant_id=TENANT, change_set_id=state.change.change_set_id)
    assert change.state is ChangeState.PUBLISHED
    result = state.service._published_branch_result(change)
    assert result["catalog"] == BASE and "dev_catalog" not in result
    raw = state.client.get(BASE + f"/{NAME}/{VERSION}/envelope").content
    value = verify(raw, signer)
    assert value["audience_tenant"] == TENANT and value["tiers"] == ["self_hosted", "studio_free"]
    assert "." not in value["signed_at"] and value["signed_at"].endswith("Z")
    zipped = state.client.get(BASE + f"/{NAME}/{VERSION}/package").content
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        files = {name[len(NAME) + 1:]: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["tool.json"])
    assert manifest["review"] == {"status": "reviewed"}
    original = json.loads(state.files["tool.json"])
    original["review"] = {"status": "reviewed"}
    assert manifest == original
    digest = check_branch_package(files, NAME).package_digest
    assert value["package"]["package_digest"] == digest == result["package_digest"]
    assert digest != hashlib.sha256(zipped).hexdigest()
    assert verify(state.client.get(BASE + "/revocations").content, signer)["entries"] == []
    state.tenant = OTHER
    assert state.client.get(BASE).json() == {"tools": []}


@pytest.mark.parametrize("capability", ["files.write", "net.http", "files.write:/tmp", "net.http:https://example.com"])
def test_forbidden_capabilities_write_nothing(tmp_path, signer, capability):
    files = package()
    manifest = json.loads(files["tool.json"])
    manifest["capabilities"] = [capability]
    files["tool.json"] = json.dumps(manifest).encode()
    with pytest.raises(tenant_publish.TenantPublishError) as error:
        tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, NOW)
    assert (error.value.code, error.value.status_code) == ("capability_not_allowed", 422)
    assert capability in error.value.failures[0]["message"]
    assert list(tmp_path.iterdir()) == []


def test_missing_signer_never_marks_change_published(publishing, monkeypatch, signer):
    monkeypatch.setattr(online_signer, "configured_signer", lambda: None)
    with pytest.raises(customization_service.CustomizationServiceError) as error:
        publishing.approve()
    assert (error.value.code, error.value.status_code) == ("signer_unavailable", 503)
    change = publishing.store.get_change_set(tenant_id=TENANT, change_set_id=publishing.change.change_set_id)
    assert change.state is not ChangeState.PUBLISHED
    assert not publishing.root.exists()
    monkeypatch.setattr(online_signer, "configured_signer", lambda: signer)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    result = publishing.service.request_publication(
        tenant=deps.TenantContext(TENANT, subject="auth0|author"),
        change_set_id=publishing.change.change_set_id)
    assert result["status"] == "published"
    change = publishing.store.get_change_set(tenant_id=TENANT, change_set_id=publishing.change.change_set_id)
    assert change.state is ChangeState.PUBLISHED


def test_capability_error_survives_service_mapping(publishing):
    manifest = json.loads(publishing.files["tool.json"])
    manifest["capabilities"] = ["files.write:/tmp"]
    publishing.files["tool.json"] = json.dumps(manifest).encode()
    with pytest.raises(customization_service.CustomizationServiceError) as error:
        publishing.approve()
    assert (error.value.code, error.value.status_code) == ("capability_not_allowed", 422)
    assert "files.write" in error.value.failures[0]["message"]
    assert not publishing.root.exists()


@pytest.mark.parametrize("fail_on", [1, 2])
def test_signing_failure_exposes_no_catalog_files(tmp_path, signer, fail_on):
    class Unavailable:
        key_id = signer.key_id
        calls = 0

        def sign(self, message):
            self.calls += 1
            if self.calls == fail_on:
                raise online_signer.SignerUnavailable("offline")
            return signer.sign(message)

    with pytest.raises(tenant_publish.TenantPublishError, match="signer_unavailable"):
        tenant_publish.publish_tenant_tool(package(), NAME, TENANT, Unavailable(), tmp_path, NOW)
    assert list(tmp_path.iterdir()) == []


def test_revoke_tenant_and_role_boundaries(publishing, signer):
    state = publishing
    state.approve()
    digest = state.client.get(BASE).json()["tools"][0]["package_digest"]
    state.tenant = OTHER
    assert state.client.post(REVOKE, json={"reason": "withdrawn"}).status_code == 404
    state.tenant, state.role = TENANT, "read_only"
    assert state.client.post(REVOKE, json={"reason": "withdrawn"}).status_code == 403
    assert len(state.client.get(BASE).json()["tools"]) == 1
    state.role = "editor"
    assert state.client.post(REVOKE, json={"reason": "withdrawn"}).status_code == 200
    assert state.client.get(BASE).json() == {"tools": []}
    assert state.client.get("/api/author/branch-tools").json()["tools"][0]["revoked"] is True
    raw = state.client.get(BASE + "/revocations").content
    entries = verify(raw, signer)["entries"]
    assert len(entries) == 1 and entries[0]["package_digest"] == digest
    assert entries[0]["reason"] == "withdrawn"
    assert state.client.post(REVOKE, json={"reason": "again"}).status_code == 200
    assert verify(state.client.get(BASE + "/revocations").content, signer)["entries"] == entries
    state.tenant = OTHER
    assert state.client.post(REVOKE, json={"reason": "again"}).status_code == 404


@pytest.mark.parametrize("reason", ["", "x" * 201])
def test_revoke_reason_bounds(publishing, reason):
    publishing.approve()
    assert publishing.client.post(REVOKE, json={"reason": reason}).status_code == 422
    assert len(publishing.client.get(BASE).json()["tools"]) == 1


def test_revoke_rate_limit(publishing):
    publishing.approve()
    for _ in range(30):
        assert publishing.client.post(REVOKE, json={"reason": "withdrawn"}).status_code == 200
    assert publishing.client.post(REVOKE, json={"reason": "withdrawn"}).status_code == 429


def test_idempotence_conflict_and_deterministic_archive(tmp_path, signer):
    files = package()
    first = tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, NOW)
    before = tenant_catalog.envelope_bytes(tmp_path, TENANT, NAME, VERSION)
    assert tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, NOW) == first
    assert tenant_catalog.envelope_bytes(tmp_path, TENANT, NAME, VERSION) == before
    tenant_publish.publish_tenant_tool(dict(reversed(list(files.items()))), NAME, OTHER, signer, tmp_path, NOW)
    assert tenant_catalog.package_path(tmp_path, TENANT, NAME, VERSION).read_bytes() == (
        tenant_catalog.package_path(tmp_path, OTHER, NAME, VERSION).read_bytes())
    files["main.csx"] += b"\n// different content\n"
    with pytest.raises(tenant_publish.TenantPublishError, match="branch_version_conflict"):
        tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, NOW)
    assert tenant_catalog.envelope_bytes(tmp_path, TENANT, NAME, VERSION) == before


def test_refresh_preserves_revocations_and_publish_does_not_unrevoke(tmp_path, signer):
    files = package()
    first = tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, NOW)
    tenant_publish.revoke_tenant_tool(tmp_path, TENANT, NAME, VERSION, "withdrawn", signer, NOW)
    entries = verify(tenant_catalog.revocations_bytes(tmp_path, TENANT), signer)["entries"]
    later = NOW + timedelta(days=4)
    tenant_publish.refresh_revocations(tmp_path, TENANT, signer, later)
    refreshed = verify(tenant_catalog.revocations_bytes(tmp_path, TENANT), signer)
    assert refreshed["entries"] == entries
    assert refreshed["issued_at"] == tenant_publish.timestamp(later)
    assert tenant_publish.publish_tenant_tool(files, NAME, TENANT, signer, tmp_path, later) == first
    assert tenant_catalog.list_versions(tmp_path, TENANT) == []


def test_old_revocations_refreshed_best_effort(publishing, signer, monkeypatch):
    state = publishing
    state.approve()
    old = datetime.now(timezone.utc) - timedelta(days=4)
    raw = online_signer.sign_revocations(signer, entries=[], issued_at=tenant_publish.timestamp(old))
    tenant_catalog.write_revocations(state.root, TENANT, raw)
    refreshed = state.client.get(BASE + "/revocations").content
    assert verify(refreshed, signer)["issued_at"] > verify(raw, signer)["issued_at"]
    tenant_catalog.write_revocations(state.root, TENANT, raw)
    monkeypatch.setattr(online_signer, "configured_signer", lambda: None)
    assert state.client.get(BASE + "/revocations").content == raw


@pytest.mark.parametrize("raw", [b"{", b"{}", b'{"issued_at":"invalid"}', b'{"issued_at":null}',
                                  b'{"issued_at":"2026-11-22T12:00:00"}'])
def test_malformed_revocation_timestamp_serves_stored_list(publishing, monkeypatch, raw):
    monkeypatch.setattr(tenant_catalog, "revocations_bytes", lambda *args: raw)
    response = publishing.client.get(BASE + "/revocations")
    assert response.status_code == 200
    assert response.content == raw


@pytest.mark.parametrize("selected,internal,invited,expected", [
    ("all", False, False, True), ("internal", True, False, True),
    ("internal", False, True, False), ("1", False, True, True),
    ("1", True, False, True), ("true", True, True, False), ("off", True, True, False),
])
def test_plugin_builder_rollout(monkeypatch, selected, internal, invited, expected):
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER", selected)
    monkeypatch.setenv("LEAF_TENANT_TOOLS_DIR", "/catalog")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", TENANT if internal else "")
    monkeypatch.setenv("LEAF_PLUGIN_BUILDER_TENANTS", TENANT if invited else "")
    assert customization_flags.plugin_builder_enabled(TENANT) is expected
    monkeypatch.setenv("LEAF_TENANT_TOOLS_DIR", " ")
    assert not customization_flags.plugin_builder_enabled(TENANT)
