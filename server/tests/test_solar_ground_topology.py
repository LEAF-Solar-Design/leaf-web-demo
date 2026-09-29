"""Typed L1/L2 equipment topology on the shared design graph, and W1 compatibility."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import write_loop  # noqa: F401; establishes the drawing-store import path
import store
from solar_design_graph import (
    GraphValidationError, deserialize_graph, load_schema, serialize_graph, validate_graph,
)
from solar_solve_results import digest, upstream_basis, version_companion
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
from test_w1_graph_versions import DRAWING, TENANT, commit, drawing, request_for  # noqa: F401, fixture
import test_solar_inverter_cabling as cabling_tests

cab = cabling_tests.cab
SERVER = Path(__file__).resolve().parents[1]

L1_ID, L2_ID = app_id("inverter", 1), app_id("inverter", 2)


def canon_sha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def central(n, number, feeds, **fields):
    """A central (L2) inverter entity with application id n and device number `number`."""
    values = dict(number=number, type_key="A", is_l2=True, position=[20, 0], model="fixture-central",
                  mppt_count=6, total_dc_inputs=36, max_dc_voltage=1500, max_ac_power_kw=250,
                  is_solaredge=False, input_assignments=[], equipment_type="central_inverter",
                  collector_capacity=4, l1_assignments=feeds)
    values.update(fields)
    return entity("inverter", n, **values)


def combiner(n, number, l2_ref, **fields):
    """A combiner box (L1) entity with no strings."""
    values = dict(number=number, type_key="A", is_l2=False, position=[4, 0], model="fixture-combiner",
                  mppt_count=1, total_dc_inputs=2, max_dc_voltage=600, max_ac_power_kw=1,
                  is_solaredge=False, input_assignments=[], equipment_type="combiner_box", l2_ref=l2_ref)
    values.update(fields)
    return entity("inverter", n, **values)


def topology_of(w1):
    """The W1 fixture in L1/L2 mode: its inverter becomes combiner box 1 feeding central inverter 1."""
    g = deepcopy(w1)
    g["settings"]["use_l2_collectors"] = True
    g["inverters"][0].update(equipment_type="combiner_box", l2_ref=L2_ID)
    g["inverters"].append(central(2, 1, [{"inverter_ref": L1_ID, "mppt_index": 0}]))
    return g


def bare(w1):
    """L1/L2 mode with no electrical content at all (the state before any device is placed)."""
    g = deepcopy(w1)
    g["settings"]["use_l2_collectors"] = True
    for key in ("electrical_zones", "frames", "panels", "strings", "inverters", "routes", "schedules"):
        g[key] = []
    return g


def rewired(value):
    """Next revision: the central inverter moved, its capacity and the feed's MPPT index changed."""
    g = deepcopy(value)
    g.update(rev=1, parent_rev=0)
    g["inverters"][1].update(position=[30, 5], collector_capacity=3)
    g["inverters"][1]["l1_assignments"][0]["mppt_index"] = 5
    return g


def l1(g):
    return g["inverters"][0]


def l2(g):
    return g["inverters"][1]


def _unconnected(g):
    l1(g)["l2_ref"] = None
    l2(g)["l1_assignments"] = []


def _direct(g):
    g["settings"]["use_l2_collectors"] = True
    l1(g).update(equipment_type="central_inverter", is_l2=True, collector_capacity=4, l1_assignments=[])


def _shared(g):
    g["inverters"].append(combiner(3, 2, L2_ID))
    l2(g)["l1_assignments"].append({"inverter_ref": app_id("inverter", 3), "mppt_index": 0})


def _full(g):
    l2(g)["collector_capacity"] = 1
    l2(g)["l1_assignments"][0]["mppt_index"] = 5


def _future(g):
    l2(g)["l1_assignments"][0]["future"] = {"k": [1, None]}
    l1(g)["future_equipment"] = "x"


def _mixed(g):
    s1, s2 = app_id("string", 1), app_id("string", 2)
    l1(g)["input_assignments"] = [a for a in l1(g)["input_assignments"] if a["string_ref"] == s1]
    l2(g)["input_assignments"] = [{"string_ref": s2, "mppt_letter": "A", "input_number": 0}]
    g["strings"][1].update(inverter_ref=L2_ID, to_ref=L2_ID)
    frame = g["frames"][0]
    for record in frame["panel_assignments"] + [cell for row in frame["matrix"] for cell in row]:
        if record["panel_ref"] == app_id("panel", 3):
            record.update(inverter_id=L2_ID, string_input_number=0)


