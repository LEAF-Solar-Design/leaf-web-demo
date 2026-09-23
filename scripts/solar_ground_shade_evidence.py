#!/usr/bin/env python3
"""Studio's G23 evidence for the shade steps (b8, b9, b10, b11) of the terrain fixture.

The b-steps continue from Studio's OWN state after a13 (G13): the terrain chain
(scripts/solar_ground_studio_evidence.py, t1 to t7) and then the a-steps that write what these
steps read, in scenario order: the layout steps a3, a4, a8, a9, a11
(scripts/solar_ground_layout_evidence.py: the drawing settings and the tracker-row blocks) and
the array steps a5, a6, a7, a13 (scripts/solar_ground_scene_evidence.py: the array store). The
analysis a-steps (a1, a2, a10, a12) and b1 to b7 belong to other slices and change none of the
state read here (the terrain grid, the LEAF-TRACKERS frames and tracker rows, the stored
torque-tube height, the array store). Everything is computed from the intake by Studio's engines
(server/solar_ground_shade.py); nothing here reads plugin output.

  b8  shade-sim                  LEAFSHADESIM
  b9  annual-shade-simulation    LEAFSHADE (every prompt its default; read only)
  b10 shade-compare              LEAFSHADECOMPARE
  b11 shade-explain              LEAFSHADEEXPLAIN at the first tracker's centre (read only)

Rows (family exports, G12/G17/G21/G23; every row {id, type, quantity: 1, unit: "each", ...}):
  shade-marker  vertices (four [x, y] points, G12 rotation), color (the true colour r<<16|g<<8|b),
                panel (the frame id whose centre the marker marks, an entity reference, else
                null); ids shade-marker-<n> in ascending (centroid y, centroid x)
  removed       of "shade-marker", count (a re-run replaces the markers it finds)
  file          role, lines, and chunks (G21); roles shade-azal-matrix, shade-sam, shade-per-panel,
                shade-csv, scene-dae, scene-pvc; ids file-<n> in role order. The per-panel table
                (one line per panel and sun angle, 4.8 MB on the terrain fixture) exceeds the
                comparator's 2 MB document bound as chunks, so its row carries `sha256`, the hex
                SHA-256 of its normalized UTF-8 text, in place of `chunks` (a proposed G23
                amendment; the plugin adapter hashes its file the same way).
  report        name, value (a length in m or an exact scalar), exactly the values the plugin
                prints and its adapter parses (G25), at the printed precision; id report-<name>:
    b8, b10   panel-samples; clearance-shift, target-clearance, raw-median-clearance (F2, when
              the panels were shifted to the target); sun-angles, ray-tests, profile, ray-step
              (F1), max-ray (F0); surface-rows, surface-cols, surface-cells (when the surface
              snapshot is built); shading-loss-percent (the cos(zenith)-weighted shade, F2),
              panels-tinted, replaced-markers (only when > 0)
    b10 also  scene-grid-rows, scene-grid-cols, scene-arrays
    b9        tracker-rows, shading-loss-percent (F1), simulated-hours
    b11       pick-distance (F1), panels, clearance-shift, target-clearance,
              raw-median-clearance, profile, sun-angles, ray-step, max-ray, blocked-angles,
              shading-loss-percent (F2)
  unexpected-change   a read-only step (b9, b11) whose committed state changed
Timings, file paths, GPU names and the CPU worker count are host quantities and never rows (G23).
Parameters are the terrain producer's plus `answers` (G23: b11 ["point:first-tracker-centre"],
the others []).

Fails closed: a malformed intake or state, an unknown step, an engine refusal, or a document the
comparator refuses is a named error, never a partial document.
"""
from __future__ import annotations

import argparse
import hashlib
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


layout_ev = _load("solar_ground_layout_evidence", HERE / "solar_ground_layout_evidence.py")
scene_ev = _load("solar_ground_scene_evidence", HERE / "solar_ground_scene_evidence.py")
ev = scene_ev.ev
compare = ev.compare
terrain = ev.terrain
scene = scene_ev.scene
shade = _load("solar_ground_shade", ROOT / "server" / "solar_ground_shade.py")
EvidenceError = ev.EvidenceError

