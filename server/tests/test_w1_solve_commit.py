"""Offline solve acceptance, correction and immutable-version undo checks."""
from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import leaf_cloud_client as cloud
import solar_solve_results as solve
from leaf_cloud_grants import CloudGrant
from solar_design_graph import GraphValidationError, validate_graph
from test_w1_design_graph import app_id, entity, graph  # noqa: F401


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commit = builtin("solar_commit_solve")
correct = builtin("solar_correct_string")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline network forbidden")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@pytest.fixture
def case(graph, monkeypatch):
    fixtures = SERVER / "tests/fixtures"
    record = json.loads((fixtures / "w1_stringer_recorded_response.json").read_text())
    request = json.loads((fixtures / record["request_fixture"]).read_text())
    response = json.loads((fixtures / record["response_fixture"]).read_text())
    frame = graph["frames"][0]
    panels = []
    for r, row in enumerate(request["grid"]["Rows"]):
        for c, wire in enumerate(row["Panels"]):
            panel = entity("panel", len(panels) + 1, frame_ref=frame["id"],
                           matrix_cell={"row": r + 1, "col": c + 1},
                           centre=[wire["X"], wire["Y"]], angle=wire["Angle"],
                           assignment={"string_ref": None, "seq": None})
            wire["Id"] = panel["id"]
            response["data"]["final_grid"]["Rows"][r]["Panels"][c]["Id"] = panel["id"]
            panels.append(panel)
    # The wire response still indexes the compact grid. Original cells have a
    # leading empty row and column, exercising both retained-index maps.
    blank = dict(request["grid"]["Rows"][0]["Panels"][0], Id="", Code=0, Seq=0)
    for row in request["grid"]["Rows"]:
        row["Panels"].insert(0, copy.deepcopy(blank))
    width = len(request["grid"]["Rows"][0]["Panels"])
    request["grid"]["Rows"].insert(0, {"Panels": [copy.deepcopy(blank) for _ in range(width)]})
    matrix = []
    for row in request["grid"]["Rows"]:
        matrix.append([{"code": "panel" if w["Code"] == 1 else "empty",
                        "panel_ref": w["Id"] or None, "seq": None,
                        "inverter_id": None, "string_input_number": None,
                        "x": w["X"], "y": w["Y"], "angle": w["Angle"]}
                       for w in row["Panels"]])
    frame.update(panel_refs=[p["id"] for p in panels], module_rows=len(matrix),
                 module_columns=width, module_slots=len(matrix) * width, matrix=matrix,
                 sequences=[], panel_assignments=[
                     {"panel_ref": p["id"], "string_ref": None, "seq": None,
                      "inverter_id": None, "string_input_number": None} for p in panels])
    graph.update(panels=panels, strings=[], inverters=[], routes=[], schedules=[])
    graph["electrical_zones"][0]["panel_refs"] = frame["panel_refs"][:]
    validate_graph(graph)
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant("fixture-tenant", ""))
    monkeypatch.setattr(cloud, "post_stringer", lambda *args: cloud.canonical_bytes(response))
    return graph, request, response


def candidate(case, *, phase="initial", job_id="fixture-job"):
    graph, request, _ = case
    binding = solve.bind_request(
        graph, request, expected_rev=graph["rev"], frame_ref=graph["frames"][0]["id"],
        tenant_id="fixture-tenant", job_id=job_id, phase=phase)
    result = cloud.proposal({"grant_ref": "fixture-grant", "request": request},
                            "fixture-tenant", job_id)
    return solve.complete_search(graph, binding, result)


