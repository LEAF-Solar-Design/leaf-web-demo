"""Assign strings to inverters or L1 collectors on the design graph: Studio's AssignStrings (the plugin's
Auto mode, the pattern matcher), ported literally in server/solar_inverter_strings.py (contract G35,
receipt assign-strings/rooftop-inverters-i2).

The graph never meets the kernel directly. server/solar_electrical_state_bridge.py (the one frozen
mapping) projects it into a G35 state, the kernel assigns every unassigned string (circuit "-") to the
drawing's target collectors (the central inverters when an L1/L2 drawing has L2 devices and no L1,
otherwise the L1 devices), and the bridge writes the new circuits and inputs back onto a copy of the
graph, which the graph validator checks. This builtin only derives the kernel's host inputs from the
graph, maps the kernel's silent outcomes to named refusals, stales dependent outputs and advances the
revision.

Host inputs the plugin reads from the capture host's settings or session, derived here:
  UseL2Collectors      the graph's settings.use_l2_collectors (the bridge's mode; never the state's)
  UsePatternStringAssignment  True: the pattern matcher is the only ported path
  UseCombinerBox       False: a combiner box's capacity is its own input count (the bridge's
                       box_input_count), never the host's CombinerBoxConnections
  L2NumMppt, L2StringsPerMppt / NumMppt, StringsPerMppt
                       the target collectors' one shared (mppt_count, total_dc_inputs / mppt_count);
                       targets that do not share one refuse STRING_CAPACITY_UNSUPPORTED, because the
                       kernel reads one host value for every collector and the validator checks each
  SessionColorCounter  1: every Studio run is a fresh session (App.mColorCtr's initial value, the
                       capture's value)
  AutoTagHeight        absent: no drawing-properties save (the bridge carries no tag height)
  DeviceNumbers        []: the bridge gives every device its number

Every string's string number is overlaid from its circuit (the kernels' grammar), so numbering continues
one past the largest circuit number on the drawing, as the plugin's tag records make it.
Existing numbers plus the unassigned count must fit MAX_STRING_NUMBER. Partial kernel assignments refuse
STRING_ASSIGNMENT_INCOMPLETE; write-back mismatches refuse STRING_ASSIGNMENT_MAPPING_FAILED, and input
letters or input numbers outside their collector's MPPT capacity refuse INVERTER_CAPACITY_EXCEEDED
before publication. Integral-float hardware counts are normalized only for the kernel; unchanged
hardware keeps its stored representation on the published graph.

NOT REPRODUCED, and named rather than faked: the colours. The kernel colours each collector's strings
(and the block) from the session counter, or keeps the block colour of a collector already holding
strings. The v1 graph carries no colour and the bridge projects none, so a collector's block colour is
unknown (the kernel reports 256, ByLayer, for it) and nothing is persisted: the operation answer returns
the kernel's colours; a string's colour on screen follows from its inverter (LEAFCOLORSTRINGS, the
follow-up read). Refused, not guessed: a drawing with electrical zones (the plugin's zone-aware pattern
path is not ported), Manual mode and the legacy cloud path (never offered).

Contract: fails closed. Every check runs before the private copy is written; the bridge and the graph
validator have the last word on the result; the input graph and request are never mutated. Never
creates or removes equipment (the bridge refuses an engine-created device without caller defaults, and
none are passed). Cost: the kernel's O(collectors x strings) plan plus two graph validations.
"""
import solar_electrical_state_bridge as bridge
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

kernel = bridge._load_sibling("solar_inverter_strings")

TOOL = "solar-assign-strings"
REASON = "strings_assigned"
MAX_STRING_NUMBER = 1_000_000
REQUEST_KEYS = frozenset({"expected_rev", "accept_excess_capacity"})
# The four collector labels AssignStrings can print; one dialog answer each, so the kernel finds its own.
LABELS = ("inverter", "combiner box", "central inverter", "L1/L2 collector")
CANCELLED = "Auto-assignment cancelled."


def _refuse(code):
    raise GraphValidationError(code)


def _targets(graph):
    """The kernel's target collectors (GetAssignStringsTargetCollectors), read from the graph."""
    l1 = [inverter for inverter in graph["inverters"] if not inverter["is_l2"]]
    l2 = [inverter for inverter in graph["inverters"] if inverter["is_l2"]]
    return l2 if graph["settings"]["use_l2_collectors"] and l2 and not l1 else l1


def _hardware_count(value):
    """Graph integer counts may be integral floats; the kernel host requires actual ints."""
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int:
        _refuse("STRING_CAPACITY_UNSUPPORTED")
    return value


