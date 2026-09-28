"""Select a named electrical zone's panels from a persisted solar graph."""
import solar_rooftop_chain
from solar_design_graph import GraphValidationError


def run(graph, params):
    if type(params) is not dict or type(params.get("zone_name")) is not str:
        raise GraphValidationError("INVALID_ZONE_SELECTION")
    zone_name = params["zone_name"]
    zones = [{"name": zone.get("name"), "panels": list(zone["panel_refs"])}
             for zone in graph["electrical_zones"]]
    if not zones:
        return {"status": "no-zones", "panel_refs": []}
    if solar_rooftop_chain._is_blank(zone_name):
        return {"status": "cancelled", "panel_refs": []}
    try:
        listed, missing = solar_rooftop_chain.by_zone_name_select(
            zones, solar_rooftop_chain._net_trim(zone_name))
    except solar_rooftop_chain.RooftopInputError:
        raise GraphValidationError("ZONE_DATA_UNSUPPORTED") from None
    if missing:
        return {"status": "missing", "panel_refs": []}
    if not listed:
        return {"status": "empty", "panel_refs": []}
    return {"status": "selected", "panel_refs": listed}
