#!/usr/bin/env python3
"""Studio's G30 evidence for the dialog batch e1, e4 and e6 on the terrain fixture.

The e-steps continue from Studio's OWN state after d8 (G13). Nothing Studio's chain commits
before e1 (t1 to t7, the a-, b- and d-steps) draws on a LEAF-PVCASE-SHADING-* layer, writes a
project-area record or writes the pile-template store, and those are the only things these
three steps read; so their state starts empty, plus the pile-template store as it stood before
e6, which is the committed intake file docs/parity/evidence/ground/terrain/pile-templates-before.json
(copied verbatim from the capture; it names two templates and no site or project). Nothing here
reads plugin output. The engines are server/solar_ground_dialogs.py.

  e1  shading-object-placement  LEAFSHADINGOBJECT  the form accepted with its defaults, kind
                                                   Tree, tree centre 250,150
  e2, e3 (LEAFSHADESIM, LEAFSHADOWCURTAIN) and e5 (LEAFFRAME) are not receipted (G30).
  e4  project-areas             LEAFAREAS          "Add Area", then OK
  e6  pile-templates            LEAFPILETEMPLATES  "+", then OK (drawing read only)

Rows (family exports, G12/G17/G21/G24/G30; every row {id, type, quantity: 1, unit: "each", ...}):
  shading-object  object_kind (tree), center (point), radius (length), restriction (the
                  restriction ring's radius, a length, or null), label and label_at (point)
                  when drawn; ids by centre (y, x)
  project-area    name, preset, pitch_override (length), sub_area_id, boundary (null or the
                  boundary's handle), from the saved record decoded, in stored order
  file            role pile-templates-json: the store file as written, G20 normalization (LF
                  line ends, no BOM), G21 chunks or the G24 digest by size
  report          e6: templates (int), active-template (str)
  unexpected-change   e6 whose drawing state moved (the G20 read-only rule)
Rows are emitted sorted by type, then id (G9). Parameters are G17's plus the G22 `answers`,
and for e1 `form_values`: the form's numeric inputs as accepted (ShadingObjectParams defaults,
ShadingObject.cs:17-33, through the form's "0.##" round trip), keyed by their field names in
lower snake_case (G30a: tree_top_diameter ... vegetation_height), values in metres.

Fails closed: a malformed intake, state or store, an engine refusal, or a document the
comparator refuses is a named error; nothing is written.
"""
from __future__ import annotations

import argparse
import hashlib
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


scene_ev = _load("solar_ground_scene_evidence", HERE / "solar_ground_scene_evidence.py")
ev = scene_ev.ev                    # the terrain producer, loaded once through the scene producer
compare = ev.compare
dialogs = _load("solar_ground_dialogs", ROOT / "server" / "solar_ground_dialogs.py")

KIND = "terrain"
TERRAIN_DIR = ROOT / "docs" / "parity" / "evidence" / "ground" / "terrain"
DEFAULT_INTAKE = TERRAIN_DIR / "intake.json"
DEFAULT_PILE_STORE = TERRAIN_DIR / "pile-templates-before.json"
DEFAULT_OUT_DIR = Path("C:/tmp/solar-parity/dialog-ev/studio")
# G30 scenario rows this slice receipts, in order: (step id, capability, Studio operation).
E_STEPS = (("e1", "shading-object-placement", "shading-object"),
           ("e4", "project-areas", "project-areas"),
           ("e6", "pile-templates", "pile-templates"))
STEP_IDS = tuple(step for step, _, _ in E_STEPS)
NOT_RECEIPTED = ("e2", "e3", "e5")
# G30 (the G22 table).
ANSWERS = {"e1": ("form:Tree:defaults", "250,150"), "e4": ("Add Area", "OK"), "e6": ("+", "OK")}
READ_ONLY = frozenset({"e6"})
FILE_ROLE = "pile-templates-json"
MAX_CHUNK = 16000                   # G21
G24_DIGEST_THRESHOLD = 1_048_576    # G24
MAX_STORE_BYTES = dialogs.MAX_STORE_CHARS
_FORM_ANSWER = re.compile(r"form:([A-Za-z]+):defaults")


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


# ------------------------------------------------------------------- state --

def new_state(pile_store_text):
    """Studio's state as the e-steps read it after d8 (see the module docstring): no shading
    objects, no project-area record, the shading form's session memory unset, and the
    pile-template store's text (None when the store file does not exist)."""
    if pile_store_text is not None and not isinstance(pile_store_text, str):
        raise EvidenceError("the pile-template store must be text or None")
    return {"shading": [], "shading_last": None, "area_record": None, "pile_store_text": pile_store_text}