def _shared_topology(collectors):
    """(MPPTs, strings per MPPT) every collector shares, else a named refusal; (0, 0) for none."""
    pairs = {(_hardware_count(inverter["mppt_count"]), _hardware_count(inverter["total_dc_inputs"]))
             for inverter in collectors}
    if not pairs:
        return 0, 0
    if len(pairs) != 1:
        _refuse("STRING_CAPACITY_UNSUPPORTED")
    (mppts, inputs), = pairs
    if mppts < 1 or inputs < mppts or inputs % mppts:
        _refuse("STRING_CAPACITY_UNSUPPORTED")
    return mppts, inputs // mppts


def _host(graph, targets):
    mode = graph["settings"]["use_l2_collectors"]
    l2_targets = bool(targets) and targets[0]["is_l2"]
    shared = _shared_topology([inverter for inverter in targets
                               if inverter.get("equipment_type") != "combiner_box"])
    l2 = shared if l2_targets else (0, 0)
    l1 = (0, 0) if l2_targets else shared
    return {"UseL2Collectors": mode, "UseCombinerBox": False, "UsePatternStringAssignment": True,
            "L2NumMppt": l2[0], "L2StringsPerMppt": l2[1], "NumMppt": l1[0], "StringsPerMppt": l1[1],
            "CombinerBoxConnections": 0, "SessionColorCounter": 1, "DeviceNumbers": []}


def _forms(accept):
    forms = {"excess_capacity": "Yes" if accept else "No"}
    for label in LABELS:
        dialog = f"assign_strings_to_{kernel._slug(label)}s"
        forms.update({dialog: "OK", dialog + "_mode": kernel.AUTO})
    return forms


def _number_strings(state):
    """Each row's string number from its circuit, so numbering continues past the largest."""
    largest, unassigned = 0, 0
    for row in state["rows"]["string-assignment"]:
        if row["_detail"]["circuit"] == bridge.UNASSIGNED:
            unassigned += 1
        match = bridge.CIRCUIT.fullmatch(row["_detail"]["circuit"].strip())
        if match is None:
            continue
        number = int(match.group("string"))
        if number > MAX_STRING_NUMBER:
            _refuse("STRING_NUMBER_OUT_OF_RANGE")
        row["_detail"]["string_number"] = number
        largest = max(largest, number)
    if largest + unassigned > MAX_STRING_NUMBER:
        _refuse("STRING_NUMBER_OUT_OF_RANGE")


def _check_assignment_result(before, result, binding, rows, assigned):
    """Every assigned circuit must survive the bridge with one input on its collector."""
    strings = {string["id"]: string for string in result["strings"]}
    inverters = {inverter["id"]: inverter for inverter in result["inverters"]}
    for handle in assigned:
        string = strings[binding["strings"][handle]["id"]]
        collector = inverters.get(string["inverter_ref"])
        if (collector is None or string["circuit_tag"] != rows[handle]["_detail"]["circuit"]
                or sum(a["string_ref"] == string["id"] for a in collector["input_assignments"]) != 1):
            _refuse("STRING_ASSIGNMENT_MAPPING_FAILED")
    old = {inverter["id"]: inverter for inverter in before["inverters"]}
    for inverter in result["inverters"]:
        if inverter["input_assignments"] == old[inverter["id"]]["input_assignments"]:
            continue
        mppts = _hardware_count(inverter["mppt_count"])
        inputs_per_mppt = _hardware_count(inverter["total_dc_inputs"]) // mppts
        for assignment in inverter["input_assignments"]:
            letter = assignment["mppt_letter"]
            if (len(letter) != 1 or not "A" <= letter <= "Z"
                    or ord(letter) - ord("A") >= mppts
                    or assignment["input_number"] >= inputs_per_mppt):
                _refuse("INVERTER_CAPACITY_EXCEEDED")


def _invalidate(before, after):
    old_strings = {string["id"]: string for string in before["strings"]}
    edited = {string["id"] for string in after["strings"]
              if (string["inverter_ref"], string["circuit_tag"]) !=
              (old_strings[string["id"]]["inverter_ref"], old_strings[string["id"]]["circuit_tag"])}
    old_inverters = {inverter["id"]: inverter for inverter in before["inverters"]}
    sources = set(edited) | {inverter["id"] for inverter in after["inverters"]
                             if inverter["input_assignments"] != old_inverters[inverter["id"]]["input_assignments"]}
    for route in after["routes"]:
        if route["from_ref"] in edited or route["to_ref"] in edited:
            route["validity"] = {"state": "stale", "reasons": [REASON]}
            sources.add(route["id"])
    for schedule in after["schedules"]:
        if not sources.isdisjoint(schedule["source_refs"]):
            schedule["validity"] = {"state": "stale", "reasons": [REASON]}
    return edited


