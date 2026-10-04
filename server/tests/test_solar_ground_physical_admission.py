"""One HTTP producer chain from licensed LandXML to head-bound physical downloads."""
import copy
import hashlib

import pytest

import catalog
import deps
import jobs
import solar_civil_operations as civil
import solar_ground_analysis as analysis
import solar_ground_buildout as bo
import solar_ground_shade as shade
import solar_physical_head as ph
import solar_tools
import store
from envelopes import install_error_handlers
from leaf_cloud_client import canonical_bytes
from routers import capabilities, drawings, solar_terrain
from routers import jobs as jobs_route
from solar_graph_context import resolve_graph_context
from solar_physical_analysis import hydrate_physical_document
from test_solar_frames_piles import (
    SQUARE_M, PRESET, TEMPLATE, MEASURED_LX_M_GENERATE, MEASURED_LX_M_PILES,
)
from test_solar_landxml_import import REAL, MEASURED_FIRST_RESULT, canonical
from test_solar_landxml_route import body_of
from test_solar_physical_state import DRAWING, PROJECT, TENANT
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api
from test_w1_solve_commit import seed

TOOLS = ("solar-physical-shade", "solar-physical-export")
# Capture these before _api installs its fixture substitutes on shared modules.
PRODUCTION_CHECKOUT_IDENTITY = jobs_route._checkout_identity
PRODUCTION_PROVENANCE = deps.effective_tools_with_provenance
PRODUCTION_FIND_TOOL = deps.find_tool
PRODUCTION_TENANT_TOOLS = deps.load_tenant_repo_tools
PRODUCTION_AUTHORED = deps._AUTHORED
PRODUCTION_RUNTIME_AUTHORITY = catalog.live_aps_runtime_authorized


@pytest.fixture
def flow(tmp_path, monkeypatch, isolated_jobs, no_network, graph):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    import broker
    preflight = broker._cap_preflight
    for client, _, tools, route, broker_module, tenant in _api(backend, tmp_path, monkeypatch):
        monkeypatch.setattr(broker_module, "_cap_preflight", preflight)
        monkeypatch.setattr(route, "_checkout_identity", PRODUCTION_CHECKOUT_IDENTITY)
        monkeypatch.setattr(deps, "effective_tools_with_provenance", PRODUCTION_PROVENANCE)
        monkeypatch.setattr(deps, "find_tool", PRODUCTION_FIND_TOOL)
        monkeypatch.setattr(deps, "load_tenant_repo_tools", PRODUCTION_TENANT_TOOLS)
        monkeypatch.setattr(deps, "_AUTHORED", PRODUCTION_AUTHORED)
        monkeypatch.setattr(catalog, "live_aps_runtime_authorized", PRODUCTION_RUNTIME_AUTHORITY)
        for tool in TOOLS:
            tools[tool] = solar_tools.trusted_record(tool)
        executor = next(iter(jobs._executors.values()))
        monkeypatch.setattr(jobs, "_executors", {
            **jobs._executors,
            **{jobs.lane_for(tools[tool], False): executor for tool in TOOLS}})
        for router in (drawings.router, solar_terrain.router, capabilities.router):
            client.app.include_router(router)
        install_error_handlers(client.app)
        monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
        monkeypatch.delenv("APS_LIVE", raising=False)
        assert store.release_checkout(backend, TENANT, DRAWING, "fixture-owner") is True
        response = client.post(f"/api/drawings/{DRAWING}/checkout",
                               json={"holder": "drafter"}, headers={"X-Tenant-Id": TENANT})
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert (receipt["acquired"], receipt["holder"]) == (True, "drafter")
        capability = receipt["checkout_capability"]
        assert isinstance(capability, str) and capability
        yield client, backend, {"X-Tenant-Id": TENANT, "X-Checkout-Capability": capability}


def route_result(response):
    assert response.status_code == 200, response.text
    return body_of(response)


def document(backend):
    return ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)


def snapshot(backend):
    return {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, DRAWING)}


def run(client, headers, discovered, tool, params, version):
    response = client.post("/api/run?wait=1", json={
        "tool": tool, "dwg": DRAWING, "dwg_version": version, "params": params,
        "catalog_digest": discovered[tool]["catalog_digest"]}, headers=headers)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True, envelope
    result = envelope["result"]
    assert result["drawing_changed"] is False
    assert result["source_version"] == version
    assert jobs.get_job(result["job_id"])["dwg_version"] == version
    return result


