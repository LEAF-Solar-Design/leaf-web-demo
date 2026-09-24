#!/usr/bin/env python3
"""Studio's G35 evidence for the inverter device steps i1, i4, i10, i18 and i19.

Each step N starts from state-i(N-1).json (G13: the previous step's reopened state, in the plugin
adapter's G35 projection; default the committed copies under docs/parity/evidence/rooftop/inverters,
--states overrides), runs the Studio engine (server/solar_inverter_devices.py) with the G35 answers,
and writes step N's delta in the plugin adapter's shape (server/solar_inverter_state.py) to iN.json.

  i1   inverter-add        AddAllInverters        the L2 fleet by the grid fallback (the captured cloud
                                                  call answered 503; the fallback is under test)
  i4   inverter-balance    INVBALANCE             Auto-Balance All finds no L1 summary: report
  i10  skid-reconcile      LEAFSKIDRECONCILE      the printed verdict: report
  i18  inverter-add        ADDINVERTER            combiner 15 at the snapped point, strings deferred
  i19  adopt-l2-inverters  LEAFADOPTL2INVERTERS   every device adopted as a fixed L2 inverter

The panel-group outlines AddAllInverters' fallback reads live in the panel groups' block definitions,
which the G35 state does not carry; they come from the committed rooftop chain intake of the same
fixture (docs/parity/evidence/rooftop/chain/intake.json, --intake overrides), refused unless its
panel-group handles are exactly the state's.

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
devices = _load("solar_inverter_devices", ROOT / "server" / "solar_inverter_devices.py")
compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")

DEFAULT_STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "chain" / "intake.json"
DEFAULT_OUT = Path("C:/tmp/solar-parity/inv-ev/studio")
CAPABILITY_VERSION = "0"
MAX_INTAKE_BYTES = 32 * 1024 * 1024

# Step -> (capability, command, Studio operation). G35.
STEPS = {"i1": ("inverter-add", "AddAllInverters", "inverter-add-all"),
         "i4": ("inverter-balance", "INVBALANCE", "inverter-balance"),
         "i10": ("skid-reconcile", "LEAFSKIDRECONCILE", "skid-reconcile"),
         "i18": ("inverter-add", "ADDINVERTER", "inverter-add"),
         "i19": ("adopt-l2-inverters", "LEAFADOPTL2INVERTERS", "adopt-l2-inverters")}
STEP_IDS = tuple(STEPS)
# G22 answers (command line) and G30a form_values (dialogs), verbatim from G35.
STEP_ANSWERS = {"i1": [], "i4": [], "i10": [], "i18": ["15", "20300,3589.19,0", "AddLater"], "i19": []}
STEP_FORM_VALUES = {
    "i1": {"inverters_count_differs_from_stringsizer": "Yes", "low_utilization_on_last_inverter": "Keep current"},
    "i4": {"branch_inverter_manager_tab": "Central Inverters",
           "branch_inverter_manager_action": "Auto-Balance All", "branch_inverter_manager": "Close"},
    "i18": {"select_equipment_type": "Combiner box"},
    "i19": {"select_l2_inverter_hardware_mapping": "default", "select_l2_inverter_hardware": "Adopt"},
}

# HOST INPUTS: the capture host's per-user settings and block-definition quantities the commands read,
# none of which the drawing state carries. Each is named by the plugin setting it stands for and was
# measured from the capture (G35 raw folder: the command logs and the reopened states), not typed:
CAPTURE_HOST = {
    # G34b: the capture host's UseL2Collectors (host-settings.json of the same host), true.
    "UseL2Collectors": True,
    "UseCombinerBox": False,
    # Only read on a non-Roof drawing (BranchCmd.cs:11554-11560); this fixture is Roof.
    "UsePatternPlacement": False,
    # NumMppt x StringsPerMppt: i1 placed 8 inverters for 173 strings and raised the low-utilization
    # dialog, which holds only for 24 (ceil(173/24) = 8, 173 mod 24 = 5, under half of 24; 22 and 23
    # also give 8 but fill the last inverter past half, so no dialog).
    "StringsPerCentralInverter": 24,
    # CombinerBoxConnections: the input count every combiner block of the capture carries (20).
    "CombinerBoxConnections": 20,
    # SuggestedInverterCount: unrecorded; the capture shows only that the count-differs dialog fired.
    "SuggestedInverterCount": None,
    # L1CollectorsPerL2: i10 printed "8 L2 x 4 slots".
    "L1CollectorsPerL2": 4,
    # L2InverterSelection: i19 wrote this model and rating on every adopted block.
    "L2InverterSelection": "TMEIC NINJA-5.05",
    # EquipmentSymbolScale for a central inverter and a combiner block: the module scale and the
    # block definition's native size are host and block-definition quantities; the captured blocks
    # carry exactly these (their ratio is the plugin's 3.0 central multiplier).
    "CentralInverterSymbolScale": 8.673644733572,
    "CombinerSymbolScale": 2.891214911191,
    # Running object snap aperture in drawing units: i18's typed point 20300,3589.19 landed on the
    # feeder vertex 39.15 away; the next candidate is 122.06 away. Any value between reproduces it.
    "OsnapApertureDrawingUnits": 50.0,
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
    answers, forms = STEP_ANSWERS[step], STEP_FORM_VALUES.get(step, {})
    if step == "i1":
        return devices.inverter_add_all(state, panel_groups, host, forms)
    if step == "i4":
        return devices.inverter_balance(state, host, forms)
    if step == "i10":
        return devices.skid_reconcile(state, host)
    if step == "i18":
        return devices.inverter_add(state, host, answers, forms)
    if step == "i19":
        return devices.adopt_l2_inverters(state, host, forms)
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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_inverter_devices
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


def run_steps(states_dir, panel_groups, host=None, revision=None, only=None):
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
            after, lines = run_engine(step, before, panel_groups, host)
            rows, settings = st.step_rows(step, before, after, lines)
            out[step] = build_document(step, fixture, before, after, rows, settings, revision)
        return out
    except OSError as exc:
        raise EvidenceError(f"a state is unreadable: {exc}") from None
    except (st.InverterStateError, devices.InverterDeviceError) as exc:
        raise EvidenceError(str(exc)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G35 evidence for i1, i4, i10, i18 and i19.")
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES,
                        help="the folder holding state-iN.json (default the committed copies)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE,
                        help="the rooftop chain intake carrying the panel-group outlines")
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs = run_steps(args.states, load_intake_groups(args.intake), revision=args.revision, only=args.step)
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
        print(f"solar-inverter-devices-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
