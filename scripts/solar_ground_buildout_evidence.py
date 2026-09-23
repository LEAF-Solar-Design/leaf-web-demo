#!/usr/bin/env python3
"""Studio's G23 evidence for the build-out steps b1, b2, b15, b16 and b17 of the terrain fixture.

The b-steps continue from Studio's OWN state after a13 (G13): the t-steps
(scripts/solar_ground_studio_evidence.py) compute the terrain state from the G11 intake,
then the a-steps that write what these steps read run in scenario order on it: a3 and a4
(the stored module and pitch), a8 and a9 (the drawn tracker rows), a10 (the grading
settings) and a11, each through its own producer (solar_ground_layout_evidence.py,
solar_ground_analysis_evidence.py). The other a-steps (a1, a2, a5 to a7, a12, a13) write
nothing a b-step reads: a slope map, a CSV, the array store (defined, then deleted), a
scene file and survey colours. b3 to b14 and b18 belong to other slices; none of them
changes the tracker layer, the stored settings, the grid or the boundary. Nothing here
reads plugin output. The engines are server/solar_ground_buildout.py.

  b1  bill-of-materials   LEAFBOM          file bom-csv, report rows (read only)
  b2  torque-tube-3d-draw LEAFTUBE3D       tube rows
  b15 pad-grading-batch   LEAFGRADEMULTI   grade-pad rows, setting rows (Auto, the boundary)
  b16 draw-road           LEAFDRAWROAD     road-line rows (centerline 20,150 to 480,150)
  b17 road-design         LEAFROAD         road-line and label rows (centerline 250,20 to 250,280)

Rows (family exports, G12/G17/G21/G23; every row {id, type, quantity: 1, unit: "each", ...}):
  file         role bom-csv, chunks (G21: LF line ends, no BOM, split after line feeds into
               strings of at most 16,000 characters), lines
  report       id report-<name>, name, value: tube-length a length and dc-capacity-kwp a
               scalar, both at the precision the command prints; the counts exact
  unexpected-change   a read-only step whose committed state moved (b1)
  tube         bbox_min, bbox_max (3D points); ids in bbox centre (y, x, z) order
  grade-pad    boundary (points, G12 rotation), elevation (length), label (text), label_at
  setting      id setting-<name>, name, value; only settings whose value changed
  road-line    role (centerline | edge | offset | cross-section), vertices (points in
               drawing order, not rotated), bulges (one number per vertex), closed
  label        text, at (point)
Ids are <type>-<n> in ascending (centroid y, centroid x) order unless stated; rows are
emitted sorted by type, then id order (G9). Parameters are G17's plus the step's G22
`answers`.

Fails closed: a malformed intake or state, an unknown step, an engine refusal, a file
line over the G21 chunk bound or a document the comparator refuses is a named error.
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
ev = layout_ev.producer
analysis_ev = _load("solar_ground_analysis_evidence", HERE / "solar_ground_analysis_evidence.py")
bo = _load("solar_ground_buildout", ROOT / "server" / "solar_ground_buildout.py")
compare = ev.compare
layout = layout_ev.layout

EvidenceError = ev.EvidenceError
# Refusals raised through the producers this one chains (one class when they share the
# terrain producer module, as they do when loaded from here).
_CHAIN_ERRORS = tuple({ev.EvidenceError, layout_ev.EvidenceError, analysis_ev.EvidenceError})
CAPABILITY_VERSION = ev.CAPABILITY_VERSION
FORMAT = ev.FORMAT
KIND = "terrain"
UNIT = ev.UNIT
# G23 scenario rows this slice owns, in order: (step id, capability, Studio operation).
B_STEPS = (("b1", "bill-of-materials", "bom"),
           ("b2", "torque-tube-3d-draw", "tube3d"),
           ("b15", "pad-grading-batch", "grade-multi"),
           ("b16", "draw-road", "draw-road"),
           ("b17", "road-design", "road"))
STEP_IDS = tuple(step for step, _, _ in B_STEPS)
BOUNDARY_PICK = "LEAF-BOUNDARY"
# G23 (the G22 table): the ordered non-default answers per step, as typed at the prompts.
ANSWERS = {"b1": (), "b2": (), "b15": ("Auto", "select:" + BOUNDARY_PICK),
           "b16": ("20,150", "480,150"), "b17": ("250,20", "250,280")}
# The a-steps whose writes a b-step reads, in scenario order (see the module docstring).
A_CHAIN = ("a3", "a4", "a8", "a9", "a10", "a11")
# Every command's units prompt took its default, Meters.
MPU = bo._analysis.meters_per_unit_for_keyword(None)
RUNTIME = analysis_ev.RUNTIME                       # the .NET runtime of the 2025 capture host
MAX_CHUNK = 16000                                   # G21
FILE_ROLE_BOM = "bom-csv"
# The committed drawing state a step can reach (b1's read-only rule compares it).
COMMITTED_KEYS = ("frames", "tracker_rows", "settings", "grading_settings", "tubes", "grade_pads",
                  "road_lines", "road_labels", "setback_rings")


# ------------------------------------------------------------------- state --

def with_buildout_store(state):
    """Studio's state plus the build-out store: tube solids, graded pads, road lines and
    labels, the grading settings and the last BOM file written. Adds missing keys in place."""
    if not isinstance(state, dict) or "grid" not in state or "frames" not in state:
        raise EvidenceError("studio state must be the terrain producer's state")
    state.setdefault("tracker_rows", [])
    state.setdefault("grading_settings", {})
    state.setdefault("tubes", [])
    state.setdefault("grade_pads", [])
    state.setdefault("road_lines", [])
    state.setdefault("road_labels", [])
    state.setdefault("last_bom", None)
    return state


def _grade_a10(state, intake):
    """a10 through the analysis producer, on the grading settings it keeps (its setting
    rows refuse a name it does not declare, so the layout settings are set aside)."""
    layout_settings = state.get("settings")
    state["settings"] = dict(state.get("grading_settings") or {})
    try:
        analysis_ev.step_rows("a10", state, intake)
        state["grading_settings"] = state["settings"]
    except _CHAIN_ERRORS as exc:
        raise EvidenceError(f"step a10 refused: {exc}") from None
    finally:
        state["settings"] = layout_settings


def a13_state(intake):
    """Studio's own state after a13 (G13): the t-steps from the intake, then A_CHAIN."""
    try:
        state = layout_ev.terrain_state(intake)          # validates the intake, runs t1 to t7
        for step_id in A_CHAIN:
            if step_id == "a10":
                _grade_a10(state, intake)
            else:
                layout_ev.step_rows(step_id, state, intake)
    except _CHAIN_ERRORS as exc:
        raise EvidenceError(str(exc)) from None
    return with_buildout_store(state)


