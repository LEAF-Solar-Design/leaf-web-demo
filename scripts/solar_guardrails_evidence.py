"""Studio's G36 evidence for the guardrails steps: g1 (GUARDRAILS) and g2 (GUARDRAILSVALIDATE).

Both steps read only the committed, name-free g0 intake (default docs/parity/evidence/batch2/g0-intake.json,
`--intakes` overrides; the Branch2025 adapter tools/parity/batch2_evidence.py writes it), run the engine in
server/solar_guardrails.py and write `<step>.json` in the G36 `exports` shape the plugin adapter uses (rows
{id, type, quantity: 1, unit: "each", ...}, kinds in name order, ids in build order). Nothing here reads plugin step
evidence.

The engine runs in `drawing` mode: Studio validates the inverters its drawing assigns strings to. The plugin validates
its in-memory list, empty after the drawing opens, so the two differ by the declared G36 guardrails divergence.

Fails closed: a missing or malformed intake, an engine refusal, or a document the comparator refuses is a named error
and nothing is written.
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


compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")
engine = _load("solar_guardrails", ROOT / "server" / "solar_guardrails.py")

DEFAULT_INTAKES = ROOT / "docs" / "parity" / "evidence" / "batch2"
DEFAULT_OUT = Path("C:/tmp/solar-parity/b2-ev/studio")
FORMAT = "batch2-v1"                       # the plugin adapter's after.format
INTAKE = "g0-intake.json"
MAX_INTAKE_BYTES = 8 * 1024 * 1024
CAPABILITY = "guardrails-monitoring"
STEPS = ("g1", "g2")                       # GUARDRAILS, then GUARDRAILSVALIDATE: the same validation twice
MODE = "drawing"
FRAME = {"coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}


class EvidenceError(ValueError):
    pass


def read_intake(directory):
    path = Path(directory) / INTAKE
    if not path.is_file():
        raise EvidenceError(f"intake {INTAKE} is missing from {directory}")
    raw = path.read_bytes()
    if len(raw) > MAX_INTAKE_BYTES:
        raise EvidenceError(f"intake {INTAKE} exceeds {MAX_INTAKE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
        raise EvidenceError(f"intake {INTAKE} is not JSON: {exc}") from None


def _row(row_id, kind, fields):
    row = {"id": {"entity_id": row_id}, "type": kind, "quantity": 1, "unit": "each"}
    for key, value in fields.items():
        if key in row:
            raise EvidenceError(f"row {row_id} field {key!r} collides with a row key")
        row[key] = value
    return row


def build_document(step, intake, revision, mode=MODE):
    if step not in STEPS:
        raise EvidenceError(f"unknown step {step!r}")
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    try:
        by_kind = engine.guardrail_rows(intake, mode)
    except engine.GuardrailError as exc:
        raise EvidenceError(f"step {step}: {exc}") from None
    parameters = {"answers": []}
    rows, references = [], []
    for kind in sorted(by_kind):
        for row_id, fields in by_kind[kind]:
            rows.append(_row(row_id, kind, fields))
            references.append(row_id)
    if len(set(references)) != len(references):
        raise EvidenceError(f"step {step} emitted a duplicate row id")
    after = {"rows": rows, "source_revision": step, "format": FORMAT}
    try:
        fixture = compare.semantic_hash(intake)
        doc = {
            "fixture_sha256": fixture,
            "input_sha256": compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
            "output_sha256": compare.semantic_hash(after),
            "revision": revision,
            "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": "0",
                         "engine": "server-builtin", "catalog": "none", "solver": "none"},
            "parameters": parameters,
            "units": "in",
            "frame": json.loads(json.dumps(FRAME)),
            "entity_mapping": {ref: ref for ref in sorted(references)},
            "before": {"recorded": False},
            "after": after,
            "changes": {"created": [], "modified": [], "deleted": []},
            "warnings": [],
            "rejected_inputs": [],
            "provenance": {"side": "studio", "step": step, "capability": CAPABILITY, "list_mode": mode,
                           "engine": "server/solar_guardrails.py"},
            "elapsed_ms": 0,
            "execution_mode": "live",
            "state": "committed",
            "survived_reopen": True,
            "synthetic_fields": ["before/recorded", "changes/unrecorded", "palette/last_check"],
            "fallback_fields": [] if revision is not None else ["revision"],
            "synthetic_flagged": True,
        }
        probe = dict(doc, revision=revision or "0" * 40)
        compare.validate_evidence(probe, "exports")
    except compare.InputError as exc:
        raise EvidenceError(f"step {step} evidence refused by the comparator: {exc}") from None
    return doc


def run_steps(intakes_dir, revision=None, only=None):
    intake = read_intake(intakes_dir)
    return {step: build_document(step, intake, revision) for step in STEPS if only in (None, step)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--intakes", type=Path, default=DEFAULT_INTAKES, help="the folder holding the G36 intakes")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--step", choices=STEPS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs = run_steps(args.intakes, args.revision, args.step)
    except EvidenceError as exc:
        print(f"solar-guardrails-evidence: {exc}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for step, doc in docs.items():
        (args.out / f"{step}.json").write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        kinds = {}
        for row in doc["after"]["rows"]:
            kinds[row["type"]] = kinds.get(row["type"], 0) + 1
        print(f"{step} {CAPABILITY}: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
