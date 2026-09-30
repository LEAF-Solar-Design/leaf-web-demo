"""Elevation (height) zones on the W1 design graph: Studio's ZONEHEIGHT, LEAFZONEASSIGNPANELHEIGHT
and LEAFZONEASSIGNSTRINGS.

Plugin semantics, ported literally in server/solar_batch2_simple.py (contract G36, receipts
zone-height-settings/batch2-z1, elevation-zone-assign-panels/batch2-z2,
elevation-zone-assign-strings/batch2-z3):

* the drawing holds ONE ordered list of elevation zones (DrawingProperties.Default.ElevationZones),
  each {name, offset, colour, strings, panels}; Studio keeps it in the kernel's own shape at
  graph["extra"]["elevation_zones"], with graph application ids where the plugin keeps handles.
  The v1 graph schema has no typed field for it and top-level extra is {"type": "object"}, so no
  schema change is needed. NOT settings.extra: settings are inside
  solar_solve_results.upstream_basis, so a zone edit there would stale every solved panel group;
  top-level extra is outside every design digest (upstream_basis, sizing_basis), as
  extra.equipment already is;
* ZONEHEIGHT appends `add` zones named "Zone {count + 1}" at offset 0, colours from the form's
  cycle restarted at every open, then sets the LAST zone's offset to the two-decimal value;
* assigning moves every matching panel (or every string) into the named zone, removing it from
  every other zone first; panels match the reference panel's size signature (here: its panel
  group's module width and height; a panel with no group has no signature, and a reference
  with no signature matches every panel).

NOT REPRODUCED, and named rather than faked: the plugin also recolours each assigned panel to the
zone colour. The v1 graph carries no per-panel colour, so Studio writes nothing for it: a panel's
zone colour follows from membership. The same choice solar-electrical-zones made.

Contract: fails closed (every check runs before the private copy is written), bounded by the
kernel's own limits, one pass per structure plus the kernel's membership moves, and it never
touches an entity: only graph["extra"]["elevation_zones"], rev and parent_rev change.
"""
import re

import solar_batch2_simple as kernel
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

