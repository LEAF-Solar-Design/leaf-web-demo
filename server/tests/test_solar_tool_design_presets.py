"""Design presets (LEAFPROFILE) as registry tools over the drawing-owned store in graph extra.

Covered: both declarations and their parameter bounds; the consult's case C7 (create preset Alpha
gives active prefix A and version 1); the recorded parity chain f1 to f5 replayed through the two
tools' own paths, row for row against the committed receipts; that a preset commit changes no
entity and no output digest; every named refusal in its fixed order; a malformed stored store;
the store and preset limits and the plugin's P-prefix collision; purity; readiness; the registry
and catalog; the commit and read through the API, jobs and broker; a tampered read receipt.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_local_read
import solar_sizing_client
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, entities
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest, require_sizing, sizing_basis
from solar_solve_results import upstream_basis
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_sizing_groups import confirm, passing, service, sizing_params  # noqa: F401
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
TENANT = "fixture-tenant"
TOOL = "solar-design-presets"
LIST = "solar-design-presets-list"
SNAPSHOT = ROOT / "docs/parity/evidence/ground/generate/profile-settings.json"
RECEIPTS = ROOT / "docs/parity/receipts/design-profile-manage"
STEPS = ("f1", "f2", "f3", "f4", "f5")
STEP_PARAMS = {"f1": {"subcommand": "Create", "name": "Alpha"}, "f2": {"subcommand": "Create", "name": "Beta"},
               "f3": {"subcommand": "Swap", "prefix": "A"}, "f4": None,
               "f5": {"subcommand": "Delete", "prefix": "B"}}
# (graph rev, graph sha256, store sha256, list output bytes, list output sha256) after each step,
# measured with python -B on 9ca48750 from the untouched fixture graph (f4 is the read; no commit).
STEP_DIGESTS = {
    "f1": (1, "768501192dfdefd5f7faa50b2d93760aee1f62e0d6e805b7bb2affa5878961ad",
           "91f98d47e719f4db929471228ee6bf1a03abfcb34e70534c44abcd8fdb958be8",
           3694, "cc28171c8f6b097be6e5def3b1a7beec21ff787862a01031cd3ffc89f86daca6"),
    "f2": (2, "4d62a177e825c53729650c982bffef3ed10497cad063bb0dd390589d28da48f8",
           "fd5dffb7e157fb1a1da860d63ca9e0efee6ba262962bd794dabd90d7c6ced791",
           5545, "7881da1cdb0087e205727f0dc45bc954f86214e12765328e3b1ab7b83885ddf6"),
    "f3": (3, "44c204f2290556b05c9d9b35f1add927ccac596801cfd4e042ead48ce62fc1a2",
           "b51ab618f294153e1b5bf703e4bd53606694400f788834721a02445ea8695b88",
           5545, "279101e78d1b50e351bbdd5ace9ec1f6bcd4f8303f6fcbced7cf823c057568e1"),
    "f4": (3, "44c204f2290556b05c9d9b35f1add927ccac596801cfd4e042ead48ce62fc1a2",
           "b51ab618f294153e1b5bf703e4bd53606694400f788834721a02445ea8695b88",
           5545, "279101e78d1b50e351bbdd5ace9ec1f6bcd4f8303f6fcbced7cf823c057568e1"),
    "f5": (4, "2d0fa9f0e75262372aa41bcf5553ca5881feda490b0888c43640e9409fa1977d",
           "91f98d47e719f4db929471228ee6bf1a03abfcb34e70534c44abcd8fdb958be8",
           3694, "cc28171c8f6b097be6e5def3b1a7beec21ff787862a01031cd3ffc89f86daca6"),
}
EMPTY_LIST = (464, "8986f711ae6ba864fae9fdced7fa3da0d9a4a0c431aeb879a456c1f20a44ba96")
KIND_COUNTS = {"s": 31, "b": 7, "i": 9, "f": 13}
STOCKED_64_STORE_BYTES = 112978
STOCKED_64_LIST = (120404, "ab0d3df7f75cefefe5275805bad0821b5e52ecdb4799b621263d0684c7b490a6")
WORST_CREATES = 14
WORST_STORE_BYTES = 261785
WORST_LIST_BYTES = 485841
LIST_REQUEST_SHA256 = "4ba9a006e7eccb1ab0ca8957278f6f176a2bef77ee0acac51b70a5990a2478bf"


def commit_builtin():
    return solar_local_graph._load_builtin(TOOL)


def list_builtin():
    return solar_local_read._load_builtin(LIST)


def kernel():
    """The kernel module the store and the builtin share (never a second import of it)."""
    return commit_builtin().preset_store.profiles


def snapshot():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def current():
    return kernel().current_settings_from_preset(snapshot())


def receipt_rows(step, side="plugin"):
    doc = json.loads((RECEIPTS / f"ground-generate-{step}.json").read_text(encoding="utf-8"))
    return doc["comparison"][side]["after"]["rows"]


def commit(value, params):
    return commit_builtin().run(copy.deepcopy(value), dict(params, expected_rev=value["rev"]))


def listed(value):
    return list_builtin().run(copy.deepcopy(value), {})


def chain(value, upto):
    """Replay f1 .. upto from `value` (f1 carries the snapshot's current settings)."""
    for step in STEPS[:STEPS.index(upto) + 1]:
        params = STEP_PARAMS[step]
        if params is None:
            continue
        if step == "f1":
            params = dict(params, current_settings=current())
        value = commit(value, params)
    return value


def create(value, name, **extra):
    return commit(value, dict({"subcommand": "Create", "name": name}, **extra))


def stocked(value, count):
    """`value` with a store of `count` presets named n0.. over the snapshot's settings, written
    through the kernel and the store module directly (the Create path is proven elsewhere)."""
    value = copy.deepcopy(value)
    builtin = commit_builtin()
    manager, settings = kernel().DesignProfileManager(), current()
    for index in range(count):
        manager.create_profile(f"n{index}", False, settings, created_utc=builtin._UNRECORDED)
    builtin.preset_store.save(value, settings, manager)
    return value


def located(value):
    value["project"].update(zip_code="44224", latitude=41.0, longitude=-81.4)
    return value


def refusal(value, params):
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(copy.deepcopy(value), params)
    return caught.value.code


# ---------------------------------------------------------------- declarations --

def settings_schema():
    specs = {"s": {"type": "string", "maxLength": 256}, "b": {"type": "boolean"},
             "i": {"type": "integer", "minimum": 0, "maximum": 1000000},
             "f": {"type": "number", "minimum": -1000000, "maximum": 1000000}}
    fields = [(name, kind) for name, kind in kernel().PRESET_FIELDS if name not in ("Name", "IsBuiltIn")]
    return {"type": "object", "properties": {name: dict(specs[kind]) for name, kind in fields},
            "required": [name for name, _ in fields], "additionalProperties": False}


COMMIT_PARAMS = {
    "type": "object",
    "properties": {
        "drawing_id": {"type": "string", "maxLength": 128},
        "expected_rev": {"type": "integer", "minimum": 0, "maximum": 2147483647},
        "subcommand": {"type": "string", "enum": ["Create", "Swap", "Delete"]},
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "prefix": {"type": "string", "minLength": 1, "maxLength": 3},
        "current_settings": None,
    },
    "required": ["expected_rev", "subcommand"], "additionalProperties": False,
}
LIST_PARAMS = {"type": "object", "properties": {"drawing_id": {"type": "string", "maxLength": 128}},
               "required": [], "additionalProperties": False}
DECLARATIONS = {
    TOOL: ("local-graph-commit", 12, "INVALID_DESIGN_PRESET_REQUEST",
           "Create, swap or delete the drawing's design presets (LEAFPROFILE); each preset keeps a "
           "copy of the drawing's 60 preset settings.", "drawing.write"),
    LIST: ("local-graph-read", 14, "INVALID_DESIGN_PRESET_LIST_REQUEST",
           "List the drawing's design presets (LEAFPROFILE List): each preset's name, prefix and "
           "settings, and which one is active.", "drawing.read"),
}


def declaration(name):
    adapter, order, code, description, capability = DECLARATIONS[name]
    stem = name.replace("-", "_")
    params = copy.deepcopy(COMMIT_PARAMS if name == TOOL else LIST_PARAMS)
    if name == TOOL:
        params["properties"]["current_settings"] = settings_schema()
    record = {"name": name, "version": "1.0.0", "description": description, "kind": "script",
              "family_id": "settings", "engine_op": stem, "entry": f"builtins/{stem}.py",
              "params": params, "returns": {"type": "object"}, "capabilities": [capability],
              "allow_local_fallback": False}
    return {"schema": "leaf.solar-tool.v1", "name": name, "builtin": f"builtins/{stem}.py",
            "family": "settings", "adapter": adapter,
            "entitlement": "run_write" if capability == "drawing.write" else "run_read",
            "requires_persisted_graph": True, "seedable": False, "invalid_request_code": code,
            "readiness": {"kind": "facets", "facets": []}, "engine": "server-builtin",
            "interaction": {"mode": "form"}, "record_store": "registry", "record": record,
            "ledger": ["design-profile-manage"], "trusted_inputs": [], "maturity": "preview",
            "wave": 2, "order": order, "scenario": "w2-rooftop"}


@pytest.mark.parametrize("name", [TOOL, LIST])
def test_design_presets_declarations(name):
    expected = declaration(name)
    assert solar_tools.get(name) == expected
    assert solar_tools.load().get(name) == expected
    assert solar_tools.trusted_record(name) == expected["record"]
    path = SERVER / "solar_tools" / (name.replace("-", "_") + ".json")
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert name not in {row["name"] for row in json.loads(
        (SERVER / "write_tools.json").read_text(encoding="utf-8"))["tools"]}


def test_design_presets_settings_schema_is_the_kernel_field_set():
    schema = solar_tools.trusted_record(TOOL)["params"]["properties"]["current_settings"]
    assert list(schema["properties"]) == list(kernel().SETTINGS_FIELD_NAMES)
    assert len(schema["properties"]) == len(schema["required"]) == 60
    kinds = {}
    for name, kind in kernel().PRESET_FIELDS:
        if name not in ("Name", "IsBuiltIn"):
            kinds[kind] = kinds.get(kind, 0) + 1
    assert kinds == KIND_COUNTS


def valid_params(**patch):
    params = {"expected_rev": 0, "subcommand": "Create", "name": "Alpha", "current_settings": current()}
    params.update(patch)
    return params


def patched_settings(**patch):
    return valid_params(current_settings=dict(current(), **patch))


SCHEMA_CASES = [
    ("create", lambda: valid_params(), True),
    ("swap-p", lambda: {"expected_rev": 3, "subcommand": "Swap", "prefix": "P27"}, True),
    ("bounds-high", lambda: patched_settings(NumMppt=1000000, Vmp=1000000.0, ModuleLayer="x" * 256), True),
    ("bounds-low", lambda: patched_settings(NumMppt=0, Vmp=-1000000, ModuleLayer=""), True),
    ("subcommand-list", lambda: valid_params(subcommand="List"), False),
    ("name-empty", lambda: valid_params(name=""), False),
    ("name-long", lambda: valid_params(name="x" * 129), False),
    ("prefix-long", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": "P123"}, False),
    ("prefix-empty", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": ""}, False),
    ("no-subcommand", lambda: {"expected_rev": 0}, False),
    ("no-revision", lambda: {"subcommand": "Create", "name": "Alpha"}, False),
    ("revision-negative", lambda: valid_params(expected_rev=-1), False),
    ("extra-key", lambda: valid_params(x=1), False),
    ("settings-missing", lambda: valid_params(
        current_settings={k: v for k, v in current().items() if k != "NumPanels"}), False),
    ("settings-extra", lambda: patched_settings(Name="Alpha"), False),
    ("int-high", lambda: patched_settings(NumMppt=1000001), False),
    ("int-negative", lambda: patched_settings(NumMppt=-1), False),
    ("int-fraction", lambda: patched_settings(NumMppt=2.5), False),
    ("number-high", lambda: patched_settings(Vmp=1000000.5), False),
    ("text-long", lambda: patched_settings(ModuleLayer="x" * 257), False),
    ("text-null", lambda: patched_settings(ModuleLayer=None), False),
    ("bool-number", lambda: patched_settings(UseCombinerBox=1), False),
]


@pytest.mark.parametrize("case,make,valid", SCHEMA_CASES, ids=[row[0] for row in SCHEMA_CASES])
def test_design_presets_params_schema(case, make, valid):
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(make()) is valid


@pytest.mark.parametrize("params,valid", [({}, True), ({"drawing_id": "solar"}, True), ({"x": 1}, False)])
def test_design_presets_list_params_schema(params, valid):
    assert Draft7Validator(solar_tools.trusted_record(LIST)["params"]).is_valid(params) is valid


# -------------------------------------------------------------- C7 and receipts --

def test_design_presets_c7_create_alpha(graph):
    after = commit(graph, {"subcommand": "Create", "name": "Alpha", "current_settings": current()})
    stored = after["extra"]["design_profiles"]
    assert set(stored) == {"schema", "current_settings", "record"}
    assert stored["schema"] == "leaf.solar-design-presets.v1"
    assert stored["record"]["ActivePrefix"] == "A"
    assert stored["record"]["Version"] == 1
    assert [(p["Name"], p["Prefix"]) for p in stored["record"]["Profiles"]] == [("Alpha", "A")]
    assert stored["record"]["Profiles"][0]["Settings"] == snapshot()
    assert stored["record"]["Profiles"][0]["CreatedUtc"] is None
    assert stored["record"]["Profiles"][0]["ReOptResults"] is None
    assert stored["current_settings"] == current()
    output = listed(after)
    assert output["active_prefix"] == "A"
    reports = {row["name"]: row["value"] for row in output["rows"] if row["type"] == "report"}
    assert reports == {"active-prefix": "A", "profile-version": 1}
    rev, graph_sha, store_sha, _, _ = STEP_DIGESTS["f1"]
    assert (after["rev"], digest(after), digest(stored)) == (rev, graph_sha, store_sha)


@pytest.mark.parametrize("step", STEPS)
def test_design_presets_receipt_chain(graph, step):
    before = graph if step == "f1" else chain(graph, STEPS[STEPS.index(step) - 1])
    after = chain(graph, step)
    output = listed(after)
    rows = output["list_rows"] if step == "f4" else output["rows"]
    assert rows == receipt_rows(step) == receipt_rows(step, "studio")
    rev, graph_sha, store_sha, size, output_sha = STEP_DIGESTS[step]
    assert (after["rev"], digest(after), digest(after["extra"]["design_profiles"])) == (rev, graph_sha, store_sha)
    assert (len(canonical_bytes(output)), digest(output)) == (size, output_sha)
    if step == "f4":
        assert after == before                             # List is a read: nothing moves
    assert output["current_settings"] == current()         # the scenario ends on the snapshot's settings


def test_design_presets_empty_drawing_lists_nothing(graph):
    output = listed(graph)
    assert output == {
        "schema": "leaf.solar-design-presets.v1", "active_prefix": None, "current_settings": None,
        "rows": [{"id": {"entity_id": "report-active-prefix"}, "type": "report", "quantity": 1,
                  "unit": "each", "name": "active-prefix", "value": ""},
                 {"id": {"entity_id": "report-profile-version"}, "type": "report", "quantity": 1,
                  "unit": "each", "name": "profile-version", "value": 1}],
        "list_rows": [{"id": {"entity_id": "report-profiles"}, "type": "report", "quantity": 1,
                       "unit": "each", "name": "profiles", "value": 0}]}
    assert (len(canonical_bytes(output)), digest(output)) == EMPTY_LIST


def test_design_presets_commit_changes_only_the_store(graph, passing, service):
    sized = confirm(located(graph), sizing_params(graph, passing))["graph"]
    evidence = require_sizing(sized)
    after = commit(sized, {"subcommand": "Create", "name": "Alpha", "current_settings": current()})
    assert (after["rev"], after["parent_rev"]) == (sized["rev"] + 1, sized["rev"])
    assert entities(after) == entities(sized)
    assert {k: v for k, v in after.items() if k not in ("rev", "parent_rev", "extra")} == \
        {k: v for k, v in sized.items() if k not in ("rev", "parent_rev", "extra")}
    assert {k: v for k, v in after["extra"].items() if k != "design_profiles"} == sized["extra"]
    assert upstream_basis(after) == upstream_basis(sized)
    assert sizing_basis(after) == sizing_basis(sized)
    assert require_sizing(after) == evidence
    assert availability.w1_graph_readiness(after) == availability.w1_graph_readiness(sized)


def test_design_presets_number_settings_are_stored_as_floats(graph):
    after = commit(graph, {"subcommand": "Create", "name": "Alpha",
                           "current_settings": dict(current(), Vmp=44, MaxPanelGap=120)})
    stored = after["extra"]["design_profiles"]
    assert type(stored["current_settings"]["Vmp"]) is float and stored["current_settings"]["Vmp"] == 44.0
    text = listed(after)["rows"][0]["settings"]
    assert '"MaxPanelGap":120.0' in text and '"Vmp":44.0' in text


def test_design_presets_negative_zero_is_refused(graph):
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(graph, patched_settings(Vmp=-0.0))
    assert caught.value.code == "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"
    assert graph == before
    after = commit_builtin().run(graph, patched_settings(Vmp=0.0))
    assert after["extra"]["design_profiles"]["current_settings"]["Vmp"] == 0.0
    assert graph == before


def test_design_presets_name_is_trimmed(graph):
    after = commit(graph, {"subcommand": "Create", "name": "  Alpha \t", "current_settings": current()})
    assert after["extra"]["design_profiles"]["record"]["Profiles"][0]["Name"] == "Alpha"
    assert listed(after)["rows"] == receipt_rows("f1")


# --------------------------------------------------------------------- refusals --

def settings_with(**patch):
    return dict(current(), **patch)


def without(field):
    return {k: v for k, v in current().items() if k != field}


REQUEST_CASES = [
    # (id, params builder, code); expected_rev is 0 and the graph is the untouched fixture.
    ("list", lambda: [], "INVALID_DESIGN_PRESET_REQUEST"),
    ("none", lambda: None, "INVALID_DESIGN_PRESET_REQUEST"),
    ("empty", lambda: {}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("no-subcommand", lambda: {"expected_rev": 0}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("no-revision", lambda: {"subcommand": "Create", "name": "Alpha"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("extra-key", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "A", "x": 1}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("drawing-id", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "A", "drawing_id": "solar"},
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("subcommand-list", lambda: {"expected_rev": 0, "subcommand": "List"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("subcommand-case", lambda: {"expected_rev": 0, "subcommand": "create", "name": "A"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("subcommand-int", lambda: {"expected_rev": 0, "subcommand": 1}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("create-no-name", lambda: {"expected_rev": 0, "subcommand": "Create"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("create-prefix", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "A", "prefix": "A"},
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-int", lambda: {"expected_rev": 0, "subcommand": "Create", "name": 1}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-blank", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "   "}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-control", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "a\tb"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-del", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "a\x7fb"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-long", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "x" * 129}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("name-long-trimmable", lambda: {"expected_rev": 0, "subcommand": "Create", "name": " " + "x" * 128},
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("swap-no-prefix", lambda: {"expected_rev": 0, "subcommand": "Swap"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("swap-name", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": "A", "name": "A"},
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-two-letters", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": "AB"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-one-digit", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": "P1"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-three-digits", lambda: {"expected_rev": 0, "subcommand": "Delete", "prefix": "P123"},
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-empty", lambda: {"expected_rev": 0, "subcommand": "Delete", "prefix": ""}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-newline", lambda: {"expected_rev": 0, "subcommand": "Delete", "prefix": "A\n"}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("prefix-int", lambda: {"expected_rev": 0, "subcommand": "Delete", "prefix": 7}, "INVALID_DESIGN_PRESET_REQUEST"),
    ("settings-list", lambda: valid_params(current_settings=[]), "INVALID_DESIGN_PRESET_REQUEST"),
    ("settings-missing", lambda: valid_params(current_settings=without("NumPanels")), "INVALID_DESIGN_PRESET_REQUEST"),
    ("settings-extra", lambda: valid_params(current_settings=settings_with(Name="Alpha")), "INVALID_DESIGN_PRESET_REQUEST"),
    ("int-text", lambda: valid_params(current_settings=settings_with(NumMppt="12")), "INVALID_DESIGN_PRESET_REQUEST"),
    ("int-bool", lambda: valid_params(current_settings=settings_with(NumMppt=True)), "INVALID_DESIGN_PRESET_REQUEST"),
    ("int-float", lambda: valid_params(current_settings=settings_with(NumMppt=2.0)), "INVALID_DESIGN_PRESET_REQUEST"),
    ("number-text", lambda: valid_params(current_settings=settings_with(Vmp="44")), "INVALID_DESIGN_PRESET_REQUEST"),
    ("number-bool", lambda: valid_params(current_settings=settings_with(Vmp=False)), "INVALID_DESIGN_PRESET_REQUEST"),
    ("number-nan", lambda: valid_params(current_settings=settings_with(Vmp=float("nan"))), "NONFINITE_NUMBER"),
    ("bool-int", lambda: valid_params(current_settings=settings_with(UseCombinerBox=1)), "INVALID_DESIGN_PRESET_REQUEST"),
    ("text-null", lambda: valid_params(current_settings=settings_with(ModuleLayer=None)), "INVALID_DESIGN_PRESET_REQUEST"),
    ("kind-before-range", lambda: valid_params(current_settings=settings_with(NumMppt=-1, Vmp="x")),
     "INVALID_DESIGN_PRESET_REQUEST"),
    ("int-negative", lambda: valid_params(current_settings=settings_with(NumMppt=-1)), "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("int-high", lambda: valid_params(current_settings=settings_with(NumPanels=1000001)), "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("number-high", lambda: valid_params(current_settings=settings_with(Vmp=1000000.5)), "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("number-low", lambda: valid_params(current_settings=settings_with(BVoc=-1000001)), "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("number-huge-int", lambda: valid_params(current_settings=settings_with(Vmp=10 ** 400)),
     "NUMBER_LIMIT_EXCEEDED"),
    ("number-big-int", lambda: valid_params(current_settings=settings_with(Vmp=2 ** 62)),
     "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("text-long", lambda: valid_params(current_settings=settings_with(ModuleLayer="x" * 257)),
     "DESIGN_PRESET_SETTINGS_OUT_OF_RANGE"),
    ("revision-stale", lambda: valid_params(expected_rev=5), "STALE_GRAPH_REVISION"),
    ("revision-text", lambda: valid_params(expected_rev="0"), "STALE_GRAPH_REVISION"),
    ("revision-bool", lambda: valid_params(expected_rev=False), "STALE_GRAPH_REVISION"),
    ("settings-required", lambda: {"expected_rev": 0, "subcommand": "Create", "name": "Alpha"},
     "DESIGN_PRESET_SETTINGS_REQUIRED"),
    ("delete-nothing", lambda: {"expected_rev": 0, "subcommand": "Delete", "prefix": "A", "current_settings": current()},
     "DESIGN_PRESET_NOT_FOUND"),
    ("swap-nothing", lambda: {"expected_rev": 0, "subcommand": "Swap", "prefix": "A", "current_settings": current()},
     "DESIGN_PRESET_SWAP_NEEDS_TWO"),
]


@pytest.mark.parametrize("case,make,code", REQUEST_CASES, ids=[row[0] for row in REQUEST_CASES])
def test_design_presets_request_refusals(graph, case, make, code):
    assert refusal(graph, make()) == code


STATE_CASES = [
    # (id, chain step the graph is at, params without expected_rev, code)
    ("name-exists", "f1", {"subcommand": "Create", "name": "  ALPHA "}, "DESIGN_PRESET_NAME_EXISTS"),
    ("swap-one", "f1", {"subcommand": "Swap", "prefix": "A"}, "DESIGN_PRESET_SWAP_NEEDS_TWO"),
    ("swap-one-unknown", "f1", {"subcommand": "Swap", "prefix": "Z"}, "DESIGN_PRESET_SWAP_NEEDS_TWO"),
    ("swap-unknown", "f2", {"subcommand": "Swap", "prefix": "C"}, "DESIGN_PRESET_NOT_FOUND"),
    ("swap-active", "f2", {"subcommand": "Swap", "prefix": "b"}, "DESIGN_PRESET_ALREADY_ACTIVE"),
    ("delete-unknown", "f2", {"subcommand": "Delete", "prefix": "P27"}, "DESIGN_PRESET_NOT_FOUND"),
    ("settings-before-state", "f2", {"subcommand": "Swap", "prefix": "C",
                                     "current_settings": {"NumMppt": 1}}, "INVALID_DESIGN_PRESET_REQUEST"),
]


@pytest.mark.parametrize("case,step,params,code", STATE_CASES, ids=[row[0] for row in STATE_CASES])
def test_design_presets_state_refusals(graph, case, step, params, code):
    value = chain(graph, step)
    assert refusal(value, dict(params, expected_rev=value["rev"])) == code


def test_design_presets_lowercase_prefix_and_supplied_settings(graph):
    value = chain(graph, "f2")
    changed = dict(current(), NumMppt=7)
    after = commit(value, {"subcommand": "Swap", "prefix": "a", "current_settings": changed})
    stored = after["extra"]["design_profiles"]
    assert stored["record"]["ActivePrefix"] == "A"
    assert stored["record"]["Profiles"][1]["Settings"]["NumMppt"] == 7       # the outgoing Beta was saved
    assert stored["current_settings"]["NumMppt"] == 12                       # Alpha's settings restored


def test_design_presets_swap_keeps_cable_fields_when_target_material_empty(graph):
    alpha = dict(current(), CableMaterial="", NecCableType="A-only")
    value = create(graph, "Alpha", current_settings=alpha)
    value = create(value, "Beta")
    supplied = dict(alpha, CableMaterial="Copper", NecCableType="B-only")
    after = commit(value, {"subcommand": "Swap", "prefix": "A", "current_settings": supplied})
    stored = after["extra"]["design_profiles"]
    assert stored["record"]["ActivePrefix"] == "A"
    assert stored["record"]["Profiles"][0]["Settings"]["NecCableType"] == "A-only"
    assert {f: stored["current_settings"][f] for f in kernel().CABLE_FIELDS} == \
        {f: supplied[f] for f in kernel().CABLE_FIELDS}
    assert stored["current_settings"]["CableMaterial"] == "Copper"
    assert stored["current_settings"]["NecCableType"] == "B-only"


def test_design_presets_swap_restores_cable_fields_when_target_material_nonempty(graph):
    assert len(kernel().CABLE_FIELDS) == 12
    alpha = dict(current(), **{f: "A-only" for f in kernel().CABLE_FIELDS})
    alpha["CableMaterial"] = "Aluminum"
    value = create(graph, "Alpha", current_settings=alpha)
    value = create(value, "Beta")
    supplied = dict(alpha, **{f: "B-only" for f in kernel().CABLE_FIELDS})
    supplied["CableMaterial"] = "Copper"
    after = commit(value, {"subcommand": "Swap", "prefix": "A", "current_settings": supplied})
    stored = after["extra"]["design_profiles"]
    assert stored["record"]["ActivePrefix"] == "A"
    assert {f: stored["current_settings"][f] for f in kernel().CABLE_FIELDS} == \
        {f: alpha[f] for f in kernel().CABLE_FIELDS}
    assert {f: stored["record"]["Profiles"][1]["Settings"][f] for f in kernel().CABLE_FIELDS} == \
        {f: supplied[f] for f in kernel().CABLE_FIELDS}


def mutate_store(value, mutation):
    value = copy.deepcopy(value)
    mutation(value["extra"]["design_profiles"])
    return value


def _profiles(store_value):
    return store_value["record"]["Profiles"]


def too_many_profiles(store_value):
    template = copy.deepcopy(_profiles(store_value)[0])
    items = []
    for index in range(65):
        item = copy.deepcopy(template)
        item.update(Name=f"n{index}", Prefix=chr(65 + index) if index < 26 else f"P{index + 1}")
        item["Settings"]["Name"] = item["Name"]
        items.append(item)
    store_value["record"].update(Profiles=items, ActivePrefix="A")


STORE_CASES = [
    ("not-object", lambda s: s.clear() or s.update({"x": 1})),
    ("schema", lambda s: s.update(schema="leaf.solar-design-presets.v2")),
    ("extra-key", lambda s: s.update(extra=1)),
    ("current-missing-field", lambda s: s["current_settings"].pop("NumPanels")),
    ("current-int-number", lambda s: s["current_settings"].update(Vmp=44)),
    ("current-range", lambda s: s["current_settings"].update(NumMppt=-1)),
    ("version", lambda s: s["record"].update(Version=2)),
    ("active-unknown", lambda s: s["record"].update(ActivePrefix="Q")),
    ("active-none", lambda s: s["record"].update(ActivePrefix=None)),
    ("active-case", lambda s: s["record"].update(ActivePrefix="a")),
    ("prefix-lowercase", lambda s: _profiles(s)[1].update(Prefix="c")),
    ("prefix-duplicate", lambda s: _profiles(s)[1].update(Prefix="A")),
    ("name-duplicate", lambda s: (_profiles(s)[1].update(Name="ALPHA"), _profiles(s)[1]["Settings"].update(Name="ALPHA"))),
    ("name-untrimmed", lambda s: (_profiles(s)[1].update(Name="Beta "), _profiles(s)[1]["Settings"].update(Name="Beta "))),
    ("settings-name", lambda s: _profiles(s)[1]["Settings"].update(Name="Gamma")),
    ("built-in", lambda s: _profiles(s)[1]["Settings"].update(IsBuiltIn=True)),
    ("created", lambda s: _profiles(s)[1].update(CreatedUtc="2026-09-23T20:43:30Z")),
    ("drawing-state", lambda s: _profiles(s)[1]["DrawingState"].update(StringNumber=9)),
    ("drawing-state-bool", lambda s: _profiles(s)[1]["DrawingState"].update(StringNumber=True)),
    ("reopt", lambda s: _profiles(s)[1].update(ReOptResults=[])),
    ("profile-key", lambda s: _profiles(s)[1].update(Extra=1)),
    ("too-many", too_many_profiles),
]


@pytest.mark.parametrize("case,mutation", STORE_CASES, ids=[row[0] for row in STORE_CASES])
def test_design_presets_store_refusals(graph, case, mutation):
    value = mutate_store(chain(graph, "f2"), mutation)
    before = copy.deepcopy(value)
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(value, {"expected_rev": value["rev"], "subcommand": "Create", "name": "Gamma"})
    assert caught.value.code == "DESIGN_PRESETS_STORE_INVALID"
    assert value == before
    with pytest.raises(GraphValidationError) as caught:
        listed(value)
    assert caught.value.code == "DESIGN_PRESETS_STORE_INVALID"


def test_design_presets_store_not_an_object_is_refused(graph):
    value = copy.deepcopy(graph)
    value["extra"]["design_profiles"] = []
    assert refusal(value, valid_params()) == "DESIGN_PRESETS_STORE_INVALID"


def test_design_presets_preset_limit(graph):
    value = stocked(graph, 64)
    assert value["extra"]["design_profiles"]["record"]["Profiles"][-1]["Prefix"] == "P64"
    assert len(canonical_bytes(value["extra"]["design_profiles"])) == STOCKED_64_STORE_BYTES
    assert refusal(value, {"expected_rev": 0, "subcommand": "Create", "name": "one more"}) == \
        "DESIGN_PRESET_LIMIT_REACHED"
    output = listed(value)
    assert (len(canonical_bytes(output)), digest(output)) == STOCKED_64_LIST


def test_design_presets_prefix_collision(graph):
    value = commit(stocked(graph, 28), {"subcommand": "Delete", "prefix": "P27"})
    assert [p["Prefix"] for p in value["extra"]["design_profiles"]["record"]["Profiles"]][-2:] == ["Z", "P28"]
    # The plugin's NextPrefix answers "P" + (count + 1) = P28, a live prefix; Studio refuses it.
    assert value["extra"]["design_profiles"]["record"]["Profiles"] and \
        kernel().load_record  # the kernel is untouched: its own test pins NextPrefix
    assert refusal(value, {"expected_rev": value["rev"], "subcommand": "Create", "name": "again"}) == \
        "DESIGN_PRESET_PREFIX_COLLISION"


def test_design_presets_store_size_limit(graph):
    worst = dict(current(), **{name: '"' * 256 for name, kind in kernel().PRESET_FIELDS
                               if kind == "s" and name not in ("Name",)})
    value = create(graph, "w0", current_settings=worst)
    for index in range(1, WORST_CREATES):
        value = create(value, f"w{index}")
    assert len(canonical_bytes(value["extra"]["design_profiles"])) == WORST_STORE_BYTES
    output = listed(value)
    assert len(canonical_bytes(output)) == WORST_LIST_BYTES
    assert refusal(value, {"expected_rev": value["rev"], "subcommand": "Create", "name": "one more"}) == \
        "DESIGN_PRESETS_STORE_TOO_LARGE"


@pytest.mark.parametrize("mode", ["raises", "not-saved", "wrong-consumed"])
def test_design_presets_kernel_refusal_is_named(graph, monkeypatch, mode):
    profiles = commit_builtin().preset_store.profiles
    leafprofile = profiles.leafprofile

    def refuse(*args, **kwargs):
        if mode == "raises":
            raise profiles.ProfileInputError("x")
        out = leafprofile(*args, **kwargs)
        if mode == "not-saved":
            out["saved"] = False
        else:
            out["consumed"] += 1
        return out

    monkeypatch.setattr(profiles, "leafprofile", refuse)
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError) as caught:
        commit_builtin().run(graph, valid_params())
    assert caught.value.code == "DESIGN_PRESET_KERNEL_REFUSED"
    assert graph == before


@pytest.mark.parametrize("params", [[], None, {"x": 1}, {"drawing_id": "solar"}])
def test_design_presets_list_request_refusals(graph, params):
    with pytest.raises(GraphValidationError) as caught:
        list_builtin().run(copy.deepcopy(graph), params)
    assert caught.value.code == "INVALID_DESIGN_PRESET_LIST_REQUEST"


def test_design_presets_builtins_are_pure(graph):
    value = chain(graph, "f2")
    params = {"expected_rev": value["rev"], "subcommand": "Swap", "prefix": "A",
              "current_settings": dict(current(), NumMppt=7)}
    original_value, original_params = copy.deepcopy(value), copy.deepcopy(params)
    first = commit_builtin().run(value, params)
    second = commit_builtin().run(value, params)
    assert first == second and value == original_value and params == original_params
    first_list, second_list = list_builtin().run(value, {}), list_builtin().run(value, {})
    assert first_list == second_list and value == original_value
    first_list["rows"].clear()
    first_list["current_settings"]["NumMppt"] = 99
    assert list_builtin().run(value, {}) == second_list and value == original_value


# ----------------------------------------------------- readiness and registry --

@pytest.mark.parametrize("state", ["ready", "units-unresolved"])
def test_design_presets_readiness(graph, state):
    if state == "units-unresolved":
        graph["project"]["units"]["meters_per_unit"] *= 2
    actual = availability.w1_local_commit_inputs(graph)
    expected = ({"input_ready": True, "input_reason": None} if state == "ready"
                else {"input_ready": False, "input_reason": "unresolved_units"})
    assert actual[TOOL] == actual[LIST] == expected


def test_design_presets_registry_and_catalog(monkeypatch):
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
    assert LIST in solar_tools.local_graph_read_tools() and LIST not in solar_tools.local_graph_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-commit"
    assert availability.SOLAR_CAPABILITIES[LIST]["adapter"] == "local-graph-read"
    assert not {TOOL, LIST} & set(availability.W1_CAPABILITIES)
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
    assert entitlements.tool_required_capability(solar_tools.trusted_record(LIST)) == "run_read"
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family["family_id"], row) for family in families for row in family["capabilities"]
             if row["name"] in (TOOL, LIST)]
    assert sorted(row["name"] for _, row in found) == [TOOL, LIST]
    for family, row in found:
        adapter, order, _, _, capability = DECLARATIONS[row["name"]]
        assert family == "settings"
        assert row["solar"] == {
            "schema": "leaf.solar-tool-view.v1", "name": row["name"], "family": "settings", "wave": 2,
            "order": order, "maturity": "preview", "engine": "server-builtin", "adapter": adapter,
            "entitlement": "run_write" if capability == "drawing.write" else "run_read",
            "interaction": {"mode": "form"}, "ledger": ["design-profile-manage"]}


# --------------------------------------------------------- rail, API and broker --

def test_design_presets_dispatch_and_revision(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = dispatch(backend, fence, TOOL, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                               "current_settings": current()})
        assert first["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (first["before_rev"], first["after_rev"]) == (0, 1)
        assert first["graph_sha256"] == STEP_DIGESTS["f1"][1]

        def run(params, version, job):
            return solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(params, drawing_id="solar"), drawing_id="solar",
                source_version=version, holder="fixture-owner", fence=fence, job_id=job)

        second = run({"expected_rev": 1, "subcommand": "Create", "name": "Beta"}, 2, "presets-beta")
        assert second["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
        assert second["graph_sha256"] == STEP_DIGESTS["f2"][1]
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            run({"expected_rev": 1, "subcommand": "Swap", "prefix": "A"}, 3, "presets-stale")
        with pytest.raises(GraphValidationError, match="DESIGN_PRESET_NAME_EXISTS"):
            run({"expected_rev": 2, "subcommand": "Create", "name": "beta"}, 3, "presets-repeat")
        assert latest(backend) == 3
    assert listed(head_graph(backend))["rows"] == receipt_rows("f2")


@pytest.fixture
def presets_api(request):
    client = request.getfixturevalue("api")
    client[2][TOOL] = solar_tools.trusted_record(TOOL)
    return client


def post(client, params):
    return client[0].post("/api/run?wait=1", json=body(client, TOOL, params))


def test_design_presets_api_commit_end_to_end(presets_api):
    response = post(presets_api, {"expected_rev": 0, "subcommand": "Create", "name": "Alpha",
                                  "current_settings": current()})
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    assert env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert env["result"]["graph_sha256"] == STEP_DIGESTS["f1"][1]
    response = post(presets_api, {"expected_rev": 1, "subcommand": "Create", "name": "Beta"})
    assert response.status_code == 200, response.text
    assert store.load_manifest(presets_api[1], TENANT, "solar")["head"] == 3
    assert listed(head_graph(presets_api[1]))["rows"] == receipt_rows("f2")
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 2 and all(row["status"] == "complete" for row in records)


def test_design_presets_api_schema_refusal(presets_api):
    response = post(presets_api, {"expected_rev": 0, "subcommand": "List"})
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(row["status"] != "complete" for row in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(row.get("error") or {}).get("reason_code") for row in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(presets_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_design_presets_api_builtin_refusal(presets_api):
    params = {"expected_rev": 0, "subcommand": "Create", "name": "Alpha"}
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(params)
    response = post(presets_api, params)
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "DESIGN_PRESET_SETTINGS_REQUIRED"
    assert store.load_manifest(presets_api[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"


def _read_api(backend, tmp_path, monkeypatch):
    """select-by-zone's read harness (test_solar_tool_select_by_zone._api) for LIST."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    record = deps.find_tool(LIST, TENANT)
    assert record == solar_tools.trusted_record(LIST)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(record, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    mode = {"tamper": False, "requests": []}

    def transport(url, *, json, headers, timeout):
        mode["requests"].append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                reply = __import__("json").loads(response.body)
                if mode["tamper"] and reply.get("ok") is True:
                    output = dict(reply["result"]["output"], active_prefix="B")
                    reply["result"].update(output=output, output_sha256=digest(output),
                                           output_bytes=len(canonical_bytes(output)))
                return reply

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, record, tenant, mode


@pytest.fixture
def list_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, chain(graph, "f3"))
    yield from _read_api(backend, tmp_path, monkeypatch)


def list_body(client):
    return {"tool": LIST, "dwg": "solar", "params": {}, "catalog_digest": deps.catalog_tool_digest(client[2])}


def test_design_presets_api_list_end_to_end(list_api):
    response = list_api[0].post("/api/run?wait=1", json=list_body(list_api))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True
    result = env["result"]
    rec = jobs.get_job(result["job_id"])
    assert rec["status"] == "complete" and rec["dwg_version"] == 1
    assert rec["params"] == {"drawing_id": "solar"}
    assert result["output"]["list_rows"] == receipt_rows("f4")
    assert (result["output_bytes"], result["output_sha256"]) == STEP_DIGESTS["f4"][3:]
    assert result["request_sha256"] == LIST_REQUEST_SHA256
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = solar_local_read.graph_read_provenance(
        result, rec["params"], TENANT, rec["job_id"], LIST, 1, backend=list_api[1])
    for key, value in receipt.items():
        assert provenance[key] == rec["provenance"][key] == value
    assert len(list_api[4]["requests"]) == 1
    manifest = store.load_manifest(list_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_design_presets_worker_rejects_a_tampered_list_receipt(list_api):
    list_api[4]["tamper"] = True
    response = list_api[0].post("/api/run?wait=1", json=list_body(list_api))
    assert response.status_code == 500, response.text
    assert response.json()["error"]["message"] == "graph read terminal proof rejected"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"
    assert store.load_manifest(list_api[1], TENANT, "solar")["head"] == 1
