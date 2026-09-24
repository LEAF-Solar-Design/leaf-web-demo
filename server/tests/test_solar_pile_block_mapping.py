"""Tests for server/solar_pile_block_mapping.py (G36 LEAFPILEBLOCKMAP: select, read back, upsert, store save)."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "solar_pile_block_mapping.py"
_SPEC = importlib.util.spec_from_file_location("solar_pile_block_mapping", _PATH)
eng = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eng)

FORM = {"source_row": 0, "save_mapping": 1, "confirm_ok": 1, "close": 1}
BOUNDS = [0.9144000000000001, 1.2192, 1.524, 1.8288000000000002, 2.1336]


def length(value):
    return {"kind": "length", "unit": "m", "value": value}


def fields(**changes):
    value = {k: v for k, v in eng.new_template("x").items() if k != "Name"}
    value.update(changes)
    value["PilesPerFrame"] = 0
    return value


def saved_source_template():
    template = eng.default_template({"template_name": "source-1", "local_width_m": 38.112, "local_height_m": 14.492})
    template = eng.read_back(template, {"template_name": "source-1", "local_width_m": 38.112,
                                        "local_height_m": 14.492})
    return {k: v for k, v in eng.serialised(template).items() if k != "Name"}


def intake(source_template=True, mapping_record=None):
    templates = []
    if source_template:
        templates.append({"template": "source-1", "fields": saved_source_template()})
    templates += [{"template": "template-1", "fields": fields(HorizontalDistancesM=[1.0, 1.0, 1.0],
                                                             RevealBucketBoundariesM=BOUNDS * 2)},
                  {"template": "template-2", "fields": fields(RevealBucketBoundariesM=BOUNDS * 3)}]
    return {"format": "pile-block-map-intake-v1", "units": "m", "mapping_record": mapping_record,
            "pile_store": {"active_template": "template-2", "templates": templates},
            "sources": [{"source": "source-1", "source_kind": "Block", "count": 138, "has_pvcase_piling": False,
                         "status": "unmapped", "local_width": length(38.112), "local_height": length(14.492)},
                        {"source": "source-2", "source_kind": "Native", "count": 12, "has_pvcase_piling": False,
                         "status": "unmapped", "local_width": None, "local_height": None}]}


def test_saving_row_zero_maps_it_with_even_end_and_bearing_stations():
    rows = eng.evidence_rows(intake(), FORM)
    assert [f["status_after"] for _, f in rows["pile-source"]] == ["mapped", "unmapped"]
    (row_id, mapping), = rows["pile-block-mapping"]
    assert row_id == "pile-block-mapping-1" and mapping["user_override"] is True and mapping["source"] == "source-1"
    assert mapping["local_width"] == length(38.112) and mapping["pvcase_template_digest"] is None
    template = json.loads(mapping["template"])
    assert template["Name"] == "source-1" and template["PilesPerFrame"] == 4
    assert [(s["OffsetM"], s["Kind"], s["Label"]) for s in template["Stations"]] == [
        (0.0, "End", "1"), (12.704, "Bearing", "2"), (25.408, "Bearing", "3"), (38.112, "End", "4")]
    assert template["RevealBucketBoundariesM"] == BOUNDS and template["HorizontalPoleCount"] == 4


def test_the_store_keeps_every_template_as_saved():
    rows = eng.evidence_rows(intake(), FORM)
    assert "pile-template" not in rows                      # no boundary copies appended, source-1 unchanged
    assert rows["report"] == [("report-active-template", {"name": "active-template", "value": "template-2"}),
                              ("report-templates", {"name": "templates", "value": 3})]


def test_a_changed_source_template_is_replaced_field_by_field():
    doc = intake()
    doc["pile_store"]["templates"][0]["fields"]["PileDiameterM"] = 0.3
    rows = eng.evidence_rows(doc, FORM)
    assert rows["pile-template"] == [("pile-template-1", {"template": "source-1", "field": "PileDiameterM",
                                                          "change": "changed", "value": "0.0"})]


def test_a_recorded_mapping_is_reloaded_and_read_back_through_the_grid():
    template = eng.default_template({"template_name": "source-1", "local_width_m": 10.0, "local_height_m": 2.0})
    template["Stations"][1]["OffsetM"] = 3.33349
    template["PileDiameterM"] = 0.21449
    record = [{"source": "source-1", "source_kind": "Block", "user_override": True,
               "template": eng.canonical_text(eng.serialised(template))}]
    doc = intake(mapping_record=record)
    doc["sources"][0]["status"] = "mapped"
    rows = eng.evidence_rows(doc, FORM)
    saved = json.loads(rows["pile-block-mapping"][0][1]["template"])
    assert saved["Stations"][1]["OffsetM"] == 3.333 and saved["PileDiameterM"] == 0.214


def test_saving_twice_is_the_same_as_saving_once():
    once = eng.evidence_rows(intake(), FORM)
    twice = eng.evidence_rows(intake(), dict(FORM, save_mapping=2))
    assert once == twice


def test_closing_without_saving_changes_nothing():
    rows = eng.evidence_rows(intake(), dict(FORM, save_mapping=0))
    assert "pile-block-mapping" not in rows and "pile-template" not in rows
    assert [f["status_after"] for _, f in rows["pile-source"]] == ["unmapped", "unmapped"]


def test_format_and_station_helpers_follow_the_plugin():
    assert eng.format_3(12.7045) == 12.705 and eng.format_3(1 / 3) == 0.333 and eng.format_3(38.112) == 38.112
    single = eng.even_station_template("n", 10.0, 1, 0.0, 0.0, "LocalX", False)
    assert [(s["OffsetM"], s["Kind"]) for s in single["Stations"]] == [(5.0, "End")]
    assert eng.source_length({"local_width_m": 0.0, "local_height_m": 3.0}) == 3.0


@pytest.mark.parametrize("mutate", [
    lambda d, f: d.update(units="in"),
    lambda d, f: d["sources"][0].update(has_pvcase_piling=True),
    lambda d, f: d["sources"][0].update(local_width=None),
    lambda d, f: f.update(source_row=5),
    lambda d, f: f.update(extra=1),
    lambda d, f: d["pile_store"].update(active_template="missing"),
    lambda d, f: d["pile_store"]["templates"].pop(0),          # the saved template is new: its sort place is unknown
    lambda d, f: d["pile_store"]["templates"][1]["fields"].pop("Stations"),
])
def test_malformed_or_uncarried_inputs_refuse(mutate):
    doc, form = intake(), dict(FORM)
    mutate(doc, form)
    with pytest.raises(eng.PileMappingError):
        eng.evidence_rows(doc, form)


def test_the_intake_is_not_mutated():
    doc = intake()
    before = copy.deepcopy(doc)
    eng.evidence_rows(doc, FORM)
    assert doc == before
