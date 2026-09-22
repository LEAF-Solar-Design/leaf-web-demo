#!/usr/bin/env python3
"""Produce Studio's W1 committed panel groups from its own grouping kernel.

The panels come from the bound intake, selected and grouped by
server/solar_panel_group_kernel.py (the pure port of the plugin's
PanelGroupCreate). Each kernel group is committed as one frame through the
existing solar_panel_groups builtin, with the kernel's own matrix supplied as
the licensed matrix, so the graph takes the builtin's validation and the same
serialize/deserialize reopen path as every other Studio mutation.

The groups capability carries no string sizing, so the builtin's sizing recheck
is bypassed for this run and the settings record says so; the metadata lists
settings/string_sizing as synthetic.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from mutation_plan import plan_sha256
import solar_panel_group_kernel as kernel
from solar_design_graph import deserialize_graph, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph

PRODUCER = "solar_w1_studio_groups.v1"
SCHEMA = "leaf.solar-w1-comparison.v1"
# The Studio frame schema carries no other installation design (const "Roof").
INSTALLATION_DESIGNS = ("Roof",)
# panel-group-create has no string sizing input: the builtin's sizing recheck is
# bypassed for this run, this record takes the place of a real one, and the
# metadata lists settings/string_sizing as synthetic.
UNSIZED_SIZING = {"mode": "global", "records": {}, "synthetic": True,
                  "reason": "panel-group-create carries no string sizing"}


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Shared with the solve producer: fixture hash binding, git revision, builtin loading.
solve = _sibling("solar_w1_studio_solve")
builtin = solve.builtin
sha256 = solve.sha256
read_json = solve.read_json
normalized_handle = solve.normalized_handle
fixture_revision = solve.fixture_revision


def handle_value(neutral):
    return int(neutral, 16)


def group_neutral_id(neutral_handles):
    """Rule G3: "group:" plus the member neutral id with the smallest hex value."""
    return "group:" + min(neutral_handles, key=handle_value)


def frame_name(neutral_id):
    # Rule G6: Studio's own label follows the neutral id. The mutation contract
    # refuses ":" in a group name, so the frame carries "group-<handle>".
    return neutral_id.replace(":", "-")


def entity(kind, identity, graph, created_at, **fields):
    # Stable ids: the same intake and identity give the same entity id.
    raw = hashlib.sha256((graph["source_hash"] + ":" + kind + ":" + identity).encode()).digest()[:16]
    return {
        "id": f"leaf:{kind}:{UUID(bytes=raw, version=4)}", "kind": kind,
        "rev": graph["rev"], "extra": {}, "validity": {"state": "valid", "reasons": []},
        "provenance": {"created_by": PRODUCER, "created_at": created_at,
                       "last_writer": PRODUCER, "source_rev": graph["rev"],
                       "source_hash": graph["source_hash"]}, **fields,
    }


def import_panels(graph, intake, kernel_panels, created_at):
    """Graph panels for exactly the polylines the kernel selected, built as the solve producer builds them."""
    selected = {}
    for panel in kernel_panels:
        handle = normalized_handle(panel["handle"])
        if handle in selected:
            raise ProducerError("invalid or duplicate panel polyline")
        selected[handle] = panel
    by_handle, dimensions = {}, {}
    for polyline in intake["polylines"]:
        if not isinstance(polyline, dict) or not isinstance(polyline.get("handle"), str):
            continue
        handle = normalized_handle(polyline["handle"])
        if handle not in selected or handle in by_handle:
            continue
        points = polyline["pts"]
        if (not isinstance(points, list) or len(points) < 3
                or any(not isinstance(p, list) or len(p) < 2
                       or any(type(v) not in (int, float) or not math.isfinite(v)
                              for v in p[:2]) for p in points)):
            raise ProducerError("invalid or duplicate panel polyline")
        dx, dy = points[1][0] - points[0][0], points[1][1] - points[0][1]
        if not math.hypot(dx, dy) or not math.dist(points[1][:2], points[2][:2]):
            raise ProducerError("panel polyline has a degenerate edge")
        panel = entity(
            "panel", handle, graph, created_at, frame_ref=None, matrix_cell=None,
            centre=[sum(p[i] for p in points) / len(points) for i in (0, 1)],
            angle=math.degrees(math.atan2(dy, dx)), assignment={"string_ref": None, "seq": None})
        panel["provenance"]["source_handle"] = polyline["handle"]
        by_handle[handle] = panel
        # The plugin's ColumnDimension runs along the row, RowDimension across it.
        dimensions[panel["id"]] = (selected[handle]["column_dim"], selected[handle]["row_dim"])
        graph["panels"].append(panel)
    if set(by_handle) != set(selected):
        raise ProducerError("kernel selected a panel absent from intake")
    return by_handle, dimensions


def group_frames(graph, kernel_groups, by_handle, dimensions, created_at):
    """One builtin group request and one frame per kernel group, matrix from the kernel."""
    groups, frames = [], []
    if not kernel_groups:
        raise ProducerError("kernel produced no groups")
    for source in kernel_groups:
        members = []
        for handle in source["members"]:
            panel = by_handle.get(normalized_handle(handle))
            if panel is None:
                raise ProducerError("kernel group references a panel absent from intake")
            members.append(panel)
        if len(members) < 2:
            raise ProducerError("kernel group has fewer than two panels; the groups builtin cannot commit it")
        refs = [panel["id"] for panel in members]
        matrix, cell_refs = [], set()
        for row in source["matrix"]:
            cells = []
            for handle in row:
                panel = None if handle is None else by_handle.get(normalized_handle(handle))
                if handle is not None and panel is None:
                    raise ProducerError("kernel matrix references a panel absent from intake")
                if panel:
                    cell_refs.add(panel["id"])
                cells.append({
                    "code": "panel" if panel else "empty", "panel_ref": panel["id"] if panel else None,
                    "seq": None, "inverter_id": None, "string_input_number": None,
                    "x": panel["centre"][0] if panel else 0.0,
                    "y": panel["centre"][1] if panel else 0.0,
                    "angle": panel["angle"] if panel else 0.0,
                })
            matrix.append(cells)
        if not matrix or not matrix[0] or any(len(row) != len(matrix[0]) for row in matrix):
            raise ProducerError("kernel matrix must be nonempty and rectangular")
        if cell_refs != set(refs):
            raise ProducerError("kernel matrix drops a group member (cell collision); the groups builtin cannot commit it")
        neutral = group_neutral_id([normalized_handle(p["provenance"]["source_handle"]) for p in members])
        anchor = by_handle[neutral[len("group:"):]]
        width, height = dimensions[anchor["id"]]
        name = frame_name(neutral)
        frame = entity(
            "frame", neutral, graph, created_at, name=name, insertion_point=anchor["centre"][:],
            installation_design="Roof", panel_refs=refs, module_rows=len(matrix),
            module_columns=len(matrix[0]), module_slots=len(matrix) * len(matrix[0]),
            module_power_watts=0, module_width_along_row=width, module_height_across_row=height,
            electrical_zone_ref=None, matrix=matrix, sequences=[], panel_assignments=[
                {"panel_ref": ref, "string_ref": None, "seq": None,
                 "inverter_id": None, "string_input_number": None} for ref in refs])
        frame["provenance"].update(licensed_matrix="kernel", kernel="solar_panel_group_kernel",
                                   angle_key=source["angle_key"], row_angle=source["row_angle"])
        frames.append(frame)
        groups.append({"name": name, "panel_refs": refs})
    all_refs = [ref for group in groups for ref in group["panel_refs"]]
    if len(all_refs) != len(set(all_refs)) or set(all_refs) != {p["id"] for p in graph["panels"]}:
        raise ProducerError("kernel groups must cover every selected panel exactly once")
    return groups, frames


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
    kernel_panels = kernel.panels_from_intake(intake, layer_contains=contains, installation_design=design)
    if not kernel_panels:
        raise ProducerError("intake has no panel polylines on the filtered layer")
    kernel_groups = kernel.group_panels(
        kernel_panels, branch_max_offset=args.branch_max_offset,
        alignment_tolerance=args.alignment_tolerance, installation_design=design)
    tenant = "studio-groups"
    created_at = datetime.now(timezone.utc).isoformat()
    graph = new_empty_graph(
        tenant_id=tenant, drawing_id="w1-" + fixture_hash[:16], source_hash=intake_hash,
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = import_panels(graph, intake, kernel_panels, created_at)
    graph["settings"]["extra"]["string_sizing"] = deepcopy(UNSIZED_SIZING)
    graph = validate_graph(graph)
    groups, frames = group_frames(graph, kernel_groups, by_handle, dimensions, created_at)

    def licensed_matrix(*, plan, **kwargs):
        # The kernel's matrix is Studio's own answer, not a recorded plugin placement.
        return {"plan_sha256": plan_sha256(plan), "frames": deepcopy(frames)}

    module = builtin("solar_panel_groups")
    # Scope the sizing bypass to this CLI invocation; every other builtin check runs.
    with patch.object(module, "require_sizing", lambda graph: None):
        graph = module.create_groups(graph, {
            "expected_rev": graph["rev"], "groups": groups,
        }, drawing_intake=intake, licensed_matrix=licensed_matrix)["graph"]
    reopened = deserialize_graph(serialize_graph(graph))
    validate_graph(reopened)
    if len(reopened["frames"]) != len(kernel_groups) or any(p["frame_ref"] is None for p in reopened["panels"]):
        raise ProducerError("committed groups do not cover every panel")
    mapping = {p["id"]: normalized_handle(p["provenance"]["source_handle"]) for p in reopened["panels"]}
    mapping.update({f["id"]: group_neutral_id([mapping[ref] for ref in f["panel_refs"]]) for f in reopened["frames"]})
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        # Rule G1: the four values come from the command line and are recorded as given.
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
        "fallback_fields": ["panels/producer-built-from-intake", "frames/producer-derived-dimensions",
                            "frames/module_power_watts/unrecorded"],
        "synthetic_flagged": True,
        "provenance": {"intake_sha256": intake_hash, "kernel": "solar_panel_group_kernel",
                       "panel_count": len(reopened["panels"]), "group_count": len(reopened["frames"])},
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
        # solve.ProducerError is a DIFFERENT class from this module's; the shared helpers
        # (fixture_revision, read_json) raise it and its message is bounded too.
        bounded = isinstance(exc, (ProducerError, solve.ProducerError, kernel.PanelGroupKernelError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-groups: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
