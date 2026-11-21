import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import context_packet
import deps
from routers import author


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a")


@pytest.fixture
def packet_inputs(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("LEAF_CONVERSE_HARNESS_URL", raising=False)
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path))
    tools = []
    monkeypatch.setattr(context_packet, "_visible_tools", lambda tenant: tools)
    monkeypatch.setattr(context_packet, "_active_jobs", lambda tenant: [])
    return tools


@pytest.mark.parametrize("mode,tenant,enabled", [
    ("off", "tenant-a", False),
    ("internal", "tenant-a", True),
    ("internal", "tenant-b", False),
    ("all", "tenant-b", True),
])
def test_packet_wire_bytes(monkeypatch, packet_inputs, mode, tenant, enabled):
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", mode)
    packet = context_packet.build_packet(tenant, "drawing-a", entitlements_override={})
    if enabled:
        assert packet.pop("inline_authoring") == {"plan_mode": True}
    expected = (
        '{"catalog":[],"catalog_hash":"' + context_packet._catalog_hash([]) + '",'
        '"entitlements":{},"active_jobs":[],"grant":{"kind":"missing","degraded":true},'
        '"classifier_hint":null,"drawing":{"id":"drawing-a","head_version":null,"layers":[],'
        '"entity_total":0},"versions":[],"checkout":{"held_by":null,"expires_at":null}}'
    )
    assert context_packet._serialize(packet).encode() == expected.encode()


def test_plan_mode_survives_catalog_trimming(monkeypatch, packet_inputs):
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "all")
    packet_inputs.extend({"name": f"tool-{i}", "description": "x" * 140}
                         for i in range(context_packet.CATALOG_CAP))
    packet = context_packet.build_packet("tenant-b", "drawing-a", entitlements_override={})
    assert packet["catalog"][-1]["more"] > 0
    assert len(context_packet._serialize(packet)) < context_packet.MAX_PACKET_CHARS
    assert packet["inline_authoring"] == {"plan_mode": True}


@pytest.mark.parametrize("tenant,review_mode,expected", [
    ("tenant-a", "internal", {"plan_mode": True, "inline_review": True, "self_approval": True}),
    ("tenant-b", "internal", {"plan_mode": False, "inline_review": False, "self_approval": False}),
    ("tenant-b", "all", {"plan_mode": False, "inline_review": True, "self_approval": False}),
])
def test_flags_follow_request_principal(monkeypatch, tenant, review_mode, expected):
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "internal")
    monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", review_mode)
    app = FastAPI()
    app.include_router(author.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(tenant)
    with TestClient(app) as client:
        response = client.get("/api/author/inline-authoring", params={"tenant_id": "tenant-a"})
    assert response.status_code == 200
    assert response.json() == expected
