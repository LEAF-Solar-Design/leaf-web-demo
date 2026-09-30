"""Frozen MULTISTRING combination, receipt memberships and local graph commit."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
ROOT = SERVER.parent
RECEIPT = ROOT / "docs/parity/receipts/string-multi-add/rooftop-demo.json"

import catalog
import checkout_capability
import deps
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_string_combo as combo
import solar_tools
import store
from solar_design_graph import GraphValidationError, deserialize_graph, serialize_graph, validate_graph
from solar_solve_results import sync_assignments
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, expected_declaration, head_graph, latest

TOOL = "solar-string-multi-add"
TENANT = "fixture-tenant"
G_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
M_SHA = "56fb003616147c2639b71276d56464213ad9473303eba5bc82ed6110c9b4d1bc"
STAMP = "2026-09-29T00:00:00+00:00"
HANDLES = ["92A9", "92AA", "92AB", "92AC", "92AD", "92AE", "92BC", "92BD", "92BE", "92BF", "92C0", "92C1", "92C2",
           "939D", "939E", "939F", "93A0", "93A1", "93A2", "93A3", "93A4", "93A5", "93A6", "93B0", "93B1", "93B2",
           "93B3"]
RECEIPT_ROWS = [
    ["92A9", "92AA", "92AB", "92AC", "92AD", "92AE", "92C2", "92C1", "92C0", "92BF", "92BE", "92BD", "92BC", "939D"],
    ["93A6", "93A5", "93A4", "93A3", "93A2", "93A1", "93A0", "939F", "939E", "93B0", "93B1", "93B2", "93B3"],
]
IDS = [app_id("panel", 101 + index) for index in range(27)]
BY_HANDLE = dict(zip(HANDLES, IDS))
NEW = [app_id("string", 901), app_id("string", 902)]


class FixedDatetime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 9, 29, tzinfo=timezone.utc)


def pin(module, monkeypatch, start=901):
    """Deterministic string ids (app_id("string", 901), 902, ...) and one fixed timestamp."""
    counter = [start]

    def fake_new_id(kind):
        value = app_id(kind, counter[0])
        counter[0] += 1
        return value

    monkeypatch.setattr(module, "new_id", fake_new_id)
    monkeypatch.setattr(module, "datetime", FixedDatetime)


def sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=False).encode("utf-8")).hexdigest()


def multi_graph(graph):
    """M: the W1 fixture plus one 3 x 9 group of 27 unwired panels named by the receipt's handles."""
    g = copy.deepcopy(graph)
    validate_graph(g)
    assert sha(g) == G_SHA
    g["settings"]["panels_in_sequence"] = 14
    panels = []
    for index, handle in enumerate(HANDLES):
        row, col = divmod(index, 9)
        panel = entity("panel", 101 + index, frame_ref=app_id("frame", 2),
                       matrix_cell={"row": row, "col": col}, centre=[col, 10 + 2 * row], angle=0,
                       assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = handle
        panels.append(panel)
    frame = entity(
        "frame", 2, name="Multi group", insertion_point=[0, 10, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=3, module_columns=9, module_slots=27,
        module_power_watts=400, module_width_along_row=1, module_height_across_row=2,
        electrical_zone_ref=None,
        matrix=[[{"code": "panel", "panel_ref": panels[r * 9 + c]["id"], "seq": None, "inverter_id": None,
                  "string_input_number": None, "x": panels[r * 9 + c]["centre"][0],
                  "y": panels[r * 9 + c]["centre"][1], "angle": 0} for c in range(9)] for r in range(3)],
        sequences=[],
        panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None, "inverter_id": None,
                            "string_input_number": None} for p in panels])
    g["panels"].extend(panels)
    g["frames"].append(frame)
    sync_assignments(g)
    validate_graph(g)
    assert sha(g) == M_SHA
    return g


def request(refs=IDS, length=14, rev=0):
    return {"operation": "add-strings", "expected_rev": rev,
            "string_length": length, "ordered_panel_refs": list(refs)}


def add(monkeypatch, g, **overrides):
    module = solar_local_graph._load_builtin(TOOL)
    pin(module, monkeypatch)
    params = {"expected_rev": 0, "string_length": 14, "ordered_panel_refs": list(IDS)}
    params.update(overrides)
    return module.add_strings(g, params)


