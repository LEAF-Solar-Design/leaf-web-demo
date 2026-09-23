#!/usr/bin/env python3
"""Studio's G23 evidence for the report steps b3, b4, b12, b13 and b14 of the terrain fixture.

The b-steps continue from Studio's OWN state after a13 (G13): the terrain producer's t1 to t7
(scripts/solar_ground_studio_evidence.py), then the a-steps that change what these steps read,
in scenario order: a3, a4, a8, a9, a11 (scripts/solar_ground_layout_evidence.py) and a5, a6,
a7, a13 (scripts/solar_ground_scene_evidence.py). a1, a2, a10 and a12 write the slope map, a
file, the grade pad with the grading settings, and face colours: none is a LEAF-TRACKERS
polyline, a civil fence or woodland entity, the terrain grid, or a module setting. b1, b2 and
b5 to b11 belong to other slices and write no entity on the layers these steps read. Nothing
here reads plugin output. The engines are server/solar_ground_reports.py.

  b3   fence-3d-audit         LEAFFENCE3DAUDIT          report rows (read only)
  b4   mesh-diff-view         LEAFMESHDIFFVIEW          report rows (read only)
  b12  vegetation-from-civil  LEAFVEGETATIONFROMCIVIL   report rows, the status-record row
  b13  fence-mesh-generate    LEAFFENCEMESHFROMCIVIL    report rows (and `removed` for a wipe)
  b14  optimal-row-spacing    LEAFOPTIMALSPACING        report rows (read only), the boundary

Rows (family exports, G12/G17/G21/G23; every row {id, type, quantity: 1, unit: "each", ...}):
  report          id report-<name>, name (neutral), value: a length or angle quantity, or the
                  exact scalar; a value the plugin prints rounded carries that rounding (the
                  format string's, F<n> or P<n>, ties away from zero)
  status-record   name (the record's purpose, vegetation-import-last) and its scalar fields
                  by neutral name; the record's timestamp is the host's clock and is not a field
  removed         of, count (entities of an output layer the step erased)
  unexpected-change   a read-only step whose committed state changed (G23)
Rows are emitted sorted by type, then id (G9).

The report rows of each step are exactly contract G25's report table: the
values the plugin prints and its adapter parses, nothing Studio could compute beyond them.
Not reported, by G23's host rule: LEAFMESHDIFFVIEW's elapsed seconds and its model-space scan
tally (a count of every drawing-database entity, which Studio's neutral state does not model).

Parameters are the terrain producer's plus `answers` (G22: b14 ["select:LEAF-BOUNDARY"], the
others []). Fails closed: a malformed intake or state, an engine refusal, committed geometry
G23 defines no row for, or a document the comparator refuses is a named error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


layout_ev = _load("solar_ground_layout_evidence", HERE / "solar_ground_layout_evidence.py")
scene_ev = _load("solar_ground_scene_evidence", HERE / "solar_ground_scene_evidence.py")
ev = scene_ev.ev
compare = ev.compare
terrain = ev.terrain
reports = _load("solar_ground_reports", ROOT / "server" / "solar_ground_reports.py")
EvidenceError = ev.EvidenceError

KIND = "terrain"
UNIT = ev.UNIT
BOUNDARY_LAYER = "LEAF-BOUNDARY"          # G14: the terrain fixture's boundary polyline
BOUNDARY_PICK = "select:" + BOUNDARY_LAYER
# G23 scenario rows this slice owns, in order: (step id, capability, Studio operation).
B_STEPS = (("b3", "fence-3d-audit", "fence-audit"),
           ("b4", "mesh-diff-view", "mesh-diff"),
           ("b12", "vegetation-from-civil", "vegetation-import"),
           ("b13", "fence-mesh-generate", "fence-mesh"),
           ("b14", "optimal-row-spacing", "optimal-spacing"))
STEP_IDS = tuple(step for step, _, _ in B_STEPS)
ANSWERS = {"b3": (), "b4": (), "b12": (), "b13": (), "b14": (BOUNDARY_PICK,)}   # G22/G23
READ_ONLY = frozenset({"b3", "b4", "b14"})
# The a-steps whose state these steps read, in G20 scenario order, and the producer of each.
A_ORDER = ("a3", "a4", "a5", "a6", "a7", "a8", "a9", "a11", "a13")
STATUS_RECORD_VEGETATION = "vegetation-import-last"
MODULE_LAYER = ""   # the user profile's module layer; Studio configures none (PANEL still counts)


# ------------------------------------------------------------------- state --

def with_report_store(state):
    """Studio's state plus what the b-steps commit: the status records, the generated fence
    mesh faces, and the vegetation and restriction outlines. Adds missing keys in place."""
    if not isinstance(state, dict) or "grid" not in state:
        raise EvidenceError("studio state must be the terrain producer's state")
    state.setdefault("status_records", {})
    state.setdefault("fence_mesh_faces", [])
    state.setdefault("vegetation_outlines", [])
    state.setdefault("restriction_outlines", [])
    return state


def a13_state(intake):
    """Studio's own state after a13 (G13), computed from the intake."""
    state = scene_ev.terrain_state(intake)
    for step_id in A_ORDER:
        try:
            if step_id in layout_ev.LAYOUT_STEPS:
                layout_ev.step_rows(step_id, state, intake)
            else:
                scene_ev.step_rows(step_id, state, intake)
        except layout_ev.EvidenceError as exc:
            raise EvidenceError(str(exc)) from None
    return with_report_store(state)


