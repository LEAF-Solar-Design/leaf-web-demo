"""Expose the NEC AC voltage-drop kernel as a registry graph read."""
import math

import solar_nec
from solar_design_graph import GraphValidationError

_KEYS = ("current_a", "one_way_length_ft", "r_ohm_per_1000ft", "x_ohm_per_1000ft",
         "power_factor", "phase", "source_voltage")
_RANGE = {"current_a": (0, 10000), "one_way_length_ft": (0, 100000),
          "r_ohm_per_1000ft": (0, 100), "x_ohm_per_1000ft": (0, 100),
          "power_factor": (-10, 10), "phase": (1, 3), "source_voltage": (0, 100000)}


def run(graph, params):
    # Fails closed: shape, then every type, then every range, before the kernel runs.
    if type(params) is not dict or set(params) != set(_KEYS):
        raise GraphValidationError("INVALID_VOLTAGE_DROP_REQUEST")
    for key in _KEYS:
        v = params[key]
        if type(v) not in (int, float):
            raise GraphValidationError("INVALID_VOLTAGE_DROP_REQUEST")
        if type(v) is float and not math.isfinite(v):
            raise GraphValidationError("INVALID_VOLTAGE_DROP_REQUEST")
    # phase stays an int: the kernel renders '{0}-phase' and 1.0 would read '1.0-phase'.
    if type(params["phase"]) is not int or params["phase"] not in (1, 3):
        raise GraphValidationError("INVALID_VOLTAGE_DROP_REQUEST")
    for key in _KEYS:
        v = params[key]
        low, high = _RANGE[key]
        if not (low <= v <= high) or (type(v) is float and v == 0 and math.copysign(1.0, v) < 0):
            raise GraphValidationError("VOLTAGE_DROP_INPUT_OUT_OF_RANGE")
    # A divisor under 1 V overflows the percentage to inf; 0 V is the kernel's own 0.0 path.
    if 0 < params["source_voltage"] < 1:
        raise GraphValidationError("VOLTAGE_DROP_INPUT_OUT_OF_RANGE")
    c = solar_nec.voltage_drop_ac(
        float(params["current_a"]), float(params["one_way_length_ft"]),
        float(params["r_ohm_per_1000ft"]), float(params["x_ohm_per_1000ft"]),
        float(params["power_factor"]), int(params["phase"]), float(params["source_voltage"]))
    return {
        "article": c.article, "short_description": c.short_description,
        "formula": c.formula, "inputs": dict(c.inputs), "result": c.result,
        "units": c.units, "rejected_alternatives": [],
        "source_url": c.source_url, "one_liner": c.to_one_liner(),
    }
