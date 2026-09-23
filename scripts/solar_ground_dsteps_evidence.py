#!/usr/bin/env python3
"""Studio's G28 evidence for the terrain d-steps d3, d4, d6, d7 and d8.

The d-steps continue from Studio's OWN state after b18 (G13): the build-out producer's chain
(scripts/solar_ground_buildout_evidence.py: t1 to t7, then a3, a4, a8, a9, a10, a11) and its
b-steps b1, b2, b15, b16, b17, which commit what a d-step reads (the tracker layer, the stored
module, the piles, the terrain grid, the layer-0 road centerline). The other a- and b-steps
write nothing a d-step reads: a5 defines array_0 and a13 deletes it, a1, a2, a6, a7 and a12
write a slope map, files and face colours, b3 to b14 write heatmap markers, files and status
records (none on a layer the d-steps read), b18 tunes the host. Nothing here reads plugin
output. The engines are server/solar_ground_dsteps.py.

  d3  trackers-to-panelgroups  LEAFTRACKERSTOPANELGROUPS  report rows (a terminal step: its result
                                                          is not chained into d4)
  d4  trench-routing           LEAFTRENCH                 trench row (start 20,20; end 120,70)
  d5  (define-array, not receipted: it only feeds d6)    LEAFDEFINEARRAY, centre 250,250
  d6  show-export-preview      LEAFSHOWEXPORT             export-preview rows (and `removed`)
  d7  show-export-preview      LEAFHIDEEXPORT             `removed` rows
  d8  yield-export             LEAFYIELDEXPORT            file rows, one per zip entry (read only)

Rows (family exports, G12/G17/G21/G24/G28; every row {id, type, quantity: 1, unit: "each", ...}):
  trench          vertices (points in drawing order, not rotated), closed, depth and width
                  (lengths), voltage_class (str); the record's neutral field names
  export-preview  role (array-outline | clearance, the command's colour rule), vertices
                  (points, G12 rotation); ids numbered by role, then centroid (y, x)
  removed         of, count (entities of an output layer the step erased)
  file            role yield-<entry name lower-cased, dots to dashes>, in zip entry order;
                  G20 normalization, the manifest's host fields blanked (G28, listed in
                  synthetic_fields), G21 chunks or the G24 digest by size
  report          d3: panel-groups-created and panel-group-slots (ints)
  unexpected-change   d8 whose committed state moved (the G20 read-only rule)
Rows are emitted sorted by type, then id (G9). Parameters are G17's plus the G22 `answers`.

The d8 overview's first line carries the drawing's file name; Studio has none and writes the
plugin's own fallback unless --drawing-file-name gives one at run time (never committed).
Fails closed: a malformed intake or state, an engine refusal, or a document the comparator
refuses is a named error.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bev = _load("solar_ground_buildout_evidence", HERE / "solar_ground_buildout_evidence.py")
scene_ev = _load("solar_ground_scene_evidence", HERE / "solar_ground_scene_evidence.py")
ev = bev.ev
compare = ev.compare
terrain = ev.terrain
scene = scene_ev.scene
dsteps = _load("solar_ground_dsteps", ROOT / "server" / "solar_ground_dsteps.py")

KIND = "terrain"
UNIT = ev.UNIT
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain" / "intake.json"
DEFAULT_OUT_DIR = Path("C:/tmp/solar-parity/dsteps-ev/studio")
# G28 scenario rows this slice owns, in order: (step id, capability, Studio operation).
D_STEPS = (("d3", "trackers-to-panelgroups", "trackers-to-panel-groups"),
           ("d4", "trench-routing", "trench"),
           ("d6", "show-export-preview", "export-preview-show"),
           ("d7", "show-export-preview", "export-preview-hide"),
           ("d8", "yield-export", "yield-export"))
STEP_IDS = tuple(step for step, _, _ in D_STEPS)
NOT_RECEIPTED = ("d1", "d2", "d5")
# G28 (the G22 table).
ANSWERS = {"d3": (), "d4": ("20,20", "120,70"), "d5": ("250", "250"), "d6": (), "d7": (), "d8": ()}
READ_ONLY = frozenset({"d8"})
PREVIEW_OF = "export-preview"
# a11's ring layer (LEAFSETBACK Array), the layer Studio's rings are placed on (CivilLayers.cs:29-31).
SETBACK_LAYER = dsteps._reports.SETBACK_LAYER_BY_KIND[bev.layout_ev.SETBACK_KIND.lower()]
MAX_CHUNK = 16000                                   # G21
G24_DIGEST_THRESHOLD = 1_048_576                    # G24
INSUNITS_METERS = 6                                 # G11: the run copy's INSUNITS
_HOST_FIELD = re.compile(r'("(%s)": )"(?:[^"\\]|\\.)*"' % "|".join(dsteps.MANIFEST_FIELDS_HOST))
SYNTHETIC_MANIFEST_FIELDS = tuple(f"yield-manifest-json/{f}" for f in dsteps.MANIFEST_FIELDS_HOST)


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


# ------------------------------------------------------------------- state --

def with_dstep_store(state):
    """Studio's state plus what the d-steps commit: trench polylines and the export preview.
    Adds missing keys in place."""
    if not isinstance(state, dict) or "grid" not in state or "frames" not in state:
        raise EvidenceError("studio state must be the terrain producer's state")
    bev.with_buildout_store(state)
    scene_ev.with_array_store(state)
    state.setdefault("trenches", [])
    state.setdefault("export_preview", [])
    return state


def b18_state(intake):
    """Studio's own state after b18 (G13), computed from the intake (see the module docstring)."""
    try:
        state = bev.a13_state(intake)
        for step_id in ("b1", "b2", "b15", "b16", "b17"):
            bev.step_rows(step_id, state, intake)
    except ValueError as exc:
        raise EvidenceError(str(exc)) from None
    return with_dstep_store(state)


