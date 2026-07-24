"""Exact catalog and drawing generation pins for conversational writes."""
from __future__ import annotations

from routers import capabilities as capabilities_router
from routers import jobs as jobs_router
from customization_store import SQLiteCustomizationStore


WRITE_TOOL = {
    "name": "arrange-panels-as-cat",
    "version": "1.0.0",
    "description": "Arrange existing panels into a cat.",
    "capabilities": ["drawing.write"],
    "entry": "tools/arrange-panels-as-cat/tool.py",
    "params": {"type": "object", "properties": {}},
}
COMMIT = "a" * 40
CATALOG = "b" * 64


def test_capability_projection_carries_server_issued_generation_pins(monkeypatch):
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [WRITE_TOOL]
    )
    monkeypatch.setattr(
        capabilities_router.customization_service,
        "effective_catalog_pin",
        lambda _tenant: {
            "catalog_commit": COMMIT,
            "effective_catalog_digest": CATALOG,
        },
    )

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="pin-tenant"
    )
    entries = [
        entry
        for family in body["families"]
        for entry in family["capabilities"]
        if entry["name"] == WRITE_TOOL["name"]
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["catalog_commit"] == COMMIT
    assert entry["effective_catalog_digest"] == CATALOG
    assert entry["tool_manifest_sha256"] == entry["catalog_digest"]


def test_capability_projection_issues_base_generation_for_fresh_tenant(monkeypatch):
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [WRITE_TOOL]
    )
    monkeypatch.setattr(
        capabilities_router.customization_service,
        "effective_catalog_pin",
        lambda _tenant: None,
    )

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="fresh-tenant"
    )
    entry = next(
        item
        for family in body["families"]
        for item in family["capabilities"]
        if item["name"] == WRITE_TOOL["name"]
    )
    expected = capabilities_router.deps.base_catalog_pin([WRITE_TOOL])
    assert entry["catalog_commit"] == expected["catalog_commit"]
    assert entry["effective_catalog_digest"] == expected["effective_catalog_digest"]


def test_fresh_enabled_tenant_uses_real_base_catalog(tmp_path, monkeypatch):
    database = tmp_path / "customization.db"
    SQLiteCustomizationStore(database).initialize()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", str(database))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.delenv("LEAF_TENANTS_DIR", raising=False)
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    capabilities_router.customization_service.reset_configured_services()

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="fresh-tenant"
    )
    entries = [
        item
        for family in body["families"]
        for item in family["capabilities"]
    ]
    writes = [
        item for item in entries if "drawing.write" in item.get("capabilities", [])
    ]

    assert writes
    assert all(item.get("catalog_commit") for item in writes)
    assert all(item.get("effective_catalog_digest") for item in writes)


def _request(**changes):
    digest = jobs_router.deps.catalog_tool_digest(WRITE_TOOL)
    values = {
        "tool": WRITE_TOOL["name"],
        "params": {},
        "dwg": "cat-panels",
        "dwg_version": 7,
        "expected_drawing_head": 7,
        "catalog_digest": digest,
        "tool_manifest_sha256": digest,
        "catalog_commit": COMMIT,
        "effective_catalog_digest": CATALOG,
    }
    values.update(changes)
    return jobs_router.RunRequest(**values)


def _run(req):
    return jobs_router.run(
        req,
        wait=0,
        tenant_id="pin-tenant",
        x_org_id=None,
        x_project_id=None,
        idempotency_key=None,
        authorization=None,
    )


def test_catalog_generation_or_manifest_drift_creates_no_job(monkeypatch):
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: WRITE_TOOL)
    monkeypatch.setattr(jobs_router, "_legacy_drawing_head", lambda *_args: 7)
    monkeypatch.setattr(
        jobs_router.customization_service,
        "effective_catalog_pin",
        lambda _tenant: {
            "catalog_commit": COMMIT,
            "effective_catalog_digest": CATALOG,
        },
    )
    submitted = []
    monkeypatch.setattr(
        jobs_router.jobs,
        "submit_job",
        lambda *_args, **_kwargs: submitted.append(True) or "unexpected-job",
    )

    stale_catalog = _run(_request(catalog_commit="c" * 40))
    stale_manifest = _run(_request(tool_manifest_sha256="sha256:" + "d" * 64))

    assert stale_catalog.status_code == 409
    assert stale_manifest.status_code == 409
    assert submitted == []


def test_backedge_write_requires_complete_exact_pins(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: WRITE_TOOL)
    monkeypatch.setattr(jobs_router.entitlements, "resolve_tier", lambda _tenant: "hosted_pro")
    submitted = []
    monkeypatch.setattr(
        jobs_router.jobs,
        "submit_job",
        lambda *_args, **_kwargs: submitted.append(True) or "unexpected-job",
    )
    req = _request(
        expected_drawing_head=None,
        catalog_commit=None,
        effective_catalog_digest=None,
        tool_manifest_sha256=None,
    )

    response = jobs_router.run(
        req,
        wait=0,
        tenant_id=jobs_router.deps.TenantContext("pin-tenant", tier="hosted_pro"),
        x_org_id=None,
        x_project_id=None,
        idempotency_key=None,
        authorization=None,
    )

    assert response.status_code == 409
    assert submitted == []
    assert "requires exact catalog and drawing pins" in response.body.decode("utf-8")
