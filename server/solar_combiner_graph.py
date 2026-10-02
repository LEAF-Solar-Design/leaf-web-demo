"""Combiner placement on the shared design graph: LEAFCOMBINERAUTO (solar_inverter_cabling.combiner_auto_place)
run on the route-aware bridge state of a graph and persisted back onto it, with the drawing's recorded combiner
intake bound to that graph first (wave-16 record sf-w2-combiners, piece one).

Two pure functions, no I/O, no clock, no network, no global state; inputs are never mutated:

  bind_intake(graph, intake) -> binding
      Proves that a combiner intake (format combiner-intake-v1, stage input-before-placement: the plugin's
      input-before-placement dump) describes this graph, and names the graph entity every intake L2 and every
      intake string stands for. The graph wins on everything it owns; any disagreement fails closed.
  place_combiners(graph, intake, panel_groups, *, hardware, new_id=None, created_at=None) -> (graph, receipt)
      bind_intake, then the kernel on solar_electrical_route_bridge.state_from_graph(graph), then
      solar_electrical_route_bridge.graph_from_state: one validated graph carrying the new combiner boxes, every
      string's new collector and input, each combiner's L2 feed and MPPT slot, the 2 homerun routes of every
      string and one feeder route per combiner. The result is proven before it is returned (every intake string
      served by exactly one combiner, every new combiner fed by exactly one L2, the hardware supplied), so a
      caller publishes all of it or none of it.

Frozen decisions (sf-w2-combiners):
  - Why bind and not build. The engine reads, per string, the dump's row index, column span, physical row key,
    group id and centroid, and per panel group the CAD panel polylines (solar_inverter_combiner.build_strings,
    reconstruct_panel_groups). The graph carries none of these, so an intake cannot be built from the graph
    alone. The intake is the drawing's own recorded dump, bound to the live graph before its geometry or numbers
    are used.
  - L2s. Every intake l2Inverters entry names exactly one graph L2 inverter with that number whose position,
    divided by the project's meters_per_unit, lies within MATCH_EPSILON (cab.MATCH_EPSILON, drawing units) of
    its InsertPt, and every graph L2 is named exactly once.
  - Strings. Every intake preBuiltStrings entry names exactly one graph string whose route's first point lies
    within MATCH_EPSILON of its endpointA and last point within MATCH_EPSILON of its endpointB (the kernel's own
    rule, cab._string_ids), and every graph string is named exactly once. The entry's L2Number is the engine's
    partition, not the drawing's prior assignment, and is never compared with the graph.
  - Context. The intake's metersPerUnit equals the graph's exactly; useL2Collectors is true; every graph L2 has
    mppt_count == l2NumMppt, total_dc_inputs == l2NumMppt * l2StringsPerMppt and collector_capacity ==
    l1CollectorsPerL2; combinerBoxConnections is at least 1 (it becomes each new box's total_dc_inputs).
  - Existing combiners. A graph holding any L1 inverter, or an intake recording existingL1s, is refused: the
    kernel does not port that path.
  - Hardware. A new combiner box takes model, max_dc_voltage and max_ac_power_kw from `hardware` only; none is
    COMBINER_HARDWARE_REQUIRED, checked before the kernel runs. The module never invents hardware.
  - Host. The kernel's host is {"UseL2Collectors": True, "L1CollectorsPerL2": l1CollectorsPerL2,
    "CombinerSymbolScale": SYMBOL_SCALE}; the symbol scale sizes block symbols only and the bridge never
    projects it.
  - Errors. Every refusal of this module is CombinerGraphError (a ValueError whose message is its code alone,
    one of CODES). The validator's GraphValidationError and the bridge's ElectricalBridgeError propagate
    unchanged. The kernel's refusals become COMBINER_NOT_PORTED (an unported branch: tracker rows, cloud
    placement) or COMBINER_KERNEL_REFUSED (anything else); a run that places no combiner is
    COMBINER_NOTHING_PLACED.

Bounds: an iterative intake pre-pass bounds text and keys by MAX_TEXT, lists by MAX_PANELS, depth by
MAX_INTAKE_DEPTH and visited values by MAX_INTAKE_NODES before hashing or reading intake fields. At most
MAX_L2 intake L2s and MAX_STRINGS intake strings (the engine's own bounds); the graph's own bounds hold for
the rest. Linear in strings and L2s: one dict per lookup, a unit-grid point index for the
endpoint match (never a scan of strings per string).
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math

import solar_electrical_route_bridge as rb
from solar_design_graph import validate_graph

legacy = rb.legacy
st = rb.st
cab = rb.cab
comb = cab.comb
canonical_sha256 = rb.canonical_sha256

BINDING_FORMAT = "leaf.solar-combiner-binding.v1"
RECEIPT_FORMAT = "leaf.solar-combiner-placement.v1"
MATCH_EPSILON = cab.MATCH_EPSILON          # drawing units
MAX_L2 = comb.MAX_L2                       # 1,000
MAX_STRINGS = comb.MAX_STRINGS             # 20,000
MAX_PANELS = comb.MAX_PANELS               # 200,000
MAX_INTAKE_DEPTH = 32
MAX_INTAKE_NODES = 64 * MAX_PANELS
MAX_NUMBER = legacy.MAX_NUMBER             # 1,000,000
MAX_TEXT = legacy.MAX_TEXT                 # 4,096
MAX_LINES = 64
MAX_LINE_CHARS = 512
SYMBOL_SCALE = 1.0
KERNEL_METRES_PER_UNIT = 0.0254  # the legacy cabling kernels compute feet as inches / 12
PLAN = {"combiner_input_plan": cab.INPUT_PLAN_APPLY}
HARDWARE_KEYS = frozenset({"model", "max_dc_voltage", "max_ac_power_kw"})
CODES = ("COMBINER_L2_MODE_REQUIRED", "COMBINER_EXISTING_L1", "COMBINER_INTAKE_INVALID",
         "COMBINER_INTAKE_UNITS_MISMATCH", "COMBINER_INTAKE_CONTEXT_MISMATCH", "COMBINER_INTAKE_L2_MISMATCH",
         "COMBINER_INTAKE_STRING_MISMATCH", "COMBINER_OUTLINES_INVALID", "COMBINER_HARDWARE_REQUIRED",
         "COMBINER_HARDWARE_INVALID", "COMBINER_NOT_PORTED", "COMBINER_KERNEL_REFUSED",
         "COMBINER_NOTHING_PLACED", "COMBINER_POSTCONDITION_FAILED")


class CombinerGraphError(ValueError):
    """A refusal of this module; the message is its code alone."""


def _fail(code):
    raise CombinerGraphError(code)


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _bound_intake(intake):
    """Visit every value once; check dict keys as their item is reached, without recursion or child lists."""
    stack = [(iter((intake,)), 1, False)]
    visited = 0
    while stack:
        children, depth, items = stack[-1]
        try:
            value = next(children)
        except StopIteration:
            stack.pop()
            continue
        if items:
            key, value = value
            if type(key) is not str or len(key) > MAX_TEXT:
                _fail("COMBINER_INTAKE_INVALID")
        visited += 1
        if visited > MAX_INTAKE_NODES or depth > MAX_INTAKE_DEPTH:
            _fail("COMBINER_INTAKE_INVALID")
        if isinstance(value, dict):
            stack.append((iter(value.items()), depth + 1, True))
        elif isinstance(value, list):
            if len(value) > MAX_PANELS:
                _fail("COMBINER_INTAKE_INVALID")
            stack.append((iter(value), depth + 1, False))
        elif type(value) is str:
            if len(value) > MAX_TEXT:
                _fail("COMBINER_INTAKE_INVALID")
        elif type(value) in (int, float):
            if not _finite_number(value):
                _fail("COMBINER_INTAKE_INVALID")
        elif value is not None and type(value) is not bool:
            _fail("COMBINER_INTAKE_INVALID")


def _dict(value):
    if not isinstance(value, dict):
        _fail("COMBINER_INTAKE_INVALID")
    return value


def _list(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        _fail("COMBINER_INTAKE_INVALID")
    return value


def _point(value, what, upper=False):
    try:
        return comb._point(value, what, upper=upper)
    except (comb.PlacementError, OverflowError):
        _fail("COMBINER_INTAKE_INVALID")


def _du(point, mpu):
    """A graph point in the intake's drawing units, by the kernel's own arithmetic: graph metres to kernel
    inches (the state bridge's projection), then inches to drawing units (cab._source_xy). Binding and the
    kernel's matchers then compare bit-identical numbers, so an intake that binds is never refused by a
    matcher for a rounding difference."""
    k = mpu / KERNEL_METRES_PER_UNIT
    return cab._source_xy((float(point[0]) / KERNEL_METRES_PER_UNIT, float(point[1]) / KERNEL_METRES_PER_UNIT), k)


# ------------------------------------------------------------------ binding --

def _context(context, l2s):
    """The intake's commandContext checked against every graph L2; returns l1CollectorsPerL2."""
    use = context.get("useL2Collectors")
    mppt, per_mppt = context.get("l2NumMppt"), context.get("l2StringsPerMppt")
    cap, box = context.get("l1CollectorsPerL2"), context.get("combinerBoxConnections")
    if type(use) is not bool or not all(_int(v, 1, MAX_NUMBER) for v in (mppt, per_mppt, cap, box)):
        _fail("COMBINER_INTAKE_INVALID")
    if use is not True:
        _fail("COMBINER_INTAKE_CONTEXT_MISMATCH")
    for inverter in l2s:
        if inverter["mppt_count"] != mppt or inverter["total_dc_inputs"] != mppt * per_mppt \
                or inverter.get("collector_capacity") != cap:
            _fail("COMBINER_INTAKE_CONTEXT_MISMATCH")
    return cap


def _bind_l2(raw, l2s, mpu):
    by_number = {inverter["number"]: inverter for inverter in l2s}
    if len(by_number) != len(l2s):
        _fail("COMBINER_INTAKE_L2_MISMATCH")
    bound = {}
    for i, item in enumerate(raw):
        item = _dict(item)
        number = item.get("Number")
        if not _int(number, 1, MAX_NUMBER):
            _fail("COMBINER_INTAKE_INVALID")
        point = _point(item.get("InsertPt"), f"l2Inverters[{i}].InsertPt", upper=True)
        inverter = by_number.get(number)
        if inverter is None or str(number) in bound or cab._dist(_du(inverter["position"], mpu), point) > MATCH_EPSILON:
            _fail("COMBINER_INTAKE_L2_MISMATCH")
        bound[str(number)] = inverter["id"]
    if len(bound) != len(l2s):
        _fail("COMBINER_INTAKE_L2_MISMATCH")
    return bound


def _bind_strings(raw, strings, mpu):
    index = cab._PointIndex()
    for string in strings:
        route = string["route"]
        if route:
            index.add(_du(route[0], mpu), (string["id"], _du(route[-1], mpu)))
    bound, used = {}, set()
    for i, item in enumerate(raw):
        item = _dict(item)
        number = item.get("StringNumber")
        if not _int(number, 0, MAX_NUMBER) or str(number) in bound:
            _fail("COMBINER_INTAKE_INVALID")
        a = _point(item.get("endpointA"), f"preBuiltStrings[{i}].endpointA")
        b = _point(item.get("endpointB"), f"preBuiltStrings[{i}].endpointB")
        hits = [ident for ident, last in index.near(a) if cab._dist(last, b) <= MATCH_EPSILON]
        if len(hits) != 1 or hits[0] in used:
            _fail("COMBINER_INTAKE_STRING_MISMATCH")
        used.add(hits[0])
        bound[str(number)] = hits[0]
    if len(used) != len(strings):
        _fail("COMBINER_INTAKE_STRING_MISMATCH")
    return bound


def _bind(g, intake):
    """(binding, l1CollectorsPerL2) of an intake on a validated graph."""
    mpu = legacy._meters_per_unit(g)
    if g["settings"]["use_l2_collectors"] is not True:
        _fail("COMBINER_L2_MODE_REQUIRED")
    if any(not inverter["is_l2"] for inverter in g["inverters"]):
        _fail("COMBINER_EXISTING_L1")
    intake = _dict(intake)
    _bound_intake(intake)
    if intake.get("format") != comb.INTAKE_FORMAT or intake.get("stage") != comb.INTAKE_STAGE:
        _fail("COMBINER_INTAKE_INVALID")
    inputs, context, drawing = (_dict(intake.get(key)) for key in ("inputs", "commandContext", "drawing"))
    if _list(inputs.get("existingL1s", []), MAX_NUMBER):
        _fail("COMBINER_EXISTING_L1")
    l2_raw = _list(inputs.get("l2Inverters"), MAX_L2)
    strings_raw = _list(inputs.get("preBuiltStrings"), MAX_STRINGS)
    units = drawing.get("metersPerUnit")
    if not _finite_number(units):
        _fail("COMBINER_INTAKE_INVALID")
    if float(units) != mpu:
        _fail("COMBINER_INTAKE_UNITS_MISMATCH")
    l2s = [inverter for inverter in g["inverters"] if inverter["is_l2"]]
    cap = _context(context, l2s)
    l2 = _bind_l2(l2_raw, l2s, mpu)
    strings = _bind_strings(strings_raw, g["strings"], mpu)
    try:
        intake_sha = canonical_sha256(intake)
    except (TypeError, ValueError, RecursionError):
        _fail("COMBINER_INTAKE_INVALID")
    return {"format": BINDING_FORMAT, "graph_sha256": canonical_sha256(g), "intake_sha256": intake_sha,
            "l2": l2, "strings": strings}, cap


def bind_intake(graph, intake):
    """The binding of `intake` to `graph` (see the module docstring); fails closed on any disagreement."""
    binding, _ = _bind(validate_graph(graph), intake)
    return binding


# ------------------------------------------------------------- kernel units --

def _kernel_outlines(panel_groups, mpu):
    """The stored outlines in kernel inches; the list itself on an inch drawing."""
    if mpu == KERNEL_METRES_PER_UNIT:
        return panel_groups
    groups = deepcopy(panel_groups)
    for group in groups:
        if not group.get("outlines"):
            continue
        outlines = []
        for ring in group["outlines"]:
            points = []
            for point in ring:
                x = point[0] * mpu / KERNEL_METRES_PER_UNIT
                y = point[1] * mpu / KERNEL_METRES_PER_UNIT
                if not math.isfinite(x) or not math.isfinite(y):
                    _fail("COMBINER_OUTLINES_INVALID")
                points.append([x, y, *point[2:]])
            outlines.append(points)
        group["outlines"] = outlines
    return groups


# ---------------------------------------------------------------- placement --

def _hardware(value):
    if value is None:
        _fail("COMBINER_HARDWARE_REQUIRED")
    if not isinstance(value, dict) or set(value) != HARDWARE_KEYS or type(value["model"]) is not str \
            or not 1 <= len(value["model"]) <= MAX_TEXT \
            or not all(_finite_number(value[k]) and 0 < value[k] <= 1e6
                       for k in ("max_dc_voltage", "max_ac_power_kw")):
        _fail("COMBINER_HARDWARE_INVALID")
    return {"model": value["model"], "max_dc_voltage": value["max_dc_voltage"],
            "max_ac_power_kw": value["max_ac_power_kw"]}


def _prove(result, binding, after, hardware):
    """Every intake string served by exactly one new combiner, every combiner fed once, the hardware supplied."""
    by_id = {inverter["id"]: inverter for inverter in result["inverters"]}
    l1 = {inverter["number"]: inverter for inverter in result["inverters"] if not inverter["is_l2"]}
    strings = {string["id"]: string for string in result["strings"]}
    try:
        served = {str(k): v for k, v in after["setting"][st.L1_SETTING].items()}
    except (KeyError, AttributeError, TypeError):
        _fail("COMBINER_POSTCONDITION_FAILED")
    ok = set(served) == set(binding["strings"])
    for number, ident in binding["strings"].items() if ok else ():
        target = l1.get(served[number])
        string = strings[ident]
        ok = ok and target is not None and string["inverter_ref"] == target["id"] == string["to_ref"]
    holders = Counter(a["string_ref"] for inverter in result["inverters"] for a in inverter["input_assignments"])
    ok = ok and set(holders) == set(strings) and all(n == 1 for n in holders.values())
    ok = ok and all(not inverter["input_assignments"] for inverter in result["inverters"] if inverter["is_l2"])
    fed = Counter(f["inverter_ref"] for inverter in result["inverters"] if inverter["is_l2"]
                  for f in inverter["l1_assignments"])
    ok = ok and set(fed) == {inverter["id"] for inverter in l1.values()} and all(n == 1 for n in fed.values())
    for inverter in l1.values():
        ok = ok and inverter.get("equipment_type") == "combiner_box" and inverter["l2_ref"] in by_id \
            and by_id[inverter["l2_ref"]]["is_l2"] \
            and all(inverter[k] == hardware[k] for k in HARDWARE_KEYS)
    legs = Counter((r["from_ref"], r["route_kind"]) for r in result["routes"] if r["route_kind"] in rb.SEGMENT_OF)
    ok = ok and set(legs) == {(ident, kind) for ident in strings for kind in rb.SEGMENT_OF} \
        and all(n == 1 for n in legs.values())
    ok = ok and all(r["to_ref"] == strings[r["from_ref"]]["inverter_ref"]
                    for r in result["routes"] if r["route_kind"] in rb.SEGMENT_OF)
    feeders = Counter(r["from_ref"] for r in result["routes"] if r["route_kind"] == "feeder")
    ok = ok and set(feeders) == {inverter["id"] for inverter in l1.values()} and all(n == 1 for n in feeders.values())
    ok = ok and all(r["to_ref"] == by_id[r["from_ref"]]["l2_ref"]
                    for r in result["routes"] if r["route_kind"] == "feeder")
    if not ok:
        _fail("COMBINER_POSTCONDITION_FAILED")
    return l1


def place_combiners(graph, intake, panel_groups, *, hardware, new_id=None, created_at=None):
    """LEAFCOMBINERAUTO on `graph` with its bound `intake` (see the module docstring); returns the validated
    graph and the placement receipt."""
    g = validate_graph(graph)
    binding, cap = _bind(g, intake)
    values = _hardware(hardware)
    try:
        cab.validate_outlines(panel_groups)
    except (cab.InverterCablingError, OverflowError, TypeError, ValueError):
        _fail("COMBINER_OUTLINES_INVALID")
    mpu = legacy._meters_per_unit(g)
    scale = mpu / KERNEL_METRES_PER_UNIT
    kernel_groups = _kernel_outlines(panel_groups, mpu)
    state, state_binding = rb.state_from_graph(g, metres_per_unit=KERNEL_METRES_PER_UNIT)
    host = {"UseL2Collectors": True, "L1CollectorsPerL2": cap, "CombinerSymbolScale": SYMBOL_SCALE}
    try:
        after, lines = cab.combiner_auto_place(state, kernel_groups, host, dict(PLAN), intake,
                                               coordinate_scale=scale)
    except cab.InverterCablingNotPortedError:
        _fail("COMBINER_NOT_PORTED")
    except (cab.InverterCablingError, ValueError, ArithmeticError):
        _fail("COMBINER_KERNEL_REFUSED")
    if not cab.levels(after)[0]:
        _fail("COMBINER_NOTHING_PLACED")
    result, _ = rb.graph_from_state(g, after, state_binding, defaults={"combiner_box": values},
                                    new_id=new_id, created_at=created_at,
                                    metres_per_unit=KERNEL_METRES_PER_UNIT)
    l1 = _prove(result, binding, after, values)
    kinds = Counter(r["route_kind"] for r in result["routes"])
    receipt = {
        "format": RECEIPT_FORMAT,
        "graph_sha256_before": binding["graph_sha256"],
        "graph_sha256_after": canonical_sha256(result),
        "intake_sha256": binding["intake_sha256"],
        "combiners": len(l1),
        "strings_served": len(binding["strings"]),
        "l2_fed": sum(1 for inverter in result["inverters"] if inverter["is_l2"] and inverter["l1_assignments"]),
        "homerun_routes": kinds["start homerun"] + kinds["end homerun"],
        "feeder_routes": kinds["feeder"],
        "combiner_ids": [inverter["id"] for inverter in result["inverters"] if not inverter["is_l2"]],
        "lines": [str(line)[:MAX_LINE_CHARS] for line in lines[:MAX_LINES]],
    }
    return result, receipt