def pinned(head):
    text = json.dumps(head)
    for index, string in enumerate(head["strings"][2:]):
        text = text.replace(string["id"], NEW[index])
    result = json.loads(text)
    for string in result["strings"][2:]:
        string["provenance"]["created_at"] = STAMP
    return result


def variant(graph, name):
    g = multi_graph(graph)
    if name == "UNSIZED":
        g["settings"]["panels_in_sequence"] = 0
        g["electrical_zones"][0]["panels_in_sequence"] = 0
    elif name == "ZONE":
        g["settings"]["panels_in_sequence"] = 0
        g["electrical_zones"][0]["panels_in_sequence"] = 14
    elif name == "SCHED":
        g["schedules"][0]["source_refs"] = g["schedules"][0]["source_refs"] + [IDS[0]]
    elif name == "TAGS":
        g["strings"][1]["circuit_tag"] = "S4"
    elif name == "UNITS":
        g["project"]["units"]["meters_per_unit"] *= 2
    validate_graph(g)
    hashes = {
        "ZONE": "3284737b1b53c8d65770f6ee15116d205af2a21b7e6f9dad02a95f66386817a1",
        "SCHED": "b1df4a5cb99c7f47209415eb273eb9355fa558b72dfd56f8f928488101f60670",
        "TAGS": "694e72fddf1b0d75f4543c73aeff9ca218d25a218a55d80c176261bbee34b992",
    }
    if name in hashes:
        assert sha(g) == hashes[name]
    return g


@pytest.fixture
def api_m(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, multi_graph(graph))
    yield from _api(backend, tmp_path, monkeypatch)


def test_string_multi_add_combo_kernel_cases():
    assert combo.string_combo(14, 27) == [[14, 13], [1, 1]]
    assert combo.sequences(14, 27) == [14, 13, 1, 1]
    for length, count in [(13, 27), (14, 11), (14, 1)]:
        result = combo.string_combo(length, count)
        assert result == [[0, 0], [0, 0]]
        assert combo.is_sentinel(result) is True
    assert combo.string_combo(14, 12) == [[13, 12], [0, 1]]


def test_string_multi_add_receipt_rows_are_frozen():
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["capability"] == "string-multi-add"
    assert receipt["comparator"]["verdict"] == "pass"
    comparison = receipt["comparison"]
    provenance = comparison["studio"]["provenance"]
    assert provenance["selected_panels"] == HANDLES
    assert provenance["multi_sequences"] == [14, 13, 1, 1] == combo.sequences(14, 27)
    assert provenance["added_lengths"] == [13, 14]
    rows = [[m["entity_id"] for m in s["ordered_membership"]]
            for s in comparison["plugin"]["after"]["strings"]
            if any(m["entity_id"] in set(HANDLES) for m in s["ordered_membership"])]
    assert rows == RECEIPT_ROWS
    flat = [handle for row in rows for handle in row]
    assert len(flat) == 27 and set(flat) == set(HANDLES)


