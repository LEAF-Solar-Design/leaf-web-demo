"""Canonical UUID SolarEdge producers, immutable reuse and historical tracking."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from threading import Lock
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import deps
import envelopes
import solar_artifacts
import solar_project_context as project
import solar_project_graph as graphs
import solar_project_read as reads
import solar_project_solaredge as se
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from routers import project_drawings as routes
import test_sip_r3b_tools as tools
import test_solar_solaredge_report as samples


@pytest.fixture
def lane(monkeypatch):
    """Historical accepted-record fixture, using the real strict legacy importer.

    Current review behavior is exercised separately by review_lane. Recorded-order
    legacy records must remain readable without silently accepting new ambiguous
    review rows.
    """
    s = tools.memory.__wrapped__(monkeypatch)
    s.set_parent(tools.embedded(samples.small_graph()))
    s.ctx = graphs.resolve_project_graph_context(s.context())
    s.tenant = deps.TenantContext(str(s.org), org_id=str(s.org), tier="hosted_pro", subject="drafter")
    s.parses = []
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    monkeypatch.setattr(se.stored, "stored_job_entitlement_verdict", lambda *args: (None, None))
    monkeypatch.setattr(se.write_loop, "drawing_mutations_refusal", lambda: None)
    monkeypatch.setattr(deps, "find_tool", lambda name, *args: solar_tools.trusted_record(name))
    def access(tenant, proj, *, write):
        if str(tenant) != str(s.org) or proj != s.project:
            project.refuse("CONTEXT_NOT_FOUND")
        return s.org, s.actor
    monkeypatch.setattr(project, "_access", access)
    lock = Lock()
    def put(key, data):
        with lock:
            if key in s.blobs and s.blobs[key] != data:
                raise store.ImmutableConflict("owned fixture conflict")
            s.writes.append(key)
            s.blobs[key] = data
    monkeypatch.setattr(s, "put_if_absent_or_verify", put)
    def parse(data):
        s.parses.append(sha256(data).hexdigest())
        return deepcopy(samples.GRIDS)
    monkeypatch.setattr(se.reports, "parse_source", parse)
    s.current_run_import = se.reports.si.run_import
    def legacy_import(*args, **kwargs):
        return s.current_run_import(*args, **{**kwargs, "review": False})
    monkeypatch.setattr(se.reports.si, "run_import", legacy_import)
    return s


@pytest.fixture
def review_lane(lane, monkeypatch):
    """Use the actual current producer, including its explicit unresolved rows."""
    monkeypatch.setattr(se.reports.si, "run_import", lane.current_run_import)
    return lane


def upload(s, data=samples.PDF, **changes):
    args = dict(tenant=s.tenant, project_id=s.project, drawing_id=s.drawing,
                input_version_id=s.ctx.parent_version_id, data=data)
    args.update(changes)
    return se.import_source(**args)


def report(s, source=None, **changes):
    source = source or upload(s)
    request = {"source_artifact_id": source["source"]["artifact_id"],
               "alignment_tolerance": 1.0, "selection_order": "recorded"}
    request.update(changes)
    return se.build_report(s.tenant, s.project, s.drawing, s.ctx.parent_version_id, request)


def params(s, result):
    return {"expected_rev": s.ctx.graph["rev"], "report_artifact_id": result["report"]["artifact_id"]}


def accept(s, result):
    prepared = tools.prepare(s, se.ACCEPT_TOOL, params(s, result), s.ctx)
    output = tools.publish(s, prepared)
    return prepared, output, tools.output(s, output)


def refused(code, operation):
    with pytest.raises((GraphValidationError, project.ProjectContextError)) as exc:
        operation()
    assert getattr(exc.value, "code", getattr(exc.value, "reason_code", None)) == code


def read(s, context=None, **params):
    ctx = context or s.ctx
    return reads.run_project_read(s.tenant, s.project, s.drawing, ctx.parent_version_id,
        se.READ_TOOL, params, catalog_digest=tools.manifest(se.READ_TOOL))["result"]


def test_source_and_report_exact_producer_and_no_execution(lane):
    s = lane
    head, versions, calls = s.head, len(s.versions), len(s.calls)
    source = upload(s)
    assert source["schema"] == se.SOURCE_SCHEMA
    assert source["input_version_id"] == str(s.ctx.parent_version_id)
    assert source["graph_project_id"] == s.ctx.graph_project_id != str(s.project)
    assert source["source_intake_sha256"] == s.ctx.intake_sha256
    source_meta, data = se._read(s.ctx, source["source"]["artifact_id"], se.sources.SOURCE_TOOL,
        "application/pdf", se.sources.SOURCE_FILENAME)
    assert data == samples.PDF
    assert source_meta["tool_manifest_sha256"] == se.producer_digest(se.sources.SOURCE_TOOL)
    result = report(s, source)
    assert result["schema"] == se.RESULT_SCHEMA and result["counts"] == samples.MATCHED_COUNTS
    meta, stored = se._load_report(s.ctx, result["report"]["artifact_id"])
    assert stored["schema"] == se.REPORT_SCHEMA
    assert stored["source_version_id"] == str(s.ctx.parent_version_id)
    assert stored["project_id"] == str(s.project)
    assert stored["graph_project_id"] == s.ctx.graph_project_id
    assert meta["tool_manifest_sha256"] == se.producer_digest(se.reports.REPORT_TOOL)
    assert (s.head, len(s.versions), len(s.calls)) == (head, versions, calls)


def test_reuse_same_pdf_report_and_quota_without_reparse(lane):
    first = upload(lane)
    again = upload(lane)
    assert first == again
    built = report(lane, first)
    assert report(lane, again) == built
    assert len(lane.parses) == 1
    assert len([key for key in lane.blobs if "/imports/solaredge/slot-" in key]) == 1


@pytest.mark.parametrize("case", ["unmatched", "ambiguous", "competition"])
def test_unresolved_review_build_reload_and_reuse_refuses_acceptance(review_lane, monkeypatch, case):
    s = review_lane
    matrices = [] if case == "unmatched" else deepcopy(samples.GRIDS[:1] if case == "competition" else samples.GRIDS)
    def parse(data):
        s.parses.append(sha256(data).hexdigest())
        return deepcopy(matrices)
    monkeypatch.setattr(se.reports, "parse_source", parse)
    before = (s.head, len(s.versions), len(s.calls), deepcopy(s.ctx.graph))
    result = report(s)
    meta, stored = se._load_report(s.ctx, result["report"]["artifact_id"])
    assert meta["tool_manifest_sha256"] == se.producer_digest(se.reports.REPORT_TOOL)
    assert result["counts"]["matched_frames"] == 0
    assert len(stored["matches"]) == len(s.ctx.graph["frames"])
    for row in stored["matches"]:
        assert row["status"] == ("unmatched" if case == "unmatched" else "ambiguous")
        assert row["pdf_grid"] is row["pdf_matrix"] is row["pdf_sub_grid"] is None
        assert row["candidate_count"] == len(row["candidates"]) == len(matrices)
    assert stored["strings"] == []
    assert len(stored["unassigned_panel_refs"]) == len(s.ctx.graph["panels"])
    assert report(s) == result
    assert len(s.parses) == 1
    refused(se.tracking.REPORT_INVALID, lambda: se._record(s.ctx, meta, stored))
    refused(se.tracking.REPORT_INVALID, lambda: se.accept(s.ctx, params(s, result)))
    refused(se.tracking.REPORT_INVALID, lambda: tools.prepare(s, se.ACCEPT_TOOL, params(s, result), s.ctx))
    assert (s.head, len(s.versions), len(s.calls), s.ctx.graph) == before


@pytest.mark.parametrize("change", ["status", "chosen", "candidate", "count", "missing-frame", "assignment"])
def test_review_validation_refuses_malformed_unresolved_rows(review_lane, change):
    s = review_lane
    result = report(s)
    meta, stored = se._load_report(s.ctx, result["report"]["artifact_id"])
    broken = deepcopy(stored)
    row = broken["matches"][0]
    if change == "status":
        row["status"] = "matched"
    elif change == "chosen":
        row["pdf_grid"] = 0
    elif change == "candidate":
        row["candidates"][0] = True
    elif change == "count":
        row["candidate_count"] += 1
    elif change == "missing-frame":
        broken["matches"].pop()
    else:
        assigned = deepcopy(samples.MATCHED_CORE["strings"][0])
        broken["strings"] = [assigned]
        broken["unassigned_panel_refs"] = [ref for ref in stored["unassigned_panel_refs"]
            if ref not in assigned["panel_refs"]]
        broken["counts"].update(strings=1, group_strings=1,
            assigned_panels=len(assigned["panel_refs"]),
            unassigned_panels=len(broken["unassigned_panel_refs"]))
    refused(se.tracking.REPORT_INVALID, lambda: se._record(s.ctx, meta, broken, review=True))


def test_current_unique_review_still_publishes_tracking_without_entity_changes(review_lane, monkeypatch):
    s = review_lane
    panels = [{"handle": "A1", "x": 0, "y": 0}, {"handle": "A2", "x": 10, "y": 0},
        {"handle": "B1", "x": 100, "y": 0}, {"handle": "B2", "x": 110, "y": 0},
        {"handle": "B3", "x": 120, "y": 0}]
    groups = [{"block": "C1", "panels": ["A1", "A2"]}, {"block": "C2", "panels": ["B1", "B2", "B3"]}]
    s.set_parent(tools.embedded(samples.design_graph(groups, panels, 1.0)))
    s.ctx = graphs.resolve_project_graph_context(s.context())
    matrices = [samples.plain_grid([1, 2]), samples.plain_grid([1, 2, 3], inv=2)]
    monkeypatch.setattr(se.reports, "parse_source", lambda data: deepcopy(matrices))
    before = deepcopy(s.ctx.graph)
    result = report(s, selection_order="unknown")
    assert result["counts"]["matched_frames"] == len(groups)
    assert result["counts"]["assigned_panels"] == len(panels)
    _, _, accepted = accept(s, result)
    for key in ("panels", "frames", "strings", "inverters", "routes", "schedules"):
        assert accepted.graph[key] == before[key]
    output = read(s, accepted)["output"]
    assert output["accepted"] is True
    assert output["counts"]["assigned_panels"] == len(panels)


def test_explicit_association_review_reuse_reset_and_canonical_acceptance(review_lane):
    s = review_lane
    source = upload(s)
    before = (s.head, len(s.versions), deepcopy(s.ctx.graph))
    refs = [frame["id"] for frame in s.ctx.graph["frames"]]
    automatic = report(s, source, selection_order="unknown")
    partial = report(s, source, selection_order="unknown", associations={refs[0]: 1})
    assert partial["counts"]["matched_frames"] == 1
    refused(se.tracking.REPORT_INVALID, lambda: se.accept(s.ctx, params(s, partial)))
    choices = {refs[1]: 0, refs[0]: 1}
    resolved = report(s, source, selection_order="unknown", associations=choices)
    meta, stored = se._load_report(s.ctx, resolved["report"]["artifact_id"])
    assert stored["request"]["associations"] == choices
    assert [row["pdf_grid"] for row in stored["matches"]] == [1, 0]
    assert [row["pdf_inverter_id"] for row in stored["strings"]] == [2, 1]
    assert resolved["counts"]["assigned_panels"] == len(s.ctx.graph["panels"])
    assert report(s, source, selection_order="unknown", associations=dict(reversed(list(choices.items())))) == resolved
    parses = len(s.parses)
    assert report(s, source, selection_order="unknown", associations={}) == automatic
    assert len(s.parses) == parses
    assert (s.head, len(s.versions), s.ctx.graph) == before
    # The stored request cannot authorize an association for a different chosen grid.
    broken = deepcopy(stored)
    broken["matches"][0]["pdf_grid"] = 0
    refused(se.tracking.REPORT_INVALID, lambda: se._record(s.ctx, meta, broken))
    _, _, accepted = accept(s, resolved)
    output = read(s, accepted)["output"]
    assert output["accepted"] is True
    assert output["provenance"]["request"]["associations"] == choices
    for key in ("panels", "frames", "strings", "inverters", "routes", "schedules"):
        assert accepted.graph[key] == before[2][key]
    assert read(s, accepted, section="frames")["output"]["items"] == [
        {key: row[key] for key in ("frame_ref", "pdf_matrix", "pdf_grid", "pdf_sub_grid")}
        for row in stored["matches"]]


@pytest.mark.parametrize("case", ["list", "bool", "negative", "float", "unknown", "out-of-range", "duplicate"])
def test_explicit_association_refusal_preserves_source_graph_and_artifacts(review_lane, case):
    s = review_lane
    source = upload(s)
    refs = [frame["id"] for frame in s.ctx.graph["frames"]]
    choices = {"list": [], "bool": {refs[0]: True}, "negative": {refs[0]: -1},
        "float": {refs[0]: 0.0}, "unknown": {"not-a-frame": 0},
        "out-of-range": {refs[0]: 9}, "duplicate": {refs[0]: 0, refs[1]: 0}}[case]
    before = (s.head, len(s.versions), len(s.calls), deepcopy(s.ctx.graph), deepcopy(s.blobs), len(s.writes))
    refused("REPORT_ASSOCIATIONS_INVALID", lambda: report(s, source, associations=choices))
    assert (s.head, len(s.versions), len(s.calls), s.ctx.graph, s.blobs, len(s.writes)) == before


def test_accept_publication_replay_and_historical_read(lane):
    s = lane
    result = report(s)
    source_graph = deepcopy(s.ctx.graph)
    prepared, output, accepted = accept(s, result)
    assert accepted.graph["rev"] == source_graph["rev"] + 1
    assert accepted.graph["parent_rev"] == source_graph["rev"]
    for key in ("panels", "frames", "strings", "inverters", "routes", "schedules"):
        assert accepted.graph[key] == source_graph[key]
    assert accepted.graph["extra"][se.tracking.STORE_KEY]["schema"] == se.STORE_SCHEMA
    assert tools.publish(s, prepared) == output
    assert len(s.versions) == 2
    readback = read(s, accepted)
    assert readback["output"]["schema"] == se.READ_SCHEMA
    assert readback["output"]["accepted"] is True
    assert readback["output"]["references"]["current"] is True
    assert readback["output"]["counts"]["assigned_panels"] == 6
    assert readback["output"]["provenance"]["report"]["source_version_id"] == str(s.ctx.parent_version_id)
    tools.commit(s, "solar-settings", {"expected_rev": accepted.graph["rev"],
        "project_changes": {"name": "Later"}}, accepted)
    historical = read(s, accepted)
    assert historical["is_head"] is False and historical["output"] == readback["output"]
    assert read(s)["output"]["accepted"] is False
    assert len(s.parses) == 1


def test_publish_revalidates_report_bytes_before_new_version(lane):
    result = report(lane)
    prepared = tools.prepare(lane, se.ACCEPT_TOOL, params(lane, result), lane.ctx)
    key = se.artifacts._key(result["report"], result["report"]["content_sha256"], blob=True)
    lane.blobs[key] += b" "
    # Publication exposes its fixed proof refusal, not the underlying storage detail.
    refused("SIP_R3_PROOF_REJECTED", lambda: tools.publish(lane, prepared))
    assert len(lane.versions) == 1


@pytest.mark.parametrize("field", ["project_id", "drawing_id", "input_version_id"])
def test_foreign_context_refuses_before_store_write(lane, field):
    before = list(lane.writes)
    refused("SIP_R1_CONTEXT_NOT_FOUND", lambda: upload(lane, **{field: uuid4()}))
    assert lane.writes == before


def test_stale_graph_report_cannot_be_accepted(lane):
    result = report(lane)
    _, _, changed = tools.commit(lane, "solar-settings", {"expected_rev": 0,
        "project_changes": {"name": "Changed"}}, lane.ctx)
    refused(se.tracking.REPORT_STALE, lambda: tools.prepare(lane, se.ACCEPT_TOOL,
        {"expected_rev": changed.graph["rev"], "report_artifact_id": result["report"]["artifact_id"]}, changed))


@pytest.mark.parametrize("change", ["producer", "kind", "bytes", "row", "foreign-history"])
def test_tampered_report_and_history_fail_closed(lane, change):
    s = lane
    result = report(s)
    ref = result["report"]
    meta_key = se.artifacts._key(ref, ref["artifact_id"])
    if change == "foreign-history":
        _, _, ctx = accept(s, result)
        intake = ctx.intake
        intake["solar_design_graph"]["extra"][se.tracking.STORE_KEY]["report"]["source_version_id"] = str(uuid4())
        intake["solar_design_graph_sha256"] = graphs.local.digest(intake["solar_design_graph"])
        s.parent = deepcopy(s.parent)
        s.parent.version_id = uuid4()
        s.set_parent(intake)
        corrupted = graphs.resolve_project_graph_context(s.context())
        refused("SOLAREDGE_IMPORT_RECORD_INVALID", lambda: se.read_tracking(s.tenant, corrupted, {}))
        return
    if change in ("producer", "kind"):
        meta = json.loads(s.blobs[meta_key])
        meta["tool_manifest_sha256" if change == "producer" else "filename"] = (
            "sha256:" + "0" * 64 if change == "producer" else "wrong.json")
        s.blobs[meta_key] = canonical_bytes(meta)
        # Altering an artifact's producer breaks its content-addressed metadata identity too.
        refused("ARTIFACT_CORRUPT" if change == "producer" else se.tracking.REPORT_KIND_MISMATCH,
            lambda: se.accept(s.ctx, params(s, result)))
    elif change == "bytes":
        key = se.artifacts._key(ref, ref["content_sha256"], blob=True)
        s.blobs[key] += b" "
        refused("ARTIFACT_CORRUPT", lambda: se.accept(s.ctx, params(s, result)))
    else:
        meta, stored = se._load_report(s.ctx, ref["artifact_id"])
        stored["strings"][0]["panel_refs"][0] = "foreign"
        refused(se.tracking.REPORT_INVALID, lambda: se._record(s.ctx, meta, stored))


def test_atomic_quota_is_per_selected_version(lane):
    s = lane
    digests = [sha256(str(i).encode()).hexdigest() for i in range(12)]
    def reserve(value):
        try:
            se._reserve(s.ctx, value)
            return True
        except GraphValidationError as exc:
            assert exc.code == "IMPORT_QUOTA_EXCEEDED"
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, digests)) == 8
    keys = [key for key in s.blobs if "/imports/solaredge/slot-" in key]
    assert len(keys) == 8
    first = json.loads(s.blobs[keys[0]])["content_sha256"]
    se._reserve(s.ctx, first)
    se._reserve(replace(s.ctx, parent_version_id=uuid4()), digests[-1])
    assert len([key for key in s.blobs if "/imports/solaredge/slot-" in key]) == 9


@pytest.fixture
def http(lane):
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(routes.no_store_responses)
    app.include_router(routes.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: lane.tenant
    with TestClient(app) as client:
        prefix = (f"/api/projects/{lane.project}/drawings/{lane.drawing}/"
            f"versions/{lane.ctx.parent_version_id}/imports/solaredge-pdf")
        yield SimpleNamespace(client=client, prefix=prefix, s=lane)


def test_http_source_report_download_and_read(http):
    s = http.s
    source = http.client.post(http.prefix, content=samples.PDF, headers={"Content-Type": "application/pdf"})
    assert source.status_code == 200, source.text
    assert source.headers["cache-control"] == "no-store"
    result = http.client.post(http.prefix + "/report", json={
        "source_artifact_id": source.json()["source"]["artifact_id"],
        "alignment_tolerance": 1, "selection_order": "recorded"})
    assert result.status_code == 200, result.text
    ref = result.json()["report"]
    download = http.client.get(ref["download"])
    assert download.status_code == 200, download.text
    assert sha256(download.content).hexdigest() == ref["content_sha256"]
    assert json.loads(download.content)["source_version_id"] == str(s.ctx.parent_version_id)
    _, _, accepted = accept(s, result.json())
    response = http.client.post((f"/api/projects/{s.project}/drawings/{s.drawing}/"
        f"versions/{accepted.parent_version_id}/solar-reads"), json={
            "tool": se.READ_TOOL, "catalog_digest": tools.manifest(se.READ_TOOL), "params": {"section": "frames"}})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["output"]["page"]["total"] == 2


def test_http_explicit_association_review_reset_and_invalid_choice(http, monkeypatch):
    s = http.s
    monkeypatch.setattr(se.reports.si, "run_import", s.current_run_import)
    source = http.client.post(http.prefix, content=samples.PDF, headers={"Content-Type": "application/pdf"})
    assert source.status_code == 200
    request = {"source_artifact_id": source.json()["source"]["artifact_id"],
        "alignment_tolerance": 1, "selection_order": "unknown"}
    automatic = http.client.post(http.prefix + "/report", json=request)
    assert automatic.status_code == 200
    refs = [frame["id"] for frame in s.ctx.graph["frames"]]
    choices = {refs[0]: 1, refs[1]: 0}
    before = (s.head, len(s.versions), deepcopy(s.ctx.graph))
    corrected = http.client.post(http.prefix + "/report", json={**request, "associations": choices})
    assert corrected.status_code == 200, corrected.text
    assert corrected.headers["cache-control"] == "no-store"
    download = http.client.get(corrected.json()["report"]["download"])
    assert download.status_code == 200
    assert download.json()["request"]["associations"] == choices
    assert sha256(download.content).hexdigest() == corrected.json()["report"]["content_sha256"]
    assert http.client.post(http.prefix + "/report", json={**request, "associations": {}}).json() == automatic.json()
    invalid = http.client.post(http.prefix + "/report", json={**request, "associations": {refs[0]: 999}})
    assert invalid.status_code == 409
    assert invalid.json()["error"]["reason_code"] == "REPORT_ASSOCIATIONS_INVALID"
    assert invalid.headers["cache-control"] == "no-store"
    assert (s.head, len(s.versions), s.ctx.graph) == before


def test_http_current_review_report_download_is_readable_without_acceptance(http, monkeypatch):
    s = http.s
    monkeypatch.setattr(se.reports.si, "run_import", s.current_run_import)
    before = (s.head, len(s.versions), deepcopy(s.ctx.graph))
    source = http.client.post(http.prefix, content=samples.PDF, headers={"Content-Type": "application/pdf"})
    assert source.status_code == 200, source.text
    request = {"source_artifact_id": source.json()["source"]["artifact_id"],
        "alignment_tolerance": 1, "selection_order": "recorded"}
    response = http.client.post(http.prefix + "/report", json=request)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    ref = response.json()["report"]
    download = http.client.get(ref["download"])
    assert download.status_code == 200, download.text
    assert sha256(download.content).hexdigest() == ref["content_sha256"]
    stored = json.loads(download.content)
    assert stored["source"]["content_sha256"] == source.json()["source"]["content_sha256"]
    assert stored["source_version_id"] == str(s.ctx.parent_version_id)
    assert all(row["status"] == "ambiguous" and row["pdf_grid"] is None for row in stored["matches"])
    assert stored["strings"] == []
    assert http.client.post(http.prefix + "/report", json=request).json() == response.json()
    refused_flag = http.client.post(http.prefix + "/report", json={**request, "review": True})
    assert refused_flag.status_code == 400
    assert refused_flag.json()["error"]["reason_code"] == "REPORT_REQUEST_INVALID"
    refused(se.tracking.REPORT_INVALID, lambda: se.accept(s.ctx, params(s, response.json())))
    assert (s.head, len(s.versions), s.ctx.graph) == before


@pytest.mark.parametrize("path,content,media,reason", [
    ("", b"not-pdf", "application/pdf", "IMPORT_NOT_A_PDF"),
    ("", b"", "text/plain", "IMPORT_MEDIA_TYPE_REFUSED"),
    ("/report", b'{"alignment_tolerance":1,"alignment_tolerance":2}', "application/json", "REPORT_REQUEST_INVALID"),
    ("/report", b"{}", "application/json", "REPORT_REQUEST_INVALID"),
    ("/report", b"{}", "text/plain", "REPORT_MEDIA_TYPE_REFUSED"),
])
def test_http_refusals_are_no_store(http, path, content, media, reason):
    response = http.client.post(http.prefix + path, content=content, headers={"Content-Type": media})
    assert response.status_code in (400, 415), response.text
    assert response.json()["error"]["reason_code"] == reason
    assert response.headers["cache-control"] == "no-store"


def test_http_stream_size_limits(http, monkeypatch):
    monkeypatch.setattr(se.sources, "MAX_IMPORT_PDF_BYTES", 5)
    response = http.client.post(http.prefix, content=iter([b"%PDF-", b"extra"]),
        headers={"Content-Type": "application/pdf"})
    assert response.status_code == 413
    monkeypatch.setattr(se.reports, "MAX_REPORT_REQUEST_BYTES", 3)
    response = http.client.post(http.prefix + "/report", content=b"{  }",
        headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_authorization_and_drain_refuse_without_artifacts(lane, monkeypatch):
    before = list(lane.writes)
    monkeypatch.setattr(se.entitlements, "entitlements_for", lambda *args: {"upload": False})
    assert upload(lane).status_code == 403
    assert lane.writes == before
    monkeypatch.setattr(se.entitlements, "entitlements_for", lambda *args: {"upload": True})
    monkeypatch.setattr(se.write_loop, "drawing_mutations_refusal", lambda: "held")
    refused("IMPORT_WRITES_DRAINED", lambda: upload(lane))
    assert lane.writes == before


def test_real_pdf_parser_matching_and_historical_tracking(lane, monkeypatch):
    s = lane
    s.set_parent(tools.embedded(samples.intake_graph()))
    s.ctx = graphs.resolve_project_graph_context(s.context())
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("project_solaredge_real_parser", se.reports.__file__)
    actual = module_from_spec(spec)
    spec.loader.exec_module(actual)
    monkeypatch.setattr(se.reports, "parse_source", actual.parse_source)
    result = report(s, alignment_tolerance=12)
    assert result["counts"] == samples.C14_COUNTS
    _, _, accepted = accept(s, result)
    output = read(s, accepted)["output"]
    assert output["counts"]["assigned_panels"] == 3526
    assert output["counts"]["strings"] == 116
    assert output["references"]["current"] is True
