"""Offline checks for the Studio electrical-zone producer on a synthetic intake."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


producer = load_module("solar_w1_studio_zones")
adapter = load_module("solar_studio_evidence")
ROOT = Path(__file__).resolve().parents[1]
# Any committed file binds the synthetic intake to a fixture; no rooftop capture is read here.
FIXTURE = ROOT / "contract" / "solar-design-graph.v1.schema.json"
WIDTH, HEIGHT = 77.0, 38.5
# Two islands far apart: a 2 x 2 block at the origin and a row of three 5000 in east.
BLOCK_A = {"1A": (0.0, 0.0), "1B": (79.0, 0.0), "1C": (0.0, 40.5), "1D": (79.0, 40.5)}
BLOCK_B = {"2A": (5000.0, 0.0), "2B": (5079.0, 0.0), "2C": (5158.0, 0.0)}
# Each window holds one whole block, the way a plugin operator drags a selection.
ZONES = ["Zone A:1:-100,-100,200,100", "Zone B:5:4900,-100,5300,100"]


def rectangle(handle, centre, layer="Panels", width=WIDTH, height=HEIGHT):
    cx, cy = centre
    hw, hh = width / 2, height / 2
    return {"handle": handle, "layer": layer, "closed": True,
            "pts": [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]]}


def intake(fixture=FIXTURE):
    polylines = [rectangle(handle, centre) for handle, centre in {**BLOCK_A, **BLOCK_B}.items()]
    # Not a panel layer, and not rectangular: the kernel selects neither.
    polylines.append(rectangle("3F", (2500.0, 0.0), layer="Outline", width=6000.0, height=400.0))
    polylines.append({"handle": "3E", "layer": "Panels", "closed": True,
                      "pts": [[0.0, 500.0], [77.0, 500.0], [38.5, 540.0]]})
    return {"source": {"dwg_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest()},
            "polylines": polylines}


def save(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def arguments(folder, *, zones=ZONES, document=None, fixture=FIXTURE, group=False,
              layer_contains="Panel", extra=()):
    path = save(folder / "intake.json", intake(fixture) if document is None else document)
    argv = ["--fixture", str(fixture), "--intake", str(path),
            "--out-graph", str(folder / "graph.json"), "--out-metadata", str(folder / "metadata.json"),
            "--layer-contains", layer_contains]
    for zone in zones:
        argv += ["--zone", zone]
    if group:
        argv += ["--group", "--branch-max-offset", "120", "--alignment-tolerance", "12"]
    return argv + list(extra)


def read(folder):
    graph = producer.deserialize_graph((folder / "graph.json").read_bytes())
    return graph, json.loads((folder / "metadata.json").read_text(encoding="utf-8"))


def handles(graph, refs):
    by_id = {panel["id"]: panel for panel in graph["panels"]}
    return [producer.normalized_handle(by_id[ref]["provenance"]["source_handle"]) for ref in refs]


@pytest.fixture(scope="module")
def zoned(tmp_path_factory):
    folder = tmp_path_factory.mktemp("studio-zones")
    assert producer.main(arguments(folder)) == 0
    graph, metadata = read(folder)
    return graph, metadata, folder


def test_reopened_graph_carries_both_zones_and_their_selected_panels(zoned):
    graph, _, _ = zoned
    assert producer.validate_graph(graph) == graph
    assert producer.deserialize_graph(producer.serialize_graph(graph)) == graph
    # Two zones created, then two assignments, each its own committed revision.
    assert graph["rev"] == 4
    assert sorted(handles(graph, [p["id"] for p in graph["panels"]])) == sorted({**BLOCK_A, **BLOCK_B})
    zones = graph["electrical_zones"]
    assert [zone["name"] for zone in zones] == ["Zone A", "Zone B"]
    assert [zone["color_index"] for zone in zones] == [1, 5]
    assert [handles(graph, zone["panel_refs"]) for zone in zones] == [
        ["1A", "1B", "1C", "1D"], ["2A", "2B", "2C"]]
    for zone in zones:
        assert zone["kind"] == "zone-el" and zone["boundary_ref"] is None
        assert (zone["module_model"], zone["inverter_model_a"]) == ("", "")
        assert zone["voc_cold"]["passes"] is None
    # No grouping was asked for, so nothing but the zones moved.
    assert graph["frames"] == [] and graph["strings"] == []
    for panel in graph["panels"]:
        assert panel["frame_ref"] is None and panel["matrix_cell"] is None


def test_metadata_records_the_joint_contract_parameters_and_identities(zoned):
    graph, metadata, folder = zoned
    assert metadata["fixture_sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert metadata["revision"] == producer.fixture_revision(FIXTURE.resolve())
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["revision"])
    assert "input_sha256" not in metadata
    # The joint contract fixes the zones family's parameters at the family alone.
    assert metadata["parameters"] == {"family": "zones"}
    assert metadata["versions"] == {
        "schema": "leaf.solar-w1-comparison.v1", "producer": "solar_w1_studio_zones.v1",
        "capability": "0", "engine": "server-builtin", "catalog": "none", "solver": "none"}
    assert metadata["coordinate_system"] == "world"
    assert metadata["geometry_units"] == "in" and metadata["angle_units"] == "deg"
    assert metadata["before"] == {"recorded": False}
    assert metadata["changes"] == {"created": [], "modified": [], "deleted": []}
    assert metadata["warnings"] == metadata["rejected_inputs"] == []
    assert metadata["elapsed_ms"] >= 0
    assert metadata["execution_mode"] == "live" and metadata["state"] == "committed"
    assert metadata["survived_reopen"] is metadata["synthetic_flagged"] is True
    assert "zones/panel-colour/not-in-graph" in metadata["fallback_fields"]
    provenance = metadata["provenance"]
    assert provenance["intake_sha256"] == hashlib.sha256((folder / "intake.json").read_bytes()).hexdigest()
    assert (provenance["zone_count"], provenance["panel_count"], provenance["group_count"]) == (2, 7, 0)
    assert provenance["zone_aware_grouping"] is False
    assert [zone["name"] for zone in provenance["zone_selection"]] == ["Zone A", "Zone B"]
    assert provenance["zone_selection"][0]["window"] == [-100.0, -100.0, 200.0, 100.0]
    mapping = metadata["entity_mapping"]
    # The receipt names the zones and their members only, so the map holds exactly those.
    assert set(mapping) == {zone["id"] for zone in graph["electrical_zones"]} | {
        ref for zone in graph["electrical_zones"] for ref in zone["panel_refs"]}
    assert [mapping[zone["id"]] for zone in graph["electrical_zones"]] == ["zone:Zone A", "zone:Zone B"]
    assert len(set(mapping.values())) == len(mapping)


def test_zones_evidence_validates_the_joint_contract(zoned):
    graph, metadata, _ = zoned
    originals = deepcopy((graph, metadata))
    evidence = adapter.build_evidence(graph, "zones", metadata)
    adapter.compare.validate_evidence(evidence, "zones")
    assert (graph, metadata) == originals
    assert evidence["input_sha256"] == adapter.compare.semantic_hash({
        "fixture_sha256": metadata["fixture_sha256"], "parameters": metadata["parameters"]})
    assert evidence["units"] == "in"
    zones = evidence["after"]["zones"]
    # A zone's name is committed state the plugin chose, so it is compared as given.
    assert [zone["name"] for zone in zones] == ["Zone A", "Zone B"]
    assert [[metadata["entity_mapping"][ref["entity_id"]] for ref in zone["membership"]]
            for zone in zones] == [["1A", "1B", "1C", "1D"], ["2A", "2B", "2C"]]
    # Rule Z7: creating a zone and assigning panels commits identity and membership
    # only, so the equipment and sizing records are fallbacks here and belong to the
    # receipts of the capabilities that set them.
    for zone in zones:
        for field in ("boundaries", "elevation", "equipment_config", "sizing_provenance"):
            assert zone[field] is None
    for field in ("boundaries", "elevation"):
        assert "zones/" + field + "/unrecorded" in evidence["fallback_fields"]
    for field in ("equipment_config", "sizing_provenance"):
        assert "zones/" + field + "/not-part-of-this-capability" in evidence["fallback_fields"]
    # Nothing is lost: this side's raw zone fields are preserved under provenance,
    # keyed by zone neutral id, so a later capability can compare them.
    raw = evidence["provenance"]["zone_fields"]
    assert set(raw) == {"zone:Zone A", "zone:Zone B"}
    assert raw["zone:Zone A"]["module_model"] == ""
    assert raw["zone:Zone A"]["dc_ac_ratio"] == 0
    assert raw["zone:Zone A"]["panels_in_sequence"] == 0
    assert raw["zone:Zone A"]["voc_cold"]["passes"] is None
    assert "group_names" not in evidence["provenance"]
    assert evidence["execution_mode"] == "live" and evidence["synthetic_flagged"] is True


def test_zone_aware_grouping_commits_one_group_set_per_zone(tmp_path):
    assert producer.main(arguments(tmp_path, group=True)) == 0
    graph, metadata = read(tmp_path)
    assert producer.validate_graph(graph) == graph
    assert graph["rev"] == 5
    zone_ids = {zone["name"]: zone["id"] for zone in graph["electrical_zones"]}
    frames = graph["frames"]
    assert [frame["name"] for frame in frames] == ["group-1A", "group-2A"]
    assert [frame["electrical_zone_ref"] for frame in frames] == [zone_ids["Zone A"], zone_ids["Zone B"]]
    assert [f["provenance"]["electrical_zone_name"] for f in frames] == ["Zone A", "Zone B"]
    assert [(f["module_rows"], f["module_columns"]) for f in frames] == [(2, 2), (1, 3)]
    assert [set(handles(graph, f["panel_refs"])) for f in frames] == [set(BLOCK_A), set(BLOCK_B)]
    for frame in frames:
        assert set(frame["panel_refs"]) <= set(
            next(z for z in graph["electrical_zones"] if z["id"] == frame["electrical_zone_ref"])["panel_refs"])
        assert frame["provenance"]["licensed_matrix"] == "kernel"
    assert metadata["provenance"]["group_count"] == 2
    assert metadata["provenance"]["zone_aware_grouping"] is True
    # A zones receipt names no group, so the groups this run built stay out of the map.
    assert not any(frame["id"] in metadata["entity_mapping"] for frame in frames)
    assert adapter.build_evidence(graph, "zones", metadata)["after"]["zones"][0]["name"] == "Zone A"


def test_a_zone_without_a_window_is_created_empty(tmp_path):
    # LEAFADDZONE alone: two zones created, saved and reopened, nothing assigned.
    assert producer.main(arguments(tmp_path, zones=["Zone A:1", "Zone B:3"])) == 0
    graph, metadata = read(tmp_path)
    assert producer.validate_graph(graph) == graph
    # Two zone creations and no assignment, so two committed revisions.
    assert graph["rev"] == 2
    zones = graph["electrical_zones"]
    assert [(zone["name"], zone["color_index"]) for zone in zones] == [("Zone A", 1), ("Zone B", 3)]
    assert all(zone["panel_refs"] == [] for zone in zones)
    assert graph["frames"] == []
    provenance = metadata["provenance"]
    assert provenance["zones_without_window"] == ["Zone A", "Zone B"]
    assert [zone["window"] for zone in provenance["zone_selection"]] == [None, None]
    assert metadata["entity_mapping"] == {zones[0]["id"]: "zone:Zone A", zones[1]["id"]: "zone:Zone B"}


def test_a_windowed_zone_that_selects_nothing_is_still_refused_beside_a_windowless_one(tmp_path, capsys):
    argv = arguments(tmp_path, zones=["Zone A:1", "Zone C:3:900000,900000,900100,900100"])
    assert producer.main(argv) == 2
    assert "zone window selected no panel" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_zone_aware_groups_metadata_builds_groups_evidence_with_one_group_per_zone(tmp_path):
    out = tmp_path / "groups-metadata.json"
    assert producer.main(arguments(tmp_path, group=True, extra=["--out-groups-metadata", str(out)])) == 0
    graph, zones_metadata = read(tmp_path)
    metadata = json.loads(out.read_text(encoding="utf-8"))
    # Rule G1: the four grouping parameters, recorded as given; the ledger's version.
    assert metadata["parameters"] == {"family": "groups", "layer_filter": "*Panel*",
                                      "branch_max_offset": 120.0, "alignment_tolerance": 12.0,
                                      "installation_design": "Roof"}
    assert metadata["versions"]["capability"] == "0" and metadata["versions"]["solver"] == "none"
    # Rule G8: every frame plus exactly the panels it grouped.
    frames = graph["frames"]
    assert set(metadata["entity_mapping"]) == {f["id"] for f in frames} | {
        ref for f in frames for ref in f["panel_refs"]}
    # The zones metadata is untouched by the groups run.
    assert zones_metadata["parameters"] == {"family": "zones"}
    evidence = adapter.build_evidence(graph, "groups", metadata)
    adapter.compare.validate_evidence(evidence, "groups")
    groups = evidence["after"]["groups"]
    assert [group["name"] for group in groups] == ["group:1A", "group:2A"]
    assert [[metadata["entity_mapping"][ref["entity_id"]] for ref in group["membership"]]
            for group in groups] == [["1A", "1B", "1C", "1D"], ["2A", "2B", "2C"]]


def test_groups_metadata_without_zone_aware_grouping_is_refused(tmp_path, capsys):
    out = tmp_path / "groups-metadata.json"
    assert producer.main(arguments(tmp_path, extra=["--out-groups-metadata", str(out)])) == 2
    assert "groups metadata requires --group" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists() and not out.exists()


def test_the_mapping_covers_the_zones_and_their_members_and_no_other_panel(tmp_path):
    # The licensed capture maps the zones and their assigned panels only. A panel
    # on the panel layer that no window covers is in the drawing and in the graph,
    # and it is not in the map, because no zones receipt can reference it.
    document = intake()
    document["polylines"].append(rectangle("4A", (0.0, 900.0)))
    assert producer.main(arguments(tmp_path, document=document)) == 0
    graph, metadata = read(tmp_path)
    by_handle = {handles(graph, [panel["id"]])[0]: panel["id"] for panel in graph["panels"]}
    assert set(by_handle) == set(BLOCK_A) | set(BLOCK_B) | {"4A"}
    zones = graph["electrical_zones"]
    assigned = [ref for zone in zones for ref in zone["panel_refs"]]
    mapping = metadata["entity_mapping"]
    assert set(mapping) == set(assigned) | {zone["id"] for zone in zones}
    assert len(mapping) == len(assigned) + len(zones) == 9
    assert by_handle["4A"] not in mapping
    # The evidence still builds: it references zones and their members and nothing else.
    evidence = adapter.build_evidence(graph, "zones", metadata)
    assert [[mapping[ref["entity_id"]] for ref in zone["membership"]]
            for zone in evidence["after"]["zones"]] == [["1A", "1B", "1C", "1D"], ["2A", "2B", "2C"]]


def test_overlapping_windows_leave_each_panel_in_the_last_zone(tmp_path):
    # Zone B's window covers the top row of Zone A's block; the builtin removes
    # those panels from Zone A first, exactly as LEAFZONEASSIGNPANELS does.
    overlapping = ["Zone A:1:-100,-100,200,100", "Zone B:5:-100,20,200,100"]
    assert producer.main(arguments(tmp_path, zones=overlapping)) == 0
    graph, _ = read(tmp_path)
    assert [handles(graph, zone["panel_refs"]) for zone in graph["electrical_zones"]] == [
        ["1A", "1B"], ["1C", "1D"]]
    members = [ref for zone in graph["electrical_zones"] for ref in zone["panel_refs"]]
    assert len(members) == len(set(members))


def test_fixture_hash_mismatch_fails_before_output(tmp_path, capsys):
    document = intake()
    document["source"]["dwg_sha256"] = "0" * 64
    assert producer.main(arguments(tmp_path, document=document)) == 2
    assert "fixture hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
    assert not (tmp_path / "metadata.json").exists()


def test_a_window_that_selects_no_panel_is_refused(tmp_path, capsys):
    argv = arguments(tmp_path, zones=ZONES + ["Zone C:3:900000,900000,900100,900100"])
    assert producer.main(argv) == 2
    assert "zone window selected no panel" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_a_duplicate_zone_name_is_refused(tmp_path, capsys):
    argv = arguments(tmp_path, zones=ZONES + ["zone a:3:-100,-100,200,100"])
    assert producer.main(argv) == 2
    assert "zone names must be unique" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


def test_zone_aware_grouping_requires_the_kernel_settings(tmp_path, capsys):
    assert producer.main(arguments(tmp_path, extra=["--group"])) == 2
    assert "positive finite number for zone-aware grouping" in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()


@pytest.mark.parametrize("spec,message", [
    ("Zone A", "NAME:COLOR"),
    ("Zone A:1:0,0,10", "zone window must be x0,y0,x1,y1"),
    ("Zone A:x:0,0,10,10", "zone colour must be an integer"),
    ("Zone A:300:0,0,10,10", "zone colour must be an integer"),
    (":1:0,0,10,10", "zone name must be nonempty"),
    ("Zone A:1:0,0,0,10", "positive area"),
    ("Zone A:1:0,0,nan,10", "four finite numbers"),
])
def test_a_malformed_zone_specification_is_refused(tmp_path, capsys, spec, message):
    assert producer.main(arguments(tmp_path, zones=[spec])) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "graph.json").exists()
