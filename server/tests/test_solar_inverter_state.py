"""Studio's G35 inverter state and step delta against the plugin adapter they port.

Covered: state validation and the bounded, duplicate-refusing load; the private pairing identity;
row order per kind; the delta (identical rows cancel, pairing by identity then by neutral keys,
removed, changed, added, ids in row order); the G20 setting records (absent is the declared default,
a nested value as canonical JSON, the route store's clock ignored); the per-save catalog
duplication (save_drawing_properties and save_noise_only); the report rules; and step_rows' emission
order. Every state is synthetic and authored here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load("solar_inverter_state", ROOT / "server" / "solar_inverter_state.py")


def device(x, y, role="inverter", placement=None, hardware=None, scale=2.0):
    return {"number": None, "role": role, "position": st.coordinate(x, y), "scale": scale,
            "rotation": st.angle(0.0), "placement": placement, "hardware": hardware,
            "_detail": {"type_key": "A", "is_l2": role != "combiner", "box_input_count": 0, "colour": 1}}


def string_row(handle, device_number=0):
    return {"string": handle, "device": device_number, "input": 1, "label": "-", "colour": 7,
            "_detail": {"circuit": "-"}}


def base_state(devices=(), strings=("A1", "A2"), settings=None):
    return {"format": st.STATE_FORMAT, "source": {"dump_sha256": "0" * 64, "reopened": True},
            "rows": {"device": [copy.deepcopy(d) for d in devices],
                     "string-assignment": [string_row(h) for h in strings],
                     "cable": [], "schedule": [], "lbd": []},
            "setting": dict(settings or {"HomerunRouting": copy.deepcopy(st.HOMERUN_ROUTING_DEFAULT)}),
            "geometry": {"strings": [], "panel_groups": []}}


# ------------------------------------------------------------------- load --

def test_validate_adds_a_private_pair_and_publish_removes_it():
    state = st.validate_state(base_state([device(1, 2)]))
    assert state["rows"]["device"][0]["_pair"] == "device:0"
    published = st.publish(state)
    assert "_pair" not in published["rows"]["device"][0]
    assert published["rows"]["device"][0]["_detail"]["colour"] == 1   # the adapter keeps _detail
    assert st.digest(published) == st.digest(base_state([device(1, 2)]))


def test_load_state_round_trips_and_refuses_duplicates(tmp_path):
    path = tmp_path / "state-i0.json"
    path.write_bytes(st.canonical(base_state([device(1, 2)])))
    assert st.load_state(path)["rows"]["device"][0]["position"]["value"] == [1.0, 2.0]
    path.write_text('{"format": "inverter-state-v1", "format": "x"}', encoding="utf-8")
    with pytest.raises(st.InverterStateError, match="repeats"):
        st.load_state(path)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(format="other"),
    lambda s: s["rows"].pop("lbd"),
    lambda s: s["rows"]["device"].append({"role": "inverter"}),
    lambda s: s["source"].update(reopened="yes"),
    lambda s: s["geometry"].update(strings=None),
    lambda s: s["rows"]["device"].append(dict(device(1, 2), position={"kind": "coordinate", "value": [1, float("nan")], "unit": "in"})),
])
def test_malformed_states_are_refused(mutate):
    state = base_state([device(1, 2)])
    mutate(state)
    with pytest.raises(st.InverterStateError):
        st.validate_state(state)


def test_new_pair_is_fresh_and_never_published():
    state = st.validate_state(base_state())
    assert st.new_pair(state, "device") == "device:new-1"
    assert st.new_pair(state, "device") == "device:new-2"
    assert "_created" not in st.publish(state)


# ------------------------------------------------------------------ order --

def test_devices_order_by_number_then_y_then_x():
    rows = [device(5, 1), device(1, 2), device(0, 1)]
    rows[0]["number"] = 2
    rows.sort(key=st.ORDER["device"])
    assert [r["position"]["value"] for r in rows] == [[5.0, 1.0], [0.0, 1.0], [1.0, 2.0]]


def test_strings_order_by_handle_value_and_cables_by_ends():
    rows = [string_row("1F"), string_row("A"), string_row(None)]
    rows.sort(key=st.ORDER["string-assignment"])
    assert [r["string"] for r in rows] == ["A", "1F", None]
    cables = [{"cable_kind": "feeder", "from": 2, "to": 1, "vertices": []},
              {"cable_kind": "dc-homerun", "from": "B", "to": 1, "vertices": []},
              {"cable_kind": "feeder", "from": 1, "to": 8, "vertices": []}]
    cables.sort(key=st.ORDER["cable"])
    assert [(c["cable_kind"], c["from"]) for c in cables] == [("dc-homerun", "B"), ("feeder", 1), ("feeder", 2)]


# ------------------------------------------------------------------ delta --

def test_identical_rows_cancel_and_added_rows_number_in_order():
    before = st.validate_state(base_state([device(1, 1)]))
    after = copy.deepcopy(before)
    after["rows"]["device"] += [dict(device(3, 5), _pair="x"), dict(device(2, 4), _pair="y")]
    rows = st.delta_rows(before, after)
    assert [(r["id"], r["fields"]["change"], r["fields"]["position"]["value"]) for r in rows["device"]] == \
        [("device-1", "added", [2.0, 4.0]), ("device-2", "added", [3.0, 5.0])]
    assert "string-assignment" not in rows


def test_an_edited_row_pairs_by_identity_and_is_changed():
    before = st.validate_state(base_state([device(1, 1), device(2, 2)]))
    after = copy.deepcopy(before)
    after["rows"]["device"][1].update(role="l2-inverter", placement="FIXED_L2")
    rows = st.delta_rows(before, after)["device"]
    assert len(rows) == 1 and rows[0]["fields"]["change"] == "changed"
    assert rows[0]["fields"]["role"] == "l2-inverter" and "_pair" not in rows[0]["fields"]


def test_a_recreated_row_pairs_by_its_neutral_key():
    before = st.validate_state(base_state([device(1, 1)]))
    after = copy.deepcopy(before)
    after["rows"]["device"][0].update(_pair="recreated", scale=9.0)
    rows = st.delta_rows(before, after)["device"]
    assert [(r["fields"]["change"], r["fields"]["scale"]) for r in rows] == [("changed", 9.0)]


def test_removed_rank_before_changed_before_added_at_one_position():
    before = st.validate_state(base_state([device(1, 1)]))
    after = copy.deepcopy(before)
    after["rows"]["device"] = [dict(device(1, 1, role="combiner"), _pair="new")]
    rows = st.delta_rows(before, after)["device"]
    # the neutral position key pairs them: one changed row, never an add plus a remove.
    assert [r["fields"]["change"] for r in rows] == ["changed"]
    after["rows"]["device"] = []
    assert [r["fields"]["change"] for r in st.delta_rows(before, after)["device"]] == ["removed"]


# --------------------------------------------------------------- settings --

def test_setting_records_absent_is_the_declared_default():
    assert st.setting_records({}, {"NumMppt": 3}) == []
    assert st.setting_records({}, {"NumMppt": 4}) == \
        [{"id": "setting-NumMppt", "key": None, "fields": {"name": "NumMppt", "value": 4}}]
    (record,) = st.setting_records({}, {"TrenchSnapDistanceM": 6.0})
    assert record["fields"]["value"] == {"kind": "length", "value": 6.0, "unit": "m"}
    (record,) = st.setting_records({"L1ToL2Assignments": {}}, {"L1ToL2Assignments": {"1": 2}})
    assert record["fields"]["value"] == '{"1":2}'
    assert st.setting_records({"X": 0}, {"X": 0.0}) == []


def test_route_store_clock_is_not_compared():
    old = {"UtilityCableRoutes": [{"Id": 1, "CreatedUtc": "a"}]}
    new = {"UtilityCableRoutes": [{"Id": 1, "CreatedUtc": "b"}]}
    assert st.setting_records(st.setting_map(old), st.setting_map(new)) == []


def test_save_appends_the_default_catalog_once_and_is_noise_only():
    state = st.validate_state(base_state())
    before = copy.deepcopy(state["setting"])
    st.save_drawing_properties(state)
    catalog = state["setting"]["HomerunRouting"]["CableCatalog"]
    assert catalog == st.DEFAULT_CATALOG * 2
    assert st.save_noise_only(before, state["setting"])
    changed = copy.deepcopy(state["setting"])
    changed["HomerunRouting"]["TrayCostMultiplier"] = 9.0
    assert not st.save_noise_only(before, changed)


# ----------------------------------------------------------------- report --

def test_report_message_rules():
    assert st.report_message("i10", ["x", "LEAFSKIDRECONCILE: MISMATCH."]) == "mismatch"
    assert st.report_message("i10", ["LEAFSKIDRECONCILE: RECONCILED."]) == "no-change"
    assert st.report_message("i4", []) == "no-imbalance"
    assert st.report_message("i99", None) == "no-change"


def test_step_rows_report_when_nothing_changed_else_delta_and_settings():
    before = st.validate_state(base_state([device(1, 1)]))
    rows, settings = st.step_rows("i4", before, copy.deepcopy(before), [])
    assert rows == [{"id": {"entity_id": "report-message"}, "type": "report", "quantity": 1, "unit": "each",
                     "name": "message", "value": "no-imbalance"}] and settings == []
    saved = st.save_drawing_properties(copy.deepcopy(before))
    rows, _ = st.step_rows("i10", before, saved, ["LEAFSKIDRECONCILE: MISMATCH."])
    assert [r["type"] for r in rows] == ["report", "setting"]   # catalog noise alone still reports
    after = copy.deepcopy(saved)
    after["rows"]["device"][0]["placement"] = "FIXED_L2"
    rows, settings = st.step_rows("i19", before, after, [])
    assert [(r["type"], r["id"]["entity_id"]) for r in rows] == [("device", "device-1"),
                                                                 ("setting", "setting-HomerunRouting")]
    assert json.loads(rows[1]["value"])["CableCatalog"] == st.DEFAULT_CATALOG * 2
    assert [s["id"] for s in settings] == ["setting-HomerunRouting"]


def test_semantic_hash_refuses_a_document_past_the_bounds():
    with pytest.raises(st.InverterStateError):
        st.semantic_hash({"rows": list(range(st.MAX_NODES + 1))})
