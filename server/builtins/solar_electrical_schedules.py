"""Electrical schedules (InsertSchedules, InsertSchedulesCmd.cs:31-121) over the W1 design graph, as a read
that writes one revision-bound JSON artifact.

The plugin inserts up to four AutoCAD tables (equipment, combiner / inverter, string, feeder) built from the
drawing's cable records. Studio builds the same tables with the ported kernel
(server/solar_inverter_outputs.py build_schedules and table_cells, read-only here) from the kernel state the
electrical state bridge (server/solar_electrical_state_bridge.py state_from_graph) projects from the graph, and
publishes the cell grids as ElectricalSchedules.json through server/solar_artifacts.py. Nothing is inserted into
the drawing and no graph entity changes.

What the bridge does not project and this read adds, one way and read-only:
  - each string row's panel count (the graph string's module_count), which the string schedule prints;
  - one dc-homerun cable row per W1 homerun route ("start homerun" or "end homerun" from a string), its points
    converted from metres to inches, which the HR (ft) column sums with the string's own route;
  - one feeder cable row per "feeder" route, in graph route order: from its L1's number, to its L2's number,
    circuit "F<l1>/<l2>", its points converted from metres to inches exactly as the homerun rows (never through
    meters_per_unit). The feeder schedule prints each row's polyline length / 12 from these points (never the
    stored length_ft) and the L2 the drawing's L1ToL2Assignments give the L1, as the plugin does.
The bridge's string geometry is rescaled from drawing units to inches for the schedule kernel too.
The route-aware bridge (solar_electrical_route_bridge.py) projects the same legs and feeders in drawing units
and refuses graphs this read accepts (BRIDGE_ROUTE_TOPOLOGY_MISMATCH on a detached homerun), so this read keeps
its own one-way projection.

Circuit source (request `circuit_source`):
  - "topology" (default): the bridge's circuit, which follows the graph's topology: the graph tag when it parses
    in the kernels' grammar and names the string's collector, else "+k/<number><letter>" from the collector.
  - "labels": the graph string's circuit_tag verbatim, the plugin's own reading of the drawing's cable records.
    A tag outside the kernels' grammar (Studio's "S<n>") is skipped, as the plugin skips it.
The summary counts `label_mismatches`: strings whose tag parses but names another number than their collector.

Host inputs (none of which the graph carries) are fixed and stated in the summary:
  - InverterCatalogRecord: the caller's `inverter_record` (13 catalog text fields), else none.
  - ModuleCatalogRecord: none. The kernel ports only the capture host's unresolved-module branch (every module
    column prints "-"); with a module it refuses cable sizing, which is not ported.
  - StringSizerStandard: the standard result of the drawing's verified global sizing evidence, else none.
    Zone sizing is not ported (the kernel reads the global response): SCHEDULES_ZONE_SIZING_UNSUPPORTED.
  - UseL2Collectors: the graph settings.use_l2_collectors value.
  - UseOptimizers false; a SolarEdge inverter refuses SCHEDULES_OPTIMIZERS_UNSUPPORTED (optimizer section not
    ported).
  - SessionCableIndexHasFeeders: true exactly when the graph holds a feeder route. The plugin lists feeders from
    its in-memory cable index; the graph's routes are Studio's persisted index, so a reopened design keeps its
    feeder schedule. The feeder schedule still needs UseL2Collectors and a non-empty L1ToL2Assignments.

Fails closed: every check runs before the kernel; a stale design refuses SOLAR_OUTPUT_NOT_CURRENT. Linear in
strings, inverters, routes and route points (one dict per lookup); feeders are bounded by the validator (one per
L1) and every table by the kernel's MAX_SCHEDULE_ROWS. Pure: no I/O, no clock, no network; inputs never mutated.
"""
import json
import re

import solar_artifacts
import solar_electrical_state_bridge as bridge
import solar_sizing_client
from solar_design_graph import GraphValidationError, _bounded_json
from solar_solve_results import require_current_export
from solar_sizing_client import units_resolved

kernel = bridge._load_sibling("solar_inverter_outputs")
st = bridge.st

