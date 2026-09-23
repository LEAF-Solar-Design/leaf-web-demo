#!/usr/bin/env python3
"""Studio's G34/G34a/G34b evidence for the Rooftop workflow flow (capability leaf-workflow-palette).

One document per fixture state (G34: the rooftop unsplit fixture, the zones fixture, the rooftop
solve fixture), computed from that state's drawing-facts intake by the pure progression engine
server/solar_workflow_flow.py (the literal port of the plugin's Rooftop flow; its docstring
names every intake field's source). The intakes are the G34 drawing-facts files
(docs/parity/evidence/rooftop/flow/{unsplit,zsplit,full_run}.json) carrying G34b's host input
`host_use_l2_collectors`, read exactly as they stand. Nothing here reads plugin output.

Rows (family exports; every row {id, type, quantity: 1, unit: "each", ...}):
  flow-step   one per Rooftop step in order, ids flow-step-1 .. flow-step-8: flow ("rooftop"),
              index (0-based), step (zones, panel-groups, solve, inverters, combiners, homeruns,
              export, reopt), can_advance (bool). G34a: no has_prerequisites field.
  flow-event  on the zones fixture only (G34a runs the event script there; --reopen-intake is the
              unsplit fixture's intake), ids flow-event-1 ..: after the per-step capture, "jump" to
              the first step whose can_advance is true, "advance" until refused (each also carries
              `advanced`), one "back", a "reopen" of the same state, then a "reopen" of the unsplit
              state with the same persisted state: event, current_index, statuses (8 entries of
              not-started | complete | stale, the per-user file's view)
Rows are emitted sorted by type, then numeric id (G9). The flow carries no geometry, so `units`
is the rooftop fixtures' drawing unit ("in", G27) and no row holds a length.

Envelope (G34b, both sides): parameters {"flow": "rooftop", "fixture": unsplit | zones | solve}
plus "reopen_fixture": "unsplit" for zones; after.format "flow-v1"; after.source_revision
"flow-<fixture>"; fixture_sha256 the canonical hash of the fixture's own intake file (the reopen
intake is named by `reopen_fixture`, not hashed in); input_sha256 per G17 over fixture and
parameters.

The document is compact JSON (sorted keys, no whitespace), versions.engine "server-builtin" (the
gate's engine vocabulary; the module is solar_workflow_flow) and versions.capability "0".

Fails closed: a malformed intake, an engine refusal or a document the comparator refuses is a
named error; nothing is written.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
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
flow = _load("solar_workflow_flow", ROOT / "server" / "solar_workflow_flow.py")

CAPABILITY = "leaf-workflow-palette"
CAPABILITY_VERSION = "0"
ENGINE = "server-builtin"
FAMILY = "exports"
FORMAT = "flow-v1"                                                                 # G34b
UNITS = "in"
FRAME = {"coordinate_system": "world", "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": "none"}                          # contract rule 6
# G34b's three fixture names; only the zones fixture runs the event script, reopening the unsplit one.
FIXTURES = ("unsplit", "zones", "solve")
EVENT_FIXTURE = "zones"
REOPEN_FIXTURE = "unsplit"
MAX_INTAKE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT = 20


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


def _row(row_id, row_type, **fields):
    return {"id": row_id, "type": row_type, "quantity": 1, "unit": "each", **fields}


def flow_step_rows(facts):
    """G34 `flow-step` rows for one drawing state."""
    try:
        steps = flow.flow_steps(facts)
    except flow.FlowInputError as exc:
        raise EvidenceError(f"the flow intake is refused: {exc}") from None
    return [_row(f"flow-step-{s['index'] + 1}", "flow-step", **s) for s in steps]


def flow_event_rows(facts, reopen_facts):
    """G34a `flow-event` rows: the event script on one drawing state, the last reopen on
    `reopen_facts`."""
    try:
        events = flow.flow_events(facts, reopen_facts)
    except flow.FlowInputError as exc:
        raise EvidenceError(f"the flow intake is refused: {exc}") from None
    return [_row(f"flow-event-{n}", "flow-event", **e) for n, e in enumerate(events, 1)]


def _row_order(row):
    suffix = row["id"][len(row["type"]) + 1:]
    return (row["type"], (0, int(suffix), "") if suffix.isdigit() else (1, 0, suffix))


def serialize(doc):
    """Compact canonical JSON."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def build_document(facts, revision, fixture, reopen_facts=None):
    """One G34 `exports` evidence document for fixture `fixture`'s drawing-facts intake, in the
    G34b envelope: flow-step rows, plus the event script (on the zones fixture only, whose
    `reopen_facts` is the unsplit fixture's intake). Validated under the comparator's bounds."""
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lowercase git commit")
    if fixture not in FIXTURES:
        raise EvidenceError(f"fixture {fixture!r} is not one of {FIXTURES}")
    if (fixture == EVENT_FIXTURE) != (reopen_facts is not None):
        raise EvidenceError(f"the {EVENT_FIXTURE} fixture, and only it, takes the {REOPEN_FIXTURE} "
                            "fixture's intake as its reopen intake")
    rows = flow_step_rows(facts)
    parameters = {"flow": flow.FLOW, "fixture": fixture}
    if reopen_facts is not None:
        rows += flow_event_rows(facts, reopen_facts)
        parameters["reopen_fixture"] = REOPEN_FIXTURE
    rows = sorted(rows, key=_row_order)
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise EvidenceError("the flow emitted a duplicate row id")
    after = {"rows": [dict(row, id={"entity_id": row["id"]}) for row in rows],
             "source_revision": f"flow-{fixture}", "format": FORMAT}
    try:
        fixture_sha = compare.semantic_hash(facts)                                 # the fixture's own intake
        hashes = (compare.semantic_hash({"fixture_sha256": fixture_sha, "parameters": parameters}),
                  compare.semantic_hash(after))
    except compare.InputError as exc:
        raise EvidenceError(f"the flow evidence is refused by the comparator: {exc}") from None
    provenance = {"side": "studio", "capability": CAPABILITY, "flow": flow.FLOW, "operation": "flow-progression"}
    doc = {
        "fixture_sha256": fixture_sha,
        "input_sha256": hashes[0],
        "output_sha256": hashes[1],
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": ENGINE, "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": UNITS,
        "frame": json.loads(json.dumps(FRAME)),
        "entity_mapping": {ref: ref for ref in sorted(ids)},  # G8
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": provenance,
        "elapsed_ms": 0,
        "execution_mode": "live",
        "state": "committed",
        "survived_reopen": True,
        "synthetic_fields": ["before/recorded", "changes/unrecorded"],
        "fallback_fields": [],
        "synthetic_flagged": True,
    }
    try:
        compare.validate_evidence(doc, FAMILY)
        compare._normalize(doc["after"], doc["entity_mapping"])
    except compare.InputError as exc:
        raise EvidenceError(f"the flow evidence is refused by the comparator: {exc}") from None
    if len(serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError(f"the flow evidence exceeds {compare.MAX_BYTES} bytes")
    return doc


def read_intake(path):
    """The intake file as UTF-8 JSON, bounded."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) > MAX_INTAKE_BYTES:
        raise EvidenceError(f"{path} exceeds {MAX_INTAKE_BYTES} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError(f"{path} is not UTF-8 JSON: {exc}") from None


def intake_revision(path):
    """Rule 2: the last commit touching the intake, in the repository that holds it. An
    untracked file or no git is an error, never a default."""
    path = Path(path).resolve()

    def git(*args):
        return subprocess.run(["git", *args], cwd=path.parent, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=True).stdout.strip()
    try:
        git("ls-files", "--error-unmatch", "--", path.name)
        rev = git("log", "-1", "--format=%H", "--", path.name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"intake revision unavailable: {exc}") from None
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise EvidenceError("intake has no committed revision")
    return rev


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G34 Rooftop flow evidence (flow-step and flow-event "
                                                 "rows) from a drawing-facts intake.")
    parser.add_argument("--intake", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fixture", choices=FIXTURES, required=True)
    parser.add_argument("--reopen-intake", type=Path,
                        help="the zones fixture's second reopen: the unsplit fixture's drawing-facts intake "
                             "(required for, and only for, --fixture zones); adds the G34a flow-event rows")
    parser.add_argument("--revision", help="the intake's 40-hex commit (default: git log on the intake)")
    args = parser.parse_args(argv)
    try:
        facts = read_intake(args.intake)
        reopen_facts = read_intake(args.reopen_intake) if args.reopen_intake is not None else None
        revision = args.revision if args.revision is not None else intake_revision(args.intake)
        doc = build_document(facts, revision, args.fixture, reopen_facts)
        text = serialize(doc)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        if compare.load_evidence(args.out) != json.loads(text):
            raise EvidenceError(f"{args.out} did not read back as written")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-workflow-flow-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
