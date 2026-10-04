"""One tenant's C14 PDF upload, report, catalog accept and tracking read over HTTP."""

import hashlib

import pytest

import checkout_capability
import deps
import jobs
import solar_artifacts
import solar_tools
import store
from envelopes import install_error_handlers
from leaf_cloud_client import canonical_bytes
from routers import drawings
from routers import jobs as jobs_route
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from test_solar_solaredge_report import (
    C14_ARTIFACT, C14_COUNTS, C14_GRAPH_SHA, PDF, SRC14, TENANT,
    cached_parse, intake_graph,
)  # noqa: F401
from test_solar_tool_solaredge_accept import C14_ACCEPT_GRAPH
from test_solar_tool_solaredge_tracking_read import OUTPUTS
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api
from test_w1_solve_commit import seed

DRAWING = "solar"
UPLOAD = f"/api/drawings/{DRAWING}/imports/solaredge-pdf"
ACCEPT = "solar-solaredge-accept"
READ = "solar-solaredge-tracking-read"
PAGE = {"offset": 0, "returned": 116, "total": 116, "next_offset": None}
# The production exchange of an X-Checkout-Capability for (holder, fence). _api
# replaces it with a lambda that grants the fixture's lease to every request;
# this file restores it, so a run proves its own checkout.
PRODUCTION_CHECKOUT_IDENTITY = jobs_route._checkout_identity


def head(backend):
    return resolve_graph_context(backend, TENANT, DRAWING, "head")