TOOL = "solar-elevation-zones"
STORE_KEY = "elevation_zones"
MAX_NAME = 255
MAX_REF = 128
# The kernel's drawing units; the graph also allows km and yd, which ZONEHEIGHT cannot store.
KERNEL_UNITS = ("in", "ft", "mm", "cm", "m")
# ByLayer. Never a zone colour (the cycle is 1, 3, 5, 4, 6, 2, 30, 210, 140, 200), so an unzoned
# panel always counts as recoloured when it joins a zone, as the plugin's colour-7 panels did.
UNZONED_COLOUR = 256
_IDS = {key: re.compile(r"leaf:" + kind + r":[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
        for key, kind in (("panels", "panel"), ("strings", "string"))}


def _refuse(code):
    raise GraphValidationError(code)


def _is_number(value):
    return type(value) in (int, float)


def _context(graph):
    """(units) for a Roof drawing whose units the kernel can store, else a named refusal."""
    if graph["project"]["installation_design"] != "Roof":
        _refuse("ROOFTOP_REQUIRED")
    units = graph["project"]["units"]["drawing_units"]
    if units not in KERNEL_UNITS:
        _refuse("UNSUPPORTED_DRAWING_UNITS")
    return units


def stored_zones(graph, units):
    """The stored list, validated by the kernel and bounded here; never trusted as written."""
    zones = graph["extra"].get(STORE_KEY, [])
    if type(zones) is not list or len(zones) > kernel.MAX_ZONES:
        _refuse("INVALID_STORED_ELEVATION_ZONES")
    result = []
    for zone in zones:
        offset = zone.get("offset") if type(zone) is dict else None
        unit = offset.get("unit") if type(offset) is dict else None
        if unit in KERNEL_UNITS and unit != units:
            # Stored under other drawing units; converting is the unit-sync owner's decision.
            _refuse("ELEVATION_ZONE_UNITS_CHANGED")
        try:
            checked = kernel._zone(zone, units)
        except kernel.BatchTwoError:
            raise GraphValidationError("INVALID_STORED_ELEVATION_ZONES") from None
        if not 1 <= len(checked["name"]) <= MAX_NAME or any(
                _IDS[key].fullmatch(ref) is None for key in ("panels", "strings") for ref in checked[key]):
            _refuse("INVALID_STORED_ELEVATION_ZONES")
        result.append(checked)
    return result


def input_readiness(graph):
    if graph["project"]["installation_design"] != "Roof":
        return {"input_ready": False, "input_reason": "rooftop_required"}
    if graph["project"]["units"]["drawing_units"] not in KERNEL_UNITS:
        return {"input_ready": False, "input_reason": "drawing_units_unsupported"}
    return {"input_ready": True, "input_reason": None}


def _commit(result, zones):
    if zones == result["extra"].get(STORE_KEY, []):
        # The plugin saves nothing when nothing moved; Studio publishes no empty version.
        _refuse("ELEVATION_ZONES_UNCHANGED")
    result["extra"][STORE_KEY] = zones
    # No entity changed, so none is stamped: rev and parent_rev advance, nothing else.
    return advance(result, [], TOOL)


def _request(params, keys):
    _bounded_json(params)
    if type(params) is not dict or set(params) != keys:
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")
    if "zone_name" in keys and (type(params["zone_name"]) is not str
                                or not 1 <= len(params["zone_name"]) <= MAX_NAME):
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")
    if "reference_panel_ref" in keys and (type(params["reference_panel_ref"]) is not str
                                          or not 1 <= len(params["reference_panel_ref"]) <= MAX_REF):
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")


def add_zones(graph, params):
    """ZONEHEIGHT: Add `add` times, set the offset, Apply, Close (ZoneHeightForm.cs)."""
    _request(params, {"expected_rev", "add", "offset"})
    add, offset = params["add"], params["offset"]
    if type(add) is not int or not 1 <= add <= 1000:
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")
    if not _is_number(offset) or not 0 <= offset <= 10000:
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    units = _context(result)
    zones = stored_zones(result, units)
    if add > kernel.MAX_ZONES - len(zones):
        _refuse("ELEVATION_ZONE_LIMIT_EXCEEDED")
    state = {"installation_design": "Roof", "units": units, "elevation_zones": zones}
    try:
        after = kernel.zone_height(state, {"add": add, "offset_in": offset})
    except kernel.BatchTwoError:
        raise GraphValidationError("ELEVATION_ZONE_MAPPING_FAILED") from None
    return {"graph": _commit(result, after), "zones": after, "added": add}


def _surrogates(items):
    """{application id: hex surrogate} in graph order; the kernel wants hex handles."""
    return {item["id"]: format(index + 1, "X") for index, item in enumerate(items)}


def _map_zones(zones, forward):
    """Zone lists into the kernel's namespace; an id the graph no longer holds passes through
    unchanged (it cannot collide with a hex surrogate) and is never moved, as the plugin keeps a
    missing handle in its zone list."""
    return [dict(zone, panels=[forward["panels"].get(ref, ref) for ref in zone["panels"]],
                 strings=[forward["strings"].get(ref, ref) for ref in zone["strings"]]) for zone in zones]


def _signature(frames, panel):
    frame = frames.get(panel["frame_ref"])
    if frame is None:
        return None
    return f"{float(frame['module_width_along_row'])!r}x{float(frame['module_height_across_row'])!r}"


def zone_colours(graph):
    """{panel id: the colour the stored zones give it}; UNZONED_COLOUR when no zone holds it."""
    units = _context(graph)
    colours = {}
    for zone in stored_zones(graph, units):
        for ref in zone["panels"]:
            colours.setdefault(ref, zone["colour"])
    return {panel["id"]: colours.get(panel["id"], UNZONED_COLOUR) for panel in graph["panels"]}


def _assign(graph, params, keys):
    _request(params, keys)
    result = checked_graph(graph, params["expected_rev"])
    units = _context(result)
    zones = stored_zones(result, units)
    if kernel._zone_named(zones, params["zone_name"]) is None:
        _refuse("MISSING_ELEVATION_ZONE")
    forward = {"panels": _surrogates(result["panels"]), "strings": _surrogates(result["strings"])}
    back = {key: {value: ref for ref, value in mapping.items()} for key, mapping in forward.items()}
    frames = {frame["id"]: frame for frame in result["frames"]}
    colours = zone_colours(result)
    mapped = _map_zones(zones, forward)
    intake = {"format": "zone-assign-intake-v1", "units": units, "elevation_zones": mapped,
              "panels": [{"panel": forward["panels"][p["id"]], "size": _signature(frames, p),
                          "colour": colours[p["id"]]} for p in result["panels"]],
              "strings": [forward["strings"][s["id"]] for s in result["strings"]]}
    return result, zones, forward, back, intake, mapped


def _unmap(zones, back):
    return [dict(zone, panels=[back["panels"].get(ref, ref) for ref in zone["panels"]],
                 strings=[back["strings"].get(ref, ref) for ref in zone["strings"]]) for zone in zones]


def assign_panels(graph, params):
    """LEAFZONEASSIGNPANELHEIGHT with ALL: every panel matching the reference's signature."""
    result, zones, forward, back, intake, mapped = _assign(
        graph, params, {"expected_rev", "zone_name", "reference_panel_ref"})
    reference = forward["panels"].get(params["reference_panel_ref"])
    if reference is None:
        _refuse("MISSING_PANEL")
    try:
        after, recoloured, skipped = kernel.zone_assign_panels(intake, mapped, params["zone_name"], reference)
    except kernel.BatchTwoError:
        raise GraphValidationError("ELEVATION_ZONE_MAPPING_FAILED") from None
    after = _unmap(after, back)
    return {"graph": _commit(result, after), "zones": after,
            "recoloured": [back["panels"][ref] for ref in recoloured], "skipped": skipped}


def assign_strings(graph, params):
    """LEAFZONEASSIGNSTRINGS with ALL: every string of the drawing, in graph order."""
    result, zones, forward, back, intake, mapped = _assign(graph, params, {"expected_rev", "zone_name"})
    try:
        after = kernel.zone_assign_strings(intake, mapped, params["zone_name"])
    except kernel.BatchTwoError:
        raise GraphValidationError("ELEVATION_ZONE_MAPPING_FAILED") from None
    after = _unmap(after, back)
    return {"graph": _commit(result, after), "zones": after}


OPERATIONS = {"add-zones": add_zones, "assign-panels": assign_panels, "assign-strings": assign_strings}


def run(graph, params):
    _bounded_json(params)
    # The operation is named as a string or the request is refused: an unhashable value never
    # reaches the lookup.
    if (type(params) is not dict or type(params.get("operation")) is not str
            or params["operation"] not in OPERATIONS):
        _refuse("INVALID_ELEVATION_ZONE_REQUEST")
    request = {key: value for key, value in params.items() if key != "operation"}
    return OPERATIONS[params["operation"]](graph, request)["graph"]
