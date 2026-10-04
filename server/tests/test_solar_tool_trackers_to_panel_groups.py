"""LEAFTRACKERSTOPANELGROUPS as a Studio tool (sf-w3-conversion-graph-tool): the drawing's current Ground
physical state, carried by the new physical_state trusted input, converted by server/solar_ground_conversion.py
into compact Ground frames that REPLACE any earlier conversion, with the panel-group counter, the binding to the
converted head-log entry and the conversion report published in one commit. The replay proof re-reads the bound,
immutable entry, so a later physical publish never breaks it. Every refusal is on a Studio code. Every expected
value was measured by running the reference with python -B."""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog  # noqa: E402
import deps  # noqa: E402
import entitlements  # noqa: E402
import jobs  # noqa: E402
import product_capability_availability as availability  # noqa: E402
import solar_physical_state as ps  # noqa: E402  (first: its write_loop import puts da/ (store) on sys.path)
import solar_physical_head as ph  # noqa: E402
import solar_ground_conversion as conv  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_solve_results as ssr  # noqa: E402
import solar_local_graph  # noqa: E402
import solar_tools  # noqa: E402
import store  # noqa: E402
import write_loop  # noqa: E402
from solar_design_graph import GraphValidationError, validate_graph  # noqa: E402
from solar_solve_results import finish_mutation  # noqa: E402
from test_solar_ground_graph_codec import _load, ground_base  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_local_graph_adapter import held  # noqa: E402
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: E402,F401
from test_w1_local_graph_rail import _api, body  # noqa: E402
from test_solar_w2_registration import expected_declaration, head_graph, latest  # noqa: E402

TOOL = "solar-trackers-to-panel-groups"
TENANT = "fixture-tenant"
DRAWING = "solar"
INVALID = "INVALID_TRACKER_CONVERSION_REQUEST"
VIEW = {"index": 0, "state": {"artifact_id": "b" * 64, "content_sha256": "d" * 64}}
SOURCE = "c" * 64
CREATED_AT = "2026-09-17T00:00:00Z"
NEW_COPY = {
    "ground_installation_required": "This solar tool works on Ground designs only",
    "ground_conversion_in_use": "Remove the strings, equipment, panels and other panel groups from this Ground design first",
    "ground_physical_state_required": "Lay out the Ground trackers first, then convert them",
    "ground_tracker_rows_required": "Add tracker rows to the Ground layout first",
    "ground_units_mismatch": "The Ground layout and the solar design use different drawing units",
    "ground_layout_invalid": "The Ground layout cannot be converted into panel groups",
    "ground_layout_too_large": "The Ground layout holds more panel slots, in total or on one tracker, than a design can carry",
}
SMALL_SHA = "1b8bd39b3f8055b1236653ebde34b8fb73bf42b805b4d30e2b8896225f9847d8"
SMALL_FRAMES_SHA = "31a4054097ed0b35d7824cd469e3d0fea244d478c0b3944803c6774c3543ba89"
REPLACED_SHA = "dbfee3f244b86b3e457612a6dd26ee26c8ae18b4508abb9c70ee26e3925888a8"
B18_FRAMES_SHA = "20f20eb89579183232ad1292deb7879e226ba1f872e00565aad6a39e5ac6f6de"
B18_SHA = "ca0b0a1e63fd7ca07f063b8ec3dc90f88a1af5e835e94961c3a7b1a1617777dd"


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def drawn(ax, ay, bx, by, slots, *, width=2.0, row_index=1, command="LEAFTRACK", block="LEAFSAT", tilt=60.0):
    return {"block": block, "source_command": command, "tracker_model": "single_axis_tracker",
            "axis_start": [ax, ay], "axis_end": [bx, by], "slots": slots, "row_index": row_index,
            "cross_axis_width_du": width, "max_tilt_deg": tilt, "rail_overhang_m": 0.05}


SMALL_ROWS = [drawn(0.0, 0.0, 0.0, 6.0, 3),
              drawn(4.0, 0.0, 4.0, 10.0, 2, width=1.0, row_index=2, command="LEAFSAT")]
ONE_ROW = [drawn(10.0, 0.0, 10.0, 8.0, 4, row_index=3)]


def small_doc(rows=None, units="m", **state):
    value = {"frames": [], "tracker_rows": copy.deepcopy(SMALL_ROWS if rows is None else rows),
             "settings": {"TrackerModulePmaxW": 450.0}}
    value.update(state)
    return ps.physical_document(value, drawing_units=units, source_sha256=SOURCE,
                                capability="trackers-to-panelgroups")


