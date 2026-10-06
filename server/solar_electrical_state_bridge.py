"""The electrical state bridge: the one mapping between the shared design graph's equipment topology
(contract/solar-design-graph.v1.schema.json, typed L1/L2 since 58338e17) and the inverter kernels' G35
state (server/solar_inverter_state.py, driven by solar_inverter_devices.py, solar_inverter_strings.py,
solar_inverter_cabling.py and solar_inverter_combiner.py).

Three pure functions, no I/O, no clock, no network:

  state_from_graph(graph) -> (state, binding)
      A G35 state a kernel engine can run on, built from the graph's equipment, string association and
      L1 to L2 topology, plus the binding that names which graph entity every state row stands for.
  adopt_state(graph, state) -> binding
      Binds a kernel state that did not come from the graph (a recorded plugin state): strings by the
      graph string's provenance.source_handle, devices by level and number.
  graph_from_state(graph, state, binding, *, defaults, new_id, created_at) -> (graph, binding)
      Writes the state's equipment topology back onto a copy of the bound graph and returns the
      validated graph and the binding for the state just read.

What is projected (both ways): equipment identity, level, number, position, type key, combiner input
count; each string's collector; each L1's L2 and MPPT slot. What is NOT projected, by design: cable rows
(homerun and feeder geometry belongs to sf-solar-ground-graph-routes), string geometry beyond the graph
string's route, schedules, LBDs, symbol scale and rotation, device hardware and the kernels' host inputs.

Frozen decisions (sf-solar-electrical-bridge):
  - Identity. A graph inverter is a state device row by the row's private `_pair`; a graph string is a
    state string row by its handle. state_from_graph gives device k (1-based, graph order) the pair
    "device:bridge-k" and string k the handle format(k, "X") (upper-case hex, the kernels' handle
    grammar). A row an engine creates carries a pair the binding does not know: it is a new device and
    gets a fresh id from `new_id`. A bound device that leaves the state is refused (removal unsupported).
  - Numbers. A device's number is the kernel's level number (cab.levels: `_number`, else the feeder
    circuit at its insertion point); when the kernel lost it (a moved device leaves its feeder vertex),
    the bound graph inverter's number. L1 and L2 numbers are separate spaces.
  - Positions. The graph holds metres, the state drawing units: x_du = x_m / meters_per_unit. A device
    whose state position equals the position the binding recorded keeps its graph position exactly
    (no float drift on a round trip); a moved or new device gets [x_du * mpu, y_du * mpu].
    A caller may pass metres_per_unit to project into a different kernel unit, passing the same value
    to graph_from_state for the round trip; the feeder graph passes inches.
  - Association. A string's collector is, in order: the device every one of its dc-homerun legs ends
    at (a leg ending at no device, at two devices, or legs disagreeing is ASSOCIATION_CONFLICT); else
    the device number in its circuit (the kernels' grammar, cab._HOMERUN_CIRCUIT) resolved against the
    L1 devices when any L1 exists, else against the L2 devices (GetAssignStringsTargetCollectors); else
    none. CombinerStringL1Assignments is never read or written: it is keyed by the combiner intake's
    string numbers, which no graph entity carries (measured on i5: its 173 entries agree with the
    homerun legs 173 of 173).
  - Circuits. state_from_graph writes the graph circuit_tag verbatim when it parses in the kernels'
    grammar and names the string's collector number; otherwise it synthesizes "+k/<number><letter>"
    (k the surrogate index, the letter the graph MPPT letter lower-cased), and "-" for an unassigned
    string. A circuit the state still carries unchanged never reaches the graph; a circuit an engine
    changed replaces the graph circuit_tag.
  - Inputs. A string whose collector and circuit are unchanged keeps its graph input assignment exactly.
    Every other assigned string takes, on its collector, the MPPT letter "A" on a combiner box (the
    schema pins combiner_box mppt_count to 1, so one letter) and otherwise the single letter of its
    circuit upper-cased ("A" when the circuit has none or several), and the lowest input number free on
    that (collector, letter), in the kernels' string order (st.ORDER: ascending handle).
  - L1 to L2. L1ToL2Assignments gives each L1's L2; L1ToL2InputAssignments its MPPT slot; an L1 whose
    slot the state does not record takes the plugin's seeded default (MpptBalanceAnalyzer: its index
    among that L2's L1s in number order, modulo the L2's mppt_count, cab.seed_input_assignments). A
    feed naming a missing device is FEED_UNRESOLVED. An L2 that ends with both feeds and direct strings
    is refused by the graph validator (L2_MIXED_INPUTS): the bridge never repairs it.
  - New equipment. A new L2 is a central_inverter; a new L1 with a combiner input count above 0 is a
    combiner_box whose total_dc_inputs is that count, else a string_inverter. Model, voltage and power
    (and for central and string inverters the MPPT and input counts, for central inverters the
    collector capacity) come only from the caller's `defaults` for that type; none: DEFAULTS_REQUIRED.
    The bridge never invents hardware.

Bounds: at most 100,000 strings (the graph's own bound) and 10,000 devices (the kernels' MAX_DEVICES);
the binding and defaults are closed shapes with bounded strings and numbers. Linear in strings, devices
and cable rows: one dict per lookup, no pass over strings per device. Every malformed input fails closed
with ElectricalBridgeError (a ValueError) whose message is its code alone; a graph the result would break
fails closed with the validator's GraphValidationError. Inputs are never mutated.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re

from solar_design_graph import new_id as graph_new_id, validate_graph


def _load_sibling(name):
    """Share a fully initialized server module and its error classes across callers."""
    return importlib.import_module(name)


st = _load_sibling("solar_inverter_state")
cab = _load_sibling("solar_inverter_cabling")

BINDING_FORMAT = "leaf.solar-electrical-binding.v1"
WRITER = "solar-electrical-bridge"
MAX_STRINGS = 100_000                      # contract: strings maxItems
MAX_DEVICES = 10_000                       # cab.MAX_DEVICES
MAX_NUMBER = 1_000_000                     # contract: inverter number maximum
MAX_TEXT = 4096                            # contract: circuit_tag and model maxLength
COMBINER_LETTER = "A"
UNASSIGNED = cab.UNASSIGNED_CIRCUIT        # "-"
CIRCUIT = cab._HOMERUN_CIRCUIT             # the kernels' circuit grammar
HANDLE = re.compile(r"[0-9A-F]{1,16}")
PAIR = re.compile(r"[A-Za-z0-9:_.-]{1,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CREATED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
DEFAULT_KEYS = {
    "central_inverter": frozenset({"model", "max_dc_voltage", "max_ac_power_kw", "mppt_count",
                                   "total_dc_inputs", "collector_capacity"}),
    "combiner_box": frozenset({"model", "max_dc_voltage", "max_ac_power_kw"}),
    "string_inverter": frozenset({"model", "max_dc_voltage", "max_ac_power_kw", "mppt_count",
                                  "total_dc_inputs"}),
}


class ElectricalBridgeError(ValueError):
    """A malformed or unsupported input: nothing is projected. The message is the code alone."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_sha256(value):
    """sha256 of canonical JSON (sort_keys, compact separators, no NaN)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ inputs --

def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _integer(value, code):
    if not _finite(value) or int(value) != value:
        raise ElectricalBridgeError(code)
    return int(value)


def _meters_per_unit(graph):
    value = graph["project"]["units"]["meters_per_unit"]
    if not _finite(value) or value <= 0:
        raise ElectricalBridgeError("BRIDGE_UNITS_UNSUPPORTED")
    return float(value)


def _validate_metres_per_unit(value):
    if value is not None and (not _finite(value) or value <= 0):
        raise ValueError("metres_per_unit must be a positive finite number")


def _state(value):
    try:
        return st.validate_state(value)
    except (st.InverterStateError, OverflowError):
        raise ElectricalBridgeError("BRIDGE_STATE_INVALID") from None


def _levels(state):
    try:
        return cab.levels(state)
    except (cab.InverterCablingError, st.InverterStateError, OverflowError):
        raise ElectricalBridgeError("BRIDGE_STATE_INVALID") from None


def _point(value):
    if not isinstance(value, list) or len(value) != 2 or \
            not all(_finite(v) for v in value):
        raise ElectricalBridgeError("BRIDGE_BINDING_INVALID")
    return [float(value[0]), float(value[1])]


def _binding(value):
    """A checked copy of a binding: closed keys, bounded maps, grammar-checked keys and values."""
    if not isinstance(value, dict) or set(value) != {"format", "graph_sha256", "strings", "devices"} \
            or value["format"] != BINDING_FORMAT or type(value["graph_sha256"]) is not str \
            or not SHA256.fullmatch(value["graph_sha256"]) \
            or not isinstance(value["strings"], dict) or len(value["strings"]) > MAX_STRINGS \
            or not isinstance(value["devices"], dict) or len(value["devices"]) > MAX_DEVICES:
        raise ElectricalBridgeError("BRIDGE_BINDING_INVALID")
    strings, devices = {}, {}
    for handle, item in value["strings"].items():
        if type(handle) is not str or not HANDLE.fullmatch(handle) \
                or not isinstance(item, dict) or set(item) != {"id", "circuit"} \
                or type(item["id"]) is not str or len(item["id"]) > 100 \
                or type(item["circuit"]) is not str or len(item["circuit"]) > MAX_TEXT:
            raise ElectricalBridgeError("BRIDGE_BINDING_INVALID")
        strings[handle] = {"id": item["id"], "circuit": item["circuit"]}
    for pair, item in value["devices"].items():
        if type(pair) is not str or not PAIR.fullmatch(pair) \
                or not isinstance(item, dict) or set(item) != {"id", "position"} \
                or type(item["id"]) is not str or len(item["id"]) > 100:
            raise ElectricalBridgeError("BRIDGE_BINDING_INVALID")
        devices[pair] = {"id": item["id"], "position": _point(item["position"])}
    return {"format": BINDING_FORMAT, "graph_sha256": value["graph_sha256"],
            "strings": strings, "devices": devices}


def _positive(value, high):
    return _finite(value) and 0 < value <= high


def _count(value, low, high):
    return type(value) is int and low <= value <= high


def _defaults(value):
    """The caller's per-type equipment values for new devices: closed keys, bounded values."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_KEYS):
        raise ElectricalBridgeError("BRIDGE_INVALID_REQUEST")
    for kind, item in value.items():
        if not isinstance(item, dict) or set(item) != DEFAULT_KEYS[kind] \
                or type(item["model"]) is not str or len(item["model"]) > MAX_TEXT \
                or not _positive(item["max_dc_voltage"], 1e6) or not _positive(item["max_ac_power_kw"], 1e6) \
                or ("mppt_count" in item and not _count(item["mppt_count"], 1, MAX_NUMBER)) \
                or ("total_dc_inputs" in item and not _count(item["total_dc_inputs"], 1, MAX_NUMBER)) \
                or ("collector_capacity" in item and not _count(item["collector_capacity"], 0, MAX_DEVICES)):
            raise ElectricalBridgeError("BRIDGE_INVALID_REQUEST")
    return copy.deepcopy(value)


