"""Synthetic observations only; no runtime client or real fixtures required."""

from copy import deepcopy
from contextlib import contextmanager
from itertools import permutations, product
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from benches import x05
from benches.x05 import score


def observation():
    project = dict(id="synthetic", drawing_fixture="drawing", report_fixture="report",
                   panel_layer_contains="synthetic-panels", drawing_units="m",
                   installation_design="Roof", branch_max_offset=1,
                   alignment_tolerance=0.1, expected_modules=4, expected_grids=2,
                   expected_strings=2)
    result = dict(id="synthetic", setup_verified=True, import_verdict="PASS",
                  panel_count=4, frame_count=2, frame_refs=["frame-a", "frame-b"],
                  report={"counts": {"pdf_panels": 4, "matchable_grids": 2,
                                     "frames": 2, "matched_frames": 2},
                          "matches": [dict(frame_ref=f, pdf_grid=i, pdf_matrix=i,
                                           pdf_sub_grid=-1, candidates=[i], candidate_count=1)
                                      for i, f in enumerate(["frame-a", "frame-b"])],
                          "strings": [{"index": 0}, {"index": 1}]})
    return project, result


def test_exact_grid_rows_and_truthful_limits():
    project, result = observation()
    steps = score([project], [result])
    assert [s.n for s in steps] == list(range(1, 8))
    assert [s.verdict for s in steps] == ["PASS", "PASS"] + ["UNREADABLE"] * 5
    assert steps[2].observed == [{"report_string_rows": 2, "expected_strings": 2}]


@pytest.mark.parametrize("status", ["unmatched", "ambiguous"])
def test_concrete_unresolved_mapping_fails(status):
    project, result = observation()
    result["report"]["matches"][0].update(status=status, pdf_grid=None)
    assert score([project], [result])[1].verdict == "FAIL"


@pytest.mark.parametrize("field", ["frame_ref", "pdf_grid"])
def test_duplicate_association_fails(field):
    project, result = observation()
    rows = result["report"]["matches"]
    rows[1][field] = rows[0][field]
    if field == "pdf_grid":
        rows[1]["candidates"] = [0]
    assert score([project], [result])[1].verdict == "FAIL"


@pytest.mark.parametrize("field", ["panel_count", "frame_count"])
def test_wrong_setup_count_is_unreadable(field):
    project, result = observation()
    result[field] += 1
    assert score([project], [result])[1].verdict == "UNREADABLE"


def test_auth_unreadable():
    project, result = observation()
    result.update(status=403)
    assert all(s.verdict == "UNREADABLE" for s in score([project], [result]))


def test_missing_project_cannot_pass_aggregate():
    project, result = observation()
    missing = dict(project, id="missing")
    assert all(s.verdict == "UNREADABLE" for s in score([project, missing], [result]))


@pytest.mark.parametrize("projects,results", [([], []), ([{}], []), (None, None)])
def test_empty_or_invalid_input_cannot_pass(projects, results):
    steps = score(projects, results)
    assert len(steps) == 7
    assert all(s.verdict == "UNREADABLE" for s in steps)


def test_partial_report_cannot_pass():
    project, result = observation()
    del result["report"]["matches"][1]
    assert score([project], [result])[1].verdict == "FAIL"
    del result["report"]["counts"]
    assert score([project], [result])[1].verdict == "UNREADABLE"


def test_duplicate_results_and_missing_setup_cannot_pass():
    project, result = observation()
    assert score([project], [result, deepcopy(result)])[0].verdict == "UNREADABLE"
    del project["drawing_units"]
    assert score([project], [result])[0].verdict == "UNREADABLE"


def test_successful_fixture_does_not_hide_failed_fixture():
    project, result = observation()
    other = dict(project, id="other")
    failed = dict(result, id="other", import_verdict="FAIL")
    assert score([project, other], [result, failed])[0].verdict == "FAIL"


def test_unknown_report_format_is_unreadable():
    project, result = observation()
    result["report"]["matches"][0] = {"association": "unknown"}
    assert score([project], [result])[1].verdict == "UNREADABLE"


@pytest.mark.parametrize("field", ["counts", "matches", "frame_refs", "candidates"])
@pytest.mark.parametrize("value", [None, {}, "synthetic", 1, True, [[]], [None], [{"bad": []}]])
def test_nested_grid_container_mutations_are_named_and_offline(field, value):
    project, result = observation()
    if field == "frame_refs":
        result[field] = value
    elif field == "candidates":
        result["report"]["matches"][0][field] = value
    else:
        result["report"][field] = value
    steps = score([project], [result])
    assert steps[1].verdict == "UNREADABLE"
    assert "unreadable" in steps[1].evidence


@pytest.mark.parametrize("value", [False, [], {}, float("nan"), float("inf"), -1, 10**100])
def test_nested_report_numeric_fields_reject_unknown_shapes(value):
    project, result = observation()
    for key in result["report"]["counts"]:
        changed = deepcopy(result)
        changed["report"]["counts"][key] = value
        assert score([project], [changed])[1].verdict == "UNREADABLE"
    for key in ("pdf_grid", "pdf_matrix", "candidate_count"):
        changed = deepcopy(result)
        changed["report"]["matches"][0][key] = value
        assert score([project], [changed])[1].verdict == "UNREADABLE"


def test_grid_score_rejects_non_object_observation():
    project, _ = observation()
    for value in (None, [], "unknown"):
        assert x05._grid_score(project, value) == ("UNREADABLE", "report_wire_unreadable")


def test_inverse_coverage_rejects_foreign_frame_and_grid():
    project, result = observation()
    result["report"]["matches"][1].update(frame_ref="foreign-frame", pdf_grid=8, candidates=[8])
    assert score([project], [result])[1].verdict == "FAIL"


def test_independent_pdf_count_must_match_before_matching_is_graded():
    project, result = observation()
    result["report"]["counts"]["pdf_panels"] = 3
    result["report"]["matches"][0]["status"] = "unmatched"
    assert score([project], [result])[1].verdict == "UNREADABLE"


def test_report_refusal_requires_exact_setup_counts():
    project, result = observation()
    result["report_product_failure"] = True
    assert score([project], [result])[1].verdict == "FAIL"
    result["frame_count"] = 1
    assert score([project], [result])[1].verdict == "UNREADABLE"


def test_selected_grid_can_have_multiple_structural_candidates():
    project, result = observation()
    result["report"]["matches"][0].update(candidates=[0, 1], candidate_count=2)
    assert score([project], [result])[1].verdict == "PASS"


@pytest.mark.parametrize("candidates,count", [([], 1), ([1], 1), ([0, 0], 2), ([0, 1], 1)])
def test_invalid_selected_candidates_are_unreadable(candidates, count):
    project, result = observation()
    result["report"]["matches"][0].update(candidates=candidates, candidate_count=count)
    assert score([project], [result])[1].verdict == "UNREADABLE"


def null_observation():
    project, result = observation()
    project.update(expected_grids=None, grid_count_policy="match_or_named")
    result["report"]["counts"]["matchable_grids"] = 29
    for row, grid in zip(result["report"]["matches"], (7, 41)):
        row.update(pdf_grid=grid, pdf_matrix=grid + 3, candidates=[grid, 99],
                   candidate_count=8)
    return project, result


def test_null_policy_accepts_noncontiguous_grids_and_capped_candidates_without_oracle():
    project, result = null_observation()
    assert x05._setup(project)
    for count in (0, 1, 29):
        result["report"]["counts"]["matchable_grids"] = count
        steps = score([project], [result])
        assert [s.verdict for s in steps] == ["PASS", "PASS"] + ["UNREADABLE"] * 5
        assert "match_or_named_grid_coverage" in steps[1].evidence


@pytest.mark.parametrize("statuses", [("unmatched", "ambiguous"),
                                     ("ambiguous", None), ("unmatched", None)])