def assign_strings(graph, request):
    _bounded_json(request)
    if (type(request) is not dict or set(request) != REQUEST_KEYS
            or type(request["expected_rev"]) is not int
            or type(request["accept_excess_capacity"]) is not bool):
        _refuse("INVALID_STRING_ASSIGNMENT_REQUEST")
    before = checked_graph(graph, request["expected_rev"])
    if before["electrical_zones"]:
        _refuse("ELECTRICAL_ZONES_UNSUPPORTED")
    unassigned = [string for string in before["strings"] if string["inverter_ref"] is None]
    if not unassigned:
        _refuse("NO_UNASSIGNED_STRINGS")
    targets = _targets(before)
    if not targets:
        _refuse("NO_STRING_COLLECTORS")
    if any(not string["route"] for string in unassigned):
        _refuse("STRING_ROUTE_REQUIRED")
    host = _host(before, targets)
    try:
        state, binding = bridge.state_from_graph(before)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    for row in state["rows"]["device"]:
        detail = row["_detail"]
        if "box_input_count" in detail:
            detail["box_input_count"] = _hardware_count(detail["box_input_count"])
    _number_strings(state)
    try:
        after_state, lines = kernel.assign_strings(state, host, _forms(request["accept_excess_capacity"]))
    except kernel.InverterStringNotPortedError:
        raise GraphValidationError("STRING_ASSIGNMENT_NOT_PORTED") from None
    except kernel.InverterStringError:
        raise GraphValidationError("STRING_ASSIGNMENT_MAPPING_FAILED") from None
    rows = {row["string"]: row for row in after_state["rows"]["string-assignment"]}
    assigned = [handle for handle, item in binding["strings"].items()
                if item["circuit"] == bridge.UNASSIGNED and rows[handle]["_detail"]["circuit"] != bridge.UNASSIGNED]
    if not assigned:
        _refuse("EXCESS_CAPACITY_DECLINED" if lines == [CANCELLED] else
                "STRING_CAPACITY_INSUFFICIENT" if lines == [] else "STRING_ASSIGNMENT_MAPPING_FAILED")
    if any(item["circuit"] == bridge.UNASSIGNED and
           rows[handle]["_detail"]["circuit"] == bridge.UNASSIGNED
           for handle, item in binding["strings"].items()):
        _refuse("STRING_ASSIGNMENT_INCOMPLETE")
    try:
        result, _ = bridge.graph_from_state(before, after_state, binding)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    old_inverters = {inverter["id"]: inverter for inverter in before["inverters"]}
    for inverter in result["inverters"]:
        previous = old_inverters[inverter["id"]]
        for field in ("mppt_count", "total_dc_inputs", "collector_capacity"):
            if field in previous and inverter.get(field) == previous[field]:
                inverter[field] = previous[field]
    _check_assignment_result(before, result, binding, rows, assigned)
    _invalidate(before, result)
    result = finish_mutation(before, result, TOOL)
    strings = {string["id"]: string for string in result["strings"]}
    colours = [{"string_ref": binding["strings"][handle]["id"],
                "inverter_ref": strings[binding["strings"][handle]["id"]]["inverter_ref"],
                "circuit_tag": strings[binding["strings"][handle]["id"]]["circuit_tag"],
                "colour": rows[handle]["colour"]}
               for handle in sorted(assigned, key=lambda h: int(h, 16))]
    collector_colours = {binding["devices"][row["_pair"]]["id"]: row["_detail"]["colour"]
                         for row in after_state["rows"]["device"] if row["_detail"]["colour"] is not None}
    return {"graph": result, "assigned": colours, "collector_colours": collector_colours, "lines": list(lines)}


def input_readiness(graph):
    """Whether AssignStrings can run on this graph; never raises on a valid graph."""
    if graph["electrical_zones"]:
        return {"input_ready": False, "input_reason": "electrical_zones_unsupported"}
    if not any(string["inverter_ref"] is None for string in graph["strings"]):
        return {"input_ready": False, "input_reason": "unassigned_strings_required"}
    if not _targets(graph):
        return {"input_ready": False, "input_reason": "string_collectors_required"}
    return {"input_ready": True, "input_reason": None}


OPERATIONS = {"assign-strings": assign_strings}


def run(graph, params):
    _bounded_json(params)
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        _refuse("INVALID_STRING_ASSIGNMENT_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](graph, request)["graph"]
