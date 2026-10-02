"""Equipment intent follows string edits; only inverters own the equipment refresh."""
import copy

import pytest

from test_solar_ground_admission import admission_base, admission_equipped, _run, lsha
from test_solar_ground_equipment import (
    graph, service, pinned, converted, sized, strung, equip, slots,
    EQUIPPED_SHA, FixedDatetime, app_id,
)
from test_w1_equipment import case
from test_w1_solve_commit import transfer

import solar_equipment
from solar_design_graph import GraphValidationError


VALID = {"state": "valid", "reasons": []}
STALE = {"state": "stale", "reasons": ["upstream_corrected"]}
INVALID = {"state": "invalid", "reasons": ["upstream_corrected"]}


def requests(g):
    return g["extra"]["equipment"]["assignment_requests"]


def refresh(g, monkeypatch):
    return _run("solar-assign-equipment", g, {
        "expected_rev": g["rev"],
        "equipment": [{key: copy.deepcopy(item[key]) for key in solar_equipment.FIELDS}
                      for item in g["inverters"]],
        "assignments": copy.deepcopy(requests(g)),
    }, monkeypatch)


def delete(g, refs, monkeypatch):
    return _run("solar-string-delete", g, {
        "operation": "delete-strings", "expected_rev": g["rev"], "string_refs": refs,
    }, monkeypatch)


def swap(g, monkeypatch):
    return _run("solar-string-swap", g, {
        "operation": "swap-strings", "expected_rev": g["rev"],
        "string_refs": [s["id"] for s in g["strings"]],
    }, monkeypatch)


def assert_assignments(g, expected):
    assert requests(g) == expected
    assert g["inverters"][0]["input_assignments"] == [
        {key: request[key] for key in ("string_ref", "mppt_letter", "input_number")}
        for request in expected]


def test_delete_prunes_intent(admission_equipped, monkeypatch):
    g = admission_equipped
    out = delete(g, [g["strings"][1]["id"]], monkeypatch)
    assert out["rev"] == 6
    assert [s["id"] for s in out["strings"]] == [g["strings"][0]["id"]]
    assert [s["validity"] for s in out["strings"]] == [VALID]
    assert_assignments(out, requests(g)[:1])
    assert out["inverters"][0]["validity"] == STALE
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "1a0ee71a1dca1ada345b71c9d6303dca0182d333d5741b628d88f88b1a4539ef"


def test_equipment_refreshes_after_delete(admission_equipped, monkeypatch):
    g = admission_equipped
    deleted = delete(g, [g["strings"][1]["id"]], monkeypatch)
    out = refresh(deleted, monkeypatch)
    assert out["rev"] == 7
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "51b7f60a45c39381351495a35c80ccc24d477b3fd738d8479502e331a460f405"


def test_delete_every_string_prunes_every_request(admission_equipped, monkeypatch):
    out = delete(admission_equipped, [s["id"] for s in admission_equipped["strings"]], monkeypatch)
    assert out["rev"] == 6 and out["strings"] == []
    assert_assignments(out, [])
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "883fd4b7fbc4ec88fd356d6a938c2373dd567572b23e4b529b27d1c3d55fb37a"


def test_swap_moves_intent_with_inputs(admission_equipped, monkeypatch):
    out = swap(admission_equipped, monkeypatch)
    expected = copy.deepcopy(requests(admission_equipped))
    expected[0]["string_ref"], expected[1]["string_ref"] = (
        expected[1]["string_ref"], expected[0]["string_ref"])
    assert out["rev"] == 6
    assert_assignments(out, expected)
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "431260a936d6a14c1a051fca131c6aafd39b38fd9b5483baef8ff409ab434d41"


def test_equipment_preserves_swapped_inputs(admission_equipped, monkeypatch):
    swapped = swap(admission_equipped, monkeypatch)
    out = refresh(swapped, monkeypatch)
    assert out["rev"] == 7
    assert_assignments(out, requests(swapped))
    assert [a["string_ref"] for a in out["inverters"][0]["input_assignments"]] == [
        s["id"] for s in admission_equipped["strings"]][::-1]
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "940bfce6712abd1679433186efc78e3ea18021a5a23a707c2eb8ead497648970"


def test_correction_keeps_intent_and_stales_dependents(admission_equipped, monkeypatch):
    out = _run("solar-correct-string", admission_equipped, transfer(admission_equipped), monkeypatch)
    assert out["rev"] == 6
    assert [s["module_count"] for s in out["strings"]] == [1, 4]
    assert [s["validity"] for s in out["strings"]] == [STALE, STALE]
    assert out["inverters"][0]["validity"] == STALE
    assert requests(out) == requests(admission_equipped)
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "542294be9b0437a1b43aa64490d78ca03c1cdef2eedfb5205d1b6f95351cdf02"


def test_equipment_refreshes_inverter_but_not_corrected_strings(admission_equipped, monkeypatch):
    corrected = _run("solar-correct-string", admission_equipped, transfer(admission_equipped), monkeypatch)
    out = refresh(corrected, monkeypatch)
    assert out["rev"] == 7
    assert [s["validity"] for s in out["strings"]] == [INVALID, INVALID]
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "7ceb862c374bd50854ebac569e9dff8447ed815cdf14328330177fa71bced9c2"


