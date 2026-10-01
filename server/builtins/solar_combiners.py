"""Place combiner boxes on the persisted design graph and persist them with their cabling in one commit: the
plugin's LEAFCOMBINERAUTO (solar_inverter_cabling.combiner_auto_place) run through
server/solar_combiner_graph.py on the drawing's own recorded combiner intake (wave-17 record
sf-w2-combiners-tool, piece two of sf-w2-combiners).

Frozen decisions (sf-w2-combiners-tool):
  - Carriage. The declaration names the trusted input source_intake. The commit rail
    (solar_local_graph._source_intake) hands the builtin the stored drawing intake of the SAME version whose
    graph it runs on, only after the stored bytes match the manifest sha256 and the intake's
    solar_design_graph_sha256 equals that graph's digest. The builtin reads two keys of it and nothing else:
    "combiner_intake" (the plugin's input-before-placement dump, format combiner-intake-v1) and
    "panel_groups" (a list of {handle, outlines}, outlines in drawing units: the shape the rooftop chain
    intake carries). Either key missing or of the wrong JSON type is INVALID_DRAWING_CONTEXT. Nothing in the
    request can supply or replace them.
  - Binding. The dump is never trusted on its own: solar_combiner_graph.place_combiners binds it to the live
    graph first (units, L2 numbers and positions, string endpoints, L2 hardware context, no existing
    combiners) and refuses any disagreement before the kernel runs.
  - Hardware. The new combiner boxes take model, max_dc_voltage and max_ac_power_kw from the request's
    "hardware" object only. The builtin never invents hardware.
  - Ids and time. New inverter and route ids are minted deterministically from the before graph's digest
    (sha256 of "solar-combiners|<digest>|<kind>|<n>", formatted as a version-4 UUID), skipping every id the
    graph already holds; created_at is the project's provenance created_at. The commit rail re-runs the
    builtin to prove a receipt, so the same graph and request always give the same result.
  - One commit. The placed graph (new combiner boxes, string reassignments, L1 feeds, every DC homerun and
    feeder route) is published by the rail's publish_version as one version: all of it or none of it. Every
    schedule becomes stale with the reason ROUTES_CHANGED. finish_mutation advances the revision once and
    stamps every changed entity.
  - Codes. Every refusal is a GraphValidationError whose code is a key of the Studio refusal map
    (web/src/lib/ribbonClusters.js SOLAR_REFUSAL_REASONS, upper-cased), except the request-shape code
    INVALID_COMBINER_REQUEST (the declaration's invalid_request_code). The module's own code rides in the
    error's path so a support reader still sees which check refused.

Readiness (hook): "valid_settings_required" outside L1/L2 mode, "unassigned_strings_required" when the graph
already holds a combiner (an L1 inverter), "string_collectors_required" with no L2 inverter,
"valid_strings_required" with no string, else ready. One pass over inverters; never raises on a valid graph.

Contract: fails closed; the input graph, request and intake are never mutated. Cost: one binding (linear in
strings and L2s), one bridge projection, one kernel pass, one bridge write-back and one graph validation.
"""
import hashlib
import math
import uuid

import solar_combiner_graph as cg
from solar_design_graph import GraphValidationError, _bounded_json, entities
from solar_sizing_client import checked_graph, digest
from solar_solve_results import finish_mutation

TOOL = "solar-combiners"
INVALID = "INVALID_COMBINER_REQUEST"
STALE_REASON = "ROUTES_CHANGED"
REQUEST_KEYS = frozenset({"expected_rev", "hardware"})
HARDWARE_KEYS = cg.HARDWARE_KEYS
MAX_MODEL = 128
MAX_RATING = 1_000_000
MAX_HANDLE = 64
# The module's closed codes onto keys of the Studio refusal map (upper-cased).
CODE_MAP = {
    "COMBINER_L2_MODE_REQUIRED": "VALID_SETTINGS_REQUIRED",
    "COMBINER_EXISTING_L1": "UNASSIGNED_STRINGS_REQUIRED",
    "COMBINER_INTAKE_INVALID": "INVALID_DRAWING_CONTEXT",
    "COMBINER_INTAKE_UNITS_MISMATCH": "SOLAR_OUTPUT_NOT_CURRENT",
    "COMBINER_INTAKE_CONTEXT_MISMATCH": "SOLAR_OUTPUT_NOT_CURRENT",
    "COMBINER_INTAKE_L2_MISMATCH": "SOLAR_OUTPUT_NOT_CURRENT",
    "COMBINER_INTAKE_STRING_MISMATCH": "SOLAR_OUTPUT_NOT_CURRENT",
    "COMBINER_OUTLINES_INVALID": "INVALID_DRAWING_CONTEXT",
    "COMBINER_HARDWARE_REQUIRED": INVALID,
    "COMBINER_HARDWARE_INVALID": INVALID,
    "COMBINER_NOT_PORTED": "CAPABILITY_NOT_READY",
    "COMBINER_KERNEL_REFUSED": "CAPABILITY_NOT_READY",
    "COMBINER_NOTHING_PLACED": "VALID_STRINGS_REQUIRED",
    "COMBINER_POSTCONDITION_FAILED": "CAPABILITY_NOT_READY",
}