def state(doc=None, view=VIEW):
    return {"view": copy.deepcopy(view), "document": small_doc() if doc is None else doc}


def request(rev=0):
    return {"expected_rev": rev}


def convert(g, doc=None, view=VIEW, params=None):
    physical = state(doc, view)
    snapshot = copy.deepcopy((g, physical, params))
    out = builtin().run(g, request(g["rev"]) if params is None else params, physical_state=physical)
    assert (g, physical, params) == snapshot
    return out


def refused(g, physical, params=None):
    with pytest.raises(GraphValidationError) as error:
        builtin().run(copy.deepcopy(g), request(g["rev"]) if params is None else params, physical_state=physical)
    return error.value.code, error.value.path


def tool_provenance(rev):
    return {"created_by": TOOL, "created_at": CREATED_AT, "last_writer": TOOL, "source_rev": rev, "tool_id": TOOL}


@pytest.fixture(scope="module")
def b18_document():
    evidence = _load("solar_ground_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py")
    raw = (ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json").read_bytes()
    value = evidence.b18_state(json.loads(raw.decode("utf-8")))
    return ps.physical_document(value, drawing_units="m", source_sha256=hashlib.sha256(raw).hexdigest(),
                                capability="trackers-to-panelgroups")


# ------------------------------------------------------------------ constants and codes --

def test_trackers_conversion_constants():
    b = builtin()
    assert (b.TOOL, b.INVALID, b.MAX_COUNTER, b.REPORT_SCHEMA) == (
        TOOL, INVALID, 1_000_000, "leaf.solar-ground-conversion.v1")
    assert b.REQUEST_KEYS == frozenset({"expected_rev"})
    assert set(b.CODE_MAP) == set(conv.CODES)
    assert solar_tools.TRUSTED_INPUTS == ("source_intake", "proposal_candidate", "solaredge_report", "physical_state", "pvcase_source")
    assert set(solar_local_graph._TRUSTED_RESOLVERS) == set(solar_tools.TRUSTED_INPUTS)
    assert solar_local_graph._TRUSTED_RESOLVERS["physical_state"] is solar_local_graph._physical_state
    assert solar_local_graph.PHYSICAL_SOURCE_KEYS == frozenset({"head_index", "state_artifact_id",
                                                                 "state_content_sha256"})


def test_trackers_conversion_codes_are_studio_codes():
    rail = (ROOT / "web" / "src" / "lib" / "ribbonClusters.js").read_text(encoding="utf-8")
    table = rail[rail.index("export const SOLAR_REFUSAL_REASONS"):]
    table = table[:table.index("})")]
    keys = set(re.findall(r"^\s+([a-z][a-z0-9_]*):", table, re.M))
    for code, sentence in NEW_COPY.items():
        assert f"  {code}: '{sentence}'," in table, code
    b = builtin()
    mapped = {code.lower() for code in b.CODE_MAP.values()}
    assert mapped == {"ground_physical_state_required", "ground_tracker_rows_required", "ground_installation_required",
                      "ground_units_mismatch", "ground_layout_invalid", "ground_layout_too_large"}
    assert mapped <= keys and all(code == code.upper() for code in b.CODE_MAP.values())
    text = (SERVER / solar_tools.get(TOOL)["builtin"]).read_text(encoding="utf-8")
    reasons = set(re.findall(r'"input_reason":\s*"([a-z][a-z0-9_]{0,63})"', text))
    assert reasons == {"ground_installation_required", "ground_conversion_in_use"} and reasons <= keys
    assert ".lower()" not in text
    literals = set(re.findall(r'_refuse\(\s*"([A-Z][A-Z0-9_]*)"', text))
    assert literals == {"GROUND_PHYSICAL_STATE_REQUIRED", "GROUND_LAYOUT_INVALID"}
    assert {code.lower() for code in literals} <= keys
    overlay = (SERVER / "product_capability_availability.py").read_text(encoding="utf-8")
    assert '"input_reason": "ground_physical_state_required"' in overlay


# ------------------------------------------------------------------ the conversion --

def test_trackers_conversion_small_converts_and_persists(graph):
    g = ground_base(graph)
    out = convert(g)
    assert sha(out) == SMALL_SHA and sha(out["frames"]) == SMALL_FRAMES_SHA
    assert (out["rev"], out["parent_rev"]) == (1, 0)
    assert [key for key in g if g[key] != out[key]] == ["rev", "parent_rev", "settings", "frames", "extra"]
    assert [f["name"] for f in out["frames"]] == ["Group 1", "Group 2"]
    assert [f["module_slots"] for f in out["frames"]] == [3, 2]
    assert all(f["rev"] == 1 and f["provenance"] == tool_provenance(0) for f in out["frames"])
    assert all(f["ground_slots"]["panel"]["rev"] == 1 and f["ground_slots"]["panel"]["provenance"]
               == tool_provenance(0) for f in out["frames"])
    assert out["settings"]["panel_group_number"] == 3 and out["settings"]["rev"] == 1
    assert out["settings"]["provenance"]["last_writer"] == TOOL
    assert out["extra"]["physical_state"] == {"head_index": 0, "state_artifact_id": "b" * 64,
                                              "state_content_sha256": "d" * 64}
    assert out["extra"]["ground_conversion"] == {
        "schema": "leaf.solar-ground-conversion.v1", "counts": {"trackers": 2, "slots": 5},
        "overlap": {"distinct_centres": 5, "centres_in_own_outline": 5, "centres_in_two_outlines": 0,
                    "centres_in_two_outlines_same_source_command": 0, "centres_in_more_than_two_outlines": 0},
        "panel_group_colour": 2}
    assert len(out["extra"]["solve_coverage"]["unassigned_panel_refs"]) == 5
    assert validate_graph(copy.deepcopy(out)) == out


def test_trackers_conversion_equals_the_kernel(graph):
    g = ground_base(graph)
    out = convert(g)
    result = conv.convert_physical_state(VIEW, small_doc(), g, provenance=tool_provenance(0), rev=1)
    assert out["frames"] == result["frames"]
    expected = copy.deepcopy(g)
    expected["frames"] = result["frames"]
    expected["settings"]["panel_group_number"] = result["settings"]["PanelGroupNumber"]
    expected["extra"]["physical_state"] = dict(result["source"])
    expected["extra"]["ground_conversion"] = {"schema": conv.RESULT_SCHEMA, "counts": result["counts"],
                                              "overlap": result["overlap"], "panel_group_colour": 2}
    assert finish_mutation(g, expected, TOOL) == out


def test_trackers_conversion_replaces_never_appends(graph):
    first = convert(ground_base(graph))
    view = {"index": 1, "state": {"artifact_id": "e" * 64, "content_sha256": "f" * 64}}
    second = convert(first, small_doc(ONE_ROW), view)
    assert sha(second) == REPLACED_SHA
    assert (second["rev"], second["parent_rev"]) == (2, 1)
    assert len(second["frames"]) == 1 and second["frames"][0]["id"] == first["frames"][0]["id"]
    assert second["frames"][0]["module_slots"] == 4 and second["frames"][0]["name"] == "Group 1"
    assert second["settings"]["panel_group_number"] == 2
    assert second["extra"]["physical_state"]["head_index"] == 1
    assert second["extra"]["ground_conversion"]["counts"] == {"trackers": 1, "slots": 4}
    again = convert(second, small_doc(ONE_ROW), view)
    assert [f["id"] for f in again["frames"]] == [f["id"] for f in second["frames"]]
    assert again["rev"] == 3 and again["frames"][0]["provenance"] == tool_provenance(2)


def test_trackers_conversion_counter_bound(graph):
    g = ground_base(graph)
    out = convert(g, small_doc(settings={"TrackerModulePmaxW": 450.0, "PanelGroupNumber": 999_998}))
    assert out["settings"]["panel_group_number"] == 1_000_000
    assert [f["name"] for f in out["frames"]] == ["Group 999998", "Group 999999"]
    over = state(small_doc(settings={"TrackerModulePmaxW": 450.0, "PanelGroupNumber": 999_999}))
    assert refused(g, over) == ("GROUND_LAYOUT_INVALID", "GROUND_CONVERSION_INPUT_INVALID")
    # The colour counter has no graph field, so the builtin bounds it itself.
    out = convert(g, small_doc(settings={"TrackerModulePmaxW": 450.0, "PanelGroupColour": 999_998}))
    assert out["extra"]["ground_conversion"]["panel_group_colour"] == 1_000_000
    assert out["settings"]["panel_group_number"] == 3
    over = state(small_doc(settings={"TrackerModulePmaxW": 450.0, "PanelGroupColour": 999_999}))
    assert refused(g, over) == ("GROUND_LAYOUT_INVALID", "GROUND_CONVERSION_INPUT_INVALID")


def test_trackers_conversion_b18_site(graph, b18_document):
    g = ground_base(graph)
    out = convert(g, b18_document)
    assert len(out["frames"]) == 237 and sum(f["module_slots"] for f in out["frames"]) == 69_678
    assert out["settings"]["panel_group_number"] == 238
    assert out["extra"]["ground_conversion"]["panel_group_colour"] == 237
    assert out["extra"]["ground_conversion"]["overlap"] == {
        "distinct_centres": 69678, "centres_in_own_outline": 69678, "centres_in_two_outlines": 25284,
        "centres_in_two_outlines_same_source_command": 0, "centres_in_more_than_two_outlines": 0}
    assert len(out["extra"]["solve_coverage"]["unassigned_panel_refs"]) == 69_678
    assert sha(out["frames"]) == B18_FRAMES_SHA and sha(out) == B18_SHA


# ------------------------------------------------------------------ readiness and refusals --

def _converted(change):
    """A valid design holding the small conversion plus `change` (each a design the replacement would break)."""
    def build(w1):
        g = convert(ground_base(w1))
        change(g, w1, ssr.slot_panel_ids(g))
        return g
    return build


def _strung(g, w1, slots):
    string = copy.deepcopy(w1["strings"][0])
    string.update(ordered_panel_refs=list(slots[:2]), module_count=2, inverter_ref=None)
    g["strings"] = [string]


def _zoned(g, w1, slots):
    zone = copy.deepcopy(w1["electrical_zones"][0])
    zone["panel_refs"] = list(slots[:2])
    g["electrical_zones"] = [zone]


def _inverter(g, w1, slots):
    inverter = copy.deepcopy(w1["inverters"][0])
    inverter["input_assignments"] = []
    g["inverters"] = [inverter]


READINESS = [
    ("ground", lambda w1: ground_base(w1), None),
    ("converted", lambda w1: convert(ground_base(w1)), None),
    ("roof", lambda w1: copy.deepcopy(w1), "ground_installation_required"),
    ("strung", _converted(_strung), "ground_conversion_in_use"),
    ("zoned", _converted(_zoned), "ground_conversion_in_use"),
    ("inverter", _converted(_inverter), "ground_conversion_in_use"),
    ("expanded", lambda w1: codec.expand_graph(convert(ground_base(w1))), "ground_conversion_in_use"),
]


@pytest.mark.parametrize("name,make,reason", READINESS, ids=[row[0] for row in READINESS])
def test_trackers_conversion_readiness(graph, name, make, reason):
    g = make(graph)
    assert validate_graph(copy.deepcopy(g)) == g
    expected = {"input_ready": reason is None, "input_reason": reason}
    assert builtin().input_readiness(g) == expected
    assert availability.w1_local_commit_inputs(g)[TOOL] == expected
    if reason is not None:
        assert refused(g, state()) == (reason.upper(), "<root>")


def test_trackers_conversion_readiness_refuses_a_frame_without_slots(graph):
    """Defence in depth: the hook itself refuses a frame it could not replace, whatever else the graph holds."""
    g = convert(ground_base(graph))
    del g["frames"][1]["ground_slots"]
    assert builtin().input_readiness(g) == {"input_ready": False, "input_reason": "ground_conversion_in_use"}


def test_trackers_conversion_readiness_refuses_an_empty_foreign_frame(graph):
    g = convert(ground_base(graph))
    g["frames"] = g["frames"][:1]
    frame = g["frames"][0]
    del frame["ground_slots"]
    g["extra"] = {}
    frame["matrix"] = [[{
        "code": "", "panel_ref": None, "seq": None, "inverter_id": None,
        "string_input_number": None, "x": 0.0, "y": 0.0, "angle": 0.0,
    } for _ in range(frame["module_columns"])] for _ in range(frame["module_rows"])]
    assert validate_graph(copy.deepcopy(g)) == g
    assert builtin().input_readiness(g) == {"input_ready": False, "input_reason": "ground_conversion_in_use"}


def test_trackers_conversion_unresolved_units_are_the_rails(graph):
    g = ground_base(graph)
    g["project"]["units"]["meters_per_unit"] = 2.0
    assert availability.w1_local_commit_inputs(g)[TOOL] == {"input_ready": False, "input_reason": "unresolved_units"}
    assert refused(g, state())[0] == "UNRESOLVED_UNITS"


@pytest.mark.parametrize("params", [None, [], {}, {"expected_rev": 0, "cancel": True}, {"expected_rev": "0"},
                                    {"expected_rev": True}, {"expected_rev": 0.0}, {"expected_rev": 0, "drawing_id": "x"}],
                         ids=["none", "list", "empty", "extra-key", "string", "bool", "float", "drawing-id"])
def test_trackers_conversion_request_shape_fails_closed(graph, params):
    snapshot = copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(ground_base(graph), params, physical_state=state())
    assert error.value.code == INVALID and params == snapshot


def test_trackers_conversion_request_rails(graph):
    with pytest.raises(GraphValidationError) as error:
        builtin().run(ground_base(graph), {"expected_rev": float("nan")}, physical_state=state())
    assert error.value.code == "NONFINITE_NUMBER"
    assert refused(ground_base(graph), state(), request(1))[0] == "STALE_GRAPH_REVISION"


@pytest.mark.parametrize("physical,path", [
    (None, "physical_state"), ({}, "physical_state"), ({"view": None}, "physical_state"),
    ({"view": None, "document": None, "extra": 1}, "physical_state"),
    ({"view": None, "document": None}, "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED"),
], ids=["none", "empty", "no-document", "extra-key", "no-head"])
def test_trackers_conversion_physical_state_required(graph, physical, path):
    assert refused(ground_base(graph), physical) == ("GROUND_PHYSICAL_STATE_REQUIRED", path)


def _too_large():
    return small_doc([drawn(0.0, 30.0 * k, 0.0, 30.0 * k + 1.0, 10_000, row_index=k) for k in range(11)])


KERNEL_REFUSALS = [
    ("no-rows", lambda: small_doc([]), "GROUND_TRACKER_ROWS_REQUIRED", "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED"),
    ("feet-document", lambda: small_doc(units="ft"), "GROUND_UNITS_MISMATCH", "GROUND_CONVERSION_UNITS_MISMATCH"),
    ("zero-width", lambda: small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, width=0.0)]), "GROUND_LAYOUT_INVALID",
     "GROUND_CONVERSION_INPUT_INVALID"),
    ("too-large", _too_large, "GROUND_LAYOUT_TOO_LARGE", "GROUND_CONVERSION_LIMIT_EXCEEDED"),
]


