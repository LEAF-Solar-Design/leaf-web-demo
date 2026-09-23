#!/usr/bin/env python3
"""Studio's G27 evidence for the rooftop chain: strings, frame groups, export settings, string data.

Each step is computed from Studio's OWN state after the step before it, starting from the rooftop
intake (G13); nothing here reads plugin output. The engines are server/solar_rooftop_chain.py.

  c1   string-flip             FlipString                 string rows
  c2   string-swap             Swap                       string-label rows (and string rows for
                                                          circuits that traded)
  c3   frame-group-create      LEAFFRAMEGROUPCREATE       setting rows
  c4   frame-group-manage      LEAFFRAMEGROUPRENAME       setting rows
  c5   frame-group-manage      LEAFFRAMEGROUPLIST         report rows (read only)
  c6   select-by-frame-group   LEAFSELECTBYFRAMEGROUP     the selection row (read only)
  c7   frame-group-manage      LEAFFRAMEGROUPDELETE       setting rows
  c9   export-settings         the export-settings command, ten answers   the export-settings row
  c10  string-data             StringData                 the string-data file row (read only)
  c11  string-rebuild          STRINGREBUILD (all)        report row rebuilt-strings (and string
                                                          rows for re-associated strings)
c8 (the trench) is not receipted from this chain (G27). c11 is the Studio side of string-rebuild's
declared divergence: G27 names the capability and its report row, not a scenario step id.

Intake (inputs only, one JSON; its canonical hash is fixture_sha256):
  units         a drawing length unit ("in" for the rooftop fixture)
  strings       [{handle, panels (stored order, first = start end), label {field: int}, and
                 optionally circuit, label_text, start and end {handle, at}, vertices}]
  panel_groups  [{handle, name, and optionally panels, outlines}] in drawing order (the committed
                 intake, docs/parity/evidence/rooftop/chain/intake.json, carries {handle, name})
  settings      {FrameGroups?, HomerunRouting?, export-settings?} as decoded; absent = default
  panel_points  optional {panel handle: [x, y]}: panel centres for the rebuild over vertices

Rows (family exports, G12/G17/G21/G27; every row {id, type, quantity: 1, unit: "each", ...}):
  string          string (handle), panels (stored order), count; ids string-<n> by handle value
  string-label    string (the owning handle) and the label's integer fields by the intake's names
  setting         id setting-<name>, name, value: canonical JSON text, every FrameGroup's
                  LastModifiedTicks replaced by 0
  export-settings module_width, module_height, module_x_spacing, module_y_spacing,
                  maintenance_margin (lengths m), orientation (int), tilt, azimuth (angles deg),
                  manufacturer, product
  selection       handles: the implied selection after the step, ascending handle value
  report          id report-<name>, name, value (exact int)
  file            role string-data, chunks (G21) or the G24 digest form, lines
  unexpected-change   a read-only step whose committed state changed
Rows are emitted sorted by type, then id (G9).

Parameters are {"answers": the G22 answers verbatim}. Fails closed: a malformed intake or
answer, an engine refusal, or a document the comparator refuses is a named error.
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


ev = _load("solar_ground_studio_evidence", HERE / "solar_ground_studio_evidence.py")
compare = ev.compare
chain = _load("solar_rooftop_chain", ROOT / "server" / "solar_rooftop_chain.py")
EvidenceError = ev.EvidenceError

KIND = "rooftop"
# G27 scenario rows, in order: (step id, capability, Studio operation).
C_STEPS = (("c1", "string-flip", "flip"),
           ("c2", "string-swap", "swap"),
           ("c3", "frame-group-create", "frame-group-create"),
           ("c4", "frame-group-manage", "frame-group-rename"),
           ("c5", "frame-group-manage", "frame-group-list"),
           ("c6", "select-by-frame-group", "frame-group-select"),
           ("c7", "frame-group-manage", "frame-group-delete"),
           ("c9", "export-settings", "export-settings"),
           ("c10", "string-data", "string-data"),
           ("c11", "string-rebuild", "string-rebuild"))
STEP_IDS = tuple(step for step, _, _ in C_STEPS)
# G22 answers for G27, verbatim (c11: Enter at the selection prompt, which rebuilds every string).
# c2's second pick is A902: G27 records that AutoCAD resolved the pick to A902, not A8FE.
ANSWERS = {
    "c1": ("string:A67A",),
    "c2": ("string:A912", "string:A902"),
    "c3": ("FGA", "groups:A646,A63B,A631"),
    "c4": ("FGA", "FGB"),
    "c5": (),
    "c6": ("FGB",),
    "c7": ("FGB",),
    "c9": ("1.134", "2.278", "0.025", "0.03", "1", "12.5", "185", "0.6", "ACME", "P440"),
    "c10": ("strings:A912,A90E",),
    "c11": (),
}
READ_ONLY = frozenset({"c5", "c6", "c10"})
INTAKE_KEYS = {"units", "strings", "panel_groups", "settings"}
INTAKE_OPTIONAL = {"panel_points"}
SETTING_NAMES = ("FrameGroups", "HomerunRouting")
EXPORT_SETTINGS_KEY = "export-settings"
FILE_ROLE_STRING_DATA = "string-data"
# G26 as extended by G27: string-data follows shade-per-panel.
FILE_ROLE_ORDER = ("terrain-csv", "scene-dae", "scene-pvc", "bom-csv", "shade-csv", "shade-azal-matrix",
                   "shade-sam", "shade-per-panel", FILE_ROLE_STRING_DATA)
MAX_CHUNK = 16000                 # G21
G24_DIGEST_THRESHOLD = 1_048_576  # G24
G24_HEAD_LINES = 40
_EXPORT_LENGTHS = ("module_width", "module_height", "module_x_spacing", "module_y_spacing", "maintenance_margin")
_EXPORT_ANGLES = ("tilt", "azimuth")


# ------------------------------------------------------------------ intake --

def validate_intake(intake):
    """The rooftop intake, fail closed. Returns the intake unchanged when it is well formed."""
    if not isinstance(intake, dict):
        raise EvidenceError("intake must be a JSON object")
    keys = set(intake)
    if not INTAKE_KEYS <= keys or keys - INTAKE_KEYS - INTAKE_OPTIONAL:
        raise EvidenceError(f"intake keys must be {sorted(INTAKE_KEYS)} plus optional {sorted(INTAKE_OPTIONAL)}")
    if intake["units"] not in compare.LENGTH_UNITS:
        raise EvidenceError(f"intake units must be one of {sorted(compare.LENGTH_UNITS)}")
    try:
        strings = [chain.validate_string(s) for s in
                   chain._bounded_list(intake["strings"], chain.MAX_STRINGS, "strings")]
        chain.validate_panel_groups(intake["panel_groups"])
        settings = intake["settings"]
        if not isinstance(settings, dict) or set(settings) - set(SETTING_NAMES) - {EXPORT_SETTINGS_KEY}:
            raise EvidenceError(f"settings take only {list(SETTING_NAMES) + [EXPORT_SETTINGS_KEY]}")
        chain.validate_frame_groups(settings.get("FrameGroups"))
        chain.load_export_settings(settings.get(EXPORT_SETTINGS_KEY))
        _panel_points(intake)
    except chain.RooftopInputError as exc:
        raise EvidenceError(f"intake refused: {exc}") from None
    handles = [s["handle"] for s in strings]
    if len(set(handles)) != len(handles):
        raise EvidenceError("intake lists a string twice")
    return intake


def _panel_points(intake):
    points = intake.get("panel_points")
    if points is None:
        return None
    if not isinstance(points, dict) or len(points) > chain.MAX_PANELS:
        raise EvidenceError(f"panel_points must be an object of at most {chain.MAX_PANELS} panel centres")
    return [(chain.neutral_handle(h, "panel point handle"), chain._xy(p, "panel centre")) for h, p in points.items()]


def initial_state(intake):
    """Studio's rooftop state from the intake (G13): strings by handle in drawing order, the
    panel groups, the drawing settings, the implied selection."""
    validate_intake(intake)
    strings = {}
    for s in intake["strings"]:
        v = chain.validate_string(s)
        strings[v["handle"]] = v
    settings = json.loads(json.dumps(intake["settings"]))
    if "FrameGroups" in settings:
        settings["FrameGroups"] = chain.validate_frame_groups(settings["FrameGroups"])
    return {"strings": strings, "panel_groups": chain.validate_panel_groups(intake["panel_groups"]),
            "settings": settings, "selection": [], "panel_points": _panel_points(intake)}


def _snapshot(state):
    """What a read-only step could reach: all of it, as its exact repr."""
    return repr(state)


# ----------------------------------------------------------------- answers --

def _pick(answer, prefix):
    """A G22 pick answer "<prefix>:<handle>[,<handle>...]" as neutral handles, in pick order."""
    if not isinstance(answer, str) or not answer.startswith(prefix + ":"):
        raise EvidenceError(f"answer {answer!r} is not a {prefix} pick")
    parts = answer[len(prefix) + 1:].split(",")
    try:
        return [chain.neutral_handle(p, f"{prefix} pick") for p in parts]
    except chain.RooftopInputError as exc:
        raise EvidenceError(str(exc)) from None


def _string(state, handle):
    if handle not in state["strings"]:
        raise EvidenceError(f"string {handle} is not in Studio's state")
    return state["strings"][handle]


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return dict({"id": row_id, "type": row_type, "quantity": 1, "unit": "each"}, **fields)


def _length(value):
    return {"kind": "length", "value": ev._finite(value, "length"), "unit": "m"}


def _angle(value):
    return {"kind": "angle", "value": ev._finite(value, "angle"), "unit": "deg"}


def string_rows(strings):
    """G27 `string` rows for the strings whose record changed, ids by ascending handle value."""
    ordered = sorted(strings, key=lambda s: chain.handle_order(s["handle"]))
    return [_row(f"string-{n}", "string", string=s["handle"], panels=list(s["panels"]), count=len(s["panels"]))
            for n, s in enumerate(ordered, 1)]


def label_rows(strings):
    """G27 `string-label` rows: the owning string and its label's integer fields."""
    ordered = sorted(strings, key=lambda s: chain.handle_order(s["handle"]))
    rows = []
    for n, s in enumerate(ordered, 1):
        chain.validate_label(s["label"])
        rows.append(_row(f"string-label-{n}", "string-label", string=s["handle"], **s["label"]))
    return rows


