"""Evaluate the plugin's design guardrails on the stored W1 graph, as a registry graph read.

Projects the persisted graph into the guardrails-intake-v1 document server/solar_guardrails.py
validates (drawing list mode, recorded device numbers) and returns the verdict and report rows in
the plugin adapter's batch2-v1 row shape. Pure and bounded: O(panels + strings + inverters), no I/O,
no allocation beyond the output. Fails closed with a named GraphValidationError on a malformed
request or a graph the kernel cannot evaluate truthfully.
"""
import math

import solar_guardrails
import solar_sizing_client
from solar_design_graph import GraphValidationError

SCHEMA = "leaf.solar-guardrails.v1"
LIST_MODE = "drawing"
WINDOW_KEY = "mppt_voltage_window"
STRING_VOLTAGE_KEY = "per_string_voltage"
WINDOW_FIELDS = frozenset(("min_v", "max_v"))
WINDOW_MAX_V = 100000
INVERTER_CONFIG = ("model", "mppt_count", "total_dc_inputs", "max_dc_voltage", "max_ac_power_kw", "is_solaredge")


def _window(params):
    if (type(params) is not dict or set(params) - {WINDOW_KEY, STRING_VOLTAGE_KEY}
            or type(params.get(STRING_VOLTAGE_KEY, False)) is not bool):
        raise GraphValidationError("INVALID_GUARDRAILS_REQUEST")
    if WINDOW_KEY not in params:
        return None
    window = params[WINDOW_KEY]
    if type(window) is not dict or set(window) != WINDOW_FIELDS:
        raise GraphValidationError("INVALID_GUARDRAILS_REQUEST")
    for key in ("min_v", "max_v"):
        value = window[key]
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            raise GraphValidationError("INVALID_GUARDRAILS_REQUEST")
    for key in ("min_v", "max_v"):
        if not 0 <= window[key] <= WINDOW_MAX_V:
            raise GraphValidationError("GUARDRAILS_WINDOW_OUT_OF_RANGE")
    return {"min_v": float(window["min_v"]), "max_v": float(window["max_v"])}


def _sized_module(graph):
    """Module electricals and string length from the drawing's re-verified sizing evidence."""
    try:
        evidence = solar_sizing_client.require_sizing(graph)
        targets = solar_sizing_client.sizing_targets(graph, evidence["mode"])
        values = set()
        for target_id in sorted(evidence["records"]):
            response = solar_sizing_client.SizingResponse.model_validate(
                evidence["records"][target_id]["response"])
            values.add((float(response.voc), float(response.bvoc), float(response.pmp),
                        float(response.vmp), float(response.imp), targets[target_id]["panels_in_sequence"]))
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("GUARDRAILS_SIZING_REQUIRED") from None
    if len(values) != 1:
        raise GraphValidationError("GUARDRAILS_SIZING_AMBIGUOUS")
    (voc, bvoc, pmp, vmp, imp, panels_in_sequence), = values
    module = {"Voc": voc, "BVoc": bvoc, "Pmp": pmp, "Vmp": vmp, "Imp": imp,
              "NumPanelsInSequence": panels_in_sequence}
    return module, evidence["mode"]


def graph_intake(graph, window):
    """The guardrails-intake-v1 document for a validated graph; (intake, sizing mode)."""
    module, mode = _sized_module(graph)
    project, settings = graph["project"], graph["settings"]
    latitude, longitude = project["latitude"], project["longitude"]
    if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
        raise GraphValidationError("GUARDRAILS_PROJECT_COORDINATES_REQUIRED")
    configs = {tuple(inverter[key] for key in INVERTER_CONFIG) for inverter in graph["inverters"]}
    if len(configs) > 1:
        raise GraphValidationError("GUARDRAILS_MIXED_INVERTERS")
    catalog, model = None, ""
    if configs:
        (model, mppt_count, total_dc_inputs, max_dc_voltage, max_ac_power_kw, is_solaredge), = configs
        catalog = {"model_name": model, "num_mppt_trackers": mppt_count, "total_dc_inputs": total_dc_inputs,
                   "max_dc_voltage": float(max_dc_voltage),
                   "mppt_voltage_range_min": window["min_v"] if window else 0.0,
                   "mppt_voltage_range_max": window["max_v"] if window else 0.0,
                   "max_ac_power_kw": float(max_ac_power_kw), "is_solar_edge": is_solaredge}
    numbers = {inverter["id"]: int(inverter["number"]) for inverter in graph["inverters"]}
    host = dict(module, NumMppt=settings["num_mppt"], StringsPerMppt=settings["strings_per_mppt"],
                OptimizerModel="", UseCombinerBox=False, InverterSelection=model, SuggestedInverterCount=0)
    intake = {
        "format": "guardrails-intake-v1", "host_settings": host, "host_settings_recorded": True,
        "drawing": {"inverter_types": {}, "project_latitude": float(latitude),
                    "project_longitude": float(longitude),
                    "project_zip_code_set": bool(project["zip_code"].strip())},
        "inverter_catalog": catalog, "device_numbers_recorded": True,
        "devices": [{"number": int(inverter["number"]), "is_l2": False, "type_key": "A"}
                    for inverter in graph["inverters"]],
        "strings": [{"circuit": string["circuit_tag"], "inverter": numbers.get(string["inverter_ref"]),
                     "panel_count": string["module_count"]} for string in graph["strings"]],
    }
    return intake, mode