def _row_circuit(row):
    detail = row.get("_detail") if isinstance(row.get("_detail"), dict) else {}
    circuit = detail.get("circuit")
    if type(circuit) is not str:
        circuit = row.get("label") if type(row.get("label")) is str else UNASSIGNED
    if len(circuit) > MAX_TEXT:
        raise ElectricalBridgeError("BRIDGE_STATE_INVALID")
    return circuit


def _circuit_number(circuit):
    match = CIRCUIT.fullmatch(circuit.strip())
    return int(match.group("device")) if match else None


def _circuit_letter(circuit):
    match = CIRCUIT.fullmatch(circuit.strip())
    letters = match.group("mppt") if match else ""
    return letters.upper() if len(letters) == 1 else COMBINER_LETTER


def _stored(mapping):
    return {str(key): mapping[key] for key in sorted(mapping)}


# ----------------------------------------------------------- graph -> state --

def state_from_graph(graph, *, metres_per_unit=None):
    """The G35 state of the graph's equipment topology and its binding (see the module docstring)."""
    _validate_metres_per_unit(metres_per_unit)
    g = validate_graph(graph)
    mpu = _meters_per_unit(g) if metres_per_unit is None else metres_per_unit
    if len(g["strings"]) > MAX_STRINGS or len(g["inverters"]) > MAX_DEVICES:
        raise ElectricalBridgeError("BRIDGE_BOUNDS_EXCEEDED")
    by_id = {inverter["id"]: inverter for inverter in g["inverters"]}
    if any(not inverter["is_l2"] for inverter in g["inverters"]) and any(
            string["inverter_ref"] is not None and by_id[string["inverter_ref"]]["is_l2"]
            for string in g["strings"]):
        raise ElectricalBridgeError("BRIDGE_LEVELS_AMBIGUOUS")
    devices, bound_devices, levels = [], {}, set()
    for k, inverter in enumerate(g["inverters"], 1):
        number = _integer(inverter["number"], "BRIDGE_DEVICE_UNNUMBERED")
        if not 1 <= number <= MAX_NUMBER:
            raise ElectricalBridgeError("BRIDGE_DEVICE_UNNUMBERED")
        level = (inverter["is_l2"], inverter["number"])
        if level in levels:
            raise ElectricalBridgeError("BRIDGE_DUPLICATE_DEVICE")
        levels.add(level)
        pair = f"device:bridge-{k}"
        x, y = inverter["position"][0] / mpu, inverter["position"][1] / mpu
        box = inverter["total_dc_inputs"] if inverter.get("equipment_type") == "combiner_box" else 0
        devices.append({"number": number, "role": "inverter" if inverter["is_l2"] else "combiner",
                        "position": st.coordinate(x, y), "scale": 1.0, "rotation": st.angle(0.0),
                        "placement": None, "hardware": None, "_pair": pair, "_number": number,
                        "_detail": {"type_key": inverter["type_key"], "is_l2": inverter["is_l2"],
                                    "box_input_count": box, "colour": None}})
        bound_devices[pair] = {"id": inverter["id"], "position": [float(x), float(y)]}
    l1_to_l2, slots = {}, {}
    for inverter in g["inverters"]:
        if inverter["is_l2"]:
            for feed in inverter.get("l1_assignments", []):
                number = _integer(by_id[feed["inverter_ref"]]["number"], "BRIDGE_DEVICE_UNNUMBERED")
                l1_to_l2[number] = _integer(inverter["number"], "BRIDGE_DEVICE_UNNUMBERED")
                slots[number] = _integer(feed["mppt_index"], "BRIDGE_STATE_INVALID")
    inputs = {a["string_ref"]: (inverter, a) for inverter in g["inverters"] for a in inverter["input_assignments"]}
    rows, geometry, bound_strings = [], [], {}
    for k, string in enumerate(g["strings"], 1):
        handle = format(k, "X")
        if string["inverter_ref"] is None:
            circuit, device, slot = UNASSIGNED, 0, 0
        else:
            inverter, assignment = inputs[string["id"]]
            number = _integer(inverter["number"], "BRIDGE_DEVICE_UNNUMBERED")
            letter = assignment["mppt_letter"]
            tag = string["circuit_tag"]
            if CIRCUIT.fullmatch(tag) and _circuit_number(tag) == inverter["number"]:
                circuit = tag
            elif re.fullmatch(r"[A-Za-z]", letter):
                circuit = f"+{k}/{number}{letter.lower()}"
            else:
                raise ElectricalBridgeError("BRIDGE_CIRCUIT_UNSUPPORTED")
            device = number
            slot = ord(letter.lower()) - ord("a") + 1 if re.fullmatch(r"[A-Za-z]", letter) else 1
        rows.append({"string": handle, "device": device, "input": slot, "label": circuit, "colour": None,
                     "_detail": {"circuit": circuit}})
        vertices = [[float(p[0]) / mpu, float(p[1]) / mpu] for p in string["route"]]
        geometry.append({"string": handle, "vertices": vertices, "midpoint": None,
                         "start": vertices[0] if vertices else None, "end": vertices[-1] if vertices else None,
                         "panels": []})
        bound_strings[handle] = {"id": string["id"], "circuit": circuit}
    settings = {"UseL2Collectors": g["settings"]["use_l2_collectors"],
                "InstallationDesign": g["project"]["installation_design"],
                "NumMppt": g["settings"]["num_mppt"], "StringPerMppt": g["settings"]["strings_per_mppt"],
                "L1ToL2Assignments": _stored(l1_to_l2), "L1ToL2InputAssignments": _stored(slots)}
    sha = canonical_sha256(g)
    state = _state({"format": st.STATE_FORMAT, "source": {"dump_sha256": sha, "reopened": False},
                    "rows": {"device": devices, "string-assignment": rows, "cable": [], "schedule": [], "lbd": []},
                    "setting": settings, "geometry": {"strings": geometry, "panel_groups": []}})
    return state, {"format": BINDING_FORMAT, "graph_sha256": sha, "strings": bound_strings, "devices": bound_devices}


