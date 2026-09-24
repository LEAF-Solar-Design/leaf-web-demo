#!/usr/bin/env python3
"""Studio's G35 evidence for the inverter cabling steps i5, i11, i12, i13, i17 and inverter-position.

Each step N starts from state-i(N-1).json (G13: the previous step's reopened state, in the plugin
adapter's G35 projection; default the committed copies under docs/parity/evidence/rooftop/inverters,
--states overrides), runs the Studio engine (server/solar_inverter_cabling.py) with the G35 answers,
and writes step N's delta in the plugin adapter's shape (server/solar_inverter_state.py) to iN.json.

  i5        combiner-auto-place          LEAFCOMBINERAUTO   placed (G35c intake), associated, homeruns and
                                                            feeders drawn
  i11       route-l2-feeders             RouteL2Feeders     every feeder adopted: report
  i12       homeruns                     HomerunsAuto       homeruns redrawn identical: report
  i13       lightweight-cabling-feeders  LEAFLITEFEEDERS    feeders reassigned, redrawn as comb paths
  i17       inverter-move                MOVEINV            declared: moved, homeruns rerouted
  position  inverter-position            POSITIONINV        declared: device 14 from state-i16, bounded
                                                            search, written to position.json

The panel-group outlines the feeder lanes and the position search read live in the panel groups'
block definitions, which the G35 state does not carry; they come from the committed rooftop chain
intake of the same fixture (docs/parity/evidence/rooftop/chain/intake.json, --intake overrides). i5
also reads the committed combiner intake (G35c: the command's input-before-placement dump, projected;
docs/parity/evidence/rooftop/inverters/combiner-intake.json, --combiner-intake overrides).

Envelope: the plugin adapter's parameters (G22 answers, G30a form_values), fixture_sha256 the digest of
the i0 state as written, input_sha256 over fixture and parameters, engine "server-builtin", capability
version "0", compact canonical JSON (the S70 producer's envelope, scripts/solar_inverter_devices_evidence.py).
Nothing here reads plugin evidence (iN.json); only the G35 states. Fails closed: a malformed state or a
document the comparator refuses is a named error and nothing is written; a step whose engine is not
ported is named on stderr and nothing is written for it. The exit status is 0 when every refused step
is one a later slice owns (OUT_OF_SLICE, none now), else 3.
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
DEFAULT_COMBINER_INTAKE = DEFAULT_STATES / "combiner-intake.json"
DEFAULT_OUT = Path("C:/tmp/solar-parity/inv-ev/studio")
CAPABILITY_VERSION = "0"
MAX_INTAKE_BYTES = 32 * 1024 * 1024
EXIT_NOT_PORTED = 3
# Steps a later slice ports; their named refusal does not fail this producer. None: G35c ported i5.
OUT_OF_SLICE = frozenset()

# Step -> (capability, command, Studio operation, before state number). G35.
STEPS = {"i5": ("combiner-auto-place", "LEAFCOMBINERAUTO", "combiner-auto-place", 4),
         "i11": ("route-l2-feeders", "RouteL2Feeders", "route-l2-feeders", 10),
         "i12": ("homeruns", "HomerunsAuto", "homeruns", 11),
         "i13": ("lightweight-cabling-feeders", "LEAFLITEFEEDERS", "lightweight-cabling-feeders", 12),
         "i17": ("inverter-move", "MOVEINV", "inverter-move", 16),
         "position": ("inverter-position", "POSITIONINV", "inverter-position", 16),
         # G36: the cabling studio on the i19 drawing: l1 opens it, l2 opens it again, Simulates and Commits.
         "l1": ("lightweight-cabling-studio", "LEAFLITEPLACE", "lightweight-cabling-studio", 19),
         "l2": ("lightweight-cabling-studio", "LEAFLITEPLACE", "lightweight-cabling-studio", 19)}
# A step whose before state is another step's Studio after state (G13), not a committed plugin state.
CHAINED = {"l2": "l1"}
STEP_IDS = tuple(STEPS)
DECLARED = frozenset({"i17", "position"})
# G22 answers (command line) and G30a form_values (dialogs), verbatim from G35 (inverter_evidence.py:
# 145-159); POSITIONINV's one answer is the handle the capture gave its prompt, the picked block A9D5
# (as MOVEINV's), not the device number; the engine resolves the device from host PositionDevice.
STEP_ANSWERS = {"i5": [], "i11": [], "i12": [], "i13": [], "i17": ["A9D5", "20153.4,3589.19,0"],
                "position": ["A9D5"], "l1": [], "l2": []}
STEP_FORM_VALUES = {"i5": {"combiner_input_plan": "Apply"},
                    "l2": {"cabling_redesign_simulate": "Simulate", "cabling_redesign_commit": "Commit"}}
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
    # EquipmentSymbolScale of a combiner block (the module scale over the block definition's native size,
    # host and block-definition quantities): every combiner block the capture holds carries exactly this
    # (the devices producer's CombinerSymbolScale, scripts/solar_inverter_devices_evidence.py).
    "CombinerSymbolScale": 2.891214911191,
    # SwitchgearReader.ReadRackExtents: the rooftop fixture holds no rack (G35a: no tracker rows), so
    # LEAFLITEFEEDERS derives no lane; i13's paths drop at each inverter's X, which only a lane-less run
    # produces.
    "RackExtents": [],
    # MOVEINV's picked block A9D5 is the combiner LEAFCOMBINERAUTO created at i5 whose feeder circuit is
    # F14/8 in state-i16 (the handle never leaves the plugin adapter; the number does).
    "MovedDevice": ["L1", 14],
    # G35: POSITIONINV of device 14 (the picked block A9D5) from state i16.
    "PositionDevice": ["L1", 14],
}


# HOST INPUTS of the batch-2 studio capture (G36), a later AutoCAD session on the same host:
LITE_HOST = dict(
    CAPTURE_HOST,
    # EquipmentSymbolScale of a combiner block in that session: all 173 combiners l2 committed carry it.
    CombinerSymbolScale=8.673644733572,
    # The L2 blocks' NUMBER attributes (LEAFADOPTL2INVERTERS renumbered them 9..31 at i19): the number each
    # l2 feeder's circuit names at the inverter its last vertex lands on; the one inverter no feeder reaches
    # (the engine's idle unit) takes the one number left, 14.
    L2Numbers=[[14901.8327700701, 3681.255695906479, 9], [14846.65419864152, 3355.060294967476, 10],
               [16924.46559667041, 3989.05109401393, 11], [18311.60743636922, 3932.459860849482, 12],
               [19821.65569654869, 3704.800787681172, 13], [20150.90569654952, 3589.187227900471, 14],
               [20260.85195774109, 3589.187227900471, 15], [14126.56588605084, 2046.065855186917, 16],
               [14281.0658860496, 1410.191276031353, 17], [14298.59861051802, 1644.100118557831, 18],
               [14340.48896297348, 2246.166246920207, 19], [14522.5827700701, 3954.99625300923, 20],
               [14670.08277007158, 3970.289594996278, 21], [16543.08713383614, 1828.816546064558, 22],
               [16613.12670764324, 4121.285469013931, 23], [16758.3563267214, 1576.097866801099, 24],
               [18436.98933559365, 3800.225486334549, 25], [18439.06415238894, 2175.590566676287, 26],
               [18680.17286698163, 2030.831390476388, 27], [19023.30618978309, 2748.140762355975, 28],
               [20188.01288361916, 2196.600526755965, 29], [20260.85195774109, 2025.737421601047, 30],
               [20260.85195774109, 3954.99625300923, 31]],
)
LITE_STEPS = frozenset({"l1", "l2"})


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


def load_combiner_intake(path):
    """The combiner intake (format combiner-intake-v1), bounded, UTF-8 JSON (a BOM tolerated)."""
    path = Path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_INTAKE_BYTES + 1)
    if len(raw) > MAX_INTAKE_BYTES:
        raise EvidenceError(f"{path.name} exceeds {MAX_INTAKE_BYTES} bytes")
    try:
        intake = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{path.name} is not UTF-8 JSON: {exc}") from None
    if not isinstance(intake, dict):
        raise EvidenceError(f"{path.name} is not a combiner intake object")
    return intake


def run_engine(step, state, panel_groups, host, combiner_intake=None):
    """(after state, printed lines) of one step's Studio engine."""
    answers, forms = ENGINE_ANSWERS.get(step, STEP_ANSWERS[step]), STEP_FORM_VALUES.get(step, {})
    if step == "i5":
        if combiner_intake is None:
            raise EvidenceError("step i5 needs the combiner intake")
        return cabling.combiner_auto_place(state, panel_groups, host, forms, combiner_intake)
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
    if step in LITE_STEPS:
        if combiner_intake is None:
            raise EvidenceError(f"step {step} needs the combiner intake (the string order)")
        if step == "l1":
            return cabling.lite_studio_open(state, host, combiner_intake)
        return cabling.lite_studio_commit(state, host, combiner_intake, forms)
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