def _validated_state(state):
    if not isinstance(state, dict) or set(state) != {"shading", "shading_last", "area_record", "pile_store_text"}:
        raise EvidenceError("studio state must be this producer's dialog state")
    return state


def _drawing_snapshot(state):
    """What a drawing-read-only step could move: the shading entities and the area record."""
    return repr((state["shading"], state["area_record"]))


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def report_row(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise EvidenceError(f"report name {name!r} is not a neutral kebab-case name")
    return _row(f"report-{name}", "report", name=name, value=value)


def shading_object_rows(placements):
    """G30 `shading-object`: one row per placed tree (its canopy circle, restriction ring and
    label, as PlaceTree draws them), ids by centre (y, x)."""
    objects = []
    for ents in placements:
        body = [e for e in ents if e["type"] == "circle" and e["layer"] == dialogs.SHADING_TREE_LAYER]
        ring = [e for e in ents if e["type"] == "circle" and e["layer"] == dialogs.SHADING_RESTRICTION_LAYER]
        labels = [e for e in ents if e["type"] == "text" and e["layer"] == dialogs.SHADING_TREE_LAYER]
        if len(body) != 1 or len(ring) > 1 or len(labels) > 1 or len(body) + len(ring) + len(labels) != len(ents):
            raise EvidenceError("a tree placement must be one canopy circle, at most one ring and one label")
        objects.append((body[0], ring[0] if ring else None, labels[0] if labels else None))
    rows = []
    for n, (body, ring, label) in enumerate(sorted(objects, key=lambda o: ev._key(*o[0]["center"])), 1):
        row = _row(f"shading-object-{n}", "shading-object", object_kind="tree", center=ev.point(body["center"]),
                   radius=ev._length(body["radius"]),
                   restriction=None if ring is None else ev._length(ring["radius"]))
        if label is not None:
            row["label"] = label["text"]
            row["label_at"] = ev.point(label["position"])
        rows.append(row)
    return rows


def project_area_rows(record_text):
    """G30 `project-area`: the saved record decoded, neutral names, in stored order."""
    try:
        record = json.loads(record_text)
        areas = record["Areas"]
        rows = [_row(f"project-area-{n}", "project-area", name=a["Name"], preset=a["FramePresetName"],
                     pitch_override=ev._length(a["PitchOverrideM"]), sub_area_id=a["SubAreaId"],
                     boundary=a["BoundaryHandle"])
                for n, a in enumerate(areas, 1)]
    except (ValueError, TypeError, KeyError) as exc:
        raise EvidenceError(f"the saved project-area record does not decode: {exc}") from None
    return rows


def normalize_text(text):
    """G20: a leading BOM removed and line endings normalized to LF."""
    if not isinstance(text, str):
        raise EvidenceError("a file row takes the file's text")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def store_file_row(text):
    """The `file` row for the store as written: G21 chunks, or the G24 digest past 1 MiB."""
    text = normalize_text(text)
    row = _row("file-1", "file", role=FILE_ROLE, lines=scene_ev.line_count(text))
    try:
        if len(text) > G24_DIGEST_THRESHOLD:
            row["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            row["chars"] = len(text)
            row["head"] = scene_ev.g21_chunks("".join(text.splitlines(keepends=True)[:40]), MAX_CHUNK)
        else:
            row["chunks"] = scene_ev.g21_chunks(text, MAX_CHUNK)
    except ValueError as exc:
        raise EvidenceError(f"the {FILE_ROLE} file is refused: {exc}") from None
    return row


def _answer_point(text):
    parts = text.split(",") if isinstance(text, str) else []
    if len(parts) != 2:
        raise EvidenceError(f"point answer {text!r} is not x,y")
    try:
        return (ev._finite(float(parts[0]), "x"), ev._finite(float(parts[1]), "y"))
    except ValueError:
        raise EvidenceError(f"point answer {text!r} is not x,y") from None


# ------------------------------------------------------------------- steps --

def step_shading_object(state, intake, ctx):
    """e1: the form accepted untouched, then the tree centre (G30 answers)."""
    form_answer, centre = ANSWERS["e1"]
    m = _FORM_ANSWER.fullmatch(form_answer)
    if m is None:
        raise EvidenceError(f"e1 form answer {form_answer!r} is not form:<kind>:defaults")
    params = dialogs.shading_form_accept(state["shading_last"])
    if params["Kind"] != m.group(1):
        raise EvidenceError(f"the form opens on kind {params['Kind']}, not the answered {m.group(1)}")
    state["shading_last"] = params
    ents = dialogs.place_tree(params, _answer_point(centre))
    state["shading"] = state["shading"] + ents
    ctx.setdefault("parameters", {})["e1"] = {"form_values": dialogs.shading_form_values(params)}
    return shading_object_rows([ents])


def step_project_areas(state, intake, ctx):
    """e4: the manager on the drawing's record, Add Area, OK; the active preset is the intake's."""
    _, text = dialogs.project_areas_add_then_ok(state["area_record"], intake["active_preset"]["Name"])
    state["area_record"] = text
    return project_area_rows(text)


def step_pile_templates(state, intake, ctx):
    """e6: the store read, the manager's +, then OK; the store file as written and its counts."""
    store, text = dialogs.pile_templates_add_then_ok(state["pile_store_text"])
    state["pile_store_text"] = text
    ctx["pile_store_after"] = text
    return [store_file_row(text), report_row("templates", len(store.list())),
            report_row("active-template", store.active)]


STEPS = {"e1": step_shading_object, "e4": step_project_areas, "e6": step_pile_templates}


def step_rows(step_id, studio_state, intake, ctx=None):
    """G30 rows for one e-step, computed from Studio's state (updated in place, G13). A
    drawing-read-only step whose drawing state moved also emits `unexpected-change`."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {tuple(STEPS)}")
    ev.validate_intake(intake, KIND)
    state = _validated_state(studio_state)
    ctx = {} if ctx is None else ctx
    before = _drawing_snapshot(state) if step_id in READ_ONLY else None
    try:
        rows = STEPS[step_id](state, intake, ctx)
    except dialogs.DialogsInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None
    if before is not None and _drawing_snapshot(state) != before:
        rows.append(_row("unexpected-change-1", "unexpected-change"))
    return rows


# --------------------------------------------------------------- documents --

def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def parameters_for(intake, step_id, extra=None):
    """G17 parameters plus G22's `answers` (and e1's `form_values`)."""
    return dict(ev.parameters_for(intake), answers=list(ANSWERS[step_id]), **(extra or {}))


def build_document(intake, step_id, capability, operation, rows, revision, extra_parameters=None,
                   provenance=None):
    """One G30 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    rows = sorted(rows, key=_row_order)
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise EvidenceError(f"step {step_id} emitted a duplicate row id")
    parameters = parameters_for(intake, step_id, extra_parameters)
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_ground_dialogs
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
        "provenance": dict({"side": "studio", "fixture_kind": KIND, "step": step_id, "capability": capability,
                            "operation": operation}, **(provenance or {})),
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


def run_steps(intake, revision, pile_store_text, only=None, state=None):
    """e1, e4, e6 in order from Studio's state after d8 (or `state`); returns ({step id:
    document} for every step, or only `only`, whose predecessors still run because they are its
    state; the final state; Studio's store text after e6, or None when e6 did not run)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = new_state(pile_store_text) if state is None else _validated_state(state)
    store_sha = None if pile_store_text is None else hashlib.sha256(pile_store_text.encode("utf-8")).hexdigest()
    ctx = {}
    out = {}
    for step_id, capability, operation in E_STEPS:
        rows = step_rows(step_id, state, intake, ctx)
        if only is None or step_id == only:
            extra = ctx.get("parameters", {}).get(step_id)
            prov = {"pile_store_before_sha256": store_sha} if step_id == "e6" and store_sha else None
            out[step_id] = build_document(intake, step_id, capability, operation, rows, revision, extra, prov)
        if step_id == only:
            break
    return out, state, ctx.get("pile_store_after")


def read_store_file(path):
    """The store file as File.ReadAllText reads it (UTF-8), None when it does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    data = path.read_bytes()
    if len(data) > MAX_STORE_BYTES:
        raise EvidenceError(f"{path} exceeds {MAX_STORE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"{path} is not UTF-8: {exc}") from None


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G30 dialog-batch evidence (e1, e4, e6) "
                                                 "from a G11 terrain intake.")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--pile-store", type=Path, default=DEFAULT_PILE_STORE,
                        help="the pile-template store before e6 (default: the committed before file)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--store-out", type=Path, help="also write Studio's store file after e6 here")
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        docs, _, store_after = run_steps(intake, ev.fixture_revision(args.intake), read_store_file(args.pile_store),
                                         args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
        if args.store_out is not None and store_after is not None:
            args.store_out.write_bytes(store_after.encode("utf-8"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-ground-dialogs-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