def adopt_state(graph, state):
    """The binding of a kernel state that did not come from the graph: every graph string by its
    provenance.source_handle (exactly one state string each, and the reverse), every graph inverter by
    level and number (a graph inverter the state lacks is DEVICE_REMOVED); unnumbered or unmatched state
    devices stay unbound and are new on write-back."""
    g = validate_graph(graph)
    s = _state(state)
    handles = [row["string"] for row in s["rows"]["string-assignment"]]
    if len(handles) > MAX_STRINGS or not all(type(h) is str and HANDLE.fullmatch(h) for h in handles) \
            or len(set(handles)) != len(handles):
        raise ElectricalBridgeError("BRIDGE_STRING_BINDING_MISMATCH")
    rows = {row["string"]: row for row in s["rows"]["string-assignment"]}
    strings = {}
    for string in g["strings"]:
        handle = string["provenance"].get("source_handle")
        if type(handle) is not str or handle not in rows or handle in strings:
            raise ElectricalBridgeError("BRIDGE_STRING_BINDING_MISMATCH")
        strings[handle] = {"id": string["id"], "circuit": _row_circuit(rows[handle])}
    if len(strings) != len(rows):
        raise ElectricalBridgeError("BRIDGE_STRING_BINDING_MISMATCH")
    pairs = set()
    for row in s["rows"]["device"]:
        pair = row.get("_pair")
        if type(pair) is not str or not PAIR.fullmatch(pair) or pair in pairs:
            raise ElectricalBridgeError("BRIDGE_STATE_INVALID")
        pairs.add(pair)
    l1, l2 = _levels(s)
    by_level = {}
    for is_l2, items in ((False, l1), (True, l2)):
        for item in items:
            if item["number"] is not None:
                key = (is_l2, item["number"])
                if key in by_level:
                    raise ElectricalBridgeError("BRIDGE_DUPLICATE_DEVICE")
                by_level[key] = item
    devices = {}
    for inverter in g["inverters"]:
        item = by_level.get((inverter["is_l2"], inverter["number"]))
        if item is None:
            raise ElectricalBridgeError("BRIDGE_DEVICE_REMOVED")
        devices[item["row"]["_pair"]] = {"id": inverter["id"], "position": [item["position"][0], item["position"][1]]}
    return {"format": BINDING_FORMAT, "graph_sha256": canonical_sha256(g), "strings": strings, "devices": devices}


