"""Expose the NEC Chapter 9 conduit fill kernel as a registry graph read."""
import solar_nec
from solar_design_graph import GraphValidationError

GAUGES = tuple(label + " " + unit for label, unit in zip(solar_nec.WIRE_LABELS, solar_nec.WIRE_UNITS))
INSULATIONS = (solar_nec.THWN2, solar_nec.XHHW2, solar_nec.USE2)
MAX_COUNT = 1000
_REQUIRED = frozenset(("conductor_gauge", "current_carrying_count", "conduit_type", "conductor_insulation"))
_ALLOWED = _REQUIRED | {"egc_gauge"}


def run(graph, params):
    if type(params) is not dict or not _REQUIRED <= set(params) <= _ALLOWED:
        raise GraphValidationError("INVALID_CONDUIT_FILL_REQUEST")
    for key, choices in (("conductor_gauge", GAUGES), ("egc_gauge", GAUGES),
                         ("conduit_type", solar_nec.CONDUIT_TYPES),
                         ("conductor_insulation", INSULATIONS)):
        if key == "egc_gauge" and key not in params:
            continue
        if type(params[key]) is not str or params[key] not in choices:
            raise GraphValidationError("INVALID_CONDUIT_FILL_REQUEST")
    count = params["current_carrying_count"]
    if type(count) is not int:
        raise GraphValidationError("INVALID_CONDUIT_FILL_REQUEST")
    if not 0 <= count <= MAX_COUNT:
        raise GraphValidationError("CONDUIT_FILL_INPUT_OUT_OF_RANGE")
    gauge = params["conductor_gauge"]
    egc = params.get("egc_gauge")
    conduit_type = params["conduit_type"]
    insulation = params["conductor_insulation"]
    r = solar_nec.size_for_circuit(gauge, count, egc, conduit_type, insulation)
    conductors = []
    for role, label, material, n in (
            ("current-carrying", gauge, insulation, count),
            ("egc", egc, solar_nec.BARE, 1 if egc is not None else 0)):
        if n:
            g, u = label.split(" ")
            conductors.append({"role": role, "gauge": g, "unit": u,
                               "insulation": material, "count": n,
                               "area_sq_in": float(solar_nec.lookup_conductor_area(g, u, material))})
    # These 14 keys are the fixed output contract, including honest unsuccessful results.
    return {
        "success": r.success,
        "trade_size": r.trade_size,
        "conduit_type": r.conduit_type,
        "conduit_type_label": solar_nec.conduit_type_label(r.conduit_type),
        "conduit_area_sq_in": float(r.conduit_area_sq_in),
        "conductor_area_sq_in": float(r.conductor_area_sq_in),
        "fill_pct": float(r.fill_pct),
        "max_fill_pct": float(r.max_fill_pct),
        "max_fill_fraction": float(solar_nec.max_fill_fraction(r.total_conductors)),
        "total_conductors": r.total_conductors,
        "failure_reason": r.failure_reason,
        "note": r.note,
        "conductors": conductors,
        "conduit_table": [{"trade_size": t,
                           "area_sq_in": float(solar_nec.lookup_conduit_area(t, conduit_type))}
                          for t in solar_nec.TRADE_SIZES],
    }
