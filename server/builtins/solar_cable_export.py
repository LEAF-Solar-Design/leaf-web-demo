"""CableExport, Export All (StringExportCmd.cs:33-94, StringHomerunExportForm.cs:318-413) over the W1 design
graph, as a read that writes one revision-bound XLSX artifact.

The plugin writes the export form's workbook: Homeruns, Equipment Schedule, Inverter Schedule and String
Schedule, then a Feeder Schedule when L2 collectors are on and the drawing holds L1 to L2 assignments. Studio
builds the same workbook with the ported kernel (server/solar_inverter_outputs.py cable_export, read-only here)
from the kernel state the route-aware electrical bridge (server/solar_electrical_route_bridge.py
state_from_graph, read-only here) projects from the CURRENT graph, and publishes the workbook bytes as
CableExport.xlsx through server/solar_artifacts.py. Nothing is drawn and no graph entity changes.

What the bridge does not project and this read adds, one way and read-only:
  - each string row's panel count (the graph string's module_count), which the Homeruns and string sheets print;
  - with circuit_source "labels", the graph string's circuit_tag verbatim on the string row and on each of its
    dc-homerun rows (the plugin's own reading of the drawing's cable records).
Every coordinate the kernel reads (string geometry, cable vertices, device positions) is rescaled from drawing
units to inches, because the kernel measures lengths in inches: x_in = x_du * meters_per_unit / 0.0254. On an
inch drawing the factor is exactly 1 and nothing is touched.

Host inputs (none of which the graph carries) come from the request or are fixed, and are stated in the summary:
  - InverterCatalogRecord: the caller's `inverter_record` (13 catalog text fields), else none.
  - SuggestedInverterCount: the caller's `suggested_inverter_count` (1 to 10000), else the kernel's 1.
  - DesignMinTempC: the caller's `design_min_temp_c` (-90 to 60), else the kernel's -40.
  - ProjectLocation: the caller's `project_location` (1 to 128 characters, no control characters and no
    U+FFFE or U+FFFF), else none.
  - ModuleCatalogRecord: none (the kernel refuses a module: cable sizing is not ported).
  - StringSizerStandard: the standard result of the drawing's verified global sizing evidence, else none.
  - UseL2Collectors: the graph settings.use_l2_collectors value.
  - UseOptimizers false; a SolarEdge inverter refuses SCHEDULES_OPTIMIZERS_UNSUPPORTED.
  - SessionCableIndexHasFeeders true: the graph is Studio's cable index, so its feeder routes are listed.

Fails closed: every check runs before the kernel; a stale design refuses SOLAR_OUTPUT_NOT_CURRENT. Linear in
strings, devices, routes and vertices (one dict per lookup). Pure: no I/O, no clock, no network; inputs never
mutated.
"""
import math
import re

import solar_artifacts
import solar_electrical_route_bridge as bridge
import solar_sizing_client
from solar_design_graph import GraphValidationError, _bounded_json
from solar_solve_results import require_current_export
from solar_sizing_client import units_resolved

kernel = bridge.legacy._load_sibling("solar_inverter_outputs")
st = bridge.st

TOOL = "solar-cable-export"
FILENAME = "CableExport.xlsx"
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FORM = {"branch_string_export": "Export All"}
CIRCUIT_SOURCES = ("topology", "labels")
TEXT_FIELDS = ("companyName", "modelName", "seriesName")
NUMBER_FIELDS = ("maxDCPower", "maxDCVoltage", "minDCVoltageFeed", "mpptVoltageRangeMin",
                 "mpptVoltageRangeMax", "numMpptTrackers", "DCInputers", "maxACPower",
                 "nominalACVoltage", "maxACCurrent")
REQUEST_KEYS = frozenset(("circuit_source", "inverter_record", "suggested_inverter_count", "design_min_temp_c",
                          "project_location"))
MAX_TEXT = 128
MAX_LOCATION = 128
MAX_INVERTER_COUNT = 10_000
MIN_TEMP_C, MAX_TEMP_C = -90, 60
INCH_M = 0.0254  # The Branch2025 kernel measures lengths in inches.
NUMBER_TEXT = re.compile(r"(-?[0-9]{1,12}(\.[0-9]{1,6})?)?")
CONTROL = re.compile(r"[\x00-\x1f\x7f￾￿]")
# Readiness: every code run() can raise, mapped to a reason the Solar rail already has a sentence for.
NOT_READY = {
    "UNRESOLVED_UNITS": {"input_ready": False, "input_reason": "unresolved_units"},
    "SOLAR_OUTPUT_NOT_CURRENT": {"input_ready": False, "input_reason": "solar_output_not_current"},
    "SIZING_CONFIRMATION_REQUIRED": {"input_ready": False, "input_reason": "sizing_confirmation_required"},
    "SCHEDULES_OPTIMIZERS_UNSUPPORTED": {"input_ready": False, "input_reason": "schedules_optimizers_unsupported"},
    "SCHEDULES_ZONE_SIZING_UNSUPPORTED": {"input_ready": False, "input_reason": "schedules_zone_sizing_unsupported"},
    "SCHEDULES_MAPPING_FAILED": {"input_ready": False, "input_reason": "schedules_mapping_failed"},
    "SCHEDULES_KERNEL_REFUSED": {"input_ready": False, "input_reason": "schedules_kernel_refused"},
}
UNSUPPORTED = {"input_ready": False, "input_reason": "schedules_input_unsupported"}


def _refuse():
    raise GraphValidationError("INVALID_CABLE_EXPORT_REQUEST")