def test_null_policy_names_unresolved_statuses_including_single_candidate_ambiguity(statuses):
    project, result = null_observation()
    for row, status in zip(result["report"]["matches"], statuses):
        if status is not None:
            candidates = [77] if status == "ambiguous" else []
            row.update(status=status, pdf_grid=None, pdf_matrix=None, pdf_sub_grid=None,
                       candidates=candidates, candidate_count=len(candidates))
    result["report"]["counts"]["matched_frames"] = statuses.count(None)
    steps = score([project], [result])
    assert [s.verdict for s in steps] == ["PASS", "PASS"] + ["UNREADABLE"] * 5
    assert "normalized status causes (not emitted row codes)" in steps[1].evidence
    for status in set(statuses) - {None}:
        assert "SOLAREDGE_GRID_" + status.upper() in steps[1].evidence


@pytest.mark.parametrize("broken", ["missing", "foreign", "duplicate-frame", "duplicate-grid",
                                    "extra", "duplicate-saved-frame"])
def test_null_policy_requires_exact_saved_frame_coverage_and_unique_matched_grids(broken):
    project, result = null_observation()
    rows = result["report"]["matches"]
    if broken == "missing":
        rows.pop()
        result["report"]["counts"]["matched_frames"] = 1
    elif broken == "extra":
        rows.append(dict(rows[0], frame_ref="extra", pdf_grid=99, candidates=[99]))
        result["report"]["counts"]["matched_frames"] = 3
    elif broken == "foreign":
        rows[1]["frame_ref"] = "foreign"
    elif broken == "duplicate-frame":
        rows[1]["frame_ref"] = rows[0]["frame_ref"]
    elif broken == "duplicate-grid":
        rows[1].update(pdf_grid=7, candidates=[7])
    else:
        result["frame_refs"][1] = result["frame_refs"][0]
    assert score([project], [result])[1].verdict in ("FAIL", "UNREADABLE")


@pytest.mark.parametrize("path,value", [
    (("panel_count",), None), (("panel_count",), 3), (("frame_count",), 0),
    (("frame_count",), True), (("frame_count",), 3), (("frame_refs",), None),
    (("frame_refs",), ["frame-a", " "]), (("frame_refs",), [[], "frame-b"]),
    (("report",), None), (("report", "counts"), []), (("report", "matches"), {}),
    (("report", "matches", 0), None),
    (("report", "matches", 0, "status"), None),
    (("report", "matches", 0, "status"), []),
    (("report", "matches", 0, "status"), "matched"),
    (("report", "matches", 0, "pdf_grid"), None),
    (("report", "matches", 0, "pdf_matrix"), True),
    (("report", "matches", 0, "pdf_sub_grid"), -2),
    (("report", "matches", 0, "candidates"), None),
    (("report", "matches", 0, "candidates"), [[7]]),
    (("report", "matches", 0, "candidates"), [True]),
    (("report", "matches", 0, "candidates"), [7, 7]),
    (("report", "matches", 0, "candidates"), []),
    (("report", "matches", 0, "candidates"), [99]),
    (("report", "matches", 0, "candidate_count"), 1),
    (("report", "matches", 0, "candidate_count"), 2**53),
])
def test_null_policy_malformed_observations_are_named_unreadable(path, value):
    project, result = null_observation()
    container = result
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    step = score([project], [result])[1]
    assert step.verdict == "UNREADABLE"
    assert "unreadable" in step.evidence or "blocker" in step.evidence


@pytest.mark.parametrize("field", ["pdf_panels", "matchable_grids", "frames", "matched_frames"])
def test_null_policy_counts_are_safe_integers_and_matched_count_is_recomputed(field):
    project, result = null_observation()
    for value in (None, True, -1, 1.5, 2**53):
        result["report"]["counts"][field] = value
        assert score([project], [result])[1].verdict == "UNREADABLE"
    result["report"]["counts"][field] = 1
    assert score([project], [result])[1].verdict == ("PASS" if field == "matchable_grids" else "UNREADABLE")


@pytest.mark.parametrize("status,candidates,count", [
    ("unmatched", [7], 1), ("ambiguous", [], 0), ("ambiguous", [7], 2),
    ("ambiguous", [7, 7], 2), ("unmatched", [], True),
])
def test_null_policy_unresolved_candidates_must_agree_with_status(status, candidates, count):
    project, result = null_observation()
    result["report"]["matches"][0].update(
        status=status, pdf_grid=None, pdf_matrix=None, pdf_sub_grid=None,
        candidates=candidates, candidate_count=count)
    result["report"]["counts"]["matched_frames"] = 1
    assert score([project], [result])[1].verdict == "UNREADABLE"


@pytest.mark.parametrize("field", ["pdf_grid", "pdf_matrix", "pdf_sub_grid"])
@pytest.mark.parametrize("missing", [False, True])
def test_null_policy_unresolved_assignments_must_be_explicit_nulls(field, missing):
    project, result = null_observation()
    row = result["report"]["matches"][0]
    row.update(status="unmatched", pdf_grid=None, pdf_matrix=None, pdf_sub_grid=None,
               candidates=[], candidate_count=0)
    if missing:
        del row[field]
    else:
        row[field] = 0
    result["report"]["counts"]["matched_frames"] = 1
    assert score([project], [result])[1].verdict == "UNREADABLE"


def test_null_policy_refusal_requires_readable_saved_frame_setup():
    project, result = null_observation()
    result["report_product_failure"] = True
    del result["report"]
    assert score([project], [result])[1].verdict == "FAIL"
    for field in ("panel_count", "frame_count", "frame_refs", "setup_verified"):
        changed = dict(result)
        del changed[field]
        assert score([project], [changed])[1].verdict == "UNREADABLE"


def test_null_policy_missing_counts_and_non_object_results_are_unreadable():
    project, result = null_observation()
    for value in (None, [], "unknown"):
        assert x05._grid_score(project, value) == ("UNREADABLE", "report_wire_unreadable")
    for field in result["report"]["counts"]:
        changed = deepcopy(result)
        del changed["report"]["counts"][field]
        assert score([project], [changed])[1].verdict == "UNREADABLE"