def test_commits_original_cells_sequences_endpoints_lengths_and_provenance(case):
    graph, request, response = case
    before = copy.deepcopy(graph)
    proposal = candidate(case)
    result = commit.commit_solve(graph, {"expected_rev": 0}, candidate=proposal)
    assert graph == before and proposal["accepted"] is False
    assert result["rev"] == 1 and result["parent_rev"] == 0
    expected = [ref for _, ref in sorted(
        (cell["Seq"], cell["Id"])
        for row in response["data"]["final_grid"]["Rows"] for cell in row["Panels"]
        if cell["Code"] == 1)]
    assert [p for s in result["strings"] for p in s["ordered_panel_refs"]] == expected
    assert [s["module_count"] for s in result["strings"]] == [12, 12]
    assert [s["circuit_tag"] for s in result["strings"]] == ["S3", "S4"]
    by_id = {p["id"]: p for p in result["panels"]}
    for string in result["strings"]:
        refs = string["ordered_panel_refs"]
        assert (string["from_ref"], string["to_ref"]) == (refs[0], refs[-1])
        assert string["route"] == [by_id[ref]["centre"] for ref in refs]
        import math
        expected_length = sum(math.dist(a, b) for a, b in zip(string["route"], string["route"][1:]))
        assert string["length_ft"] == pytest.approx(expected_length / .3048)
        polarity = string["extra"]["polarity"]
        assert polarity["source"] == "derived" and polarity["source_rev"] == 0
        assert polarity["negative_panel_ref"] == refs[0]
        assert polarity["positive_panel_ref"] == refs[-1]
        for seq, ref in enumerate(refs):
            assert by_id[ref]["assignment"] == {"string_ref": string["id"], "seq": seq}
    assert result["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [], "unassigned_panel_refs": []}
    assert validate_graph(result) == result
    assert solve.require_current_export(result) == result
    assert result["opaque_stores"] == before["opaque_stores"]
    assert "grant_ref" not in json.dumps(result)


@pytest.mark.parametrize("field", ["revision", "source", "settings", "catalog", "panel", "frame", "zone"])
def test_stale_results_refused_even_for_same_revision_content_edits(case, field):
    graph, _, _ = case
    proposal = candidate(case)
    if field == "revision":
        graph["rev"], graph["parent_rev"] = 1, 0
    elif field == "source":
        graph["source_hash"] = "b" * 64
    elif field == "settings":
        graph["settings"]["panels_in_sequence"] += 1
    elif field == "catalog":
        graph["catalog_versions"]["modules"] = "fixture-new"
    elif field == "panel":
        graph["panels"][0]["centre"][0] += 1
    elif field == "frame":
        graph["frames"][0]["name"] = "Edited group"
    else:
        graph["electrical_zones"][0]["panel_refs"].reverse()
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError, match="STALE_SOLVE_RESULT"):
        solve.accept_candidate(graph, proposal, expected_rev=graph["rev"])
    assert graph == before


@pytest.mark.parametrize("defect", ["duplicate", "missing", "mapped_path", "request_hash", "tenant", "job"])
def test_bad_proposal_never_assigns_panels(case, defect):
    graph, _, _ = case
    proposal = candidate(case)
    result = proposal["proposal"]
    path = result["proposal"]["data"]["best_result"]["info"]["visited_path"]
    if defect == "duplicate":
        path[1] = path[0][:]
    elif defect == "missing":
        path.pop()
    elif defect == "mapped_path":
        result["visited_path"][0] = [0, 0]
    elif defect == "request_hash":
        result["request_sha256"] = "0" * 64
    elif defect == "tenant":
        result["tenant_id"] = "other"
    else:
        result["job_id"] = "other"
    code = {"duplicate": "DUPLICATE_SOLVE_PANELS", "missing": "UNASSIGNED_SOLVE_PANELS"}.get(
        defect, "INVALID_SOLVE_PROPOSAL")
    with pytest.raises(GraphValidationError, match=code):
        solve.accept_candidate(graph, proposal, expected_rev=0)
    assert not graph["strings"]


@pytest.mark.parametrize("axis,negative", [(0, False), (1, False), (0, True), (1, True)])
def test_accept_candidate_rejects_out_of_bounds_mapped_path(case, monkeypatch, axis, negative):
    graph, _, _ = case
    proposal = candidate(case)
    before = copy.deepcopy(graph)
    frame = graph["frames"][0]
    proposal["proposal"]["visited_path"][0][axis] = (
        -1 if negative else frame["module_rows" if axis == 0 else "module_columns"])
    # Exercise acceptance's own guard after the completion validation boundary.
    monkeypatch.setattr(solve, "complete_search", lambda *args: proposal)
    # GraphValidationError carries ": <path>"; anchor the code, admit the path.
    with pytest.raises(GraphValidationError, match="^INVALID_PATH_INDICES: "):
        solve.accept_candidate(graph, proposal, expected_rev=0)
    assert graph == before


@pytest.mark.parametrize("lengths", [[], [12], [24, 1], [0, 24], [-1, 25],
                                    [True, 23], [12.0, 12], ["12", 12]])
def test_accept_candidate_rejects_invalid_sequence_lengths(case, monkeypatch, lengths):
    graph, _, _ = case
    proposal = candidate(case)
    before = copy.deepcopy(graph)
    proposal["proposal"]["proposal"]["data"]["best_result"]["info"]["sequence_length"] = lengths
    # Exercise acceptance's own guard after the completion validation boundary.
    monkeypatch.setattr(solve, "complete_search", lambda *args: proposal)
    # GraphValidationError carries ": <path>"; anchor the code, admit the path.
    with pytest.raises(GraphValidationError, match="^TRUNCATED_SEQUENCE_LENGTH: "):
        solve.accept_candidate(graph, proposal, expected_rev=0)
    assert graph == before


