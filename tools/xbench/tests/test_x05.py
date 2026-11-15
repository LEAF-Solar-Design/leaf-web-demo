"""Synthetic observations only; no runtime client or real fixtures required."""

from copy import deepcopy
from contextlib import contextmanager
from itertools import permutations, product
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


def live_harness(monkeypatch, *, failure=None, health=None, server_code=None):
    """Exact owner-confirmed client signatures, with no browser or disk IO."""
    import xbench_session

    project, result = observation()
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

    class Client:
        def __init__(self, transport, /):
            self.stage = 0
            assert transport("GET", "/api/probe") == (200, {"ok": True})
            assert transport("GET", "/api/bytes") == (200, b"synthetic")

        def create_project(self, name):
            assert name.startswith("xbench-x05-")
            events.append("create")
            return {"project_id": "new-project", "org_id": "new-org"}

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
                    "intake": {"solar_design_graph": {"rev": self.stage,
                        "panels": [{}] * (4 if self.stage >= 2 else 0),
                        "frames": [{"id": ref} for ref in ["frame-a", "frame-b"]]
                                  if self.stage >= 3 else []}}}

        def checkout(self, project_id, drawing_id, ttl_s=900):
            assert (project_id, drawing_id, ttl_s) == ("new-project", "adopted-drawing", 900)
            events.append("checkout")
            if failure == "checkout":
                raise RuntimeError("synthetic checkout failure")
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
                                               "InstallationDesign": "Roof"}
            else:
                assert tool == "solar-panels-from-drawing"
                return 202, {"job_id": "panels-job"}
            self.stage += 1
            return 200, {"output": {}}

        def wait_job(self, job_id, timeout_s=300):
            assert job_id == "panels-job" and timeout_s == 300
            events.append("wait-job")
            if failure == "mutation":
                return {"status": "failed"}
            self.stage += 1
            return {"status": "complete"}

        def release(self, project_id, drawing_id, capability):
            assert (project_id, drawing_id, capability) == ("new-project", "adopted-drawing", "private-capability")
            events.append("release")
            if failure == "cleanup":
                raise RuntimeError("synthetic release failure")

        def delete_project(self, project_id):
            assert project_id == "new-project"
            events.append("delete")
            if failure == "cleanup":
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
            if failure == "report-refusal":
                return 422, {"content-type": "application/json"}, json.dumps({
                    "error": {"code": server_code if server_code is not None else "REPORT_GRID_UNMATCHED"}}).encode()
            payload = {"report": {"download": "/api/report-artifact"}}
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


def test_live_client_contract_and_current_version_routes(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch)
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
    assert "private-capability" not in json.dumps(receipts)


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


def test_ground_setup_is_explicitly_unreadable_in_roof_slice(monkeypatch):
    ctx, events, requests, receipts = live_harness(monkeypatch)
    ctx.manifest["projects"][0]["installation_design"] = "Ground"
    assert not x05._setup(ctx.manifest["projects"][0])
    assert all(step.verdict == "UNREADABLE" for step in x05.run(ctx))
    assert events == [] and requests == []


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