def live_harness(monkeypatch, *, failure=None, health=None, server_code=None, envelope=None,
                 installation_design="Roof", saved_strings=None, unresolved=False,
                 report_artifact="e" * 64, saved_graph=None):
    """Exact owner-confirmed client signatures, with no browser or disk IO."""
    import xbench_session

    project, result = observation()
    if unresolved:
        result["report"]["matches"][1].update(status="unmatched", pdf_grid=None,
            pdf_matrix=None, pdf_sub_grid=None, candidates=[], candidate_count=0)
        result["report"]["counts"]["matched_frames"] = 1
    project["installation_design"] = installation_design
    events, requests, receipts = [], [], []
    mutation_keys = set()
    source_sha = "a" * 40
    ctx = SimpleNamespace(manifest={"projects": [project]}, fixtures=[],
                          base="https://synthetic.invalid", source_sha=source_sha,
                          out_dir="synthetic-output")
    health_values = iter(health if health is not None else [source_sha, source_sha])

    def health_read(base):
        assert base == ctx.base
        events.append("health")
        value = next(health_values)
        if isinstance(value, Exception):
            raise value
        return value

    @contextmanager
    def signed_in(repo, source, *, origin):
        assert source == source_sha and origin == ctx.base
        events.append("open")
        try:
            yield object(), {"web_build": source_sha}
        finally:
            events.append("close")
            if failure == "combined":
                raise RuntimeError("synthetic-private-close-canary")

    class Client:
        def __init__(self, transport, /):
            self.stage = 0
            assert transport("GET", "/api/probe") == (200, {"ok": True})
            assert transport("GET", "/api/bytes") == (200, b"synthetic")

        def create_project(self, name):
            assert name.startswith("xbench-x05-")
            ctx.created_name = name
            events.append("create")
            return {"project_id": "new-project", "org_id": "new-org"}

        def tools(self):
            return [{"name": "solar-panel-groups", "label": "Normal grouping"}]

        def upload_drawing(self, filename, content, *, engine=None):
            assert filename.endswith(".dwg") and content == b"drawing"
            assert engine is None
            events.append("upload")
            if failure == "upload":
                raise RuntimeError("synthetic upload failure")
            return "source-drawing"

        def wait_upload(self, drawing_id, timeout_s=300):
            assert drawing_id == "source-drawing" and timeout_s == 300
            events.append("wait-upload")
            if failure == "upload-envelope":
                return envelope
            return {"status": "ready"}

        def adopt_drawing(self, project_id, source_drawing_id, name, version=1):
            assert (project_id, source_drawing_id, version) == ("new-project", "source-drawing", 1)
            assert name
            events.append("adopt")
            return {"drawing_id": "adopted-drawing", "version_id": "v0"}

        def drawing_context(self, project_id, version_id, drawing_id, /):
            assert (project_id, drawing_id) == ("new-project", "adopted-drawing")
            assert version_id in (f"v{self.stage}", f"v{self.stage - 1}")
            events.append(("context", version_id, self.stage))
            return {"context": {"head_version_id": f"v{self.stage}",
                                "is_head": version_id == f"v{self.stage}",
                                "organization_id": "new-org", "project_id": project_id,
                                "drawing_id": drawing_id, "intake_sha256": "b" * 64},
                    "intake": {"solar_design_graph": deepcopy(saved_graph) if
                        self.stage >= 4 and saved_graph is not None else {"rev": self.stage,
                        "panels": [{}] * (4 if self.stage >= 2 else 0),
                        "frames": [{"id": ref} for ref in ["frame-a", "frame-b"]]
                                  if self.stage >= 3 else [],
                        "strings": saved_strings if self.stage >= 4 else []}}}

        def checkout(self, project_id, drawing_id, ttl_s=900):
            assert (project_id, drawing_id, ttl_s) == ("new-project", "adopted-drawing", 900)
            events.append("checkout")
            if failure == "checkout":
                raise RuntimeError("synthetic checkout failure")
            if failure == "checkout-envelope":
                return envelope
            return "private-capability", {}

        def run(self, tool, /, *, org_id, project_id, version_id, params,
                capability=None, idempotency_key=None, persist=False, wait=False):
            assert (org_id, project_id, version_id) == ("new-org", "new-project", f"v{self.stage}")
            assert params["drawing_id"] == "adopted-drawing"
            assert params["expected_rev"] == self.stage
            assert capability == "private-capability"
            assert isinstance(idempotency_key, str) and idempotency_key.startswith("xbench-x05-")
            assert idempotency_key not in mutation_keys
            mutation_keys.add(idempotency_key)
            assert persist is False and wait is False
            events.append(tool)
            if tool == "solar-settings":
                assert "project_changes" not in params
                assert params["changes"] == {"panel_layer_contains": project["panel_layer_contains"]}
                assert params["initialize"]["source_intake_sha256"] == "b" * 64
                assert params["initialize"]["units"]["drawing_units"] == "m"
            elif tool == "solar-panel-groups":
                assert params["geometry_only"] is True
                assert params["discover"] == {"BranchMaxOffset": 1, "AlignmentTolerance": 0.1,
                                               "InstallationDesign": installation_design}
            elif tool == "solar-solaredge-accept":
                assert params == {"drawing_id": "adopted-drawing", "expected_rev": 3,
                    "report_artifact_id": "e" * 64,
                    **({"accept_matched_only": True} if unresolved else {})}
                if failure == "accept":
                    return 422, {"error": {"code": "synthetic-private-canary"}}
                if failure == "accept-unknown":
                    return 200, {"unexpected": "synthetic-private-canary"}
                if failure in ("accept-async", "accept-job-failed", "accept-job-no-result"):
                    return 202, {"job_id": "accept-job"}
            else:
                assert tool == "solar-panels-from-drawing"
                return 202, {"job_id": "panels-job"}
            self.stage += 1
            if tool == "solar-solaredge-accept":
                version = "v2" if failure == "accept-stale-version" else f"v{self.stage}"
                return 200, {"graph_commit": {"output_version_id":
                    "v-wrong" if failure == "accept-receipt-mismatch" else version},
                    "output_version_id": version, "history_operation_id": "synthetic-operation",
                    "history_hash": "a" * 64, "execution_provenance": {}}
            return 200, {"output": {}}

        def wait_job(self, job_id, timeout_s=300):
            assert job_id in ("panels-job", "accept-job") and timeout_s == 300
            events.append("wait-job")
            if failure == "mutation" or (job_id == "accept-job" and failure == "accept-job-failed"):
                return {"status": "failed"}
            self.stage += 1
            if job_id == "accept-job" and failure != "accept-job-no-result":
                return {"status": "complete", "result": {
                    "graph_commit": {"output_version_id": f"v{self.stage}"},
                    "output_version_id": f"v{self.stage}", "history_operation_id": "synthetic-operation",
                    "history_hash": "a" * 64, "execution_provenance": {}}}
            return {"status": "complete"}

        def release(self, project_id, drawing_id, capability):
            assert (project_id, drawing_id, capability) == ("new-project", "adopted-drawing", "private-capability")
            events.append("release")
            if failure in ("cleanup", "combined"):
                raise RuntimeError("synthetic release failure")

        def delete_project(self, project_id):
            assert project_id == "new-project"
            events.append("delete")
            if failure in ("cleanup", "combined"):
                raise RuntimeError("synthetic delete failure")

    def api(page, method, path, *, origin, json_body=None, data=None, headers=None, timeout_s=60.0):
        pdf_post = method == "POST" and "/imports/solaredge-pdf" in path
        assert origin == ctx.base and timeout_s == (300 if pdf_post else 60.0)
        requests.append((method, path, json_body))
        if path == "/api/probe":
            payload = {"ok": True}
        elif path == "/api/bytes":
            return 200, {"content-type": "application/octet-stream"}, b"synthetic"
        elif path.endswith("/report"):
            assert method == "POST" and json_body == {"source_artifact_id": "d" * 64,
                "alignment_tolerance": 0.1, "selection_order": "unknown"}
            if failure == "combined":
                raise RuntimeError("synthetic-private-report-canary")
            if failure == "report-envelope":
                return 200, {"content-type": "application/json"}, json.dumps(envelope).encode()
            if failure == "report-refusal":
                return 422, {"content-type": "application/json"}, json.dumps({
                    "error": {"code": server_code if server_code is not None else "REPORT_GRID_UNMATCHED"}}).encode()
            payload = {"report": {"download": "/api/report-artifact", "artifact_id": report_artifact}}
        elif path == "/api/report-artifact":
            assert method == "GET"
            payload = {"schema": "leaf.solar-project-solaredge-report.v1", **result["report"]}
        else:
            assert method == "POST" and path.endswith("/imports/solaredge-pdf")
            assert data == b"report" and headers == {"Content-Type": "application/pdf"}
            if failure in ("source-auth", "source-refusal"):
                return (403 if failure == "source-auth" else 422), {"content-type": "application/json"}, json.dumps({
                    "error": {"code": server_code if server_code is not None else "IMPORT_PDF_INVALID"}}).encode()
            payload = {"schema": "leaf.solar-project-import-source.v1",
                       "source": {"artifact_id": ("c" if "/v1/" in path else "d") * 64}}
        return 200, {"content-type": "application/json; charset=utf-8"}, json.dumps(payload).encode()

    monkeypatch.setitem(sys.modules, "xbench_studio", SimpleNamespace(StudioClient=Client))
    monkeypatch.setattr(xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(xbench_session, "api", api)
    monkeypatch.setattr(x05, "read_health", health_read)
    monkeypatch.setattr(x05, "_fixture", lambda ctx, identifier: identifier.encode())
    monkeypatch.setattr(x05, "write_json_atomic", lambda path, payload: receipts.append(payload))
    return ctx, events, requests, receipts


@pytest.mark.parametrize("installation_design", ["Roof", "Ground"])
def test_live_client_contract_and_current_version_routes(monkeypatch, installation_design):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, installation_design=installation_design)
    assert x05._setup(ctx.manifest["projects"][0])
    assert [s.verdict for s in x05.run(ctx)] == ["PASS", "PASS"] + ["UNREADABLE"] * 5
    base = "/api/projects/new-project/drawings/adopted-drawing/versions/"
    source_routes = [path for method, path, _ in requests if path.endswith("/solaredge-pdf")]
    assert source_routes == [base + "v1/imports/solaredge-pdf", base + "v3/imports/solaredge-pdf"]
    assert ("POST", base + "v3/imports/solaredge-pdf/report",
            {"source_artifact_id": "d" * 64, "alignment_tolerance": 0.1,
             "selection_order": "unknown"}) in requests
    assert events[0] == "health"
    assert events[-4:] == ["release", "delete", "close", "health"]
    assert events.count("wait-job") == 1
    assert events.count("solar-panel-groups") == 1
    assert "solar-solaredge-accept" not in events
    assert ("GET", "/api/report-artifact", None) in requests
    assert "private-capability" not in json.dumps(receipts)