# ----------------------------------------------------------- state -> graph --

def _new_device(g, item, is_l2, number, mode, defaults, mpu, ident, created_at):
    box = item["row"]["_detail"].get("box_input_count") if isinstance(item["row"].get("_detail"), dict) else 0
    box = box if type(box) is int and 0 < box <= MAX_NUMBER else 0
    kind = "central_inverter" if is_l2 else ("combiner_box" if box else "string_inverter")
    values = defaults.get(kind)
    if values is None:
        raise ElectricalBridgeError("BRIDGE_DEFAULTS_REQUIRED")
    detail = item["row"].get("_detail") if isinstance(item["row"].get("_detail"), dict) else {}
    type_key = detail.get("type_key") if type(detail.get("type_key")) is str and 1 <= len(detail["type_key"]) <= 16 else "A"
    x, y = item["position"]
    inverter = {"id": ident, "kind": "inverter", "rev": g["rev"],
                "provenance": {"created_by": WRITER, "created_at": created_at, "last_writer": WRITER,
                               "source_rev": g["rev"]},
                "extra": {}, "validity": {"state": "valid", "reasons": []},
                "number": number, "type_key": type_key, "is_l2": is_l2, "position": [x * mpu, y * mpu],
                "model": values["model"], "mppt_count": 1 if kind == "combiner_box" else values["mppt_count"],
                "total_dc_inputs": box if kind == "combiner_box" else values["total_dc_inputs"],
                "max_dc_voltage": values["max_dc_voltage"], "max_ac_power_kw": values["max_ac_power_kw"],
                "is_solaredge": False, "input_assignments": []}
    if mode:
        inverter["equipment_type"] = kind
        if is_l2:
            inverter.update(collector_capacity=values["collector_capacity"], l1_assignments=[])
        else:
            inverter["l2_ref"] = None
    return inverter