def _setting_value(name, value):
    if name == "FrameGroups":
        value = chain.frame_groups_without_clock(value)
    return chain.canonical_setting_text(value)


def setting_rows(before, after):
    """G20 setting rows for FrameGroups and HomerunRouting whose canonical text changed; an
    absent FrameGroups reads as its declared default [] (DrawingPropertiesJson.cs:529)."""
    rows = []
    for name in sorted(SETTING_NAMES):
        default = [] if name == "FrameGroups" else None
        old = _setting_value(name, before.get(name, default))
        new = _setting_value(name, after.get(name, default))
        if old != new:
            rows.append(_row(f"setting-{name}", "setting", name=name, value=new))
    return rows


def export_settings_row(settings):
    fields = {}
    for key in chain.EXPORT_SETTINGS_FIELDS:
        value = settings[key]
        if key in _EXPORT_LENGTHS:
            fields[key] = _length(value)
        elif key in _EXPORT_ANGLES:
            fields[key] = _angle(value)
        else:
            fields[key] = value
    return _row("export-settings-1", "export-settings", **fields)


def report_row(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise EvidenceError(f"report name {name!r} is not a neutral kebab-case name")
    return _row(f"report-{name}", "report", name=name, value=value)


def g21_chunks(text, limit=MAX_CHUNK):
    """G21: split only after a line feed into pieces of at most `limit` characters; one linear pass."""
    lines = text.split("\n")
    pieces = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    chunks, current, size = [], [], 0
    for piece in pieces:
        if len(piece) > limit:
            raise EvidenceError(f"a single line of {len(piece)} characters exceeds the G21 chunk limit {limit}")
        if size + len(piece) > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def normalize_file_text(text):
    """G20: LF line ends and no leading BOM."""
    if not isinstance(text, str):
        raise EvidenceError("a file row takes text")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def file_rows(files):
    """files: {role: text as written}; ids file-<n> in the G26/G27 role order."""
    rows = []
    for role in files:
        if role not in FILE_ROLE_ORDER:
            raise EvidenceError(f"file role {role!r} is not one of {FILE_ROLE_ORDER}")
    for n, role in enumerate(sorted(files, key=FILE_ROLE_ORDER.index), 1):
        text = normalize_file_text(files[role])
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        row = _row(f"file-{n}", "file", role=role, lines=lines)
        if len(text) > G24_DIGEST_THRESHOLD:
            row["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            row["chars"] = len(text)
            row["head"] = g21_chunks("".join(text.splitlines(keepends=True)[:G24_HEAD_LINES]))
        else:
            row["chunks"] = g21_chunks(text)
        rows.append(row)
    return rows


# ------------------------------------------------------------------- steps --

def step_flip(state, answers):
    """c1: the picked string flips."""
    (handle,) = _pick(answers[0], "string")
    flipped = chain.string_flip(_string(state, handle))
    state["strings"][handle] = flipped
    return string_rows([flipped])


def step_swap(state, answers):
    """c2: the two picked strings swap their labels (and circuits)."""
    (h1,) = _pick(answers[0], "string")
    (h2,) = _pick(answers[1], "string")
    old1, old2 = _string(state, h1), _string(state, h2)
    new1, new2 = chain.string_swap(old1, old2)
    state["strings"][h1], state["strings"][h2] = new1, new2
    pairs = ((old1, new1), (old2, new2))
    changed_labels = [new for old, new in pairs if old["label"] != new["label"]]
    changed_records = [new for old, new in pairs if old.get("circuit") != new.get("circuit")]
    return label_rows(changed_labels) + string_rows(changed_records)


def _save_frame_groups(state, groups):
    before = dict(state["settings"])
    state["settings"]["FrameGroups"] = groups
    return setting_rows(before, state["settings"])


def step_frame_group_create(state, answers):
    """c3: the name, then the picked panel groups."""
    picked = _pick(answers[1], "groups")
    outcome, groups = chain.frame_group_create(state["settings"].get("FrameGroups"), answers[0], picked,
                                               [g["handle"] for g in state["panel_groups"]], chain.dotnet_utc_ticks())
    if outcome != "created":
        return []
    return _save_frame_groups(state, groups)


def step_frame_group_rename(state, answers):
    """c4: old name, new name; only a rename saves."""
    outcome, groups = chain.rename_by_name(state["settings"].get("FrameGroups"), answers[0].strip(),
                                           answers[1].strip(), chain.dotnet_utc_ticks())
    if outcome != chain.RENAMED:
        return []
    return _save_frame_groups(state, groups)


def step_frame_group_list(state, answers):
    """c5: the printed count and each listed group's frame count (read only)."""
    listed = chain.frame_group_list(state["settings"].get("FrameGroups"))
    rows = [report_row("frame-groups", listed["count"])]
    rows += [report_row(f"frame-group-{n}-frames", g["frames"]) for n, g in enumerate(listed["groups"], 1)]
    return rows


def _drawing_handles(state):
    handles = {g["handle"] for g in state["panel_groups"]}
    for s in state["strings"].values():
        handles.add(s["handle"])
        handles.update(s["panels"])
        for end in ("start", "end"):
            if end in s:
                handles.add(s[end]["handle"])
    for g in state["panel_groups"]:
        handles.update(g.get("panels", ()))
    return handles


def step_frame_group_select(state, answers):
    """c6: the implied selection after the command (read only: a selection is not drawing state)."""
    result = chain.frame_group_select(state["settings"].get("FrameGroups"), answers[0], _drawing_handles(state))
    selection = sorted(result["handles"], key=chain.handle_order) if result["status"] == "selected" \
        else sorted(state["selection"], key=chain.handle_order)
    return [_row("selection-1", "selection", handles=selection)]


def step_frame_group_delete(state, answers):
    """c7: the named group is removed; only a removal saves."""
    groups, removed = chain.delete_by_name(state["settings"].get("FrameGroups"), answers[0].strip())
    if removed is None:
        return []
    return _save_frame_groups(state, groups)


def step_export_settings(state, answers):
    """c9: the ten prompts; the row when any field differs from the step before."""
    stored = state["settings"].get(EXPORT_SETTINGS_KEY)
    before = chain.load_export_settings(stored)
    after = chain.export_settings_prompt(stored, list(answers))
    state["settings"][EXPORT_SETTINGS_KEY] = after
    return [export_settings_row(after)] if after != before else []


def step_string_data(state, answers):
    """c10: the picked strings, deduplicated in pick order; the file it writes (read only)."""
    seen, selected = set(), []
    for h in _pick(answers[0], "strings"):
        if h not in seen:
            seen.add(h)
            selected.append(_string(state, h))
    text = chain.string_data(state["panel_groups"], selected)
    return [] if text is None else file_rows({FILE_ROLE_STRING_DATA: text})


def step_string_rebuild(state, answers):
    """c11: every string, in drawing order; the rebuilt count and the re-associated strings."""
    old = list(state["strings"].values())
    rebuilt, count = chain.string_rebuild(old, state["panel_points"])
    changed = [new for before, new in zip(old, rebuilt) if before["panels"] != new["panels"]]
    state["strings"] = {s["handle"]: s for s in rebuilt}
    return [report_row("rebuilt-strings", count)] + string_rows(changed)


STEPS = {"c1": step_flip, "c2": step_swap, "c3": step_frame_group_create, "c4": step_frame_group_rename,
         "c5": step_frame_group_list, "c6": step_frame_group_select, "c7": step_frame_group_delete,
         "c9": step_export_settings, "c10": step_string_data, "c11": step_string_rebuild}


def step_rows(step_id, state, answers=None):
    """G27 rows for one step from Studio's state (updated in place, G13). A read-only step
    whose state moved also emits `unexpected-change`."""
    if step_id not in STEPS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    answers = ANSWERS[step_id] if answers is None else tuple(answers)
    if len(answers) != len(ANSWERS[step_id]):
        raise EvidenceError(f"step {step_id} takes {len(ANSWERS[step_id])} answers")
    before = _snapshot(state) if step_id in READ_ONLY else None
    try:
        rows = STEPS[step_id](state, answers)
    except chain.RooftopInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None
    if before is not None and _snapshot(state) != before:
        rows.append(_row("unexpected-change-1", "unexpected-change"))
    return rows


# --------------------------------------------------------------- documents --

def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def parameters_for(step_id):
    """G22: the step's answers verbatim."""
    return {"answers": list(ANSWERS[step_id])}


def build_document(intake, step_id, capability, operation, rows, revision):
    """One G27 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    rows = sorted(rows, key=_row_order)
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise EvidenceError(f"step {step_id} emitted a duplicate row id")
    parameters = parameters_for(step_id)
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": step_id, "format": ev.FORMAT}
    synthetic = ["before/recorded", "changes/unrecorded"]
    if any(row["type"] == "setting" and row["name"] == "FrameGroups" for row in rows):
        synthetic.append("setting/FrameGroups/LastModifiedTicks")
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_rooftop_chain
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


def run_steps(intake, revision, only=None):
    """Every G27 step in order from the intake (G13); returns ({step id: document}, state) for
    every step, or only `only` (its predecessors still run: they are its state)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    state = initial_state(intake)
    out = {}
    for step_id, capability, operation in C_STEPS:
        rows = step_rows(step_id, state)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, capability, operation, rows, revision)
        if step_id == only:
            break
    return out, state


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G27 rooftop-chain evidence from a rooftop intake.")
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
        print(f"solar-rooftop-chain-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