TOOL = "solar-electrical-schedules"
SCHEMA = "leaf.solar-electrical-schedules.v1"
FILENAME = "ElectricalSchedules.json"
MEDIA_TYPE = "application/json"
CIRCUIT_SOURCES = ("topology", "labels")
HOMERUN_SEGMENTS = {"start homerun": "start", "end homerun": "end"}
FEEDER_KIND = "feeder"  # graph route_kind; the kernel's cable_kind has the same spelling
TEXT_FIELDS = ("companyName", "modelName", "seriesName")
NUMBER_FIELDS = ("maxDCPower", "maxDCVoltage", "minDCVoltageFeed", "mpptVoltageRangeMin",
                 "mpptVoltageRangeMax", "numMpptTrackers", "DCInputers", "maxACPower",
                 "nominalACVoltage", "maxACCurrent")
MAX_TEXT = 128
INCH_M = 0.0254  # The Branch2025 kernel measures lengths in inches.
NUMBER_TEXT = re.compile(r"(-?[0-9]{1,12}(\.[0-9]{1,6})?)?")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _request(params):
    _bounded_json(params)
    if type(params) is not dict or not set(params) <= {"circuit_source", "inverter_record"}:
        raise GraphValidationError("INVALID_SCHEDULES_REQUEST")
    source = params.get("circuit_source", "topology")
    if type(source) is not str or source not in CIRCUIT_SOURCES:
        raise GraphValidationError("INVALID_SCHEDULES_REQUEST")
    record = params.get("inverter_record")
    if record is not None or "inverter_record" in params:
        if type(record) is not dict or set(record) != set(TEXT_FIELDS + NUMBER_FIELDS):
            raise GraphValidationError("INVALID_SCHEDULES_REQUEST")
        for key in TEXT_FIELDS:
            value = record[key]
            if type(value) is not str or len(value) > MAX_TEXT or CONTROL.search(value):
                raise GraphValidationError("INVALID_SCHEDULES_REQUEST")
        for key in NUMBER_FIELDS:
            value = record[key]
            if type(value) is not str or not NUMBER_TEXT.fullmatch(value):
                raise GraphValidationError("INVALID_SCHEDULES_REQUEST")
        record = dict(record)
    return source, record


def _sizer(graph):
    """The standard result of the verified global sizing, or None when the drawing was never sized."""
    evidence = graph["settings"]["extra"].get("string_sizing")
    if evidence is None:
        return None
    solar_sizing_client.require_sizing(graph)
    if evidence["mode"] != "global":
        raise GraphValidationError("SCHEDULES_ZONE_SIZING_UNSUPPORTED")
    standard = evidence["records"][graph["settings"]["id"]]["response"]["simulation_results"]["standard"]
    return {"Conditions": standard["Conditions"], "max_module_voltage": standard["max_module_voltage"],
            "string_design_voltage": standard["string_design_voltage"]}


def label_mismatches(graph):
    """Strings whose circuit_tag parses in the kernels' grammar but names another number than their collector."""
    numbers = {inverter["id"]: inverter["number"] for inverter in graph["inverters"]}
    count = 0
    for string in graph["strings"]:
        ref = string["inverter_ref"]
        match = bridge.CIRCUIT.fullmatch(string["circuit_tag"].strip())
        if ref is not None and match and int(match.group("device")) != numbers[ref]:
            count += 1
    return count


