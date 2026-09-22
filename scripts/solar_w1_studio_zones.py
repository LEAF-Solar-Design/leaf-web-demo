#!/usr/bin/env python3
"""Produce Studio's W1 electrical zones, and optionally its zone-aware groups.

The plugin's run is LEAFADDZONE per zone, then LEAFZONEASSIGNPANELS per zone with
a selection window, then optionally PanelGroupCreateZoneAware. This producer takes
the same shape: each ``--zone NAME:COLOR:x0,y0,x1,y1`` creates one zone through the
solar_electrical_zones builtin and assigns the panels the window selects, in
ascending handle order, so the run reproduces the plugin's own selection rather
than a rule invented here. Zones stay disjoint because the builtin removes an
assigned panel from every other zone first, exactly as the plugin does; two
overlapping windows therefore leave each shared panel in the LAST zone named.
A ``--zone NAME:COLOR`` with no window is LEAFADDZONE alone: created, assigned
nothing, and named under provenance.zones_without_window.

With ``--group`` the pure kernel runs the ordinary grouping once per zone
(group_panels_by_zone) and each group is committed as one frame through the
existing solar_panel_groups builtin, stamped with its zone, so the graph takes
that builtin's validation (including its zone-coverage check) and the same
serialize/deserialize reopen path as every other Studio mutation. With
``--out-groups-metadata`` the run also writes groups-family metadata (rules G1
and G8), so the same graph can back a zone-aware groups receipt.

The zone capabilities carry no string sizing, so the groups builtin's sizing
recheck is bypassed for this run and the settings record says so; the metadata
lists settings/string_sizing as synthetic. The plugin's per-entity recolour on
assignment has no counterpart in the v1 graph, which carries no panel colour;
that is named in fallback_fields rather than faked.
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

PRODUCER = "solar_w1_studio_zones.v1"
SCHEMA = "leaf.solar-w1-comparison.v1"
# docs/parity/solar-ledger.json: electrical-zone-add, electrical-zone-assign-panels
# and panel-group-create-zone-aware all carry capability_version "0". The receipt
# compares this against its own --capability-version, so it is the LEDGER's
# version string and never the capability name.
CAPABILITY_VERSION = "0"
# The Studio frame schema carries no other installation design (const "Roof").
INSTALLATION_DESIGNS = ("Roof",)
MAX_ZONE_NAME = 255
MAX_COLOR_INDEX = 256


class ProducerError(ValueError):
    """A bounded, payload-free producer refusal."""


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Shared with the groups producer: fixture binding, git revision, builtin loading,
# the neutral group id and the unsized-settings record.
groups = _sibling("solar_w1_studio_groups")
solve = groups.solve
builtin = groups.builtin
sha256 = groups.sha256
read_json = groups.read_json
normalized_handle = groups.normalized_handle
fixture_revision = groups.fixture_revision
group_neutral_id = groups.group_neutral_id
frame_name = groups.frame_name
UNSIZED_SIZING = groups.UNSIZED_SIZING


def zone_neutral_id(name):
    """Rule G3 for zones: a zone's neutral id is "zone:" plus the name it was given."""
    return "zone:" + name


def parse_zone(text):
    """One ``--zone NAME:COLOR[:x0,y0,x1,y1]``, recorded exactly as it was typed.

    Without a window the zone is created and assigned nothing, which is exactly
    what LEAFADDZONE commits; the window is then None.
    """
    if not isinstance(text, str) or text.count(":") not in (1, 2):
        raise ProducerError("zone must be NAME:COLOR or NAME:COLOR:x0,y0,x1,y1")
    name, colour, *rest = text.split(":")
    if not name.strip() or len(name) > MAX_ZONE_NAME:
        raise ProducerError("zone name must be nonempty and carry no colon")
    try:
        color_index = int(colour)
    except ValueError:
        raise ProducerError("zone colour must be an integer 0 to 256") from None
    if not 0 <= color_index <= MAX_COLOR_INDEX:
        raise ProducerError("zone colour must be an integer 0 to 256")
    if not rest:
        return {"name": name, "color_index": color_index, "window": None}
    parts = rest[0].split(",")
    if len(parts) != 4:
        raise ProducerError("zone window must be x0,y0,x1,y1")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise ProducerError("zone window must be four finite numbers") from None
    if any(not math.isfinite(value) for value in values):
        raise ProducerError("zone window must be four finite numbers")
    x0, x1 = sorted((values[0], values[2]))
    y0, y1 = sorted((values[1], values[3]))
    if x0 == x1 or y0 == y1:
        raise ProducerError("zone window must have a positive area")
    return {"name": name, "color_index": color_index, "window": (x0, y0, x1, y1)}


