"""Expose the NEC feeder OCPD sizing kernel as a registry graph read."""
import math

import solar_nec
from solar_design_graph import GraphValidationError

_KEYS = ("continuous_current_a", "is_continuous", "egc_material")
_MATERIALS = (solar_nec.COPPER, solar_nec.ALUMINUM)
_MAX_CURRENT_A = 10000


def run(graph, params):
    # Reads nothing from graph. Fails closed: shape, then every type, then the range, before the kernel runs.
    if type(params) is not dict or set(params) != set(_KEYS):
        raise GraphValidationError("INVALID_FEEDER_OCPD_REQUEST")
    current = params["continuous_current_a"]
    if type(current) not in (int, float) or (type(current) is float and not math.isfinite(current)):
        raise GraphValidationError("INVALID_FEEDER_OCPD_REQUEST")
    if type(params["is_continuous"]) is not bool:
        raise GraphValidationError("INVALID_FEEDER_OCPD_REQUEST")
    # The kernel sizes any string other than exactly "Copper" as aluminum, so the enum is closed here.
    if type(params["egc_material"]) is not str or params["egc_material"] not in _MATERIALS:
        raise GraphValidationError("INVALID_FEEDER_OCPD_REQUEST")
    if not (0 <= current <= _MAX_CURRENT_A) or (
            type(current) is float and current == 0 and math.copysign(1.0, current) < 0):
        raise GraphValidationError("FEEDER_OCPD_INPUT_OUT_OF_RANGE")
    # float() so an int input and its float spelling give identical canonical bytes.
    result = solar_nec.size_feeder_ocpd(float(current), params["is_continuous"], params["egc_material"])
    return {
        "continuous_current_a": result.continuous_current_a,
        "is_continuous": params["is_continuous"],
        "egc_material": params["egc_material"],
        "min_ocpd_a": result.min_ocpd_a,
        "ocpd_rating_a": result.ocpd_rating_a,
        "egc_gauge": result.egc_gauge,
        "note": result.note,
    }