@pytest.mark.parametrize("name,doc,code,path", KERNEL_REFUSALS, ids=[row[0] for row in KERNEL_REFUSALS])
def test_trackers_conversion_kernel_refusals_map_to_studio_codes(graph, name, doc, code, path):
    assert refused(ground_base(graph), state(doc())) == (code, path)
    assert builtin().CODE_MAP[path] == code


# ------------------------------------------------------------------ registry --

def test_trackers_conversion_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    expected = expected_declaration(TOOL, "stringing", 5, INVALID, [], ["trackers-to-panelgroups"], None, {}, [])
    expected.update(readiness={"kind": "hook"}, trusted_inputs=["physical_state"], wave=3,
                    scenario="w3-ground-electrical")
    assert actual == expected
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
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


def test_trackers_conversion_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    assert validator.is_valid(request()) and validator.is_valid({"expected_rev": 2147483647, "drawing_id": "d" * 128})
    for value in [{}, {"expected_rev": -1}, {"expected_rev": 2147483648}, {"expected_rev": "0"},
                  {"expected_rev": 0, "cancel": True}, {"expected_rev": 0, "drawing_id": "d" * 129}]:
        assert not validator.is_valid(value), value


@pytest.mark.parametrize("change,message", [
    ({"trusted_inputs": ["physical_state", "source_intake"]},
     "physical_state requires a non-seed local graph commit and no other trusted input"),
    ({"seedable": True}, "physical_state requires a non-seed local graph commit and no other trusted input"),
], ids=["with-source-intake", "seedable"])
def test_trackers_conversion_loader_rule(tmp_path, change, message):
    directory = tmp_path / "solar_tools"
    directory.mkdir()
    (tmp_path / "builtins").mkdir()
    for path in (SERVER / "solar_tools").glob("*.json"):
        (directory / path.name).write_bytes(path.read_bytes())
        (tmp_path / "builtins" / (path.stem + ".py")).write_text("", encoding="utf-8")
    (tmp_path / "capability_families.json").write_bytes((SERVER / "capability_families.json").read_bytes())
    declaration = solar_tools.get(TOOL)
    declaration.update(change)
    (directory / "solar_trackers_to_panel_groups.json").write_text(json.dumps(declaration), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError, match="^" + message + "$"):
        solar_tools.load(directory=directory, server_dir=tmp_path)