@pytest.mark.parametrize("installation_design", ["Roof", "Ground"])
@pytest.mark.parametrize("failure", [None, "report-refusal"])
def test_null_policy_collects_report_with_actual_frames_and_cleans_up(monkeypatch,
                                                                   installation_design, failure):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, installation_design=installation_design, failure=failure)
    ctx.manifest["projects"][0].update(expected_grids=None, grid_count_policy="match_or_named")
    steps = x05.run(ctx)
    assert [s.verdict for s in steps] == ["PASS", "FAIL" if failure else "PASS"] + ["UNREADABLE"] * 5
    assert receipts[0]["results"][0]["frame_count"] == 2
    assert any(path.endswith("/report") for _, path, _ in requests)
    assert events[-4:] == ["release", "delete", "close", "health"]
    assert events.count("release") == events.count("delete") == 1
    if failure is None:
        assert ("GET", "/api/report-artifact", None) in requests
    else:
        assert "product_report_refusal" in steps[1].evidence


@pytest.mark.parametrize("policy", [None, "exact", "MATCH_OR_NAMED", "match_or_named ", [], {}])
def test_null_grid_requires_explicit_policy_before_any_io(monkeypatch, policy):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    project = ctx.manifest["projects"][0]
    project["expected_grids"] = None
    if policy is not None:
        project["grid_count_policy"] = policy
    assert not x05._setup(project)
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert events == [] and requests == []
    assert receipts[0]["results"] == []


@pytest.mark.parametrize("field,value", [
    ("expected_modules", None), ("expected_strings", 0), ("expected_grids", True),
    ("expected_grids", 0), ("expected_grids", 2**53),
    ("branch_max_offset", float("inf")), ("alignment_tolerance", False),
])
def test_null_policy_preserves_other_numeric_setup_guards_without_io(monkeypatch, field, value):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    project = ctx.manifest["projects"][0]
    project.update(expected_grids=None, grid_count_policy="match_or_named")
    project[field] = value
    assert not x05._setup(project)
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert events == [] and requests == []


def test_explicit_policy_does_not_weaken_positive_exact_grid_contract():
    project, result = observation()
    project["grid_count_policy"] = "match_or_named"
    result["report"]["matches"][0].update(status="unmatched", pdf_grid=None)
    assert score([project], [result])[1].verdict == "FAIL"
    project, result = observation()
    project["grid_count_policy"] = "match_or_named"
    result["report"]["matches"][0].update(pdf_grid=7, candidates=[7])
    assert score([project], [result])[1].verdict == "FAIL"


@pytest.mark.parametrize("failure", ["upload", "checkout", "mutation"])
def test_live_lifecycle_cleanup_on_failure(monkeypatch, failure):
    ctx, events, requests, receipts = live_harness(monkeypatch, failure=failure)
    steps = x05.run(ctx)
    assert steps[1].verdict == "UNREADABLE"
    assert steps[0].verdict == ("PASS" if failure == "mutation" else "UNREADABLE")
    assert events.count("delete") == 1
    assert events.count("release") == (1 if failure == "mutation" else 0)
    assert events[-3:] == ["delete", "close", "health"]


def test_cleanup_errors_preserve_evidence_and_are_sanitized(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch, failure="cleanup")
    assert [s.verdict for s in x05.run(ctx)][:2] == ["PASS", "PASS"]
    assert receipts[0]["results"][0]["cleanup_errors"] == [
        "CHECKOUT_RELEASE_UNREADABLE", "PROJECT_DELETE_UNREADABLE"]
    assert events[-4:] == ["release", "delete", "close", "health"]


def test_source_drift_invalidates_all_observations(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch, health=["a" * 40, "b" * 40])
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert receipts[0]["results"][0]["reason"] == "source_identity_unreadable"


def test_unreadable_final_health_invalidates_observations(monkeypatch):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, health=["a" * 40, RuntimeError("synthetic health failure")])
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert events[-4:] == ["release", "delete", "close", "health"]


def test_initial_source_mismatch_prevents_browser_start(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch, health=["b" * 40, "b" * 40])
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert events == ["health", "health"] and requests == []


@pytest.mark.parametrize("failure,verdicts", [
    ("source-auth", ["UNREADABLE", "UNREADABLE"]),
    ("source-refusal", ["FAIL", "UNREADABLE"]),
    ("report-refusal", ["PASS", "FAIL"]),
])
def test_live_refusals_distinguish_auth_from_product(monkeypatch, failure, verdicts):
    ctx, events, requests, receipts = live_harness(monkeypatch, failure=failure)
    assert [s.verdict for s in x05.run(ctx)][:2] == verdicts
    assert events[-4:] == ["release", "delete", "close", "health"]


@pytest.mark.parametrize("projects", [[], [{}], None])
def test_missing_setup_does_not_open_browser_or_read_health(monkeypatch, projects):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    ctx.manifest["projects"] = projects
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert events == [] and requests == []


def test_unconfigured_drawing_keeps_corpus_coverage_unreadable(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    ctx.manifest["fixtures"] = [{"id": "drawing", "role": "drawing"},
                                {"id": "held-out", "role": "drawing"}]
    steps = x05.run(ctx)
    assert all(step.verdict == "UNREADABLE" for step in steps)
    assert steps[0].observed == {"projects": 2}
    assert events.count("create") == 1 and events.count("delete") == 1


def test_invalid_sibling_does_not_suppress_configured_drawing(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    ctx.manifest["projects"].append({"id": "missing-setup"})
    steps = x05.run(ctx)
    assert all(step.verdict == "UNREADABLE" for step in steps)
    assert events.count("upload") == 1 and events.count("delete") == 1
    assert len(receipts[0]["results"]) == 2


@pytest.mark.parametrize("expected_grids", [1, 3])
def test_ground_discovery_requires_exact_grid_count_and_cleans_up(monkeypatch, expected_grids):
    ctx, events, requests, receipts = live_harness(monkeypatch, installation_design="Ground")
    ctx.manifest["projects"][0]["expected_grids"] = expected_grids
    steps = x05.run(ctx)
    assert [step.verdict for step in steps] == ["PASS"] + ["UNREADABLE"] * 6
    assert "discovery_count_blocker" in steps[1].evidence
    assert events.count("solar-panel-groups") == 1
    assert events[-4:] == ["release", "delete", "close", "health"]
    assert events.count("release") == events.count("delete") == 1
    source_routes = [path for method, path, _ in requests if path.endswith("/solaredge-pdf")]
    assert source_routes == [
        "/api/projects/new-project/drawings/adopted-drawing/versions/v1/imports/solaredge-pdf"]
    assert not any(path.endswith("/report") or path == "/api/report-artifact"
                   for _, path, _ in requests)
    assert receipts[0]["results"][0]["frame_count"] == 2


@pytest.mark.parametrize("installation_design", [
    "ground", "GROUND", "roof", "Roof ", " Ground", "", "Wall", None, True, 1,
    [], {}, ["Ground"],
])
def test_unsupported_installation_design_is_unreadable_without_io(monkeypatch, installation_design):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, installation_design=installation_design)
    assert not x05._setup(ctx.manifest["projects"][0])
    assert all(step.verdict == "UNREADABLE" for step in x05.run(ctx))
    assert events == [] and requests == []
    assert receipts[0]["results"] == []


@pytest.mark.parametrize("field", ["expected_modules", "expected_grids", "expected_strings",
                                   "branch_max_offset", "alignment_tolerance"])
@pytest.mark.parametrize("value", [None, 0, True, False, float("nan"), float("inf"),
                                   float("-inf"), -1, 10**400])
@pytest.mark.parametrize("installation_design", ["Roof", "Ground"])
def test_numeric_setup_boundaries_are_unreadable_without_io(monkeypatch, field, value,
                                                           installation_design):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, installation_design=installation_design)
    ctx.manifest["projects"][0][field] = value
    steps = x05.run(ctx)
    assert all(step.verdict == "UNREADABLE" for step in steps)
    assert "setup_or_session_unreadable" in steps[0].evidence
    assert events == [] and requests == []
    assert receipts[0]["results"] == []


