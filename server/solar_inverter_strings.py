"""Studio ports of the plugin's string assignment and string colouring (contract G35, G35a):
assign-strings (i2 AssignStrings, Auto mode, the pattern matcher, to the drawing's central inverters)
and color-strings (i3 LEAFCOLORSTRINGS, one colour per inverter).

Literal ports of Branch2025 (read 2026-09-24 at C:/tmp/solar-parity/wt-b25-s69, master 6b940d51, the
captured build):

  assign-strings (BranchCmd.AssignStrings, :13647-13759)
    :13677-13692   the collector label: under UseL2Collectors, L2 blocks only -> "central inverter",
                   L1 only -> "combiner box", both -> "L1/L2 collector", none -> the legacy label
    :13705-13707   the mode dialog ("Assign strings to <label>s": Auto or Manual, OK or Cancel)
    :13728-13736   Auto with the host's UsePatternStringAssignment takes the pattern path; the legacy
                   cloud path and Manual are not ported
    :13902-13994   TryPrepareAssignStringsAuto: the unassigned strings (circuit "-", GetUnassignedStrings
                   :16945-16982, each with its extents centre, GetCableCentroid :16984-16994), the target
                   collectors (GetAssignStringsTargetCollectors :14032-14041: the L2 list when L1/L2 mode
                   has L2 blocks and no L1 block, else the L1 list), their existing loads
                   (RefreshAssignStringsTargetLoads :13996-14030, strings grouped by the circuit's
                   inverter number), the capacity check ("Not Enough Capacity" stops; "Excess Capacity"
                   Yes continues, No prints "Auto-assignment cancelled.")
    :14068-14112   capacity per collector: an L2 block in L1/L2 mode holds L2NumMppt x L2StringsPerMppt
                   (host settings, each falling back to the drawing's NumMppt / StringPerMppt, then the
                   host's NumMppt / StringsPerMppt); otherwise GetStringsPerInverter (:10649-10682)
    :14116-14259   AssignStringsPattern's plan: strings ordered by centroid X then Y; the next string
                   number is one past the largest on any label (:14169-14176); row groups
                   (BuildPatternStringRowGroups :14372-14419: tracker rows by PanelRef id :14421-14457,
                   the rest by BuildGeometricFallbackRows :14459-14498, each ordered by row sequence,
                   X, Y, the groups by centroid X then Y); collectors ordered by insertion X, Y, number;
                   per collector the nearest row seed (TryFindNearestAssignableRowSeed :14516-14555,
                   squared distance, first strict minimum) and the seed-outward walk
                   (BuildSeedOutwardRowOrder :14557-14585) until its capacity is spent
    :14587-14608   the tag: CircuitNumber (StringPlacement.cs:2419-2455, no custom tagging) with the
                   MPPT letter of CircuitNumberMath.ComputeMpptLetter (CircuitNumberMath.cs:59-88)
    :14610-14798   the write: one colour per collector (EnsurePatternInverterWriteState :14670-14710:
                   the block's own colour when it already holds strings and is neither 0 nor 7, else
                   the next of its type family at App.mColorCtr, which the block takes too); each
                   string's polyline takes that colour, its record the tag, its label the tag text
                   and the tag record (TagXData, :14741-14752: MPPTs, strings per MPPT, string number,
                   inverter number, MPPT letter, strings on the inverter, terminal 1
                   (StringPlacement.cs:100), the colour)
    StringPlacement.cs:343-364
                   the first collector's StringPlacement persists the auto tag height (0.4 x the panel
                   definition's short side) into the drawing settings with one drawing-properties save,
                   which also appends HomerunRoutingConfig.CreateDefault()'s catalog (the G27 save
                   finding). Measured: the capture's catalog grew by exactly one pair across the run
                   (10 entries at state-i1, 12 at state-i2) although one StringPlacement is built per
                   collector, so one save is modelled per run (inferred: the later constructions'
                   saves leave the stored settings as the first wrote them).
  color-strings (BranchCmd.RecolorStringsByInverter, :11121-11219)
    :11124-11129   every String-layer string (GetStrings(true), AcadCommandBase.cs:840-889); none: the
                   "no strings found" line
    :11149-11163   the inverter number from the string's circuit (Cable.cs:30-43, CircuitTagParser.cs:
                   22-66); none or not positive: colour 7; else the first pick of the inverter's type
                   family (GetTypeColorArray :10928-10933, InverterTypeConfig.cs:79-90, the untyped
                   six-colour array BranchCmdCore.cs:3106-3116) at (number - 1) mod its length
    :11169-11174   StringEntities (String.cs:63-122): only a string whose start block, end block and
                   label all exist is recoloured (polyline and both blocks; the label keeps its colour)
    :11207-11209   the printed count. No drawing-properties save.

Declared divergence: none. Both capabilities are zero-diff under G35.

HOST INPUTS. Values the commands read from the capture host's per-user settings, the session, or a
block definition, never from the drawing state, arrive as the `host` argument, named by the plugin
setting they stand for (the producer, scripts/solar_inverter_strings_evidence.py, records the captured
values and how each was measured). A device's number lives only on its NUMBER attribute, which the G35
state does not carry (the plugin adapter's module docstring); an engine reads a device's private
`_number` when a Studio engine set it, else the host's DeviceNumbers list (number by insertion point).

Pure functions over plain data: no CAD host, no I/O, no network. Every engine takes a G35 state
(server/solar_inverter_state.py), never mutates it, and returns (new state, printed lines). Bounded:
the plan is O(collectors x strings) distance scans, each string assigned once. Malformed input fails
closed with InverterStringError; a path this port does not cover raises InverterStringNotPortedError,
never a guess.
"""
from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
import re
import sys


