"""Expose the NEC ampacity kernel as a registry graph read."""
import math

import solar_nec
from solar_design_graph import GraphValidationError

_KEYS = ("base_ampacity", "temp_factor", "conduit_factor")
_MAX = {"base_ampacity": 10000, "temp_factor": 10, "conduit_factor": 10}


def run(graph, params):
    if type(params) is not dict or set(params) != set(_KEYS):
        raise GraphValidationError("INVALID_AMPACITY_REQUEST")
    values = []
    for key in _KEYS:
        v = params[key]
        if type(v) not in (int, float):
            raise GraphValidationError("INVALID_AMPACITY_REQUEST")
        if type(v) is float and not math.isfinite(v):
            raise GraphValidationError("INVALID_AMPACITY_REQUEST")
        if not (0 <= v <= _MAX[key]) or (type(v) is float and math.copysign(1.0, v) < 0):
            raise GraphValidationError("AMPACITY_INPUT_OUT_OF_RANGE")
        values.append(float(v))
    base, temp, conduit = values
    c = solar_nec.ampacity_correction_310_15b16(base, temp, conduit)
    alt = c.rejected_alternatives[0]
    return {
        "article": c.article, "short_description": c.short_description,
        "formula": c.formula, "inputs": dict(c.inputs), "result": c.result,
        "units": c.units,
        "rejected_alternatives": [{
            "description": alt.description,
            "would_have_resulted_in": alt.would_have_resulted_in,
            "why_rejected": alt.why_rejected,
        }],
        "source_url": c.source_url, "one_liner": c.to_one_liner(),
    }