# ------------------------------------------------------------------ the commit rail --

def stored(tmp_path, monkeypatch, g):
    """A drawing whose stored intake carries the graph (real ingest, no stubs)."""
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.delenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", raising=False)
    monkeypatch.setenv("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")
    backend = store.FilesystemBackend(str(tmp_path / "drawings"))
    from solar_sizing_client import digest
    intake = {"dwg": {}, "layers": [], "polylines": [], "inserts": [], "faces3d": [], "blockdefs": [],
              "geodata": None, "solar_design_graph": g, "solar_design_graph_sha256": digest(g)}
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(intake), encoding="utf-8")
    store.ingest_drawing(backend, TENANT, str(path), drawing_id=DRAWING)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def dispatch(backend, params, *, source_version=1, job_id="conversion-job"):
    with held(backend) as fence:
        return solar_local_graph.run_local_graph_commit(
            backend, TENANT, TOOL, dict(params, drawing_id=DRAWING), drawing_id=DRAWING,
            source_version=source_version, holder="fixture-owner", fence=fence, job_id=job_id)


def prove(backend, receipt, params, job_id="conversion-job", source_version=1):
    return solar_local_graph.graph_commit_provenance(
        receipt, dict(params, drawing_id=DRAWING), TENANT, job_id, TOOL, source_version, backend=backend)