def _feed_to_central(g):
    g["inverters"].append(central(3, 2, []))
    l2(g)["l1_assignments"][0]["inverter_ref"] = app_id("inverter", 3)


def _ref_to_missing(g):
    l1(g)["l2_ref"] = app_id("inverter", 9)
    l2(g)["l1_assignments"] = []


def _feed(g):
    return l2(g)["l1_assignments"][0]


ADMITTED = [
    ("l2-mode-empty-topology", "bare", lambda g: None, "d7156732b3391195ac721beee629c88f3d258340e38506503b60c6722fd5f7cd"),
    ("unconnected-l1", "topology", _unconnected, "6e1223a2d0d92b276799009f56cbeecd765663c84e265dddada798a46a2d41b1"),
    ("string-inverter-l1", "topology", lambda g: l1(g).update(equipment_type="string_inverter"), "254d500f86010520d4f2f42cb907a70642107a269602c830defbda71abe4fd83"),
    ("strings-direct-on-l2", "w1", _direct, "b16dbdfaef74fd995cf7b3318d8f90a7c7f1a006fcb36f442dc8fb96195190ba"),
    ("shared-mppt-index", "topology", _shared, "6f009399edf7310e7c34c44324caae4f5b7669dd544a960a3e4fd43797d18269"),
    ("full-capacity-top-index", "topology", _full, "8e1e163b7c3b54d4a005d0b23d1b2d57be5d45ce200fca721b49ca96c7a38a2d"),
    ("unknown-fields", "topology", _future, "4377485c959c46ea2bd1d198d6ef7529db559231a79e955c3e357f9bcca955cd"),
]

REFUSED = [
    ("legacy-is-l2", "w1", lambda g: l1(g).update(is_l2=True), "INVALID_GRAPH_SCHEMA"),
    ("legacy-with-l2-ref", "w1", lambda g: l1(g).update(l2_ref=None), "INVALID_GRAPH_SCHEMA"),
    ("legacy-in-l2-mode", "w1", lambda g: g["settings"].update(use_l2_collectors=True), "EQUIPMENT_TYPE_REQUIRED"),
    ("l2-mode-not-boolean", "w1", lambda g: g["settings"].update(use_l2_collectors="yes"), "INVALID_GRAPH_SCHEMA"),
    ("typed-without-l2-mode", "topology", lambda g: g["settings"].update(use_l2_collectors=False), "L2_MODE_REQUIRED"),
    ("unknown-equipment-type", "topology", lambda g: l2(g).update(equipment_type="skid"), "INVALID_GRAPH_SCHEMA"),
    ("central-not-l2", "topology", lambda g: l2(g).update(is_l2=False), "INVALID_GRAPH_SCHEMA"),
    ("combiner-is-l2", "topology", lambda g: l1(g).update(is_l2=True), "INVALID_GRAPH_SCHEMA"),
    ("central-with-l2-ref", "topology", lambda g: l2(g).update(l2_ref=None), "INVALID_GRAPH_SCHEMA"),
    ("central-without-capacity", "topology", lambda g: l2(g).pop("collector_capacity"), "INVALID_GRAPH_SCHEMA"),
    ("central-without-feeds", "topology", lambda g: l2(g).pop("l1_assignments"), "INVALID_GRAPH_SCHEMA"),
    ("combiner-without-l2-ref", "topology", lambda g: l1(g).pop("l2_ref"), "INVALID_GRAPH_SCHEMA"),
    ("combiner-with-feeds", "topology", lambda g: l1(g).update(l1_assignments=[]), "INVALID_GRAPH_SCHEMA"),
    ("combiner-with-capacity", "topology", lambda g: l1(g).update(collector_capacity=1), "INVALID_GRAPH_SCHEMA"),
    ("combiner-two-mppt", "topology", lambda g: l1(g).update(mppt_count=2), "INVALID_GRAPH_SCHEMA"),
    ("negative-capacity", "topology", lambda g: l2(g).update(collector_capacity=-1), "INVALID_GRAPH_SCHEMA"),
    ("capacity-over-bound", "topology", lambda g: l2(g).update(collector_capacity=10001), "INVALID_GRAPH_SCHEMA"),
    ("bool-mppt-index", "topology", lambda g: _feed(g).update(mppt_index=True), "INVALID_GRAPH_SCHEMA"),
    ("negative-mppt-index", "topology", lambda g: _feed(g).update(mppt_index=-1), "INVALID_GRAPH_SCHEMA"),
    ("malformed-feed-ref", "topology", lambda g: _feed(g).update(inverter_ref="inverter-1"), "INVALID_GRAPH_SCHEMA"),
    ("feed-without-index", "topology", lambda g: _feed(g).pop("mppt_index"), "INVALID_GRAPH_SCHEMA"),
    ("duplicate-l1-number", "topology", lambda g: g["inverters"].append(combiner(3, 1, None)), "DUPLICATE_EQUIPMENT_NUMBER"),
    ("duplicate-l2-number", "topology", lambda g: g["inverters"].append(central(3, 1, [])), "DUPLICATE_EQUIPMENT_NUMBER"),
    ("l2-mixed-inputs", "topology", _mixed, "L2_MIXED_INPUTS"),
    ("over-collector-capacity", "topology", lambda g: l2(g).update(collector_capacity=0), "L2_CAPACITY_EXCEEDED"),
    ("mppt-index-out-of-range", "topology", lambda g: _feed(g).update(mppt_index=6), "L2_CAPACITY_EXCEEDED"),
    ("feed-to-missing", "topology", lambda g: _feed(g).update(inverter_ref=app_id("inverter", 9)), "L2_ASSIGNMENT_MISMATCH"),
    ("feed-to-string", "topology", lambda g: _feed(g).update(inverter_ref=app_id("string", 1)), "L2_ASSIGNMENT_MISMATCH"),
    ("feed-to-central", "topology", _feed_to_central, "L2_ASSIGNMENT_MISMATCH"),
    ("l2-ref-without-feed", "topology", lambda g: l2(g).update(l1_assignments=[]), "L2_ASSIGNMENT_MISMATCH"),
    ("feed-without-l2-ref", "topology", lambda g: l1(g).update(l2_ref=None), "L2_ASSIGNMENT_MISMATCH"),
    ("l2-ref-to-missing", "topology", _ref_to_missing, "L2_ASSIGNMENT_MISMATCH"),
    ("duplicate-feed", "topology", lambda g: l2(g)["l1_assignments"].append({"inverter_ref": L1_ID, "mppt_index": 1}), "DUPLICATE_L2_INPUT"),
    ("feed-on-two-l2s", "topology", lambda g: g["inverters"].append(central(3, 2, [{"inverter_ref": L1_ID, "mppt_index": 0}])), "L2_ASSIGNMENT_MISMATCH"),
    ("combiner-over-inputs", "topology", lambda g: l1(g).update(total_dc_inputs=1), "INVERTER_CAPACITY_EXCEEDED"),
]


