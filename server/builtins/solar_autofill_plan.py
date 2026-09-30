"""Preview half of AUTOFILL: compute with the pure port server/solar_autofill.py,
publish nothing. The apply half is builtins/solar_autofill.py (solar-autofill).

Groups are every frame, with members in the frame's panel_refs order.
SCAN ORDER is part of the answer: the kernel's DP breaks ties by group order
(see server/solar_autofill.py). The plugin scans in AutoCAD database order;
rebuilding a group's block gives it a new handle at the end, so it scans last.
Studio approximates this with frames sorted stably by rev ascending, ties in
committed list order. A frame a later write touched follows untouched frames.
This is a declared approximation: any Studio frame write moves it, not only
writes that rebuild blocks.

The zone key is (frame["electrical_zone_ref"] or "") + "|", the plugin's
(electrical ?? "") + "|" + (elevation ?? "") with no elevation zone in Studio,
as in scripts/solar_w1_studio_autofill.py. Geometry comes from the panel record:
centre as stored and kernel.angle_rationalise(math.radians(panel["angle"])).
The graph stores the outline's first edge direction in degrees; the plugin
uses the rectangle's long side folded into [0, 2 pi]. They agree on every
captured rooftop panel. An outline starting on its short side would differ
by 90 degrees, a declared limit.

String length is the request's panels_per_string, else the graph's committed
settings.panels_in_sequence when an int of at least 1 within the kernel bound,
else STRING_LENGTH_NOT_SIZED. The plugin reads mSettings.NumPanelsInSequence
(BranchCmd.cs:3617).

The plan is PROVEN before being offered: the commit builtin's own run executes
on a validated private copy, then proves the revert on the committed copy.
Only a plan that commits and reverts is returned as apply.request.
A commit refusal is returned as apply.status "refused" with
its code, never raised. Fails closed on malformed requests and bounded: one
validate, one pass over frames and members, one kernel run, at most one
dry-run apply and one dry-run revert.
"""
import copy
import importlib.util
import math
from pathlib import Path

import solar_panel_group_kernel as kernel
from solar_autofill import AutofillError, autofill
from solar_design_graph import GraphValidationError, _bounded_json, validate_graph
from solar_sizing_client import units_resolved

TOOL = "solar-autofill-plan"
PLAN_SCHEMA = "leaf.solar-autofill-plan.v1"
# The kernel's MAX_STRING_LENGTH and the schema maximum.
MAX_PANELS_PER_STRING = 4096
# AutoCAD rotations are in [0, 360); within +/-720 degrees the kernel's
# repeated-subtraction angle folding takes at most two turns and preserves values.
MAX_PANEL_ANGLE = 720
# contract/solar-design-graph.v1.schema.json properties.rev.maximum.
MAX_GRAPH_REV = 1000000
# The commit tool's own bounds, so an echoed tolerance is always accepted there.
MIN_ALIGNMENT_TOLERANCE = 1e-6
MAX_ALIGNMENT_TOLERANCE = 1e6
_ALLOWED = frozenset(("panels_per_string", "alignment_tolerance"))


def _load_applier():
    """Load the commit builtin once by path, using the exact code apply runs."""
    path = Path(__file__).resolve().with_name("solar_autofill.py")
    spec = importlib.util.spec_from_file_location("_solar_autofill_plan_applier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_APPLIER = _load_applier()


def _request(params):
    """Return (length or None, tolerance or None), or refuse the request.

    Length accepts ints and integral floats in [1, 4096], converted exactly.
    Tolerance accepts ints or floats in [1e-6, 1e6], returned as float.
    Neither accepts bool, str, None, or out-of-range values.
    """
    if type(params) is not dict or not set(params) <= _ALLOWED:
        raise GraphValidationError("INVALID_AUTOFILL_PLAN_REQUEST")
    length = None
    if "panels_per_string" in params:
        value = params["panels_per_string"]
        if type(value) is float and math.isfinite(value) and value.is_integer():
            value = int(value)
        if type(value) is not int or not 1 <= value <= MAX_PANELS_PER_STRING:
            raise GraphValidationError("INVALID_AUTOFILL_PLAN_REQUEST")
        length = value
    tolerance = None
    if "alignment_tolerance" in params:
        value = params["alignment_tolerance"]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not MIN_ALIGNMENT_TOLERANCE <= value <= MAX_ALIGNMENT_TOLERANCE):
            raise GraphValidationError("INVALID_AUTOFILL_PLAN_REQUEST")
        tolerance = float(value)
    return length, tolerance


