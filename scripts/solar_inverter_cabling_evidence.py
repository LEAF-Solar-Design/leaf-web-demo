#!/usr/bin/env python3
"""Studio's G35 evidence for the inverter cabling steps i5, i11, i12, i13, i17 and inverter-position.

Each step N starts from state-i(N-1).json (G13: the previous step's reopened state, in the plugin
adapter's G35 projection; default the committed copies under docs/parity/evidence/rooftop/inverters,
--states overrides), runs the Studio engine (server/solar_inverter_cabling.py) with the G35 answers,
and writes step N's delta in the plugin adapter's shape (server/solar_inverter_state.py) to iN.json.

  i5        combiner-auto-place          LEAFCOMBINERAUTO   not ported in this slice (the engine refuses,
                                                            named; a later slice ports it)
  i11       route-l2-feeders             RouteL2Feeders     every feeder adopted: report
  i12       homeruns                     HomerunsAuto       homeruns redrawn identical: report
  i13       lightweight-cabling-feeders  LEAFLITEFEEDERS    feeders reassigned, redrawn as comb paths
  i17       inverter-move                MOVEINV            declared: moved, homeruns rerouted
  position  inverter-position            POSITIONINV        declared: device 14 from state-i16, bounded
                                                            search, written to position.json

The panel-group outlines the feeder lanes and the position search read live in the panel groups'
block definitions, which the G35 state does not carry; they come from the committed rooftop chain
intake of the same fixture (docs/parity/evidence/rooftop/chain/intake.json, --intake overrides).

Envelope: the plugin adapter's parameters (G22 answers, G30a form_values), fixture_sha256 the digest of
the i0 state as written, input_sha256 over fixture and parameters, engine "server-builtin", capability
version "0", compact canonical JSON (the S70 producer's envelope, scripts/solar_inverter_devices_evidence.py).
Nothing here reads plugin evidence (iN.json); only the G35 states. Fails closed: a malformed state or a
document the comparator refuses is a named error and nothing is written; a step whose engine is not
ported is named on stderr and nothing is written for it. The exit status is 0 when every refused step
is one a later slice owns (OUT_OF_SLICE, i5), else 3.
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
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


st = _load("solar_inverter_state", ROOT / "server" / "solar_inverter_state.py")
cabling = _load("solar_inverter_cabling", ROOT / "server" / "solar_inverter_cabling.py")
compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")

DEFAULT_STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "chain" / "intake.json"
DEFAULT_OUT = Path("C:/tmp/solar-parity/inv-ev/studio")
CAPABILITY_VERSION = "0"
MAX_INTAKE_BYTES = 32 * 1024 * 1024
EXIT_NOT_PORTED = 3
# Steps a later slice ports (G35: LEAFCOMBINERAUTO); their named refusal does not fail this producer.
OUT_OF_SLICE = frozenset({"i5"})

# Step -> (capability, command, Studio operation, before state number). G35.
STEPS = {"i5": ("combiner-auto-place", "LEAFCOMBINERAUTO", "combiner-auto-place", 4),
         "i11": ("route-l2-feeders", "RouteL2Feeders", "route-l2-feeders", 10),
         "i12": ("homeruns", "HomerunsAuto", "homeruns", 11),
         "i13": ("lightweight-cabling-feeders", "LEAFLITEFEEDERS", "lightweight-cabling-feeders", 12),
         "i17": ("inverter-move", "MOVEINV", "inverter-move", 16),
         "position": ("inverter-position", "POSITIONINV", "inverter-position", 16)}
STEP_IDS = tuple(STEPS)
DECLARED = frozenset({"i17", "position"})
# G22 answers (command line) and G30a form_values (dialogs), verbatim from G35 (inverter_evidence.py:
# 145-159); POSITIONINV's one answer is the device it positions.
STEP_ANSWERS = {"i5": [], "i11": [], "i12": [], "i13": [], "i17": ["A9D5", "20153.4,3589.19,0"],
                "position": ["14"]}
STEP_FORM_VALUES = {"i5": {"combiner_input_plan": "Apply"}}
# G35b: the engine's MOVEINV point is the point the jig acquired, not the typed text: the capture typed
# "20153.4,3589.19,0" under running object snaps and AutoCAD delivered the snapped panel vertex below
# (the committed device position; InverterMoveJig.cs:51-69 takes it as is). Object snap is host input no
# state carries, so both sides use the acquired point; the recorded parameters stay the G22 answers.
ACQUIRED_MOVE_POINT = "20150.90569654952,3589.187227900471"
ENGINE_ANSWERS = {"i17": ["A9D5", ACQUIRED_MOVE_POINT]}

# HOST INPUTS: the capture host's per-user settings and the picked entities the commands read, none of
# which the drawing state carries; each named by the plugin value it stands for and measured from the
# capture (G35 raw folder: the command logs and the reopened states), not typed:
CAPTURE_HOST = {
    # G34b: the capture host's UseL2Collectors (host-settings.json of the same host), true.
    "UseL2Collectors": True,
    # L1CollectorsPerL2: i10 printed "8 L2 x 4 slots".
    "L1CollectorsPerL2": 4,
    # SwitchgearReader.ReadRackExtents: the rooftop fixture holds no rack (G35a: no tracker rows), so
    # LEAFLITEFEEDERS derives no lane; i13's paths drop at each inverter's X, which only a lane-less run
    # produces.
    "RackExtents": [],
    # MOVEINV's picked block A9D5 is the combiner LEAFCOMBINERAUTO created at i5 whose feeder circuit is
    # F14/8 in state-i16 (the handle never leaves the plugin adapter; the number does).
    "MovedDevice": ["L1", 14],
    # G35: POSITIONINV of device 14 from state i16.
    "PositionDevice": ["L1", 14],
}


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


def _serialize(doc):
    """Compact canonical JSON (the comparator reader's 2 MB bound)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def load_intake_groups(path):
    """The chain intake's panel groups: [{handle, outlines}] in GetPanelGroupData order."""
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_INTAKE_BYTES + 1)
    if len(raw) > MAX_INTAKE_BYTES:
        raise EvidenceError(f"{path.name} exceeds {MAX_INTAKE_BYTES} bytes")
    try:
        intake = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path.name} is not UTF-8 JSON: {exc}") from None
    groups = intake.get("panel_groups") if isinstance(intake, dict) else None
    if not isinstance(groups, list):
        raise EvidenceError(f"{path.name} carries no panel_groups")
    return [{"handle": g.get("handle"), "outlines": g.get("outlines") or []} for g in groups if isinstance(g, dict)]


