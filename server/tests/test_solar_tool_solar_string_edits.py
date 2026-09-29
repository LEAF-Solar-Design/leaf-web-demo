"""Frozen string-edit projections, graph digests, atomic refusals and local rail."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import checkout_capability
import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_rooftop_chain as chain
import solar_tools
import store
from solar_design_graph import GraphValidationError, validate_graph
from solar_solve_results import sync_assignments
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_solar_tool_solar_string_conductors import add_route_dependencies
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest, expected_declaration

FLIP = "solar-string-flip"
SWAP = "solar-string-swap"
TENANT = "fixture-tenant"
S1, S2 = [app_id("string", n) for n in (1, 2)]
P1, P2, P3 = [app_id("panel", n) for n in (1, 2, 3)]
INV = app_id("inverter", 1)
BASE_HASHES = {
    "G": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
    "C2": "12a1fe7029e3fc6a4b21f7f843994e938b2d920e7469cdafa33ecda168d210d1",
    "SW": "c8b558b824940aca32e9ad119e46979715ea8a32441b5d66213e192628edd2ad",
    "U": "df8bd6c0371258c2751d463c799493af66b7e58ded10fd79de789db151b4d54e",
    "N": "c61fae2abdd5f65608da69e50c3938034ae1d5890cfd01885028c9108671ba35",
    "DEP": "43dff22f69583bca5b45105af026ec3dfe644d596613ad3b81f5ae1a5ef92735",
}


def sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=False).encode("utf-8")).hexdigest()


def variant(graph, name="G"):
    g = copy.deepcopy(graph)
    validate_graph(g)
    assert sha(g) == BASE_HASHES["G"]
    if name == "C2":
        s1 = g["strings"][0]
        s1.update(ordered_panel_refs=[P1, P2, P3], module_count=3,
                  to_ref=P3, route=[[0, 0], [1, 0], [2, 0]])
        s1["extra"]["polarity"] = {
            "source": "derived", "rule": "ordered-selection-first-negative-last-positive",
            "negative_panel_ref": P1, "positive_panel_ref": P3, "source_rev": 0}
        g["strings"] = [s1]
        g["inverters"][0]["input_assignments"] = g["inverters"][0]["input_assignments"][:1]
        g["schedules"][0]["source_refs"] = [s1["id"]]
        sync_assignments(g)
    elif name == "SW":
        g["strings"][0]["circuit_tag"] = "1/1a"
        g["strings"][1]["circuit_tag"] = "2/1b"
    elif name == "U":
        g["inverters"][0]["input_assignments"] = g["inverters"][0]["input_assignments"][:1]
        g["strings"][1]["inverter_ref"] = None
        sync_assignments(g)
    elif name == "N":
        g["inverters"][0]["input_assignments"] = []
        for string in g["strings"]:
            string["inverter_ref"] = None
        sync_assignments(g)
    elif name == "DEP":
        add_route_dependencies(g)
    elif name == "ST":
        g["strings"][0]["validity"] = {"state": "stale", "reasons": ["settings_changed"]}
    validate_graph(g)
    if name in BASE_HASHES:
        assert sha(g) == BASE_HASHES[name]
    return g


def request_for(g, tool=FLIP):
    params = {"operation": "flip-string" if tool == FLIP else "swap-strings",
              "expected_rev": g["rev"]}
    params.update({"string_ref": S1} if tool == FLIP else {"string_refs": [S1, S2]})
    return params


def run(g, tool=FLIP, params=None):
    return solar_local_graph._load_builtin(tool).run(
        g, request_for(g, tool) if params is None else params)


def stale(reason):
    return {"state": "stale", "reasons": [reason]}


def inputs(g):
    return [(a["string_ref"], a["mppt_letter"], a["input_number"])
            for a in g["inverters"][0]["input_assignments"]]


def test_string_edit_flip_kernel_projection():
    original = {"handle": "1", "panels": ["1", "2"], "label": {}}
    result = chain.string_flip(original)
    assert result == {"handle": "1", "panels": ["2", "1"], "label": {}}
    assert sha(result) == "6086ce43e74b81ab8d21bb4c875c43a316c7cfbd17ef0029e78983ec666fae24"
    assert chain.string_flip(result) == chain.validate_string(original)
    original["panels"] = ["1", "2", "3"]
    result = chain.string_flip(original)
    assert result == {"handle": "1", "panels": ["3", "2", "1"], "label": {}}
    assert sha(result) == "1c380beadc6e0d7f001b2e8921b0e432979353c8c4630724112f3c5b2c416cf9"
    assert chain.string_flip(result) == chain.validate_string(original)


def test_string_edit_flip_fixture_string(graph):
    g = variant(graph)
    before = copy.deepcopy(g)
    after = run(g)
    assert g == before
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    s = after["strings"][0]
    assert s["ordered_panel_refs"] == [P2, P1]
    assert (s["from_ref"], s["to_ref"]) == (INV, P1)
    assert s["route"] == [[1, 0], [0, 0]] and s["length_ft"] == 10
    assert s["validity"] == {"state": "valid", "reasons": []}
    assert s["provenance"]["last_writer"] == s["provenance"]["tool_id"] == FLIP
    assert [p["assignment"]["seq"] for p in after["panels"]] == [1, 0, 0]
    assert [s["ordered_panel_refs"] for s in after["frames"][0]["sequences"]] == [[P2, P1], [P3]]
    assert after["strings"][1] == g["strings"][1]
    assert after["inverters"] == g["inverters"]
    for key in ("routes", "schedules"):
        assert after[key][0]["validity"] == stale("string_flipped")
    assert sha(after) == "fbe9041adcb3df15bfe2f0901430b0691b2aa7f92351f9ad9a44e3a1ced54aac"


def test_string_edit_flip_twice_restores(graph):
    g = variant(graph)
    after = run(run(g))
    assert after["rev"] == 2
    for key in ("ordered_panel_refs", "from_ref", "to_ref", "route"):
        assert after["strings"][0][key] == g["strings"][0][key]
    assert [p["assignment"] for p in after["panels"]] == [p["assignment"] for p in g["panels"]]
    for key in ("sequences", "panel_assignments", "matrix"):
        assert after["frames"][0][key] == g["frames"][0][key]
    assert sha(after) == "e27a88325e738e78e0f1a1a17a5aee7dd80ee485f476ba048c7e34a1d56597ce"


def test_string_edit_flip_c2_three_panels(graph):
    g = variant(graph, "C2")
    after = run(g)
    s = after["strings"][0]
    assert s["ordered_panel_refs"] == [P3, P2, P1]
    assert (s["from_ref"], s["to_ref"]) == (P3, P1)
    assert s["route"] == [[2, 0], [1, 0], [0, 0]]
    polarity = dict(g["strings"][0]["extra"]["polarity"],
                    negative_panel_ref=P3, positive_panel_ref=P1)
    assert s["extra"]["polarity"] == polarity
    assert [p["assignment"]["seq"] for p in after["panels"]] == [2, 1, 0]
    assert sha(after) == "3e851cf3800e467032defc1f213f0d1afd2b70411fdd74701714e84302f22cb5"
    restored = run(after)
    for key in ("ordered_panel_refs", "from_ref", "to_ref", "route"):
        assert restored["strings"][0][key] == g["strings"][0][key]
    assert restored["strings"][0]["extra"]["polarity"] == g["strings"][0]["extra"]["polarity"]
    assert sha(restored) == "2fbece1844a7b98f9f391d7ca60e48f5a9876891f894e65f46f503eac5f4e31d"


def test_string_edit_flip_invalidates_only_dependents(graph):
    g = variant(graph, "DEP")
    after = run(g)
    assert [r["validity"]["state"] for r in after["routes"]] == ["stale", "valid", "stale"]
    assert [s["validity"]["state"] for s in after["schedules"]] == [
        "stale", "valid", "stale", "stale", "valid"]
    for entity in after["routes"] + after["schedules"]:
        if entity["validity"]["state"] == "stale":
            assert entity["validity"] == stale("string_flipped")
    assert after["routes"][1] == g["routes"][1]
    for index in (1, 4):
        assert after["schedules"][index] == g["schedules"][index]
    assert sha(after) == "72d9be648dd25e34a18e93a33b59d8235af6f9e07691fd2794872a8ef53d5a25"


@pytest.mark.parametrize("tool", [FLIP, SWAP])
def test_string_edit_inverter_only_schedule_invalidation(graph, tool):
    g = variant(graph, "DEP")
    schedule = copy.deepcopy(g["schedules"][0])
    schedule["id"] = app_id("schedule", 99)
    schedule["source_refs"] = [INV]
    schedule["validity"] = {"state": "valid", "reasons": []}
    g["schedules"].append(schedule)
    validate_graph(g)
    before = copy.deepcopy(g)
    after = run(g, tool)
    assert g == before
    if tool == SWAP:
        assert inputs(after) == [(S2, "A", 0), (S1, "A", 1)]
        assert after["schedules"][-1]["validity"] == stale("string_swapped")
    else:
        assert after["inverters"] == g["inverters"]
        assert after["schedules"][-1] == schedule


@pytest.mark.parametrize("defect,patch", [
    ("result-type", None),
    ("keys", {"unexpected": 1}),
    ("handle", {"handle": "2"}),
    ("label", {"label": {"unexpected": 1}}),
    ("panels-type", {"panels": ("2", "1")}),
    ("panel-type", {"panels": ["2", []]}),
    ("panel-count", {"panels": ["2", "1", "1"]}),
    ("duplicate-surrogate", {"panels": ["1", "1"]}),
])
def test_string_edit_flip_malformed_kernel_output_is_atomic(graph, monkeypatch, defect, patch):
    g = variant(graph)
    params = request_for(g)
    before, original_params = copy.deepcopy(g), copy.deepcopy(params)
    builtin = solar_local_graph._load_builtin(FLIP)
    calls = []

    def malformed(source):
        calls.append(copy.deepcopy(source))
        if defect == "result-type":
            return None
        result = {"handle": "1", "panels": ["2", "1"], "label": {}}
        result.update(copy.deepcopy(patch))
        return result

    monkeypatch.setattr(builtin.chain, "string_flip", malformed)
    with pytest.raises(GraphValidationError) as error:
        builtin.run(g, params)
    assert error.value.code == "STRING_EDIT_MAPPING_FAILED"
    assert calls == [{"handle": "1", "panels": ["1", "2"], "label": {}}]
    assert g == before and params == original_params


@pytest.mark.parametrize("defect,patch", [
    ("results-type", None),
    ("result-count", None),
    ("result-type", None),
    ("keys", {"unexpected": 1}),
    ("handle", {"handle": "2"}),
    ("panels", {"panels": ["2", "1"]}),
    ("label-type", {"label": []}),
    ("label-keys", {"label": {"slot": 1, "unexpected": 1}}),
    ("boolean-slot", {"label": {"slot": True}}),
    ("slot-range", {"label": {"slot": 3}}),
    ("circuit-type", {"circuit": 7}),
    ("duplicate-slots", {"label": {"slot": 2}}),
])
def test_string_edit_swap_malformed_kernel_output_is_atomic(graph, monkeypatch, defect, patch):
    g = variant(graph, "SW")
    params = request_for(g, SWAP)
    before, original_params = copy.deepcopy(g), copy.deepcopy(params)
    builtin = solar_local_graph._load_builtin(SWAP)
    calls = []

    def malformed(first, second):
        calls.append(copy.deepcopy([first, second]))
        results = copy.deepcopy([first, second])
        if defect == "results-type":
            return None
        if defect == "result-count":
            return results[:1]
        if defect == "result-type":
            results[0] = None
        else:
            results[0].update(copy.deepcopy(patch))
        return results

    monkeypatch.setattr(builtin.chain, "string_swap", malformed)
    with pytest.raises(GraphValidationError) as error:
        builtin.run(g, params)
    assert error.value.code == "STRING_EDIT_MAPPING_FAILED"
    assert calls == [[
        {"handle": "1", "panels": ["1", "2"], "label": {"slot": 1}, "circuit": "1/1a"},
        {"handle": "2", "panels": ["1"], "label": {"slot": 2}, "circuit": "2/1b"},
    ]]
    assert g == before and params == original_params


def test_string_edit_flip_keeps_a_stale_string_stale(graph):
    g = variant(graph, "ST")
    after = run(g)
    assert after["strings"][0]["validity"] == stale("settings_changed")
    assert sha(after) == "e91fa026ef2b1b1af4e0edc8718dc2fd3a32d890589947b02a4bb4911443069b"


def test_string_edit_swap_kernel_projection():
    ka = {"handle": "1", "panels": ["1", "2"], "label": {"slot": 1}, "circuit": "1/1a"}
    kb = {"handle": "2", "panels": ["1"], "label": {"slot": 2}, "circuit": "2/1b"}
    result = list(chain.string_swap(ka, kb))
    assert result == [
        {"circuit": "2/1b", "handle": "1", "label": {"slot": 2}, "panels": ["1", "2"]},
        {"circuit": "1/1a", "handle": "2", "label": {"slot": 1}, "panels": ["1"]}]
    assert sha(result) == "5ae96bb30def392d411697a03830a31dae62189d8970ac2273f20904477b7f4f"


def test_string_edit_swap_fixture(graph):
    g = variant(graph, "SW")
    before = copy.deepcopy(g)
    after = run(g, SWAP)
    assert g == before
    assert [s["circuit_tag"] for s in after["strings"]] == ["2/1b", "1/1a"]
    for old, new in zip(g["strings"], after["strings"]):
        for key in old.keys() - {"circuit_tag", "rev", "provenance"}:
            assert new[key] == old[key]
    assert inputs(after) == [(S2, "A", 0), (S1, "A", 1)]
    assert [p["string_input_number"] for p in after["frames"][0]["panel_assignments"]] == [1, 1, 0]
    for key in ("routes", "schedules"):
        assert after[key][0]["validity"] == stale("string_swapped")
    assert after["inverters"][0]["validity"] == {"state": "valid", "reasons": []}
    assert after["inverters"][0]["rev"] == 1
    assert sha(after) == "cb4df22ee2653f2c22df37a36c6f4b1b2638f88af346b4c8fe0fbabd50eaf249"


def test_string_edit_swap_back_restores(graph):
    g = variant(graph, "SW")
    first = run(g, SWAP)
    params = dict(request_for(first, SWAP), string_refs=[S2, S1])
    after = run(first, SWAP, params)
    assert [s["circuit_tag"] for s in after["strings"]] == ["1/1a", "2/1b"]
    assert after["inverters"][0]["input_assignments"] == g["inverters"][0]["input_assignments"]
    assert sha(after) == "c0dee1feb80a1d20409e3a6218e993449b272b08f679af0bb1ec5705fd27afe5"


def test_string_edit_swap_with_an_unassigned_partner(graph):
    g = variant(graph, "U")
    after = run(g, SWAP)
    assert after["strings"][0]["inverter_ref"] is None
    assert after["strings"][1]["inverter_ref"] == INV
    assert inputs(after) == [(S2, "A", 0)]
    assert [s["circuit_tag"] for s in after["strings"]] == ["S2", "S1"]
    assert after["routes"][0]["validity"] == stale("string_swapped")
    assert sha(after) == "ece80d08a2ce247852ce6a8c548de430824b23810b53028bbaa8801d0e83162e"


def test_string_edit_swap_without_assignments(graph):
    g = variant(graph, "N")
    after = run(g, SWAP)
    assert [s["circuit_tag"] for s in after["strings"]] == ["S2", "S1"]
    assert after["inverters"] == g["inverters"]
    assert sha(after) == "af80f65e1dcf9a7db8ea326f3ab30a444d7094ea2abdc8fe70d7cf704c6e8ad2"
    after = run(variant(graph, "DEP"), SWAP)
    assert len(after["routes"]) == 3 and len(after["schedules"]) == 5
    for entity in after["routes"] + after["schedules"]:
        assert entity["validity"] == stale("string_swapped")
    assert sha(after) == "e7af25297bf8d2f454e294dbff0f6a8abb33c1ae221dbfcf533faf1d6dd9a677"


@pytest.mark.parametrize("tool,name,defect,code", [
    (FLIP, "G", "missing", "MISSING_STRING"),
    (FLIP, "G", "revision", "STALE_GRAPH_REVISION"),
    (FLIP, "G", "bounds", "STRING_EDIT_BOUNDS_EXCEEDED"),
    (SWAP, "SW", "same", "SAME_STRING_TWICE"),
    (SWAP, "SW", "missing", "MISSING_STRING"),
    (SWAP, "SW", "revision", "STALE_GRAPH_REVISION"),
    (SWAP, "G", "circuit", "STRING_EDIT_BOUNDS_EXCEEDED"),
])
def test_string_edit_refusals_are_atomic(graph, tmp_path, monkeypatch, tool, name, defect, code):
    g = variant(graph, name)
    params = request_for(g, tool)
    if defect == "missing":
        params.update({"string_ref": app_id("string", 99)} if tool == FLIP
                      else {"string_refs": [S1, app_id("string", 99)]})
    elif defect == "revision":
        params["expected_rev"] = 1
    elif defect == "bounds":
        monkeypatch.setattr(solar_local_graph._load_builtin(FLIP).chain, "MAX_PANELS_PER_STRING", 1)
    elif defect == "same":
        params["string_refs"] = [S1, S1]
    elif defect == "circuit":
        allowed = copy.deepcopy(g)
        allowed["strings"][0]["circuit_tag"] = "x" * 1024
        assert run(allowed, SWAP)["strings"][1]["circuit_tag"] == "x" * 1024
        g["strings"][0]["circuit_tag"] = "x" * 1025
        validate_graph(g)
    before, original_params = copy.deepcopy(g), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        run(g, tool, params)
    assert error.value.code == code
    backend, _ = seed(tmp_path, monkeypatch, g)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, tool, params)
    assert error.value.code == code
    assert g == before and params == original_params
    assert head_graph(backend) == g
    assert latest(backend) == 1


@pytest.mark.parametrize("tool,patch", [
    (FLIP, {"operation": "other"}),
    (FLIP, {"operation": ["flip-string"]}),
    (FLIP, {"unknown": 1}),
    (FLIP, {"string_ref": ""}),
    (FLIP, {"string_ref": 5}),
    (FLIP, {"string_ref": "x" * 129}),
    (FLIP, {"expected_rev": True}),
    (FLIP, {"drawing_id": "solar"}),
    (SWAP, {"operation": "flip-string"}),
    (SWAP, {"string_refs": [S1]}),
    (SWAP, {"string_refs": [S1, S2, S1]}),
    (SWAP, {"string_refs": "ab"}),
    (SWAP, {"string_refs": [S1, 7]}),
    (SWAP, {"string_refs": [S1, ""]}),
    (SWAP, {"unknown": 1}),
])
def test_string_edit_request_shape_fails_closed(graph, tool, patch):
    g = variant(graph)
    before = copy.deepcopy(g)
    params = dict(request_for(g, tool), **patch)
    code = "INVALID_STRING_FLIP_REQUEST" if tool == FLIP else "INVALID_STRING_SWAP_REQUEST"
    with pytest.raises(GraphValidationError) as error:
        run(g, tool, params)
    assert error.value.code == code
    assert g == before


@pytest.mark.parametrize("tool", [FLIP, SWAP])
def test_string_edit_registry_and_catalog(monkeypatch, tool):
    declaration = solar_tools.get(tool)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip()
    ref = {"type": "string", "minLength": 1, "maxLength": 128}
    if tool == FLIP:
        expected = expected_declaration(
            FLIP, "stringing", 72, "INVALID_STRING_FLIP_REQUEST", ["strings"],
            ["string-flip"], "flip-string", {"string_ref": ref}, ["string_ref"])
    else:
        expected = expected_declaration(
            SWAP, "stringing", 74, "INVALID_STRING_SWAP_REQUEST", ["strings"],
            ["string-swap"], "swap-strings",
            {"string_refs": {"type": "array", "minItems": 2, "maxItems": 2,
                             "uniqueItems": True, "items": ref}}, ["string_refs"])
    assert actual == expected
    assert "default" not in actual["record"]["params"]["properties"]["expected_rev"]
    assert tool in solar_tools.local_graph_tools()
    assert tool not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"]
             if row["name"] == tool]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


@pytest.mark.parametrize("tool", [FLIP, SWAP])
def test_string_edit_params_schema(graph, tool):
    schema = solar_tools.trusted_record(tool)["params"]
    validator = Draft7Validator(schema)
    valid = request_for(variant(graph), tool)
    assert validator.is_valid(valid)
    assert not validator.is_valid(dict(valid, unknown=1))
    if tool == FLIP:
        missing = dict(valid)
        del missing["string_ref"]
        assert not validator.is_valid(missing)
    else:
        assert not validator.is_valid(dict(valid, string_refs=[S1, S1]))
        assert not validator.is_valid(dict(valid, string_refs=[S1]))
    assert "default" not in schema["properties"]["expected_rev"]


@pytest.mark.parametrize("tool", [FLIP, SWAP])
def test_string_edit_readiness(graph, tool):
    g = variant(graph)
    assert availability.w1_graph_readiness(g)[tool] == {"input_ready": True, "input_reason": None}
    empty = copy.deepcopy(g)
    empty.update(strings=[], routes=[], schedules=[])
    for inverter in empty["inverters"]:
        inverter["input_assignments"] = []
    sync_assignments(empty)
    assert availability.w1_graph_readiness(empty)[tool] == {
        "input_ready": False, "input_reason": "strings_required"}
    g["project"]["units"]["meters_per_unit"] *= 2
    assert availability.w1_local_commit_inputs(g)[tool] == {
        "input_ready": False, "input_reason": "unresolved_units"}


def commit(api, g, tool):
    api[2][tool] = solar_tools.trusted_record(tool)
    response = api[0].post("/api/run?wait=1", json=body(api, tool, request_for(g, tool)))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    return env


@pytest.mark.parametrize("tool", [FLIP, SWAP])
def test_string_edit_run_rail_commits_one_job(api, graph, tool):
    g = variant(graph)
    env = commit(api, g, tool)
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 2
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    assert rows[0]["job_id"] == env["result"]["job_id"]
    assert jobs.get_job(rows[0]["job_id"])["status"] == "complete"
    assert head_graph(api[1]) == run(g, tool)
    if tool == FLIP:
        assert sha(head_graph(api[1])) == "fbe9041adcb3df15bfe2f0901430b0691b2aa7f92351f9ad9a44e3a1ced54aac"


@pytest.mark.parametrize("tool,patch", [
    (SWAP, {"string_refs": [S1, S1]}), (FLIP, {"unknown": 1}),
])
def test_string_edit_broker_refuses_schema_violations(api, graph, tool, patch):
    api[2][tool] = solar_tools.trusted_record(tool)
    params = dict(request_for(variant(graph), tool), **patch)
    response = api[0].post("/api/run?wait=1", json=body(api, tool, params))
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(rec.get("error") or {}).get("reason_code") for rec in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_string_edit_drawing_undo_redo(api, graph, monkeypatch):
    from routers import drawings

    g = variant(graph)
    commit(api, g, FLIP)
    client, backend, _, route, _, tenant = api
    client.app.include_router(drawings.router)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "string-edit-test-checkout-secret")
    _, fence = route._checkout_identity()
    headers = {checkout_capability.CAPABILITY_HEADER: checkout_capability.mint(tenant, "solar", fence)}
    committed = head_graph(backend)
    undone = client.post("/api/drawings/solar/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert head_graph(backend) == g
    redone = client.post("/api/drawings/solar/redo", headers=headers)
    assert redone.status_code == 200, redone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
    assert head_graph(backend) == committed