@pytest.mark.parametrize("stage,code", [("upload", "SOURCE_WIRE_UNREADABLE"),
                                       ("checkout", "CONTEXT_DRIFT"),
                                       ("report", "REPORT_WIRE_UNREADABLE")])
@pytest.mark.parametrize("envelope", [None, [], {"unexpected": "synthetic-canary"}])
def test_success_status_malformed_envelopes_cleanup(monkeypatch, stage, code, envelope):
    ctx, events, requests, receipts = live_harness(
        monkeypatch, failure=stage + "-envelope", envelope=envelope)
    steps = x05.run(ctx)
    assert steps[1].verdict == "UNREADABLE"
    assert receipts[0]["results"][0]["reason_code"] == code
    assert events.count("delete") == events.count("close") == 1
    assert events.count("release") == (1 if stage == "report" else 0)
    assert "synthetic-canary" not in json.dumps(receipts)


def test_report_and_all_cleanup_failures_preserve_sanitized_evidence(monkeypatch, tmp_path):
    from xbench_core import write_json_atomic

    ctx, events, requests, receipts = live_harness(monkeypatch, failure="combined")
    ctx.out_dir = tmp_path
    monkeypatch.setattr(x05, "write_json_atomic", write_json_atomic)
    assert all(step.verdict == "UNREADABLE" for step in x05.run(ctx))
    assert events[-4:] == ["release", "delete", "close", "health"]
    text = (tmp_path / "x05-evidence.json").read_text(encoding="utf-8")
    assert "canary" not in text and "private-capability" not in text
    observed = json.loads(text)["results"][0]
    assert observed["reason"] == "session_contract_unreadable"
    assert observed["cleanup_errors"] == ["CHECKOUT_RELEASE_UNREADABLE", "PROJECT_DELETE_UNREADABLE"]


def test_score_indexes_projects_and_results_linearly():
    class CountedDict(dict):
        reads = 0

        def get(self, *args):
            type(self).reads += 1
            return super().get(*args)

    for count in (100, 400):
        project, result = observation()
        projects = [CountedDict(project, id=f"p{i}") for i in range(count)]
        results = [CountedDict(result, id=f"p{i}") for i in range(count)]
        CountedDict.reads = 0
        assert [s.verdict for s in score(projects, results)][:2] == ["PASS", "PASS"]
        assert CountedDict.reads < 40 * count
        assert score(projects, results[:-1])[0].verdict == "UNREADABLE"
        assert score(projects, results + [results[0]])[0].verdict == "UNREADABLE"
        assert score(projects + [projects[0]], results)[0].verdict == "UNREADABLE"
        mixed = list(results)
        mixed[-1] = dict(mixed[-1], import_verdict="FAIL")
        assert score(projects, mixed)[0].verdict == "FAIL"


@pytest.mark.parametrize("failure", ["source-refusal", "report-refusal"])
@pytest.mark.parametrize("injected", [
    "IMPORT_CANARY_SECRET_DO_NOT_PERSIST",
    "REPORT_" + "SYNTHETIC_PRIVATE_MARKER_" * 500,
    "IMPORT_PDF_INVALID\nAuthorization: synthetic-canary",
    {"token": "synthetic-private-canary"},
])
def test_server_codes_are_allowlisted_in_actual_evidence_file(monkeypatch, tmp_path, failure, injected):
    from xbench_core import write_json_atomic

    ctx, events, requests, receipts = live_harness(
        monkeypatch, failure=failure, server_code=injected)
    ctx.out_dir = tmp_path
    monkeypatch.setattr(x05, "write_json_atomic", write_json_atomic)
    steps = x05.run(ctx)
    text = (tmp_path / "x05-evidence.json").read_text(encoding="utf-8")
    assert all(step.verdict != "FAIL" for step in steps)
    for marker in ("CANARY_SECRET", "SYNTHETIC_PRIVATE_MARKER", "synthetic-canary", "synthetic-private-canary"):
        assert marker not in text
    evidence = json.loads(text)
    emitted = evidence["results"][0]["reason_code"]
    assert emitted == "SERVER_REASON_UNRECOGNISED"
    assert emitted in x05.SAFE_REASON_CODES and len(emitted) <= x05.MAX_REASON_CODE


@pytest.mark.parametrize("where", ["manifest", "verified"])
@pytest.mark.parametrize("record", [None, [], {"id": []}, {"id": {}}, {"id": "../bad"},
                                     {"id": "x" * 129}, {"id": "bad\x00id"}])
def test_malformed_fixture_metadata_is_named_and_offline(monkeypatch, where, record):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    if where == "manifest":
        ctx.manifest["fixtures"] = [record]
    else:
        ctx.fixtures = [record]
    steps = x05.run(ctx)
    assert all(step.verdict == "UNREADABLE" for step in steps)
    assert all(step.evidence == "FIXTURE_METADATA_UNREADABLE" for step in steps)
    assert receipts[0]["reason_code"] == "FIXTURE_METADATA_UNREADABLE"
    assert events == [] and requests == []


@pytest.mark.parametrize("value", [[], {}, ["drawing"], "../drawing", "", "d" * 129])
def test_bad_drawing_reference_never_opens_network(monkeypatch, value):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    ctx.manifest["projects"][0]["drawing_fixture"] = value
    assert all(step.verdict == "UNREADABLE" for step in x05.run(ctx))
    assert receipts[0]["reason_code"] == "PROJECT_METADATA_UNREADABLE"
    assert events == [] and requests == []


@pytest.mark.parametrize("broken", [None, "missing", "duplicate-grid", "duplicate-frame"])
def test_project_result_frame_and_match_permutations_preserve_verdicts(broken):
    first, observed = observation()
    projects = [first, dict(first, id="other")]
    results = [observed, deepcopy(observed)]
    results[1]["id"] = "other"
    rows = results[1]["report"]["matches"]
    if broken == "missing":
        rows.pop()
    elif broken == "duplicate-grid":
        rows[1].update(pdf_grid=0, candidates=[0])
    elif broken == "duplicate-frame":
        rows[1]["frame_ref"] = rows[0]["frame_ref"]
    expected = [step.verdict for step in score(projects, results)]
    assert expected[1] == ("PASS" if broken is None else "FAIL")
    orders = list(permutations(range(2)))
    for project_order, result_order, frame_order, match_order in product(orders, repeat=4):
        changed = deepcopy(results)
        for result in changed:
            result["frame_refs"] = [result["frame_refs"][i] for i in frame_order]
            if len(result["report"]["matches"]) == 2:
                result["report"]["matches"] = [result["report"]["matches"][i] for i in match_order]
        steps = score([projects[i] for i in project_order], [changed[i] for i in result_order])
        assert [step.verdict for step in steps] == expected


