#!/usr/bin/env python3
"""Studio's G33 evidence for the PVcase solve: step v1 (LEAFPVCASESOLVE, capability pvcase-solve).

Computed from Studio's OWN solve of the committed intake (G13), never from plugin output; the engine is
server/solar_pvcase_solve.py, a literal port of the plugin command, its input builder and the headless
solver. The intake is docs/parity/evidence/rooftop/pvcase/intake.json (the plugin adapter writes it
from the v0 capture; its shape is documented in the engine's module docstring): the panel groups in
the plugin's read order with their matrix rows, and the panels-per-string setting. It carries no L2
snapshot: the capture registers no L2 inverter, so the solve takes the zero L2 path.

Rows (exports family, G12/G17 conventions; every row {id, type, quantity: 1, unit: "each", ...}):
  panel-assignment   group (the panel group's handle as the intake writes it) and assignments: a
                     list of [panel id, inverter id, string input number] in matrix row and panel
                     order, for every panel (Code != 0); one row per panel group, ids
                     panel-assignment-<n> by ascending handle value (G9, G33)
The matrix's drawing-file-name field never enters evidence (it is not in the intake). Parameters are
{"answers": []} (G22: v1 takes no answers). Fails closed: a malformed intake, a solve that refuses
(no panel groups, no usable panels) or a document the comparator refuses is a named error.
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
pvcase = _load("solar_pvcase_solve", ROOT / "server" / "solar_pvcase_solve.py")
EvidenceError = ev.EvidenceError

KIND = "rooftop"
FORMAT = ev.FORMAT  # "ground-v1": the plugin's rooftop adapter writes the same format for every G27+ step
STEP_ID = "v1"
CAPABILITY = "pvcase-solve"
OPERATION = "pvcase-solve"
ANSWERS = ()  # G33: v1 takes no answers
ROW_TYPE = "panel-assignment"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "pvcase" / "intake.json"


def _row(row_id, row_type, **fields):
    return dict({"id": row_id, "type": row_type, "quantity": 1, "unit": "each"}, **fields)


def assignment_rows(groups):
    """G33 panel-assignment rows, one per panel group, ascending by handle value."""
    ordered = sorted(groups, key=lambda g: pvcase.handle_order(g["handle"]))
    return [_row(f"{ROW_TYPE}-{n}", ROW_TYPE, group=g["handle"], assignments=pvcase.panel_assignments(g))
            for n, g in enumerate(ordered, 1)]


def solve_rows(intake):
    """Studio's v1: the solve over the intake, then its rows. Returns (rows, solve outcome)."""
    try:
        outcome = pvcase.pvcase_solve(intake)
    except pvcase.PvcaseInputError as exc:
        raise EvidenceError(f"intake refused: {exc}") from None
    if outcome["status"] != pvcase.SOLVED:
        raise EvidenceError(f"step {STEP_ID} refused: {outcome['message']}")
    return assignment_rows(outcome["panel_groups"]), outcome


def parameters_for():
    """G22: the step's answers verbatim."""
    return {"answers": list(ANSWERS)}


def build_document(intake, rows, revision):
    """The v1 `exports` evidence document, validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise EvidenceError(f"step {STEP_ID} emitted a duplicate row id")
    parameters = parameters_for()
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": STEP_ID, "format": FORMAT}
    try:
        # The intake is hashed on the input-scan bounds (the same canonical bytes as semantic_hash):
        # a full rooftop matrix set is near the comparison document's node budget.
        fixture = compare.scan_input(intake)
        hashes = (compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
                  compare.semantic_hash(after))
    except compare.InputError as exc:
        raise EvidenceError(f"step {STEP_ID} evidence refused by the comparator: {exc}") from None
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": hashes[0],
        "output_sha256": hashes[1],
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": ev.CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_pvcase_solve
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
        "provenance": {"side": "studio", "fixture_kind": KIND, "step": STEP_ID, "capability": CAPABILITY,
                       "operation": OPERATION},
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
        raise EvidenceError(f"step {STEP_ID} evidence refused by the comparator: {exc}") from None
    if len(ev._serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"step {STEP_ID} evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def run(intake, revision):
    """The v1 document from the intake (G13)."""
    rows, _ = solve_rows(intake)
    return build_document(intake, rows, revision)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G33 PVcase-solve evidence from the pvcase intake.")
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--revision", help="40-hex commit; default: the last commit touching the intake (rule 2)")
    args = parser.parse_args(argv)
    try:
        intake = json.loads(args.intake.read_text(encoding="utf-8"))
        revision = args.revision if args.revision is not None else ev.fixture_revision(args.intake)
        doc = run(intake, revision)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        target = args.out_dir / f"{STEP_ID}.json"
        target.write_text(ev._serialize(doc), encoding="utf-8")
        if json.loads(target.read_text(encoding="utf-8")) != json.loads(ev._serialize(doc)):
            raise EvidenceError(f"{target} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-pvcase-solve-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