def _int_map(state, name):
    try:
        return cab._int_map(state["setting"].get(name), name)
    except (cab.InverterCablingError, OverflowError):
        raise ElectricalBridgeError("BRIDGE_STATE_INVALID") from None


def graph_from_state(graph, state, binding, *, defaults=None, new_id=None, created_at=None,
                     metres_per_unit=None):
    """The graph with the state's equipment topology written onto a copy, validated, and the binding of
    that state to the result (see the module docstring for every rule)."""
    _validate_metres_per_unit(metres_per_unit)
    g = validate_graph(graph)
    s = _state(state)
    b = _binding(binding)
    values = _defaults(defaults)
    if b["graph_sha256"] != canonical_sha256(g):
        raise ElectricalBridgeError("BRIDGE_BINDING_STALE")
    mpu = _meters_per_unit(g) if metres_per_unit is None else metres_per_unit
    mode = g["settings"]["use_l2_collectors"]
    by_id = {inverter["id"]: inverter for inverter in g["inverters"]}
    strings_by_id = {string["id"]: string for string in g["strings"]}
    bound_ids = [item["id"] for item in b["devices"].values()]
    if len(set(bound_ids)) != len(bound_ids) or set(bound_ids) != set(by_id) \
            or sorted(item["id"] for item in b["strings"].values()) != sorted(strings_by_id):
        raise ElectricalBridgeError("BRIDGE_BINDING_INVALID")

    # Devices: bound rows keep their graph entity, unbound rows are new.
    pairs = set()
    for row in s["rows"]["device"]:
        pair = row.get("_pair")
        if type(pair) is not str or not PAIR.fullmatch(pair) or pair in pairs:
            raise ElectricalBridgeError("BRIDGE_STATE_INVALID")
        pairs.add(pair)
    l1, l2 = _levels(s)
    if l2 and not mode:
        raise ElectricalBridgeError("BRIDGE_L2_MODE_REQUIRED")
    if len(l1) + len(l2) > MAX_DEVICES:
        raise ElectricalBridgeError("BRIDGE_BOUNDS_EXCEEDED")
    resolved, seen_pairs, numbers = [], set(), set()
    for is_l2, items in ((False, l1), (True, l2)):
        for item in items:
            pair = item["row"].get("_pair")
            bound = b["devices"].get(pair) if type(pair) is str else None
            if bound is not None:
                if pair in seen_pairs or by_id[bound["id"]]["is_l2"] != is_l2:
                    raise ElectricalBridgeError("BRIDGE_STATE_INVALID")
                seen_pairs.add(pair)
            number = item["number"]
            if number is None and bound is not None:
                number = _integer(by_id[bound["id"]]["number"], "BRIDGE_DEVICE_UNNUMBERED")
            if type(number) is not int or not 1 <= number <= MAX_NUMBER:
                raise ElectricalBridgeError("BRIDGE_DEVICE_UNNUMBERED")
            if (is_l2, number) in numbers:
                raise ElectricalBridgeError("BRIDGE_DUPLICATE_DEVICE")
            numbers.add((is_l2, number))
            resolved.append((item, is_l2, number, bound))
    if seen_pairs != set(b["devices"]):
        raise ElectricalBridgeError("BRIDGE_DEVICE_REMOVED")

    # Circuit-only associations must not silently change when the available levels change.
    # Check before minting equipment or changing the graph copy; homerun ends may move strings.
    circuit_levels = {False: {}, True: {}}
    for item, is_l2, number, bound in resolved:
        circuit_levels[is_l2][number] = bound["id"] if bound is not None else (item["row"]["_pair"],)
    circuit_targets = circuit_levels[False] if circuit_levels[False] else circuit_levels[True]
    leg_handles = {row.get("from") for row in s["rows"]["cable"]
                   if row.get("cable_kind") == "dc-homerun" and type(row.get("from")) is str}
    for row in s["rows"]["string-assignment"]:
        handle = row.get("string")
        if type(handle) is not str or handle not in b["strings"] or handle in leg_handles:
            continue
        bound = b["strings"][handle]
        circuit = _row_circuit(row)
        if circuit == bound["circuit"]:
            target = None if circuit.strip() == UNASSIGNED else circuit_targets.get(_circuit_number(circuit))
            if target != strings_by_id[bound["id"]]["inverter_ref"]:
                raise ElectricalBridgeError("BRIDGE_LEVELS_AMBIGUOUS")
    if any(bound is None for _, _, _, bound in resolved):
        if created_at is None or type(created_at) is not str or len(created_at) > 32 \
                or not CREATED_AT.fullmatch(created_at):
            raise ElectricalBridgeError("BRIDGE_INVALID_REQUEST")
    mint = graph_new_id if new_id is None else new_id
    result = copy.deepcopy(g)
    current = {inverter["id"]: inverter for inverter in result["inverters"]}
    taken = {entity["id"] for key in ("strings", "inverters", "panels", "frames", "routes", "schedules",
                                      "electrical_zones") for entity in result[key]}
    taken.update({result["project"]["id"], result["settings"]["id"]})
    device_id, level_ids, new_binding = {}, {False: {}, True: {}}, {}
    for item, is_l2, number, bound in resolved:
        pair = item["row"]["_pair"]
        x, y = item["position"]
        if bound is not None:
            inverter = current[bound["id"]]
            if [x, y] != bound["position"]:
                inverter["position"] = [x * mpu, y * mpu]
            if inverter["number"] != number:
                inverter["number"] = number
            detail = item["row"].get("_detail") if isinstance(item["row"].get("_detail"), dict) else {}
            if type(detail.get("type_key")) is str and 1 <= len(detail["type_key"]) <= 16:
                inverter["type_key"] = detail["type_key"]
            box = detail.get("box_input_count")
            if inverter.get("equipment_type") == "combiner_box" and type(box) is int and 0 < box <= MAX_NUMBER:
                inverter["total_dc_inputs"] = box
        else:
            ident = mint("inverter")
            if type(ident) is not str or len(ident) > 100:
                raise ElectricalBridgeError("BRIDGE_INVALID_REQUEST")
            if ident in taken:
                raise ElectricalBridgeError("BRIDGE_ID_COLLISION")
            inverter = _new_device(result, item, is_l2, number, mode, values, mpu, ident, created_at)
            result["inverters"].append(inverter)
            current[ident] = inverter
        taken.add(inverter["id"])
        device_id[id(item["row"])] = inverter["id"]
        level_ids[is_l2][number] = inverter["id"]
        new_binding[pair] = {"id": inverter["id"], "position": [x, y]}

    # L1 to L2 topology.
    l1_to_l2 = _int_map(s, "L1ToL2Assignments")
    slots = _int_map(s, "L1ToL2InputAssignments")
    if l1_to_l2 and not mode:
        raise ElectricalBridgeError("BRIDGE_L2_MODE_REQUIRED")
    if mode:
        feeds = {}
        for l1_number, l2_number in l1_to_l2.items():
            if l1_number not in level_ids[False] or l2_number not in level_ids[True]:
                raise ElectricalBridgeError("BRIDGE_FEED_UNRESOLVED")
            feeds.setdefault(l2_number, []).append(l1_number)
        for number, ident in level_ids[False].items():
            inverter = current[ident]
            l2_number = l1_to_l2.get(number)
            if "equipment_type" in inverter:
                inverter["l2_ref"] = level_ids[True][l2_number] if l2_number is not None else None
        for l2_number, ident in level_ids[True].items():
            inverter = current[ident]
            count = inverter["mppt_count"]
            wanted = {}
            for index, l1_number in enumerate(sorted(feeds.get(l2_number, []))):
                slot = slots.get(l1_number)
                wanted[level_ids[False][l1_number]] = slot if slot is not None else (index % count if count > 0 else 0)
            kept = []
            for feed in inverter["l1_assignments"]:
                if feed["inverter_ref"] in wanted:
                    slot = wanted.pop(feed["inverter_ref"])
                    if feed["mppt_index"] != slot:
                        feed["mppt_index"] = slot
                    kept.append(feed)
            order = {level_ids[False][n]: n for n in feeds.get(l2_number, [])}
            for ref in sorted(wanted, key=lambda r: order[r]):
                kept.append({"inverter_ref": ref, "mppt_index": wanted[ref]})
            inverter["l1_assignments"] = kept

    # Strings: association, circuit and inputs.
    rows = {}
    for row in s["rows"]["string-assignment"]:
        handle = row.get("string")
        if type(handle) is not str or handle in rows:
            raise ElectricalBridgeError("BRIDGE_STATE_INVALID")
        if handle not in b["strings"]:
            raise ElectricalBridgeError("BRIDGE_UNBOUND_STRING")
        rows[handle] = row
    if set(rows) != set(b["strings"]):
        raise ElectricalBridgeError("BRIDGE_STRING_MISSING")
    at_position = {}
    for item, _, _, _ in resolved:
        at_position.setdefault(item["position"], []).append(device_id[id(item["row"])])
    legs = {}
    for row in s["rows"]["cable"]:
        if row.get("cable_kind") == "dc-homerun" and row.get("from") in rows:
            try:
                end = tuple(st.point_of(row["vertices"][-1], "homerun vertex"))
            except (st.InverterStateError, KeyError, IndexError, TypeError, OverflowError):
                raise ElectricalBridgeError("BRIDGE_STATE_INVALID") from None
            legs.setdefault(row["from"], set()).add(end)
    targets = level_ids[False] if level_ids[False] else level_ids[True]
    old_inputs = {a["string_ref"]: (inverter["id"], a) for inverter in g["inverters"] for a in inverter["input_assignments"]}
    decided, keep, fresh = {}, set(), []
    for handle in sorted(rows, key=lambda h: st.ORDER["string-assignment"](rows[h])):
        row, bound = rows[handle], b["strings"][handle]
        circuit = _row_circuit(row)
        if handle in legs:
            owners = set()
            for end in legs[handle]:
                ids = at_position.get(end, [])
                if len(ids) != 1:
                    raise ElectricalBridgeError("BRIDGE_ASSOCIATION_CONFLICT")
                owners.add(ids[0])
            if len(owners) != 1:
                raise ElectricalBridgeError("BRIDGE_ASSOCIATION_CONFLICT")
            target = owners.pop()
        elif circuit.strip() == UNASSIGNED:
            target = None
        else:
            target = targets.get(_circuit_number(circuit))
        ref = bound["id"]
        decided[ref] = (target, circuit, circuit != bound["circuit"])
        old = old_inputs.get(ref)
        if target is not None and old is not None and old[0] == target and circuit == bound["circuit"]:
            keep.add(ref)
        elif target is not None:
            fresh.append((ref, target, circuit))
    strings_after = {string["id"]: string for string in result["strings"]}
    for ref, (target, circuit, changed) in decided.items():
        string = strings_after[ref]
        if string["inverter_ref"] != target:
            string["inverter_ref"] = target
            string["to_ref"] = target
        if changed:
            string["circuit_tag"] = circuit
    used = {}
    for inverter in result["inverters"]:
        inverter["input_assignments"] = [a for a in inverter["input_assignments"] if a["string_ref"] in keep]
        for a in inverter["input_assignments"]:
            used.setdefault((inverter["id"], a["mppt_letter"]), set()).add(a["input_number"])
    next_free = {}
    for ref, target, circuit in fresh:
        inverter = current[target]
        letter = COMBINER_LETTER if inverter.get("equipment_type") == "combiner_box" else _circuit_letter(circuit)
        taken_numbers = used.setdefault((target, letter), set())
        number = next_free.get((target, letter), 0)
        while number in taken_numbers:
            number += 1
        taken_numbers.add(number)
        next_free[(target, letter)] = number + 1
        inverter["input_assignments"].append({"string_ref": ref, "mppt_letter": letter, "input_number": number})

    # The frames' redundant input views follow the new assignments.
    inputs = {a["string_ref"]: (inverter["id"], a["input_number"])
              for inverter in result["inverters"] for a in inverter["input_assignments"]}
    panels = {panel["id"]: panel for panel in result["panels"]}
    for frame in result["frames"]:
        for record in frame["panel_assignments"] + [c for r in frame["matrix"] for c in r if c["panel_ref"] is not None]:
            wanted = inputs.get(panels[record["panel_ref"]]["assignment"]["string_ref"], (None, None))
            if (record["inverter_id"], record["string_input_number"]) != wanted:
                record["inverter_id"], record["string_input_number"] = wanted
    result = validate_graph(result)
    strings_binding = {handle: {"id": b["strings"][handle]["id"], "circuit": _row_circuit(row)}
                       for handle, row in rows.items()}
    return result, {"format": BINDING_FORMAT, "graph_sha256": canonical_sha256(result),
                    "strings": strings_binding, "devices": new_binding}