def run_engine(step, state, panel_groups, host):
    """(after state, printed lines) of one step's Studio engine."""
    answers, forms = ENGINE_ANSWERS.get(step, STEP_ANSWERS[step]), STEP_FORM_VALUES.get(step, {})
    if step == "i5":
        return cabling.combiner_auto_place(state, panel_groups, host, forms)
    if step == "i11":
        return cabling.route_l2_feeders(state, panel_groups, host)
    if step == "i12":
        return cabling.homeruns_auto(state, panel_groups, host)
    if step == "i13":
        return cabling.lite_feeders(state, host)
    if step == "i17":
        return cabling.inverter_move(state, host, answers)
    if step == "position":
        return cabling.inverter_position(state, panel_groups, host)
    raise EvidenceError(f"step {step!r} is not one of {STEP_IDS}")


def parameters_for(step):
    parameters = {"answers": list(STEP_ANSWERS[step])}
    if step in STEP_FORM_VALUES:
        parameters["form_values"] = dict(STEP_FORM_VALUES[step])
    return parameters


def build_document(step, fixture, before, after, rows, settings, revision):
    """One G35 exports document in the plugin adapter's shape, validated by the comparator."""
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lower-case git commit id")
    capability, command, operation, _ = STEPS[step]
    parameters = parameters_for(step)
    body = {"rows": rows, "source_revision": step, "format": st.FORMAT}
    references = [r["id"]["entity_id"] for r in rows]
    if len(set(references)) != len(references):
        raise EvidenceError(f"step {step} emitted a duplicate row id")
    try:
        input_hash = st.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}, "input")
        output_hash = st.semantic_hash(body, f"{step} after")
    except st.InverterStateError as exc:
        raise EvidenceError(str(exc)) from None
    synthetic = ["before/recorded", "changes/unrecorded"]
    if any(record["fields"]["name"] == st.ROUTE_SETTING for record in settings):
        synthetic.append(f"after.rows.setting-{st.ROUTE_SETTING}.value.{st.ROUTE_CLOCK}")
    provenance = {"side": "studio", "step": step, "capability": capability, "command": command,
                  "operation": operation,
                  "before_state_sha256": st.digest(st.publish(before)),
                  "after_state_sha256": st.digest(st.publish(after))}
    if step in DECLARED:
        provenance["declared_divergence"] = True
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": input_hash,
        "output_sha256": output_hash,
        "revision": revision,
        "versions": {"schema": st.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_inverter_cabling
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": st.UNITS,
        "frame": json.loads(json.dumps(st.FRAME)),
        "entity_mapping": {ref: ref for ref in sorted(references)},  # G8
        "before": {"recorded": False},
        "after": body,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": provenance,
        "elapsed_ms": 0,
        "execution_mode": "live",
        "state": "committed",
        "survived_reopen": True,
        "synthetic_fields": synthetic,
        "fallback_fields": [] if revision is not None else ["revision"],
        "synthetic_flagged": True,
    }
    probe = dict(doc, revision=revision or "0" * 40)
    try:
        compare.validate_evidence(probe, "exports")
        compare._normalize(doc["after"], doc["entity_mapping"])
    except compare.InputError as exc:
        raise EvidenceError(f"step {step} evidence refused by the comparator: {exc}") from None
    return doc