def test_trackers_conversion_rail_persists_and_proves(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())["head"]
    receipt = dispatch(backend, request())
    assert receipt["new_version"] == {"drawing_id": DRAWING, "version": 2, "parent": 1}
    assert (receipt["before_rev"], receipt["after_rev"], receipt["drawing_changed"]) == (0, 1, True)
    out = head_graph(backend)
    assert out == convert(g, small_doc(), head) and latest(backend) == 2
    assert out["extra"]["physical_state"] == solar_local_graph.physical_state_source(head)
    proof = prove(backend, receipt, request())
    assert (proof["execution_mode"], proof["new_version"]) == ("local_graph_commit", 2)
    # A later physical publish moves the head; the proof still re-reads the entry the commit names.
    moved = ph.publish_physical_state(backend, TENANT, DRAWING, ps.physical_document(
        {"frames": [], "tracker_rows": copy.deepcopy(ONE_ROW), "settings": {"TrackerModulePmaxW": 450.0}},
        drawing_units="m", source_sha256=SOURCE, capability="trackers-to-panelgroups",
        parent=head["state"]["artifact_id"]))["head"]
    assert moved["index"] == 1
    assert prove(backend, receipt, request())["new_version"] == 2
    second = dispatch(backend, request(1), source_version=2, job_id="conversion-again")
    replaced = head_graph(backend)
    assert second["new_version"]["version"] == 3 and replaced["extra"]["physical_state"]["head_index"] == 1
    assert [f["module_slots"] for f in replaced["frames"]] == [4]
    assert prove(backend, second, request(1), "conversion-again", 2)["new_version"] == 3
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request(0), source_version=3, job_id="conversion-stale")
    assert error.value.code == "STALE_GRAPH_REVISION" and latest(backend) == 3