def test_string_multi_add_c3_splits_27_panels_into_14_and_13(graph, monkeypatch):
    m = multi_graph(graph)
    before = copy.deepcopy(m)
    result = add(monkeypatch, m)
    assert m == before
    assert result["sequences"] == [14, 13, 1, 1]
    assert result["circuit_tags"] == ["S3", "S4"]
    assert result["string_refs"] == NEW
    assert result["ordered_panel_refs"] == [IDS[:14], IDS[14:]]
    assert result["total"] == 4
    g = result["graph"]
    assert (g["rev"], g["parent_rev"]) == (1, 0)
    assert g["settings"]["string_number"] == 5
    assert g["strings"][:2] == m["strings"]
    for key in ("inverters", "routes", "schedules"):
        assert g[key] == m[key]
    assert g["frames"][0] == m["frames"][0]
    for string, refs in zip(g["strings"][2:], [IDS[:14], IDS[14:]]):
        assert string["module_count"] == len(refs)
        assert (string["from_ref"], string["to_ref"]) == (refs[0], refs[-1])
        polarity = string["extra"]["polarity"]
        assert (polarity["negative_panel_ref"], polarity["positive_panel_ref"]) == (refs[0], refs[-1])
        assert string["inverter_ref"] is None
        assert string["validity"] == {"state": "valid", "reasons": []}
        assert string["wire_gauge"] == "" and string["tag_text_ref"] is None
        provenance = string["provenance"]
        assert provenance["created_by"] == provenance["last_writer"] == provenance["tool_id"] == TOOL
        assert provenance["created_at"] == STAMP
    assert [s["length_ft"] for s in g["strings"][2:]] == [66.4245775959164, 63.14373770090329]
    assert g["strings"][2]["route"] == [
        [0, 10], [1, 10], [2, 10], [3, 10], [4, 10], [5, 10], [6, 10],
        [7, 10], [8, 10], [0, 12], [1, 12], [2, 12], [3, 12], [4, 12]]
    assert [p["assignment"]["seq"] for p in g["panels"] if p["id"] in IDS] == (
        list(range(14)) + list(range(13)))
    assert g["extra"]["solve_coverage"] == {"duplicate_panel_refs": [], "unassigned_panel_refs": []}
    assert deserialize_graph(serialize_graph(g)) == g
    assert sha(g) == "2c2551233569e3ae0f5a8b4ba1d1409f2655cc2d1e13af17982b26117c63fe06"


def test_string_multi_add_receipt_order_commits_the_plugin_rows(graph, monkeypatch):
    refs = [BY_HANDLE[h] for row in RECEIPT_ROWS for h in row]
    result = add(monkeypatch, multi_graph(graph), ordered_panel_refs=refs)
    assert result["sequences"] == [14, 13, 1, 1]
    handles = dict(zip(IDS, HANDLES))
    assert [[handles[ref] for ref in row] for row in result["ordered_panel_refs"]] == RECEIPT_ROWS
    assert [s["length_ft"] for s in result["graph"]["strings"][2:]] == [78.42213090789929, 66.4245775959164]
    assert sha(result["graph"]) == "0edc711c1fcd57830c64443056e9e2c694aa5c9158e9bfa2fd5467474fcf2af4"

@pytest.mark.parametrize("length,sequences,sizes,last_tag,digest", [
    (5, [5, 4, 3, 3], [5, 5, 5, 4, 4, 4], "S8", "706d24065f766c6988821479020f1137ca84d383a44b0450d2fcf6565724459c"),
    (4, [4, 3, 6, 1], [4, 4, 4, 4, 4, 4, 3], "S9", "6d75851ac562a91ec638df8b1ad6ee0d4a1ed6c7ce655e00d5c83a06e76f74d9"),
    (3, [3, 2, 9, 0], [3] * 9, "S11", "114d89ee9501dbbc3d22a2b7bf0b45f6780aed66aea0aebc7fa6535f5589f665"),
    (2, [2, 1, 13, 1], [2] * 13 + [1], "S16", "2f498406578e5a3f077c0642ebaf1556e6e841f929b0c31d367d0d9023913963"),
    (1, [1, 0, 27, 0], [1] * 27, "S29", "8496fcef42734ea3cc2e6dac29f7c6b83256d3d951c30746d0280e0f2efdca88"),
])
def test_string_multi_add_other_targets(graph, monkeypatch, length, sequences, sizes, last_tag, digest):
    result = add(monkeypatch, multi_graph(graph), string_length=length)
    assert result["sequences"] == sequences
    assert [len(row) for row in result["ordered_panel_refs"]] == sizes
    assert result["circuit_tags"][-1] == last_tag
    assert [ref for row in result["ordered_panel_refs"] for ref in row] == IDS
    assert sha(result["graph"]) == digest