def test_ground_physical_admission_producer_chain(flow):
    client, backend, headers = flow
    url = f"/api/drawings/{DRAWING}"
    imported = route_result(client.post(
        url + "/imports/landxml?drawing_units=m&crs=none", content=REAL,
        headers={**headers, "Content-Type": "application/xml"}))
    assert imported["created"] is True and imported["head"]["index"] == 0
    assert imported["points"] == {"declared": 441, "accepted": 441, "skipped": 0}
    assert canonical(imported) == MEASURED_FIRST_RESULT

    def operate(previous, operation, **params):
        result = route_result(client.post(url + "/terrain/operations?project_id=" + PROJECT,
            json={"operation": operation,
                  "expected_head": previous["head"]["state"]["artifact_id"], **params},
            headers=headers))
        assert result["created"] is True and result["outcome"] == "published"
        assert result["head"]["parent"] == previous["head"]["state"]["artifact_id"]
        assert document(backend)[0] == result["head"]
        return result

    generated = operate(imported, "frame-generate", boundary=SQUARE_M,
                        preset=PRESET, drawing_units="m")
    assert generated["summary"] == MEASURED_LX_M_GENERATE
    _, preceding = document(backend)
    assert len(preceding["state"]["frames"]) == MEASURED_LX_M_GENERATE["frames_added"]
    preceding = copy.deepcopy(preceding)
    mpu = preceding["units"]["meters_per_unit"]
    expected_grade = bo.grade_multi(preceding["state"]["grid"], [SQUARE_M], mpu,
                                   mode="Auto", value_du=None, runtime=bo.RUNTIME_NET8)
    assert expected_grade["succeeded"] is True
    pad = expected_grade["pads"][0]
    graded = operate(generated, "grade-pad", boundary=SQUARE_M)
    _, graded_document = document(backend)
    pads = graded_document["state"]["grade_pads"]
    assert pads[-1] == pad
    assert pads[:-1] == preceding["state"].get("grade_pads", [])
    assert graded_document["state"]["grid"] == preceding["state"]["grid"]
    assert graded_document["state"]["frames"] == preceding["state"]["frames"]
    assert graded["summary"] == {
        "pads_added": 1, "grade_pads": len(pads), "mode": expected_grade["mode"],
        "elevation_m": pad["elevation_m"], "label": pad["label"]["text"],
        **{key: expected_grade[key] for key in ("total_cut_m3", "total_fill_m3", "net_m3")}}
    piled = operate(graded, "piling-generate", preset=PRESET, pile_template=TEMPLATE)
    assert piled["summary"] == MEASURED_LX_M_PILES
    final_head, final_document = document(backend)
    assert len(final_document["state"]["piles"]) == MEASURED_LX_M_PILES["piles"]
    assert final_document["state"]["frames"] == preceding["state"]["frames"]
    frame_handles = {frame["handle"] for frame in final_document["state"]["frames"]}
    assert all(pile["pile"]["source_tracker"] in frame_handles
               for pile in final_document["state"]["piles"])
    reopened = route_result(client.get(url + "/terrain?view=civil&project_id=" + PROJECT,
                                       headers=headers))
    assert reopened == {
        "schema": civil.VIEW_SCHEMA, "stored": True, "head": final_head,
        "preview": piled["preview"], "standing": piled["standing"],
        "grade_pads": len(final_document["state"]["grade_pads"])}

    response = client.get("/api/capabilities", params={"drawing_id": DRAWING,
        "project_id": PROJECT, "drawing_version": "head"}, headers=headers)
    catalog_result = route_result(response)
    discovered = {}
    # Require shipped winning provenance as well as the HTTP catalog projection;
    # resolver failure must fail this test, never degrade to invented discovery.
    effective = {row["name"]: (row, source) for row, source in PRODUCTION_PROVENANCE(TENANT)}
    for tool in TOOLS:
        matches = [entry for family in catalog_result["families"]
                   for entry in family["capabilities"] if entry["name"] == tool]
        assert len(matches) == 1
        entry = matches[0]
        shipped = solar_tools.trusted_record(tool)
        assert effective[tool] == (shipped, deps.TOOL_SOURCE_WRITE_SEED)
        assert entry["provenance"] == shipped.get("provenance", {})
        assert entry["solar"] == solar_tools.catalog_view(shipped)
        assert entry["catalog_digest"] == deps.catalog_tool_digest(shipped)
        assert entry["availability"]["runnable"] is True, entry["availability"]
        discovered[tool] = entry

    manifest = copy.deepcopy(store.load_manifest(backend, TENANT, DRAWING))
    context = resolve_graph_context(backend, TENANT, DRAWING, "head")
    version = context["resolved_version"]
    before_graph = copy.deepcopy(context["graph"])
    before_objects = snapshot(backend)
    inputs = hydrate_physical_document(final_document)
    sim = shade.shade_sim(inputs["frames"], inputs["dtm"], inputs["meters_per_unit"],
                          settings={}, existing_heatmap=0)
    assert sim["succeeded"] is True
    result = run(client, headers, discovered, TOOLS[0], {}, version)
    output = result["output"]
    assert (output["schema"], output["maturity"], output["scope"], output["head"]) == (
        "leaf.solar-physical-shade.v1", "preview", "cpu-terrain-native-frame-centres", final_head)
    assert output["units"] == final_document["units"]
    assert output["sample_count"] == len(sim["panels"])
    assert output["mean_shade"] == sim["mean_shade"]
    assert output["datum_shift_m"] == sim["binding"]["shift_m"]
    assert output["surface"] == sim["surface"]
    result_settings = {"mode": "defaults", "target_clearance_m": 1.5,
                       "profile_selection": "automatic"}
    assert output["settings"] == result_settings
    profile = sim["profile"]
    assert output["profile"] == {"name": profile["name"], "angle_count": len(profile["angles"]),
        **{key: profile[key] for key in ("ray_step_m", "max_ray_m", "estimated_samples")}}
    expected_frames = [{"sample_index": i, "frame_index": panel["entity"],
                        "shade": sim["result"]["weighted_per_panel"][i]}
                       for i, panel in enumerate(sim["panels"])]
    assert output["frames"] == expected_frames[:len(output["frames"])]
    assert len(output["frames"]) == min(len(expected_frames), 200)
    assert output["frames_omitted"] == len(expected_frames) - len(output["frames"])
    assert result["output_bytes"] == len(canonical_bytes(output))
    assert result["output_sha256"] == hashlib.sha256(canonical_bytes(output)).hexdigest()
    assert snapshot(backend) == before_objects
    assert store.load_manifest(backend, TENANT, DRAWING) == manifest
    current = resolve_graph_context(backend, TENANT, DRAWING, "head")
    assert current["resolved_version"] == version and current["graph"] == before_graph
    assert document(backend) == (final_head, final_document)

    expected_files = {"terrain-csv": analysis.terrain_csv_file_bytes(
        final_document["state"]["grid"], mpu), "shade-sam": sim["files"]["shade-sam"].encode("utf-8")}
    for fmt, content in expected_files.items():
        export_before = snapshot(backend)
        result = run(client, headers, discovered, TOOLS[1], {
            "expected_head": final_head["state"]["artifact_id"], "format": fmt}, version)
        output = result["output"]
        summary, ref = output["summary"], output["artifact"]
        assert output["head"] == summary["head"] == final_head
        assert (summary["schema"], summary["maturity"], summary["format"]) == (
            "leaf.solar-physical-export.v1", "preview", fmt)
        assert summary["units"] == final_document["units"]
        grid = final_document["state"]["grid"]
        assert summary["grid"] == {"rows": grid["rows"], "cols": grid["cols"],
                                   "cells": grid["rows"] * grid["cols"]}
        if fmt == "shade-sam":
            assert summary["scope"] == "cpu-terrain-native-frame-centres"
            assert summary["settings"] == result_settings
            assert summary["sample_count"] == len(sim["panels"])
            assert summary["profile"] == profile["name"]
            assert summary["mean_shade"] == sim["mean_shade"]
            assert summary["datum_shift_m"] == sim["binding"]["shift_m"]
        else:
            assert summary["scope"] == "terrain-nodes"
            assert all(summary[key] is None for key in (
                "settings", "sample_count", "profile", "mean_shade", "datum_shift_m"))
        filename = "terrain.csv" if fmt == "terrain-csv" else "shade-sam.csv"
        assert ref["filename"] == filename and ref["media_type"] == "text/csv"
        assert ref["source_version"] == version
        assert ref["byte_length"] == len(content)
        assert ref["content_sha256"] == hashlib.sha256(content).hexdigest()
        assert ref["download"] == url + "/artifacts/" + ref["artifact_id"]
        download = client.get(ref["download"], headers=headers)
        assert download.status_code == 200, download.text
        assert download.content == content
        assert int(download.headers["content-length"]) == len(content)
        assert download.headers["content-disposition"] == f'attachment; filename="{filename}"'
        assert download.headers["etag"] == '"' + ref["content_sha256"] + '"'
        assert download.headers["x-leaf-artifact-id"] == ref["artifact_id"]
        assert download.headers["x-leaf-source-version"] == str(version)
        assert store.load_manifest(backend, TENANT, DRAWING) == manifest
        current = resolve_graph_context(backend, TENANT, DRAWING, "head")
        assert current["resolved_version"] == version and current["graph"] == before_graph
        assert document(backend) == (final_head, final_document)
        after_objects = snapshot(backend)
        assert all(after_objects[key] == value for key, value in export_before.items())
        assert all("/artifacts/" in key for key in after_objects.keys() - export_before.keys())