def test_trackers_conversion_proof_rejects_another_entry(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())["head"]
    receipt = dispatch(backend, request())
    project = g["project"]["id"]
    source = solar_local_graph.physical_state_source(head)
    found = solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, source)
    assert found["view"] == {"index": 0, "state": {"artifact_id": head["state"]["artifact_id"],
                                                   "content_sha256": head["state"]["content_sha256"]}}
    assert found["document"] == small_doc()
    for bad in [dict(source, head_index=1), dict(source, state_content_sha256="0" * 64),
                dict(source, state_artifact_id="0" * 64), dict(source, extra=1), {}]:
        with pytest.raises((ValueError, LookupError)):
            solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, bad)
    with pytest.raises((ValueError, LookupError)):
        solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, "leaf:project:other", source)
    # A real, later state named under the first entry's index: only the entry bytes tell them apart.
    moved = ph.publish_physical_state(backend, TENANT, DRAWING, ps.physical_document(
        {"frames": [], "tracker_rows": copy.deepcopy(ONE_ROW), "settings": {"TrackerModulePmaxW": 450.0}},
        drawing_units="m", source_sha256=SOURCE, capability="trackers-to-panelgroups",
        parent=head["state"]["artifact_id"]))["head"]
    later = solar_local_graph.physical_state_source(moved)
    assert solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, later)["view"]["index"] == 1
    with pytest.raises(ValueError):
        solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, dict(later, head_index=0))
    with pytest.raises(ValueError):
        solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, dict(source, head_index=True))
    # An artifact whose own record disagrees with the entry is refused too.
    real = ps.load_physical_state

    def other_content(*args, **kwargs):
        meta, document = real(*args, **kwargs)
        return dict(meta, content_sha256="0" * 64), document
    monkeypatch.setattr(ps, "load_physical_state", other_content)
    with pytest.raises(ValueError):
        solar_local_graph._physical_state_entry(backend, TENANT, DRAWING, project, source)
    monkeypatch.setattr(ps, "load_physical_state", real)
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        prove(backend, receipt, {"expected_rev": 1})