def run_steps(states_dir, panel_groups, host=None, revision=None, only=None):
    """({step: document}, {step: not-ported reason}) for every owned step (or only `only`)."""
    states_dir = Path(states_dir)
    host = dict(CAPTURE_HOST if host is None else host)
    if only is not None and only not in STEPS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    try:
        fixture = st.digest(st.publish(st.load_state(states_dir / "state-i0.json")))
        out, refused = {}, {}
        for step in STEP_IDS:
            if only is not None and step != only:
                continue
            before = st.load_state(states_dir / f"state-i{STEPS[step][3]}.json")
            try:
                after, lines = run_engine(step, before, panel_groups, host)
            except cabling.InverterCablingNotPortedError as exc:
                refused[step] = str(exc)
                continue
            rows, settings = st.step_rows(step, before, after, lines)
            out[step] = build_document(step, fixture, before, after, rows, settings, revision)
        return out, refused
    except OSError as exc:
        raise EvidenceError(f"a state is unreadable: {exc}") from None
    except (st.InverterStateError, cabling.InverterCablingError) as exc:
        raise EvidenceError(str(exc)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G35 evidence for i5, i11, i12, i13, i17 and position.")
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES,
                        help="the folder holding state-iN.json (default the committed copies)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE,
                        help="the rooftop chain intake carrying the panel-group outlines")
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs, refused = run_steps(args.states, load_intake_groups(args.intake), revision=args.revision,
                                  only=args.step)
        args.out.mkdir(parents=True, exist_ok=True)
        for step, doc in docs.items():
            target = args.out / f"{step}.json"
            target.write_text(_serialize(doc), encoding="utf-8")
            if json.loads(target.read_text(encoding="utf-8")) != json.loads(_serialize(doc)):
                raise EvidenceError(f"{target} did not read back as written")
            counts = {}
            for item in doc["after"]["rows"]:
                counts[item["type"]] = counts.get(item["type"], 0) + 1
            print(f"{step} {STEPS[step][0]}: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no rows"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-inverter-cabling-evidence: {exc}", file=sys.stderr)
        return 2
    for step, reason in refused.items():
        print(f"solar-inverter-cabling-evidence: {step} {STEPS[step][0]} not written: not ported: {reason}",
              file=sys.stderr)
    return EXIT_NOT_PORTED if set(refused) - OUT_OF_SLICE else 0


if __name__ == "__main__":
    raise SystemExit(main())