def _load_sibling(name):
    """A server module by path (any cwd), shared through sys.modules so its error classes are one."""
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load_sibling("solar_inverter_state")

UNASSIGNED_CIRCUIT = "-"                          # AppConstants.UnassignedCircuitText
# BranchCmdCore.cs:3106-3116: red, yellow, blue, green, magenta, cyan.
UNTYPED_COLOURS = (1, 2, 5, 3, 6, 4)
# InverterTypeConfig.cs:83-88.
TYPE_COLOURS = {"B": (5, 4, 3), "C": (8, 30, 40)}
TYPE_A_COLOURS = (1, 2, 6)
UNASSIGNED_COLOUR = 7                             # BranchCmd.cs:11155
LABEL_COLOUR = 7                                  # BranchCmd.cs:14794
TERMINAL_NUMBER = 1                               # StringPlacement.cs:100
TAG_HEIGHT_FRACTION = 0.4                         # StringPlacement.cs:346-347
# The capture's i1 command log prints each device's insertion point to one decimal place; a
# DeviceNumbers entry matches a device within half of that.
POSITION_TOLERANCE = 0.05 + 1e-9
MAX_STRINGS = 200_000
MAX_DEVICES = 10_000
MAX_NUMBER = 1_000_000
INT32_MAX = 2**31 - 1

HOST_KEYS = frozenset({
    "UseL2Collectors", "UseCombinerBox", "UsePatternStringAssignment", "L2NumMppt", "L2StringsPerMppt",
    "NumMppt", "StringsPerMppt", "CombinerBoxConnections", "SessionColorCounter", "AutoTagHeight",
    "DeviceNumbers"})

AUTO, MANUAL = "Auto", "Manual"


class InverterStringError(ValueError):
    """A malformed state, host input or answer: nothing is assigned."""


class InverterStringNotPortedError(InverterStringError):
    """The command would take a path this port does not cover (named in the message)."""


# ------------------------------------------------------------------ inputs --

def _host(host):
    if not isinstance(host, dict) or set(host) - HOST_KEYS:
        raise InverterStringError(f"host inputs must be an object of {sorted(HOST_KEYS)}")
    return host


def _host_bool(host, key):
    value = host.get(key)
    if type(value) is not bool:
        raise InverterStringError(f"host input {key} must be true or false")
    return value


def _host_int(host, key, default=0):
    value = host.get(key, default)
    if value is None:
        return default
    if type(value) is not int or abs(value) > INT32_MAX:
        raise InverterStringError(f"host input {key} must be an integer")
    return value


def _setting_int(state, name):
    value = state["setting"].get(name, st.DECLARED_DEFAULTS.get(name, 0))
    return value if type(value) is int else 0


def _detail(item):
    detail = item.get("_detail")
    return detail if isinstance(detail, dict) else {}


def _is_l2(device):
    """A device's L2 flag as its record stores it (Inverter.cs:121-167): the private detail when
    present, else its role (a combiner is the only L1 role)."""
    detail = _detail(device)
    if type(detail.get("is_l2")) is bool:
        return detail["is_l2"]
    return device["role"] != "combiner"