@pytest.mark.parametrize("refs,length,sequences,digest", [
    (IDS[:14], 14, [14, 13, 1, 0], "8b17bc7ef94ecf8a227921bf7f7b5ad96dcd7001010310e8e1a7076af54384b4"),
    (IDS[:13], 14, [14, 13, 0, 1], "983118aa7d065de068dea68fb5642f311cf26aa68ddcaeb99afd4a5b664a4fe4"),
    (IDS[:12], 14, [13, 12, 0, 1], "a9e03bfceae694b3720da9f7ae4a3a59c22af08c227d6843f55a91865d7cee08"),
    (IDS[:1], 1, [1, 0, 1, 0], "2879f9a8de8be0f5db772a35ed48497c591880050d0c1caec16367ffb0246492"),
])
def test_string_multi_add_partial_selections(graph, monkeypatch, refs, length, sequences, digest):
    result = add(monkeypatch, multi_graph(graph), ordered_panel_refs=refs, string_length=length)
    assert result["sequences"] == sequences
    assert result["ordered_panel_refs"] == [refs]
    assert result["total"] == 3
    assert sha(result["graph"]) == digest


def test_string_multi_add_one_string_equals_single_add(graph, monkeypatch):
    m = multi_graph(graph)
    single = solar_local_graph._load_builtin("solar-string-add")
    monkeypatch.setattr(single, "new_id", lambda kind: app_id(kind, 901))
    monkeypatch.setattr(single, "datetime", FixedDatetime)
    single_graph = single.add_string(copy.deepcopy(m), {
        "expected_rev": 0, "ordered_panel_refs": IDS[:14]})["graph"]
    assert sha(single_graph) == "62fdc946dc4bf55fb6aaa66b1e3f6cdfb589d99456a77e134805b578d3a2b985"
    result = add(monkeypatch, m, ordered_panel_refs=IDS[:14])
    assert result["graph"] == json.loads(json.dumps(single_graph).replace(
        '"solar-string-add"', '"solar-string-multi-add"'))


def test_string_multi_add_keeps_the_callers_order(graph, monkeypatch):
    result = add(monkeypatch, multi_graph(graph), ordered_panel_refs=IDS[::-1])
    assert result["ordered_panel_refs"] == [IDS[::-1][:14], IDS[::-1][14:]]
    assert [row[0] for row in result["ordered_panel_refs"]] == [IDS[26], IDS[12]]
    assert sha(result["graph"]) == "4ccc93baa0709130900017dba49f6908bbabda62ed024d53a6f93f9925339816"


def test_string_multi_add_zone_sized_graph(graph, monkeypatch):
    result = add(monkeypatch, variant(graph, "ZONE"))
    assert result["ordered_panel_refs"] == [IDS[:14], IDS[14:]]
    assert sha(result["graph"]) == "f98f57ded6f29a381f9250f4056cdeb0335f3afcc9768d2f3c78ed643b301caf"


def test_string_multi_add_stales_panel_dependents_only(graph, monkeypatch):
    g = variant(graph, "SCHED")
    after = add(monkeypatch, g)["graph"]
    assert after["schedules"][0]["validity"] == {"state": "stale", "reasons": ["upstream_corrected"]}
    assert after["schedules"][0]["rev"] == 1
    assert after["routes"] == g["routes"]
    assert after["strings"][:2] == g["strings"]
    assert sha(after) == "e72ce24d702d15787e985671551bd00ad2abf1380323405029fcf2e5c0426315"


def test_string_multi_add_tags_skip_existing(graph, monkeypatch):
    result = add(monkeypatch, variant(graph, "TAGS"))
    assert result["circuit_tags"] == ["S3", "S5"]
    assert result["graph"]["settings"]["string_number"] == 6
    assert sha(result["graph"]) == "9c2f2c3c3a6f86992f9e19a2ebf5ccb59c58007df86dc2d96b5c1d8dafa46236"