def test_trackers_conversion_proof_rejects_a_rewritten_head_entry_parent(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())["head"]
    receipt = dispatch(backend, request())
    assert head["index"] == 0
    assert prove(backend, receipt, request())["new_version"] == 2
    backend.put(ph.entry_key(TENANT, DRAWING, 0), ph.entry_bytes(
        0, g["project"]["id"], "0" * 64, head["state"]["artifact_id"], head["state"]["content_sha256"]))
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        prove(backend, receipt, request())


def test_trackers_conversion_proof_rejects_a_broken_chain_at_index_one(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())["head"]
    moved = ph.publish_physical_state(backend, TENANT, DRAWING, ps.physical_document(
        {"frames": [], "tracker_rows": copy.deepcopy(ONE_ROW), "settings": {"TrackerModulePmaxW": 450.0}},
        drawing_units="m", source_sha256=SOURCE, capability="trackers-to-panelgroups",
        parent=head["state"]["artifact_id"]))["head"]
    receipt = dispatch(backend, request())
    assert moved["index"] == 1
    assert head_graph(backend)["extra"]["physical_state"]["head_index"] == 1
    assert prove(backend, receipt, request())["new_version"] == 2
    assert moved["state"]["artifact_id"] != head["state"]["artifact_id"]
    backend.put(ph.entry_key(TENANT, DRAWING, 1), ph.entry_bytes(
        1, g["project"]["id"], moved["state"]["artifact_id"], moved["state"]["artifact_id"],
        moved["state"]["content_sha256"]))
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        prove(backend, receipt, request())


@pytest.mark.parametrize("rewrite", ["wrong-index", "missing-key", "non-canonical"])
def test_trackers_conversion_proof_rejects_a_corrupt_predecessor(graph, tmp_path, monkeypatch, rewrite):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())["head"]
    moved = ph.publish_physical_state(backend, TENANT, DRAWING, ps.physical_document(
        {"frames": [], "tracker_rows": copy.deepcopy(ONE_ROW), "settings": {"TrackerModulePmaxW": 450.0}},
        drawing_units="m", source_sha256=SOURCE, capability="trackers-to-panelgroups",
        parent=head["state"]["artifact_id"]))["head"]
    receipt = dispatch(backend, request())
    assert moved["index"] == 1
    assert head_graph(backend)["extra"]["physical_state"]["head_index"] == 1
    assert prove(backend, receipt, request())["new_version"] == 2
    key = ph.entry_key(TENANT, DRAWING, 0)
    original = json.loads(backend.get(key))
    assert original["state"] == head["state"]["artifact_id"]
    if rewrite == "wrong-index":
        raw = ph.entry_bytes(17, g["project"]["id"], None, original["state"], original["content_sha256"])
    elif rewrite == "missing-key":
        del original["schema"]
        raw = json.dumps(original, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    else:
        raw = json.dumps(original, indent=2, allow_nan=False).encode("utf-8")
    assert json.loads(raw)["state"] == head["state"]["artifact_id"]
    backend.put(key, raw)
    with pytest.raises(ValueError, match="graph commit terminal proof rejected"):
        prove(backend, receipt, request())


def test_trackers_conversion_rail_refusals_are_atomic(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request())
    assert (error.value.code, error.value.path) == ("GROUND_PHYSICAL_STATE_REQUIRED",
                                                    "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED")
    assert head_graph(backend) == g and latest(backend) == 1
    backend.put(ph.entry_key(TENANT, DRAWING, 0), b"{}")
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request(), job_id="conversion-corrupt")
    assert error.value.code == "PHYSICAL_STATE_UNAVAILABLE"
    assert head_graph(backend) == g and latest(backend) == 1


