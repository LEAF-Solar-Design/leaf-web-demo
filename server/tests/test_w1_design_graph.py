"""Public W1 graph coverage and correction invariants, with no network calls."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_design_graph import (  # noqa: E402
    GraphValidationError, deserialize_graph, load_schema, new_id,
    require_revision, serialize_graph, validate_graph,
)
from solar_dependencies import affected_entities, membership_changes  # noqa: E402
import solar_design_graph as sdg  # noqa: E402


def app_id(kind, number):
    return f"leaf:{kind}:00000000-0000-4000-8000-{number:012d}"


def entity(kind, id_number, **fields):
    return {
        "id": app_id(kind, id_number), "kind": kind, "rev": 0,
        "provenance": {
            "created_by": "fixture", "created_at": "2026-09-17T00:00:00Z",
            "last_writer": "fixture", "source_rev": 0, "source_hash": "a" * 64,
            "catalog_versions": {"modules": "fixture-v1"}, "source_handle": "A1",
        },
        "extra": {}, "validity": {"state": "valid", "reasons": []}, **fields,
    }


@pytest.fixture
def graph():
    cold = {"passes": True, "override_accepted": False, "suggested_string_length": 2,
            "per_module": 50.0, "string_voltage": 100.0, "max_dc_voltage": 600.0}
    units = {
        "drawing_units": "in", "meters_per_unit": 0.0254, "source": "drawing_marker",
        "compute_units": "m", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "elevation_datum": "local roof", "crs": None,
        "drawing_unit_is_feet": False, "warnings": [],
    }
    panels = [entity(
        "panel", n, frame_ref=app_id("frame", 1), matrix_cell={"row": 0, "col": n - 1},
        centre=[n, 0], angle=0,
        assignment={"string_ref": app_id("string", 1 if n < 3 else 2), "seq": n - 1 if n < 3 else 0},
    ) for n in (1, 2, 3)]
    strings = [entity(
        "string", n, circuit_tag=f"S{n}", circuit_kind="String",
        ordered_panel_refs=[panel["id"] for panel in panels if panel["assignment"]["string_ref"] == app_id("string", n)],
        module_count=2 if n == 1 else 1, from_ref=panels[0 if n == 1 else 2]["id"],
        to_ref=app_id("inverter", 1), tag_text_ref=None, wire_gauge="10 AWG",
        length_ft=10, route=[[0, 0], [1, 0]], inverter_ref=app_id("inverter", 1),
    ) for n in (1, 2)]
    frame = entity(
        "frame", 1, name="Roof group", insertion_point=[0, 0, 0], installation_design="Roof",
        panel_refs=[p["id"] for p in panels], module_rows=1, module_columns=3,
        module_slots=3, module_power_watts=400, module_width_along_row=1,
        module_height_across_row=2, electrical_zone_ref=app_id("zone-el", 1),
        matrix=[[{"code": "panel", "panel_ref": p["id"], "seq": p["assignment"]["seq"],
                  "inverter_id": app_id("inverter", 1), "string_input_number": 1 if p is panels[2] else 0,
                  "x": p["centre"][0], "y": 0, "angle": 0} for p in panels]],
        sequences=[{"string_ref": s["id"], "ordered_panel_refs": s["ordered_panel_refs"][:]} for s in strings],
        panel_assignments=[{"panel_ref": p["id"], **p["assignment"],
                            "inverter_id": app_id("inverter", 1), "string_input_number": 1 if p is panels[2] else 0} for p in panels],
    )
    return {
        "graph_schema_version": 1, "rev": 0, "parent_rev": None,
        "source_hash": "a" * 64, "catalog_versions": {"modules": "fixture-v1", "inverters": "fixture-v2"},
        "project": entity("project", 1, name="Synthetic rooftop", zip_code="00000",
                          latitude=None, longitude=None, installation_design="Roof", units=units,
                          graph_schema_version=1, site_revision="fixture-site-1"),
        "settings": entity(
            "settings", 1, panel_layer_contains="Panels", panel_group_layer="Groups",
            string_layer="Strings", home_run_layer="Homeruns", panels_in_sequence=2,
            num_mppt=1, strings_per_mppt=2, optimizer_ratio=1, use_l2_collectors=False,
            panel_group_number=2, string_number=3, inverter_number=2, mppt_letter="A",
            global_string_sizing_confirmed=True, voc_cold=copy.deepcopy(cold),
        ),
        "electrical_zones": [entity("zone-el", 1, name="Roof", color_index=1,
                                    panel_refs=[p["id"] for p in panels], module_model="fixture-module",
                                    inverter_model_a="fixture-inverter", inverter_count_a=1,
                                    panels_in_sequence=2, dc_ac_ratio=1.2, voc_cold=cold, boundary_ref=None)],
        "frames": [frame], "panels": panels, "strings": strings,
        "inverters": [entity("inverter", 1, number=1, type_key="A", is_l2=False,
                              position=[5, 0], model="fixture-inverter", mppt_count=1,
                              total_dc_inputs=2, max_dc_voltage=600, max_ac_power_kw=1,
                              is_solaredge=False,
                              input_assignments=[{"string_ref": s["id"], "mppt_letter": "A", "input_number": n}
                                                 for n, s in enumerate(strings)])],
        "routes": [entity("route", 1, route_kind="start homerun", points=[[0, 0], [5, 0]],
                           from_ref=strings[0]["id"], to_ref=app_id("inverter", 1),
                           wire_gauge="10 AWG", length_ft=16.4042, point_units="m", length_units="ft")],
        "schedules": [entity("schedule", 1, rows=[["S1", 2], ["S2", 1]],
                              headers=["Circuit", "Modules"], insertion_point=[10, 10],
                              layer="LEAF-SCHEDULES", source_rev=0,
                              source_refs=[s["id"] for s in strings], column_units=[None, "count"])],
        "opaque_stores": {"unmodeled": {"payload_ref": "opaque:fixture", "sha256": "b" * 64, "byte_length": 7}},
        "orphaned_xdata": [{"payload_ref": "opaque:orphan", "sha256": "c" * 64, "byte_length": 9}],
        "extra": {},
    }


def test_schema_and_w1_fixture(graph):
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(graph)
    assert validate_graph(graph) == graph
    assert deserialize_graph(serialize_graph(graph)) == graph


def test_unknown_fields_provenance_and_opaque_refs_survive(graph):
    graph["future_root"] = {"nested": [False, None, {"label": "untouched"}]}
    graph["panels"][0]["extra"] = {"unmodeled": {"number": 3}}
    graph["panels"][0]["assignment"]["future_assignment"] = [1, 2]
    graph["panels"][0]["matrix_cell"]["future_cell"] = True
    graph["settings"]["voc_cold"]["future_cold"] = "retained"
    graph["schedules"][0]["validity"] = {"state": "stale", "reasons": ["source_changed"]}
    restored = deserialize_graph(serialize_graph(graph))
    assert restored == graph
    restored["panels"][0]["extra"]["unmodeled"]["number"] = 4
    assert graph["panels"][0]["extra"]["unmodeled"]["number"] == 3


@pytest.mark.parametrize("field,value", [
    ("drawing_units", "unknown"), ("meters_per_unit", None),
    ("meters_per_unit", 1e-12), ("compute_units", "in"),
    ("wcs_to_ucs", []), ("elevation_datum", ""),
])
def test_unknown_units_refuse(graph, field, value):
    graph["project"]["units"][field] = value
    with pytest.raises(GraphValidationError, match="UNKNOWN_UNITS"):
        validate_graph(graph)


def test_missing_scale_refuses(graph):
    del graph["project"]["units"]["meters_per_unit"]
    with pytest.raises(GraphValidationError, match="UNKNOWN_UNITS"):
        serialize_graph(graph)


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_unsupported_versions_refuse(graph, version):
    graph["graph_schema_version"] = version
    with pytest.raises(GraphValidationError, match="UNSUPPORTED_SCHEMA_VERSION"):
        validate_graph(graph)


def test_panel_transfer_changes_both_memberships_and_dependents(graph):
    after = copy.deepcopy(graph)
    after["rev"], after["parent_rev"] = 1, 0
    moved = after["panels"][1]
    old_string, new_string = after["strings"]
    old_string["ordered_panel_refs"].remove(moved["id"])
    new_string["ordered_panel_refs"].append(moved["id"])
    old_string["module_count"], new_string["module_count"] = 1, 2
    moved["assignment"] = {"string_ref": new_string["id"], "seq": 1}
    frame = after["frames"][0]
    frame["panel_assignments"][1].update(moved["assignment"])
    frame["panel_assignments"][1]["string_input_number"] = 1
    frame["matrix"][0][1]["string_input_number"] = 1
    frame["sequences"] = [{"string_ref": s["id"], "ordered_panel_refs": s["ordered_panel_refs"][:]} for s in after["strings"]]
    changes = membership_changes(graph, after)
    assert set(changes) == {old_string["id"], new_string["id"]}
    assert (changes[old_string["id"]]["before_count"], changes[old_string["id"]]["after_count"]) == (2, 1)
    assert (changes[new_string["id"]]["before_count"], changes[new_string["id"]]["after_count"]) == (1, 2)
    affected = affected_entities(graph, after, [moved["id"]])
    assert set(affected) == {moved["id"], old_string["id"], new_string["id"],
                             app_id("inverter", 1), app_id("route", 1), app_id("schedule", 1)}
    assert graph["strings"][0]["module_count"] == 2
    assert after["opaque_stores"] == graph["opaque_stores"]
    assert after["project"] == graph["project"]


def test_stale_job_and_revision_chain(graph):
    assert require_revision(graph, 0) == graph
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        require_revision(graph, 1)
    graph["rev"], graph["parent_rev"] = 2, 0
    with pytest.raises(GraphValidationError, match="INVALID_REVISION_CHAIN"):
        validate_graph(graph)


@pytest.mark.parametrize("defect,code", [
    ("count", "STRING_COUNT_MISMATCH"), ("assignment", "PANEL_ASSIGNMENT_MISMATCH"),
    ("handle", "INVALID_GRAPH_SCHEMA"), ("duplicate", "DUPLICATE_APPLICATION_ID"),
    ("matrix", "MATRIX_SEQUENCE_MISMATCH"), ("input", "DUPLICATE_INVERTER_INPUT"),
])
def test_malformed_graph_refuses(graph, defect, code):
    if defect == "count":
        graph["strings"][0]["module_count"] = 7
    elif defect == "assignment":
        graph["panels"][0]["assignment"]["seq"] = 7
    elif defect == "handle":
        graph["panels"][0]["id"] = "A1"
    elif defect == "duplicate":
        graph["panels"].append(copy.deepcopy(graph["panels"][0]))
    elif defect == "matrix":
        graph["frames"][0]["matrix"][0][0]["seq"] = 7
    elif defect == "input":
        graph["inverters"][0]["input_assignments"][1]["input_number"] = 0
    with pytest.raises(GraphValidationError, match=code):
        validate_graph(graph)


def test_json_bounds_and_duplicate_keys(graph):
    with pytest.raises(GraphValidationError, match="DUPLICATE_JSON_KEY"):
        deserialize_graph('{"graph_schema_version":1,"graph_schema_version":2}')
    graph["extra"]["bad"] = float("nan")
    with pytest.raises(GraphValidationError, match="NONFINITE_NUMBER"):
        validate_graph(graph)
    graph["extra"] = nested = {}
    for _ in range(34):
        nested["child"] = {}
        nested = nested["child"]
    with pytest.raises(GraphValidationError, match="GRAPH_LIMIT_EXCEEDED"):
        validate_graph(graph)


def test_ids_are_application_owned():
    first, second = new_id("panel"), new_id("panel")
    assert first != second
    Draft202012Validator(load_schema()["$defs"]["id"]).validate(first)


class _CountingValidator:
    """Delegates to a real validator and counts full-schema passes."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def is_valid(self, instance):
        return self.inner.is_valid(instance)

    def iter_errors(self, instance):
        self.calls += 1
        return self.inner.iter_errors(instance)