@pytest.mark.parametrize("name,patch,code", [
    ("M", {"string_length": 13}, "MULTI_ADD_INFEASIBLE_COUNT"),
    ("M", {"ordered_panel_refs": IDS[:11]}, "MULTI_ADD_INFEASIBLE_COUNT"),
    ("M", {"ordered_panel_refs": IDS[:1]}, "MULTI_ADD_INFEASIBLE_COUNT"),
    ("M", {"string_length": 15}, "STRING_TOO_LONG"),
    ("UNSIZED", {}, "STRING_LENGTH_NOT_SIZED"),
    ("M", {"ordered_panel_refs": IDS[:13] + [app_id("panel", 1)]}, "PANEL_ALREADY_ASSIGNED"),
    ("M", {"ordered_panel_refs": IDS[:13] + [app_id("panel", 99)]}, "MISSING_PANEL"),
    ("M", {"ordered_panel_refs": IDS[:13] + [IDS[0]]}, "DUPLICATE_PANEL_MEMBERSHIP"),
    ("M", {"expected_rev": 1}, "STALE_GRAPH_REVISION"),
    ("UNITS", {}, "UNRESOLVED_UNITS"),
])
def test_string_multi_add_refusals_are_atomic(graph, tmp_path, monkeypatch, name, patch, code):
    g = variant(graph, name)
    params = dict(request(), **patch)
    before, original_params = copy.deepcopy(g), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        solar_local_graph._load_builtin(TOOL).run(g, params)
    assert error.value.code == code
    backend, _ = seed(tmp_path, monkeypatch, g)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, TOOL, params)
    assert error.value.code == code
    assert g == before and params == original_params
    assert head_graph(backend) == g
    assert latest(backend) == 1


@pytest.mark.parametrize("counter,existing_tag,tags,next_number", [
    (3.0, "S2", ["S3", "S4"], 5),
    (-0.0, "S2", ["S0", "S3"], 4),
    (3, "S5", ["S3", "S4"], 6),
])
def test_string_multi_add_counter_is_next_unused_integer(
        graph, monkeypatch, counter, existing_tag, tags, next_number):
    g = multi_graph(graph)
    g["settings"]["string_number"] = counter
    g["strings"][1]["circuit_tag"] = existing_tag
    validate_graph(g)
    before = copy.deepcopy(g)
    result = add(monkeypatch, g)
    assert result["circuit_tags"] == tags
    number = result["graph"]["settings"]["string_number"]
    assert number == next_number
    assert type(number) is int
    assert g == before


@pytest.mark.parametrize("sequence", [[14, 13, 1, 2], [15, 12, 1, 1]])
def test_string_multi_add_invalid_sizes_refuse_without_publication(
        graph, tmp_path, monkeypatch, sequence):
    g = multi_graph(graph)
    params = request()
    before, original_params = copy.deepcopy(g), copy.deepcopy(params)
    module = solar_local_graph._load_builtin(TOOL)
    assert not module.combo.is_sentinel([sequence[:2], sequence[2:]])
    monkeypatch.setattr(module.combo, "sequences", lambda length, count: list(sequence))
    with pytest.raises(GraphValidationError) as error:
        module.run(g, params)
    assert error.value.code == "MULTI_ADD_INFEASIBLE_COUNT"
    backend, _ = seed(tmp_path, monkeypatch, g)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, TOOL, params)
    assert error.value.code == "MULTI_ADD_INFEASIBLE_COUNT"
    assert g == before and params == original_params
    assert head_graph(backend) == g
    assert latest(backend) == 1


DROP = object()


@pytest.mark.parametrize("patch", [
    {"operation": "other"}, {"operation": ["add-strings"]},
    {"operation": "add-string"}, {"unknown": 1}, {"drawing_id": "solar"},
    {"expected_rev": True}, {"expected_rev": "0"}, {"string_length": 0},
    {"string_length": 901}, {"string_length": True}, {"string_length": 14.0},
    {"string_length": DROP}, {"ordered_panel_refs": []}, {"ordered_panel_refs": "abc"},
    {"ordered_panel_refs": [5]}, {"ordered_panel_refs": [""]},
    {"ordered_panel_refs": ["x" * 129]},
    {"ordered_panel_refs": [f"x{i}" for i in range(901)]},
])
def test_string_multi_add_request_shape_fails_closed(graph, patch):
    g = multi_graph(graph)
    before = copy.deepcopy(g)
    params = request()
    for key, value in patch.items():
        if value is DROP:
            del params[key]
        else:
            params[key] = value
    with pytest.raises(GraphValidationError) as error:
        solar_local_graph._load_builtin(TOOL).run(g, params)
    assert error.value.code == "INVALID_STRING_MULTI_ADD_REQUEST"
    assert g == before