def _device_numbers(host):
    """The host's DeviceNumbers: [{position: [x, y], number}]."""
    listed = host.get("DeviceNumbers") or []
    if not isinstance(listed, list) or len(listed) > MAX_DEVICES:
        raise InverterStringError(f"host input DeviceNumbers must be a list of at most {MAX_DEVICES}")
    out = []
    for entry in listed:
        if not isinstance(entry, dict) or type(entry.get("number")) is not int or \
                not 0 < entry["number"] <= MAX_NUMBER:
            raise InverterStringError("a DeviceNumbers entry is {position: [x, y], number > 0}")
        position = entry.get("position")
        if not isinstance(position, list) or len(position) != 2 or \
                not all(type(v) in (int, float) and math.isfinite(v) for v in position):
            raise InverterStringError("a DeviceNumbers position is two finite numbers")
        out.append((float(position[0]), float(position[1]), entry["number"]))
    return out


def device_number(device, listed):
    """The number a device's NUMBER attribute holds: the private `_number` a Studio engine set, else
    the one DeviceNumbers entry at its insertion point, else None."""
    own = device.get("_number")
    if type(own) is int and own > 0:
        return own
    x, y = st.point_of(device["position"])
    found = [n for lx, ly, n in listed if abs(lx - x) <= POSITION_TOLERANCE and abs(ly - y) <= POSITION_TOLERANCE]
    if len(found) > 1:
        raise InverterStringError(f"two DeviceNumbers entries match the device at ({x}, {y})")
    return found[0] if found else None


# ----------------------------------------------------------------- parsing --

def parse_circuit(circuit):
    """CircuitTagParser.Parse (CircuitTagParser.cs:22-66): the digits after the first '/', else -1.
    (A digit Convert.ToInt32 cannot read, or an overflow, is -1 as the plugin's catch makes it.)"""
    if not isinstance(circuit, str):
        return -1
    slash = circuit.find("/")
    if slash < 0:
        return -1
    digits = ""
    for ch in circuit[slash + 1:]:
        if ch.isdecimal():
            digits += ch
        else:
            break
    if not digits or not digits.isascii() or len(digits) > 10 or int(digits) > INT32_MAX:
        return -1
    return int(digits)


def _circuit(item):
    circuit = _detail(item).get("circuit")
    if isinstance(circuit, str):
        return circuit
    if isinstance(item.get("label"), str):
        return item["label"]
    raise InverterStringError(f"string {item.get('string')!r} carries no circuit")


def parse_panel_id(panel_id):
    """PanelRef.TryParsePanelId (LeafSolarDesign.Core/Panels/PanelRef.cs:81-95): "<prefix>:<tracker
    handle>:<sub index>" -> (tracker handle, sub index), else None. The prefix is PanelRef's own
    HandlePrefix; any three-part id with a non-negative integer sub index is taken here, and the G35
    state carries plain panel handles (no colon), which never parse."""
    if not isinstance(panel_id, str) or not panel_id.strip():
        return None
    parts = panel_id.split(":")
    if len(parts) != 3 or not parts[1].strip() or not re.fullmatch(r"[0-9]{1,9}", parts[2]):
        return None
    return parts[1].upper(), int(parts[2])


def type_colours(state, number):
    """GetTypeColorArray (BranchCmd.cs:10928-10933, :10864-10875): the colour family of the inverter's
    assigned type (InverterTypeConfig.ColorFamily), else the untyped six colours."""
    assignments = state["setting"].get("InverterTypeAssignments") or {}
    types = state["setting"].get("InverterTypes") or {}
    if not isinstance(assignments, dict) or not isinstance(types, dict):
        raise InverterStringError("InverterTypeAssignments and InverterTypes must be objects")
    key = assignments.get(str(number))
    config = types.get(key) if isinstance(key, str) else None
    if not isinstance(config, dict):
        return UNTYPED_COLOURS
    type_key = config.get("TypeKey") if isinstance(config.get("TypeKey"), str) and config.get("TypeKey") else key
    return TYPE_COLOURS.get(type_key[0], TYPE_A_COLOURS)