class TestValidationMemo:
    @pytest.fixture(autouse=True)
    def _fresh_caches(self):
        sdg._reset_validation_caches()
        yield
        sdg._reset_validation_caches()

    def _count_full_validator(self, monkeypatch):
        units, full = sdg._schema_validators()
        counter = _CountingValidator(full)
        monkeypatch.setattr(sdg, "_SCHEMA_VALIDATORS", (units, counter))
        return counter

    def test_repeat_returns_equal_isolated_copies(self, graph):
        first = validate_graph(graph)
        second = validate_graph(graph)
        assert first == second == graph
        assert first is not second and first is not graph and second is not graph
        first["extra"]["mutated"] = True
        second["panels"][0]["extra"]["mutated"] = 1
        third = validate_graph(graph)
        assert third == graph
        assert "mutated" not in third["extra"] and "mutated" not in third["panels"][0]["extra"]
        assert "mutated" not in graph["extra"]

    def test_identical_graph_skips_schema_and_changed_graph_reruns(self, graph, monkeypatch):
        counter = self._count_full_validator(monkeypatch)
        validate_graph(graph)
        assert counter.calls == 1
        validate_graph(copy.deepcopy(graph))
        assert counter.calls == 1
        graph["extra"]["note"] = "changed"
        validate_graph(graph)
        assert counter.calls == 2

    def test_key_order_shares_one_memo_entry(self, graph, monkeypatch):
        counter = self._count_full_validator(monkeypatch)
        validate_graph(graph)
        reordered = dict(reversed(list(graph.items())))
        assert list(reordered) != list(graph)
        assert validate_graph(reordered) == graph
        assert counter.calls == 1
        assert len(sdg._VALIDATED_GRAPH_MEMO) == 1

    def test_refusal_is_repeated_and_never_memoized(self, graph, monkeypatch):
        counter = self._count_full_validator(monkeypatch)
        graph["strings"][0]["module_count"] = 7
        for attempt in range(3):
            with pytest.raises(GraphValidationError, match="STRING_COUNT_MISMATCH"):
                validate_graph(graph)
            assert counter.calls == attempt + 1
        assert len(sdg._VALIDATED_GRAPH_MEMO) == 0

    def test_memo_is_bounded(self, graph, monkeypatch):
        monkeypatch.setattr(sdg, "_VALIDATED_GRAPH_MEMO_MAX", 2)
        for number in range(5):
            graph["extra"]["n"] = number
            validate_graph(graph)
            assert len(sdg._VALIDATED_GRAPH_MEMO) <= 2
        assert len(sdg._VALIDATED_GRAPH_MEMO) == 2
        assert all(type(key) is bytes and len(key) == 32 for key in sdg._VALIDATED_GRAPH_MEMO)

    def test_bounds_check_runs_on_memo_hit(self, graph, monkeypatch):
        calls = []
        real = sdg._bounded_json

        def spy(value):
            calls.append(value)
            return real(value)

        monkeypatch.setattr(sdg, "_bounded_json", spy)
        validate_graph(graph)
        validate_graph(graph)
        assert len(calls) == 2
        assert len(sdg._VALIDATED_GRAPH_MEMO) == 1

    def test_schema_read_at_most_once(self, graph, monkeypatch):
        calls = []
        real = sdg.load_schema

        def spy():
            calls.append(1)
            return real()

        monkeypatch.setattr(sdg, "load_schema", spy)
        for number in range(4):
            graph["extra"]["n"] = number
            validate_graph(graph)
            validate_graph(graph)
        graph["strings"][0]["module_count"] = 7
        with pytest.raises(GraphValidationError, match="STRING_COUNT_MISMATCH"):
            validate_graph(graph)
        assert len(calls) == 1