KIND = "terrain"
# G23 scenario rows this slice owns, in order: (step id, capability, Studio operation).
B_STEPS = (("b8", "shade-sim", "shade-sim"),
           ("b9", "annual-shade-simulation", "annual-shade"),
           ("b10", "shade-compare", "shade-compare"),
           ("b11", "shade-explain", "shade-explain"))
STEP_IDS = tuple(step for step, _, _ in B_STEPS)
PICK_FIRST_TRACKER = "point:first-tracker-centre"
# G23: the ordered non-default answers per step, every value a string.
ANSWERS = {"b8": (), "b9": (), "b10": (), "b11": (PICK_FIRST_TRACKER,)}
# The a-steps whose state the b-steps read, in scenario order (G20); each belongs to the
# layout producer or the scene producer.
A_CHAIN = ("a3", "a4", "a5", "a6", "a7", "a8", "a9", "a11", "a13")
SHADE_FILE_ROLES = ("shade-azal-matrix", "shade-csv", "shade-per-panel", "shade-sam")
DIGEST_ROLES = ("shade-per-panel",)
# G24: files over this many normalized characters are carried as sha256 + chars + a 40-line head.
G24_DIGEST_THRESHOLD = 1_048_576
MAX_CHUNK = scene_ev.MAX_CHUNK
ANGLE_UNIT = "deg"
MARKER_OF = "shade-marker"


# ------------------------------------------------------------------- state --

def with_shade_state(state):
    """Studio's state plus what the shade steps keep: the heatmap markers drawn and the files
    last written. Adds missing keys in place."""
    if not isinstance(state, dict) or "grid" not in state or "frames" not in state:
        raise EvidenceError("studio state must be the terrain producer's state")
    scene_ev.with_array_store(state)
    state.setdefault("tracker_rows", [])
    state.setdefault("settings", {})
    state.setdefault("shade_heatmap", [])
    state.setdefault("shade_files", {})
    return state


def studio_state(intake):
    """Studio's own state after a13, computed from the intake (G13): the terrain chain, then the
    layout and array a-steps in scenario order."""
    ev.validate_intake(intake, KIND)
    try:
        state = layout_ev.terrain_state(intake)
        scene_ev.with_array_store(state)
        for step in A_CHAIN:
            if step in layout_ev.LAYOUT_STEPS:
                layout_ev.step_rows(step, state, intake)
            else:
                scene_ev.step_rows(step, state, intake)
    except ValueError as exc:
        raise EvidenceError(f"Studio's state after a13 is refused: {exc}") from None
    return with_shade_state(state)


def drawing_entities(state):
    """Model space as the shade engines read it, in drawing order: the LEAF-TRACKERS frame
    polylines (t3) and then the tracker-row blocks (a8, a9) with their row axes."""
    ents = [dict(f) for f in state["frames"]]
    ents += [{"type": "INSERT", "axis_start": tuple(p["axis_start"]), "axis_end": tuple(p["axis_end"])}
             for p in state["tracker_rows"]]
    return ents


def _inputs(state):
    mpu = terrain.meters_per_unit_for_keyword(ev.UNITS_KEYWORD)
    dtm = terrain.terrain_interpolator(state["grid"], mpu)
    return drawing_entities(state), dtm, mpu, state.get("settings") or {}


def _snapshot(state):
    """The committed state a read-only shade command could reach."""
    return json.dumps({"frames": state["frames"], "trackers": state["tracker_rows"],
                       "heatmap": state["shade_heatmap"], "arrays": state["arrays"],
                       "settings": state.get("settings")}, sort_keys=True, default=str)


def first_tracker_centre(entities):
    """G23 b11's answer: the centre of the first LEAF-TRACKERS polyline the capture's selection
    returned, its vertex sum divided by 4.0 as the capture's expression computes it. A
    whole-drawing selection lists the newest entity first, so that polyline is the last one in
    drawing order (the capture's explained panel is the last of its panels)."""
    polys = [e for e in entities if e.get("type") == "LWPOLYLINE" and shade._is_layer(e, shade.TRACKERS_LAYER)]
    if not polys:
        raise EvidenceError("b11 picks a tracker centre, but the drawing has no LEAF-TRACKERS polyline")
    pts = polys[-1]["vertices"]
    return (sum(p[0] for p in pts) / 4.0, sum(p[1] for p in pts) / 4.0)


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def _angle(value):
    return {"kind": "angle", "value": ev._finite(value, "angle"), "unit": ANGLE_UNIT}