@pytest.fixture
def flow(tmp_path, monkeypatch, isolated_jobs, no_network, cached_parse):
    backend, _ = seed(tmp_path, monkeypatch, intake_graph())
    # _api supplies an in-process broker transport. Restore its cost-cap
    # admission stub (this chain must exercise admission) and its checkout
    # identity stub (each run must present the capability the checkout route
    # issued, exactly as Studio does).
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    import broker
    preflight = broker._cap_preflight
    for client, _, tools, route, broker_module, tenant in _api(backend, tmp_path, monkeypatch):
        monkeypatch.setattr(broker_module, "_cap_preflight", preflight)
        monkeypatch.setattr(route, "_checkout_identity", PRODUCTION_CHECKOUT_IDENTITY)
        for tool in (ACCEPT, READ):
            tools[tool] = solar_tools.trusted_record(tool)
        # _api installed one inline executor per lane of the tools it knew; the
        # read runs on the fast lane, so give both added tools' lanes that executor.
        executor = next(iter(jobs._executors.values()))
        monkeypatch.setattr(jobs, "_executors", {
            **jobs._executors,
            **{jobs.lane_for(tools[tool], False): executor for tool in (ACCEPT, READ)}})
        # Include the drawing routes on the same app and tenant as /api/run.
        client.app.include_router(drawings.router)
        install_error_handlers(client.app)
        monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
        monkeypatch.delenv("APS_LIVE", raising=False)
        # _api holds the fixture's lease directly in the store, which issues no
        # capability. Release it and take the drawing through the checkout route.
        assert store.release_checkout(backend, TENANT, DRAWING, "fixture-owner") is True
        response = client.post(f"/api/drawings/{DRAWING}/checkout",
                               json={"holder": "drafter"}, headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert (receipt["acquired"], receipt["holder"]) == (True, "drafter")
        capability = receipt["checkout_capability"]
        assert isinstance(capability, str) and capability
        yield client, backend, tools, capability


def upload(flow):
    client, backend = flow[:2]
    response = client.post(UPLOAD, content=PDF, headers={
        "X-Tenant-Id": TENANT, "Content-Type": "application/pdf"})
    assert response.status_code == 200, response.text
    source = response.json()["source"]
    assert source["artifact_id"] == SRC14
    assert solar_artifacts.read_artifact(backend, TENANT, DRAWING, source["artifact_id"])[1] == PDF
    assert head(backend)["resolved_version"] == 1
    return source


def build_report(flow, source):
    client, backend = flow[:2]
    response = client.post(UPLOAD + "/report", json={
        "source_artifact_id": source["artifact_id"], "alignment_tolerance": 12},
        headers={"X-Tenant-Id": TENANT})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["report"]["artifact_id"] == C14_ARTIFACT
    assert result["counts"] == C14_COUNTS
    context = head(backend)
    assert context["resolved_version"] == 1
    assert context["graph_sha256"] == C14_GRAPH_SHA
    return result["report"]


def post_run(flow, tool, params, version, capability=None):
    client, _, tools = flow[:3]
    headers = {"X-Tenant-Id": TENANT}
    if capability is not None:
        headers["X-Checkout-Capability"] = capability
    return client.post("/api/run?wait=1", json={
        "tool": tool, "dwg": DRAWING, "dwg_version": version, "params": params,
        "catalog_digest": deps.catalog_tool_digest(tools[tool])},
        headers=headers)


def run(flow, tool, params, version, capability=None):
    response = post_run(flow, tool, params, version, capability)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True, envelope
    return envelope["result"]


def accept_params(report, context):
    return {"report_artifact_id": report["artifact_id"],
            "expected_rev": context["graph"]["rev"]}


def accept(flow, report):
    backend = flow[1]
    context = head(backend)
    result = run(flow, ACCEPT, accept_params(report, context),
                 context["resolved_version"], capability=flow[3])
    assert result["new_version"] == {"drawing_id": DRAWING, "version": 2, "parent": 1}
    assert digest(head(backend)["graph"]) == C14_ACCEPT_GRAPH
    return result["new_version"]["version"]


def tracking_read(flow, version):
    result = run(flow, READ, {}, version)
    output = result["output"]
    assert output["accepted"] is True
    assert output["labels_only"] is True
    assert len(output["items"]) == 116
    assert output["page"] == PAGE
    content = canonical_bytes(output)
    assert (len(content), hashlib.sha256(content).hexdigest()) == OUTPUTS["c14"]
    assert (result["output_bytes"], result["output_sha256"]) == OUTPUTS["c14"]
    assert jobs.get_job(result["job_id"])["dwg_version"] == version == 2
    return result


def no_electrical_design(backend):
    graph = head(backend)["graph"]
    for section in ("strings", "inverters", "routes", "schedules"):
        assert graph[section] == []
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    assert (manifest["head"], manifest["latest"]) == (2, 2)


def test_solaredge_flow_upload_route(flow):
    upload(flow)


def test_solaredge_flow_report_route(flow):
    build_report(flow, upload(flow))


def test_solaredge_flow_accept_run(flow):
    accept(flow, build_report(flow, upload(flow)))


def test_solaredge_flow_tracking_read_run(flow):
    version = accept(flow, build_report(flow, upload(flow)))
    tracking_read(flow, version)


def test_solaredge_flow_no_electrical_design(flow):
    version = accept(flow, build_report(flow, upload(flow)))
    no_electrical_design(flow[1])
    tracking_read(flow, version)
    no_electrical_design(flow[1])


def test_solaredge_flow_one_chain(flow):
    # flow seeds the unchanged C14 drawing at version 1 on this one backend.
    assert head(flow[1])["graph_sha256"] == C14_GRAPH_SHA
    source = upload(flow)
    report = build_report(flow, source)
    version = accept(flow, report)
    no_electrical_design(flow[1])
    tracking_read(flow, version)
    no_electrical_design(flow[1])


def test_solaredge_flow_accept_requires_checkout(flow):
    backend = flow[1]
    report = build_report(flow, upload(flow))
    context = head(backend)
    issued = flow[3]
    # Studio's accept carries the capability its checkout returned. Without it,
    # or with one the server did not issue, the run is refused before any job.
    for capability in (None, "not-a-capability", issued[:-1]):
        response = post_run(flow, ACCEPT, accept_params(report, context),
                            context["resolved_version"], capability)
        assert response.status_code == 403, response.text
        assert response.json()["reason_code"] == "CHECKOUT_REQUIRED"
        assert jobs._query("SELECT job_id FROM jobs") == []
        assert store.load_manifest(backend, TENANT, DRAWING)["head"] == 1
        assert head(backend)["graph_sha256"] == C14_GRAPH_SHA
    with pytest.raises(checkout_capability.CapabilityRejected):
        checkout_capability.verify(
            "not-a-capability", TENANT, DRAWING,
            store.load_manifest(backend, TENANT, DRAWING)["checkout"])
    accept(flow, report)