def run_steps(states_dir, panel_groups, host=None, revision=None, only=None, combiner_intake=None):
    """({step: document}, {step: not-ported reason}) for every owned step (or only `only`); i5 reads
    `combiner_intake`."""
    states_dir = Path(states_dir)
    host_default = host is None
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
            step_host = LITE_HOST if step in LITE_STEPS and host_default else host
            try:
                if step in CHAINED:
                    before, _ = run_engine(CHAINED[step], before, panel_groups, step_host, combiner_intake)
                after, lines = run_engine(step, before, panel_groups, step_host, combiner_intake)
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
    parser = argparse.ArgumentParser(description="Studio G35 evidence for i5, i11, i12, i13, i17 and position, and G36's l1 and l2.")
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES,
                        help="the folder holding state-iN.json (default the committed copies)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE,
                        help="the rooftop chain intake carrying the panel-group outlines")
    parser.add_argument("--combiner-intake", type=Path, default=DEFAULT_COMBINER_INTAKE,
                        help="i5's combiner intake (default the committed copy)")
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        combiner_intake = load_combiner_intake(args.combiner_intake) if args.step in (None, "i5", "l1", "l2") else None
        docs, refused = run_steps(args.states, load_intake_groups(args.intake), revision=args.revision,
                                  only=args.step, combiner_intake=combiner_intake)
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