def mppt_letter(string_number, num_mppt, strings_per_mppt):
    """CircuitNumberMath.ComputeMpptLetter (CircuitNumberMath.cs:59-88): 'a' when the topology is
    incomplete; else the group of (n - 1) mod the inverter's total, capped at the last MPPT."""
    if num_mppt <= 0 or strings_per_mppt <= 0:
        return "a"
    local = (string_number - 1) % (num_mppt * strings_per_mppt)
    group = min(max(local, 0) // strings_per_mppt, num_mppt - 1)
    return chr(ord("a") + group)


def tag_text(inverter_number, string_number, num_mppt, strings_per_mppt):
    """CircuitNumber.mTagText with no custom tagging (StringPlacement.cs:2445-2455)."""
    return f"+{string_number}/{inverter_number}{mppt_letter(string_number, num_mppt, strings_per_mppt)}"


# ---------------------------------------------------------------- the drawing --

def _geometry(state):
    by_handle = {}
    for item in state["geometry"]["strings"]:
        if isinstance(item, dict) and isinstance(item.get("string"), str):
            by_handle[item["string"]] = item
    return by_handle


def _point(value, what):
    if not isinstance(value, (list, tuple)) or len(value) < 2 or \
            not all(type(v) in (int, float) and math.isfinite(v) for v in value[:2]):
        raise InverterStringError(f"{what} must be a finite point")
    return float(value[0]), float(value[1])


def centroid(geometry):
    """GetCableCentroid (BranchCmd.cs:16984-16994): the centre of the polyline's extents."""
    vertices = [_point(v, "string vertex") for v in geometry.get("vertices") or []]
    if not vertices:
        raise InverterStringError(f"string {geometry.get('string')!r} has no vertices")
    xs = [p[0] for p in vertices]
    ys = [p[1] for p in vertices]
    return (min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5


def _strings(state):
    """Every String-layer string: (row, geometry), in the state's order."""
    rows = state["rows"]["string-assignment"]
    if len(rows) > MAX_STRINGS:
        raise InverterStringError(f"more than {MAX_STRINGS} strings")
    geometry = _geometry(state)
    out = []
    for item in rows:
        handle = item.get("string")
        if not isinstance(handle, str):
            raise InverterStringNotPortedError("a string created by the chain carries no handle to find its geometry")
        out.append((item, geometry.get(handle)))
    return out


def _annotated(item, geometry):
    """StringEntities.mEntitiesFound (String.cs:72-83): the start block, end block and label exist."""
    return geometry is not None and geometry.get("start") is not None and geometry.get("end") is not None \
        and item.get("label") is not None


def _collectors(state, host):
    """(L1 list, L2 list) of {row, number, x, y, is_l2} in the state's device order (App.gInverterList,
    App.gL2CollectorList)."""
    devices = state["rows"]["device"]
    if len(devices) > MAX_DEVICES:
        raise InverterStringError(f"more than {MAX_DEVICES} devices")
    listed = _device_numbers(host)
    l1, l2 = [], []
    for device in devices:
        x, y = st.point_of(device["position"])
        entry = {"row": device, "number": device_number(device, listed), "x": x, "y": y, "is_l2": _is_l2(device)}
        (l2 if entry["is_l2"] else l1).append(entry)
    return l1, l2


def _collector_topology(state, host, collector, use_l2):
    """(MPPTs, strings per MPPT) of GetCollectorNumMppt / GetCollectorStringsPerMppt
    (BranchCmd.cs:14068-14100)."""
    num_mppt = strings_per_mppt = 0
    if collector["is_l2"] and use_l2:
        num_mppt = _host_int(host, "L2NumMppt")
        strings_per_mppt = _host_int(host, "L2StringsPerMppt")
    if num_mppt <= 0:
        num_mppt = _setting_int(state, "NumMppt")
        if num_mppt <= 0:
            num_mppt = _host_int(host, "NumMppt")
    if strings_per_mppt <= 0:
        strings_per_mppt = _setting_int(state, "StringPerMppt")
        if strings_per_mppt <= 0:
            strings_per_mppt = _host_int(host, "StringsPerMppt")
    return num_mppt, strings_per_mppt


def _collector_capacity(state, host, collector, use_l2, l1):
    """GetStringsPerCollector (BranchCmd.cs:14102-14112) and its fallback GetStringsPerInverter
    (:10649-10682): the first L1 block of the same number with a positive input count, then the
    type's capacity, then the host default."""
    if collector["is_l2"] and use_l2:
        num_mppt, strings_per_mppt = _collector_topology(state, host, collector, use_l2)
        if num_mppt * strings_per_mppt > 0:
            return num_mppt * strings_per_mppt
    for combiner in l1:
        box = _detail(combiner["row"]).get("box_input_count")
        if combiner["number"] == collector["number"] and type(box) is int and box > 0:
            return box
    assignments = state["setting"].get("InverterTypeAssignments") or {}
    if isinstance(assignments, dict) and str(collector["number"]) in assignments:
        raise InverterStringNotPortedError("a typed inverter's strings-per-inverter capacity")
    if _host_bool(host, "UseCombinerBox"):
        return _host_int(host, "CombinerBoxConnections")
    return _host_int(host, "NumMppt") * _host_int(host, "StringsPerMppt")


def _row_groups(strings):
    """BuildPatternStringRowGroups (BranchCmd.cs:14372-14419) over the ordered unassigned strings,
    each a dict {cx, cy, row, geometry}. Returns [{key, strings, cx, cy}]."""
    groups, fallback = {}, []
    for cable in strings:
        tracker = next((parsed[0] for parsed in map(parse_panel_id, cable["geometry"].get("panels") or [])
                        if parsed is not None), None)
        if tracker is None:
            fallback.append(cable)
            continue
        groups.setdefault("TRACKER:" + tracker, {"key": "TRACKER:" + tracker, "strings": []})["strings"].append(cable)
    for group in geometric_rows(fallback):
        groups[group["key"]] = group

    def sequence(cable):
        subs = [parsed[1] for parsed in map(parse_panel_id, cable["geometry"].get("panels") or [])
                if parsed is not None and parsed[1] >= 0]
        return float(min(subs)) if subs else cable["cx"]

    result = list(groups.values())
    for group in result:
        group["strings"].sort(key=lambda c: (sequence(c), c["cx"], c["cy"]))
        n = len(group["strings"])
        group["cx"] = sum(c["cx"] for c in group["strings"]) / n
        group["cy"] = sum(c["cy"] for c in group["strings"]) / n
    result.sort(key=lambda g: (g["cx"], g["cy"]))
    return result


def geometric_rows(strings):
    """BuildGeometricFallbackRows (BranchCmd.cs:14459-14498): strings by Y then X, a new row whenever
    a centroid's Y leaves the running row mean by more than the tolerance."""
    ordered = sorted(strings, key=lambda c: (c["cy"], c["cx"]))
    if not ordered:
        return []
    min_y, max_y = ordered[0]["cy"], ordered[-1]["cy"]
    tolerance = max(1.0, (max_y - min_y) / max(4.0, math.sqrt(len(ordered)) * 2.0))
    groups, current, current_y = [], None, 0.0
    for cable in ordered:
        if current is None or abs(cable["cy"] - current_y) > tolerance:
            current = {"key": f"GEOROW:{len(groups) + 1}", "strings": []}
            groups.append(current)
            current_y = cable["cy"]
        current["strings"].append(cable)
        count = len(current["strings"])
        current_y = (current_y * (count - 1) + cable["cy"]) / count
    return groups


def _nearest_seed(collector, rows):
    """TryFindNearestAssignableRowSeed (BranchCmd.cs:14516-14555), no electrical zones: the string of
    least squared distance to the collector's insertion point, the first of equals."""
    best, best_d = None, math.inf
    for row in rows:
        for cable in row["strings"]:
            dx, dy = cable["cx"] - collector["x"], cable["cy"] - collector["y"]
            d = dx * dx + dy * dy
            if d < best_d:
                best_d, best = d, (row, cable)
    return best


def seed_outward(strings, seed, limit):
    """BuildSeedOutwardRowOrder (BranchCmd.cs:14557-14585): the seed, then one forward and one back
    at each step, up to `limit`."""
    if not strings or limit <= 0:
        return []
    index = next((i for i, c in enumerate(strings) if c is seed), 0)
    ordered = [strings[index]]
    target = min(len(strings), limit)
    step = 1
    while len(ordered) < target:
        if index + step < len(strings):
            ordered.append(strings[index + step])
        if len(ordered) >= target:
            break
        if index - step >= 0:
            ordered.append(strings[index - step])
        step += 1
    return ordered


def _slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ------------------------------------------------------------------ assign --

def assign_strings(state, host, form_values):
    """AssignStrings on the Studio state, Auto mode by the pattern matcher. `form_values`:
    {"assign_strings_to_<label>s": "OK" | "Cancel", "assign_strings_to_<label>s_mode": "Auto" |
    "Manual", "excess_capacity": "Yes" | "No"} (each read only when its dialog fires). Returns
    (new state, printed lines)."""
    host = _host(host)
    form_values = form_values or {}
    new = copy.deepcopy(state)
    lines = []
    use_l2 = _host_bool(host, "UseL2Collectors")
    use_combiner = _host_bool(host, "UseCombinerBox")
    l1, l2 = _collectors(new, host)
    legacy = "combiner box" if use_combiner else "inverter"
    if use_l2:
        label = "L1/L2 collector" if l1 and l2 else "central inverter" if l2 else "combiner box" if l1 else legacy
    else:
        label = legacy

    dialog = f"assign_strings_to_{_slug(label)}s"
    if form_values.get(dialog) == "Cancel":
        return new, lines
    if form_values.get(dialog) != "OK":
        raise InverterStringError(f"{dialog} needs OK or Cancel")
    mode = form_values.get(dialog + "_mode")
    if mode == MANUAL:
        raise InverterStringNotPortedError("manual string assignment")
    if mode != AUTO:
        raise InverterStringError(f"{dialog}_mode needs Auto or Manual")
    if not _host_bool(host, "UsePatternStringAssignment"):
        raise InverterStringNotPortedError("the legacy cloud string assignment")

    # TryPrepareAssignStringsAuto (BranchCmd.cs:13902-13994).
    all_strings = _strings(new)
    unassigned = []
    for item, geometry in all_strings:
        if _circuit(item) == UNASSIGNED_CIRCUIT:
            if geometry is None:
                raise InverterStringError(f"string {item['string']!r} has no geometry")
            cx, cy = centroid(geometry)
            unassigned.append({"row": item, "geometry": geometry, "cx": cx, "cy": cy})
    targets = l2 if use_l2 and not l1 and l2 else l1
    for collector in targets:
        if collector["number"] is None:
            raise InverterStringError("a target collector's number is unknown (no _number, no DeviceNumbers entry)")
    loads = {}
    for item, _ in all_strings:
        number = parse_circuit(_circuit(item))
        if number > 0:
            loads[number] = loads.get(number, 0) + 1
    if not unassigned:
        lines.append("No unassigned strings found in the drawing.")
        return new, lines
    if not targets:
        lines.append(f"No {label}s found in the drawing. Place {label}s first using AddInverter.")
        return new, lines
    capacity_of = {id(c): _collector_capacity(new, host, c, use_l2, l1) for c in targets}
    total_capacity = sum(max(0, capacity_of[id(c)] - loads.get(c["number"], 0)) for c in targets)
    total = len(unassigned)
    if total_capacity < total:
        # The "Not Enough Capacity" message box; nothing is assigned.
        return new, lines
    if total_capacity > total:
        if form_values.get("excess_capacity") != "Yes":
            if form_values.get("excess_capacity") not in ("Yes", "No"):
                raise InverterStringError("the Excess Capacity dialog needs Yes or No")
            lines.append("Auto-assignment cancelled.")
            return new, lines
    else:
        lines.append(f"{total} unassigned strings, {total_capacity} available slots across "
                     f"{len(targets)} {label}(s).")

    # AssignStringsPattern (BranchCmd.cs:14116-14259).
    zones = new["setting"].get("ElectricalZones")
    if isinstance(zones, list) and zones:
        raise InverterStringNotPortedError("pattern assignment under electrical zones")
    ordered = sorted(unassigned, key=lambda c: (c["cx"], c["cy"]))
    remaining = {}
    for collector in targets:
        if collector["number"] not in remaining:
            remaining[collector["number"]] = max(0, capacity_of[id(collector)] - loads.get(collector["number"], 0))
    numbers = [d.get("string_number") for d in (_detail(item) for item, _ in all_strings)]
    next_string = max([n for n in numbers if type(n) is int] + [0]) + 1
    rows = _row_groups(ordered)
    order = sorted((c for c in targets if remaining.get(c["number"], 0) > 0),
                   key=lambda c: (c["x"], c["y"], c["number"]))
    assignments = []
    for collector in order:
        while remaining.get(collector["number"], 0) > 0:
            found = _nearest_seed(collector, rows)
            if found is None:
                break
            row, seed = found
            placed = 0
            for cable in seed_outward(row["strings"], seed, remaining[collector["number"]]):
                if remaining[collector["number"]] <= 0:
                    break
                assignments.append((cable, collector, next_string))
                next_string += 1
                row["strings"].remove(cable)
                remaining[collector["number"]] -= 1
                placed += 1
            if not row["strings"]:
                rows.remove(row)
            if placed == 0:
                break

    # The write (BranchCmd.cs:14610-14798).
    colour_counter = _host_int(host, "SessionColorCounter", 1)
    colours = {}
    saved = False
    for cable, collector, string_number in assignments:
        number = collector["number"]
        if number not in colours:
            block = _detail(collector["row"]).get("colour")
            block = block if type(block) is int else 256
            if loads.get(number, 0) > 0 and block not in (0, 7):
                colour = block
            else:
                family = type_colours(new, number)
                colour = family[colour_counter % len(family)]
                colour_counter += 1
            detail = dict(_detail(collector["row"]))
            detail["colour"] = colour
            collector["row"]["_detail"] = detail
            colours[number] = colour
            if not saved and host.get("AutoTagHeight") is not None:
                # StringPlacement.cs:343-364: the auto tag height, one drawing-properties save.
                height = host["AutoTagHeight"]
                if type(height) not in (int, float) or not math.isfinite(height) or height <= 0:
                    raise InverterStringError("host input AutoTagHeight must be a positive number")
                new["setting"]["TagHeight"] = float(height)
                st.save_drawing_properties(new)
                saved = True
        item, geometry = cable["row"], cable["geometry"]
        if not _annotated(item, geometry):
            raise InverterStringNotPortedError("a string without its start block, end block or label "
                                               "(the annotation hydration)")
        num_mppt, strings_per_mppt = _collector_topology(new, host, collector, use_l2)
        letter = mppt_letter(string_number, num_mppt, strings_per_mppt)
        tag = tag_text(number, string_number, num_mppt, strings_per_mppt)
        detail = dict(_detail(item))
        detail.update({"circuit": tag, "num_mppt": num_mppt, "strings_per_mppt": strings_per_mppt,
                       "string_number": string_number, "mppt": letter,
                       "strings_on_inverter": num_mppt * strings_per_mppt,
                       "terminal_number": TERMINAL_NUMBER, "color_counter": colours[number]})
        item.update({"device": number, "input": ord(letter) - ord("a") + 1, "label": tag,
                     "colour": colours[number], "_detail": detail})

    # :14316-14346; elapsed is at least one second in the plugin's own rounding.
    lines.append(f"Pattern assignment complete. {len(assignments):,} strings assigned to "
                 f"{len(targets):,} collectors in 1 seconds.")
    st.sort_rows(new)
    return new, lines


# ------------------------------------------------------------------- colour --

def color_strings(state, host):
    """LEAFCOLORSTRINGS on the Studio state: every string recoloured by its inverter. Returns (new
    state, printed lines)."""
    _host(host)
    new = copy.deepcopy(state)
    strings = _strings(new)
    if not strings:
        layer = new["setting"].get("StringLayer", st.DECLARED_DEFAULTS["StringLayer"])
        return new, [f"LEAFCOLORSTRINGS: no strings found on layer {layer}."]
    inverter_colour = {}
    recoloured = 0
    for item, geometry in strings:
        number = parse_circuit(_circuit(item))
        if number <= 0:
            colour = UNASSIGNED_COLOUR
        elif number in inverter_colour:
            colour = inverter_colour[number]
        else:
            family = type_colours(new, number)
            colour = family[(number - 1) % len(family)] if family else UNASSIGNED_COLOUR
            inverter_colour[number] = colour
        if not _annotated(item, geometry):
            continue
        item["colour"] = colour
        recoloured += 1
        # :11176-11204: an in-block module overlay per panel whose id names a tracker sub-panel.
        if any(parse_panel_id(p) is not None for p in geometry.get("panels") or []):
            raise InverterStringNotPortedError("in-block module overlays for tracker panel ids")
    lines = [f"LEAFCOLORSTRINGS: recoloured {recoloured} of {len(strings)} strings across "
             f"{len(inverter_colour)} inverter(s); drew 0 in-block module overlay(s)."]
    st.sort_rows(new)
    return new, lines
