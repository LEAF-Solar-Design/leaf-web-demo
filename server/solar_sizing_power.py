"""Module power proven by the current confirmed sizing response."""
import math

import solar_sizing_client as cloud
from solar_design_graph import GraphValidationError


MODULE_POWER_MAX = 1_000_000


def record_module_power(record):
    """Return rated power without rounding or accepting legacy evidence."""
    try:
        if record["adapter_version"] != cloud.ADAPTER_VERSION:
            raise ValueError()
        response = cloud.SizingResponse.model_validate(record["response"])
        power = response.pmp
        if (type(record["response"]["pmp"]) not in (int, float)
                or type(power) not in (int, float) or not math.isfinite(power)
                or not 0 < power <= MODULE_POWER_MAX):
            raise ValueError()
        return power
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise GraphValidationError("MODULE_POWER_REQUIRED") from None


def frame_module_power(graph, panel_refs, zone_ref):
    """Resolve the one sizing record covering this frame."""
    try:
        evidence = graph["settings"]["extra"]["string_sizing"]
        if evidence["mode"] == "global":
            target = graph["settings"]["id"]
        elif evidence["mode"] == "zones":
            zone = next((zone for zone in graph["electrical_zones"]
                         if zone["id"] == zone_ref), None)
            if zone is None or not set(panel_refs) <= set(zone["panel_refs"]):
                raise GraphValidationError("INVALID_ZONE_COVERAGE")
            target = zone_ref
        else:
            raise GraphValidationError("MODULE_POWER_REQUIRED")
        return record_module_power(evidence["records"][target])
    except (KeyError, TypeError, AttributeError):
        raise GraphValidationError("MODULE_POWER_REQUIRED") from None


def sizing_power_ready(graph):
    """Whether every available sizing record proves usable module power."""
    try:
        records = graph["settings"]["extra"]["string_sizing"]["records"]
        return bool(records) and all(record_module_power(record) for record in records.values())
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False
