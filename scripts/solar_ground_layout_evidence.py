#!/usr/bin/env python3
"""Studio's ground layout evidence for the G20 steps a3, a4, a8, a9 and a11 (contract v6).

The a-steps continue the terrain fixture from Studio's OWN state after t7 (G13):
`solar_ground_studio_evidence.py` computes that state from the G11 intake, and this
producer runs the layout engines of server/solar_ground_layout.py on it:

  a3  LEAFMODULE    tracker-module-spec     setting rows (the module it saves)
  a4  LEAFSPACING   row-spacing-calculator  setting rows (the minimum pitch it saves)
  a8  LEAFTRACK     tracker-layout          tracker-row rows
  a9  LEAFSAT       sat-layout-from-gcr     tracker-row rows, setting rows
  a11 LEAFSETBACK   setback-boundary        setback-ring rows (Array, the boundary, 5)

The other a-steps belong to other capabilities; none of them changes what these
steps read (the drawing settings, the terrain grid and the boundary), so this
producer runs its own steps in scenario order on the t7 state.

Evidence, family `exports`, after = {rows, source_revision: <step id>, format:
"ground-v1"}; every row is {id, type, quantity: 1, unit: "each", ...} (G12, G17, G20):
  setting       id setting-<name>; name (the product setting name); value (a length
                when the name ends in M, an angle in deg when it ends in Deg, a nested
                object as its canonical JSON text, otherwise the exact scalar). One row
                per setting whose value changed in the step; a setting absent before
                reads as its declared default, so one first written at its default is
                not a change.
  tracker-row   block, insert ([x, y]), rotation (deg), scale_x, scale_y (lengths),
                row_index, slots, axis_start, axis_end ([x, y]), cross_axis_width
                (length), source_command, tracker_model; ids in (row_index, slots) order.
  setback-ring  setback_kind (array | fence | collection; G21), vertices ([x, y] points from the
                smallest rounded (y, x), counter-clockwise), distance (length); ids in
                ascending (centroid y, centroid x) order.
Rows are emitted sorted by type, then by id order within the type (G9).

Parameters are G17's (active_preset, units_keyword, grid_cells_long_axis) plus G20's
`answers`, the ordered non-default answers of the step.

Fails closed: a malformed intake, an unknown step, an engine refusal, a document over
the comparator's bounds or an untracked intake is refused with a named error, never a
partial document.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
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


producer = _load("solar_ground_studio_evidence", HERE / "solar_ground_studio_evidence.py")
compare = producer.compare
layout = _load("solar_ground_layout", ROOT / "server" / "solar_ground_layout.py")

EvidenceError = producer.EvidenceError
CAPABILITY_VERSION = producer.CAPABILITY_VERSION
FORMAT = producer.FORMAT
KIND = "terrain"
# Step id -> (capability, operation); the operation names the plugin command replayed.
LAYOUT_STEPS = {
    "a3": ("tracker-module-spec", "module"),
    "a4": ("row-spacing-calculator", "spacing"),
    "a8": ("tracker-layout", "track"),
    "a9": ("sat-layout-from-gcr", "sat"),
    "a11": ("setback-boundary", "setback"),
}
LAYOUT_ORDER = ("a3", "a4", "a8", "a9", "a11")
BOUNDARY_PICK = "LEAF-BOUNDARY"   # G20: the boundary answer is the LEAF-BOUNDARY polyline
SETBACK_KIND = "Array"
SETBACK_DISTANCE = 5
# G22: the frozen answers table, every value the string recorded for that prompt.
STEP_ANSWERS = {
    "a3": [],
    "a4": [],
    "a8": ["select:" + BOUNDARY_PICK],
    "a9": ["select:" + BOUNDARY_PICK],
    "a11": [SETBACK_KIND, "select:" + BOUNDARY_PICK, str(SETBACK_DISTANCE)],
}
UNIT = producer.UNIT


# ------------------------------------------------------------------- rows --

def _length(value):
    return {"kind": "length", "value": producer._finite(value, "length"), "unit": UNIT}


def _angle(value):
    return {"kind": "angle", "value": producer._finite(value, "angle"), "unit": "deg"}


def setting_value(name, value):
    """G20: length for ...M, angle for ...Deg, canonical JSON text for a nested object,
    otherwise the exact scalar."""
    if isinstance(value, (dict, list)):
        return layout.canonical_setting_text(value)
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if name.endswith("Deg"):
        return _angle(value)
    if name.endswith("M"):
        return _length(value)
    return value


def setting_rows(before, after):
    return [{"id": f"setting-{name}", "type": "setting", "quantity": 1, "unit": "each",
             "name": name, "value": setting_value(name, value)}
            for name, value in layout.changed_settings(before, after)]


def tracker_rows(placements):
    ordered = sorted(placements, key=lambda p: (p["row_index"], p["slots"]))
    rows = []
    for n, p in enumerate(ordered, 1):
        rows.append({"id": f"tracker-row-{n}", "type": "tracker-row", "quantity": 1, "unit": "each",
                     "block": p["block"], "insert": producer.point(p["insert"][:2]),
                     "rotation": _angle(math.degrees(p["rotation_rad"])),
                     "scale_x": _length(p["scale"][0]), "scale_y": _length(p["scale"][1]),
                     "row_index": p["row_index"], "slots": p["slots"],
                     "axis_start": producer.point(p["axis_start"]), "axis_end": producer.point(p["axis_end"]),
                     "cross_axis_width": _length(p["cross_axis_width_du"]),
                     "source_command": p["source_command"], "tracker_model": p["tracker_model"]})
    return rows


def setback_rows(kind, distance, rings):
    ordered = sorted(rings, key=lambda ring: producer._key(*producer._centroid(ring)))
    return [{"id": f"setback-ring-{n}", "type": "setback-ring", "quantity": 1, "unit": "each",
             "setback_kind": kind, "vertices": [producer.point(p) for p in producer.canonical_corners(ring)],
             "distance": _length(distance)}
            for n, ring in enumerate(ordered, 1)]


def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


# ------------------------------------------------------------------- steps --

def _save(state, writes):
    """A command's settings save: returns the setting rows it changes."""
    before = state.get("settings") or {}
    after = layout.save_settings(before, writes)
    state["settings"] = after
    return setting_rows(before, after)


