"""Electrical zones: the Studio counterpart of LEAFADDZONE and LEAFZONEASSIGNPANELS.

Measured plugin semantics (Branch2025 Commands.cs:3306-3322, the drawing-wide list
DrawingProperties.Default.ElectricalZones):

* a zone is ``{Name, ColorIndex, PanelHandles, BoundaryHandle}`` plus its equipment
  and sizing fields, held once per drawing; MEMBERSHIP LIVES ON THE ZONE, never on
  the panel and never on the group;
* assigning a panel REMOVES it from every other zone first (Commands.cs:3306-3319),
  so the zones always partition the panels they name;
* LEAFADDZONE writes a name and a colour index and nothing else: every other zone
  field stays at its schema-neutral value until sizing or equipment fills it in.

NOT REPRODUCED, and named rather than faked: LEAFZONEASSIGNPANELS also recolours
each assigned entity to the zone's colour (Commands.cs:3322). The v1 graph carries
no per-panel colour, so Studio writes nothing for it; the producer names
``zones/panel-colour/not-in-graph`` in its fallback fields.

Both entry points validate before they touch the graph, bound every input, and
fail closed on anything malformed. Geometry is never read and never written.
"""
from datetime import datetime, timezone

from solar_design_graph import GraphValidationError, _bounded_json, new_id
from solar_sizing_client import advance, checked_graph

TOOL = "solar-electrical-zones"
MAX_NAME = 255
# Schema bound for electrical_zone.color_index (contract/solar-design-graph.v1.schema.json).
MAX_COLOR_INDEX = 256
# One drawing's zones; the plugin's palette list is small and this refuses a runaway caller.
MAX_ZONES = 256
# Schema bound for a refs array.
MAX_REFS = 100000


def _zone_name(name):
    """The plugin trims the prompted name and refuses an empty one."""
    if type(name) is not str or not name.strip() or len(name) > MAX_NAME:
        raise GraphValidationError("INVALID_ZONE_REQUEST")
    return name


def _find(graph, name):
    # AutoCAD zone names are compared without case, as the palette's own lookup does.
    folded = name.casefold()
    return next((zone for zone in graph["electrical_zones"] if zone["name"].casefold() == folded), None)


def add_zone(graph, params):
    """Create one named zone at its schema-neutral values; a duplicate name is refused."""
    _bounded_json(params)
    if type(params) is not dict or set(params) != {"expected_rev", "name", "color_index"}:
        raise GraphValidationError("INVALID_ZONE_REQUEST")
    name = _zone_name(params["name"])
    color_index = params["color_index"]
    if type(color_index) is not int or not 0 <= color_index <= MAX_COLOR_INDEX:
        raise GraphValidationError("INVALID_ZONE_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    if len(result["electrical_zones"]) >= MAX_ZONES:
        raise GraphValidationError("ZONE_LIMIT_EXCEEDED")
    if _find(result, name) is not None:
        raise GraphValidationError("DUPLICATE_ZONE_NAME")
    zone = {
        "id": new_id("zone-el"), "kind": "zone-el", "rev": result["rev"], "extra": {},
        "validity": {"state": "valid", "reasons": []},
        "provenance": {"created_by": TOOL, "created_at": datetime.now(timezone.utc).isoformat(),
                       "last_writer": TOOL, "source_rev": result["rev"],
                       "source_hash": result["source_hash"]},
        # LEAFADDZONE sets exactly these two; the rest stay unset until sizing writes them.
        "name": name, "color_index": color_index, "panel_refs": [],
        "module_model": "", "inverter_model_a": "", "inverter_count_a": 0,
        "panels_in_sequence": 0, "dc_ac_ratio": 0,
        "voc_cold": {"passes": None, "override_accepted": False, "suggested_string_length": None,
                     "per_module": None, "string_voltage": None, "max_dc_voltage": None},
        "boundary_ref": None,
    }
    result["electrical_zones"].append(zone)
    return {"graph": advance(result, [zone], TOOL), "zone_id": zone["id"], "name": name}


def assign_panels(graph, params):
    """Assign panels to a named zone, removing each from every other zone first.

    Zones partition: a panel named here leaves the zone it was in, exactly as
    Commands.cs:3306-3319 does. Membership order is the caller's selection order,
    appended after the zone's existing members.
    """
    _bounded_json(params)
    if (type(params) is not dict or set(params) != {"expected_rev", "name", "panel_refs"}
            or type(params["panel_refs"]) is not list
            or not 1 <= len(params["panel_refs"]) <= MAX_REFS
            or any(type(ref) is not str for ref in params["panel_refs"])):
        raise GraphValidationError("INVALID_ZONE_REQUEST")
    name = _zone_name(params["name"])
    refs = params["panel_refs"]
    requested = set(refs)
    if len(requested) != len(refs):
        raise GraphValidationError("DUPLICATE_PANEL_MEMBERSHIP")
    result = checked_graph(graph, params["expected_rev"])
    zone = _find(result, name)
    if zone is None:
        raise GraphValidationError("MISSING_ELECTRICAL_ZONE")
    if not requested <= {panel["id"] for panel in result["panels"]}:
        raise GraphValidationError("MISSING_PANEL")
    changed, moved = [zone], []
    for other in result["electrical_zones"]:
        if other is zone:
            continue
        remaining = [ref for ref in other["panel_refs"] if ref not in requested]
        if len(remaining) != len(other["panel_refs"]):
            moved.extend(ref for ref in other["panel_refs"] if ref in requested)
            other["panel_refs"] = remaining
            changed.append(other)
    held = set(zone["panel_refs"])
    zone["panel_refs"] = zone["panel_refs"] + [ref for ref in refs if ref not in held]
    return {"graph": advance(result, changed, TOOL), "zone_id": zone["id"],
            "name": name, "panel_count": len(zone["panel_refs"]), "moved": moved}


OPERATIONS = {"add": add_zone, "assign-panels": assign_panels}


def run(intake, params):
    _bounded_json(params)
    if type(params) is not dict or params.get("operation") not in OPERATIONS:
        raise GraphValidationError("INVALID_ZONE_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](intake, request)["graph"]