def pinned_harness(monkeypatch, tmp_path, *, change=None, raw=None, **kwargs):
    fixture_reader = x05._fixture
    ctx, events, requests, receipts = live_harness(monkeypatch, **kwargs)
    oracle = {"status": "READABLE", "source": {
        "bytes": 6, "sha256": hashlib.sha256(b"report").hexdigest()},
        "totals": {"modules": 4, "optimizers": 2, "strings": 2, "inverters": 1},
        "strings": [{"identifier": "1.1", "optimizers": 1, "modules": 2,
                     "model": "synthetic-private-canary"},
                    {"identifier": "1.2", "optimizers": 1, "modules": 2}]}
    if change:
        change(oracle)
    payload = json.dumps(oracle).encode() if raw is None else raw
    ctx.manifest["projects"][0]["oracle_fixture"] = "oracle"
    ctx.manifest["fixtures"] = []
    for identifier, data in (("drawing", b"drawing"), ("report", b"report"),
                             ("oracle", payload)):
        path = tmp_path / (identifier + ".fixture")
        path.write_bytes(data)
        ctx.manifest["fixtures"].append({"id": identifier, "path": str(path),
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    ctx.fixtures = [{"id": identifier, "ok": True} for identifier in
                    ("drawing", "report", "oracle")]
    monkeypatch.setattr(x05, "_fixture", fixture_reader)
    return ctx, events, requests, receipts


def electrical_strings():
    return [{"id": f"leaf:string:00000000-0000-4000-8000-{i:012d}"} for i in (1, 2)]


@pytest.mark.parametrize("saved,verdict,reason", [
    ([], "FAIL", "product_saved_string_count_mismatch"),
    (electrical_strings(), "UNREADABLE", "saved_optimizer_measurements_unavailable"),
])
def test_pinned_oracle_normal_accept_reads_saved_electrical_count(monkeypatch, tmp_path,
                                                               saved, verdict, reason):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, saved_strings=saved)
    steps = x05.run(ctx)
    assert [s.verdict for s in steps] == ["PASS", "PASS", verdict] + ["UNREADABLE"] * 4
    assert reason in steps[2].evidence
    assert steps[2].observed == [{"report_string_rows": 2, "expected_strings": 2}]
    observed = receipts[0]["results"][0]
    assert observed["saved_string_count"] == len(saved)
    assert observed["accepted"] is observed["saved_head_read"] is True
    accepted = events.index("solar-solaredge-accept")
    assert events[accepted + 1:accepted + 3] == [("context", "v3", 4), ("context", "v4", 4)]
    assert events.count("release") == events.count("delete") == 1
    text = json.dumps(receipts)
    assert "leaf:string:" not in text and "synthetic-private-canary" not in text


@pytest.mark.parametrize("change", [
    lambda o: o.update(status="UNREADABLE"),
    lambda o: o["source"].update(bytes=7),
    lambda o: o["source"].update(sha256="f" * 64),
    lambda o: o["totals"].update(strings=3),
    lambda o: o["totals"].update(modules=6),
    lambda o: o["totals"].update(optimizers=3),
    lambda o: o["totals"].update(inverters=2),
    lambda o: o["strings"][0].update(identifier="01.1"),
    lambda o: o["strings"][0].update(identifier="1.2"),
    lambda o: o["strings"][0].update(identifier="١.1"),
    lambda o: o["strings"][0].update(modules=True),
    lambda o: o["strings"][0].update(optimizers=0),
    lambda o: o["strings"][0].update(modules=2**53),
    lambda o: o["strings"].extend([o["strings"][0]] * 20000),
])
def test_invalid_oracle_is_named_sanitized_and_never_mutates(monkeypatch, tmp_path, change):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, change=change)
    assert all(s.verdict == "UNREADABLE" for s in x05.run(ctx))
    assert receipts[0]["results"][0]["reason_code"] == "ORACLE_UNREADABLE"
    assert "create" not in events and not any(e == "solar-settings" for e in events)
    assert "canary" not in json.dumps(receipts)


@pytest.mark.parametrize("raw", [
    b'{"status":"READABLE","status":"READABLE"}',
    b'{"source":{"bytes":6,"bytes":6}}',
    b'{"value":NaN}', b'{"value":Infinity}', b'{"value":1e999}',
    b'\xffsynthetic-private-canary', b'[' + b' ' * (5 * 1024 * 1024) + b']',
])
def test_bad_oracle_json_is_sanitized_before_mutation(monkeypatch, tmp_path, raw):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, raw=raw)
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert receipts[0]["results"][0]["reason_code"] == "ORACLE_UNREADABLE"
    assert "create" not in events and "canary" not in json.dumps(receipts)


@pytest.mark.parametrize("binding", ["missing", "report", "foreign", "bad-pin"])
def test_oracle_fixture_pin_and_project_binding_required(monkeypatch, tmp_path, binding):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path)
    if binding == "foreign":
        ctx.fixtures = [f for f in ctx.fixtures if f["id"] != "oracle"]
    elif binding == "bad-pin":
        ctx.manifest["fixtures"][-1]["sha256"] = "0" * 64
    else:
        ctx.manifest["projects"][0]["oracle_fixture"] = binding
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert receipts[0]["results"][0]["reason_code"] == "ORACLE_UNREADABLE"
    assert "create" not in events


@pytest.mark.parametrize("saved", [None, {}, [None], ["leaf:string:bad"],
                                   [{"id": "bad"}], electrical_strings() * 2])
def test_malformed_saved_strings_never_establish_failure(monkeypatch, tmp_path, saved):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, saved_strings=saved)
    steps = x05.run(ctx)
    assert steps[2].verdict == "UNREADABLE"
    assert "saved_string_count" not in receipts[0]["results"][0]
    assert events.count("release") == events.count("delete") == 1


@pytest.mark.parametrize("failure", ["accept", "accept-unknown", "accept-job-failed"])
def test_acceptance_failure_preserves_uncertainty_and_cleanup(monkeypatch, tmp_path, failure):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, failure=failure)
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert "saved_string_count" not in receipts[0]["results"][0]
    assert events.count("release") == events.count("delete") == 1
    assert "canary" not in json.dumps(receipts)


@pytest.mark.parametrize("artifact", [None, "e" * 63, "E" * 64, {"secret": "canary"}])
def test_accept_requires_report_envelope_artifact_pin(monkeypatch, tmp_path, artifact):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, report_artifact=artifact)
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert "solar-solaredge-accept" not in events
    assert events.count("release") == events.count("delete") == 1


@pytest.mark.parametrize("unresolved", [False, True])
def test_explicit_null_grid_uses_matched_only_for_valid_unresolved_rows(monkeypatch, tmp_path,
                                                                     unresolved):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path,
                                                   unresolved=unresolved, saved_strings=[])
    ctx.manifest["projects"][0].update(expected_grids=None, grid_count_policy="match_or_named")
    assert [s.verdict for s in x05.run(ctx)][:3] == ["PASS", "PASS", "FAIL"]
    assert events.count("solar-solaredge-accept") == 1


def test_step3_unreadable_sibling_takes_precedence_over_count_failure():
    project, result = observation()
    project["oracle_fixture"] = "oracle"
    result.update(oracle_readable=True, accepted=True, saved_head_read=True, saved_string_count=0)
    assert score([project], [result])[2].verdict == "FAIL"
    sibling = dict(project, id="other")
    missing = dict(result, id="other", accepted=False)
    for projects, results in (([project, sibling], [result, missing]),
                              ([sibling, project], [missing, result])):
        assert score(projects, results)[2].verdict == "UNREADABLE"


def test_self_consistent_oracle_still_requires_frozen_project_counts(monkeypatch, tmp_path):
    def change(oracle):
        oracle["totals"]["modules"] = 6
        oracle["strings"][0]["modules"] = 4

    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, change=change)
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert "create" not in events


