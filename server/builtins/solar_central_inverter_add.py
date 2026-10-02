"""Add one central inverter (an L2 collector) at a picked point: the plugin's ADDINVERTER with the equipment
choice "Central inverter" (StringHomeRunCmd, ported as solar_inverter_devices.inverter_add) on the design graph.

Before this tool nothing registered could create an L2: solar-assign-equipment writes every device as an L1,
solar-combiners and solar-feeders both require an L2 that already exists, and the device kernel had no caller.

The graph never meets the kernel directly. server/solar_electrical_state_bridge.py projects the graph's
equipment into the kernel's state, the kernel appends one L2 row at the point, and the bridge writes that row
back as a new inverter entity with the request's hardware.

Frozen decisions (sf-w2-central-inverter-add):
  - The point is in drawing units, the unit the kernel and the plugin's jig take; the stored position is
    [x * meters_per_unit, y * meters_per_unit] (the bridge's rule for a new device). The point is placed
    exactly where it is given: the client resolves object snaps, so the kernel's running-snap aperture is the
    smallest positive number and can only select a candidate at the point itself.
  - `number` is optional. Absent, the kernel assigns the lowest unused L2 number, as the plugin's default
    answer does. Present, it must be unused among L2 devices; an L1 may carry the same number.
  - `collector_capacity` is at least 1 and equal to every existing L2's capacity: solar-feeders refuses a
    fleet with mixed capacities, and a collector that can take nothing is no collector.
  - Strings, L1 devices and their assignments are never touched. Nothing is assigned to the new device here.
  - Every route and every schedule becomes stale with the reason "equipment_changed", as solar-assign-equipment
    does; an entity already stale keeps its state and reasons.
  - Deterministic: the new entity's id comes from the parent graph and the request, and its created_at is the
    project's own stamp in the bridge's UTC form, so the same request on the same parent gives the same graph.

Readiness (hook): "valid_settings_required" while use_l2_collectors is off, "valid_strings_required" with no
string, "capability_not_ready" at the bridge's device limit, else ready. Never raises on a valid graph.

Contract: fails closed. Every check runs before the private copy is written; the bridge and the graph validator
have the last word; the input graph and request are never mutated. Creates exactly one inverter and removes
nothing. Cost: one bridge projection each way and one kernel pass, linear in devices and strings.
"""
import hashlib
import math
import sys
import uuid
from datetime import datetime, timezone

import solar_electrical_state_bridge as bridge
import solar_inverter_devices as devices
from solar_design_graph import GraphValidationError, _bounded_json, entities
from solar_sizing_client import checked_graph, digest
from solar_solve_results import finish_mutation

TOOL = "solar-central-inverter-add"
INVALID = "INVALID_CENTRAL_INVERTER_REQUEST"
STALE_REASON = "equipment_changed"
REQUIRED_KEYS = frozenset({"expected_rev", "point", "hardware"})
OPTIONAL_KEYS = frozenset({"number"})
HARDWARE_KEYS = bridge.DEFAULT_KEYS["central_inverter"]
MAX_NUMBER = 999_999                 # the kernel's number prompt takes at most six digits
MAX_POINT = 1_000_000.0              # drawing units
MAX_RATING = 1_000_000.0             # the bridge's bound on a voltage and a power
MAX_COUNT = bridge.MAX_NUMBER        # the bridge's bound on mppt_count and total_dc_inputs
MAX_CAPACITY = bridge.MAX_DEVICES    # the contract's bound on collector_capacity
MAX_MODEL = bridge.MAX_TEXT
EXACT_POINT_APERTURE = sys.float_info.min * sys.float_info.epsilon   # 5e-324: selects only the point itself
HOST = {"UseL2Collectors": True, "CentralInverterSymbolScale": 1.0,
        "OsnapApertureDrawingUnits": EXACT_POINT_APERTURE}
FORM = {"select_equipment_type": "Central inverter"}


