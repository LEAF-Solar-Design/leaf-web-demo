"""Convert the drawing's current Ground physical state into compact Ground frames on the persisted
design graph and publish them in one commit: the plugin's LEAFTRACKERSTOPANELGROUPS run through
server/solar_ground_conversion.py (wave-18 record sf-w3-conversion-graph-tool, the last conversion
piece).

Frozen decisions (sf-w3-conversion-graph-tool):
  - Carriage. The declaration names the trusted input physical_state and nothing else. The commit
    rail (solar_local_graph._physical_state) reads the drawing's current physical head once and hands
    the builtin {"view", "document"} (both None when no state was ever published). Nothing in the
    request can name or replace the state.
  - Binding. The result records the state it converted under graph.extra.physical_state
    ({head_index, state_artifact_id, state_content_sha256}); the rail refuses a result that does not
    name the head it handed over (PHYSICAL_STATE_UNBOUND), and the replay proof re-reads exactly that
    immutable head-log entry and artifact, so a later physical publish never breaks the proof.
  - Replace, never append. The conversion's frame ids are deterministic in the graph's source_hash and
    the tracker ordinal, so a second conversion appended to the first would collide. Every prior frame
    (all of them are earlier conversions: readiness refuses any other content) is dropped and the new
    frames take their place. A design with anything built on the frames (panels, strings, inverters,
    routes, schedules, electrical zones, or a frame without a ground_slots block) refuses
    GROUND_CONVERSION_IN_USE before anything is computed; nothing is ever silently removed.
  - Counters. settings.panel_group_number becomes the layout's next PanelGroupNumber; the colour
    counter (no graph field) rides in graph.extra.ground_conversion with the counts and the overlap
    report. All of it is one version: the rail publishes it or nothing.
  - Ids, time and revision. Frames and their slot templates carry rev = before rev + 1 and the
    provenance {created_by, last_writer, tool_id: TOOL, created_at: the project's provenance
    created_at, source_rev: before rev}, exactly what finish_mutation stamps, so the replay proof
    reproduces every byte. No clock, no random id.
  - Codes. Every refusal is a GraphValidationError whose code, lower-cased, is a key of the Studio
    refusal map (web/src/lib/ribbonClusters.js SOLAR_REFUSAL_REASONS), except the request-shape code
    INVALID_TRACKER_CONVERSION_REQUEST (the declaration's invalid_request_code) and the rail's own codes
    (STALE_GRAPH_REVISION, UNRESOLVED_UNITS, NONFINITE_NUMBER). The kernel's code rides in the error's
    path.

Readiness (hook): "ground_installation_required" on a non-Ground project, "ground_conversion_in_use"
when the design holds anything the replacement would discard, else ready. One pass over the
collections; never raises on a valid graph. Whether a physical state exists is answered by the
availability overlay (product_capability_availability), which can see the store.

Contract: fails closed; the input graph, request and physical state are never mutated. Cost: one
conversion (the kernel's), one coverage pass and one graph validation (finish_mutation).
"""
import copy

import solar_ground_conversion as conv
from solar_design_graph import COLLECTIONS, GraphValidationError, _bounded_json
from solar_sizing_client import checked_graph
from solar_solve_results import finish_mutation

TOOL = "solar-trackers-to-panel-groups"
INVALID = "INVALID_TRACKER_CONVERSION_REQUEST"
REQUEST_KEYS = frozenset({"expected_rev"})
MAX_COUNTER = 1_000_000  # $defs.settings.panel_group_number maximum
REPORT_SCHEMA = conv.RESULT_SCHEMA
# The kernel's closed codes onto keys of the Studio refusal map (upper-cased).
CODE_MAP = {
    "GROUND_CONVERSION_PHYSICAL_HEAD_REQUIRED": "GROUND_PHYSICAL_STATE_REQUIRED",
    "GROUND_CONVERSION_TRACKER_ROWS_REQUIRED": "GROUND_TRACKER_ROWS_REQUIRED",
    "GROUND_CONVERSION_GROUND_PROJECT_REQUIRED": "GROUND_INSTALLATION_REQUIRED",
    "GROUND_CONVERSION_UNITS_MISMATCH": "GROUND_UNITS_MISMATCH",
    "GROUND_CONVERSION_INPUT_INVALID": "GROUND_LAYOUT_INVALID",
    "GROUND_CONVERSION_LIMIT_EXCEEDED": "GROUND_LAYOUT_TOO_LARGE",
}


def _refuse(code, path="<root>"):
    raise GraphValidationError(code, path)


def input_readiness(graph):
    """Graph-only readiness, in order: a Ground project, then nothing the replacement would drop."""
    if graph["project"]["installation_design"] != "Ground":
        return {"input_ready": False, "input_reason": "ground_installation_required"}
    if (any(graph[key] for key in COLLECTIONS if key != "frames")
            or any("ground_slots" not in frame for frame in graph["frames"])):
        return {"input_ready": False, "input_reason": "ground_conversion_in_use"}
    return {"input_ready": True, "input_reason": None}


def _request(params):
    _bounded_json(params)
    if type(params) is not dict or set(params) != REQUEST_KEYS or type(params["expected_rev"]) is not int:
        _refuse(INVALID)
    return params["expected_rev"]


def run(graph, params, *, physical_state=None):
    before = checked_graph(graph, _request(params))
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        _refuse(readiness["input_reason"].upper())
    if (type(physical_state) is not dict or set(physical_state) != {"view", "document"}):
        _refuse("GROUND_PHYSICAL_STATE_REQUIRED", "physical_state")
    base = copy.deepcopy(before)
    base["frames"] = []
    rev = before["rev"] + 1
    provenance = {"created_by": TOOL, "created_at": before["project"]["provenance"]["created_at"],
                  "last_writer": TOOL, "source_rev": before["rev"], "tool_id": TOOL}
    try:
        result = conv.convert_physical_state(physical_state["view"], physical_state["document"], base,
                                             provenance=provenance, rev=rev)
    except conv.GroundConversionError as exc:
        _refuse(CODE_MAP[exc.code], exc.code)
    number = result["settings"]["PanelGroupNumber"]
    colour = result["settings"]["PanelGroupColour"]
    if (type(number) is not int or not 0 <= number <= MAX_COUNTER
            or type(colour) is not int or not 0 <= colour <= MAX_COUNTER):
        _refuse("GROUND_LAYOUT_INVALID", "GROUND_CONVERSION_INPUT_INVALID")
    base["frames"] = result["frames"]
    base["settings"]["panel_group_number"] = number
    base["extra"]["physical_state"] = dict(result["source"])
    base["extra"]["ground_conversion"] = {
        "schema": REPORT_SCHEMA, "counts": dict(result["counts"]), "overlap": dict(result["overlap"]),
        "panel_group_colour": colour}
    return finish_mutation(before, base, TOOL)
