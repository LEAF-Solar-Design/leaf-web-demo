"""Product-path coverage for Solar's first graph seed and ordinary graph edits.

Storage environment, broker ledger and tenant table, capacity and metric hooks,
and the APS accessor are replaced with isolated filesystem harness seams.
Job execution and upload extraction run inline, and HTTP broker transport calls
the real broker in process while external network access is prohibited.
Catalog lookup, authored tools, tenant dependencies, auth and submission metadata,
runtime authorization, and backend selection use the reference fixture seams.
DXF parsing, upload and checkout routes, availability and input readiness gates,
the broker, and graph seed and local commit implementations remain real.
"""
import copy
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import broker_client
import deps
import guest_uploads
import jobs
import solar_graph_seed
import solar_local_graph as local
import store
import write_loop
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401

TENANT = "fixture-tenant"
HEADERS = {"X-Tenant-Id": TENANT}
DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nABCD\n8\nPanels\n70\n1\n"
    "10\n111.25\n20\n222.5\n10\n333.75\n20\n444.0\n10\n555.5\n20\n666.25\n"
    "0\nENDSEC\n0\nEOF\n"
).encode("utf-8")
UNITS = {
    "drawing_units": "ft",
    "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    "elevation_datum": "unknown",
    "crs": None,
}


@pytest.fixture
def product(isolated_jobs, no_network, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import drawings as drawings_route
    from routers import jobs as route
    from routers import uploads as uploads_route

    for name, value in (("LEAF_DRAWING_STORE", "legacy"), ("LEAF_DRAWING_MUTATIONS_ENABLED", "1"),
                        ("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1"),
                        ("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest")),
                        ("LEAF_UPLOADS_DIR", str(tmp_path / "uploads")),
                        ("LEAF_STORE_DIR", str(tmp_path / "store")),
                        ("BROKER_LEDGER", str(tmp_path / "ledger.jsonl")),
                        ("BROKER_TENANTS", str(tmp_path / "tenants.json")),
                        ("LEAF_BROKER_STORE", "legacy"), ("LEAF_RUNTIME_ENV", "staging")):
        monkeypatch.setenv(name, value)
    for name in ("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "LEAF_AUTH_LIVE", "LEAF_ENTITLEMENTS_FILE",
                 "LEAF_EXACT_WRITE_PINS_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    tools = {tool["name"]: tool for tool in
             json.loads((SERVER / "write_tools.json").read_text())["tools"]}

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {
        jobs.lane_for(tool, False): InlineExecutor() for tool in tools.values()})
    monkeypatch.setattr(route.deps, "find_tool", lambda name, *a: tools.get(name))
    monkeypatch.setattr(route.deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    guest_uploads._reset_rate_state()
    monkeypatch.setattr(guest_uploads, "start_extraction_thread",
                        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
                            tenant_id, drawing_id, ext))

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                return __import__("json").loads(response.body)

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    for router in (route.router, uploads_route.router, drawings_route.router):
        app.include_router(router)
    tenant = route.deps.TenantContext(TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    for dep in ("require_tenant", "require_active_tenant"):
        app.dependency_overrides[getattr(route.deps, dep)] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, tools


def upload(client):
    response = client.post(
        "/api/drawings/upload",
        files={"file": ("mine.dxf", io.BytesIO(DXF))}, headers=HEADERS)
    assert response.status_code == 202, response.text
    receipt = response.json()
    assert receipt["tenant_kind"] == "account"
    assert receipt["tenant_id"] == TENANT
    drawing_id = receipt["drawing_id"]
    assert drawing_id
    status = client.get(f"/api/drawings/{drawing_id}/upload-status", headers=HEADERS)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "ready"
    versions = client.get(f"/api/drawings/{drawing_id}/versions", headers=HEADERS)
    assert versions.status_code == 200, versions.text
    entries = versions.json()["versions"]
    assert len(entries) == 1
    assert entries[0]["v"] == 1
    return drawing_id, entries[0]["sha256"]


def run(client, tools, drawing_id, params, *, tool="solar-settings",
        capability=None, dwg_version=None):
    payload = {
        "tool": tool, "dwg": drawing_id, "params": copy.deepcopy(params),
        "catalog_digest": deps.catalog_tool_digest(tools[tool]),
    }
    if dwg_version is not None:
        payload["dwg_version"] = dwg_version
    headers = dict(HEADERS)
    if capability is not None:
        headers["X-Checkout-Capability"] = capability
    return client.post("/api/run?wait=1", json=payload, headers=headers)


def seed_params(digest):
    return {
        "expected_rev": 0, "changes": {"panels_in_sequence": 3},
        "initialize": {
            "schema_version": 1, "source_intake_sha256": digest,
            "units": copy.deepcopy(UNITS),
        },
    }


def checkout(client, drawing_id):
    response = client.post(
        f"/api/drawings/{drawing_id}/checkout",
        json={"holder": "drafter"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["acquired"] is True
    assert receipt["holder"] == "drafter"
    capability = receipt["checkout_capability"]
    assert isinstance(capability, str) and capability
    return capability


def assert_parent(backend, drawing_id, digest):
    version, key, entry = store.resolve_version_entry(backend, TENANT, drawing_id, 1)
    assert version == 1
    assert entry["sha256"] == digest == hashlib.sha256(backend.get(key)).hexdigest()
    assert store.load_manifest(backend, TENANT, drawing_id)["head"] == 1
    with pytest.raises(GraphValidationError) as exc:
        resolve_graph_context(backend, TENANT, drawing_id, "head")
    assert exc.value.code == "GRAPH_NOT_EMBEDDED"
    context = solar_graph_seed.resolve_seed_context(
        backend, TENANT, drawing_id, 1, source_intake_sha256=digest)
    assert context["seed_ready"] is True


def assert_refusal(response, reason, *, status=409):
    assert response.status_code == status, response.text
    assert response.json()["reason_code"] == reason


def assert_jobs_and_head(backend, drawing_id, count, head):
    assert len(jobs._query("SELECT job_id FROM jobs")) == count
    assert store.load_manifest(backend, TENANT, drawing_id)["head"] == head


def seed(client, tools, drawing_id, digest, capability):
    response = run(client, tools, drawing_id, seed_params(digest), capability=capability)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["schema_version"] == local.SEED_RESULT_SCHEMA
    assert result["new_version"] == {
        "drawing_id": drawing_id, "version": 2, "parent": 1}
    assert result["after_rev"] == 1
    assert result["replayed"] is False
    record = jobs.get_job(result["job_id"])
    assert record["status"] == "complete"
    assert record["dwg_version"] == 1
    assert record["provenance"]["seeded"] is True


def change(client, tools, drawing_id, capability):
    response = run(
        client, tools, drawing_id,
        {"expected_rev": 1, "changes": {"num_mppt": 2}}, capability=capability)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["schema_version"] == local.RESULT_SCHEMA
    assert result["new_version"] == {
        "drawing_id": drawing_id, "version": 3, "parent": 2}
    assert result["before_rev"] == 1
    assert result["after_rev"] == 2


def assert_graph(client, backend, drawing_id, digest, *, rev, num_mppt):
    response = client.get(f"/api/drawings/{drawing_id}/intake", headers=HEADERS)
    assert response.status_code == 200, response.text
    intake = response.json()["intake"]
    assert 111.25 in [
        coordinate for polyline in intake["polylines"]
        for point in polyline["pts"] for coordinate in point]
    assert intake["polylines"][0]["handle"] == "ABCD"
    assert intake["polylines"][0]["closed"] is True
    assert intake["layers"] == ["Panels"]
    assert intake["solar_design_graph"]["rev"] == rev
    context = resolve_graph_context(backend, TENANT, drawing_id, "head")
    assert context["current_head"] == context["resolved_version"] == rev + 1
    graph = context["graph"]
    assert graph["rev"] == rev
    assert graph["settings"]["panels_in_sequence"] == 3
    assert graph["settings"]["num_mppt"] == num_mppt
    assert graph["panels"] == []
    assert graph["strings"] == []
    assert graph["project"]["units"]["drawing_units"] == "ft"
    assert graph["source_hash"] == digest
    assert intake["solar_design_graph"] == graph


def seed_default(backend, drawing_id, digest):
    graph = resolve_graph_context(backend, TENANT, drawing_id, "head")["graph"]
    empty = solar_graph_seed.new_empty_graph(
        tenant_id=TENANT, drawing_id=drawing_id, source_hash=digest,
        units=UNITS, created_at=graph["settings"]["provenance"]["created_at"])
    default = graph["settings"]["num_mppt"]
    assert default == empty["settings"]["num_mppt"]
    return default


def test_upload_is_a_seed_parent(product):
    client, backend, _ = product
    drawing_id, digest = upload(client)
    assert_parent(backend, drawing_id, digest)


def test_ordinary_run_on_an_upload_names_the_seed(product):
    client, backend, tools = product
    drawing_id, _ = upload(client)
    response = run(client, tools, drawing_id,
                   {"expected_rev": 0, "changes": {"panels_in_sequence": 3}})
    assert_refusal(response, "graph_seed_required")
    assert_jobs_and_head(backend, drawing_id, 0, 1)


def test_seed_needs_the_real_checkout(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    response = run(client, tools, drawing_id, seed_params(digest))
    assert_refusal(response, "CHECKOUT_REQUIRED", status=403)
    assert_jobs_and_head(backend, drawing_id, 0, 1)
    response = run(client, tools, drawing_id, seed_params(digest),
                   capability="not-a-capability")
    assert response.status_code in {401, 403, 409}, response.text
    assert_jobs_and_head(backend, drawing_id, 0, 1)


def test_seed_through_the_product_path(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    seed(client, tools, drawing_id, digest, capability)
    default = seed_default(backend, drawing_id, digest)
    assert_graph(client, backend, drawing_id, digest, rev=1, num_mppt=default)
    assert_jobs_and_head(backend, drawing_id, 1, 2)


def test_ordinary_change_on_the_seeded_upload(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    seed(client, tools, drawing_id, digest, capability)
    seed_default(backend, drawing_id, digest)
    change(client, tools, drawing_id, capability)
    assert_graph(client, backend, drawing_id, digest, rev=2, num_mppt=2)
    assert_jobs_and_head(backend, drawing_id, 2, 3)


def test_correction_still_needs_strings(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    seed(client, tools, drawing_id, digest, capability)
    response = run(client, tools, drawing_id,
                   {"expected_rev": 1, "memberships": []},
                   tool="solar-correct-string", capability=capability)
    assert_refusal(response, "strings_required")
    assert_jobs_and_head(backend, drawing_id, 1, 2)


@pytest.mark.parametrize("version,reason", [
    (1, "not_current_head"), (None, "graph_already_embedded"),
])
def test_a_second_seed_is_refused_before_submission(product, version, reason):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    capability = checkout(client, drawing_id)
    seed(client, tools, drawing_id, digest, capability)
    response = run(client, tools, drawing_id, seed_params(digest),
                   capability=capability, dwg_version=version)
    assert_refusal(response, reason)
    assert_jobs_and_head(backend, drawing_id, 1, 2)


def test_the_whole_walk_leaves_two_jobs_and_releases(product):
    client, backend, tools = product
    drawing_id, digest = upload(client)
    assert_parent(backend, drawing_id, digest)
    response = run(client, tools, drawing_id,
                   {"expected_rev": 0, "changes": {"panels_in_sequence": 3}})
    assert_refusal(response, "graph_seed_required")
    assert_jobs_and_head(backend, drawing_id, 0, 1)
    response = run(client, tools, drawing_id, seed_params(digest))
    assert_refusal(response, "CHECKOUT_REQUIRED", status=403)
    assert_jobs_and_head(backend, drawing_id, 0, 1)
    capability = checkout(client, drawing_id)
    seed(client, tools, drawing_id, digest, capability)
    default = seed_default(backend, drawing_id, digest)
    assert_graph(client, backend, drawing_id, digest, rev=1, num_mppt=default)
    change(client, tools, drawing_id, capability)
    response = run(client, tools, drawing_id,
                   {"expected_rev": 2, "memberships": []},
                   tool="solar-correct-string", capability=capability)
    assert_refusal(response, "strings_required")
    assert_jobs_and_head(backend, drawing_id, 2, 3)
    response = run(client, tools, drawing_id, seed_params(digest),
                   capability=capability, dwg_version=1)
    assert_refusal(response, "not_current_head")
    assert_jobs_and_head(backend, drawing_id, 2, 3)
    response = run(client, tools, drawing_id, seed_params(digest),
                   capability=capability)
    assert_refusal(response, "graph_already_embedded")
    assert_jobs_and_head(backend, drawing_id, 2, 3)
    assert_graph(client, backend, drawing_id, digest, rev=2, num_mppt=2)
    response = client.delete(
        f"/api/drawings/{drawing_id}/checkout",
        headers={**HEADERS, "X-Checkout-Capability": capability})
    assert response.status_code == 200, response.text
    rows = jobs._query("SELECT tool, status FROM jobs")
    assert len(rows) == 2
    assert all(row["tool"] == "solar-settings" and row["status"] == "complete"
               for row in rows)