def test_unassigned_panels_explicit_and_cannot_export_current(case):
    graph, _, _ = case
    graph["panels"].append(entity("panel", 999, frame_ref=None, matrix_cell=None,
                                   centre=[100, 100], angle=0,
                                   assignment={"string_ref": None, "seq": None}))
    result = solve.accept_candidate(graph, candidate(case), expected_rev=0)
    assert result["extra"]["solve_coverage"]["unassigned_panel_refs"] == [app_id("panel", 999)]
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        solve.require_current_export(result)


def test_background_completion_is_not_acceptance_and_stale_acceptance_refuses(case):
    graph, request, response = case
    first = candidate(case)
    assert first["initial_complete"] and not first["background_complete"]
    initial = solve.accept_candidate(graph, first, expected_rev=0)
    snapshot = copy.deepcopy(initial)
    improvement = candidate((initial, request, response), phase="background", job_id="background-job")
    assert improvement["background_complete"] and not improvement["initial_complete"]
    assert initial == snapshot
    accepted = solve.accept_candidate(initial, improvement, expected_rev=1)
    assert accepted["rev"] == 2
    assert accepted["frames"][0]["extra"]["solve"]["accepted_phase"] == "background"
    assert [s["id"] for s in accepted["strings"]] == [s["id"] for s in initial["strings"]]
    corrected = correct.run(initial, {"expected_rev": 1, "settings_changes": {"optimizer_ratio": 2}})
    with pytest.raises(GraphValidationError, match="STALE_SOLVE_RESULT"):
        solve.accept_candidate(corrected, improvement, expected_rev=2)


def test_background_requires_initial_and_cancel_changes_nothing(case):
    graph, request, _ = case
    with pytest.raises(GraphValidationError, match="INITIAL_SOLVE_REQUIRED"):
        candidate(case, phase="background")
    assert commit.commit_solve(graph, {"expected_rev": 0, "cancel": True}, candidate=None) == graph
    assert correct.run(graph, {"expected_rev": 0, "cancel": True}) == graph
    with pytest.raises(RuntimeError, match="broker"):
        commit.run(graph, {})
    request["grid"]["Rows"][1]["Panels"][1]["Id"] = "not-the-graph-panel"
    with pytest.raises(GraphValidationError, match="SOLVE_GRID_MISMATCH"):
        candidate(case)


def transfer(graph):
    first, second = graph["strings"]
    return {"expected_rev": graph["rev"], "memberships": [
        {"string_ref": first["id"], "ordered_panel_refs": first["ordered_panel_refs"][:1]},
        {"string_ref": second["id"], "ordered_panel_refs": second["ordered_panel_refs"] + first["ordered_panel_refs"][1:]},
    ]}


@pytest.mark.parametrize("mode", ["membership", "settings"])
def test_correction_invalidates_strings_equipment_routes_and_schedules(graph, mode):
    before = copy.deepcopy(graph)
    params = transfer(graph) if mode == "membership" else {
        "expected_rev": 0, "settings_changes": {"panels_in_sequence": 3}}
    result = correct.run(graph, params)
    assert graph == before and result["rev"] == 1
    for collection in ("strings", "inverters", "routes", "schedules"):
        assert all(e["validity"]["state"] == "stale" for e in result[collection])
    assert validate_graph(result) == result
    if mode == "membership":
        assert [s["module_count"] for s in result["strings"]] == [1, 2]
        assert result["panels"][1]["assignment"]["string_ref"] == result["strings"][1]["id"]
    else:
        assert not result["settings"]["global_string_sizing_confirmed"]
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        solve.require_current_export(result)


def test_duplicate_membership_is_explicit_and_atomic(graph):
    before = copy.deepcopy(graph)
    params = transfer(graph)
    params["memberships"] = params["memberships"][1:]
    with pytest.raises(GraphValidationError, match="DUPLICATE_PANEL_MEMBERSHIP"):
        correct.run(graph, params)
    assert graph == before


@pytest.mark.parametrize("params", [None, [], {"expected_rev": True},
    {"expected_rev": 0, "memberships": [{}]},
    {"expected_rev": 0, "settings_changes": {"global_string_sizing_confirmed": True}},
    {"expected_rev": 0, "settings_changes": {"optimizer_ratio": float("nan")}},
])
def test_malformed_corrections_fail_closed(graph, params):
    with pytest.raises(GraphValidationError):
        correct.run(graph, params)