def report_row(name, value):
    return _row(f"report-{name}", "report", name=name, value=value)


def shade_marker_rows(markers, state, entities):
    """G23 shade-marker rows: ids by ascending centroid (y, x); `panel` names the frame (G12 id
    among every live frame) the marker's panel sample was read from."""
    by_handle = {handle: fid for fid, _, handle in ev.frame_ids(state["frames"]) if handle}
    ordered = sorted(markers, key=lambda m: ev._key(*ev._centroid(m["vertices"])))
    rows = []
    for n, m in enumerate(ordered, 1):
        pos = m.get("entity")
        source = entities[pos] if isinstance(pos, int) and 0 <= pos < len(entities) else {}
        rows.append(_row(f"shade-marker-{n}", "shade-marker",
                         vertices=[ev.point(p) for p in ev.canonical_corners(m["vertices"])],
                         color=m["true_color"], panel=by_handle.get(source.get("handle"))))
    return rows


def normalize_text(text, role):
    """G20: LF line ends and no leading BOM (the DAE's stamps emptied by the scene producer's rule)."""
    if role in scene_ev.FILE_ROLES:
        return scene_ev.normalize_file_text(text, role)
    if role not in SHADE_FILE_ROLES or not isinstance(text, str):
        raise EvidenceError(f"a file row takes text and one of the roles {SHADE_FILE_ROLES + scene_ev.FILE_ROLES}")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


# G26: the frozen order of file roles (ids file-<n> follow it).
G26_FILE_ROLE_ORDER = ("terrain-csv", "scene-dae", "scene-pvc", "bom-csv", "shade-csv", "shade-azal-matrix", "shade-sam", "shade-per-panel")