def _refuse(code, path="<root>"):
    raise GraphValidationError(code, path)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _rating(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAX_RATING


def _request(params):
    """The closed request, fails closed: (expected_rev, number or None, (x, y), hardware)."""
    _bounded_json(params)
    if type(params) is not dict or not REQUIRED_KEYS <= set(params) <= REQUIRED_KEYS | OPTIONAL_KEYS:
        _refuse(INVALID)
    if type(params["expected_rev"]) is not int:
        _refuse(INVALID, "expected_rev")
    number = params.get("number")
    if "number" in params and not _integer(number, 1, MAX_NUMBER):
        _refuse(INVALID, "number")
    point = params["point"]
    if (type(point) is not list or len(point) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > MAX_POINT for v in point)):
        _refuse(INVALID, "point")
    hardware = params["hardware"]
    if (type(hardware) is not dict or set(hardware) != HARDWARE_KEYS
            or type(hardware["model"]) is not str or not 1 <= len(hardware["model"]) <= MAX_MODEL
            or not hardware["model"].strip()
            or not _rating(hardware["max_dc_voltage"]) or not _rating(hardware["max_ac_power_kw"])
            or not _integer(hardware["mppt_count"], 1, MAX_COUNT)
            or not _integer(hardware["total_dc_inputs"], 1, MAX_COUNT)
            or not _integer(hardware["collector_capacity"], 1, MAX_CAPACITY)):
        _refuse(INVALID, "hardware")
    return (params["expected_rev"], number, (float(point[0]), float(point[1])),
            {key: hardware[key] for key in sorted(HARDWARE_KEYS)})


def _minter(graph, params):
    seed = TOOL + "|" + digest(graph) + "|" + digest(params)
    held = {item["id"] for item in entities(graph)}
    held.update({graph["project"]["id"], graph["settings"]["id"]})
    counter = [0]

    def mint(kind):
        while True:
            counter[0] += 1
            raw = hashlib.sha256(f"{seed}|{kind}|{counter[0]}".encode("utf-8")).digest()[:16]
            ident = f"leaf:{kind}:{uuid.UUID(bytes=raw, version=4)}"
            if ident not in held:
                held.add(ident)
                return ident
    return mint


def _created_at(graph):
    """The project's own stamp in the form the bridge takes (UTC, a trailing Z)."""
    text = graph["project"]["provenance"]["created_at"]
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _refuse("CAPABILITY_NOT_READY", "created_at")
    if stamp.tzinfo is None:
        _refuse("CAPABILITY_NOT_READY", "created_at")
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def input_readiness(graph):
    """Whether one central inverter can be added to this graph; never raises on a valid graph."""
    if graph["settings"].get("use_l2_collectors") is not True:
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    if not graph["strings"]:
        return {"input_ready": False, "input_reason": "valid_strings_required"}
    if len(graph["inverters"]) >= bridge.MAX_DEVICES:
        return {"input_ready": False, "input_reason": "capability_not_ready"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params):
    expected_rev, number, (x, y), hardware = _request(params)
    before = checked_graph(graph, expected_rev)
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        _refuse(readiness["input_reason"].upper())
    collectors = [item for item in before["inverters"] if item["is_l2"]]
    if number is not None and any(item["number"] == number for item in collectors):
        _refuse(INVALID, "number")
    if any(item["collector_capacity"] != hardware["collector_capacity"] for item in collectors):
        _refuse(INVALID, "hardware.collector_capacity")
    stamp = _created_at(before)
    try:
        state, binding = bridge.state_from_graph(before)
        answers = ["" if number is None else str(number), f"{x!r},{y!r}", devices.ASSIGN_LATER]
        after, _ = devices.inverter_add(state, HOST, answers, FORM)
        result, _ = bridge.graph_from_state(before, after, binding, defaults={"central_inverter": hardware},
                                            new_id=_minter(before, params), created_at=stamp)
    except bridge.ElectricalBridgeError as exc:
        _refuse("CAPABILITY_NOT_READY", str(exc)[:64])
    except devices.InverterDeviceError as exc:
        _refuse("CAPABILITY_NOT_READY", type(exc).__name__)
    if len(result["inverters"]) != len(before["inverters"]) + 1:
        _refuse("CAPABILITY_NOT_READY", "inverters")
    for item in result["routes"] + result["schedules"]:
        if item["validity"]["state"] != "stale":
            item["validity"] = {"state": "stale", "reasons": [STALE_REASON]}
    return finish_mutation(before, result, TOOL)
