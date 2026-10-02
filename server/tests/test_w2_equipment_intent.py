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
    assert lsha(out) == "cdf2dcc14e65c2729a1e22e7731e5fddce39b4b50c23430d6e9b0da1a18ceeac"


def test_equipment_refreshes_after_delete(admission_equipped, monkeypatch):
    g = admission_equipped
    deleted = delete(g, [g["strings"][1]["id"]], monkeypatch)
    out = refresh(deleted, monkeypatch)
    assert out["rev"] == 7
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "800bb11c6d374fc68f65972feac12046ed7f31b1ed6a96c9e12af332f621b625"


def test_delete_every_string_prunes_every_request(admission_equipped, monkeypatch):
    out = delete(admission_equipped, [s["id"] for s in admission_equipped["strings"]], monkeypatch)
    assert out["rev"] == 6 and out["strings"] == []
    assert_assignments(out, [])
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "41e742be6b5dd0d93d61293b39d455273fbb536b70f2effc4d112f56e0a41e62"


def test_swap_moves_intent_with_inputs(admission_equipped, monkeypatch):
    out = swap(admission_equipped, monkeypatch)
    expected = copy.deepcopy(requests(admission_equipped))
    expected[0]["string_ref"], expected[1]["string_ref"] = (
        expected[1]["string_ref"], expected[0]["string_ref"])
    assert out["rev"] == 6
    assert_assignments(out, expected)
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "4c2fb1101859a7a3a1c1d6a9d1e6f74bd7caef90a6e25ea032c3ce47494b3b2c"


def test_equipment_preserves_swapped_inputs(admission_equipped, monkeypatch):
    swapped = swap(admission_equipped, monkeypatch)
    out = refresh(swapped, monkeypatch)
    assert out["rev"] == 7
    assert_assignments(out, requests(swapped))
    assert [a["string_ref"] for a in out["inverters"][0]["input_assignments"]] == [
        s["id"] for s in admission_equipped["strings"]][::-1]
    assert solar_equipment.equipment_ready(out) is True
    assert lsha(out) == "d56e44425fe021cd9a37092f5817bdd1d84542566161a03f8b303f7a17e91820"


def test_correction_keeps_intent_and_stales_dependents(admission_equipped, monkeypatch):
    out = _run("solar-correct-string", admission_equipped, transfer(admission_equipped), monkeypatch)
    assert out["rev"] == 6
    assert [s["module_count"] for s in out["strings"]] == [1, 4]
    assert [s["validity"] for s in out["strings"]] == [STALE, STALE]
    assert out["inverters"][0]["validity"] == STALE
    assert requests(out) == requests(admission_equipped)
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "1cb21ed2b109bb675b748a66affccce77c9655389438ae50334fc99a76f17988"


def test_equipment_refreshes_inverter_but_not_corrected_strings(admission_equipped, monkeypatch):
    corrected = _run("solar-correct-string", admission_equipped, transfer(admission_equipped), monkeypatch)
    out = refresh(corrected, monkeypatch)
    assert out["rev"] == 7
    assert [s["validity"] for s in out["strings"]] == [INVALID, INVALID]
    assert out["inverters"][0]["validity"] == VALID
    assert solar_equipment.equipment_ready(out) is False
    assert lsha(out) == "119b130f05137fb02938468159a62d851d992d1dcc34353212e1082672fbc35b"


@pytest.mark.parametrize("tool,digest", [
    ("solar-string-flip", "fe7dffe7d096ef0f1aceba10a66d7843337b13477f863eae4fd2d17fe9d3506f"),
    ("solar-string-conductors", "4fd107efd85035f3ab9ed8ff01edc0d7c41d4a92248510a8947e975ada48cd81"),
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