def test_invalid_null_grid_rows_never_enable_matched_only(monkeypatch, tmp_path):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, unresolved=True)
    ctx.manifest["projects"][0].update(expected_grids=None, grid_count_policy="match_or_named")
    # Corrupt only the mocked report download; the independent oracle stays valid.
    import xbench_session

    original = xbench_session.api

    def malformed(*args, **kwargs):
        status, headers, body = original(*args, **kwargs)
        if args[2] == "/api/report-artifact":
            report = json.loads(body)
            report["matches"][1]["candidates"] = [0]
            body = json.dumps(report).encode()
        return status, headers, body

    monkeypatch.setattr(xbench_session, "api", malformed)
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert "solar-solaredge-accept" not in events
    assert events.count("release") == events.count("delete") == 1


def test_async_normal_accept_counts_fresh_head_only_after_completion(monkeypatch, tmp_path):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path,
                                                   failure="accept-async", saved_strings=[])
    assert x05.run(ctx)[2].verdict == "FAIL"
    accepted = events.index("solar-solaredge-accept")
    assert events[accepted + 1:accepted + 4] == [
        "wait-job", ("context", "v3", 4), ("context", "v4", 4)]
    assert receipts[0]["results"][0]["saved_string_count"] == 0
    assert events.count("release") == events.count("delete") == 1


@pytest.mark.parametrize("failure", ["accept-stale-version", "accept-receipt-mismatch", "accept-job-no-result"])
def test_accept_completion_must_bind_receipt_and_independently_read_head(monkeypatch, tmp_path, failure):
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, failure=failure, saved_strings=[])
    assert x05.run(ctx)[2].verdict == "UNREADABLE"
    assert receipts[0]["results"][0]["reason_code"] == "SAVED_STRINGS_UNREADABLE"
    assert events.count("release") == events.count("delete") == 1
    assert "synthetic-private-canary" not in json.dumps(receipts)


def ui_contract():
    """Complete numeric scorer inputs; no claim that optimizer totals ship."""
    project, result = observation()
    project.update(collect_reconciliation_ui=True, oracle_fixture="oracle", expected_kw_dc="2")
    values = dict(zip(x05.METRICS, ("4", "2", "1", "2", "2")))
    rows = [dict(metric=m, studio=v, oracle=v, delta="0", verdict="PASS")
            for m, v in values.items()]
    ui = dict(before_rows=rows, reopened_rows=deepcopy(rows),
              before_drafts=dict(values), reopened_drafts=dict(values),
              saved_revision_before=7, saved_revision_after=7,
              reopen_observed=True, recommendations=[{"group_count": 2}],
              grouping_status="proposals_shown")
    ui["csv_text"] = table_csv(rows)
    result.update(oracle_readable=True, accepted=True, saved_head_read=True,
                  saved_string_count=2, ui_head_stable=True, saved_values=dict(values),
                  oracle_values=dict(values), strings_sha256_before="a" * 64,
                  strings_sha256_after="a" * 64, ui_observation=ui)
    return project, result


def table_csv(rows):
    return ",".join(x05.COLUMNS) + "\n" + "\n".join(
        ",".join(row[key] for key in x05.COLUMNS) for row in rows) + "\n"


def test_reopen_different_revision_cannot_pass_with_equal_content():
    project, result = ui_contract()
    result["ui_observation"]["saved_revision_after"] = 8
    steps = score([project], [result])
    assert steps[6].verdict == "FAIL"
    assert [s.verdict for s in steps[3:6]] == ["PASS"] * 3


@pytest.mark.parametrize("key", ["saved_revision_before", "saved_revision_after"])
@pytest.mark.parametrize("value", [None, True, False, -1, 7.0, "7", 10 ** 1000])
def test_reopen_unreadable_revision_preserves_other_stages(key, value):
    project, result = ui_contract()
    result["ui_observation"][key] = value
    steps = score([project], [result])
    assert steps[6].verdict == "UNREADABLE"
    assert [s.verdict for s in steps[3:6]] == ["PASS"] * 3


@pytest.mark.parametrize("key", ["saved_revision_before", "saved_revision_after"])
def test_reopen_missing_revision_is_unreadable(key):
    project, result = ui_contract()
    del result["ui_observation"][key]
    assert score([project], [result])[6].verdict == "UNREADABLE"


def test_complete_ui_scorer_contract_preserves_step3_uncertainty():
    project, result = ui_contract()
    steps = score([project], [result])
    assert [s.verdict for s in steps] == ["PASS", "PASS", "UNREADABLE"] + ["PASS"] * 4
    assert "optimizer_measurements_unavailable" in steps[2].evidence
    assert steps[4].observed[0]["table_sha256_before"] == steps[4].observed[0]["table_sha256_after"]


@pytest.mark.parametrize("change,failed", [
    ("table", [5]), ("csv", [5]), ("delta", [6]),
    ("strings", [7]), ("reopened-table", [7]), ("drafts", [7]),
])
def test_independent_ui_mismatches_are_detected(change, failed):
    project, result = ui_contract()
    ui = result["ui_observation"]
    if change == "table":
        ui["before_rows"][0]["studio"] = "5"
        ui["csv_text"] = table_csv(ui["before_rows"])
    elif change == "csv":
        ui["csv_text"] = ui["csv_text"].replace("panels,4,4", "panels,5,4")
    elif change == "delta":
        result["saved_values"]["panels"] = "5"
        ui["before_rows"][0].update(studio="5", delta="1", verdict="FAIL")
        ui["csv_text"] = table_csv(ui["before_rows"])
    elif change == "strings":
        result["strings_sha256_after"] = "b" * 64
    elif change == "reopened-table":
        ui["reopened_rows"][0]["studio"] = "5"
    else:
        ui["reopened_drafts"]["panels"] = "5"
    steps = score([project], [result])
    assert all(steps[n - 1].verdict == "FAIL" for n in failed)
    if change == "delta":
        assert steps[4].observed[0]["table_truth"] == "PASS"


def test_partial_grouping_and_nullable_dc_keep_readable_siblings():
    project, result = ui_contract()
    project.update(expected_kw_dc=None, reconciliation_exclusions=["kw_dc"])
    result["oracle_values"]["kw_dc"] = None
    result["saved_values"]["kw_dc"] = None
    ui = result["ui_observation"]
    ui.update(status="UNREADABLE", grouping_status="UNREADABLE", recommendations=None)
    for rows in (ui["before_rows"], ui["reopened_rows"]):
        rows[-1].update(studio="", oracle="", delta="", verdict="UNREADABLE")
    for key in ("before_drafts", "reopened_drafts"):
        ui[key]["kw_dc"] = ""
    ui["csv_text"] = table_csv(ui["before_rows"])
    assert [s.verdict for s in score([project], [result])][3:] == ["UNREADABLE", "PASS", "PASS", "PASS"]
    project.update(expected_grids=None, grid_count_policy="match_or_named")
    ui.update(grouping_status="no_proposals_shown", recommendations=[])
    assert "report_grid_count_unreadable" in score([project], [result])[3].evidence
    del project["reconciliation_exclusions"]
    assert [s.verdict for s in score([project], [result])][4:6] == ["UNREADABLE"] * 2


@pytest.mark.parametrize("exclusions", [["optimizers"], ["kw_dc", "kw_dc"], None, "kw_dc", ["private-secret"]])
def test_unexpected_exclusions_are_unreadable(exclusions):
    project, result = ui_contract()
    project["reconciliation_exclusions"] = exclusions
    assert all(s.verdict == "UNREADABLE" for s in score([project], [result])[3:])


def test_dc_exclusion_requires_explicit_null_and_missing_dc_is_not_waived():
    project, result = ui_contract()
    project["reconciliation_exclusions"] = ["kw_dc"]
    assert all(s.verdict == "UNREADABLE" for s in score([project], [result])[3:])
    del project["expected_kw_dc"]
    assert all(s.verdict == "UNREADABLE" for s in score([project], [result])[3:])
    project["reconciliation_exclusions"] = []
    assert [s.verdict for s in score([project], [result])][4:6] == ["UNREADABLE"] * 2