def step_rows(step_id, studio_state, intake):
    """The G20 rows of one layout step, computed from Studio's own state (G13), which
    this updates: `settings` (the stored drawing settings, absent until first saved),
    `tracker_rows` (placements drawn so far) and `setback_rings`."""
    if step_id not in LAYOUT_STEPS:
        raise EvidenceError(f"step {step_id!r} is not a layout step ({', '.join(LAYOUT_ORDER)})")
    if not isinstance(studio_state, dict) or not isinstance(intake, dict):
        raise EvidenceError("studio_state and intake must be objects")
    boundary = intake.get("boundary")
    loaded = layout.load_settings(studio_state.get("settings") or {})
    try:
        if step_id == "a3":
            return _save(studio_state, layout.module_command(loaded))
        if step_id == "a4":
            out = layout.spacing_command(loaded)
            return _save(studio_state, out["writes"]) if out["writes"] else []
        if step_id == "a8":
            out = layout.tracker_command(boundary, loaded, studio_state.get("grid"))
            studio_state.setdefault("tracker_rows", []).extend(out["placements"])
            return tracker_rows(out["placements"])
        if step_id == "a9":
            out = layout.sat_command(boundary, loaded, studio_state.get("grid"))
            studio_state.setdefault("tracker_rows", []).extend(out["placements"])
            rows = tracker_rows(out["placements"])
            return rows + (_save(studio_state, out["writes"]) if out["writes"] else [])
        out = layout.setback_command(boundary, {"kind": SETBACK_KIND, "distance": SETBACK_DISTANCE})
        studio_state.setdefault("setback_rings", []).extend(out["rings"])
        return setback_rows(out["kind"], out["distance"], out["rings"])
    except layout.LayoutInputError as exc:
        raise EvidenceError(f"step {step_id} ({LAYOUT_STEPS[step_id][1]}) refused: {exc}") from None


def terrain_state(intake):
    """Studio's state after t7: the producer's own terrain chain, step by step (G13)."""
    producer.validate_intake(intake, KIND)
    preset, pile_store = producer.load_stores(intake)
    ctx = {"intake": intake, "preset": preset, "pile_store": pile_store,
           "mpu": producer.METERS_PER_UNIT[intake["units"]]}
    state = producer.new_state()
    for step_id, _, operation in producer.SCENARIOS[KIND]:
        try:
            producer.STEPS[operation](state, ctx)
        except (producer.frames.GroundFramesError, producer.terrain.TerrainInputError) as exc:
            raise EvidenceError(f"step {step_id} ({operation}) refused: {exc}") from None
    return state


# ---------------------------------------------------------------- documents --

def parameters_for(intake, step_id):
    return dict(producer.parameters_for(intake), answers=list(STEP_ANSWERS[step_id]))


def build_document(intake, step_id, rows, revision):
    """One G20 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    capability, operation = LAYOUT_STEPS[step_id]
    rows = sorted(rows, key=_row_order)
    parameters = parameters_for(intake, step_id)
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": step_id, "format": FORMAT}
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
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_layout
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(producer.FRAME)),
        "entity_mapping": {ref: ref for ref in sorted(row["id"] for row in rows)},  # G8
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
    if len(producer._serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {step_id} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run_layout(intake, revision, only=None, state=None):
    """Every layout step in order on Studio's t7 state; returns {step id: document}, all
    steps or only `only` (its predecessors still run: they are its state). `state` may
    carry an already computed t7 state (it is updated in place)."""
    if only is not None and only not in LAYOUT_STEPS:
        raise EvidenceError(f"step {only!r} is not a layout step ({', '.join(LAYOUT_ORDER)})")
    if state is None:
        state = terrain_state(intake)
    else:
        producer.validate_intake(intake, KIND)
    out = {}
    for step_id in LAYOUT_ORDER:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, rows, revision)
        if step_id == only:
            break
    return out


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio ground layout evidence (G20 a3, a4, a8, a9, a11).")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=LAYOUT_ORDER)
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs = run_layout(intake, producer.fixture_revision(args.intake), args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(producer._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(producer._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-layout-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
