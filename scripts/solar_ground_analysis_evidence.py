#!/usr/bin/env python3
"""Studio's G20 evidence for the terrain analytics steps a1, a2, a10 and a12.

Contract G20 (C:/tmp/solar-parity/W1-IDENTITY-CONTRACT.md, with G8 to G19): the terrain
fixture continues from t7 in one session. This producer owns four of its steps:

  a1   slope-heatmap          LEAFSLOPE        one `slope-map` row
  a2   terrain-csv-export     LEAFTERRAINCSV   one `file` row, role terrain-csv
  a10  pad-grading            LEAFGRADE        one `grade-pad` row plus the `setting`
                                               rows the step changed
  a12  survey-terrain-import  LEAFSURVEY       one `survey-colors` row

Each step is computed from Studio's OWN state (G13): the G14 terrain chain t1 to t7
from the intake through solar_ground_studio_evidence's steps, then these steps in
order. The a3 to a9 and a11 steps between them belong to other producers and touch
nothing these four read (the terrain grid, the intake boundary, the intake terrain
faces) or the settings a10 writes, so skipping them leaves every a-step's input
unchanged. Nothing here reads plugin output.

Rows (G12/G17 conventions: one point per coordinate, lengths in m, ids <type>-<n>):
  slope-map      rows, cols, extent {min, max}, cell_colors (row-major, one ACI per cell:
                 LEAFSLOPE colours its solids by index, never by true colour)
  file           role, chunks (G21: the file's text, BOM removed, line endings LF, split
                 after line feeds into strings of at most 16,000 characters), lines
  grade-pad      boundary (points, G12 rotation), elevation (length), label (text),
                 label_at (point)
  setting        id setting-<name>, name, value (a length when the name ends in M, an
                 angle when it ends in Deg, else the exact scalar); only settings whose
                 value changed, absent read as the declared default
  survey-colors  colors (true-colour integers in the intake's terrain_faces order)
Rows are emitted sorted by (type, n), setting rows by name (G9).

The parameters are solar_ground_studio_evidence's (active_preset, units_keyword,
grid_cells_long_axis) plus G20's `answers`, the step's ordered non-default answers.

Fails closed: a malformed intake, an unknown step, an untracked intake, or a document
the frozen comparator refuses is a named error, never a partial document.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys


def g21_chunks(text, limit=16000):
    """G21: split normalized file text only after line feeds ("\\n", never the other
    str.splitlines boundaries) into strings of at most `limit` characters that concatenate
    to the text; a single line longer than `limit` refuses. One linear pass."""
    lines = text.split("\n")
    pieces = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    chunks, current, size = [], [], 0
    for piece in pieces:
        if len(piece) > limit:
            raise ValueError(f"a single line of {len(piece)} characters exceeds the G21 chunk limit {limit}")
        if size + len(piece) > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    return module


studio = sys.modules.get("solar_ground_studio_evidence") or _load(
    "solar_ground_studio_evidence", HERE / "solar_ground_studio_evidence.py")
compare = studio.compare
analysis = _load("solar_ground_analysis", ROOT / "server" / "solar_ground_analysis.py")

EvidenceError = studio.EvidenceError
CAPABILITY_VERSION = studio.CAPABILITY_VERSION
FIXTURE_KIND = "terrain"
# G20: (step id, capability, Studio operation), in scenario order. The operation names the
# plugin command the step replays: slope-map is LEAFSLOPE, terrain-csv LEAFTERRAINCSV,
# grade LEAFGRADE and survey LEAFSURVEY.
STEPS = (("a1", "slope-heatmap", "slope-map"),
         ("a2", "terrain-csv-export", "terrain-csv"),
         ("a10", "pad-grading", "grade"),
         ("a12", "survey-terrain-import", "survey"))
STEP_IDS = tuple(step for step, _, _ in STEPS)
# G20: each step's ordered non-default answers. A selection is written `select:<what was
# picked>`; every other prompt (the units prompt, the CSV path) took its default.
STEP_ANSWERS = {"a1": [], "a2": [], "a10": ["select:LEAF-BOUNDARY", "Auto"],
                "a12": ["select:LEAF-TERRAIN:all"]}
# The units prompt took its default (Meters) in every step.
UNITS_KEYWORD = None
FILE_ROLE_TERRAIN_CSV = "terrain-csv"
# G20 absent-is-default: what each setting these steps write reads as before it is
# stored (DrawingPropertiesJson.cs:364, :415; GradingMode.Slope = 0, :27).
DECLARED_DEFAULTS = {analysis.GRADING_ELEVATION_SETTING: 0.0, analysis.GRADING_MODE_SETTING: 0}
# The frozen comparator refuses any string longer than this (solar_w1_compare._bounded).
COMPARATOR_MAX_STRING = 16384
# The .NET runtime whose number formatting Studio reproduces in written files.
RUNTIME = analysis.RUNTIME_NET8


# ------------------------------------------------------------------- state --

def terrain_state(intake):
    """Studio's committed state after t7, computed from the intake through the G14
    terrain chain (G13), plus the keys these steps keep: `settings` (the drawing
    settings written so far) and `terrain_face_colors`."""
    studio.validate_intake(intake, FIXTURE_KIND)
    preset, pile_store = studio.load_stores(intake)
    ctx = {"intake": intake, "preset": preset, "pile_store": pile_store,
           "mpu": studio.METERS_PER_UNIT[intake["units"]]}
    state = studio.new_state()
    for step_id, _, operation in studio.SCENARIOS[FIXTURE_KIND]:
        try:
            studio.STEPS[operation](state, ctx)
        except (studio.frames.GroundFramesError, studio.terrain.TerrainInputError) as exc:
            raise EvidenceError(f"step {step_id} ({operation}) refused: {exc}") from None
    state["settings"] = {}
    state["terrain_face_colors"] = None
    return state


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def slope_map_row(grid, cells):
    """G20 slope-map: the terrain-mesh row's shape with each cell's LEAFSLOPE colour."""
    return _row("slope-map-1", "slope-map", rows=grid["rows"], cols=grid["cols"],
                extent=studio._extent(grid), cell_colors=[cell["color_index"] for cell in cells])


def normalize_file_text(raw):
    """G20 file text: UTF-8 with a byte order mark removed and line endings LF."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise EvidenceError("a written file is not UTF-8") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def file_row(n, role, raw):
    text = normalize_file_text(raw)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    try:
        chunks = g21_chunks(text)
    except ValueError as exc:
        raise EvidenceError(f"the {role} file is refused: {exc}") from None
    return _row(f"file-{n}", "file", role=role, chunks=chunks, lines=lines)