def _refuse(code, path="<root>"):
    raise GraphValidationError(code, path)


def _rating(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAX_RATING


def _request(params):
    _bounded_json(params)
    if type(params) is not dict or set(params) != REQUEST_KEYS or type(params["expected_rev"]) is not int:
        _refuse(INVALID)
    hardware = params["hardware"]
    if (type(hardware) is not dict or set(hardware) != HARDWARE_KEYS
            or type(hardware["model"]) is not str or not 1 <= len(hardware["model"]) <= MAX_MODEL
            or not hardware["model"].strip()
            or not _rating(hardware["max_dc_voltage"]) or not _rating(hardware["max_ac_power_kw"])):
        _refuse(INVALID)
    return params["expected_rev"], {key: hardware[key] for key in sorted(HARDWARE_KEYS)}


def _drawing_inputs(source_intake):
    if type(source_intake) is not dict:
        _refuse("INVALID_DRAWING_CONTEXT", "source_intake")
    intake, groups = source_intake.get("combiner_intake"), source_intake.get("panel_groups")
    if type(intake) is not dict:
        _refuse("INVALID_DRAWING_CONTEXT", "combiner_intake")
    if type(groups) is not list:
        _refuse("INVALID_DRAWING_CONTEXT", "panel_groups")
    for group in groups:
        if (type(group) is not dict or set(group) != {"handle", "outlines"}
                or type(group["handle"]) is not str or not 1 <= len(group["handle"]) <= MAX_HANDLE
                or type(group["outlines"]) is not list):
            _refuse("INVALID_DRAWING_CONTEXT", "panel_groups")
    return intake, groups


def _minter(graph):
    seed = "solar-combiners|" + digest(graph)
    held = {item["id"] for item in entities(graph)}
    held.update({graph["project"]["id"], graph["settings"]["id"]})
    counter = [0]

    def mint(kind):
        while True:
            counter[0] += 1
            raw = hashlib.sha256(f"{seed}|{kind}|{counter[0]}".encode("utf-8")).digest()[:16]
            ident = f"leaf:{kind}:{uuid.UUID(bytes=raw, version=4)}"
            if ident not in held:
                held.add(ident)
                return ident
    return mint


def input_readiness(graph):
    """Whether LEAFCOMBINERAUTO can place combiners on this graph; never raises on a valid graph."""
    if graph["settings"].get("use_l2_collectors") is not True:
        return {"input_ready": False, "input_reason": "valid_settings_required"}
    if any(not inverter["is_l2"] for inverter in graph["inverters"]):
        return {"input_ready": False, "input_reason": "unassigned_strings_required"}
    if not any(inverter["is_l2"] for inverter in graph["inverters"]):
        return {"input_ready": False, "input_reason": "string_collectors_required"}
    if not graph["strings"]:
        return {"input_ready": False, "input_reason": "valid_strings_required"}
    return {"input_ready": True, "input_reason": None}


def run(graph, params, *, source_intake=None):
    expected_rev, hardware = _request(params)
    before = checked_graph(graph, expected_rev)
    readiness = input_readiness(before)
    if not readiness["input_ready"]:
        _refuse(readiness["input_reason"].upper())
    intake, groups = _drawing_inputs(source_intake)
    try:
        result, _ = cg.place_combiners(before, intake, groups, hardware=hardware, new_id=_minter(before),
                                       created_at=before["project"]["provenance"]["created_at"])
    except cg.CombinerGraphError as exc:
        _refuse(CODE_MAP[str(exc)], str(exc))
    except cg.rb.ElectricalBridgeError as exc:
        _refuse("CAPABILITY_NOT_READY", str(exc)[:64])
    for schedule in result["schedules"]:
        schedule["validity"] = {"state": "stale", "reasons": [STALE_REASON]}
    return finish_mutation(before, result, TOOL)