def _neutral(entity):
    """A Studio drawing entity as the engines' neutral entity (layer, type, geometry)."""
    layer = entity.get("layer") or ""
    if entity.get("type") == "LWPOLYLINE" and isinstance(entity.get("vertices"), (list, tuple)):
        return {"type": "polyline", "layer": layer, "vertices": [list(v) for v in entity["vertices"]],
                "closed": bool(entity.get("closed", False)), "elevation": entity.get("elevation", 0.0)}
    return {"type": "other", "layer": layer}


def _polyline(layer, vertices, closed=True):
    return {"type": "polyline", "layer": layer, "vertices": [list(v) for v in vertices], "closed": closed,
            "elevation": 0.0}


def drawing_entities(state, intake):
    """Studio's model space as the traversal sees it, in creation order: the boundary, the
    terrain faces, the terrain mesh, frames, piles and markers, the tracker-row blocks (their
    block holds no civil fence or woodland geometry), setback rings, array outlines, then what
    the b-steps committed. Only layers and linear geometry matter to the engines."""
    ents = [_polyline(BOUNDARY_LAYER, intake["boundary"])]
    ents += [{"type": "face", "layer": ev.TERRAIN_LAYER}] * len(intake["terrain_faces"])
    ents += [{"type": "face", "layer": terrain.TOPO_LAYER}] * int(state.get("mesh_faces") or 0)
    for key in ("frames", "piles", "collision_layer", "slope_markers"):
        ents += [_neutral(e) for e in state.get(key) or [] if isinstance(e, dict)]
    ents += [{"type": "block", "layer": reports.LAYER_TRACKERS} for _ in state.get("tracker_rows") or []]
    ents += [_polyline(reports.SETBACK_LAYER_BY_KIND[layout_ev.SETBACK_KIND.lower()], ring)
             for ring in state.get("setback_rings") or []]
    ents += [_polyline(scene_ev.scene.ARRAY_LAYER, o["vertices"]) for o in state.get("array_outlines") or []]
    ents += [_polyline(reports.LAYER_VEGETATION, v) for v in state["vegetation_outlines"]]
    ents += [_polyline(reports.LAYER_SHADING_RESTRICTION, v) for v in state["restriction_outlines"]]
    ents += [{"type": "face", "layer": reports.LAYER_FENCE_MESH}] * len(state["fence_mesh_faces"])
    return ents


def _terrain_z(state):
    interp = terrain.terrain_interpolator(state["grid"], terrain.meters_per_unit_for_keyword(ev.UNITS_KEYWORD))
    return None if interp is None else interp.interpolate_z


def _mpu(intake):
    return ev.METERS_PER_UNIT[intake["units"]]


def _snapshot(state):
    """The committed state a read-only step could reach: all of it, as its exact repr (key
    order included, so a rebuild that reorders anything also counts as a change)."""
    return repr(state)


# -------------------------------------------------------------------- rows --

def _length(value):
    return {"kind": "length", "value": ev._finite(value, "length"), "unit": UNIT}


def _angle(value):
    return {"kind": "angle", "value": ev._finite(value, "angle"), "unit": "deg"}