def seed(tmp_path, monkeypatch, graph):
    import write_loop
    import store
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [],
              "faces3d": [], "blockdefs": [], "geodata": None,
              "solar_design_graph": graph, "solar_design_graph_sha256": solve.digest(graph)}
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(intake))
    store.ingest_drawing(backend, "fixture-tenant", str(path), drawing_id="solar")
    return backend, intake


def seed_graphless(tmp_path, monkeypatch):
    import store
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [],
              "faces3d": [], "blockdefs": [], "geodata": None,
              "custom": {"keep": [1, 2, 3]}}
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(intake))
    store.ingest_drawing(backend, "fixture-tenant", str(path), drawing_id="solar")
    return backend, intake


def first_graph(backend, graph):
    import hashlib
    import store
    _, key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    first = copy.deepcopy(graph)
    first.update(rev=1, parent_rev=0, source_hash=hashlib.sha256(backend.get(key)).hexdigest())
    return first


_publish_jobs = itertools.count()
_default = object()


def publish(backend, parent, before, after, *, holder="fixture-owner", fence=_default,
            job_id=_default, request_sha256="a" * 64, acquire=True):
    import write_loop
    import store
    acquired_fence = None
    if acquire:
        acquired_fence = store.acquire_checkout_fence(
            backend, "fixture-tenant", "solar", "fixture-owner", 300)
    try:
        return solve.publish_version(
            backend, "fixture-tenant", "solar", parent_version=parent,
            before=before, after=after, holder=holder,
            fence=acquired_fence if fence is _default else fence,
            job_id=f"fixture-job-{next(_publish_jobs)}" if job_id is _default else job_id,
            request_sha256=request_sha256)
    finally:
        if acquire:
            store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


@pytest.mark.parametrize("overrides", [
    {"holder": None}, {"holder": ""}, {"holder": "anonymous:unnamed-writer"},
    {"fence": None}, {"fence": 0}, {"fence": -1}, {"fence": True}, {"fence": "1"},
    {"fence": 1.0},
])
def test_publish_requires_named_identity_and_positive_integer_fence(graph, tmp_path, monkeypatch, overrides):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with pytest.raises(GraphValidationError, match="CHECKOUT_REQUIRED"):
        publish(backend, 1, graph, correct.run(graph, transfer(graph)), **overrides)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


