"""Voltage evidence shared by saved solar design reads."""
import math

import solar_sizing_client
from solar_design_graph import GraphValidationError


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
    """Evaluate voltage evidence for each string in a validated saved graph."""
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
