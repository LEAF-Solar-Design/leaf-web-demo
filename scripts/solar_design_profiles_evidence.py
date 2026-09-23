#!/usr/bin/env python3
"""Studio's G32 evidence for the design-profile steps f1 to f5 (LEAFPROFILE).

The profile steps run on the generate fixture after g6, but nothing g1 to g6 commits is read by
a profile step: a profile's state is the drawing's design-profiles record, which starts empty,
plus the plugin's current settings, which are an INPUT (the committed snapshot
docs/parity/evidence/ground/generate/profile-settings.json, the settings object of profile
Alpha in f1's record). So Studio chains f1 to f5 from an empty profile store and the current
settings that snapshot was copied from (G13). Nothing here reads plugin output. The engine is
server/solar_design_profiles.py.

  f1  Create "Alpha", adopt No   design-profile rows, report active-prefix and profile-version
  f2  Create "Beta"              the same
  f3  Swap "A"                   the same
  f4  List (read only)           report profiles (int) and profile-<n> ("<prefix> <name>")
  f5  Delete "B", confirm Yes    design-profile rows, report active-prefix and profile-version

Rows (family exports, G12 conventions; every row {id, type, quantity: 1, unit: "each", ...}):
  design-profile  name, prefix, settings (the canonical JSON text of the profile's settings
                  object); ids design-profile-<n> in stored order
  report          active-prefix (str; "" when the record has none), profile-version (int),
                  profiles (int), profile-<n> (str)
  unexpected-change   f4 whose committed state moved (the G20 read-only rule)
Rows are emitted sorted by type, then id (G9). Parameters are G17's plus the G22 `answers`.
The record's creation times are host dependent and never enter a row.

Fails closed: a malformed intake or settings snapshot, an engine refusal, or a document the
comparator refuses is a named error.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
profiles = _load("solar_design_profiles", ROOT / "server" / "solar_design_profiles.py")

KIND = "generate"
CAPABILITY = "design-profile-manage"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "ground" / "generate" / "intake.json"
DEFAULT_SETTINGS = ROOT / "docs" / "parity" / "evidence" / "ground" / "generate" / "profile-settings.json"
DEFAULT_OUT_DIR = Path("C:/tmp/solar-parity/profile-ev/studio")
# G32 scenario rows, in order: (step id, Studio operation). Capability design-profile-manage for all five.
F_STEPS = (("f1", "create"), ("f2", "create"), ("f3", "swap"), ("f4", "list"), ("f5", "delete"))
STEP_IDS = tuple(step for step, _ in F_STEPS)
# G32 (the G22 table).
ANSWERS = {"f1": ("Create", "Alpha", "No"), "f2": ("Create", "Beta"), "f3": ("Swap", "A"),
           "f4": ("List",), "f5": ("Delete", "B", "Yes")}
READ_ONLY = frozenset({"f4"})
# A fixed creation time: the record carries one, no row does, so the run stays deterministic.
CREATED_UTC = datetime(2000, 1, 1, tzinfo=timezone.utc)


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


# ------------------------------------------------------------------- state --

def new_state(settings_snapshot):
    """An empty profile store and the plugin's current settings the snapshot was copied from."""
    try:
        settings = profiles.current_settings_from_preset(settings_snapshot)
    except profiles.ProfileInputError as exc:
        raise EvidenceError(f"profile settings snapshot refused: {exc}") from None
    return {"manager": profiles.DesignProfileManager(), "settings": settings}


def _check_state(state):
    if not isinstance(state, dict) or not isinstance(state.get("manager"), profiles.DesignProfileManager) \
            or not isinstance(state.get("settings"), dict):
        raise EvidenceError("studio state must be {manager, settings} from new_state")
    return state


def _snapshot(state):
    """The committed state a read-only step could reach: the record and the current settings."""
    return json.dumps([state["manager"].record(), state["settings"]], sort_keys=True)


# -------------------------------------------------------------------- rows --

def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def report_row(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise EvidenceError(f"report name {name!r} is not a neutral kebab-case name")
    return _row(f"report-{name}", "report", name=name, value=value)


def state_rows(manager):
    """G32 state: one design-profile row per profile in stored order, the active prefix and the
    record's version."""
    record = manager.record()
    rows = [_row(f"design-profile-{n}", "design-profile", name=p["Name"], prefix=p["Prefix"],
                 settings=None if p["Settings"] is None else profiles.canonical_settings_text(p["Settings"]))
            for n, p in enumerate(record["Profiles"], 1)]
    rows.append(report_row("active-prefix", record["ActivePrefix"] or ""))
    rows.append(report_row("profile-version", record["Version"]))
    return rows


def list_rows(listed):
    """G32 f4: the count and one "<prefix> <name>" per listed profile, in listed order."""
    return [report_row("profiles", len(listed))] + \
        [report_row(f"profile-{n}", f"{prefix} {name}") for n, (prefix, name) in enumerate(listed, 1)]


# ------------------------------------------------------------------- steps --

def step_rows(step_id, state, ctx=None):
    """G32 rows for one step, computed from Studio's state (updated in place, G13). A read-only
    step whose state moved also emits `unexpected-change`."""
    if step_id not in ANSWERS:
        raise EvidenceError(f"step {step_id!r} is not one of {STEP_IDS}")
    state = _check_state(state)
    ctx = {} if ctx is None else ctx
    before = _snapshot(state) if step_id in READ_ONLY else None
    try:
        result = profiles.leafprofile(state["manager"], state["settings"], list(ANSWERS[step_id]),
                                      created_utc=CREATED_UTC)
    except profiles.ProfileInputError as exc:
        raise EvidenceError(f"step {step_id} refused: {exc}") from None
    ctx.setdefault("messages", {})[step_id] = result["messages"]
    rows = list_rows(result["listed"]) if step_id == "f4" else state_rows(state["manager"])
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


def build_document(intake, step_id, operation, rows, revision):
    """One G32 `exports` evidence document, validated under the comparator's bounds."""
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_design_profiles
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
        "provenance": {"side": "studio", "fixture_kind": KIND, "step": step_id, "capability": CAPABILITY,
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


def run_steps(intake, settings_snapshot, revision, only=None):
    """f1 to f5 in order from an empty profile store; returns ({step id: document} for every
    step, or only `only`, whose predecessors still run because they are its state; the final
    state; the editor lines per step)."""
    if only is not None and only not in STEP_IDS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    ev.validate_intake(intake, KIND)
    state = new_state(settings_snapshot)
    ctx = {}
    out = {}
    for step_id, operation in F_STEPS:
        rows = step_rows(step_id, state, ctx)
        if only is None or step_id == only:
            out[step_id] = build_document(intake, step_id, operation, rows, revision)
        if step_id == only:
            break
    return out, state, ctx.get("messages", {})


# --------------------------------------------------------------------- CLI --

def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G32 design-profile evidence (f1 to f5) "
                                                 "from the generate intake and the settings snapshot.")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--step", choices=STEP_IDS)
    args = parser.parse_args(argv)
    try:
        intake = compare.load_evidence(args.intake)
        snapshot = compare.load_evidence(args.settings)
        docs, _, _ = run_steps(intake, snapshot, ev.fixture_revision(args.intake), args.step)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for step_id, doc in docs.items():
            target = args.out_dir / f"{step_id}.json"
            target.write_text(ev._serialize(doc), encoding="utf-8")
            if compare.load_evidence(target) != json.loads(ev._serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-design-profiles-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