def grade_pad_row(n, result):
    """G20 grade-pad: the pad polyline (G12 rotation), its elevation and its label."""
    corners = studio.canonical_corners(result["pad"]["vertices"])
    return _row(f"grade-pad-{n}", "grade-pad", boundary=[studio.point(p) for p in corners],
                elevation=studio._length(result["elevation_m"]), label=result["label"]["text"],
                label_at=studio.point(result["label"]["at"]))


def setting_value(name, value):
    """G20: a length when the name ends in M, an angle when it ends in Deg, else exact."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if type(value) in (int, float):
        if name.endswith("Deg"):
            return {"kind": "angle", "value": studio._finite(value, name), "unit": "deg"}
        if name.endswith("M"):
            return studio._length(value)
    return value


def _same_setting(a, b):
    if type(a) in (int, float) and type(b) in (int, float):
        return float(a) == float(b)
    return a == b


def setting_rows(before, after):
    """One row per setting whose value changed, sorted by name; a setting absent before
    reads as its declared default, so writing the default is not a change."""
    rows = []
    for name in sorted(set(before) | set(after)):
        if name not in DECLARED_DEFAULTS:
            raise EvidenceError(f"setting {name!r} has no declared default")
        was = before.get(name, DECLARED_DEFAULTS[name])
        now = after.get(name, DECLARED_DEFAULTS[name])
        if not _same_setting(was, now):
            rows.append(_row(f"setting-{name}", "setting", name=name, value=setting_value(name, now)))
    return rows


def survey_colors_row(colors):
    return _row("survey-colors-1", "survey-colors", colors=list(colors))


# ------------------------------------------------------------------- steps --

def _mpu():
    return analysis.meters_per_unit_for_keyword(UNITS_KEYWORD)


def step_slope_map(state, intake):
    result = analysis.slope_heatmap(state["grid"], _mpu(), RUNTIME)
    if not result["succeeded"]:
        return []
    return [slope_map_row(state["grid"], result["cells"])]


def step_terrain_csv(state, intake):
    result = analysis.terrain_csv_export(state["grid"], _mpu(), RUNTIME)
    if not result["succeeded"]:
        return []
    return [file_row(1, FILE_ROLE_TERRAIN_CSV, result["bytes"])]


def step_grade(state, intake):
    # The pad is the LEAF-BOUNDARY polyline, the intake boundary in drawing order (G11).
    result = analysis.grade_pad(state["grid"], intake["boundary"], _mpu(), mode="Auto", runtime=RUNTIME)
    if not result["succeeded"]:
        return []
    before = dict(state["settings"])
    state["settings"].update(result["settings"])
    return [grade_pad_row(1, result)] + setting_rows(before, state["settings"])


def step_survey(state, intake):
    # Every LEAF-TERRAIN face is selected, in entity order: the intake's terrain_faces (G11).
    result = analysis.survey_colors(intake["terrain_faces"], _mpu())
    if not result["succeeded"]:
        return []
    state["terrain_face_colors"] = result["colors"]
    return [survey_colors_row(result["colors"])]


OPERATIONS = {"slope-map": step_slope_map, "terrain-csv": step_terrain_csv,
              "grade": step_grade, "survey": step_survey}


def step_rows(step_id, studio_state, intake):
    """The G20 rows of one a-step, computed from Studio's state after the step before it;
    the state is updated with what the step commits."""
    operation = next((op for step, _, op in STEPS if step == step_id), None)
    if operation is None:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    try:
        return OPERATIONS[operation](studio_state, intake)
    except analysis.AnalysisInputError as exc:
        raise EvidenceError(f"step {step_id} ({operation}) refused: {exc}") from None


# ---------------------------------------------------------------- documents --

def _row_order(row):
    _, _, tail = row["id"].rpartition("-")
    return (row["type"], 0, int(tail), "") if tail.isdigit() else (row["type"], 1, 0, row["id"])


def parameters_for(intake, step_id):
    return dict(studio.parameters_for(intake), answers=list(STEP_ANSWERS[step_id]))


def build_document(intake, step_id, rows, revision):
    """One G12 `exports` evidence document with the envelope
    solar_ground_studio_evidence.build_document writes, validated under the comparator's
    bounds."""
    capability, operation = next((cap, op) for step, cap, op in STEPS if step == step_id)
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    for row in rows:
        if row["type"] == "file" and any(len(c) > COMPARATOR_MAX_STRING for c in row["chunks"]):
            raise EvidenceError(
                f"step {step_id} evidence refused: a {row['role']} chunk exceeds "
                f"the frozen comparator's string bound of {COMPARATOR_MAX_STRING} characters")
    rows = sorted(rows, key=_row_order)
    parameters = parameters_for(intake, step_id)
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": step_id, "format": studio.FORMAT}
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_analysis
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(studio.FRAME)),
        "entity_mapping": {ref: ref for ref in sorted(row["id"] for row in rows)},
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": {"side": "studio", "fixture_kind": FIXTURE_KIND, "step": step_id,
                       "capability": capability, "operation": operation},
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
    if len(studio._serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {step_id} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run_steps(intake, only=None):
    """Every owned step's rows in scenario order from Studio's t7 state (G13);
    returns {step id: rows}, all steps, or only `only` (its predecessors still run)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    state = terrain_state(intake)
    out = {}
    for step_id in STEP_IDS:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = rows
        if step_id == only:
            break
    return out


def run_documents(intake, revision, only=None):
    """{step id: document or EvidenceError}: every step is attempted, so one refusal
    never hides the others; the caller decides what a refusal means."""
    out = {}
    for step_id, rows in run_steps(intake, only).items():
        try:
            out[step_id] = build_document(intake, step_id, rows, revision)
        except EvidenceError as exc:
            out[step_id] = exc
    return out


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G20 evidence for a1, a2, a10 and a12 from a G11 intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=STEP_IDS)
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs = run_documents(intake, studio.fixture_revision(args.intake), args.step)
        refused = [str(doc) for doc in docs.values() if isinstance(doc, EvidenceError)]
        written = {step: doc for step, doc in docs.items() if not isinstance(doc, EvidenceError)}
        if written:
            args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in written.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(studio._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(studio._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
        if refused:
            raise EvidenceError("; ".join(refused))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-analysis-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