@pytest.mark.parametrize("tool,digest", [
    ("solar-string-flip", "f5351b8f233ed5ebe7329007d239f3cfde8725024005b177bb4dadd9394a0359"),
    ("solar-string-conductors", "ce6c9fd3f1bd6d4bc0fcc5f7ae53fc5f7ae69e57733cfeb2022d82d499348fda"),
], ids=["flip", "conductors"])
def test_compatible_edits_keep_readiness(admission_equipped, monkeypatch, tool, digest):
    refs = [s["id"] for s in admission_equipped["strings"]]
    params = {"expected_rev": 5}
    if tool == "solar-string-flip":
        params.update(operation="flip-string", string_ref=refs[0])
    else:
        params.update(operation="set-conductors",
                      assignments=[{"string_ref": ref, "wire_gauge": "8 AWG"} for ref in refs])
    out = _run(tool, admission_equipped, params, monkeypatch)
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == digest


@pytest.mark.parametrize("index", [0, 1], ids=["first-string", "second-string"])
def test_rooftop_schedule_still_refuses_delete(case, monkeypatch, index):
    graph, params, _ = case
    g = solar_equipment.equipment_candidate(graph, params)
    before = copy.deepcopy(g)
    assert g["rev"] == 1
    with pytest.raises(GraphValidationError) as exc:
        delete(g, [g["strings"][index]["id"]], monkeypatch)
    assert exc.value.code == "STRING_REFERENCE_NOT_CLEARED"
    assert g == before and requests(g) == requests(before)


@pytest.mark.parametrize("previous,reasons,refreshed,expected", [
    (STALE, [], solar_equipment.INVERTER_REFRESHED_REASONS, VALID),
    (STALE, [], frozenset(), INVALID),
    ({"state": "invalid", "reasons": ["upstream_failure"]}, [],
     solar_equipment.INVERTER_REFRESHED_REASONS,
     {"state": "invalid", "reasons": ["upstream_failure"]}),
    ({"state": "stale", "reasons": []}, [], solar_equipment.INVERTER_REFRESHED_REASONS,
     {"state": "stale", "reasons": []}),
    ({"state": "stale", "reasons": ["upstream_corrected", "EQUIPMENT_POWER_EXCEEDED"]},
     ["EQUIPMENT_POWER_EXCEEDED"], solar_equipment.INVERTER_REFRESHED_REASONS,
     {"state": "invalid", "reasons": ["EQUIPMENT_POWER_EXCEEDED"]}),
    ({"state": "stale", "reasons": ["upstream_corrected", "upstream_failure"]}, [],
     solar_equipment.INVERTER_REFRESHED_REASONS,
     {"state": "invalid", "reasons": ["upstream_failure"]}),
], ids=["inverter-refreshed", "string-default", "failure-retained", "unexplained-stale",
        "power-still-exceeded", "mixed-upstream"])
def test_validity_refresh_ownership(previous, reasons, refreshed, expected):
    entity = {"validity": copy.deepcopy(previous)}
    if refreshed:
        solar_equipment._validity(entity, reasons, refreshed)
    else:
        solar_equipment._validity(entity, reasons)
    assert entity["validity"] == expected


def test_delete_keeps_surviving_request_exactly_in_order(admission_equipped, monkeypatch):
    # Swap first so the survivor uses input 1 rather than being implicitly renumbered.
    g = swap(admission_equipped, monkeypatch)
    expected = copy.deepcopy(requests(g)[1:])
    out = delete(g, [requests(g)[0]["string_ref"]], monkeypatch)
    assert requests(out) == expected
    assert [(r["mppt_letter"], r["input_number"]) for r in requests(out)] == [("A", 1)]


def test_delete_keeps_several_survivors_in_their_stored_order(admission_equipped, monkeypatch):
    g = copy.deepcopy(admission_equipped)
    r0 = requests(g)[0]
    r1 = requests(g)[1]
    x = copy.deepcopy(r0)
    x["string_ref"] = "survivor-x"
    y = copy.deepcopy(r0)
    y["string_ref"] = "survivor-y"
    g["extra"]["equipment"]["assignment_requests"] = copy.deepcopy([r0, x, r1, y])
    out = delete(g, [g["strings"][1]["id"]], monkeypatch)
    assert requests(out) == [r0, x, y]
    assert requests(out) != list(reversed([r0, x, y]))


def test_only_inverter_call_receives_refresh_reasons(admission_equipped, monkeypatch):
    g = delete(admission_equipped, [admission_equipped["strings"][1]["id"]], monkeypatch)
    original = solar_equipment._validity
    calls = []

    def recording(entity, reasons, refreshed=frozenset()):
        calls.append((entity["kind"], refreshed))
        return original(entity, reasons, refreshed)

    monkeypatch.setattr(solar_equipment, "_validity", recording)
    solar_equipment.equipment_candidate(g, {
        "expected_rev": g["rev"],
        "equipment": [{key: item[key] for key in solar_equipment.FIELDS} for item in g["inverters"]],
        "assignments": requests(g),
    })
    assert [value for kind, value in calls if kind == "inverter"] == [
        solar_equipment.INVERTER_REFRESHED_REASONS]
    assert [value for kind, value in calls if kind == "string"] == [frozenset()] * len(g["strings"])


def test_rooftop_refusal_keeps_stored_requests(case, monkeypatch):
    graph, params, _ = case
    g = solar_equipment.equipment_candidate(graph, params)
    before = copy.deepcopy(requests(g))
    with pytest.raises(GraphValidationError) as exc:
        delete(g, [g["strings"][0]["id"]], monkeypatch)
    assert exc.value.code == "STRING_REFERENCE_NOT_CLEARED"
    assert requests(g) == before