def file_rows(files):
    """files: {role: text as written}. One `file` row per role, ids file-<n> in role order."""
    rows = []
    # G26: file rows follow the frozen role order.
    for n, role in enumerate(sorted(files, key=G26_FILE_ROLE_ORDER.index), 1):
        text = normalize_text(files[role], role)
        row = _row(f"file-{n}", "file", role=role, lines=scene_ev.line_count(text))
        if len(text) > G24_DIGEST_THRESHOLD:
            # G24: a file this large is compared by the hash of its normalized text; `head` localizes a diff.
            row["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            row["chars"] = len(text)
            row["head"] = scene_ev.g21_chunks("".join(text.splitlines(keepends=True)[:40]), MAX_CHUNK)
        else:
            try:
                row["chunks"] = scene_ev.g21_chunks(text, MAX_CHUNK)
            except ValueError as exc:
                raise EvidenceError(f"the {role} file is refused: {exc}") from None
        rows.append(row)
    return rows


def _clearance_reports(binding):
    """The auto topo-bound suffix of the panel line (LeafShadeSimCommand.cs:2204-2206, F2): only a
    shifted binding prints it; the plugin adapter parses no other form."""
    if not binding or binding["mode"] != "shifted":
        return []
    return [report_row("clearance-shift", ev._length(shade.printed(binding["shift_m"], 2))),
            report_row("target-clearance", ev._length(shade.printed(binding["target_m"], 2))),
            report_row("raw-median-clearance", ev._length(shade.printed(binding["median_clearance_m"], 2)))]


def _profile_reports(profile):
    """Profile name, angle count, step (F1) and max ray (F0), :565-569 and :210-211."""
    return [report_row("profile", profile["name"]),
            report_row("sun-angles", len(profile["angles"])),
            report_row("ray-step", ev._length(shade.printed(profile["ray_step_m"], 1))),
            report_row("max-ray", ev._length(shade.printed(profile["max_ray_m"], 0)))]


def sim_report_rows(sim):
    """What LEAFSHADESIM prints that no host decides and the plugin adapter parses (G25):
    the panel line (LeafShadeSimCommand.cs:545-547 with :2204-2206), the ray-test line
    (:564-569), the surface snapshot line (:583-586) and the completion line (:847-853)."""
    panels = len(sim["panels"])
    rows = ([report_row("panel-samples", panels)] + _clearance_reports(sim["binding"])
            + _profile_reports(sim["profile"])
            + [report_row("ray-tests", panels * len(sim["profile"]["angles"]))])
    surface = sim.get("surface")
    if surface is not None:
        rows += [report_row("surface-rows", surface["rows"]), report_row("surface-cols", surface["cols"]),
                 report_row("surface-cells", surface["cells"])]
    rows += [report_row("shading-loss-percent", shade.printed(sim["mean_shade"] * 100.0, 2)),
             report_row("panels-tinted", len(sim["markers"]))]
    if sim["cleared"] > 0:
        rows.append(report_row("replaced-markers", sim["cleared"]))
    return rows


def _unexpected(rows, before, state):
    if _snapshot(state) != before:
        rows.append(_row("unexpected-change-1", "unexpected-change"))
    return rows


# ------------------------------------------------------------------- steps --

def step_sim(state, answers):
    """b8, LEAFSHADESIM: the markers it draws (replacing any), its three exports, its reports."""
    ents, dtm, mpu, settings = _inputs(state)
    sim = shade.shade_sim(ents, dtm, mpu, settings, len(state["shade_heatmap"]))
    if not sim["succeeded"]:
        return []
    state["shade_heatmap"] = sim["markers"]
    state["shade_files"] = dict(sim["files"])
    return (shade_marker_rows(sim["markers"], state, ents) + file_rows(sim["files"])
            + sim_report_rows(sim) + ev.removed_rows({MARKER_OF: sim["cleared"]}))


def step_annual(state, answers):
    """b9, LEAFSHADE with every prompt's default: the table it writes and what it prints; it
    commits nothing to the drawing."""
    before = _snapshot(state)
    ents, _, mpu, _ = _inputs(state)
    res = shade.annual_shade(ents, mpu)
    if not res["succeeded"]:
        return _unexpected([], before, state)
    state["shade_files"] = {"shade-csv": res["csv"]}
    rows = file_rows({"shade-csv": res["csv"]}) + [                  # ShadeCommand.cs:48, :142-144
        report_row("tracker-rows", res["row_count"]),
        report_row("shading-loss-percent", shade.printed(res["annual_loss_pct"], 1)),
        report_row("simulated-hours", res["simulated_hours"])]
    return _unexpected(rows, before, state)


def step_compare(state, answers):
    """b10, LEAFSHADECOMPARE: the re-run's markers and exports, then the scene export of Studio's
    grid and arrays (none after a13) and the preview (nothing without arrays)."""
    ents, dtm, mpu, settings = _inputs(state)
    dsm = terrain.terrain_interpolator(state["dsm"], mpu) if state.get("dsm") is not None else None
    out = shade.shade_compare(ents, dtm, mpu, settings, len(state["shade_heatmap"]), state["arrays"],
                              lambda grid, records: scene.export_scene(grid, records, dsm_grid=dsm))
    sim, export = out["sim"], out["export"]
    rows, files = [], {}
    if sim["succeeded"]:
        state["shade_heatmap"] = sim["markers"]
        files.update(sim["files"])
        rows += (shade_marker_rows(sim["markers"], state, ents) + sim_report_rows(sim)
                 + ev.removed_rows({MARKER_OF: sim["cleared"]}))
    if export["succeeded"]:
        state["last_scene"] = {"scene-dae": export["dae"], "scene-pvc": export["pvc"]}
        files.update(state["last_scene"])
        rows += [report_row("scene-grid-rows", export["rows"]), report_row("scene-grid-cols", export["cols"]),
                 report_row("scene-arrays", export["array_count"])]
    state["shade_files"] = files
    return rows + file_rows(files)


def step_explain(state, answers):
    """b11, LEAFSHADEEXPLAIN at the answered point: what it prints; it commits nothing."""
    if tuple(answers) != (PICK_FIRST_TRACKER,):
        raise EvidenceError(f"b11 answers {list(answers)!r}; G23 freezes [{PICK_FIRST_TRACKER!r}]")
    before = _snapshot(state)
    ents, dtm, mpu, settings = _inputs(state)
    res = shade.shade_explain(ents, dtm, mpu, settings, first_tracker_centre(ents))
    if not res["succeeded"]:
        return _unexpected([], before, state)
    x = res["explanation"]
    # LeafShadeSimCommand.cs:200-214: the pick line, the panels line, the profile line, the result.
    rows = ([report_row("pick-distance", ev._length(shade.printed(res["pick_distance_m"], 1))),
             report_row("panels", res["panel_count"])]
            + _clearance_reports(res["binding"]) + _profile_reports(res["profile"])
            + [report_row("blocked-angles", x["blocked_angles"]),
               report_row("shading-loss-percent", shade.printed(x["weighted_shade"] * 100.0, 2))])
    return _unexpected(rows, before, state)


STEPS = {"b8": step_sim, "b9": step_annual, "b10": step_compare, "b11": step_explain}


def step_rows(step_id, studio_state_, intake):
    """G23 rows for one b-step, computed from Studio's state (updated in place, G13)."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = with_shade_state(studio_state_)
    try:
        return STEPS[step_id](state, ANSWERS[step_id])
    except (shade.ShadeInputError, scene.SceneInputError, terrain.TerrainInputError) as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None


# --------------------------------------------------------------- documents --

def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def parameters_for(intake, step_id):
    """G17 parameters plus G23's `answers`."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]))


def build_document(intake, step_id, capability, operation, rows, revision):
    """One G23 `exports` evidence document, validated under the comparator's bounds. A marker's
    `panel` is an entity reference, mapped like every row id (G8)."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    rows = sorted(rows, key=_row_order)
    parameters = parameters_for(intake, step_id)

    def _ref(row):
        out = dict(row, id={"entity_id": row["id"]})
        if row.get("panel"):
            out["panel"] = {"entity_id": row["panel"]}
        return out
    after = {"rows": [_ref(row) for row in rows], "source_revision": step_id, "format": ev.FORMAT}
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_shade
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": intake["units"],
        "frame": json.loads(json.dumps(ev.FRAME)),
        "entity_mapping": {ref: ref for ref in sorted({row["id"] for row in rows}
                                                     | {row["panel"] for row in rows if row.get("panel")})},
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
    """b8, b9, b10, b11 in order from Studio's a13 state (or `state`); {step id: document} for
    every step, or only `only` (its predecessors still run: they are its state)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    state = studio_state(intake) if state is None else with_shade_state(state)
    out = {}
    for step_id, capability, operation in B_STEPS:
        rows = step_rows(step_id, state, intake)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, capability, operation, rows, revision)
        if step_id == only:
            break
    return out, state


