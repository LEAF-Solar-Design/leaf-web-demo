"""Local string-inverter candidates for the existing drawing transaction.

Input numbers are zero-based within each named MPPT. Rejected capacity requests
remain in drawing-owned intent; they never become invalid graph references.
"""
import copy
import math
from datetime import datetime, timezone

from solar_design_graph import GraphValidationError, _bounded_json, new_id, validate_graph
from solar_sizing_client import advance, checked_graph, require_sizing


FIELDS = {"id", "number", "type_key", "model", "position", "rotation", "scale",
          "block_name", "layer", "mppt_count", "total_dc_inputs", "mppt_inputs",
          "max_dc_voltage", "max_ac_power_kw", "max_dc_power_kw", "is_solaredge"}


def _number(value, low=0, high=1000000):
    return type(value) in (int, float) and low < value <= high


def _configuration(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise GraphValidationError("INVALID_EQUIPMENT_CONFIGURATION")
    for key in ("number", "mppt_count", "total_dc_inputs"):
        if type(value[key]) is not int or not 1 <= value[key] <= 4096:
            raise GraphValidationError("INVALID_EQUIPMENT_CONFIGURATION")
    for key in ("type_key", "model", "block_name", "layer"):
        if type(value[key]) is not str or not value[key].strip() or len(value[key]) > 255:
            raise GraphValidationError("INVALID_EQUIPMENT_CONFIGURATION")
    if (len(value["type_key"]) > 16 or type(value["is_solaredge"]) is not bool
            or type(value["id"]) is not str):
        raise GraphValidationError("INVALID_EQUIPMENT_CONFIGURATION")
    capacities = value["mppt_inputs"]
    if (type(capacities) is not dict or len(capacities) != value["mppt_count"]
            or any(not label.strip() or len(label) > 32 or type(count) is not int
                   or not 1 <= count <= 4096 for label, count in capacities.items())
            or sum(capacities.values()) != value["total_dc_inputs"]):
        raise GraphValidationError("INVALID_EQUIPMENT_CAPACITY")
    for key in ("max_dc_voltage", "max_ac_power_kw", "max_dc_power_kw"):
        if not _number(value[key]):
            raise GraphValidationError("INVALID_EQUIPMENT_LIMIT")
    for key in ("position", "scale"):
        vector = value[key]
        if (type(vector) is not list or len(vector) != 3
                or any(type(n) not in (int, float) or abs(n) > 1000000 for n in vector)
                or (key == "scale" and any(n == 0 for n in vector))):
            raise GraphValidationError("INVALID_EQUIPMENT_TRANSFORM")
    if type(value["rotation"]) not in (int, float) or abs(value["rotation"]) > 1000000:
        raise GraphValidationError("INVALID_EQUIPMENT_TRANSFORM")


def _validity(entity, reasons):
    previous = entity["validity"]
    retained = [reason for reason in previous["reasons"] if not reason.startswith("EQUIPMENT_")]
    unresolved = previous["state"] != "valid" and not previous["reasons"]
    entity["validity"] = {**previous, "reasons": retained + sorted(set(reasons)),
                          "state": "invalid" if retained or reasons else
                          previous["state"] if unresolved else "valid"}


def equipment_candidate(graph, params):
    """Return one isolated revision; the broker owns DWG creation and publication."""
    _bounded_json(params)
    if (type(params) is not dict
            or set(params) - {"expected_rev", "equipment", "assignments", "cancel", "preview"}
            or any(type(params.get(k, False)) is not bool for k in ("cancel", "preview"))):
        raise GraphValidationError("INVALID_EQUIPMENT_REQUEST")
    result = checked_graph(graph, params.get("expected_rev"))
    if params.get("cancel", False):
        return result
    require_sizing(result)
    equipment, requests = params.get("equipment"), params.get("assignments")
    if (type(equipment) is not list or not 1 <= len(equipment) <= 128
            or type(requests) is not list or len(requests) > 100000):
        raise GraphValidationError("INVALID_EQUIPMENT_REQUEST")
    previous = {item["id"]: item for item in result["inverters"]}
    inverters, numbers = {}, set()
    for config in equipment:
        _configuration(config)
        ref = config["id"] or new_id("inverter")
        if ref in inverters or config["number"] in numbers:
            raise GraphValidationError("DUPLICATE_EQUIPMENT")
        numbers.add(config["number"])
        item = copy.deepcopy(previous.get(ref))
        if item is None:
            item = {"id": ref, "kind": "inverter", "rev": result["rev"], "extra": {},
                    "validity": {"state": "valid", "reasons": []},
                    "provenance": {"created_by": "solar-assign-equipment",
                                   "created_at": datetime.now(timezone.utc).isoformat(),
                                   "last_writer": "solar-assign-equipment", "source_rev": result["rev"]}}
        item.update(copy.deepcopy({key: value for key, value in config.items() if key != "id"}))
        item.update(is_l2=False, input_assignments=[])
        inverters[ref] = item
    if not set(previous) <= set(inverters):
        raise GraphValidationError("EQUIPMENT_REMOVAL_UNSUPPORTED")
    result["inverters"] = list(inverters.values())
    strings = {s["id"]: s for s in result["strings"]}
    issues = {ref: [] for ref in strings}
    inverter_issues = {ref: [] for ref in inverters}
    seen, occupied = set(), set()
    for string in strings.values():
        string["inverter_ref"] = None
        string["to_ref"] = None
    for request in requests:
        if (type(request) is not dict
                or set(request) != {"string_ref", "inverter_ref", "mppt_letter", "input_number"}
                or any(type(request[k]) is not str for k in ("string_ref", "inverter_ref", "mppt_letter"))
                or type(request["input_number"]) is not int or not 0 <= request["input_number"] <= 1000000):
            raise GraphValidationError("INVALID_EQUIPMENT_ASSIGNMENT")
        ref, inverter_ref = request["string_ref"], request["inverter_ref"]
        if ref not in strings or inverter_ref not in inverters or ref in seen:
            raise GraphValidationError("INVALID_EQUIPMENT_ASSIGNMENT")
        seen.add(ref)
        item = inverters[inverter_ref]
        slot = (inverter_ref, request["mppt_letter"], request["input_number"])
        if (request["input_number"] >= item["mppt_inputs"].get(request["mppt_letter"], 0)
                or slot in occupied):
            issues[ref].append("EQUIPMENT_INPUT_CAPACITY_EXCEEDED")
            inverter_issues[inverter_ref].append("EQUIPMENT_INPUT_CAPACITY_EXCEEDED")
            continue
        occupied.add(slot)
        item["input_assignments"].append({k: request[k] for k in ("string_ref", "mppt_letter", "input_number")})
        strings[ref].update(inverter_ref=inverter_ref, to_ref=inverter_ref)
    panels = {p["id"]: p for p in result["panels"]}
    frames = {f["id"]: f for f in result["frames"]}
    zones = {z["id"]: z for z in result["electrical_zones"]}
    mode = result["settings"]["extra"]["string_sizing"]["mode"]
    power = {ref: 0 for ref in inverters}
    for ref, string in strings.items():
        inverter_ref = string["inverter_ref"]
        if inverter_ref is None:
            issues[ref].append("EQUIPMENT_UNASSIGNED")
            continue
        item = inverters[inverter_ref]
        voltage = 0
        if not string["ordered_panel_refs"]:
            issues[ref].append("EQUIPMENT_EMPTY_STRING")
        for panel_ref in string["ordered_panel_refs"]:
            frame = frames.get(panels[panel_ref]["frame_ref"])
            target = result["settings"] if mode == "global" else zones.get(frame["electrical_zone_ref"] if frame else None)
            if target is None or not _number(target["voc_cold"].get("per_module")):
                issues[ref].append("EQUIPMENT_VOLTAGE_UNKNOWN")
            else:
                voltage += target["voc_cold"]["per_module"]
            if frame is None or not _number(frame["module_power_watts"]):
                issues[ref].append("EQUIPMENT_POWER_UNKNOWN")
            else:
                power[inverter_ref] += frame["module_power_watts"] / 1000
        if voltage > item["max_dc_voltage"]:
            issues[ref].append("EQUIPMENT_VOLTAGE_EXCEEDED")
        inverter_issues[inverter_ref].extend(issues[ref])
    for ref, item in inverters.items():
        if power[ref] > item["max_dc_power_kw"] and not math.isclose(power[ref], item["max_dc_power_kw"], rel_tol=1e-12):
            inverter_issues[ref].append("EQUIPMENT_POWER_EXCEEDED")
            for assignment in item["input_assignments"]:
                issues[assignment["string_ref"]].append("EQUIPMENT_POWER_EXCEEDED")
        _validity(item, inverter_issues[ref])
    for ref, string in strings.items():
        _validity(string, issues[ref])
    inputs = {a["string_ref"]: (item["id"], a["input_number"])
              for item in inverters.values() for a in item["input_assignments"]}
    for frame in result["frames"]:
        records = frame["panel_assignments"] + [c for row in frame["matrix"] for c in row if c["panel_ref"] is not None]
        for record in records:
            string_ref = panels[record["panel_ref"]]["assignment"]["string_ref"]
            record["inverter_id"], record["string_input_number"] = inputs.get(string_ref, (None, None))
    for derived in result["routes"] + result["schedules"]:
        derived["validity"] = {"state": "stale", "reasons": ["equipment_changed"]}
    result["extra"]["equipment"] = {"assignment_requests": copy.deepcopy(requests)}
    changed = result["inverters"] + result["strings"] + result["frames"] + result["routes"] + result["schedules"]
    return advance(result, changed, "solar-assign-equipment")


def equipment_ready(graph):
    """Recompute readiness from persisted intent and limits after reopening."""
    graph = validate_graph(graph)
    configs = [{key: item.get(key) for key in FIELDS} for item in graph["inverters"]]
    try:
        candidate = equipment_candidate(graph, {"expected_rev": graph["rev"], "equipment": configs,
                                               "assignments": graph["extra"]["equipment"]["assignment_requests"]})
    except (GraphValidationError, KeyError):
        return False
    if (any(old["input_assignments"] != new["input_assignments"]
            for old, new in zip(graph["inverters"], candidate["inverters"]))
            or any(old["inverter_ref"] != new["inverter_ref"]
                   for old, new in zip(graph["strings"], candidate["strings"]))):
        return False
    return bool(candidate["strings"]) and all(
        item["validity"]["state"] == "valid"
        for item in [candidate["project"], candidate["settings"]] + candidate["electrical_zones"]
        + candidate["frames"] + candidate["panels"] + candidate["strings"] + candidate["inverters"])