def test_excluded_dc_row_still_required_and_compared_in_csv():
    project, result = ui_contract()
    project.update(expected_kw_dc=None, reconciliation_exclusions=["kw_dc"])
    ui = result["ui_observation"]
    ui["csv_text"] = ui["csv_text"].replace("kw_dc,2,2", "kw_dc,3,2")
    assert score([project], [result])[4].verdict == "FAIL"
    ui["before_rows"].pop()
    assert score([project], [result])[4].verdict == "UNREADABLE"


def test_valid_no_proposals_and_shown_delta_mismatch():
    project, result = ui_contract()
    ui = result["ui_observation"]
    ui.update(grouping_status="no_proposals_shown", recommendations=[])
    assert score([project], [result])[3].verdict == "PASS"
    ui["before_rows"][0]["delta"] = "1"
    ui["csv_text"] = table_csv(ui["before_rows"])
    assert [s.verdict for s in score([project], [result])][4:6] == ["FAIL"] * 2


def test_delta_subtraction_uses_isolated_exact_decimal_context():
    from decimal import localcontext

    project, result = ui_contract()
    project["expected_kw_dc"] = "123456789012345678.000000000000000001"
    result["oracle_values"]["kw_dc"] = project["expected_kw_dc"]
    result["saved_values"]["kw_dc"] = "123456789012345678.000000000000000002"
    ui = result["ui_observation"]
    ui["before_rows"][-1].update(studio=result["saved_values"]["kw_dc"],
        oracle=project["expected_kw_dc"], delta="0.000000000000000001", verdict="FAIL")
    ui["csv_text"] = table_csv(ui["before_rows"])
    with localcontext() as context:
        context.prec = 2
        steps = score([project], [result])
    assert steps[4].verdict == "PASS" and steps[5].verdict == "FAIL"


@pytest.mark.parametrize("value", ["-1", "01", "1.0", "١", "1e0", "9" * 39])
def test_observed_counts_require_bounded_ascii_canonical_numbers(value):
    project, result = ui_contract()
    result["ui_observation"]["before_rows"][0]["studio"] = value
    assert score([project], [result])[4].verdict == "UNREADABLE"


@pytest.mark.parametrize("missing", ["metric", "duplicate", "source", "oracle", "blank", "malformed", "reopen"])
def test_missing_or_malformed_ui_evidence_never_manufactures_fail(missing):
    project, result = ui_contract()
    ui = result["ui_observation"]
    if missing == "metric":
        ui["before_rows"].pop()
    elif missing == "duplicate":
        ui["before_rows"][1] = dict(ui["before_rows"][0])
    elif missing == "source":
        result["saved_values"]["optimizers"] = None
    elif missing == "oracle":
        result["oracle_values"]["optimizers"] = None
    elif missing == "blank":
        ui["before_rows"][1].update(studio="", delta="", verdict="UNREADABLE")
    elif missing == "malformed":
        ui["before_rows"][1]["studio"] = "02"
    else:
        ui["reopen_observed"] = False
    steps = score([project], [result])
    assert steps[6 if missing == "reopen" else 4].verdict == "UNREADABLE"


def test_ui_fixture_aggregation_retains_unknowns_duplicates_and_missing():
    project, result = ui_contract()
    other = dict(project, id="other")
    bad = deepcopy(result)
    bad["ui_observation"]["before_rows"][0]["studio"] = "5"
    for projects, results in (([project, other], [bad]), ([project], [bad, result]),
                              ([project, project], [bad])):
        assert all(s.verdict == "UNREADABLE" for s in score(projects, results)[3:])


def measured_graph():
    panels = [f"leaf:panel:00000000-0000-4000-8000-{i:012d}" for i in range(1, 5)]
    return dict(rev=4, panels=[{"id": ref} for ref in panels],
        frames=[{"id": "leaf:frame:00000000-0000-4000-8000-000000000001",
                 "panel_refs": panels, "module_power_watts": 500}],
        inverters=[{"id": "leaf:inverter:00000000-0000-4000-8000-000000000001",
                    "equipment_type": "string_inverter"}],
        strings=[dict(row, ordered_panel_refs=panels[i * 2:i * 2 + 2], module_count=2)
                 for i, row in enumerate(electrical_strings())])


@pytest.mark.parametrize("mode", ["normal", "fallback-label", "callback-error", "head-drift", "changed-strings"])
def test_flagged_normal_ui_lifecycle_is_independent_sanitized_and_cleans_up(monkeypatch, tmp_path, mode):
    from benches import x05_table_observer
    from xbench_core import write_json_atomic

    graph = measured_graph()
    ctx, events, requests, receipts = pinned_harness(monkeypatch, tmp_path, saved_graph=graph)
    ctx.out_dir = tmp_path
    project = ctx.manifest["projects"][0]
    project.update(collect_reconciliation_ui=True, expected_kw_dc=None,
                   reconciliation_exclusions=["kw_dc"])
    monkeypatch.setattr(x05, "write_json_atomic", write_json_atomic)
    client_class = sys.modules["xbench_studio"].StudioClient
    if mode == "fallback-label":
        monkeypatch.setattr(client_class, "tools", lambda self: [{"name": "solar-panel-groups"}])
    (tmp_path / "x05-ui-1").mkdir()
    original_context = client_class.drawing_context
    observed = []

    def context(self, *args):
        current = original_context(self, *args)
        if observed and mode == "head-drift":
            current["context"]["head_version_id"] = "private-secret-head"
        if observed and mode == "changed-strings":
            current["intake"]["solar_design_graph"]["strings"][0]["extra"] = "private-secret"
        return current

    monkeypatch.setattr(client_class, "drawing_context", context)

    def observe(page, **kwargs):
        assert page is not None
        assert kwargs["project_name"] == ctx.created_name
        assert kwargs["version_id"] == "v4"
        assert kwargs["grouping_label"] == ("solar-panel-groups" if mode == "fallback-label" else "Normal grouping")
        assert kwargs["oracle_values"] == dict(panels="4", optimizers="2", inverters="1", strings="2", kw_dc=None)
        assert kwargs["out_dir"].parent == tmp_path and kwargs["out_dir"].is_dir()
        assert kwargs["out_dir"].name == "x05-ui-2"
        observed.append(kwargs)
        if mode == "callback-error":
            raise RuntimeError("private-secret-callback")
        _, result = ui_contract()
        ui = result["ui_observation"]
        for key in ("before_rows", "reopened_rows"):
            ui[key][1].update(studio="", delta="", verdict="UNREADABLE")
            ui[key][-1].update(oracle="", delta="", verdict="UNREADABLE")
        for key in ("before_drafts", "reopened_drafts"):
            ui[key]["kw_dc"] = ""
        ui["csv_text"] = table_csv(ui["before_rows"])
        ui["private_debug"] = "private-secret-dom-csv"
        return ui

    monkeypatch.setattr(x05_table_observer, "observe_reconciliation", observe)
    steps = x05.run(ctx)
    assert len(observed) == 1
    assert events.count("release") == events.count("delete") == events.count("close") == 1
    text = (tmp_path / "x05-evidence.json").read_text()
    assert "private-secret" not in text and ctx.created_name not in text and "leaf:string:" not in text
    result = json.loads(text)["results"][0]
    assert result["saved_values"] == dict(panels="4", optimizers=None, inverters="1", strings="2", kw_dc="2")
    assert result["saved_metric_reasons"]["optimizers"] == "OPTIMIZER_MEASUREMENT_ABSENT"
    if mode in ("normal", "fallback-label"):
        assert [s.verdict for s in steps][3:] == ["PASS", "UNREADABLE", "UNREADABLE", "PASS"]
        assert steps[4].observed[0]["csv_view"] == "PASS"
    elif mode == "changed-strings":
        assert steps[6].verdict == "FAIL"
    else:
        assert all(s.verdict == "UNREADABLE" for s in steps[3:])