def _snapshot(state):
    return json.dumps({k: state.get(k) for k in COMMITTED_KEYS}, sort_keys=True, default=repr)


def tracker_entities(state):
    """The tracker layer in drawing order: the frame polylines (t3), then the drawn rows
    (a8, a9), as the engines' neutral entities."""
    frames = [{"kind": "polyline", "layer": f.get("layer"), "vertices": f["vertices"]} for f in state["frames"]]
    return frames + [dict(p, kind="tracker") for p in state["tracker_rows"]]


def stored_module(state):
    """GetActiveModule over the stored settings (BomCommand.cs ReadStoredModule)."""
    module = layout.get_active_module(layout.load_settings(state.get("settings") or {}))
    return {"cross_axis_m": module.cross_axis_m, "along_axis_m": module.along_axis_m, "pmax_w": module.pmax_w}


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def _numbered(items, key):
    """Stable order by `key`, numbered from 1."""
    return enumerate(sorted(items, key=key), 1)


def normalize_file_text(raw):
    """G20: the file's text with a UTF-8 BOM removed and line endings normalized to LF."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if not isinstance(text, str):
        raise EvidenceError("a file row takes the file's bytes or text")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def line_count(text):
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def file_row(n, role, raw):
    text = normalize_file_text(raw)
    try:
        chunks = analysis_ev.g21_chunks(text, MAX_CHUNK)
    except ValueError as exc:
        raise EvidenceError(f"the {role} file is refused: {exc}") from None
    return _row(f"file-{n}", "file", role=role, chunks=chunks, lines=line_count(text))


# G23 report names, in the order the command computes them; tube-length is a length.
REPORT_NAMES = ("tracker-sections", "module-slots", "tube-length", "estimated-piles", "drives", "dc-capacity-kwp")


def report_rows(report):
    """G23: one row per reported value. A value the command prints rounded (tube length,
    DC capacity) is its printed text read back, so both sides compare at that precision."""
    rows = []
    for name in REPORT_NAMES:
        if name not in report:
            continue
        value = report[name]
        if name == "tube-length":
            value = ev._length(float(value))
        elif name == "dc-capacity-kwp":
            value = ev._finite(float(value), name)
        rows.append(_row(f"report-{name}", "report", name=name, value=value))
    return rows


def tube_rows(solids):
    def centre(s):
        c = [(s["bbox_min"][i] + s["bbox_max"][i]) / 2.0 for i in range(3)]
        return (round(c[1], 6), round(c[0], 6), round(c[2], 6))
    return [_row(f"tube-{n}", "tube", bbox_min=ev.point(s["bbox_min"]), bbox_max=ev.point(s["bbox_max"]))
            for n, s in _numbered(solids, centre)]


def grade_pad_rows(pads):
    return [_row(f"grade-pad-{n}", "grade-pad",
                 boundary=[ev.point(p) for p in ev.canonical_corners(pad["vertices"])],
                 elevation=ev._length(pad["elevation_m"]), label=pad["label"]["text"],
                 label_at=ev.point(pad["label"]["at"]))
            for n, pad in _numbered(pads, lambda p: ev._key(*ev._centroid(p["vertices"])))]


def road_line_rows(lines):
    return [_row(f"road-line-{n}", "road-line", role=line["role"],
                 vertices=[ev.point(p) for p in line["vertices"]],
                 bulges=[ev._finite(b, "bulge") for b in line["bulges"]], closed=bool(line["closed"]))
            for n, line in _numbered(lines, lambda l: ev._key(*ev._centroid(l["vertices"])))]


def label_rows(labels):
    return [_row(f"label-{n}", "label", text=label["text"], at=ev.point(label["at"]))
            for n, label in _numbered(labels, lambda l: ev._key(*l["at"]))]


def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def _answer_point(text):
    """A picked point typed as "x,y"."""
    parts = text.split(",") if isinstance(text, str) else []
    if len(parts) != 2:
        raise EvidenceError(f"point answer {text!r} is not x,y")
    try:
        return (ev._finite(float(parts[0]), "x"), ev._finite(float(parts[1]), "y"))
    except ValueError:
        raise EvidenceError(f"point answer {text!r} is not x,y") from None


# ------------------------------------------------------------------- steps --

def step_bom(state, intake, answers):
    """b1, LEAFBOM: the CSV it writes and the summary it prints; read only."""
    before = _snapshot(state)
    result = bo.bom_command(tracker_entities(state), stored_module(state), meters_per_unit=MPU, runtime=RUNTIME)
    rows = []
    if result["succeeded"]:
        state["last_bom"] = result["csv_bytes"]
        rows = [file_row(1, FILE_ROLE_BOM, result["csv_bytes"])] + report_rows(result["report"])
    if _snapshot(state) != before:
        rows.append(_row("unexpected-change-1", "unexpected-change"))
    return rows


def step_tube(state, intake, answers):
    """b2, LEAFTUBE3D: one solid per tracker row, following Studio's grid."""
    settings = layout.load_settings(state.get("settings") or {})
    result = bo.torque_tube_command(tracker_entities(state), state["grid"], settings.get("TorqueTubeHeightM", 0.0),
                                    settings.get("TrackerTorqueTubeRadiusM", 0.0), MPU, RUNTIME)
    state["tubes"] = state["tubes"] + result["solids"]
    return tube_rows(result["solids"])