def test_string_multi_add_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip()
    assert actual == expected_declaration(
        TOOL, "stringing", 71, "INVALID_STRING_MULTI_ADD_REQUEST", ["panels"],
        ["string-multi-add"], "add-strings",
        {"string_length": {"type": "integer", "minimum": 1, "maximum": 900},
         "ordered_panel_refs": {"type": "array", "minItems": 1, "maxItems": 900,
                                "items": {"type": "string", "minLength": 1, "maxLength": 128}}},
        ["string_length", "ordered_panel_refs"])
    for name in ("expected_rev", "string_length"):
        assert "default" not in actual["record"]["params"]["properties"][name]
    tools = solar_tools.local_graph_tools()
    assert TOOL in tools
    assert tools.index(TOOL) == tools.index("solar-string-add") + 1
    assert TOOL not in availability.W1_CAPABILITIES
    assert len(availability.W1_CAPABILITIES) == 9
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"]
             if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_string_multi_add_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    valid = request()
    assert validator.is_valid(valid)
    missing = dict(valid)
    del missing["string_length"]
    assert not validator.is_valid(missing)
    for patch in [
        {"unknown": 1}, {"string_length": 0}, {"string_length": 901}, {"string_length": 14.5},
        {"ordered_panel_refs": []}, {"ordered_panel_refs": [f"x{i}" for i in range(901)]},
        {"ordered_panel_refs": [""]},
    ]:
        assert not validator.is_valid(dict(valid, **patch))


def test_string_multi_add_readiness(graph):
    m = multi_graph(graph)
    assert availability.w1_graph_readiness(m)[TOOL] == {"input_ready": True, "input_reason": None}
    assert availability.w1_local_commit_inputs(dict(m, panels=[]))[TOOL] == {
        "input_ready": False, "input_reason": "panels_required"}
    assert availability.w1_local_commit_inputs(variant(graph, "UNITS"))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


def commit(api_m):
    api_m[2][TOOL] = solar_tools.trusted_record(TOOL)
    response = api_m[0].post("/api/run?wait=1", json=body(api_m, TOOL, request()))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    return env


def test_string_multi_add_run_rail_commits_one_job(api_m):
    env = commit(api_m)
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(api_m[1], TENANT, "solar")["head"] == 2
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    assert rows[0]["job_id"] == env["result"]["job_id"]
    assert jobs.get_job(rows[0]["job_id"])["status"] == "complete"
    head = head_graph(api_m[1])
    assert len(head["strings"]) == 4
    assert [s["module_count"] for s in head["strings"]] == [2, 1, 14, 13]
    assert [s["circuit_tag"] for s in head["strings"]] == ["S1", "S2", "S3", "S4"]
    assert sha(pinned(head)) == "2c2551233569e3ae0f5a8b4ba1d1409f2655cc2d1e13af17982b26117c63fe06"


@pytest.mark.parametrize("patch", [
    {"string_length": 0}, {"unknown": 1}, {"ordered_panel_refs": []},
])
def test_string_multi_add_broker_refuses_schema_violations(api_m, patch):
    api_m[2][TOOL] = solar_tools.trusted_record(TOOL)
    response = api_m[0].post("/api/run?wait=1", json=body(api_m, TOOL, dict(request(), **patch)))
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(rec.get("error") or {}).get("reason_code") for rec in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api_m[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_string_multi_add_drawing_undo_redo(api_m, graph, monkeypatch):
    from routers import drawings

    m = multi_graph(graph)
    commit(api_m)
    client, backend, _, route, _, tenant = api_m
    client.app.include_router(drawings.router)
    client.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    monkeypatch.setenv("LEAF_CHECKOUT_CAP_SECRET", "string-multi-add-test-checkout-secret")
    _, fence = route._checkout_identity()
    headers = {checkout_capability.CAPABILITY_HEADER: checkout_capability.mint(tenant, "solar", fence)}
    committed = head_graph(backend)
    undone = client.post("/api/drawings/solar/undo", headers=headers)
    assert undone.status_code == 200, undone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 1
    assert head_graph(backend) == m
    redone = client.post("/api/drawings/solar/redo", headers=headers)
    assert redone.status_code == 200, redone.text
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
    assert head_graph(backend) == committed