def _request(params):
    """(circuit source, inverter record or None, inverter count or None, design low, location or None)."""
    _bounded_json(params)
    if type(params) is not dict or not set(params) <= REQUEST_KEYS:
        _refuse()
    source = params.get("circuit_source", "topology")
    if type(source) is not str or source not in CIRCUIT_SOURCES:
        _refuse()
    record = None
    if "inverter_record" in params:
        record = params["inverter_record"]
        if type(record) is not dict or set(record) != set(TEXT_FIELDS + NUMBER_FIELDS):
            _refuse()
        for key in TEXT_FIELDS:
            value = record[key]
            if type(value) is not str or len(value) > MAX_TEXT or CONTROL.search(value):
                _refuse()
        for key in NUMBER_FIELDS:
            value = record[key]
            if type(value) is not str or not NUMBER_TEXT.fullmatch(value):
                _refuse()
        record = dict(record)
    count = None
    if "suggested_inverter_count" in params:
        count = params["suggested_inverter_count"]
        if type(count) is float and math.isfinite(count) and count.is_integer():
            count = int(count)                       # JSON Schema's integer admits 5.0
        if type(count) is not int or not 1 <= count <= MAX_INVERTER_COUNT:
            _refuse()
    low = kernel.DEFAULT_DESIGN_MIN_TEMP_C
    if "design_min_temp_c" in params:
        low = params["design_min_temp_c"]
        if type(low) not in (int, float) or not math.isfinite(low) or not MIN_TEMP_C <= low <= MAX_TEMP_C:
            _refuse()
    location = None
    if "project_location" in params:
        location = params["project_location"]
        if type(location) is not str or not 1 <= len(location) <= MAX_LOCATION or CONTROL.search(location):
            _refuse()
    return source, record, count, float(low), location


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


def _inches(point, scale):
    return [point[0] * scale, point[1] * scale]


def export_state(graph, source):
    """The route bridge's state plus panel counts and label circuits, in inches (see the module docstring)."""
    try:
        state, binding = bridge.state_from_graph(graph)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    scale = graph["project"]["units"]["meters_per_unit"] / INCH_M
    if scale != 1:
        for geometry in state["geometry"]["strings"]:
            geometry["vertices"] = [_inches(vertex, scale) for vertex in geometry["vertices"]]
            for key in ("start", "end", "midpoint"):
                if geometry[key] is not None:
                    geometry[key] = _inches(geometry[key], scale)
        for row in state["rows"]["cable"]:
            row["vertices"] = [st.coordinate(*_inches(st.point_of(vertex), scale)) for vertex in row["vertices"]]
        for row in state["rows"]["device"]:
            row["position"] = st.coordinate(*_inches(st.point_of(row["position"]), scale))
    strings = {string["id"]: string for string in graph["strings"]}
    circuits = {}
    for row in state["rows"]["string-assignment"]:
        string = strings[binding["strings"][row["string"]]["id"]]
        row["_detail"]["panel_count"] = str(string["module_count"])
        if source == "labels":
            row["_detail"]["circuit"] = string["circuit_tag"]
        circuits[row["string"]] = row["_detail"]["circuit"]
    for row in state["rows"]["cable"]:
        if row["cable_kind"] == "dc-homerun":
            row["_detail"]["circuit"] = circuits[row["from"]]
    try:
        return st.validate_state(state)
    except (st.InverterStateError, ValueError):
        raise GraphValidationError("SCHEDULES_MAPPING_FAILED") from None


def run(graph, params):
    source, record, count, low, location = _request(params)
    if not units_resolved(graph):
        raise GraphValidationError("UNRESOLVED_UNITS")
    graph = require_current_export(graph)
    if any(inverter["is_solaredge"] for inverter in graph["inverters"]):
        raise GraphValidationError("SCHEDULES_OPTIMIZERS_UNSUPPORTED")
    sizer = _sizer(graph)
    state = export_state(graph, source)
    host = {"UseL2Collectors": graph["settings"]["use_l2_collectors"], "UseOptimizers": False,
            "InverterCatalogRecord": record, "ModuleCatalogRecord": None, "StringSizerStandard": sizer,
            "SessionCableIndexHasFeeders": True, "SuggestedInverterCount": count,
            "DesignMinTempC": low, "ProjectLocation": location}
    try:
        _, _, workbook = kernel.cable_export(state, host, FORM)
        records = kernel.string_records(state, host)
    except kernel.InverterOutputError:
        raise GraphValidationError("SCHEDULES_KERNEL_REFUSED") from None
    summary = {"circuit_source": source, "strings": len(records), "modules": sum(r["modules"] for r in records),
               "feeders": sum(1 for row in state["rows"]["cable"] if row["cable_kind"] == "feeder"),
               "inverter_record": "caller" if record is not None else "absent",
               "sizing": "global" if sizer is not None else "absent", "module_catalog": "unresolved"}
    if workbook is None:
        return {"status": "no-export", "reason": "no-homeruns", **summary}
    summary = {"status": "written", "sheets": [name for name, _ in workbook["sheets"]],
               "rows": [len(grid) for _, grid in workbook["sheets"]], **summary}
    return solar_artifacts.ArtifactOutput(summary, MEDIA_TYPE, FILENAME, workbook["bytes"])


def input_readiness(graph):
    """Readiness hook: the checks run() makes with the default request, never raising."""
    try:
        run(graph, {})
    except GraphValidationError as error:
        return dict(NOT_READY.get(error.code, UNSUPPORTED))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return dict(UNSUPPORTED)
    return {"input_ready": True, "input_reason": None}