def report_row(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise EvidenceError(f"report name {name!r} is not a neutral kebab-case name")
    return {"id": f"report-{name}", "type": "report", "quantity": 1, "unit": "each", "name": name, "value": value}


def status_record_row(n, name, fields):
    row = {"id": f"status-record-{n}", "type": "status-record", "quantity": 1, "unit": "each", "name": name}
    for key, value in fields.items():
        if key in row or key == "kind" or isinstance(value, (dict, list)):
            raise EvidenceError(f"status record field {key!r} is not a neutral scalar")
        # G26: hyphenated names. The plugin stores each value as invariant text (a double 1.0 is "1"),
        # so an integral float is committed as an integer.
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        row[key.replace("_", "-")] = value
    return row


def fence_audit_rows(audit):
    """LEAFFENCE3DAUDIT's printed counts the plugin adapter parses (G25; FenceSurface.cs:488-491):
    source-3D, flat, flat drapeable, generated mesh faces."""
    return [report_row("source-3d", audit["source_3d"]),
            report_row("flat", audit["flat"]),
            report_row("flat-drapeable", audit["drapable_flat"]),
            report_row("mesh-faces", audit["mesh_faces"])]


def mesh_diff_rows(result):
    """LEAFMESHDIFFVIEW's printed counts the plugin adapter parses (G25; LeafMeshDiffViewCommand.cs
    :106-109, :974-982): sampled points, PVcase block references, those skipped without column
    metadata. A run that shows no points prints no such line, so it reports nothing."""
    if not result["succeeded"]:
        return []
    return [report_row("sampled-points", result["panel_points"]),
            report_row("block-references", result["third_party_block_references"]),
            report_row("blocks-skipped", result["third_party_blocks_without_metadata"])]


def vegetation_rows(result):
    """LEAFVEGETATIONFROMCIVIL's printed counts the plugin adapter parses (G25;
    LeafShadingObjectCommand.cs:133-147) and the import status record it saves
    (VegetationMass.cs:346-398)."""
    rows = [report_row("imported-regions", result["imported_regions"]),
            report_row("source-regions", result["source_regions"]),
            report_row("height-labels", result["height_labels"]),
            report_row("mesh-faces", result["mesh_faces"])]
    rows.append(status_record_row(1, STATUS_RECORD_VEGETATION, reports.vegetation_status_record(result)))
    return rows


def fence_mesh_rows(result):
    """LEAFFENCEMESHFROMCIVIL's printed counts the plugin adapter parses (G25;
    FenceSurface.cs:528-535) and its wipe."""
    rows = [report_row("fence-items", result["source_fences"]),
            report_row("source-3d", result["source_3d"]),
            report_row("flat", result["flat"]),
            report_row("draped", result["drapable_flat"]),
            report_row("mesh-faces", result["mesh_faces"])]
    return rows + ev.removed_rows({"fence-mesh-face": result["erased"]})


def spacing_rows(settings, result):
    """LEAFOPTIMALSPACING's printed values the plugin adapter parses (G25;
    OptimalRowSpacingCommand.cs:339-361), each at its format string's precision; target-achieved
    is the word it prints (yes, or no before the widest-pitch note)."""
    f, p = reports.net_fixed, reports.net_percent
    rows = [report_row("optimal-pitch", _length(f(result["optimal_pitch_m"], 3))),
            report_row("optimal-gcr", f(result["optimal_gcr"], 3)),
            report_row("capture-percent", p(result["irradiance_fraction"], 2)),
            report_row("shading-loss-percent", f(result["annual_shade_loss_pct"], 2)),
            report_row("target-achieved", "yes" if result["target_achieved"] else "no")]
    for k, e in enumerate(result["sweep"][:reports.SPACING_TOP_K], 1):
        rows += [report_row(f"sweep-{k}-pitch", _length(f(e["pitch_m"], 3))),
                 report_row(f"sweep-{k}-gcr", f(e["gcr"], 3)),
                 report_row(f"sweep-{k}-capture-percent", p(e["irradiance_fraction"], 2)),
                 report_row(f"sweep-{k}-shading-loss-percent", f(e["annual_shade_loss_pct"], 2)),
                 report_row(f"sweep-{k}-rows", e["row_count"])]
    return rows


# ------------------------------------------------------------------- steps --

def step_fence_audit(state, intake):
    """b3: read only."""
    audit = reports.fence_audit(drawing_entities(state, intake), _terrain_z(state), _mpu(intake))
    return fence_audit_rows(audit)


def step_mesh_diff(state, intake):
    """b4: read only (the viewer is a window, not drawing state)."""
    grid = state["grid"] or {}
    result = reports.mesh_diff(drawing_entities(state, intake), _terrain_z(state), int(grid.get("rows", 0)),
                               int(grid.get("cols", 0)), _mpu(intake), MODULE_LAYER)
    return mesh_diff_rows(result)


def step_vegetation(state, intake):
    """b12: restriction regions Yes (the default); the status record is saved either way."""
    result = reports.vegetation_import(drawing_entities(state, intake), _terrain_z(state), _mpu(intake),
                                       create_restrictions=True)
    if result["masses"] or result["restrictions"]:
        raise EvidenceError("b12 committed vegetation outlines, for which G23 defines no row")
    state["status_records"][STATUS_RECORD_VEGETATION] = reports.vegetation_status_record(result)
    return vegetation_rows(result)


def step_fence_mesh(state, intake):
    """b13: height 2 m, ray-hit width 0.2 m (the defaults); the layer is wiped first."""
    result = reports.fence_mesh_build(drawing_entities(state, intake), _terrain_z(state), _mpu(intake),
                                      reports.DEFAULT_FENCE_HEIGHT_M, reports.DEFAULT_FENCE_WIDTH_M)
    if result["faces"]:
        raise EvidenceError("b13 committed fence mesh faces, for which G23 defines no row")
    state["fence_mesh_faces"] = []
    return fence_mesh_rows(result)


def step_spacing(state, intake):
    """b14: the boundary picked, every other prompt its default; read only."""
    out = reports.spacing_command(intake["boundary"], state.get("settings") or {})
    if "error" in out:
        raise EvidenceError(f"b14 refused: {out['error']}")
    return spacing_rows(out["settings"], out["result"])


STEPS = {"b3": step_fence_audit, "b4": step_mesh_diff, "b12": step_vegetation,
         "b13": step_fence_mesh, "b14": step_spacing}


def step_rows(step_id, studio_state, intake):
    """G23 rows for one b-step, computed from Studio's state (updated in place, G13). A
    read-only step whose state moved also emits `unexpected-change`."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = with_report_store(studio_state)
    before = _snapshot(state) if step_id in READ_ONLY else None
    try:
        rows = STEPS[step_id](state, intake)
    except reports.ReportInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None
    if before is not None and _snapshot(state) != before:
        rows.append({"id": "unexpected-change-1", "type": "unexpected-change", "quantity": 1, "unit": "each"})
    return rows


# --------------------------------------------------------------- documents --

def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def parameters_for(intake, step_id):
    """G17 parameters plus G22's `answers`."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]))


def build_document(intake, step_id, capability, operation, rows, revision):
    """One G23 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    rows = sorted(rows, key=_row_order)
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise EvidenceError(f"step {step_id} emitted a duplicate row id")
    parameters = parameters_for(intake, step_id)
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": step_id, "format": ev.FORMAT}
    try:
        fixture = compare.semantic_hash(intake)
        hashes = (compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
                  compare.semantic_hash(after))
    except compare.InputError as exc:
        raise EvidenceError(f"step {step_id} evidence refused by the comparator: {exc}") from None
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": hashes[0],
        "output_sha256": hashes[1],
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": ev.CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_reports
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(ev.FRAME)),
        "entity_mapping": {ref: ref for ref in sorted(ids)},  # G8
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": {"side": "studio", "fixture_kind": KIND, "step": step_id, "capability": capability,
                       "operation": operation},
        "elapsed_ms": 0,
        "execution_mode": "live",
        "state": "committed",
        "survived_reopen": True,
        "synthetic_fields": ["before/recorded", "changes/unrecorded"],
        "fallback_fields": [],
        "synthetic_flagged": True,
    }
    try:
        compare.validate_evidence(doc, "exports")
        compare._normalize(doc["after"], doc["entity_mapping"])
    except compare.InputError as exc:
        raise EvidenceError(f"step {step_id} evidence refused by the comparator: {exc}") from None
    if len(ev._serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {step_id} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run_steps(intake, revision, only=None, state=None):
    """b3, b4, b12, b13, b14 in order from Studio's a13 state (or `state`); returns
    ({step id: document}, state) for every step, or only `only` (its predecessors still run:
    they are its state)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = a13_state(intake) if state is None else with_report_store(state)
    out = {}
    for step_id, capability, operation in B_STEPS:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, capability, operation, rows, revision)
        if step_id == only:
            break
    return out, state


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G23 report evidence (b3, b4, b12, b13, b14) "
                                                 "from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=STEP_IDS)
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs, _ = run_steps(intake, ev.fixture_revision(args.intake), args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-reports-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