def rows_for_intake(intake):
    """The kernel's rows in the plugin adapter's batch2-v1 shape: kinds in name order, ids in build order."""
    by_kind = solar_guardrails.guardrail_rows(intake, LIST_MODE)
    rows = []
    for kind in sorted(by_kind):
        for row_id, fields in by_kind[kind]:
            row = {"id": {"entity_id": row_id}, "type": kind, "quantity": 1, "unit": "each"}
            row.update(fields)
            rows.append(row)
    return rows


def _cold_module_voltage(graph):
    """Return module voltage, design temperature, and any evidence gap."""
    try:
        evidence = solar_sizing_client.require_sizing(graph)
        if evidence["mode"] != "global" or len(evidence["records"]) != 1:
            return None, None, "global_module_voltage_required"
        record = next(iter(evidence["records"].values()))
        response = solar_sizing_client.SizingResponse.model_validate(record["response"])
        voltage = solar_sizing_client.compute_voc_cold(response.voc, response.bvoc,
                                                       response.min_temp)
    except GraphValidationError as error:
        return None, None, error.code.lower()
    except ValueError:
        return None, None, "invalid_module_voltage"
    if not math.isfinite(voltage) or voltage <= 0:
        return None, None, "invalid_module_voltage"
    return voltage, response.min_temp, None


def string_voltage_report(graph, window):
    module_voc, min_temp, sizing_reason = _cold_module_voltage(graph)

    devices = {device["id"]: device for device in graph["inverters"]}
    settings = graph["settings"]
    rows = []
    for string in graph["strings"]:
        device = devices.get(string["inverter_ref"])
        reason = sizing_reason
        if device is None:
            reason = "inverter_binding_required"
        elif (string["validity"]["state"] != "valid"
                or device["validity"]["state"] != "valid"):
            reason = "valid_circuit_and_inverter_required"
        elif (device["is_solaredge"] or device["is_l2"] or settings["use_l2_collectors"]
              or settings["optimizer_ratio"] != 1 or string["circuit_kind"] != "String"):
            reason = "conventional_direct_string_required"
        elif device["max_dc_voltage"] <= 0:
            reason = "inverter_dc_voltage_limit_required"

        cold = {"status": "unverified", "reason": reason}
        if reason is None:
            voltage = module_voc * string["module_count"]
            limit = device["max_dc_voltage"]
            cold = {"status": "pass" if voltage <= limit else "fail",
                    "voc_v": voltage, "saved_inverter_max_dc_v": limit}
        rows.append({
            "string_ref": string["id"],
            "inverter_ref": string["inverter_ref"],
            "module_count": string["module_count"],
            "cold_dc_limit": cold,
            "hot_mppt_window": {"status": "unverified",
                                "reason": "vmp_temperature_model_required"},
        })
    return {
        "schema": "leaf.solar-string-voltage.v1",
        "status": "incomplete",
        "design_min_temp_c": min_temp,
        "caller_mppt_window": window,
        "rows": rows,
    }


def run(graph, params):
    window = _window(params)
    if params.get(STRING_VOLTAGE_KEY):
        return string_voltage_report(graph, window)
    intake, mode = graph_intake(graph, window)
    try:
        snapshot = solar_guardrails.build_snapshot(intake, LIST_MODE)
        rows = rows_for_intake(intake)
    except solar_guardrails.GuardrailError:
        raise GraphValidationError("GUARDRAILS_INPUT_UNSUPPORTED") from None
    status = next(row["value"] for row in rows if row["type"] == "report" and row["name"] == "status")
    # These six top-level keys and six input keys are the fixed output contract.
    return {"schema": SCHEMA, "list_mode": LIST_MODE, "status": status, "rows": rows,
            "inputs": {"module_source": "sizing-" + mode, "mppt_window": "caller" if window else "absent",
                       "design_min_temp_c": float(snapshot["min_temp_c"]),
                       "inverter_count": len(graph["inverters"]), "string_count": len(graph["strings"]),
                       "linked_string_count": len(snapshot["strings"])}}


def input_readiness(graph):
    """Readiness hook: the same checks as run(), never raising."""
    try:
        run(graph, {})
    except GraphValidationError as error:
        return {"input_ready": False, "input_reason": error.code.lower()}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return {"input_ready": False, "input_reason": "guardrails_input_unsupported"}
    return {"input_ready": True, "input_reason": None}
