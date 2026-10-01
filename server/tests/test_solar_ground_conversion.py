"""sf-w3-conversion-graph-kernel: the drawing's current Ground physical state becomes compact Ground
frames (codec leaf.solar-ground-slots.v1) with deterministic ids; overlaps are reported, never
removed. Every expected value was measured by running this module with python -B."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_design_graph as sdg  # noqa: E402
import solar_ground_graph_codec as codec  # noqa: E402
import solar_ground_conversion as conv  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import write_loop  # noqa: E402
from test_solar_ground_graph_codec import _load, b18, canon, canon_sha, ground_base, ground_graph, uid  # noqa: E402,F401
from test_w1_design_graph import entity, graph  # noqa: E402,F401
from test_w1_solve_commit import seed  # noqa: E402

TENANT = "fixture-tenant"
DRAWING = "solar"
PROJECT = "leaf:project:00000000-0000-4000-8000-000000000001"
SOURCE = "c" * 64
VIEW = {"index": 0, "state": {"artifact_id": "b" * 64, "content_sha256": "d" * 64}}


def provenance():
    return deepcopy(entity("frame", 1)["provenance"])


def drawn(ax, ay, bx, by, slots, *, width=2.0, row_index=1, command="LEAFTRACK", block="LEAFSAT", tilt=60.0):
    return {"block": block, "source_command": command, "tracker_model": "single_axis_tracker",
            "axis_start": [ax, ay], "axis_end": [bx, by], "slots": slots, "row_index": row_index,
            "cross_axis_width_du": width, "max_tilt_deg": tilt, "rail_overhang_m": 0.05}


SMALL_ROWS = [drawn(0.0, 0.0, 0.0, 6.0, 3),
              drawn(4.0, 0.0, 4.0, 10.0, 2, width=1.0, row_index=2, command="LEAFSAT")]


def small_doc(rows=None, units="m", **state):
    value = {"frames": [], "tracker_rows": deepcopy(SMALL_ROWS if rows is None else rows),
             "settings": {"TrackerModulePmaxW": 450.0}}
    value.update(state)
    return ps.physical_document(value, drawing_units=units, source_sha256=SOURCE,
                                capability="trackers-to-panelgroups")


def convert(w1, doc=None, g=None, view=VIEW, prov=None, rev=0):
    return conv.convert_physical_state(view, small_doc() if doc is None else doc,
                                       ground_base(w1) if g is None else g,
                                       provenance=provenance() if prov is None else prov, rev=rev)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(conv.GroundConversionError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code and str(error.value) == code
    return error.value


@pytest.fixture(scope="module")
def site(b18):
    evidence = _load("solar_ground_dsteps_evidence", ROOT / "scripts" / "solar_ground_dsteps_evidence.py")
    raw = (ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json").read_bytes()
    state = evidence.b18_state(json.loads(raw.decode("utf-8")))
    document = ps.physical_document(state, drawing_units="m", source_sha256=hashlib.sha256(raw).hexdigest(),
                                    capability="trackers-to-panelgroups")
    results = {}

    def result_for(w1):
        """The b18 conversion for the W1 fixture graph, computed once per module."""
        key = canon_sha(ground_base(w1))
        if key not in results:
            results[key] = conv.convert_physical_state(VIEW, document, ground_base(w1),
                                                       provenance=provenance(), rev=0)
        return results[key]
    return {"state": state, "document": document, "result": result_for, "evidence": evidence}


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    store_backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: store_backend)
    return store_backend


def with_power(g, watts):
    value = deepcopy(g)
    for frame in value["frames"]:
        frame["module_power_watts"] = watts
    return value


def test_ground_conversion_kernel_constants():
    assert conv.RESULT_SCHEMA == "leaf.solar-ground-conversion.v1"
    assert conv.CODES == frozenset({
        "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED", "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED",
        "GROUND_CONVERSION_GROUND_PROJECT_REQUIRED", "GROUND_CONVERSION_UNITS_MISMATCH",
        "GROUND_CONVERSION_INPUT_INVALID", "GROUND_CONVERSION_LIMIT_EXCEEDED"})
    assert conv.MAX_GRID_CELLS == 2_000_000 and conv.MAX_PROVENANCE_BYTES == 16384
    assert issubclass(conv.GroundConversionError, ValueError)


def test_ground_conversion_kernel_ids_are_the_scale_record_scheme():
    key = "a" * 64 + ":ground-frame:0"
    assert conv.deterministic_id("frame", key) == uid("frame", key) == "leaf:frame:25d4df7a-231a-4b42-ac8d-9afa94bdabca"


def test_ground_conversion_kernel_tracker_entities_port(site):
    state = site["state"]
    before = repr(state)
    ents = conv.tracker_entities(state)
    assert ents == site["evidence"].bev.tracker_entities(state)
    assert len(ents) == 1197 + 237 and repr(state) == before


def test_ground_conversion_kernel_small_result(graph):
    result = convert(graph)
    assert canon_sha(result) == "db5b85d4051f5ca1b4192c147c5e1d6564a941d1dba0356e25a6f7cb474913c9"
    assert result["counts"] == {"trackers": 2, "slots": 5}
    assert result["settings"] == {"PanelGroupNumber": 3, "PanelGroupColour": 2}
    first = result["frames"][0]
    assert (first["name"], first["module_power_watts"], first["ground_slots"]["count"]) == ("Group 1", 450.0, 3)
    g = ground_base(graph)
    g["frames"] = result["frames"]
    sdg._reset_validation_caches()
    assert sdg.validate_graph(g) == g
    expanded = codec.expand_graph(g)
    sdg._reset_validation_caches()
    assert sdg.validate_graph(expanded) == expanded
    assert canon_sha(expanded) == "a8c6b54c38f72fbe6b237e62004ccbc869609db6447a14f7c1b56ff6cb002b9b"


def test_ground_conversion_kernel_b18_site(site, graph):
    result = site["result"](graph)
    assert result["counts"] == {"trackers": 237, "slots": 69678}
    assert result["settings"] == {"PanelGroupNumber": 238, "PanelGroupColour": 237}
    assert result["overlap"] == {
        "distinct_centres": 69678, "centres_in_own_outline": 69678, "centres_in_two_outlines": 25284,
        "centres_in_two_outlines_same_source_command": 0, "centres_in_more_than_two_outlines": 0}
    assert result["source"] == {"head_index": 0, "state_artifact_id": "b" * 64, "state_content_sha256": "d" * 64}
    assert canon_sha(result["frames"]) == "8ca97b092f404bc24ab5f0dfbdd4522b7f4aaf07e764b2c2baf1d069f7b05081"
    assert canon_sha(result) == "6f2f6d00d47a59ab28edb8b4502b30f31496ec90db7e239ef4904b0b4597f0d7"


def test_ground_conversion_kernel_b18_is_the_scale_record_graph(site, b18, graph, monkeypatch):
    w1 = graph
    g = ground_base(w1)
    g["frames"] = site["result"](w1)["frames"]
    fixture = ground_graph(w1, b18, 237)
    assert g == fixture
    assert canon_sha(with_power(g, 400)) == "a7e92f0cedec74b369d232918e12cf4bcd25e98e92409726648295c311b27a0b"
    assert canon_sha(g) == "f0354c47fb7c9bc78023a3cf8ae68f42ff5c7f4373b581d8ade774895abb465f"

    def never(*args, **kwargs):
        raise AssertionError("expanded")
    monkeypatch.setattr(codec, "expand_graph", never)
    sdg._reset_validation_caches()
    assert sdg.validate_graph(g) == g


@pytest.mark.parametrize("n,scale_sha,own_sha", [
    (2, "ef1bae34e7de39a6f3902e389b84f98f154c5dc45c901f4c8ac18d4278316b4c",
     "2af3e4e2e7c31aa2192b67498ef3feabac483d96da30344333475579bab61b3e"),
    (20, "8f1ba0024fb00a0d5d807dc4b9ecc413809389e7138c5a71fa05892aee59f582",
     "49353c4d094510cf79b4e1b8f6d1ccde6bc607d0af300576f52ecb792db8b06b"),
], ids=["two-trackers", "twenty-trackers"])
def test_ground_conversion_kernel_expansion_is_the_scale_record(site, b18, graph, n, scale_sha, own_sha):
    w1 = graph
    g = ground_base(w1)
    g["frames"] = site["result"](w1)["frames"][:n]
    expanded = codec.expand_graph(g)
    assert expanded == codec.expand_graph(ground_graph(w1, b18, n))
    assert canon_sha(with_power(expanded, 400)) == scale_sha
    assert canon_sha(expanded) == own_sha
    sdg._reset_validation_caches()
    assert sdg.validate_graph(expanded) == expanded


OVERLAPS = [
    ("same-command", [drawn(0.0, 0.0, 0.0, 6.0, 3), drawn(0.0, 0.0, 0.0, 6.0, 3)], (3, 6, 6, 6, 0)),
    ("other-command", [drawn(0.0, 0.0, 0.0, 6.0, 3), drawn(0.0, 0.0, 0.0, 6.0, 3, command="LEAFSAT")], (3, 6, 6, 0, 0)),
    ("three-copies", [drawn(0.0, 0.0, 0.0, 6.0, 3)] * 3, (3, 9, 0, 0, 9)),
    ("no-command", [drawn(0.0, 0.0, 0.0, 6.0, 3, command=None, block=None, tilt=None)] * 2, (3, 6, 6, 0, 0)),
]


@pytest.mark.parametrize("name,rows,expected", OVERLAPS, ids=[row[0] for row in OVERLAPS])
def test_ground_conversion_kernel_overlaps_are_kept_and_reported(graph, name, rows, expected):
    result = convert(graph, small_doc(rows))
    assert result["counts"] == {"trackers": len(rows), "slots": 3 * len(rows)}
    assert len({frame["id"] for frame in result["frames"]}) == len(rows)
    assert tuple(result["overlap"].values()) == expected


@pytest.mark.parametrize("budget", [1000, 1], ids=["thousand", "one"])
def test_ground_conversion_kernel_grid_bound_keeps_the_counts(site, graph, monkeypatch, budget):
    import solar_ground_dsteps as dsteps
    state = site["state"]
    layout = dsteps.tracker_panel_layout(conv.tracker_entities(state), 1.0, state["settings"])
    result = site["result"](graph)
    commands = [frame["tracker"]["source"]["source_command"] for frame in result["frames"]]
    monkeypatch.setattr(conv, "MAX_GRID_CELLS", budget)
    assert conv.overlap_report(layout["trackers"], commands) == result["overlap"]


def _doc_with(**state_change):
    doc = small_doc()
    doc["state"] = dict(doc["state"], **state_change)
    return doc


def _graph(change):
    def build(w1):
        g = ground_base(w1)
        change(g, w1)
        return g
    return build


REFUSALS = [
    ("head-missing", {"view": None}, "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED"),
    ("document-missing", {"doc": "none"}, "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED"),
    ("view-malformed", {"view": {"index": 0}}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("view-artifact-not-hex", {"view": {"index": 0, "state": {"artifact_id": "B" * 64, "content_sha256": "d" * 64}}},
     "GROUND_CONVERSION_INPUT_INVALID"),
    ("document-units-unknown", {"doc_units": {"drawing_units": "m", "meters_per_unit": 0.5}}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("rev-negative", {"rev": -1}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("rev-bool", {"rev": True}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("rev-over-bound", {"rev": 1000001}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("provenance-missing-created-by", {"prov_drop": "created_by"}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("provenance-source-rev-string", {"prov_set": {"source_rev": "0"}}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("provenance-oversized", {"prov_set": {"tool_id": "x" * 20000}}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("graph-no-source-hash", {"graph": _graph(lambda g, w1: g.pop("source_hash"))}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("roof-project", {"graph": _graph(lambda g, w1: g["project"].update(installation_design="Roof"))},
     "GROUND_CONVERSION_GROUND_PROJECT_REQUIRED"),
    ("graph-units-inches", {"graph": _graph(lambda g, w1: g["project"].update(units=deepcopy(w1["project"]["units"])))},
     "GROUND_CONVERSION_UNITS_MISMATCH"),
    ("document-units-feet", {"doc": small_doc(units="ft")}, "GROUND_CONVERSION_UNITS_MISMATCH"),
    ("no-tracker-rows-key", {"doc": ps.physical_document({"frames": []}, drawing_units="m", source_sha256=SOURCE,
                                                          capability="trackers-to-panelgroups")},
     "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED"),
    ("empty-tracker-rows", {"doc": small_doc([])}, "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED"),
    ("zero-slot-rows", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 0)])}, "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED"),
    ("frames-not-list", {"doc": _doc_with(frames={})}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("nan-axis", {"doc": _doc_with(tracker_rows=[dict(SMALL_ROWS[0], axis_end=[0.0, float("nan")])])},
     "GROUND_CONVERSION_INPUT_INVALID"),
    ("zero-width", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, width=0.0)])}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("tilt-over-90", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, tilt=95.0)])}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("source-command-over-64", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, command="X" * 65)])},
     "GROUND_CONVERSION_INPUT_INVALID"),
    ("block-not-string", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, block=7)])}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("axis-beyond-plan-bound", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 2e9, 3)])}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("width-over-1000-m", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 6.0, 3, width=1001.0)])}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("row-length-over-bound", {"doc": small_doc([dict(drawn(0.0, 0.0, 0.0, 6.0, 3), row_length_m=200000.0)])},
     "GROUND_CONVERSION_INPUT_INVALID"),
    ("stored-group-number-string", {"doc": small_doc(settings={"PanelGroupNumber": "1"})}, "GROUND_CONVERSION_INPUT_INVALID"),
    ("frame-over-10000-slots", {"doc": small_doc([drawn(0.0, 0.0, 0.0, 20000.0, 10001)])}, "GROUND_CONVERSION_LIMIT_EXCEEDED"),
    ("site-over-100000-slots", {"doc": small_doc([drawn(10.0 * i, 0.0, 10.0 * i, 10000.0, 10000) for i in range(11)])},
     "GROUND_CONVERSION_LIMIT_EXCEEDED"),
]


@pytest.mark.parametrize("name,case,code", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_ground_conversion_kernel_refusals(graph, name, case, code):
    view = case.get("view", VIEW)
    doc = case.get("doc", small_doc())
    if doc == "none":
        doc = None
    if "doc_units" in case:
        doc = dict(doc, units=case["doc_units"])
    g = case["graph"](graph) if "graph" in case else ground_base(graph)
    prov = provenance()
    if "prov_drop" in case:
        del prov[case["prov_drop"]]
    prov.update(case.get("prov_set", {}))
    rev = case.get("rev", 0)
    snapshot = deepcopy((view, doc, g, prov))
    refused(code, conv.convert_physical_state, view, doc, g, provenance=prov, rev=rev)
    assert repr((view, doc, g, prov)) == repr(snapshot)


@pytest.mark.parametrize("field", ["block", "source_command"])
def test_ground_conversion_kernel_tracker_text_surrogate(graph, field):
    row = dict(SMALL_ROWS[0], **{field: "\ud800"})
    refused("GROUND_CONVERSION_INPUT_INVALID", convert, graph, _doc_with(tracker_rows=[row]))


@pytest.mark.parametrize("change", [
    {"tool_id": "x" * 4097},
    {"source_handle": "x" * 4097},
    {"catalog_versions": {"modules": "x" * 4097}},
    {"created_by": "\ud800"},
], ids=["tool-id-over-bound", "source-handle-over-bound", "catalog-version-over-bound", "created-by-surrogate"])
def test_ground_conversion_kernel_provenance_text_bounds(graph, change):
    prov = provenance()
    prov.update(change)
    refused("GROUND_CONVERSION_INPUT_INVALID", convert, graph, prov=prov)


def test_ground_conversion_kernel_axis_coordinate_beyond_plan_bound(graph):
    refused("GROUND_CONVERSION_INPUT_INVALID", convert, graph,
            small_doc([drawn(2e9, 0.0, 2e9, 6.0, 3)]))


def test_ground_conversion_kernel_site_limit_refuses_before_layout(graph, monkeypatch):
    calls = []

    def layout(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(conv.dsteps, "tracker_panel_layout", layout)
    doc = next(case["doc"] for name, case, code in REFUSALS if name == "site-over-100000-slots")
    refused("GROUND_CONVERSION_LIMIT_EXCEEDED", convert, graph, doc)
    assert calls == []


@pytest.mark.parametrize("change", [
    {"source_hash": "oops"},
    {"source_hash": "x" * 4097},
    {"parameters": []},
    {"parameters": None},
    {"created_at": "oops"},
    {"catalog_versions": {"x" * 4097: "v1"}},
    {"parameters": {"nested": ["x" * 4097]}},
], ids=["source-hash-invalid", "source-hash-over-bound", "parameters-list", "parameters-null",
        "created-at-invalid", "catalog-version-key-over-bound", "nested-parameter-over-bound"])
def test_ground_conversion_kernel_provenance_schema_and_nested_text_bounds(graph, change):
    prov = provenance()
    prov.update(change)
    refused("GROUND_CONVERSION_INPUT_INVALID", convert, graph, prov=prov)


def test_ground_conversion_kernel_provenance_deep_parameters(graph):
    nested = []
    for _ in range(3000):
        nested = [nested]
    prov = provenance()
    prov["parameters"] = {"nested": nested}
    refused("GROUND_CONVERSION_INPUT_INVALID", convert, graph, prov=prov)


def test_ground_conversion_kernel_inputs_untouched_outputs_unaliased(graph, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the kernel must not validate or expand")
    monkeypatch.setattr(codec, "expand_graph", never)
    monkeypatch.setattr(sdg, "validate_graph", never)
    doc, g, prov = small_doc(), ground_base(graph), provenance()
    snapshot = repr((doc, g, prov))
    result = conv.convert_physical_state(VIEW, doc, g, provenance=prov, rev=0)
    assert repr((doc, g, prov)) == snapshot
    result["frames"][0]["provenance"]["created_by"] = "changed"
    result["frames"][0]["ground_slots"]["panel"]["provenance"]["created_by"] = "changed"
    assert prov == provenance() and result["frames"][1]["provenance"] == prov
    assert canon(convert(graph)) == canon(conv.convert_physical_state(VIEW, small_doc(), ground_base(graph),
                                                                      provenance=provenance(), rev=0))


def test_ground_conversion_kernel_ids_follow_the_graph_source_hash(graph):
    other = ground_base(graph)
    other["source_hash"] = "e" * 64
    first = convert(graph)["frames"][0]
    moved = convert(graph, g=other)["frames"][0]
    assert first["id"] == "leaf:frame:25d4df7a-231a-4b42-ac8d-9afa94bdabca"
    assert moved["id"] == "leaf:frame:0ba88211-7dfa-4193-8681-d5ff032f8e10"
    assert codec.decode_slots(first["ground_slots"]).ids != codec.decode_slots(moved["ground_slots"]).ids


def test_ground_conversion_kernel_feet(graph):
    g = ground_base(graph)
    g["project"]["units"] = dict(g["project"]["units"], drawing_units="ft", meters_per_unit=0.3048)
    result = convert(graph, small_doc(units="ft"), g)
    tracker = result["frames"][0]["tracker"]
    assert (tracker["length_m"], tracker["cross_axis_width_m"]) == (1.8288000000000002, 0.6096)
    assert canon_sha(result) == "451c6e6f55d3910d2a77371a16f2d49335440da01114637f0b592a41dd8d8a55"


def test_ground_conversion_kernel_slot_bounds_are_inclusive(graph):
    one = convert(graph, small_doc([drawn(0.0, 0.0, 0.0, 20000.0, 10000)]))
    assert one["counts"] == {"trackers": 1, "slots": 10000}
    full = convert(graph, small_doc([drawn(10.0 * i, 0.0, 10.0 * i, 10000.0, 10000) for i in range(10)]))
    assert full["counts"] == {"trackers": 10, "slots": 100000}


def test_ground_conversion_kernel_current_head_empty(backend, graph):
    refused("GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED", conv.convert_current_ground_state, backend, TENANT, DRAWING,
            ground_base(graph), project_id=PROJECT, provenance=provenance(), rev=0)


def test_ground_conversion_kernel_current_head_binds_the_published_state(backend, graph):
    published = ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    result = conv.convert_current_ground_state(backend, TENANT, DRAWING, ground_base(graph), project_id=PROJECT,
                                               provenance=provenance(), rev=0)
    head = published["head"]
    assert result["source"] == {"head_index": 0, "state_artifact_id": head["state"]["artifact_id"],
                                "state_content_sha256": head["state"]["content_sha256"]}
    view, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert result == conv.convert_physical_state(view, document, ground_base(graph), provenance=provenance(), rev=0)
    assert result["frames"] == convert(graph)["frames"]


def test_ground_conversion_kernel_current_head_errors_pass_through(backend, graph):
    ph.publish_physical_state(backend, TENANT, DRAWING, small_doc())
    with pytest.raises(ph.PhysicalHeadError) as error:
        conv.convert_current_ground_state(backend, TENANT, DRAWING, ground_base(graph),
                                          project_id="leaf:project:00000000-0000-4000-8000-000000000009",
                                          provenance=provenance(), rev=0)
    assert error.value.code == "PHYSICAL_HEAD_PROJECT_MISMATCH"


def test_ground_conversion_kernel_b18_through_the_head(backend, site, graph):
    ph.publish_physical_state(backend, TENANT, DRAWING, site["document"])
    result = conv.convert_current_ground_state(backend, TENANT, DRAWING, ground_base(graph),
                                               project_id=PROJECT, provenance=provenance(), rev=0)
    assert result["frames"] == site["result"](graph)["frames"]
    assert result["overlap"] == site["result"](graph)["overlap"]
