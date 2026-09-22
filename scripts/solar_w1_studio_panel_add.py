#!/usr/bin/env python3
"""Produce Studio's committed panel claim: group the drawing, free panels, then claim them.

ADDPANEL's default mode CLAIMS an unassociated panel into the group the operator
named, so a run whose every panel is already grouped has nothing to claim. This
producer therefore reaches the capture's starting state the same way the capture
did: the panels come from the bound intake, the groups are created by the same
kernel and the same solar_panel_groups builtin the groups producer uses, from
rule G1's four parameters and nothing new, server/builtins/solar_panel_remove.py
frees exactly the panels `--remove` names, and server/builtins/solar_panel_add.py
then adds them to the one group `--to` names.

The group `--to` names is deliberately free to be a group the freed panels never
belonged to, which is what the capture did (handle 93E8 left the 555-panel group
and joined Group 6): a committed state the grouping kernel would never produce is
what makes the receipt prove the ADD rather than re-prove panel-group-create.

The provenance records which panels were freed, which group received them and the
group sizes the run ended with, so the receipt shows the membership that moved.

The groups capability carries no string sizing, so the groups builtin's sizing
recheck is bypassed for the grouping step exactly as the groups producer bypasses
it, and the metadata lists settings/string_sizing as synthetic.
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
from solar_design_graph import deserialize_graph, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph

PRODUCER = "solar_w1_studio_panel_add.v1"
MAX_CLAIMED = 4096


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The grouping half of this run IS the groups producer and the freeing half IS the
# panel-remove producer: same kernel, same entity identities, same builtin commits.
# Only the claim and the metadata are new here.
groups = _sibling("solar_w1_studio_groups")
removes = _sibling("solar_w1_studio_panel_remove")
solve = groups.solve
kernel = groups.kernel
builtin = groups.builtin
sha256 = groups.sha256
read_json = groups.read_json
normalized_handle = groups.normalized_handle
fixture_revision = groups.fixture_revision
group_neutral_id = groups.group_neutral_id
removal_plan = removes.removal_plan
# Rule G8, unchanged by the claim: the evidence references exactly the groups that
# exist and the panels their membership names. A claimed panel is back inside a
# group, so it is mapped again, from the receiving group rather than its old one.
evidence_mapping = removes.evidence_mapping
SCHEMA = groups.SCHEMA
INSTALLATION_DESIGNS = groups.INSTALLATION_DESIGNS


def commit_groups(intake, intake_hash, fixture_hash, args, design, created_at):
    """The committed grouped state the plugin's claim starts from, rule G1 parameters."""
    kernel_panels = kernel.panels_from_intake(intake, layer_contains=args.layer_contains,
                                              installation_design=design)
    if not kernel_panels:
        raise ProducerError("intake has no panel polylines on the filtered layer")
    kernel_groups = kernel.group_panels(
        kernel_panels, branch_max_offset=args.branch_max_offset,
        alignment_tolerance=args.alignment_tolerance, installation_design=design)
    graph = new_empty_graph(
        tenant_id="studio-panel-add", drawing_id="w1-" + fixture_hash[:16],
        source_hash=intake_hash,
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = groups.import_panels(graph, intake, kernel_panels, created_at)
    graph["settings"]["extra"]["string_sizing"] = deepcopy(groups.UNSIZED_SIZING)
    graph = validate_graph(graph)
    specifications, frames = groups.group_frames(graph, kernel_groups, by_handle, dimensions, created_at)

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
    return grouped


def target_handle(requested):
    """The handle inside the group's neutral id, from "group:<handle>" or the bare handle."""
    text = requested.strip() if isinstance(requested, str) else requested
    if isinstance(text, str) and text.lower().startswith("group:"):
        text = text[len("group:"):]
    return normalized_handle(text)


def target_frame(grouped, handles, requested):
    """The one group `--to` names, resolved by rule G3's neutral id on the grouped state.

    One pass over the frames: a group is named by the neutral id it carries at the
    moment it is grouped, which is the id the operator reads off the evidence.
    """
    neutral = "group:" + target_handle(requested)
    matches = [frame for frame in grouped["frames"]
               if group_neutral_id([handles[ref] for ref in frame["panel_refs"]]) == neutral]
    if len(matches) != 1:
        raise ProducerError("add names a group absent from the grouped drawing")
    return matches[0], neutral


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
    requested = args.remove
    if not isinstance(requested, list) or not 1 <= len(requested) <= MAX_CLAIMED:
        raise ProducerError("remove requires at least one panel handle")
    fixture = args.fixture.resolve()
    fixture_hash = sha256(fixture.read_bytes())
    intake, intake_hash = read_json(args.intake)
    if not isinstance(intake, dict) or intake.get("source", {}).get("dwg_sha256") != fixture_hash:
        raise ProducerError("fixture hash mismatch with intake")
    revision = fixture_revision(fixture)
    if not isinstance(intake.get("polylines"), list) or not intake["polylines"]:
        raise ProducerError("intake requires panel polylines")
    created_at = datetime.now(timezone.utc).isoformat()
    grouped = commit_groups(intake, intake_hash, fixture_hash, args, design, created_at)
    handles = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in grouped["panels"]}
    per_frame, freed_ids = removal_plan(grouped, requested)
    target, requested_group = target_frame(grouped, handles, args.to)
    target_id = target["id"]

    remover = builtin("solar_panel_remove")
    graph = grouped
    for frame_id, refs in per_frame.items():
        graph = remover.remove_panels(graph, {
            "expected_rev": graph["rev"], "frame_ref": frame_id, "panel_refs": refs})["graph"]
    freed = {frame["id"]: frame for frame in graph["frames"]}
    group_before = group_neutral_id([handles[ref] for ref in freed[target_id]["panel_refs"]])
    size_before = len(freed[target_id]["panel_refs"])
    # The claim is ONE call over every freed panel, in the order the flags named them,
    # which is the selection order the command took.
    adder = builtin("solar_panel_add")
    claim = adder.add_panels(graph, {"expected_rev": graph["rev"], "frame_ref": target_id,
                                     "panel_refs": freed_ids})
    reopened = deserialize_graph(serialize_graph(claim["graph"]))
    validate_graph(reopened)
    if {panel["id"] for panel in reopened["panels"]} != set(handles):
        raise ProducerError("the claim must keep every panel it started with")
    if len(reopened["frames"]) != len(grouped["frames"]):
        raise ProducerError("the claim must leave every group in place")
    after = {frame["id"]: frame for frame in reopened["frames"]}
    if set(after) != set(freed):
        raise ProducerError("the claim must leave every group in place")
    claimed = set(freed_ids)  # hoisted: one set, never rebuilt per group
    if set(after[target_id]["panel_refs"]) != set(freed[target_id]["panel_refs"]) | claimed:
        raise ProducerError("the receiving group did not take exactly the claimed panels")
    for frame_id, before in freed.items():
        if frame_id != target_id and after[frame_id] != before:
            raise ProducerError("a group the claim did not name changed")
    if any(panel["frame_ref"] is None for panel in reopened["panels"]):
        raise ProducerError("the claim must put every freed panel back in a group")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        # Rule G1: the groups parameters, as given, and nothing new for the claim;
        # the group and the selection the command took are recorded in provenance.
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
        "fallback_fields": ["panels/producer-built-from-intake"],
        "synthetic_flagged": True,
        "provenance": {"intake_sha256": intake_hash, "kernel": "solar_panel_group_kernel",
                       "panel_count": len(reopened["panels"]), "group_count": len(reopened["frames"]),
                       # The run grouped the drawing and freed the panels first: this says
                       # how many groups it built, which panels it freed, and which group
                       # claimed them. Rule G3 derives a group's neutral id from its CURRENT
                       # members, so a claim whose handle is the lowest renames the receiving
                       # group: both ids are recorded, as is the id the flag asked for.
                       "groups_created": len(grouped["frames"]),
                       "panels_freed": len(freed_ids),
                       "freed": sorted((handles[ref] for ref in freed_ids), key=lambda h: int(h, 16)),
                       "panels_added": len(freed_ids),
                       "added": {"requested": requested_group, "group_before": group_before,
                                 "group": group_neutral_id(
                                     [handles[ref] for ref in after[target_id]["panel_refs"]]),
                                 "panels": [handles[ref] for ref in freed_ids],
                                 "size_before": size_before,
                                 "size_after": len(after[target_id]["panel_refs"])},
                       "group_sizes": sorted(len(frame["panel_refs"]) for frame in reopened["frames"])},
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
    parser.add_argument("--remove", action="append", default=[], required=True,
                        help="source handle of a panel to free from its group; repeatable")
    parser.add_argument("--to", required=True,
                        help="the receiving group's neutral id, group:<handle> or that handle")
    args = parser.parse_args(argv)
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object or an intake body.
        # The sibling producers raise their OWN ProducerError classes; every one of
        # those messages is bounded too, so they pass through unchanged.
        bounded = isinstance(exc, (ProducerError, groups.ProducerError, removes.ProducerError,
                                   solve.ProducerError, kernel.PanelGroupKernelError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-panel-add: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
