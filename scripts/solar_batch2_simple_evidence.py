"""Studio's G36 evidence for the simple batch-2 steps: z1 s2 s3 s4 f1 q1.

Each step reads only its committed, name-free intake (default docs/parity/evidence/batch2/,
`--intakes` overrides; the Branch2025 adapter tools/parity/batch2_evidence.py writes them), runs the
engine in server/solar_batch2_simple.py and writes `<step>.json` in the G36 `exports` shape the
plugin adapter uses (rows {id, type, quantity: 1, unit: "each", ...}, kinds in name order, ids in
build order). Nothing here reads plugin step evidence.

  z1  zone-height-settings      z0-state.json    form_values {add: 1, offset_in: 24}
  s2  shadow-curtain            s1-markers.json
  s3  shade-loss-heatmap        s4-state.json
  s4  shade-loss-heatmap-clear  s4-state.json
  f1  frame-information         f0-intake.json
  q1  deep-search-status        (no intake: Studio keeps no deep-search sessions for this drawing)

Fails closed: a missing or malformed intake, an engine refusal, or a document the comparator
refuses is a named error and nothing is written for that step.
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
engine = _load("solar_batch2_simple", ROOT / "server" / "solar_batch2_simple.py")

DEFAULT_INTAKES = ROOT / "docs" / "parity" / "evidence" / "batch2"
DEFAULT_OUT = Path("C:/tmp/solar-parity/b2-ev/studio")
FORMAT = "batch2-v1"                       # the plugin adapter's after.format
MAX_INTAKE_BYTES = 8 * 1024 * 1024
# step -> (capability, units, intake file or None, form_values)
STEPS = {
    "z1": ("zone-height-settings", "in", "z0-state.json", {"add": 1, "offset_in": 24}),
    "s2": ("shadow-curtain", "m", "s1-markers.json", None),
    "s3": ("shade-loss-heatmap", "m", "s4-state.json", None),
    "s4": ("shade-loss-heatmap-clear", "m", "s4-state.json", None),
    "f1": ("frame-information", "m", "f0-intake.json", None),
    "q1": ("deep-search-status", "in", None, None),
    # G36 addendum: LEAFFRAME, OK then Cancel, over the same frame store intake as f1.
    "f2": ("frame-park-settings", "m", "f0-intake.json", {"ok": 1, "cancel": 1}),
}
# The plugin adapter's frame (ground_evidence.FRAME): world coordinates, identity transform.
FRAME = {"coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}
# q1 has no intake; the plugin adapter hashes this marker as its fixture (batch2_evidence.py q1).
Q1_FIXTURE_VALUE = {"deep_search_sessions": None}


class EvidenceError(ValueError):
    pass


def read_intake(directory, name):
    path = Path(directory) / name
    if not path.is_file():
        raise EvidenceError(f"intake {name} is missing from {directory}")
    raw = path.read_bytes()
    if len(raw) > MAX_INTAKE_BYTES:
        raise EvidenceError(f"intake {name} exceeds {MAX_INTAKE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError as exc:
        raise EvidenceError(f"intake {name} is not JSON: {exc}") from None


def _row(row_id, kind, fields):
    row = {"id": {"entity_id": row_id}, "type": kind, "quantity": 1, "unit": "each"}
    for key, value in fields.items():
        if key in row:
            raise EvidenceError(f"row {row_id} field {key!r} collides with a row key")
        row[key] = value
    return row


def step_rows(step, intake):
    """{kind: [(id, fields)]} for one step, from its intake."""
    try:
        if step == "z1":
            zones = engine.zone_height(intake, STEPS["z1"][3])
            rows = engine.zone_rows(intake, zones)
            return {"elevation-zone": rows} if rows else {}
        if step == "s2":
            return {"report": engine.shadow_curtain(intake)}
        if step == "s3":
            return {"report": engine.shade_loss_heatmap(intake)}
        if step == "s4":
            return {"report": engine.shade_loss_heatmap_clear(intake)}
        if step == "f1":
            return {"frame-info": engine.frame_rows(intake)}
        if step == "f2":
            return {"report": engine.frame_park_settings(intake, STEPS["f2"][3])}
        if step == "q1":
            return {"report": engine.deep_search_status([])}
    except engine.BatchTwoError as exc:
        raise EvidenceError(f"step {step}: {exc}") from None
    raise EvidenceError(f"unknown step {step!r}")


def build_document(step, intake, revision):
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    capability, units, _, form_values = STEPS[step]
    parameters = {"answers": []}
    if form_values is not None:
        parameters["form_values"] = dict(form_values)
    rows, references = [], []
    by_kind = step_rows(step, intake)
    for kind in sorted(by_kind):
        for row_id, fields in by_kind[kind]:
            rows.append(_row(row_id, kind, fields))
            references.append(row_id)
    if len(set(references)) != len(references):
        raise EvidenceError(f"step {step} emitted a duplicate row id")
    after = {"rows": rows, "source_revision": step, "format": FORMAT}
    fixture_value = Q1_FIXTURE_VALUE if intake is None else intake
    try:
        fixture = compare.semantic_hash(fixture_value)
        doc = {
            "fixture_sha256": fixture,
            "input_sha256": compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
            "output_sha256": compare.semantic_hash(after),
            "revision": revision,
            "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": "0",
                         "engine": "server-builtin", "catalog": "none", "solver": "none"},
            "parameters": parameters,
            "units": units,
            "frame": json.loads(json.dumps(FRAME)),
            "entity_mapping": {ref: ref for ref in sorted(references)},
            "before": {"recorded": False},
            "after": after,
            "changes": {"created": [], "modified": [], "deleted": []},
            "warnings": [],
            "rejected_inputs": [],
            "provenance": {"side": "studio", "step": step, "capability": capability,
                           "engine": "server/solar_batch2_simple.py"},
            "elapsed_ms": 0,
            "execution_mode": "live",
            "state": "committed",
            "survived_reopen": True,
            "synthetic_fields": ["before/recorded", "changes/unrecorded"],
            "fallback_fields": [] if revision is not None else ["revision"],
            "synthetic_flagged": True,
        }
        probe = dict(doc, revision=revision or "0" * 40)
        compare.validate_evidence(probe, "exports")
    except compare.InputError as exc:
        raise EvidenceError(f"step {step} evidence refused by the comparator: {exc}") from None
    return doc


def run_steps(intakes_dir, revision=None, only=None):
    out = {}
    for step, (_, _, intake_name, _) in STEPS.items():
        if only is not None and step != only:
            continue
        intake = None if intake_name is None else read_intake(intakes_dir, intake_name)
        out[step] = build_document(step, intake, revision)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--intakes", type=Path, default=DEFAULT_INTAKES, help="the folder holding the G36 intakes")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--step", choices=tuple(STEPS))
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs = run_steps(args.intakes, args.revision, args.step)
    except EvidenceError as exc:
        print(f"solar-batch2-simple-evidence: {exc}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for step, doc in docs.items():
        (args.out / f"{step}.json").write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n",
                                               encoding="utf-8")
        kinds = {}
        for row in doc["after"]["rows"]:
            kinds[row["type"]] = kinds.get(row["type"], 0) + 1
        print(f"{step} {STEPS[step][0]}: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
