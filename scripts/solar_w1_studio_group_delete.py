#!/usr/bin/env python3
"""Produce Studio's committed panel-group delete: group the drawing, then delete every group.

PanelGroupDeleteAll is drawing-wide, so a run that never grouped anything would
leave exactly the same empty graph as a run that deleted eleven groups. This
producer therefore starts from the same committed state the plugin started from:
the panels come from the bound intake, the groups are created by the same kernel
and the same solar_panel_groups builtin the groups producer uses, from rule G1's
four parameters and nothing new, and server/builtins/solar_panel_group_delete.py
then removes every one of them. The provenance records how many groups were
created and how many were deleted, so the receipt shows the run did not skip
grouping.

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

PRODUCER = "solar_w1_studio_group_delete.v1"


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The grouping half of this run IS the groups producer: same kernel, same entity
# identities, same builtin commit. Only the delete and the metadata are new here.
groups = _sibling("solar_w1_studio_groups")
solve = groups.solve
kernel = groups.kernel
builtin = groups.builtin
sha256 = groups.sha256
read_json = groups.read_json
normalized_handle = groups.normalized_handle
fixture_revision = groups.fixture_revision
group_neutral_id = groups.group_neutral_id
SCHEMA = groups.SCHEMA
INSTALLATION_DESIGNS = groups.INSTALLATION_DESIGNS


def evidence_mapping(graph):
    """Rule G8: exactly the groups the evidence carries and the panels their membership names.

    A panel the evidence never references is NOT mapped, so a graph the delete has
    emptied maps nothing: after PanelGroupDeleteAll the plugin's evidence references
    no entity either, and a mapping of every surviving panel would disagree with it
    for entities neither side compares. One bounded pass over the frames.
    """
    handles = {panel["id"]: normalized_handle(panel["provenance"]["source_handle"])
               for panel in graph["panels"]}
    mapping = {}
    for frame in graph["frames"]:
        members = frame["panel_refs"]
        mapping.update({ref: handles[ref] for ref in members})
        mapping[frame["id"]] = group_neutral_id([handles[ref] for ref in members])
    return mapping


def commit_groups(intake, intake_hash, fixture_hash, args, design, created_at):
    """The committed grouped state the plugin's delete starts from, rule G1 parameters."""
    kernel_panels = kernel.panels_from_intake(intake, layer_contains=args.layer_contains,
                                              installation_design=design)
    if not kernel_panels:
        raise ProducerError("intake has no panel polylines on the filtered layer")
    kernel_groups = kernel.group_panels(
        kernel_panels, branch_max_offset=args.branch_max_offset,
        alignment_tolerance=args.alignment_tolerance, installation_design=design)
    graph = new_empty_graph(
        tenant_id="studio-group-delete", drawing_id="w1-" + fixture_hash[:16],
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
    created_groups = sorted(group_neutral_id([handles[ref] for ref in frame["panel_refs"]])
                            for frame in grouped["frames"])

    remover = builtin("solar_panel_group_delete")
    removal = remover.delete_all_groups(grouped, {"expected_rev": grouped["rev"]})
    if removal["no_op"] or removal["deleted"] != len(created_groups):
        raise ProducerError("the delete did not remove every committed group")
    reopened = deserialize_graph(serialize_graph(removal["graph"]))
    validate_graph(reopened)
    if reopened["frames"] or any(panel["frame_ref"] is not None or panel["matrix_cell"] is not None
                                 for panel in reopened["panels"]):
        raise ProducerError("a deleted group survived the reopen")
    if {panel["id"] for panel in reopened["panels"]} != set(handles):
        raise ProducerError("the delete must keep every panel it started with")
    mapping = evidence_mapping(reopened)
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    if mapping:
        # Rule G8 again, as an output check: every group went, so the evidence
        # references nothing and the mapping is empty.
        raise ProducerError("a deleted group still has entities in the mapping")
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        # Rule G1: the groups parameters, as given, and nothing new for the delete.
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
                       # The run grouped the drawing first: these two say so, and
                       # separate a real delete from a run that never grouped.
                       "groups_created": len(created_groups), "groups_deleted": removal["deleted"],
                       "deleted_groups": created_groups},
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
    args = parser.parse_args(argv)
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object or an intake body.
        # The sibling producers raise their OWN ProducerError classes; every one of
        # those messages is bounded too, so they pass through unchanged.
        bounded = isinstance(exc, (ProducerError, groups.ProducerError, solve.ProducerError,
                                   kernel.PanelGroupKernelError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-group-delete: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