def scan_order(graph):
    """Frames stably by rev ascending; see the module's scan-order contract."""
    return sorted(graph["frames"], key=lambda frame: frame["rev"])


def solver_groups(graph):
    """One entry per frame in scan order; one panel lookup and one member pass."""
    panels = {panel["id"]: panel for panel in graph["panels"]}
    groups = []
    for frame in scan_order(graph):
        members = []
        for ref in frame["panel_refs"]:
            panel = panels[ref]
            if abs(panel["angle"]) > MAX_PANEL_ANGLE:
                raise GraphValidationError("AUTOFILL_PANEL_ANGLE_UNSUPPORTED")
            members.append({"id": ref, "x": float(panel["centre"][0]), "y": float(panel["centre"][1]),
                            "angle": kernel.angle_rationalise(math.radians(panel["angle"]))})
        groups.append({"id": frame["id"], "zone": (frame["electrical_zone_ref"] or "") + "|",
                       "panels": members})
    return groups


def run(graph, params):
    _bounded_json(params)
    length, tolerance = _request(params)
    result = validate_graph(graph)
    if not units_resolved(result):
        raise GraphValidationError("UNRESOLVED_UNITS")
    source = "request"
    if length is None:
        length, source = result["settings"]["panels_in_sequence"], "settings"
        if type(length) is not int or not 1 <= length <= MAX_PANELS_PER_STRING:
            raise GraphValidationError("STRING_LENGTH_NOT_SIZED")
    frames = scan_order(result)
    try:
        plan = autofill(solver_groups(result), length)
    except AutofillError:
        raise GraphValidationError("AUTOFILL_PLAN_REFUSED") from None
    except ArithmeticError:
        raise GraphValidationError("AUTOFILL_GEOMETRY_UNSUPPORTED") from None
    corrections = [{"from_ref": row["from"], "to_ref": row["to"], "panel_refs": list(row["panels"])}
                   for row in plan["corrections"]]
    apply = {"status": "none", "reason_code": None, "request": None, "revert_request": None}
    if corrections:
        request = {"operation": "apply-corrections", "expected_rev": result["rev"],
                   "corrections": corrections}
        if tolerance is not None:
            request["alignment_tolerance"] = tolerance
        try:
            if result["rev"] + 2 > MAX_GRAPH_REV:
                raise GraphValidationError("AUTOFILL_REVERT_UNAVAILABLE")
            if tolerance is not None:
                touched = {row[key] for row in corrections for key in ("from_ref", "to_ref")}
                for frame in result["frames"]:
                    if frame["id"] not in touched:
                        continue
                    row_angle = frame["provenance"].get("row_angle", 0.0)
                    if (type(row_angle) not in (int, float)
                            or abs(row_angle) > 4 * math.pi or not math.isfinite(row_angle)):
                        raise GraphValidationError("AUTOFILL_REGRID_METADATA_INVALID")
            applied = _APPLIER.run(copy.deepcopy(result), copy.deepcopy(request))
            revert_request = {"operation": "revert-corrections",
                              "expected_rev": result["rev"] + 1,
                              "corrections": copy.deepcopy(corrections)}
            try:
                _APPLIER.run(copy.deepcopy(applied), copy.deepcopy(revert_request))
            except GraphValidationError:
                raise GraphValidationError("AUTOFILL_REVERT_UNAVAILABLE") from None
        except GraphValidationError as refusal:
            apply = {"status": "refused", "reason_code": refusal.code, "request": None,
                     "revert_request": None}
        except ArithmeticError:
            apply = {"status": "refused", "reason_code": "AUTOFILL_GEOMETRY_UNSUPPORTED",
                     "request": None, "revert_request": None}
        else:
            apply = {"status": "ready", "reason_code": None, "request": request,
                     "revert_request": revert_request}
    return {
        "schema": PLAN_SCHEMA, "graph_rev": result["rev"],
        "panels_per_string": length, "panels_per_string_source": source,
        "scan_order": [frame["id"] for frame in frames],
        "feasible": plan["feasible"], "valid": plan["valid"],
        "violations": list(plan["violations"]), "disruption": plan["disruption"],
        "total_moved": plan["total_moved"],
        "counts_before": {frame["id"]: len(frame["panel_refs"]) for frame in frames},
        "counts_after": dict(plan["counts"]),
        "corrections": [dict(correction, distance=row["distance"], chain=row["chain"])
                        for correction, row in zip(corrections, plan["corrections"])],
        "apply": apply,
    }
