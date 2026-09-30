"""LEAFSTRINGMID on the W1 graph: one midpoint-connection string along the kernel's path, receipt m1 replayed."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_batch2_simple as kernel
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError, validate_graph
from solar_solve_results import sync_assignments
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import _api, body
from test_w1_solve_commit import seed
from test_solar_w2_registration import REF, dispatch, expected_declaration, head_graph, latest

TOOL = "solar-string-midpoint"
TENANT = "fixture-tenant"
INTAKE = ROOT / "docs/parity/evidence/batch2/m0-intake.json"
RECEIPT = ROOT / "docs/parity/receipts/string-midpoint-connection/w4-m1.json"
G_SHA = "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
M_SHA = "3f28dc15f6d209320ed9c005d8476bdbb15ea85a82ae218c43f795cc62368f67"
ROWS, COLS, DX, DY = 2, 6, 2.1, 1.2
P1 = app_id("panel", 1)
DANGLING = app_id("panel", 99)
LOOSE = app_id("panel", 150)
NEW = app_id("string", 901)
STAMP = "2026-09-30T00:00:00+00:00"
DROP = object()


def sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=False).encode("utf-8")).hexdigest()


def P(row, col):
    return app_id("panel", 101 + row * COLS + col)


class FixedDatetime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 9, 30, tzinfo=timezone.utc)


def builtin(monkeypatch=None):
    """The builtin as the rail loads it, with app_id("string", 901) as the new id and one fixed time."""
    module = solar_local_graph._load_builtin(TOOL)
    counter = [901]

    def fake_new_id(kind):
        value = app_id(kind, counter[0])
        counter[0] += 1
        return value

    module.new_id = fake_new_id
    module.datetime = FixedDatetime
    return module


def mid_graph(graph, angle=0):
    """M: the W1 fixture plus one 2 x 6 group of unwired panels (module 2 x 1 m, pitch 2.1 x 1.2 m) and a sized
    length of 14."""
    g = copy.deepcopy(graph)
    validate_graph(g)
    assert sha(g) == G_SHA
    g["settings"]["panels_in_sequence"] = 14
    panels = []
    for index in range(ROWS * COLS):
        row, col = divmod(index, COLS)
        panels.append(entity("panel", 101 + index, frame_ref=app_id("frame", 2),
                             matrix_cell={"row": row, "col": col}, centre=[col * DX, 10 + row * DY], angle=angle,
                             assignment={"string_ref": None, "seq": None}))
    frame = entity(
        "frame", 2, name="Mid group", insertion_point=[0, 10, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=ROWS, module_columns=COLS, module_slots=ROWS * COLS,
        module_power_watts=400, module_width_along_row=2, module_height_across_row=1, electrical_zone_ref=None,
        matrix=[[{"code": "panel", "panel_ref": panels[r * COLS + c]["id"], "seq": None, "inverter_id": None,
                  "string_input_number": None, "x": panels[r * COLS + c]["centre"][0],
                  "y": panels[r * COLS + c]["centre"][1], "angle": angle} for c in range(COLS)]
                for r in range(ROWS)],
        sequences=[],
        panel_assignments=[{"panel_ref": p["id"], "string_ref": None, "seq": None, "inverter_id": None,
                            "string_input_number": None} for p in panels])
    g["panels"].extend(panels)
    g["frames"].append(frame)
    sync_assignments(g)
    return validate_graph(g)


def variant(graph, name="M"):
    g = mid_graph(graph, angle=90 if name == "ROT" else 0)
    if name == "M":
        assert sha(g) == M_SHA
    elif name == "UNSIZED":
        g["settings"]["panels_in_sequence"] = 0
        g["electrical_zones"][0]["panels_in_sequence"] = 0
    elif name == "SHORT":
        g["settings"]["panels_in_sequence"] = 5
    elif name == "UNITS":
        g["project"]["units"]["meters_per_unit"] *= 2
    elif name == "LOOSE":
        g["panels"].append(entity("panel", 150, frame_ref=None, matrix_cell=None, centre=[0, 20], angle=0,
                                  assignment={"string_ref": None, "seq": None}))
    elif name == "GAP":
        g["panels"][-1]["centre"] = [100.0, 100.0]
    elif name == "WIRED":
        g = builtin().add_midpoint_string(g, request())["graph"]
    return validate_graph(g)


def request(start=None, end=None, rev=0):
    return {"expected_rev": rev, "start_panel_ref": start or P(0, 0), "end_panel_ref": end or P(0, 5)}


def replay_graph(graph, intake):
    """m0-intake as a W1 graph: panel i (intake order) is app_id("panel", i + 1), all of them in one one-row frame
    whose module width and height are twice the intake half extents in metres; centres in metres; angle 0; no
    strings, inverters, routes, schedules or electrical zones; sized length 14. Returns (graph, {id: handle})."""
    g = copy.deepcopy(graph)
    scale = g["project"]["units"]["meters_per_unit"]
    hx, hy = intake["half_extents"]
    refs = [app_id("panel", n) for n in range(1, len(intake["panels"]) + 1)]
    handles = dict(zip(refs, (row["handle"] for row in intake["panels"])))
    frame = copy.deepcopy(g["frames"][0])
    frame.update(id=app_id("frame", 1), name="Group 1", panel_refs=list(refs), module_rows=1,
                 module_columns=len(refs), module_slots=len(refs),
                 module_width_along_row=2 * hx * scale, module_height_across_row=2 * hy * scale,
                 electrical_zone_ref=None, sequences=[],
                 matrix=[[{"code": "panel", "panel_ref": ref, "seq": None, "inverter_id": None,
                           "string_input_number": None, "x": float(col), "y": 0.0, "angle": 0}
                          for col, ref in enumerate(refs)]],
                 panel_assignments=[{"panel_ref": ref, "string_ref": None, "seq": None, "inverter_id": None,
                                     "string_input_number": None} for ref in refs])
    panels = []
    for col, (ref, row) in enumerate(zip(refs, intake["panels"])):
        panel = copy.deepcopy(g["panels"][0])
        panel.update(id=ref, frame_ref=frame["id"], matrix_cell={"row": 0, "col": col},
                     centre=[row["x"] * scale, row["y"] * scale], angle=0,
                     assignment={"string_ref": None, "seq": None})
        panels.append(panel)
    g.update(panels=panels, frames=[frame], strings=[], electrical_zones=[], inverters=[], routes=[],
             schedules=[])
    g["settings"]["panels_in_sequence"] = 14
    sync_assignments(g)
    return validate_graph(g), handles


def receipt_rows():
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["comparator"]["verdict"] == "pass"
    out = []
    for side in ("plugin", "studio"):
        (row,) = receipt["comparison"][side]["after"]["rows"]
        assert row["id"] == {"entity_id": "mid-string-1"} and row["type"] == "mid-string"
        out.append({key: value for key, value in row.items() if key not in ("id", "type", "quantity", "unit")})
    assert receipt["comparison"]["plugin"]["parameters"] == {"answers": ["handle:8D3A", "handle:8D9D"]}
    return out


def test_string_midpoint_kernel_m1():
    intake = json.loads(INTAKE.read_text(encoding="utf-8-sig"))
    rows = kernel.string_midpoint_rows(intake, "8D3A", "8D9D")
    assert sha(rows) == "b16fc327ab96e69e436c41c56d2e376ce3f1a19e6d2069db8deb46acde8d94ee"
    (row_id, fields), = rows
    assert row_id == "mid-string-1"
    for side in receipt_rows():
        assert side == fields


def test_string_midpoint_receipt_replay(graph):
    intake = json.loads(INTAKE.read_text(encoding="utf-8-sig"))
    g, handles = replay_graph(graph, intake)
    assert (len(g["panels"]), len(g["frames"]), len(g["strings"])) == (3526, 1, 0)
    assert sha(g) == "8ab4bd26be4f2a1a6c7d3316a3dd24fa75e593cf42eff406ced5d5c15d4d8d54"
    by_handle = {handle: ref for ref, handle in handles.items()}
    before = copy.deepcopy(g)
    result = builtin().add_midpoint_string(g, request(by_handle["8D3A"], by_handle["8D9D"]))
    assert g == before
    mapped = dict(result["mid_string"], panels=[handles[ref] for ref in result["mid_string"]["panels"]])
    assert sha(mapped) == "84ba3737ee8225cd8ce4acd5a3efc238b8d1252823ae36509eb82e1218ddc58e"
    for side in receipt_rows():
        assert side == mapped
    after = result["graph"]
    (string,) = after["strings"]
    assert (string["id"], string["circuit_tag"], string["module_count"]) == (NEW, "S3", 9)
    assert string["ordered_panel_refs"] == result["ordered_panel_refs"] == result["mid_string"]["panels"]
    assert [handles[ref] for ref in string["ordered_panel_refs"]] == mapped["panels"]
    assert string["extra"]["midpoint"] == {
        "source": "derived", "rule": "leafstringmid-floor-half", "tag_index": 4,
        "tag_panel_ref": app_id("panel", 1510), "label_text": "MID",
        "label_height_m": 1.0667608265699007, "source_rev": 0}
    assert handles[app_id("panel", 1510)] == "8DF1"
    assert string["route"] == [g["panels"][int(ref[-12:]) - 1]["centre"] for ref in string["ordered_panel_refs"]]
    assert string["length_ft"] == 92.03606215312601
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    assert sha(string) == "d8c8d16ab72510d3fe8376dd37070636cc009df3d35676ea08f0c71b59a5747b"
    assert sha(after) == "02d3c15d44e0b08cc59df8ecb45f3eefe47ae5cc129bb19a564b908d3529e647"


@pytest.mark.parametrize("start,end,path,tag_index,length_ft,mid_sha,graph_sha", [
    pytest.param(P(0, 0), P(0, 5), [P(0, c) for c in range(6)], 3, 34.44881889763779,
                 "71cacb0e763877d447a67af26ea0b7344809b03e51e7389c50a80570bbe57211",
                 "db36535c0133c10d19914e8c118c87aa70cafec4432d192c417de02cc83f0e9c", id="row"),
    pytest.param(P(0, 5), P(0, 0), [P(0, c) for c in range(5, -1, -1)], 3, 34.44881889763779,
                 "eb95d6f17c9b99633ba984f2afcb8c829c408938b5cd8494c8c1ff6819a9a082",
                 "a67c2e0c0cd4b771c0927192cae8667cf0b10d73c033f40aa7631c484e5cd69e", id="reverse"),
    pytest.param(P(0, 0), P(1, 5), [P(0, 0), P(0, 1), P(0, 2), P(0, 3), P(0, 4), P(1, 5)], 3, 35.4943481774592,
                 "4e805ee157c559df91d2e7ea43576c8dbf360507c02cb50167b59080fcf23d47",
                 "839b0af0afa669e7d62dc9b5b5a0633498cf7a13a6c7fd80700a448d384176de", id="corner"),
    pytest.param(P(0, 0), P(0, 2), [P(0, 0), P(0, 1), P(0, 2)], 1, 13.779527559055119,
                 "b4754e453a847c713a9ebdf855e9bdc20ea0cdbd8be827cf56ddaf7159b80b13",
                 "04d5f1fa910db83c6cf007ef16cfca0b610e12eee56b58c81bbb019cd9364236", id="three"),
])
def test_string_midpoint_commits_on_the_grid(graph, start, end, path, tag_index, length_ft, mid_sha, graph_sha):
    g = variant(graph)
    before, params = copy.deepcopy(g), request(start, end)
    original = copy.deepcopy(params)
    result = builtin().add_midpoint_string(g, params)
    assert g == before and params == original
    after = result["graph"]
    assert (result["string_ref"], result["circuit_tag"]) == (NEW, "S3")
    assert result["ordered_panel_refs"] == result["mid_string"]["panels"] == path
    assert result["mid_string"]["label_index"] == tag_index
    assert result["mid_string"]["label_height"] == 35.213669
    assert sha(result["mid_string"]) == mid_sha
    string = after["strings"][-1]
    assert string["ordered_panel_refs"] == path and string["module_count"] == len(path)
    assert (string["from_ref"], string["to_ref"], string["inverter_ref"], string["tag_text_ref"]) == (
        path[0], path[-1], None, None)
    assert string["extra"]["midpoint"]["tag_index"] == tag_index
    assert string["extra"]["midpoint"]["tag_panel_ref"] == path[tag_index]
    assert string["extra"]["midpoint"]["label_height_m"] == 0.894427190999916
    assert string["length_ft"] == length_ft
    assert after["strings"][:2] == g["strings"]
    assert after["routes"] == g["routes"] and after["schedules"] == g["schedules"]
    assert after["settings"]["string_number"] == 4
    assert (after["rev"], after["parent_rev"]) == (1, 0)
    assert [p["assignment"] for p in after["panels"] if p["id"] in path] == [
        {"string_ref": NEW, "seq": path.index(p["id"])} for p in after["panels"] if p["id"] in path]
    assert sha(after) == graph_sha
    assert builtin().run(g, dict(params, operation="add-midpoint-string")) == after


def test_string_midpoint_extents(graph):
    module = builtin()
    frame = {"module_width_along_row": 2, "module_height_across_row": 1}
    # Trig-derived values: glibc and MSVC libm differ in the last bit (Linux CI read
    # 2.1213203435596424 where Windows reads 2.121320343559643), so compare within 1e-12.
    def near(angle, expected):
        assert module.extents(frame, {"angle": angle}) == pytest.approx(expected, rel=1e-12, abs=1e-12)
    near(0, (2.0, 1.0))
    near(90, (1.0000000000000002, 2.0))
    near(30, (2.232050807568877, 1.8660254037844386))
    near(-45, (2.121320343559643, 2.121320343559643))
    near(180, (2.0, 1.0000000000000002))
    turned = builtin().add_midpoint_string(variant(graph, "ROT"), request())
    assert turned["ordered_panel_refs"] == [P(0, c) for c in range(6)]


@pytest.mark.parametrize("name,params,code", [
    pytest.param("M", request(P(0, 3), P(0, 3)), "MIDPOINT_ENDPOINTS_IDENTICAL", id="identical"),
    pytest.param("M", request(rev=1), "STALE_GRAPH_REVISION", id="stale"),
    pytest.param("UNITS", request(), "UNRESOLVED_UNITS", id="units"),
    pytest.param("UNSIZED", request(), "STRING_LENGTH_NOT_SIZED", id="unsized"),
    pytest.param("M", request(DANGLING, P(0, 5)), "MISSING_PANEL", id="start-missing"),
    pytest.param("M", request(P(0, 0), DANGLING), "MISSING_PANEL", id="end-missing"),
    pytest.param("M", request(P1, P(0, 5)), "DIFFERENT_PANEL_GROUPS", id="groups"),
    pytest.param("LOOSE", request(LOOSE, P(0, 5)), "PANEL_GROUP_REQUIRED", id="ungrouped"),
    pytest.param("M", request(P(0, 2), P(1, 2)), "MIDPOINT_PATH_TOO_SHORT", id="adjacent"),
    pytest.param("GAP", request(P(0, 0), P(1, 5)), "MIDPOINT_NO_PATH", id="no-path"),
    pytest.param("SHORT", request(), "STRING_TOO_LONG", id="too-long"),
    pytest.param("WIRED", request(rev=1), "PANEL_ALREADY_ASSIGNED", id="wired"),
])
def test_string_midpoint_refusals_are_atomic(graph, tmp_path, monkeypatch, name, params, code):
    g = variant(graph, name)
    params = dict(params, operation="add-midpoint-string")
    before, original = copy.deepcopy(g), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, params)
    assert error.value.code == code
    backend, _ = seed(tmp_path, monkeypatch, g)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, TOOL, params)
    assert error.value.code == code
    assert g == before and params == original
    assert head_graph(backend) == g
    assert latest(backend) == 1


@pytest.mark.parametrize("width,height", [(1e160, 1), (1e-310, 1e-310)])
def test_string_midpoint_extreme_dimensions_refuse_atomically(graph, width, height):
    g = variant(graph)
    g["frames"][1]["module_width_along_row"] = width
    g["frames"][1]["module_height_across_row"] = height
    validate_graph(g)
    before = sha(g)
    params = dict(request(), operation="add-midpoint-string")
    original = sha(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, params)
    assert error.value.code == "STRING_MIDPOINT_GEOMETRY_OUT_OF_RANGE"
    assert sha(g) == before and sha(params) == original


def test_string_midpoint_preserves_untouched_frame(graph):
    g = variant(graph)
    g["frames"][0]["sequences"].reverse()
    validate_graph(g)
    before = sha(g)
    original_frame = sha(g["frames"][0])
    result = builtin().add_midpoint_string(g, request())
    assert sha(g) == before
    assert sha(result["graph"]["frames"][0]) == original_frame
    assert result["graph"]["frames"][1]["sequences"] == [
        {"string_ref": NEW, "ordered_panel_refs": [P(0, c) for c in range(6)]}]


@pytest.mark.parametrize("collision,tag,counter", [(False, "S3", 4), (True, "S4", 5)])
def test_string_midpoint_integral_float_counter(graph, collision, tag, counter):
    g = variant(graph)
    g["settings"]["string_number"] = 3.0
    if collision:
        g["strings"][0]["circuit_tag"] = "S3"
    validate_graph(g)
    before = sha(g)
    result = builtin().add_midpoint_string(g, request())
    assert result["circuit_tag"] == result["graph"]["strings"][-1]["circuit_tag"] == tag
    assert result["graph"]["settings"]["string_number"] == counter
    assert type(result["graph"]["settings"]["string_number"]) is int
    assert sha(g) == before


@pytest.mark.parametrize("limit", [6, 5])
def test_string_midpoint_length_limit_boundary(graph, limit):
    g = variant(graph)
    g["settings"]["panels_in_sequence"] = limit
    validate_graph(g)
    before = sha(g)
    module = builtin()
    if limit == 6:
        result = module.add_midpoint_string(g, request())
        assert result["ordered_panel_refs"] == [P(0, c) for c in range(6)]
        assert result["graph"]["strings"][-1]["module_count"] == limit
    else:
        with pytest.raises(GraphValidationError) as error:
            module.add_midpoint_string(g, request())
        assert error.value.code == "STRING_TOO_LONG"
    assert sha(g) == before


def test_string_midpoint_counter_exhaustion_is_atomic(graph):
    module = builtin()
    g = variant(graph)
    g["settings"]["string_number"] = module.single.MAX_STRING_NUMBER - 1
    g["strings"][0]["circuit_tag"] = f"S{module.single.MAX_STRING_NUMBER - 1}"
    validate_graph(g)
    before = sha(g)
    with pytest.raises(GraphValidationError) as error:
        module.add_midpoint_string(g, request())
    assert error.value.code == "STRING_NUMBER_EXHAUSTED"
    assert sha(g) == before


def test_string_midpoint_run_checks_outer_json_bound(graph):
    g = variant(graph)
    before = sha(g)
    params = []
    for _ in range(34):
        params = [params]
    original = sha(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, params)
    assert error.value.code == "GRAPH_LIMIT_EXCEEDED"
    assert sha(g) == before and sha(params) == original


def test_string_midpoint_kernel_refusal_is_mapped(graph, monkeypatch):
    module = builtin()

    def refuse(*args):
        raise kernel.BatchTwoError("a panel has no centroid")

    monkeypatch.setattr(module.kernel, "string_midpoint_rows", refuse)
    with pytest.raises(GraphValidationError) as error:
        module.add_midpoint_string(variant(graph), request())
    assert error.value.code == "STRING_MIDPOINT_MAPPING_FAILED"


VALID = dict(request(), operation="add-midpoint-string")


@pytest.mark.parametrize("patch", [
    {"operation": "other"}, {"operation": ["add-midpoint-string"]}, {"operation": 7},
    {"operation": "add-string"}, {"operation": DROP}, {"unknown": 1}, {"drawing_id": "solar"},
    {"expected_rev": True}, {"expected_rev": "0"}, {"expected_rev": -1}, {"expected_rev": 2147483648},
    {"expected_rev": 0.0}, {"expected_rev": DROP}, {"start_panel_ref": DROP}, {"end_panel_ref": DROP},
    {"start_panel_ref": ""}, {"start_panel_ref": "x" * 129}, {"end_panel_ref": 7}, {"end_panel_ref": None},
    {"start_panel_ref": [P(0, 0)]},
])
def test_string_midpoint_request_shape_fails_closed(graph, patch):
    g = variant(graph)
    before = copy.deepcopy(g)
    params = dict(VALID)
    for key, value in patch.items():
        if value is DROP:
            del params[key]
        else:
            params[key] = value
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, params)
    assert error.value.code == "INVALID_STRING_MIDPOINT_REQUEST"
    assert g == before


def test_string_midpoint_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == expected_declaration(
        TOOL, "stringing", 73, "INVALID_STRING_MIDPOINT_REQUEST", ["frames"],
        ["string-midpoint-connection"], "add-midpoint-string",
        {"start_panel_ref": REF, "end_panel_ref": REF}, ["start_panel_ref", "end_panel_ref"])
    assert "default" not in actual["record"]["params"]["properties"]["expected_rev"]
    tools = solar_tools.local_graph_tools()
    assert TOOL in tools and TOOL not in solar_tools.local_graph_read_tools()
    assert tools.index("solar-string-flip") < tools.index(TOOL) < tools.index("solar-string-swap")
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-commit"
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_string_midpoint_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    assert validator.is_valid(VALID)
    assert validator.is_valid(dict(VALID, drawing_id="solar", expected_rev=2147483647))
    for key in ("expected_rev", "start_panel_ref", "end_panel_ref"):
        missing = dict(VALID)
        del missing[key]
        assert not validator.is_valid(missing), key
    for patch in [{"unknown": 1}, {"operation": "add-string"}, {"expected_rev": -1},
                  {"expected_rev": 2147483648}, {"start_panel_ref": ""}, {"end_panel_ref": "x" * 129},
                  {"drawing_id": "d" * 129}]:
        assert not validator.is_valid(dict(VALID, **patch)), patch


def test_string_midpoint_readiness(graph):
    m = variant(graph)
    assert availability.w1_graph_readiness(m)[TOOL] == {"input_ready": True, "input_reason": None}
    bare = copy.deepcopy(graph)
    bare["frames"] = []
    for panel in bare["panels"]:
        panel["frame_ref"] = None
        panel["matrix_cell"] = None
    assert availability.w1_local_commit_inputs(bare)[TOOL] == {
        "input_ready": False, "input_reason": "frames_required"}
    assert availability.w1_local_commit_inputs(variant(graph, "UNITS"))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# --- the rail ----------------------------------------------------------------------------------------------------


@pytest.fixture
def api_m(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, variant(graph))
    yield from _api(backend, tmp_path, monkeypatch)


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=body(api, TOOL, params))


def pinned(head):
    """The head with the rail's new string id and time replaced by the pinned ones."""
    text = json.dumps(head).replace(head["strings"][-1]["id"], NEW)
    result = json.loads(text)
    result["strings"][-1]["provenance"]["created_at"] = STAMP
    return result