def build(w1, base, change):
    g = {"w1": deepcopy, "topology": topology_of, "bare": bare}[base](w1)
    change(g)
    return g


FILL_MAX_DC_VOLTAGE = 1500   # not in the i5 state: device hardware is null until adopt-l2-inverters
FILL_MAX_AC_POWER_KW = 1     # not in the i5 state, same reason


def i5_graph(w1):
    """The committed i5 kernel result projected onto the topology contract (test scaffolding, not the
    bridge). Returns (graph, after state, intake)."""
    before, intake, groups = cabling_tests.committed_fixture()
    after, _ = cab.combiner_auto_place(before, groups, cabling_tests.CAPTURE, cabling_tests.PLAN, intake)
    l1_devices, l2_devices = cab.levels(after)
    handles = cab._string_ids(after, intake)            # {intake string number: state string handle}
    string_l1 = {handles[int(k)]: v for k, v in after["setting"]["CombinerStringL1Assignments"].items()}
    l1_to_l2 = {int(k): v for k, v in after["setting"]["L1ToL2Assignments"].items()}
    mppt_slot = {int(k): v for k, v in after["setting"]["L1ToL2InputAssignments"].items()}
    context = intake["commandContext"]
    mpu = intake["drawing"]["metersPerUnit"]
    numbers = sorted(handles)

    def l1_id(number):
        return app_id("inverter", 100 + number)

    def l2_id(number):
        return app_id("inverter", 200 + number)

    g = bare(w1)
    for n in numbers:
        target = l1_id(string_l1[handles[n]])
        g["strings"].append(entity(
            "string", n, circuit_tag=handles[n], circuit_kind="String", ordered_panel_refs=[],
            module_count=0, from_ref=None, to_ref=target, tag_text_ref=None, wire_gauge="",
            length_ft=0, route=[], inverter_ref=target))
    for item in l1_devices:
        served = [n for n in numbers if string_l1[handles[n]] == item["number"]]
        x, y = item["position"]
        g["inverters"].append(entity(
            "inverter", 100 + item["number"], number=item["number"],
            type_key=item["row"]["_detail"]["type_key"], is_l2=False, position=[x * mpu, y * mpu],
            model="", mppt_count=1, total_dc_inputs=item["row"]["_detail"]["box_input_count"],
            max_dc_voltage=FILL_MAX_DC_VOLTAGE, max_ac_power_kw=FILL_MAX_AC_POWER_KW, is_solaredge=False,
            input_assignments=[{"string_ref": app_id("string", n), "mppt_letter": "A", "input_number": k}
                               for k, n in enumerate(served)],
            equipment_type="combiner_box", l2_ref=l2_id(l1_to_l2[item["number"]])))
    for item in l2_devices:
        feeds = sorted(number for number, target in l1_to_l2.items() if target == item["number"])
        x, y = item["position"]
        g["inverters"].append(entity(
            "inverter", 200 + item["number"], number=item["number"],
            type_key=item["row"]["_detail"]["type_key"], is_l2=True, position=[x * mpu, y * mpu],
            model="", mppt_count=context["l2NumMppt"],
            total_dc_inputs=context["l2NumMppt"] * context["l2StringsPerMppt"],
            max_dc_voltage=FILL_MAX_DC_VOLTAGE, max_ac_power_kw=FILL_MAX_AC_POWER_KW, is_solaredge=False,
            input_assignments=[], equipment_type="central_inverter",
            collector_capacity=cabling_tests.CAPTURE["L1CollectorsPerL2"],
            l1_assignments=[{"inverter_ref": l1_id(number), "mppt_index": mppt_slot[number]} for number in feeds]))
    return g, after, intake