def test_publish_requires_checkout(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    def refuse_commit(*args, **kwargs):
        raise AssertionError("commit attempted")
    monkeypatch.setattr(write_loop, "_put_bytes_version", refuse_commit)
    with pytest.raises(GraphValidationError, match="CHECKOUT_REQUIRED"):
        publish(backend, 1, graph, correct.run(graph, transfer(graph)), acquire=False, fence=1)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


@pytest.mark.parametrize("wrong", ["holder", "fence"])
def test_publish_requires_own_checkout(graph, tmp_path, monkeypatch, wrong):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    def refuse_commit(*args, **kwargs):
        raise AssertionError("commit attempted")
    monkeypatch.setattr(write_loop, "_put_bytes_version", refuse_commit)
    try:
        with pytest.raises(GraphValidationError, match="CHECKOUT_DENIED"):
            publish(backend, 1, graph, correct.run(graph, transfer(graph)), acquire=False,
                    holder="intruder" if wrong == "holder" else "fixture-owner",
                    fence=fence + 1 if wrong == "fence" else fence)
        assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def test_publish_requires_unexpired_checkout(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    import time
    backend, _ = seed(tmp_path, monkeypatch, graph)
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", .05)
    time.sleep(.2)
    def refuse_commit(*args, **kwargs):
        raise AssertionError("commit attempted")
    monkeypatch.setattr(write_loop, "_put_bytes_version", refuse_commit)
    with pytest.raises(GraphValidationError, match="CHECKOUT_REQUIRED"):
        publish(backend, 1, graph, correct.run(graph, transfer(graph)), acquire=False, fence=fence)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


def test_publish_persists_job_binding_and_exact_receipt(graph, tmp_path, monkeypatch):
    import hashlib
    import write_loop
    import store
    backend, intake = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    receipt = publish(backend, 1, graph, after, job_id="job-a")
    intake_sha = hashlib.sha256(cloud.canonical_bytes(solve.version_companion(intake, graph, after))).hexdigest()
    assert receipt == {"version": 2, "parent_version": 1, "graph_sha256": solve.digest(after),
                       "intake_sha256": intake_sha, "job_id": "job-a",
                       "request_sha256": "a" * 64, "replayed": False}
    entry = next(e for e in store.load_manifest(backend, "fixture-tenant", "solar")["versions"]
                 if e["v"] == 2)
    assert entry["workitem_id"] == "solar-graph:job-a"
    assert entry["tool"] == "solar-graph"
    assert entry["note"] == "solar-graph-commit:" + "a" * 64


@pytest.mark.parametrize("acquire", [True, False])
def test_publish_replays_before_head_check_with_or_without_lease(graph, tmp_path, monkeypatch, acquire):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    receipt = publish(backend, 1, graph, after, job_id="job-a")
    assert not store.checkout_active(store.load_manifest(backend, "fixture-tenant", "solar")["checkout"])
    replay = publish(backend, 1, graph, after, job_id="job-a", acquire=acquire, fence=1)
    assert replay == dict(receipt, replayed=True)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 2


def test_replay_refuses_a_different_parent_with_identical_bytes(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    _, key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    same = backend.get(key)
    version = write_loop._put_bytes_version(
        backend, "fixture-tenant", "solar", same, parent_version=1,
        meta={"tool": "fixture", "note": "fixture-noop"}, require_parent_is_head=True)
    assert version == 2
    after = correct.run(graph, transfer(graph))
    receipt = publish(backend, 2, graph, after, job_id="job-a")
    assert receipt["version"] == 3 and receipt["parent_version"] == 2
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
        publish(backend, 1, graph, after, job_id="job-a", acquire=False, fence=1)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 3
    replay = publish(backend, 2, graph, after, job_id="job-a", acquire=False, fence=1)
    assert replay == dict(receipt, replayed=True)


def test_replay_with_a_missing_parent_is_a_reused_binding(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED") as exc:
        publish(backend, 77, graph, after, job_id="job-a", acquire=False, fence=1)
    assert type(exc.value) is GraphValidationError
    assert "fixture-tenant" not in str(exc.value)


def test_a_second_request_under_one_job_is_refused_after_the_head_read(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    g0 = graph
    g1 = correct.run(g0, transfer(g0))
    g2 = correct.run(g1, {"expected_rev": g1["rev"], "settings_changes": {"panels_in_sequence": 3}})
    backend, _ = seed(tmp_path, monkeypatch, g0)
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    real_read_intake = write_loop.read_intake
    state = {"injected": False}

    def paused_read_intake(*args, **kwargs):
        if not state["injected"]:
            state["injected"] = True
            monkeypatch.setattr(write_loop, "read_intake", real_read_intake)
            publish(backend, 1, g0, g1, job_id="job-j", request_sha256="a" * 64,
                    acquire=False, fence=fence)
            monkeypatch.setattr(write_loop, "read_intake", paused_read_intake)
        return real_read_intake(*args, **kwargs)

    monkeypatch.setattr(write_loop, "read_intake", paused_read_intake)
    try:
        with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
            publish(backend, 2, g1, g2, job_id="job-j", request_sha256="b" * 64,
                    acquire=False, fence=fence)
        manifest = store.load_manifest(backend, "fixture-tenant", "solar")
        entries = [e for e in manifest["versions"] if e.get("workitem_id") == "solar-graph:job-j"]
        assert len(entries) == 1 and entries[0]["v"] == 2
        assert manifest["latest"] == 2
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def test_the_same_request_racing_itself_is_a_replay(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    g0 = graph
    g1 = correct.run(g0, transfer(g0))
    backend, _ = seed(tmp_path, monkeypatch, g0)
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    real_read_intake = write_loop.read_intake
    state = {"injected": False}

    def paused_read_intake(*args, **kwargs):
        if not state["injected"]:
            state["injected"] = True
            monkeypatch.setattr(write_loop, "read_intake", real_read_intake)
            publish(backend, 1, g0, g1, job_id="job-j", request_sha256="a" * 64,
                    acquire=False, fence=fence)
            monkeypatch.setattr(write_loop, "read_intake", paused_read_intake)
        return real_read_intake(*args, **kwargs)

    monkeypatch.setattr(write_loop, "read_intake", paused_read_intake)
    try:
        receipt = publish(backend, 1, g0, g1, job_id="job-j", request_sha256="a" * 64,
                          acquire=False, fence=fence)
        assert receipt["version"] == 2 and receipt["parent_version"] == 1
        assert receipt["replayed"] is True
        manifest = store.load_manifest(backend, "fixture-tenant", "solar")
        entries = [e for e in manifest["versions"] if e.get("workitem_id") == "solar-graph:job-j"]
        assert len(entries) == 1 and entries[0]["v"] == 2
        assert manifest["latest"] == 2
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def test_replay_with_a_matching_parent_whose_payload_is_missing_is_a_reused_binding(
        graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    _, parent_key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    real_get = backend.get

    def missing_parent(key):
        if key == parent_key:
            raise KeyError(key)
        return real_get(key)

    monkeypatch.setattr(backend, "get", missing_parent)
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED") as exc:
        publish(backend, 1, graph, after, job_id="job-a", acquire=False, fence=1)
    assert type(exc.value) is GraphValidationError
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 2


def test_a_replay_read_failure_is_a_path_free_oserror(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    _, parent_key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    real_get = backend.get

    def denied_parent(key):
        if key == parent_key:
            raise PermissionError(13, "Permission denied", str(tmp_path / "secret-location" / "00000001.dwg"))
        return real_get(key)

    monkeypatch.setattr(backend, "get", denied_parent)
    with pytest.raises(OSError) as exc:
        publish(backend, 1, graph, after, job_id="job-a", acquire=False, fence=1)
    assert not isinstance(exc.value, GraphValidationError)
    assert exc.value.errno == 13
    assert isinstance(exc.value, PermissionError)
    assert exc.value.filename is None
    assert "secret-location" not in str(exc.value)
    assert "secret-location" not in repr(exc.value)
    assert "solar graph store access failed" in str(exc.value)
    assert exc.value.__suppress_context__ is True


def test_a_fresh_publish_read_failure_is_a_path_free_oserror(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    _, parent_key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    real_get = backend.get

    def denied_parent(key):
        if key == parent_key:
            raise PermissionError(13, "Permission denied", str(tmp_path / "secret-location" / "00000001.dwg"))
        return real_get(key)

    monkeypatch.setattr(backend, "get", denied_parent)
    with pytest.raises(OSError) as exc:
        publish(backend, 1, graph, after, job_id="job-p")
    assert not isinstance(exc.value, GraphValidationError)
    assert exc.value.errno == 13
    assert isinstance(exc.value, PermissionError)
    assert exc.value.filename is None
    assert "secret-location" not in str(exc.value)
    assert "secret-location" not in repr(exc.value)
    assert "solar graph store access failed" in str(exc.value)
    assert exc.value.__suppress_context__ is True
    monkeypatch.setattr(backend, "get", real_get)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


def test_an_oserror_without_errno_is_a_path_free_oserror(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    _, parent_key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    real_get = backend.get

    def failed_parent(key):
        if key == parent_key:
            raise OSError("cannot read " + str(tmp_path / "secret-location"))
        return real_get(key)

    monkeypatch.setattr(backend, "get", failed_parent)
    with pytest.raises(OSError) as exc:
        publish(backend, 1, graph, after, job_id="job-a", acquire=False, fence=1)
    assert exc.value.errno is None
    assert str(exc.value) == "solar graph store access failed"
    assert "secret-location" not in repr(exc.value)


def test_replay_with_an_undecodable_parent_is_a_reused_binding(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    _, key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    backend.put(key, b"not json")
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
        publish(backend, 1, graph, after, job_id="job-a", acquire=False, fence=1)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 2


@pytest.mark.parametrize("different", ["request", "content"])
def test_publish_refuses_reused_job_binding(graph, tmp_path, monkeypatch, different):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    after = correct.run(graph, transfer(graph))
    publish(backend, 1, graph, after, job_id="job-a")
    request_sha = "a" * 64
    if different == "request":
        request_sha = "b" * 64
    else:
        after = copy.deepcopy(after)
        after["extra"]["different_content"] = True
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
        publish(backend, 1, graph, after, job_id="job-a", request_sha256=request_sha)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 2


@pytest.mark.parametrize("overrides", [
    {"job_id": ""}, {"job_id": "x" * 129}, {"job_id": "job a"}, {"job_id": "job/a"},
    {"request_sha256": "A" * 64}, {"request_sha256": "a" * 63}, {"request_sha256": "g" * 64},
])
def test_publish_refuses_invalid_job_binding(graph, tmp_path, monkeypatch, overrides):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with pytest.raises(GraphValidationError, match="INVALID_JOB_BINDING"):
        publish(backend, 1, graph, correct.run(graph, transfer(graph)), **overrides)
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


def test_publish_respects_mutation_guard_with_valid_checkout(graph, tmp_path, monkeypatch):
    import write_loop
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    with pytest.raises(GraphValidationError, match="DRAWING_MUTATION_REFUSED"):
        publish(backend, 1, graph, correct.run(graph, transfer(graph)))
    assert store.load_manifest(backend, "fixture-tenant", "solar")["latest"] == 1


def test_correction_version_and_existing_restore_recover_graph_and_validity(graph, tmp_path, monkeypatch):
    backend, intake = seed(tmp_path, monkeypatch, graph)
    import write_loop
    from routers.drawings import restore_drawing_version
    after = correct.run(graph, transfer(graph))
    receipt = publish(backend, 1, graph, after)
    assert receipt["version"] == 2 and receipt["graph_sha256"] == solve.digest(after)
    version, current = write_loop.read_intake(backend, "fixture-tenant", "solar")
    assert version == 2 and current == solve.version_companion(intake, graph, after)
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        publish(backend, 1, graph, after)
    restored = restore_drawing_version("fixture-tenant", "solar", 1, actor="undo", backend=backend)
    assert restored.version == 3
    _, restored_intake = write_loop.read_intake(backend, "fixture-tenant", "solar")
    assert restored_intake == intake
    assert solve.require_current_export(restored_intake["solar_design_graph"]) == graph


def test_accepting_background_is_one_reversible_version(case, tmp_path, monkeypatch):
    graph, request, response = case
    initial = solve.accept_candidate(graph, candidate(case), expected_rev=0)
    backend, intake = seed(tmp_path, monkeypatch, initial)
    improvement = candidate((initial, request, response), phase="background")
    after = solve.accept_candidate(initial, improvement, expected_rev=1)
    assert publish(backend, 1, initial, after)["version"] == 2
    import write_loop
    from routers.drawings import restore_drawing_version
    restore_drawing_version("fixture-tenant", "solar", 1, actor="undo", backend=backend)
    assert write_loop.read_intake(backend, "fixture-tenant", "solar")[1] == intake


def test_companion_rejects_moved_basis_and_keeps_unknown_intake(graph):
    after = correct.run(graph, transfer(graph))
    intake = {"solar_design_graph": graph, "solar_design_graph_sha256": solve.digest(graph),
              "future_cad_data": {"keep": [1, 2, 3]}}
    result = solve.version_companion(intake, graph, after)
    assert result["future_cad_data"] == intake["future_cad_data"]
    intake["solar_design_graph_sha256"] = "0" * 64
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_COMPANION"):
        solve.version_companion(intake, graph, after)


def test_seed_publish_receipt_payload_and_replay(graph, tmp_path, monkeypatch):
    import hashlib
    import store
    backend, intake = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    receipt = publish(backend, 1, None, first, job_id="seed-job")
    _, key = store.resolve_version(backend, "fixture-tenant", "solar", 2)
    data = backend.get(key)
    assert receipt == {
        "version": 2, "parent_version": 1, "replayed": False, "seeded": True,
        "graph_sha256": solve.digest(first), "intake_sha256": hashlib.sha256(data).hexdigest(),
        "job_id": "seed-job", "request_sha256": "a" * 64,
    }
    assert json.loads(data) == dict(intake, solar_design_graph=first,
                                    solar_design_graph_sha256=solve.digest(first))
    manifest = store.load_manifest(backend, "fixture-tenant", "solar")
    entry = next(e for e in manifest["versions"] if e["v"] == 2)
    assert entry["parent"] == 1
    assert entry["workitem_id"] == "solar-graph:seed-job"
    assert entry["note"] == "solar-graph-seed:" + "a" * 64
    assert entry["tool"] == "solar-graph"
    replay = publish(backend, 1, None, first, job_id="seed-job", acquire=False, fence=1)
    assert replay == dict(receipt, replayed=True)
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 2


@pytest.mark.parametrize("different", ["request", "mode"])
def test_seed_job_binding_cannot_be_reused(graph, tmp_path, monkeypatch, different):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    publish(backend, 1, None, first, job_id="seed-job")
    before, after, request = None, first, "b" * 64
    if different == "mode":
        before, request = first, "a" * 64
        after = correct.run(first, {"expected_rev": 1,
                                    "settings_changes": {"panels_in_sequence": 3}})
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
        publish(backend, 1, before, after, job_id="seed-job", request_sha256=request)
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 2


def test_ordinary_job_cannot_replay_as_seed(graph, tmp_path, monkeypatch):
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    publish(backend, 1, graph, correct.run(graph, transfer(graph)), job_id="edit-job")
    with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
        publish(backend, 1, None, first_graph(backend, graph), job_id="edit-job")
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 2


def test_seed_refuses_moved_head(graph, tmp_path, monkeypatch):
    import store
    import write_loop
    backend, intake = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    data = cloud.canonical_bytes(dict(intake, competitor=True))
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    try:
        assert write_loop._put_bytes_version(
            backend, "fixture-tenant", "solar", data, parent_version=1,
            meta={"tool": "fixture"}, holder="fixture-owner", fence=fence,
            require_parent_is_head=True) == 2
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        publish(backend, 1, None, first)
    version, key = store.resolve_version(backend, "fixture-tenant", "solar", "head")
    assert version == 2 and backend.get(key) == data


def test_seed_refuses_existing_graph(graph, tmp_path, monkeypatch):
    import store
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with pytest.raises(GraphValidationError, match="GRAPH_ALREADY_EMBEDDED"):
        publish(backend, 1, None, first_graph(backend, graph))
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1


@pytest.mark.parametrize("companion", ["solar_design_graph", "solar_design_graph_sha256"])
def test_seed_refuses_partial_parent(graph, tmp_path, monkeypatch, companion):
    import store
    backend, intake = seed_graphless(tmp_path, monkeypatch)
    intake[companion] = graph if companion == "solar_design_graph" else solve.digest(graph)
    # Install the partial parent as the initial fixture, before publication.
    _, key = store.resolve_version(backend, "fixture-tenant", "solar", 1)
    backend.put(key, cloud.canonical_bytes(intake))
    with pytest.raises(GraphValidationError, match="INVALID_SEED_PARENT"):
        publish(backend, 1, None, first_graph(backend, graph))
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1


@pytest.mark.parametrize("rev,parent_rev", [(0, None), (2, 1)])
def test_seed_requires_first_revision(graph, tmp_path, monkeypatch, rev, parent_rev):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    first.update(rev=rev, parent_rev=parent_rev)
    with pytest.raises(GraphValidationError, match="INVALID_SEED_GRAPH"):
        publish(backend, 1, None, first)
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1


def test_seed_requires_source_bytes_hash(graph, tmp_path, monkeypatch):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    first["source_hash"] = "0" * 64
    with pytest.raises(GraphValidationError, match="SOURCE_HASH_MISMATCH"):
        publish(backend, 1, None, first)
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1


@pytest.mark.parametrize("wrong", ["missing", "holder", "fence"])
def test_seed_requires_own_checkout(graph, tmp_path, monkeypatch, wrong):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    fence = 1
    if wrong != "missing":
        fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    try:
        code = "CHECKOUT_REQUIRED" if wrong == "missing" else "CHECKOUT_DENIED"
        with pytest.raises(GraphValidationError, match=code):
            publish(backend, 1, None, first, acquire=False,
                    holder="intruder" if wrong == "holder" else "fixture-owner",
                    fence=fence + 1 if wrong == "fence" else fence)
        assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1
    finally:
        if wrong != "missing":
            store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def test_seed_respects_mutation_guard(graph, tmp_path, monkeypatch):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    with pytest.raises(GraphValidationError, match="DRAWING_MUTATION_REFUSED"):
        publish(backend, 1, None, first)
    assert len(store.load_manifest(backend, "fixture-tenant", "solar")["versions"]) == 1


def test_seeded_graph_accepts_ordinary_successor(graph, tmp_path, monkeypatch):
    import store
    backend, _ = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    publish(backend, 1, None, first, job_id="seed-job")
    second = correct.run(first, {"expected_rev": 1,
                                 "settings_changes": {"panels_in_sequence": 3}})
    receipt = publish(backend, 2, first, second)
    assert receipt["version"] == 3 and receipt["parent_version"] == 2
    assert set(receipt) == {"version", "parent_version", "graph_sha256", "intake_sha256",
                            "job_id", "request_sha256", "replayed"}
    entry = next(e for e in store.load_manifest(backend, "fixture-tenant", "solar")["versions"]
                 if e["v"] == 3)
    assert entry["note"] == "solar-graph-commit:" + "a" * 64


def test_seed_companion_preserves_parent_and_rejects_non_object(graph, tmp_path, monkeypatch):
    backend, intake = seed_graphless(tmp_path, monkeypatch)
    first = first_graph(backend, graph)
    original = copy.deepcopy(intake)
    result = solve.version_companion(intake, None, first)
    assert result == dict(intake, solar_design_graph=first,
                          solar_design_graph_sha256=solve.digest(first))
    result["custom"]["keep"].append(4)
    assert intake == original
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_COMPANION"):
        solve.version_companion([], None, first)


def test_export_rechecks_upstream_edits_from_existing_settings_builtin(case):
    graph, _, _ = case
    solved = solve.accept_candidate(graph, candidate(case), expected_rev=0)
    settings = builtin("solar_settings")
    edited = settings.run(solved, {"expected_rev": 1, "changes": {"optimizer_ratio": 2}})
    with pytest.raises(GraphValidationError, match="SOLAR_OUTPUT_NOT_CURRENT"):
        solve.require_current_export(edited)