def test_string_midpoint_run_rail_commits_one_job(api_m, graph):
    response = post(api_m, VALID)
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert store.load_manifest(api_m[1], TENANT, "solar")["head"] == 2
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "complete"
    head = head_graph(api_m[1])
    assert head["strings"][-1]["ordered_panel_refs"] == [P(0, c) for c in range(6)]
    assert pinned(head) == builtin().add_midpoint_string(variant(graph), request())["graph"]
    assert sha(pinned(head)) == "db36535c0133c10d19914e8c118c87aa70cafec4432d192c417de02cc83f0e9c"


@pytest.mark.parametrize("patch", [{"end_panel_ref": DROP}, {"unknown": 1}, {"start_panel_ref": ""}])
def test_string_midpoint_broker_refuses_schema_violations(api_m, patch):
    params = dict(VALID)
    for key, value in patch.items():
        if value is DROP:
            del params[key]
        else:
            params[key] = value
    response = post(api_m, params)
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(api_m[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_string_midpoint_builtin_refusal_reaches_the_rail(api_m):
    response = post(api_m, dict(VALID, start_panel_ref=P(0, 2), end_panel_ref=P(1, 2)))
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "MIDPOINT_PATH_TOO_SHORT"
    assert store.load_manifest(api_m[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"


def test_string_midpoint_dispatch_publishes_and_refuses_stale(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, variant(graph))
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, VALID)
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (receipt["before_rev"], receipt["after_rev"], receipt["replayed"]) == (0, 1, False)
        # A fresh string id and time per call: the same job id cannot replay, exactly as solar-string-add.
        with pytest.raises(GraphValidationError, match="JOB_BINDING_REUSED"):
            dispatch(backend, fence, TOOL, VALID)
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(VALID, drawing_id="solar"), drawing_id="solar",
                source_version=2, holder="fixture-owner", fence=fence, job_id="string-midpoint-stale")
    assert latest(backend) == 2
    head = head_graph(backend)
    assert len(head["strings"]) == 3 and head["strings"][-1]["ordered_panel_refs"] == [P(0, c) for c in range(6)]
