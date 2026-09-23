#!/usr/bin/env python3
"""Studio's G20 evidence for the array and scene steps (a5, a6, a7, a13) of the terrain fixture.

The a-steps continue from Studio's OWN state after t7 (G13): that state is computed from the
G11 intake by the terrain producer's own steps (scripts/solar_ground_studio_evidence.py), and
the arrays and the scene come from Studio's engine (server/solar_ground_scene.py). Nothing
here reads plugin output. The a-steps between them (a1 to a4) and after a7 (a8 to a12) belong
to other slices; none of them writes the array store or the terrain grid, the only state these
four steps read.

  a5  define-array         LEAFDEFINEARRAY, centre 250,250, every other prompt its default
  a6  list-arrays          LEAFLISTARRAYS (read only)
  a7  export-pvsyst-scene  LEAFEXPORTSCENE
  a13 delete-array         LEAFDELETEARRAY, array_0

Rows (family exports, G12/G17/G20; every row {id, type, quantity: 1, unit: "each", ...}):
  array   key, centre (point), modules_x, modules_y, orientation, module_w, module_h,
          spacing_x, spacing_y (lengths), tilt, azimuth (angles, deg), size_x, size_y
          (lengths), outline (four points, G12 rotation); ids array-<n> ordered by key
  file    role (scene-dae | scene-pvc), text (LF line ends, no BOM, the DAE's <created> and
          <modified> contents emptied), lines; ids file-<n> ordered by role
  removed of "array", count
  unexpected-change   a read-only step whose committed state changed (G20, a6)
a5 emits the array it adds, a6 the arrays it reports, a7 the two files, a13 the removal.
Parameters are the terrain producer's plus `answers`, the step's ordered non-default answers.

Fails closed: a malformed intake or state, an unknown step, a file text over the
comparator's string bound, or a document the comparator refuses is a named error.
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


ev = _load("solar_ground_studio_evidence", HERE / "solar_ground_studio_evidence.py")
compare = ev.compare
terrain = ev.terrain
scene = _load("solar_ground_scene", ROOT / "server" / "solar_ground_scene.py")
EvidenceError = ev.EvidenceError

KIND = "terrain"
# G20 scenario rows this slice owns, in order: (step id, capability, Studio operation).
A_STEPS = (("a5", "define-array", "array-define"),
           ("a6", "list-arrays", "array-list"),
           ("a7", "export-pvsyst-scene", "scene-export"),
           ("a13", "delete-array", "array-delete"))
STEP_IDS = tuple(step for step, _, _ in A_STEPS)
# G20: the ordered non-default answers per step, as typed at the prompts.
ANSWERS = {"a5": ("250", "250"), "a6": (), "a7": (), "a13": ("array_0",)}
# The LEAFDEFINEARRAY prompts a5's answers fill, in prompt order (the centre X and Y).
DEFINE_ANSWERED = ("centre_x", "centre_y")
FILE_ROLES = ("scene-dae", "scene-pvc")
MAX_FILE_TEXT = 16384            # the comparator's string bound (solar_w1_compare._bounded)
ANGLE_UNIT = "deg"
_STAMP = re.compile(r"<(created|modified)>[^<]*</\1>|<(created|modified) />")


# ------------------------------------------------------------------- state --

def with_array_store(state):
    """Studio's state plus the array store: records, LEAF-ARRAY outlines, project export
    settings (None = the plugin's defaults) and the last scene written. Adds missing keys in place."""
    if not isinstance(state, dict) or "grid" not in state:
        raise EvidenceError("studio state must be the terrain producer's state")
    state.setdefault("arrays", [])
    state.setdefault("array_outlines", [])
    state.setdefault("export_settings", None)
    state.setdefault("dsm", None)
    state.setdefault("last_scene", None)
    return state


def terrain_state(intake, through="t7"):
    """Studio's own state after terrain step `through`, computed from the intake by the terrain
    producer's steps in G14 order (G13). The scene reads only the grid, which t1 commits."""
    ev.validate_intake(intake, KIND)
    steps = ev.SCENARIOS[KIND]
    if through not in {step for step, _, _ in steps}:
        raise EvidenceError(f"step {through!r} is not in the {KIND} scenario list")
    preset, pile_store = ev.load_stores(intake)
    ctx = {"intake": intake, "preset": preset, "pile_store": pile_store,
           "mpu": ev.METERS_PER_UNIT[intake["units"]]}
    state = ev.new_state()
    for step_id, _, operation in steps:
        try:
            ev.STEPS[operation](state, ctx)
        except (ev.frames.GroundFramesError, terrain.TerrainInputError) as exc:
            raise EvidenceError(f"step {step_id} ({operation}) refused: {exc}") from None
        if step_id == through:
            break
    return with_array_store(state)


def _snapshot(state):
    """The committed state a read-only array command could reach: the store and its outlines."""
    return json.dumps({"arrays": state["arrays"], "outlines": state["array_outlines"],
                       "settings": state["export_settings"]}, sort_keys=True)


# -------------------------------------------------------------------- rows --

def _angle(value):
    return {"kind": "angle", "value": ev._finite(value, "angle"), "unit": ANGLE_UNIT}


def array_rows(records, outlines):
    """G20 `array` rows, ids array-<n> in key order; the outline is the first LEAF-ARRAY
    polyline carrying the key (an empty list when the drawing has none)."""
    rows = []
    for n, r in enumerate(sorted(records, key=lambda rec: rec["key"]), 1):
        outline = next((o["vertices"] for o in outlines if o["key"] == r["key"]), None)
        rows.append({"id": f"array-{n}", "type": "array", "quantity": 1, "unit": "each",
                     "key": r["key"], "centre": ev.point((r["centre_x"], r["centre_y"])),
                     "modules_x": r["modules_x"], "modules_y": r["modules_y"],
                     "orientation": r["orientation"],
                     "module_w": ev._length(r["module_width_m"]), "module_h": ev._length(r["module_height_m"]),
                     "spacing_x": ev._length(r["module_x_spacing_m"]),
                     "spacing_y": ev._length(r["module_y_spacing_m"]),
                     "tilt": _angle(r["tilt_deg"]), "azimuth": _angle(r["azimuth_deg"]),
                     "size_x": ev._length(2 * r["half_x"]), "size_y": ev._length(2 * r["half_y"]),
                     "outline": [] if outline is None else
                     [ev.point(p) for p in ev.canonical_corners(outline)]})
    return rows


def normalize_file_text(text, role):
    """G20: LF line ends, a leading UTF-8 BOM removed, and in the DAE the <created> and
    <modified> contents replaced by the empty string."""
    if not isinstance(text, str) or role not in FILE_ROLES:
        raise EvidenceError(f"a file row takes text and one of the roles {FILE_ROLES}")
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if role == "scene-dae":
        text = _STAMP.sub(lambda m: "<{0}></{0}>".format(m.group(1) or m.group(2)), text)
    return text


def line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def file_rows(files):
    """files: {role: text as written}. One `file` row per role, ids file-<n> in role order."""
    rows = []
    for n, role in enumerate(sorted(files), 1):
        text = normalize_file_text(files[role], role)
        if len(text) > MAX_FILE_TEXT:
            raise EvidenceError(f"the {role} text is {len(text)} characters; a row holds at most {MAX_FILE_TEXT}")
        rows.append({"id": f"file-{n}", "type": "file", "quantity": 1, "unit": "each",
                     "role": role, "text": text, "lines": line_count(text)})
    return rows


def _answer_number(text, what):
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise EvidenceError(f"{what} answer {text!r} is not a number") from None
    return ev._finite(value, what)


# ------------------------------------------------------------------- steps --

def step_define(state, answers):
    """a5, LEAFDEFINEARRAY: the answered prompts take the answers, the rest their defaults."""
    if len(answers) > len(DEFINE_ANSWERED):
        raise EvidenceError("a5 answers more prompts than it names")
    given = {name: _answer_number(a, name) for name, a in zip(DEFINE_ANSWERED, answers)}
    record = scene.define_array(state["arrays"], given, state["export_settings"])
    if record is None:
        return []
    outline = {"key": record["key"], "vertices": scene.array_outline(record)}
    state["arrays"] = state["arrays"] + [record]
    state["array_outlines"] = state["array_outlines"] + [outline]
    return array_rows([record], state["array_outlines"])


def step_list(state, answers):
    """a6, LEAFLISTARRAYS: the arrays it reports, and `unexpected-change` if the state moved."""
    before = _snapshot(state)
    records = scene.list_arrays(state["arrays"])
    scene.list_report(state["arrays"])
    rows = array_rows(records, state["array_outlines"])
    if _snapshot(state) != before:
        rows.append({"id": "unexpected-change-1", "type": "unexpected-change", "quantity": 1, "unit": "each"})
    return rows


def step_export(state, answers):
    """a7, LEAFEXPORTSCENE: the DAE and PVC from Studio's grid, DSM (none in this fixture) and arrays."""
    mpu = terrain.meters_per_unit_for_keyword(ev.UNITS_KEYWORD)
    dtm = terrain.terrain_interpolator(state["grid"], mpu)
    dsm = terrain.terrain_interpolator(state["dsm"], mpu) if state["dsm"] is not None else None
    result = scene.export_scene(dtm, state["arrays"], dsm_grid=dsm)
    if not result["succeeded"]:
        return []
    state["last_scene"] = {"scene-dae": result["dae"], "scene-pvc": result["pvc"]}
    return file_rows(state["last_scene"])


def step_delete(state, answers):
    """a13, LEAFDELETEARRAY: the answered key; a removal is one `removed` array."""
    result = scene.delete_array(state["arrays"], state["array_outlines"], answers[0] if answers else "")
    state["arrays"] = result["records"]
    state["array_outlines"] = result["outlines"]
    return ev.removed_rows({"array": 1}) if result["status"] == "removed" else []


STEPS = {"a5": step_define, "a6": step_list, "a7": step_export, "a13": step_delete}


def step_rows(step_id, studio_state, intake):
    """G20 rows for one a-step, computed from Studio's state (updated in place, G13)."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = with_array_store(studio_state)
    try:
        return STEPS[step_id](state, ANSWERS[step_id])
    except scene.SceneInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None


# --------------------------------------------------------------- documents --

def parameters_for(intake, step_id):
    """G17 parameters plus G20's `answers`."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]))


def build_document(intake, step_id, capability, operation, rows, revision):
    """The terrain producer's document with the a-step's parameters and input hash."""
    doc = ev.build_document(intake, KIND, step_id, capability, operation, rows, revision)
    doc["parameters"] = parameters_for(intake, step_id)
    try:
        doc["input_sha256"] = compare.semantic_hash({"fixture_sha256": doc["fixture_sha256"],
                                                     "parameters": doc["parameters"]})
        compare.validate_evidence(doc, "exports")
    except compare.InputError as exc:
        raise EvidenceError(f"step {step_id} evidence refused by the comparator: {exc}") from None
    return doc


def run_steps(intake, revision, only=None, state=None):
    """a5, a6, a7, a13 in order from Studio's t7 state (or `state`); {step id: document} for
    every step, or only `only` (its predecessors still run: they are its state)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    state = terrain_state(intake) if state is None else with_array_store(state)
    out = {}
    for step_id, capability, operation in A_STEPS:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, capability, operation, rows, revision)
        if step_id == only:
            break
    return out, state


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G20 array and scene evidence from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--scene-dir", type=Path, help="also write the a7 DAE and PVC here, as written")
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs, state = run_steps(intake, ev.fixture_revision(args.intake), args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
        if args.scene_dir is not None and state["last_scene"] is not None:
            args.scene_dir.mkdir(parents=True, exist_ok=True)
            for role, suffix in (("scene-dae", ".dae"), ("scene-pvc", ".pvc")):
                # newline="" keeps the writer's CRLF; UTF-8 without a BOM, as the plugin writes.
                with open(args.scene_dir / f"studio-pvsyst-scene{suffix}", "w", encoding="utf-8", newline="") as fh:
                    fh.write(state["last_scene"][role])
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-scene-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
