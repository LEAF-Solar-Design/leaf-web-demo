"""Colour every string by its inverter on the design graph: Studio's LEAFCOLORSTRINGS
(BranchCmd.RecolorStringsByInverter), ported literally in server/solar_inverter_strings.py
`color_strings` (contract G35, receipt color-strings/rooftop-inverters-i3). A read: the graph stores no
colour, and a string's colour follows from its assignment, so nothing is written.

The graph never meets the kernel directly. server/solar_electrical_state_bridge.py (the one frozen
mapping) projects it into a G35 state: an unassigned string carries circuit "-", an assigned one its
graph circuit_tag when that parses in the kernels' grammar and names its collector's number, else the
synthesized "+k/<number><letter>". The kernel then parses each circuit the way the plugin's
CircuitTagParser does (the digits right after the first "/"), and colours the string:
  - no inverter number (unassigned, or a tag whose digits do not follow the slash): colour 7;
  - otherwise the untyped six-colour array (1, 2, 5, 3, 6, 4) at (number - 1) mod 6.
A string with no route has no start or end block in the bridge's state, so, like a plugin string
missing its annotations, it is counted but not recoloured (colour null here).

Declared divergence (maturity preview): the plugin picks a typed colour family when the drawing's
InverterTypeAssignments and InverterTypes settings type the inverter (B: 5, 4, 3; C: 8, 30, 40; other
keys 1, 2, 6). The v1 graph carries neither setting (an inverter's type_key alone does not type it; the
recorded i1 drawing has type_key "A" on every device and both settings empty, and colours untyped), and
the bridge projects neither, so every colour here is from the untyped family and the answer says so
(`colour_family` "untyped"). In-block module overlays for tracker panel ids are refused by the kernel;
the bridge carries no panel ids, so that path is reachable only through a kernel refusal, which maps to
COLOR_STRINGS_NOT_PORTED.

Contract: pure and fails closed. The input graph and params are never mutated and nothing is written;
the bridge (which validates the graph) and the kernel have the last word; every bridge or kernel error
becomes a named GraphValidationError, never an escape. Cost: the bridge's linear projection plus the
kernel's one pass over the strings; the answer is one row per string, bounded by the read adapter's
1 MiB output limit (READ_OUTPUT_LIMIT_EXCEEDED above it).
"""
import solar_electrical_state_bridge as bridge
from solar_design_graph import GraphValidationError, _bounded_json

kernel = bridge._load_sibling("solar_inverter_strings")

TOOL = "solar-color-strings"
INVALID = "INVALID_COLOR_STRINGS_REQUEST"
SCHEMA = "leaf.solar-string-colours.v1"
COLOUR_FAMILY = "untyped"


def _refuse(code):
    raise GraphValidationError(code)


def string_colours(graph):
    """The kernel's LEAFCOLORSTRINGS answer on the graph: (rows in graph string order, printed lines)."""
    try:
        state, binding = bridge.state_from_graph(graph)
    except bridge.ElectricalBridgeError as error:
        raise GraphValidationError(error.code) from None
    try:
        after, lines = kernel.color_strings(state, {})
    except kernel.InverterStringNotPortedError:
        raise GraphValidationError("COLOR_STRINGS_NOT_PORTED") from None
    except kernel.InverterStringError:
        raise GraphValidationError("COLOR_STRINGS_MAPPING_FAILED") from None
    rows = {row["string"]: row for row in after["rows"]["string-assignment"]}
    if set(rows) != set(binding["strings"]) or len(rows) != len(after["rows"]["string-assignment"]):
        _refuse("COLOR_STRINGS_MAPPING_FAILED")
    by_id = {item["id"]: handle for handle, item in binding["strings"].items()}
    out = []
    for string in graph["strings"]:
        colour = rows[by_id[string["id"]]]["colour"]
        if colour is not None and (type(colour) is not int or not 0 <= colour <= 256):
            _refuse("COLOR_STRINGS_MAPPING_FAILED")
        out.append({"string_ref": string["id"], "colour": colour})
    return out, list(lines)


def run(graph, params):
    _bounded_json(params)
    if type(params) is not dict or params:
        _refuse(INVALID)
    rows, lines = string_colours(graph)
    return {"schema": SCHEMA, "colour_family": COLOUR_FAMILY, "lines": lines, "strings": rows,
            "counts": {"strings": len(rows), "recoloured": sum(row["colour"] is not None for row in rows)}}
