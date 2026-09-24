#!/usr/bin/env python3
"""Studio's G35 evidence for the string steps i2 and i3.

Each step N starts from state-i(N-1).json (G13: the previous step's reopened state, in the plugin
adapter's G35 projection; default the committed copies under docs/parity/evidence/rooftop/inverters,
--states overrides), runs the Studio engine (server/solar_inverter_strings.py) with the G35 answers,
and writes step N's delta in the plugin adapter's shape (server/solar_inverter_state.py) to iN.json.

  i2  assign-strings  AssignStrings     Auto, the pattern matcher, to the central inverters; Excess
                                        Capacity Yes
  i3  color-strings   LEAFCOLORSTRINGS  every string recoloured by its inverter

Envelope: the comparator's exports family, the plugin adapter's parameters (G22 answers, G30a
form_values), fixture_sha256 the digest of the i0 state as written, input_sha256 over fixture and
parameters, engine "server-builtin", capability version "0", compact canonical JSON. Nothing here
reads plugin evidence (iN.json); only the G35 states, which are this producer's inputs by contract.
Fails closed: a malformed state, an engine refusal, or a document the comparator refuses is a named
error and nothing is written for that run.
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
strings = _load("solar_inverter_strings", ROOT / "server" / "solar_inverter_strings.py")
compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")

DEFAULT_STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
DEFAULT_OUT = Path("C:/tmp/solar-parity/inv-ev/studio")
CAPABILITY_VERSION = "0"

# Step -> (capability, command, Studio operation). G35.
STEPS = {"i2": ("assign-strings", "AssignStrings", "assign-strings"),
         "i3": ("color-strings", "LEAFCOLORSTRINGS", "color-strings")}
STEP_IDS = tuple(STEPS)
# G22 answers (command line) and G30a form_values (dialogs), verbatim from G35.
STEP_ANSWERS = {"i2": [], "i3": []}
STEP_FORM_VALUES = {
    "i2": {"assign_strings_to_central_inverters": "OK", "assign_strings_to_central_inverters_mode": "Auto",
           "excess_capacity": "Yes"},
}

# HOST INPUTS: the capture host's per-user settings, session state and block-definition quantities the
# commands read, none of which the drawing state carries. Each is named by the plugin setting it
# stands for and was measured from the capture (G35 raw folder: the command logs and the reopened
# states), not typed:
CAPTURE_HOST = {
    # G34b: the capture host's UseL2Collectors, true; UseCombinerBox false (the same host as i1).
    "UseL2Collectors": True,
    "UseCombinerBox": False,
    # i2 printed "Pattern assignment complete.", the pattern path's own line (BranchCmd.cs:14346).
    "UsePatternStringAssignment": True,
    # L2NumMppt x L2StringsPerMppt: every label i2 wrote records 6 MPPTs, 6 strings per MPPT and 36
    # strings on the inverter (the tag record, BranchCmd.cs:14741-14752).
    "L2NumMppt": 6,
    "L2StringsPerMppt": 6,
    # The host's NumMppt and StringsPerMppt: unrecorded one by one (i1 shows only their product, 24).
    # Read only when a collector has no L2 topology and the drawing none either; the fixture's
    # drawing holds NumMppt 3 and StringPerMppt 3, so neither is read here.
    "NumMppt": None,
    "StringsPerMppt": None,
    # CombinerBoxConnections: the input count every combiner block of the capture carries (20).
    "CombinerBoxConnections": 20,
    # App.mColorCtr at the command's start: 1, its initial value (App.cs:93); every step ran in a
    # freshly started AutoCAD (each command log opens with the menu and CUI load). i2's collectors took
    # 2, 5, 3, 6, 4 in collector order: the untyped family from index 1.
    "SessionColorCounter": 1,
    # 0.4 x the panel definition's short side (StringPlacement.cs:343-347): the value the fixture's
    # settings already hold, which i2's save rewrote unchanged (no TagHeight row in the capture).
    "AutoTagHeight": 20.750786408000746,
    # The NUMBER attribute of each device, as i1's command log printed it ("inverter #n at (x, y)"),
    # one decimal place.
    "DeviceNumbers": [
        {"position": [14298.6, 1644.1], "number": 1}, {"position": [16758.4, 1576.1], "number": 2},
        {"position": [18680.2, 2030.8], "number": 3}, {"position": [20260.9, 2025.7], "number": 4},
        {"position": [14522.6, 3955.0], "number": 5}, {"position": [16613.1, 4121.3], "number": 6},
        {"position": [18437.0, 3800.2], "number": 7}, {"position": [20260.9, 3955.0], "number": 8},
    ],
}


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


def _serialize(doc):
    """Compact canonical JSON (the comparator reader's 2 MB bound)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def run_engine(step, state, host):
    """(after state, printed lines) of one step's Studio engine."""
    if step == "i2":
        return strings.assign_strings(state, host, STEP_FORM_VALUES["i2"])
    if step == "i3":
        return strings.color_strings(state, host)
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
    capability, command, operation = STEPS[step]
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
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": input_hash,
        "output_sha256": output_hash,
        "revision": revision,
        "versions": {"schema": st.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_inverter_strings
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
        "provenance": {"side": "studio", "step": step, "capability": capability, "command": command,
                       "operation": operation,
                       "before_state_sha256": st.digest(st.publish(before)),
                       "after_state_sha256": st.digest(st.publish(after))},
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


def run_steps(states_dir, host=None, revision=None, only=None):
    """{step: document} for every owned step (or only `only`), each from state-i(N-1).json."""
    states_dir = Path(states_dir)
    host = dict(CAPTURE_HOST if host is None else host)
    if only is not None and only not in STEPS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    try:
        fixture = st.digest(st.publish(st.load_state(states_dir / "state-i0.json")))
        out = {}
        for step in STEP_IDS:
            if only is not None and step != only:
                continue
            number = int(step[1:])
            before = st.load_state(states_dir / f"state-i{number - 1}.json")
            after, lines = run_engine(step, before, host)
            rows, settings = st.step_rows(step, before, after, lines)
            out[step] = build_document(step, fixture, before, after, rows, settings, revision)
        return out
    except OSError as exc:
        raise EvidenceError(f"a state is unreadable: {exc}") from None
    except (st.InverterStateError, strings.InverterStringError) as exc:
        raise EvidenceError(str(exc)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G35 evidence for i2 and i3.")
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES,
                        help="the folder holding state-iN.json (default the committed copies)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs = run_steps(args.states, revision=args.revision, only=args.step)
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
        print(f"solar-inverter-strings-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