def test_ground_topology_w1_fixture_is_byte_identical(graph):
    assert validate_graph(graph) == graph
    assert canon_sha(graph) == "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"
    encoded = serialize_graph(graph).encode("utf-8")
    assert len(encoded) == 11244
    assert sha256(encoded).hexdigest() == "1fd29adc97543a32e739c14bcad1257284b6fcf15eb08d9d3a1407df0b9c84fc"
    assert upstream_basis(graph) == "b013a3c9353bea41c2f9118c5b26bead06496673b6d0ca005f99735722b5dc44"
    after = deepcopy(graph)
    after.update(rev=1, parent_rev=0)
    intake = {"layers": ["Panels"], "polylines": [], "solar_design_graph": graph,
              "solar_design_graph_sha256": digest(graph)}
    companion = version_companion(intake, graph, after)
    assert canon_sha(companion) == "3b6adb8bc8973ba6ebef8e9e036bb4426be980e0fdaff4d5d2ce5f215a0de99d"
    assert companion["solar_design_graph_sha256"] == "a1cf8d4ed8fa48f297f9072507e0a38718ebf04b6ee3f1cb901aa07d4181fa76"


def test_ground_topology_schema_checks_and_admits_topology(graph):
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for value in (graph, topology_of(graph), bare(graph)):
        assert list(validator.iter_errors(value)) == []


def test_ground_topology_round_trip(graph):
    value = topology_of(graph)
    assert validate_graph(value) == value
    assert canon_sha(value) == "d5c400309cb9d13f7557465cd3cdf325f6a30108dce600b57362dd83b3b51d1e"
    encoded = serialize_graph(value).encode("utf-8")
    assert len(encoded) == 12105
    assert sha256(encoded).hexdigest() == "26a892edcfe13336c09390d50aba6ed1b564d4319c8b38613a5ee100c1504fe0"
    assert deserialize_graph(encoded) == value


@pytest.mark.parametrize("name,base,change,expected", ADMITTED, ids=[row[0] for row in ADMITTED])
def test_ground_topology_admitted_variants(graph, name, base, change, expected):
    g = build(graph, base, change)
    assert validate_graph(g) == g
    assert canon_sha(g) == expected
    assert deserialize_graph(serialize_graph(g)) == g


@pytest.mark.parametrize("name,base,change,expected", REFUSED, ids=[row[0] for row in REFUSED])
def test_ground_topology_refusals(graph, name, base, change, expected):
    g = build(graph, base, change)
    before = deepcopy(g)
    with pytest.raises(GraphValidationError) as exc:
        validate_graph(g)
    assert exc.value.code == expected
    assert g == before


def test_ground_topology_upstream_basis_ignores_equipment(graph):
    value = topology_of(graph)
    expected = "feca2c152f9341a1f857ca5848c33d7ed01918884c079098bea42571c74a7f91"
    assert upstream_basis(value) == expected
    assert upstream_basis(rewired(value)) == expected
    assert upstream_basis(graph) == "b013a3c9353bea41c2f9118c5b26bead06496673b6d0ca005f99735722b5dc44"
    assert upstream_basis(graph) != expected