def schedule_state(graph, source):
    """The bridge's state plus panel counts, homerun and feeder cable rows (see the module docstring)."""
    try:
        state, binding = bridge.state_from_graph(graph)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    mpu = graph["project"]["units"]["meters_per_unit"]
    scale = mpu / INCH_M
    if scale != 1:
        for geometry in state["geometry"]["strings"]:
            geometry["vertices"] = [[x * scale, y * scale] for x, y in geometry["vertices"]]
            for key in ("start", "end", "midpoint"):
                if geometry[key] is not None:
                    x, y = geometry[key]
                    geometry[key] = [x * scale, y * scale]
    strings = {string["id"]: string for string in graph["strings"]}
    handles = {bound["id"]: handle for handle, bound in binding["strings"].items()}
    numbers = {inverter["id"]: inverter["number"] for inverter in graph["inverters"]}
    circuits = {}
    for row in state["rows"]["string-assignment"]:
        string = strings[binding["strings"][row["string"]]["id"]]
        row["_detail"]["panel_count"] = str(string["module_count"])
        if source == "labels":
            row["_detail"]["circuit"] = string["circuit_tag"]
        circuits[row["string"]] = row["_detail"]["circuit"]
    cables = []
    for k, route in enumerate(graph["routes"], 1):
        if route["route_kind"] == FEEDER_KIND:
            # validate_graph proved from_ref an L1 and to_ref an L2; the bridge proved both numbered (an integral
            # float is accepted there), and the kernel reads an int source only.
            l1 = int(numbers[route["from_ref"]])
            l2 = int(numbers[route["to_ref"]])
            cables.append({"cable_kind": "feeder", "from": l1, "to": l2,
                           "vertices": [st.coordinate(p[0] / INCH_M, p[1] / INCH_M) for p in route["points"]],
                           "length": {"kind": "length", "value": float(route["length_ft"]), "unit": "ft"},
                           "_detail": {"circuit": f"F{l1}/{l2}", "gauge": route["wire_gauge"] or "NA",
                                       "closed": False},
                           "_pair": f"cable:schedules-{k}"})
            continue
        if route["route_kind"] not in HOMERUN_SEGMENTS or route["from_ref"] not in handles:
            continue
        handle = handles[route["from_ref"]]
        cables.append({"cable_kind": "dc-homerun", "from": handle, "to": numbers.get(route["to_ref"]),
                       "segment": HOMERUN_SEGMENTS[route["route_kind"]],
                       "vertices": [st.coordinate(p[0] / INCH_M, p[1] / INCH_M) for p in route["points"]],
                       "length": {"kind": "length", "value": float(route["length_ft"]), "unit": "ft"},
                       "_detail": {"circuit": circuits[handle], "gauge": route["wire_gauge"] or "NA",
                                   "closed": False},
                       "_pair": f"cable:schedules-{k}"})
    state["rows"]["cable"] = cables
    try:
        return st.validate_state(state)
    except (st.InverterStateError, ValueError):
        raise GraphValidationError("SCHEDULES_MAPPING_FAILED") from None


def render(document):
    """Compact canonical JSON, UTF-8, no trailing newline."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def run(graph, params):
    source, record = _request(params)
    if not units_resolved(graph):
        raise GraphValidationError("UNRESOLVED_UNITS")
    graph = require_current_export(graph)
    if any(inverter["is_solaredge"] for inverter in graph["inverters"]):
        raise GraphValidationError("SCHEDULES_OPTIMIZERS_UNSUPPORTED")
    sizer = _sizer(graph)
    state = schedule_state(graph, source)
    host = {"UseL2Collectors": graph["settings"]["use_l2_collectors"], "UseOptimizers": False,
            "InverterCatalogRecord": record, "ModuleCatalogRecord": None, "StringSizerStandard": sizer,
            "SessionCableIndexHasFeeders": any(row["cable_kind"] == FEEDER_KIND for row in state["rows"]["cable"])}
    try:
        records = kernel.string_records(state, host)
        tables = [{"title": table["title"], "cells": kernel.table_cells(table)}
                  for table in kernel.build_schedules(state, host)]
    except kernel.InverterOutputError:
        raise GraphValidationError("SCHEDULES_KERNEL_REFUSED") from None
    summary = {"circuit_source": source, "strings": len(records),
               "modules": sum(r["modules"] for r in records),
               "label_mismatches": label_mismatches(graph),
               "inverter_record": "caller" if record is not None else "absent",
               "sizing": "global" if sizer is not None else "absent", "module_catalog": "unresolved"}
    if not tables:
        return {"status": "no-schedules", "reason": "no-schedule-data", **summary}
    content = render({"schema": SCHEMA, "circuit_source": source, "tables": tables})
    summary = {"status": "written", "tables": [table["title"] for table in tables], **summary}
    return solar_artifacts.ArtifactOutput(summary, MEDIA_TYPE, FILENAME, content)


def input_readiness(graph):
    """Readiness hook: the checks run() makes with the default request, never raising."""
    try:
        run(graph, {})
    except GraphValidationError as error:
        return {"input_ready": False, "input_reason": error.code.lower()}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return {"input_ready": False, "input_reason": "schedules_input_unsupported"}
    return {"input_ready": True, "input_reason": None}