def step_grade_multi(state, intake, answers):
    """b15, LEAFGRADEMULTI: Auto for every pad; the pad is the boundary polyline."""
    if not answers or answers[1:] != ("select:" + BOUNDARY_PICK,):
        raise EvidenceError("b15 answers must be the mode and the boundary selection")
    result = bo.grade_multi(state["grid"], [intake["boundary"]], MPU, mode=answers[0], runtime=RUNTIME)
    if not result["succeeded"]:
        return []
    before = dict(state["grading_settings"])
    after = dict(before)
    after.update(result["settings"])
    state["grading_settings"] = after
    state["grade_pads"] = state["grade_pads"] + result["pads"]
    try:
        settings = analysis_ev.setting_rows(before, after)
    except _CHAIN_ERRORS as exc:
        raise EvidenceError(f"step b15 refused: {exc}") from None
    return grade_pad_rows(result["pads"]) + settings


def step_draw_road(state, intake, answers):
    """b16, LEAFDRAWROAD: every prompt at its default, a two-point centerline."""
    result = bo.draw_road([_answer_point(a) for a in answers], drawn=True, runtime=RUNTIME)
    state["road_lines"] = state["road_lines"] + result["lines"]
    return road_line_rows(result["lines"]) if result["succeeded"] else []


def step_road(state, intake, answers):
    """b17, LEAFROAD: a two-point centerline, every other prompt at its default."""
    result = bo.road_design([_answer_point(a) for a in answers], drawn=True, meters_per_unit=MPU, runtime=RUNTIME)
    state["road_lines"] = state["road_lines"] + result["lines"]
    state["road_labels"] = state["road_labels"] + result["labels"]
    if not result["succeeded"]:
        return []
    return road_line_rows(result["lines"]) + label_rows(result["labels"])