def window_panels(kernel_panels, window):
    """AutoCAD's Window selection: a panel is selected when its whole rectangle is inside.

    Ascending handle order, which is the database order a plugin selection returns.
    """
    x0, y0, x1, y1 = window
    chosen = [panel for panel in kernel_panels
              if all(x0 <= x <= x1 and y0 <= y <= y1
                     for x, y in kernel.grown_corners(panel, 0.0))]
    return sorted(chosen, key=lambda panel: kernel.handle_sort_key(panel["handle"]))


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
    """Graph panels for exactly the polylines the kernel selected, as the solve producer builds them."""
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


def commit_zones(graph, specs, kernel_panels, by_handle):
    """Create every zone, then assign each window's panels, through the builtin only."""
    module = builtin("solar_electrical_zones")
    selections = []
    for spec in specs:
        graph = module.add_zone(graph, {
            "expected_rev": graph["rev"], "name": spec["name"],
            "color_index": spec["color_index"]})["graph"]
        if spec["window"] is None:
            # LEAFADDZONE alone: the zone is created and nothing is assigned.
            continue
        chosen = window_panels(kernel_panels, spec["window"])
        if not chosen:
            raise ProducerError("zone window selected no panel")
        refs = []
        for panel in chosen:
            graph_panel = by_handle.get(normalized_handle(panel["handle"]))
            if graph_panel is None:
                raise ProducerError("selected panel is absent from the graph")
            refs.append(graph_panel["id"])
        selections.append((spec["name"], refs))
    for name, refs in selections:
        graph = module.assign_panels(graph, {
            "expected_rev": graph["rev"], "name": name, "panel_refs": refs})["graph"]
    return graph


def zone_frames(graph, kernel_groups, by_handle, dimensions, created_at):
    """One builtin group request and one frame per zone-aware kernel group."""
    if not kernel_groups:
        raise ProducerError("zone-aware kernel produced no groups")
    zone_ids = {zone["name"]: zone["id"] for zone in graph["electrical_zones"]}
    requests, frames = [], []
    for source in kernel_groups:
        zone_ref = zone_ids.get(source["zone"])
        if zone_ref is None:
            raise ProducerError("kernel group names a zone absent from the graph")
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
            electrical_zone_ref=zone_ref, matrix=matrix, sequences=[], panel_assignments=[
                {"panel_ref": ref, "string_ref": None, "seq": None,
                 "inverter_id": None, "string_input_number": None} for ref in refs])
        frame["provenance"].update(licensed_matrix="kernel", kernel="solar_panel_group_kernel",
                                   angle_key=source["angle_key"], row_angle=source["row_angle"],
                                   electrical_zone_name=source["zone"])
        frames.append(frame)
        requests.append({"name": name, "panel_refs": refs})
    grouped = [ref for request in requests for ref in request["panel_refs"]]
    zoned = {ref for zone in graph["electrical_zones"] for ref in zone["panel_refs"]}
    if len(grouped) != len(set(grouped)) or not set(grouped) <= zoned:
        raise ProducerError("zone-aware groups must cover each zoned panel at most once")
    return requests, frames