def test_ground_topology_version_companion_publishes_topology(graph):
    value = topology_of(graph)
    intake = {"layers": ["Panels"], "polylines": [], "solar_design_graph": value,
              "solar_design_graph_sha256": digest(value)}
    companion = version_companion(intake, value, rewired(value))
    assert companion["solar_design_graph"] == rewired(value)
    assert companion["solar_design_graph_sha256"] == "8d9df00ef5600589ab7828fec4405d64db99e6004e57d6e5132a0b095b45ad4f"
    assert canon_sha(companion) == "d5db3777ab825487df137c2a958c0b2cd1440a8390e679af1816bd1965b24929"


def test_ground_topology_store_publish_reopen(graph, drawing):
    backend, _ = drawing
    value = topology_of(graph)
    first = request_for(backend, value)
    assert commit(drawing, first) == 2
    bundle = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING,
                                     project_id=first["project_id"])
    assert bundle["graph"] == first["graph"]
    assert bundle["graph"]["inverters"][1]["l1_assignments"] == [
        {"inverter_ref": L1_ID, "mppt_index": 0}]
    second = request_for(backend, rewired(value), apply_id="apply-2", dwg=b"second DWG")
    assert commit(drawing, second, dwg=b"second DWG") == 3
    reopened = store.read_graph_bundle(store.FilesystemBackend(backend.root), TENANT, DRAWING,
                                       project_id=second["project_id"])["graph"]
    assert reopened["rev"] == 1
    assert reopened["inverters"][1]["l1_assignments"][0]["mppt_index"] == 5
    assert reopened["settings"]["use_l2_collectors"] is True


def test_ground_topology_settings_builtin_admits_mode_only_with_topology(graph):
    spec = importlib.util.spec_from_file_location("solar_settings", SERVER / "builtins" / "solar_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    empty = bare(graph)
    empty["settings"]["use_l2_collectors"] = False
    for value, mode, code in (
        (graph, True, "EQUIPMENT_TYPE_REQUIRED"),
        (empty, True, None),
        (topology_of(graph), False, "L2_MODE_REQUIRED"),
    ):
        intake = deepcopy(value)
        params = {"expected_rev": 0, "changes": {"use_l2_collectors": mode}}
        before_intake, before_params = deepcopy(intake), deepcopy(params)
        if code is not None:
            with pytest.raises(GraphValidationError) as exc:
                module.run(intake, params)
            assert exc.value.code == code
        else:
            result = module.run(intake, params)
            assert result["rev"] == 1
            assert result["settings"]["use_l2_collectors"] is True
            assert result["settings"]["global_string_sizing_confirmed"] is False
        assert intake == before_intake
        assert params == before_params


def test_ground_topology_i5_kernel_state_fits_the_contract(graph):
    g, after, intake = i5_graph(graph)
    assert validate_graph(g) == g
    assert len(g["strings"]) == 173
    l1_devices = [item for item in g["inverters"] if not item["is_l2"]]
    l2_devices = [item for item in g["inverters"] if item["is_l2"]]
    assert len(l1_devices) == 14
    assert len(l2_devices) == 8
    assert sum(len(item["l1_assignments"]) for item in l2_devices) == 14
    assert max(len(item["l1_assignments"]) for item in l2_devices) <= 2
    assert sum(len(item["input_assignments"]) for item in l1_devices) == 173
    assert max(len(item["input_assignments"]) for item in l1_devices) <= 19
    assert canon_sha(g) == "b8b397762b34bc9689adedb74529ce8e5547d49eb923fec95c1d5cadc33c75b8"
    assert deserialize_graph(serialize_graph(g)) == g
    assert len(after["rows"]["device"]) == 22
    feeders = [row for row in after["rows"]["cable"] if row["cable_kind"] == "feeder"]
    assert sorted((row["from"], row["to"]) for row in feeders) == sorted(
        (int(k), v) for k, v in after["setting"]["L1ToL2Assignments"].items())
    handles = cab._string_ids(after, intake)
    string_l1 = {handles[int(k)]: v for k, v in after["setting"]["CombinerStringL1Assignments"].items()}
    positions = {item["number"]: item["position"] for item in cab.levels(after)[0]}
    homeruns = [row for row in after["rows"]["cable"] if row["cable_kind"] == "dc-homerun"]
    assert len(homeruns) == 346
    for row in homeruns:
        assert tuple(row["vertices"][-1]["value"]) == tuple(positions[string_l1[row["from"]]])
