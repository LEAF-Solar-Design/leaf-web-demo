#!/usr/bin/env python3
"""Produce Studio's committed auto-fill: group the drawing, remove panels, rebalance.

AUTOFILL is scoped to nothing: it scans every panel group and moves membership
until each group holds a count that splits into strings of n-2..n. A run that
never grouped anything, and never made a group infeasible, has nothing to
rebalance, so this producer starts from the same committed state the plugin
started from. The panels come from the bound intake, the groups are created by
the same kernel and the same solar_panel_groups builtin the groups producer uses,
from rule G1's four parameters and nothing new, server/builtins/solar_panel_remove.py
then drops exactly the panels `--remove` names, and server/solar_autofill.py (the
pure port of the plugin's OptimalPlanSolver and SnakePanelSelector) computes the
corrections that server/builtins/solar_autofill.py applies.

GROUP ORDER. The solver's tie-breaks read the order the plugin SCANNED the groups
in, which is AutoCAD's database order for a select-all (BranchCmd.cs:3631). A
command that rebuilds a group's block reference gives it a new handle at the end
of the database, so a group an earlier edit touched is scanned LAST. REMOVEPANEL
rebuilds the group it cuts: on the 2026-09-23 capture the 71-panel group's block
was 9D2F before AUTOFILL ran, created by that removal, above every original group
block 9C93..9CFB (receipts/w2-autofill-20260923/FINDING.md). This producer
reproduces exactly that: the committed frame order, with every frame the removal
touched moved to the end in the order the removal touched them.

GEOMETRY. The port reads the panel centre and rotation the PLUGIN reads
(GetEntityCentre and Panel.mAngle, BranchCmd.cs:3682-3697), which is what the
grouping kernel already computes per panel: the rectangle centre and its
rationalised angle in radians. The graph's own panel record carries a
polyline-vertex mean and an edge direction in degrees, which is a different
quantity, so the kernel's values are used and the metadata says so.

REVERT. With `--revert` the run applies the rebalance and then undoes it with
server/builtins/solar_autofill.py's revert_corrections, so the written graph is
the auto-filled drawing put back to the membership it held before AUTOFILL ran.
Studio's revert is CORRECT where the plugin's AutoFillRevert is not: the plugin
keys its snapshot by block handle and AutoFill rebuilds every group it changes
under a new one, so its revert skips exactly the groups that moved. Provenance
carries the corrections that were made AND `reverted`, so the evidence says which
plan was undone. Without the flag the run behaves exactly as it did before.

The groups capability carries no string sizing, so the groups builtin's sizing
recheck is bypassed for the grouping step exactly as the groups producer bypasses
it, and the metadata lists settings/string_sizing as synthetic. The string length
auto-fill solves against is `--max-string-length`
(mSettings.NumPanelsInSequence, BranchCmd.cs:3617); it is recorded in provenance
rather than in parameters, which rule G1 pins to the four grouping values.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from mutation_plan import plan_sha256
from solar_autofill import AutofillError, autofill
from solar_design_graph import deserialize_graph, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph

PRODUCER = "solar_w1_studio_autofill.v1"
MAX_REMOVED = 4096
MAX_STRING_LENGTH = 4096


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The grouping half of this run IS the groups producer and the removal half IS the
# panel-remove producer: same kernel, same entity identities, same builtin commits.
# Only the rebalance and the metadata are new here.
remove = _sibling("solar_w1_studio_panel_remove")
groups = remove.groups
solve = groups.solve
kernel = groups.kernel
builtin = groups.builtin
sha256 = groups.sha256
read_json = groups.read_json
normalized_handle = groups.normalized_handle
fixture_revision = groups.fixture_revision
group_neutral_id = groups.group_neutral_id
evidence_mapping = remove.evidence_mapping
SCHEMA = groups.SCHEMA
INSTALLATION_DESIGNS = groups.INSTALLATION_DESIGNS


def commit_groups(intake, intake_hash, fixture_hash, args, design, created_at):
    """The committed grouped state auto-fill starts from, rule G1 parameters.

    Returns ``(graph, kernel panels by neutral handle)``: the port needs the
    plugin's own panel centre and angle, which the kernel has already computed.
    """
    kernel_panels = kernel.panels_from_intake(intake, layer_contains=args.layer_contains,
                                              installation_design=design)
    if not kernel_panels:
        raise ProducerError("intake has no panel polylines on the filtered layer")
    kernel_groups = kernel.group_panels(
        kernel_panels, branch_max_offset=args.branch_max_offset,
        alignment_tolerance=args.alignment_tolerance, installation_design=design)
    graph = new_empty_graph(
        tenant_id="studio-autofill", drawing_id="w1-" + fixture_hash[:16],
        source_hash=intake_hash,
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = groups.import_panels(graph, intake, kernel_panels, created_at)
    graph["settings"]["extra"]["string_sizing"] = deepcopy(groups.UNSIZED_SIZING)
    graph = validate_graph(graph)
    specifications, frames = groups.group_frames(graph, kernel_groups, by_handle,
                                                 dimensions, created_at)

    def licensed_matrix(*, plan, **kwargs):
        # The kernel's matrix is Studio's own answer, not a recorded plugin placement.
        return {"plan_sha256": plan_sha256(plan), "frames": deepcopy(frames)}

    module = builtin("solar_panel_groups")
    # Scope the sizing bypass to this CLI invocation; every other builtin check runs.
    with patch.object(module, "require_sizing", lambda graph: None):
        grouped = module.create_groups(graph, {
            "expected_rev": graph["rev"], "groups": specifications,
        }, drawing_intake=intake, licensed_matrix=licensed_matrix)["graph"]
    if len(grouped["frames"]) != len(kernel_groups) or any(
            panel["frame_ref"] is None for panel in grouped["panels"]):
        raise ProducerError("committed groups do not cover every panel")
    geometry = {normalized_handle(panel["handle"]): panel for panel in kernel_panels}
    return grouped, geometry


def scan_order(graph, rebuilt):
    """The frames in the order AUTOFILL would scan them (module docstring).

    Every frame the removal did NOT touch keeps its committed position; every one
    it did rebuild goes to the end, in the order the removal touched them. One
    pass over the frames, no per-frame rescan.
    """
    touched = {frame_id: position for position, frame_id in enumerate(rebuilt)}
    ordered = [frame for frame in graph["frames"] if frame["id"] not in touched]
    by_id = {frame["id"]: frame for frame in graph["frames"]}
    for frame_id in rebuilt:
        frame = by_id.get(frame_id)
        if frame is None:
            raise ProducerError("the removal named a group that is no longer committed")
        ordered.append(frame)
    if len(ordered) != len(graph["frames"]):
        raise ProducerError("the scan order must name every group exactly once")
    return ordered


def solver_groups(graph, geometry, rebuilt):
    """The port's input: one entry per frame, members in the frame's own order.

    The zone key is the plugin's ``(electrical ?? "") + "|" + (elevation ?? "")``
    (OptimalPlanSolver.cs:161); Studio carries no elevation zone, so the second
    half is always empty.
    """
    # One pass over the panels, then one over the members: never a graph scan per member.
    by_id = {panel["id"]: panel for panel in graph["panels"]}
    handles = {panel_id: normalized_handle(panel["provenance"]["source_handle"])
               for panel_id, panel in by_id.items()}
    entries = []
    for frame in scan_order(graph, rebuilt):
        panels = []
        for ref in frame["panel_refs"]:
            source = geometry.get(handles[ref])
            if source is None:
                raise ProducerError("a group member has no panel geometry from the intake")
            location = by_id[ref]["matrix_cell"]
            panels.append({"id": ref, "x": source["centre"][0], "y": source["centre"][1],
                           "angle": source["angle"],
                           "row": location["row"] if location else None,
                           "col": location["col"] if location else None})
        entries.append({"id": frame["id"],
                        "zone": (frame["electrical_zone_ref"] or "") + "|",
                        "panels": panels})
    return entries


def produce(args):
    started = time.monotonic()
    contains = args.layer_contains
    if not isinstance(contains, str) or not contains.strip() or len(contains) > 255:
        raise ProducerError("layer contains must be a nonempty layer name fragment")
    for label, value in (("branch max offset", args.branch_max_offset),
                         ("alignment tolerance", args.alignment_tolerance)):
        if type(value) is not float or not math.isfinite(value) or value <= 0:
            raise ProducerError(f"{label} must be a positive finite number")
    design = args.installation_design
    if design not in INSTALLATION_DESIGNS:
        raise ProducerError("installation design must be Roof: the Studio frame schema carries no other value")
    length = args.max_string_length
    if type(length) is not int or not 1 <= length <= MAX_STRING_LENGTH:
        raise ProducerError("max string length must be a positive integer")
    requested = args.remove
    if not isinstance(requested, list) or len(requested) > MAX_REMOVED:
        raise ProducerError("remove takes at most one panel handle per flag")
    fixture = args.fixture.resolve()
    fixture_hash = sha256(fixture.read_bytes())
    intake, intake_hash = read_json(args.intake)
    if not isinstance(intake, dict) or intake.get("source", {}).get("dwg_sha256") != fixture_hash:
        raise ProducerError("fixture hash mismatch with intake")
    revision = fixture_revision(fixture)
    if not isinstance(intake.get("polylines"), list) or not intake["polylines"]:
        raise ProducerError("intake requires panel polylines")
    created_at = datetime.now(timezone.utc).isoformat()
    grouped, geometry = commit_groups(intake, intake_hash, fixture_hash, args, design, created_at)
    handles = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in grouped["panels"]}

    graph, rebuilt, removed_ids = grouped, [], []
    if requested:
        per_frame, removed_ids = remove.removal_plan(grouped, requested)
        remover = builtin("solar_panel_remove")
        for frame_id, refs in per_frame.items():
            removal = remover.remove_panels(graph, {
                "expected_rev": graph["rev"], "frame_ref": frame_id, "panel_refs": refs})
            graph = removal["graph"]
            rebuilt.append(frame_id)

    # One pass for the state auto-fill starts from: the membership a revert must
    # restore exactly, and the neutral id and count each group carries there.
    before_members = {frame["id"]: frozenset(handles[ref] for ref in frame["panel_refs"])
                      for frame in graph["frames"]}
    before_ids = {frame_id: group_neutral_id(members)
                  for frame_id, members in before_members.items()}
    before_counts = {frame_id: len(members) for frame_id, members in before_members.items()}
    plan = autofill(solver_groups(graph, geometry, rebuilt), length)
    corrections = plan["corrections"]
    reverted = bool(args.revert)

    if corrections:
        applier = builtin("solar_autofill")
        request = [{"from_ref": correction["from"], "to_ref": correction["to"],
                    "panel_refs": list(correction["panels"])}
                   for correction in corrections]
        graph = applier.apply_corrections(graph, {
            "expected_rev": graph["rev"], "corrections": request,
            "alignment_tolerance": args.alignment_tolerance})["graph"]
        if reverted:
            # The SAME plan, undone: revert_corrections refuses unless the graph is
            # still in the auto-filled state, so this cannot half-apply.
            graph = applier.revert_corrections(graph, {
                "expected_rev": graph["rev"], "corrections": request})["graph"]
    reopened = deserialize_graph(serialize_graph(graph))
    validate_graph(reopened)
    if {panel["id"] for panel in reopened["panels"]} != set(handles):
        raise ProducerError("the rebalance must keep every panel it started with")
    if len(reopened["frames"]) != len(grouped["frames"]):
        raise ProducerError("the rebalance must leave every group in place")
    surviving = {frame["id"]: frame for frame in reopened["frames"]}
    if set(surviving) != set(before_ids):
        raise ProducerError("the rebalance must leave every group in place")
    ungrouped = set(removed_ids)  # hoisted: one set, never rebuilt per panel
    for panel in reopened["panels"]:
        inside = panel["frame_ref"] is not None
        if (panel["id"] in ungrouped) is inside:
            raise ProducerError("a removed panel is still a member of a group")
    # The solver conserves each zone's total, so the drawing's grouped panel count
    # is exactly what the removal left. Anything else is a lost or duplicated panel.
    if sum(len(frame["panel_refs"]) for frame in reopened["frames"]) != sum(before_counts.values()):
        raise ProducerError("the rebalance must conserve the grouped panel count")
    if reverted:
        # Membership, not counts: the whole point of the revert is that every group
        # holds the SAME panels again, not merely the same number of them.
        restored = {frame_id: frozenset(handles[ref] for ref in frame["panel_refs"])
                    for frame_id, frame in surviving.items()}
        if restored != before_members:
            raise ProducerError("the revert must restore every group's membership exactly")
    else:
        for frame_id, frame in surviving.items():
            if len(frame["panel_refs"]) != plan["counts"][frame_id]:
                raise ProducerError("a committed group does not hold the count the solver planned")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    correction_records = [
        {"from": before_ids[correction["from"]], "to": before_ids[correction["to"]],
         "panels": [handles[ref] for ref in correction["panels"]],
         "distance": round(correction["distance"], 3), "chain": correction["chain"]}
        for correction in corrections]
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        # Rule G1: the groups parameters, as given, and nothing new for the
        # rebalance; the string length the solver used is in provenance.
        "parameters": {"family": "groups", "layer_filter": "*" + contains + "*",
                       "branch_max_offset": float(args.branch_max_offset),
                       "alignment_tolerance": float(args.alignment_tolerance),
                       "installation_design": design},
        "versions": {"schema": SCHEMA, "producer": PRODUCER, "capability": "0",
                     "engine": "server-builtin", "catalog": "none", "solver": "none"},
        "coordinate_system": "world", "geometry_units": "in", "angle_units": "deg",
        "entity_mapping": mapping, "before": {"recorded": False},
        "changes": {"created": [], "modified": [], "deleted": []}, "warnings": [], "rejected_inputs": [],
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "execution_mode": "live", "state": "committed",
        "survived_reopen": True, "synthetic_fields": ["before", "changes", "settings/string_sizing"],
        "fallback_fields": ["panels/producer-built-from-intake",
                            "autofill/geometry-from-grouping-kernel"],
        "synthetic_flagged": True,
        "provenance": {"intake_sha256": intake_hash, "kernel": "solar_panel_group_kernel",
                       "panel_count": len(reopened["panels"]), "group_count": len(reopened["frames"]),
                       # The run grouped the drawing first, then removed, then
                       # rebalanced: this says how many groups it built, which
                       # panels it freed, and every correction the port made, in
                       # the solver's own order. Rule G3 derives a group's neutral
                       # id from its CURRENT members, so a correction names the
                       # ids the two groups had BEFORE the move.
                       "groups_created": len(grouped["frames"]),
                       "panels_removed": len(removed_ids),
                       "removed": sorted(handles[ref] for ref in removed_ids),
                       "max_string_length": length,
                       "solver": "OptimalPlanSolver", "solver_feasible": plan["feasible"],
                       "solver_valid": plan["valid"], "violations": plan["violations"],
                       "panels_moved": plan["total_moved"],
                       "groups_modified": len({correction["from"] for correction in corrections}
                                              | {correction["to"] for correction in corrections}),
                       "corrections": correction_records,
                       # True when the corrections above were MADE and then undone, so
                       # the committed groups hold their pre-auto-fill membership again.
                       # Studio's revert is correct where the plugin's is not, which
                       # the receipt records as a declared divergence.
                       "reverted": reverted},
    }
    args.out_graph.write_text(serialize_graph(reopened) + "\n", encoding="utf-8")
    args.out_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True,
                                            allow_nan=False) + "\n", encoding="utf-8")
    return reopened, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "intake", "out-graph", "out-metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--branch-max-offset", type=float, required=True)
    parser.add_argument("--alignment-tolerance", type=float, required=True)
    parser.add_argument("--layer-contains", required=True)
    parser.add_argument("--installation-design", required=True)
    parser.add_argument("--max-string-length", type=int, required=True,
                        help="the string length auto-fill solves against (NumPanelsInSequence)")
    parser.add_argument("--remove", action="append", default=[],
                        help="source handle of a panel to remove before the rebalance; repeatable")
    parser.add_argument("--revert", action="store_true",
                        help="undo the rebalance after it is applied and write the reverted graph")
    args = parser.parse_args(argv)
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object or an intake body.
        # The sibling producers raise their OWN ProducerError classes; every one of
        # those messages is bounded too, so they pass through unchanged.
        bounded = isinstance(exc, (ProducerError, remove.ProducerError, groups.ProducerError,
                                   solve.ProducerError, kernel.PanelGroupKernelError,
                                   AutofillError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-autofill: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