# --------------------------------------------------------------------- CLI --

FILE_SUFFIX = {"shade-azal-matrix": "-azal-matrix.csv", "shade-sam": "-sam.csv",
               "shade-per-panel": "-per-panel.csv", "shade-csv": "-shade.csv",
               "scene-dae": "-pvsyst-scene.dae", "scene-pvc": "-pvsyst-scene.pvc"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G23 shade evidence (b8 to b11) from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--files-dir", type=Path, help="also write each step's files here, as written")
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        revision = ev.fixture_revision(args.intake)
        state = studio_state(intake)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, capability, operation in B_STEPS:
            rows = step_rows(step_id, state, intake)
            if args.step is not None and step_id != args.step:
                continue
            doc = build_document(intake, step_id, capability, operation, rows, revision)
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
            if args.files_dir is not None and step_id in ("b8", "b9", "b10"):
                args.files_dir.mkdir(parents=True, exist_ok=True)
                for role, text in sorted(state["shade_files"].items()):
                    # newline="" keeps the writer's CRLF; a BOM the plugin writes is U+FEFF in the text.
                    with open(args.files_dir / f"studio-{step_id}{FILE_SUFFIX[role]}", "w", encoding="utf-8",
                              newline="") as fh:
                        fh.write(text)
            if step_id == args.step:
                break
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-shade-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