STEPS = {"b1": step_bom, "b2": step_tube, "b15": step_grade_multi, "b16": step_draw_road, "b17": step_road}


def step_rows(step_id, studio_state, intake):
    """G23 rows for one b-step, computed from Studio's state (updated in place, G13)."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    if not isinstance(intake, dict):
        raise EvidenceError("intake must be an object")
    state = with_buildout_store(studio_state)
    try:
        return STEPS[step_id](state, intake, ANSWERS[step_id])
    except bo.BuildoutInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None


# --------------------------------------------------------------- documents --

def parameters_for(intake, step_id):
    """G17 parameters plus G23's `answers`."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]))


def build_document(intake, step_id, rows, revision):
    """One G23 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    capability, operation = next((cap, op) for step, cap, op in B_STEPS if step == step_id)
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_buildout
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(ev.FRAME)),
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
    if len(ev._serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {step_id} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run_steps(intake, revision, only=None, state=None):
    """b1, b2, b15, b16, b17 in order from Studio's a13 state (or `state`); returns
    ({step id: document} for every step, or only `only`, whose predecessors still run
    because they are its state; the final state)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    if state is None:
        state = a13_state(intake)
    else:
        ev.validate_intake(intake, KIND)
        state = with_buildout_store(state)
    out = {}
    for step_id, _, _ in B_STEPS:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, rows, revision)
        if step_id == only:
            break
    return out, state


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G23 build-out evidence (b1, b2, b15, b16, b17) "
                                                 "from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--csv-dir", type=Path, help="also write the b1 BOM CSV here, byte for byte")
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
        if args.csv_dir is not None and state["last_bom"] is not None:
            args.csv_dir.mkdir(parents=True, exist_ok=True)
            (args.csv_dir / "studio-bom.csv").write_bytes(state["last_bom"])
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-buildout-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