def produce(args):
    started = time.monotonic()
    contains = args.layer_contains
    if not isinstance(contains, str) or not contains.strip() or len(contains) > 255:
        raise ProducerError("layer contains must be a nonempty layer name fragment")
    design = args.installation_design
    if design not in INSTALLATION_DESIGNS:
        raise ProducerError("installation design must be Roof: the Studio frame schema carries no other value")
    specs = [parse_zone(text) for text in (args.zone or [])]
    if not specs:
        raise ProducerError("at least one zone is required")
    names = [spec["name"].casefold() for spec in specs]
    if len(set(names)) != len(names):
        raise ProducerError("zone names must be unique")
    groups_metadata_path = getattr(args, "out_groups_metadata", None)
    if groups_metadata_path is not None and not args.group:
        raise ProducerError("groups metadata requires --group: only zone-aware grouping builds groups")
    if args.group:
        for label, value in (("branch max offset", args.branch_max_offset),
                             ("alignment tolerance", args.alignment_tolerance)):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ProducerError(f"{label} must be a positive finite number for zone-aware grouping")
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
    created_at = datetime.now(timezone.utc).isoformat()
    graph = new_empty_graph(
        tenant_id="studio-zones", drawing_id="w1-" + fixture_hash[:16], source_hash=intake_hash,
        units={"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
               "elevation_datum": "unrecorded", "crs": ""}, created_at=created_at)
    by_handle, dimensions = import_panels(graph, intake, kernel_panels, created_at)
    graph["settings"]["extra"]["string_sizing"] = deepcopy(UNSIZED_SIZING)
    graph = validate_graph(graph)
    geometry = [(p["id"], p["centre"], p["angle"]) for p in graph["panels"]]
    graph = commit_zones(graph, specs, kernel_panels, by_handle)
    kernel_groups = []
    if args.group:
        # The kernel matches on its own handle spelling, so hand a zone's members
        # back in that spelling rather than in the graph's normalized form.
        handles = {by_handle[normalized_handle(panel["handle"])]["id"]: panel["handle"]
                   for panel in kernel_panels}
        kernel_groups = kernel.group_panels_by_zone(
            kernel_panels, [{"name": zone["name"], "members": [handles[ref] for ref in zone["panel_refs"]]}
                            for zone in graph["electrical_zones"]],
            branch_max_offset=args.branch_max_offset, alignment_tolerance=args.alignment_tolerance,
            installation_design=design)
        requests, frames = zone_frames(graph, kernel_groups, by_handle, dimensions, created_at)

        def licensed_matrix(*, plan, **kwargs):
            # The kernel's matrix is Studio's own answer, not a recorded plugin placement.
            return {"plan_sha256": plan_sha256(plan), "frames": deepcopy(frames)}

        module = builtin("solar_panel_groups")
        # Scope the sizing bypass to this CLI invocation; every other builtin check runs.
        with patch.object(module, "require_sizing", lambda graph: None):
            graph = module.create_groups(graph, {
                "expected_rev": graph["rev"], "groups": requests,
            }, drawing_intake=intake, licensed_matrix=licensed_matrix)["graph"]
    reopened = deserialize_graph(serialize_graph(graph))
    validate_graph(reopened)
    if [(p["id"], p["centre"], p["angle"]) for p in reopened["panels"]] != geometry:
        raise ProducerError("zones must not move geometry")
    zones = reopened["electrical_zones"]
    if [zone["name"] for zone in zones] != [spec["name"] for spec in specs]:
        raise ProducerError("committed zones do not match the requested zones")
    members = [ref for zone in zones for ref in zone["panel_refs"]]
    if len(members) != len(set(members)):
        raise ProducerError("zones must partition the panels they name")
    # A zone created without a window is empty by design (LEAFADDZONE alone); a
    # windowed zone must still hold at least one panel after every assignment.
    if any(not zone["panel_refs"] for zone, spec in zip(zones, specs) if spec["window"] is not None):
        raise ProducerError("every committed zone must hold at least one panel")
    if len(reopened["frames"]) != len(kernel_groups):
        raise ProducerError("committed frames do not match the zone-aware groups")
    # A zones receipt references the zones and their members and nothing else, so
    # the map carries exactly those: every zone, plus every panel a zone assigned.
    # The licensed capture maps the same set and no more (2 zones plus their 215
    # assigned panels on rooftop_demo), never a panel left unassigned and never a
    # group the zone-aware run happened to build alongside them.
    handle_of = {p["id"]: normalized_handle(p["provenance"]["source_handle"])
                 for p in reopened["panels"]}
    mapping = {zone["id"]: zone_neutral_id(zone["name"]) for zone in zones}
    for ref in members:
        neutral = handle_of.get(ref)
        if neutral is None:
            raise ProducerError("a zone names a panel absent from the graph")
        mapping[ref] = neutral
    if len(set(mapping.values())) != len(mapping):
        raise ProducerError("entity mapping must be one-to-one")
    metadata = {
        "fixture_sha256": fixture_hash, "revision": revision,
        # The joint contract fixes the zones family's parameters at the family
        # alone, so both sides hash the same input; the windows and colours this
        # run was given are recorded under provenance instead.
        "parameters": {"family": "zones"},
        "versions": {"schema": SCHEMA, "producer": PRODUCER, "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin", "catalog": "none", "solver": "none"},
        "coordinate_system": "world", "geometry_units": "in", "angle_units": "deg",
        "entity_mapping": mapping, "before": {"recorded": False},
        "changes": {"created": [], "modified": [], "deleted": []}, "warnings": [], "rejected_inputs": [],
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "execution_mode": "live", "state": "committed",
        "survived_reopen": True, "synthetic_fields": ["before", "changes", "settings/string_sizing"],
        "fallback_fields": ["panels/producer-built-from-intake",
                            "zones/panel-colour/not-in-graph",
                            "zones/boundary_ref/unrecorded"],
        "synthetic_flagged": True,
        "provenance": {
            "intake_sha256": intake_hash, "kernel": "solar_panel_group_kernel",
            "panel_count": len(reopened["panels"]), "zone_count": len(zones),
            "group_count": len(reopened["frames"]),
            "layer_filter": "*" + contains + "*", "installation_design": design,
            "zone_selection": [{"name": spec["name"], "color_index": spec["color_index"],
                                "window": None if spec["window"] is None else list(spec["window"])}
                               for spec in specs],
            "zones_without_window": [spec["name"] for spec in specs if spec["window"] is None],
            "zone_aware_grouping": bool(args.group),
            "branch_max_offset": args.branch_max_offset if args.group else None,
            "alignment_tolerance": args.alignment_tolerance if args.group else None,
        },
    }
    groups_metadata = None
    if groups_metadata_path is not None:
        # A zone-aware GROUPS receipt references the groups and their members and
        # nothing else (rule G8): every frame, plus exactly the panels it grouped.
        group_mapping = {}
        for frame in reopened["frames"]:
            frame_handles = []
            for ref in frame["panel_refs"]:
                neutral = handle_of.get(ref)
                if neutral is None:
                    raise ProducerError("a group names a panel absent from the graph")
                group_mapping[ref] = neutral
                frame_handles.append(neutral)
            group_mapping[frame["id"]] = group_neutral_id(frame_handles)
        if len(set(group_mapping.values())) != len(group_mapping):
            raise ProducerError("group entity mapping must be one-to-one")
        groups_metadata = deepcopy(metadata)
        groups_metadata.update({
            # Rule G1: the four values come from the command line and are recorded as given.
            "parameters": {"family": "groups", "layer_filter": "*" + contains + "*",
                           "branch_max_offset": float(args.branch_max_offset),
                           "alignment_tolerance": float(args.alignment_tolerance),
                           "installation_design": design},
            # docs/parity/solar-ledger.json: panel-group-create-zone-aware is version "0".
            "versions": {"schema": SCHEMA, "producer": PRODUCER, "capability": CAPABILITY_VERSION,
                         "engine": "server-builtin", "catalog": "none", "solver": "none"},
            "entity_mapping": group_mapping,
            "fallback_fields": ["panels/producer-built-from-intake", "frames/producer-derived-dimensions",
                                "frames/module_power_watts/unrecorded"],
        })
    args.out_graph.write_text(serialize_graph(reopened) + "\n", encoding="utf-8")
    args.out_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True,
                                            allow_nan=False) + "\n", encoding="utf-8")
    if groups_metadata is not None:
        groups_metadata_path.write_text(json.dumps(groups_metadata, indent=2, sort_keys=True,
                                                   allow_nan=False) + "\n", encoding="utf-8")
    return reopened, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "intake", "out-graph", "out-metadata"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--zone", action="append", required=True,
                        help="NAME:COLOR[:x0,y0,x1,y1], repeatable, in the order the plugin created them;"
                             " without a window the zone is created and assigned nothing")
    parser.add_argument("--layer-contains", required=True)
    parser.add_argument("--installation-design", default="Roof")
    parser.add_argument("--group", action="store_true")
    parser.add_argument("--branch-max-offset", type=float)
    parser.add_argument("--alignment-tolerance", type=float)
    parser.add_argument("--out-groups-metadata", type=Path,
                        help="with --group, also write groups-family metadata for a zone-aware groups receipt")
    args = parser.parse_args(argv)
    try:
        produce(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError) as exc:
        # Never print an input object or an intake body. The shared helpers raise
        # the groups and solve producers' own ProducerError classes, which are
        # DIFFERENT classes from this module's and whose messages are bounded too.
        bounded = isinstance(exc, (ProducerError, groups.ProducerError, solve.ProducerError,
                                   kernel.PanelGroupKernelError))
        message = str(exc) if bounded else getattr(exc, "code", None)
        message = message or getattr(exc, "classification", "invalid producer input or output")
        print(f"solar-w1-studio-zones: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