def test_trackers_conversion_rail_refuses_an_unbound_result(graph, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    module = builtin()
    original = module.run

    def unbound(*args, **kwargs):
        out = original(*args, **kwargs)
        out["extra"]["physical_state"] = dict(out["extra"]["physical_state"], head_index=7)
        return out
    monkeypatch.setattr(module, "run", unbound)
    with pytest.raises(GraphValidationError) as error:
        dispatch(backend, request())
    assert error.value.code == "PHYSICAL_STATE_UNBOUND"
    assert head_graph(backend) == g and latest(backend) == 1


def test_trackers_conversion_availability_reads_the_head(graph, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, ground_base(graph))
    readiness = availability.w1_input_readiness(TENANT, DRAWING)
    assert readiness[TOOL] == {"input_ready": False, "input_reason": "ground_physical_state_required"}
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {"input_ready": True, "input_reason": None}

    def broken(key):
        raise OSError("store down")
    monkeypatch.setattr(backend, "exists", broken)
    unchanged = {"x": {"input_ready": True, "input_reason": None}}
    assert availability.physical_state_readiness(dict(unchanged), backend, TENANT, DRAWING) == unchanged
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {"input_ready": True, "input_reason": None}


def test_trackers_conversion_availability_keeps_graph_reasons(graph, tmp_path, monkeypatch):
    stored(tmp_path, monkeypatch, copy.deepcopy(graph))
    assert availability.w1_input_readiness(TENANT, DRAWING)[TOOL] == {
        "input_ready": False, "input_reason": "ground_installation_required"}


def test_trackers_conversion_b18_through_the_rail(graph, b18_document, tmp_path, monkeypatch):
    g = ground_base(graph)
    backend = stored(tmp_path, monkeypatch, g)
    head = ph.publish_physical_state(backend, TENANT, DRAWING, b18_document)["head"]
    receipt = dispatch(backend, request())
    out = head_graph(backend)
    assert len(out["frames"]) == 237 and out["settings"]["panel_group_number"] == 238
    assert out["extra"]["physical_state"] == solar_local_graph.physical_state_source(head)
    assert sha(out["frames"]) == B18_FRAMES_SHA
    assert prove(backend, receipt, request())["new_version"] == 2


@pytest.fixture
def api_ground(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, ground_base(graph))
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def api_no_head(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, ground_base(graph))
    yield from _api(backend, tmp_path, monkeypatch)


@pytest.fixture
def api_no_rows(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend = stored(tmp_path, monkeypatch, ground_base(graph))
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc([]))
    yield from _api(backend, tmp_path, monkeypatch)


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=body(api, TOOL, params))


def jobs_now():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def test_trackers_conversion_run_rail_commits_one_job(api_ground):
    response = post(api_ground, request())
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": DRAWING, "version": 2, "parent": 1}
    records = jobs_now()
    assert len(records) == 1 and records[0]["status"] == "complete"
    out = head_graph(api_ground[1])
    assert [f["module_slots"] for f in out["frames"]] == [3, 2] and out["settings"]["panel_group_number"] == 3


def test_trackers_conversion_readiness_refuses_before_a_job(api_no_head):
    response = post(api_no_head, request())
    assert response.status_code == 409, response.text
    env = response.json()
    assert env.get("ok") is not True and env["reason_code"] == "ground_physical_state_required"
    assert env["availability"]["refusal_reasons"] == ["ground_physical_state_required"]
    assert store.load_manifest(api_no_head[1], TENANT, DRAWING)["head"] == 1 and jobs_now() == []


def test_trackers_conversion_builtin_refusal_reaches_the_rail(api_no_rows):
    response = post(api_no_rows, request())
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "GROUND_TRACKER_ROWS_REQUIRED"
    assert store.load_manifest(api_no_rows[1], TENANT, DRAWING)["head"] == 1
    records = jobs_now()
    assert len(records) == 1 and records[0]["status"] == "failed"