def _snapshot(state):
    """The committed state a read-only step could reach, as its exact repr."""
    return repr(state)


def _terrain_z(state):
    interp = terrain.terrain_interpolator(state["grid"], terrain.meters_per_unit_for_keyword(ev.UNITS_KEYWORD))
    return None if interp is None else interp.interpolate_z


def model_space(state, intake):
    """Studio's model space in creation order, as the d-step engines read it: the boundary,
    the frames, the piles, the setback rings, the array outlines, the grade pads, the road
    lines, the trenches and the export preview. Only layers and linear geometry matter."""
    ents = [{"type": "polyline", "layer": bev.BOUNDARY_PICK, "vertices": intake["boundary"], "closed": True}]
    ents += [{"type": "polyline", "layer": f.get("layer"), "vertices": f["vertices"], "closed": True}
             for f in state["frames"]]
    for p in state.get("piles") or []:
        cyl = p["cylinder"]
        ents.append({"type": "circle", "layer": p.get("layer", dsteps.LAYER_PILING),
                     "center": (cyl["center_x"], cyl["center_y"]), "radius": cyl["diameter_du"] / 2.0,
                     "top_z": cyl["top_z"]})
    ents += [{"type": "polyline", "layer": SETBACK_LAYER, "vertices": r, "closed": True}
             for r in state.get("setback_rings") or []]
    ents += [{"type": "polyline", "layer": scene.ARRAY_LAYER, "vertices": o["vertices"], "closed": True}
             for o in state["array_outlines"]]
    ents += [{"type": "polyline", "layer": bev.bo.GRADE_LAYER, "vertices": p["vertices"], "closed": True}
             for p in state["grade_pads"]]
    ents += [{"type": "polyline", "layer": line["layer"], "vertices": line["vertices"],
              "closed": bool(line["closed"])} for line in state["road_lines"]]
    ents += [{"type": "polyline", "layer": t["layer"], "vertices": t["vertices"], "closed": False}
             for t in state["trenches"]]
    ents += [{"type": "polyline", "layer": p["layer"], "vertices": p["vertices"], "closed": True}
             for p in state["export_preview"]]
    return ents


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def report_row(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise EvidenceError(f"report name {name!r} is not a neutral kebab-case name")
    return _row(f"report-{name}", "report", name=name, value=value)


def trench_rows(trenches):
    """G28 `trench`: vertices in drawing order (a trench is open), ids by centroid (y, x)."""
    ordered = sorted(trenches, key=lambda t: ev._key(*ev._centroid(t["vertices"])))
    return [_row(f"trench-{n}", "trench", vertices=[ev.point(p) for p in t["vertices"]],
                 closed=bool(t["closed"]), depth=ev._length(t["depth_m"]), width=ev._length(t["width_m"]),
                 voltage_class=t["voltage_class"])
            for n, t in enumerate(ordered, 1)]


def export_preview_rows(polylines):
    """G28 `export-preview`: role and the G12-rotated vertices, ids by role, then centroid."""
    ordered = sorted(polylines, key=lambda p: (p["role"], ev._key(*ev._centroid(p["vertices"]))))
    return [_row(f"export-preview-{n}", "export-preview", role=p["role"],
                 vertices=[ev.point(v) for v in ev.canonical_corners(p["vertices"])])
            for n, p in enumerate(ordered, 1)]


def yield_role(entry_name):
    """G28: yield-<entry name lower-cased, dots to dashes>."""
    return "yield-" + entry_name.lower().replace(".", "-")


def blank_host_fields(manifest_text):
    """G28: the manifest's exported_at_utc, project_name, drawing_path and plugin_version
    values replaced by the empty string."""
    return _HOST_FIELD.sub(lambda m: m.group(1) + '""', manifest_text)


def normalize_text(data):
    """G20: UTF-8 text with a leading BOM removed and line endings normalized to LF."""
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if not isinstance(text, str):
        raise EvidenceError("a file row takes the file's bytes or text")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def yield_file_rows(entries):
    """One `file` row per zip entry, ids file-<n> in entry order (G28)."""
    rows = []
    for n, (name, data) in enumerate(entries, 1):
        text = normalize_text(data)
        if name == "manifest.json":
            text = blank_host_fields(text)
        row = _row(f"file-{n}", "file", role=yield_role(name), lines=scene_ev.line_count(text))
        try:
            if len(text) > G24_DIGEST_THRESHOLD:
                row["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                row["chars"] = len(text)
                row["head"] = scene_ev.g21_chunks("".join(text.splitlines(keepends=True)[:40]), MAX_CHUNK)
            else:
                row["chunks"] = scene_ev.g21_chunks(text, MAX_CHUNK)
        except ValueError as exc:
            raise EvidenceError(f"the {name} entry is refused: {exc}") from None
        rows.append(row)
    return rows


def _answer_point(text):
    parts = text.split(",") if isinstance(text, str) else []
    if len(parts) != 2:
        raise EvidenceError(f"point answer {text!r} is not x,y")
    try:
        return (ev._finite(float(parts[0]), "x"), ev._finite(float(parts[1]), "y"))
    except ValueError:
        raise EvidenceError(f"point answer {text!r} is not x,y") from None


# ------------------------------------------------------------------- steps --

def step_trackers_to_panel_groups(state, intake, ctx):
    """d3: the tracker rows through the key-aware reader; a terminal step, nothing is committed."""
    result = dsteps.trackers_to_panel_groups(bev.tracker_entities(state), bev.MPU)
    return [report_row("panel-groups-created", result["panel_groups_created"]),
            report_row("panel-group-slots", result["panel_group_slots"])]


def step_trench(state, intake, ctx):
    """d4: the two picked points; the route avoids panel groups and inverters, follows trenches."""
    start, end = (_answer_point(a) for a in ANSWERS["d4"])
    result = dsteps.trench_command(start, end, model_space(state, intake))
    if not result["succeeded"]:
        return []
    state["trenches"] = state["trenches"] + [result["trench"]]
    return trench_rows([result["trench"]])


def step_define(state, intake, ctx):
    """d5: LEAFDEFINEARRAY at 250,250 through the scene producer (the a5 answers are d5's)."""
    try:
        scene_ev.step_rows("a5", state, intake)
    except ValueError as exc:
        raise EvidenceError(f"step d5 refused: {exc}") from None
    return []


def step_show_export(state, intake, ctx):
    """d6: the array store as read, the project margin; any prior preview is erased first."""
    records = scene.list_arrays(state["arrays"])
    settings = scene.export_settings(state["export_settings"])
    result = dsteps.show_export(records, settings["maintenance_margin_m"], len(state["export_preview"]))
    if not result["succeeded"]:
        return []
    state["export_preview"] = list(result["polylines"])
    return ev.removed_rows({PREVIEW_OF: result["erased"]}) + export_preview_rows(result["polylines"])


def step_hide_export(state, intake, ctx):
    """d7: every preview entity erased."""
    result = dsteps.hide_export(len(state["export_preview"]))
    state["export_preview"] = []
    return ev.removed_rows({PREVIEW_OF: result["erased"]})


def yield_entries(state, intake, drawing_file_name=None):
    """d8's zip entries, (name, bytes) in entry order, from Studio's state; host fields empty."""
    ents = model_space(state, intake)
    closed = [e for e in ents if e["type"] == "polyline" and e.get("closed")]
    template = intake["pile_template"]
    # PileTemplateStore.Normalize (:259-260): an absent or empty list is the default buckets.
    boundaries = (template.get("RevealBucketBoundariesM") if isinstance(template, dict) else None) or None
    bundle = dsteps.yield_bundle(bev.tracker_entities(state), bev.stored_module(state), ents, closed,
                                 terrain_z=_terrain_z(state), reveal_boundaries_m=boundaries,
                                 drawing_file_name=drawing_file_name, meters_per_unit=bev.MPU)
    return dsteps.yield_zip_entries(bundle, dsteps.insunits_name(INSUNITS_METERS))


def step_yield(state, intake, ctx):
    """d8: the zip's entries as file rows; read only."""
    entries = yield_entries(state, intake, ctx.get("drawing_file_name"))
    ctx["yield_entries"] = entries
    return yield_file_rows(entries)


STEPS = {"d3": step_trackers_to_panel_groups, "d4": step_trench, "d5": step_define,
         "d6": step_show_export, "d7": step_hide_export, "d8": step_yield}


def step_rows(step_id, studio_state, intake, ctx=None):
    """G28 rows for one d-step, computed from Studio's state (updated in place, G13; d3
    commits nothing). A read-only step whose state moved also emits `unexpected-change`."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {tuple(STEPS)}")
    ev.validate_intake(intake, KIND)
    state = with_dstep_store(studio_state)
    ctx = {} if ctx is None else ctx
    before = _snapshot(state) if step_id in READ_ONLY else None
    try:
        rows = STEPS[step_id](state, intake, ctx)
    except dsteps.DStepsInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None
    if before is not None and _snapshot(state) != before:
        rows.append(_row("unexpected-change-1", "unexpected-change"))
    return rows


# --------------------------------------------------------------- documents --

def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def parameters_for(intake, step_id):
    """G17 parameters plus G22's `answers`."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]))


def build_document(intake, step_id, capability, operation, rows, revision):
    """One G28 `exports` evidence document, validated under the comparator's bounds."""
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
    synthetic = ["before/recorded", "changes/unrecorded"]
    if step_id == "d8":
        synthetic += list(SYNTHETIC_MANIFEST_FIELDS)
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": hashes[0],
        "output_sha256": hashes[1],
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": ev.CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_dsteps
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
        "synthetic_fields": synthetic,
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


def run_steps(intake, revision, only=None, state=None, drawing_file_name=None):
    """d3, d4, d5, d6, d7, d8 in order from Studio's b18 state (or `state`); returns
    ({step id: document} for every receipted step, or only `only`, whose predecessors still
    run because they are its state; the final state; the d8 zip entries or None)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = b18_state(intake) if state is None else with_dstep_store(state)
    ctx = {"drawing_file_name": drawing_file_name}
    capabilities = {step: (cap, op) for step, cap, op in D_STEPS}
    out = {}
    for step_id in ("d3", "d4", "d5", "d6", "d7", "d8"):
        rows = step_rows(step_id, state, intake, ctx)
        if step_id in capabilities and (only is None or step_id == only):
            out[step_id] = build_document(intake, step_id, *capabilities[step_id], rows, revision)
        if step_id == only:
            break
    return out, state, ctx.get("yield_entries")


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G28 d-step evidence (d3, d4, d6, d7, d8) "
                                                 "from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--drawing-file-name", help="the plugin run copy's file name, for the d8 overview")
    parser.add_argument("--zip", type=Path, help="also write Studio's d8 zip here")
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs, _, entries = run_steps(intake, ev.fixture_revision(args.intake), args.step,
                                     drawing_file_name=args.drawing_file_name)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
        if args.zip is not None and entries is not None:
            with zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in entries:
                    zf.writestr(name, data)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-dsteps-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
