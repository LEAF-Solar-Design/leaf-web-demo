#!/usr/bin/env python3
"""Studio's G35 evidence for the inverter output steps i6, i7, i8, i9, i14, i15, i16, i20 and i21.

Each step N starts from state-i(N-1).json (G13: the previous step's reopened state, in the plugin
adapter's G35 projection; default the committed copies under docs/parity/evidence/rooftop/inverters,
--states overrides), runs the Studio engine (server/solar_inverter_outputs.py) with the G35 answers, and
writes step N's delta in the plugin adapter's shape (server/solar_inverter_state.py) to iN.json.

  i6   trench-routing-auto      LEAFTRENCHAUTO       declared: panel-group INSERTs are routed too; every
                                                     route exceeds the plugin's grid cap here, no-change
  i7   cable-to-tray-snap-auto  LEAFCABLETOTRAYAUTO  no trench: none-snapped
  i8   insert-schedules         InsertSchedules      the three tables at 22000,5500
  i9   cable-export             CableExport          declared: the Export All workbook as a `file` row
  i14  lbd-placement            AddLBD               the marker beside the picked combiner
  i15  lbd-placement            LEAFPLACELBD         the block on feeder BA99
  i16  homerun-adjust           HomerunAdjust        no HOMERUN-TRUNK layer: report
  i20  cable-to-tray-snap       LEAFCABLETOTRAY      no trench within 5 m of BA99: report (one settings save)
  i21  devices-pattern-place    LEAFDEVICESPATTERN   no tracker rows: report

The panel-group outlines i6 reads live in the panel groups' block definitions, which the G35 state does
not carry; they come from the committed rooftop chain intake of the same fixture (--intake), refused
unless its panel-group handles are exactly the state's (as the devices producer reads them).

Envelope: the comparator's exports family, the plugin adapter's parameters (G22 answers, G30a
form_values), fixture_sha256 the digest of the i0 state as written, input_sha256 over fixture and
parameters, engine "server-builtin", capability version "0", compact canonical JSON. Nothing here reads
plugin evidence (iN.json); only the G35 states, which are this producer's inputs by contract. Fails
closed: a malformed state, an engine refusal, or a document the comparator refuses is a named error and
nothing is written for that run.
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
outputs = _load("solar_inverter_outputs", ROOT / "server" / "solar_inverter_outputs.py")
compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")

DEFAULT_STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "chain" / "intake.json"
DEFAULT_OUT = Path("C:/tmp/solar-parity/inv-ev/studio")
CAPABILITY_VERSION = "0"
MAX_INTAKE_BYTES = 32 * 1024 * 1024
WORKBOOK_NAME = "i9-string-export.xlsx"

# Step -> (capability, command, Studio operation). G35.
STEPS = {"i6": ("trench-routing-auto", "LEAFTRENCHAUTO", "trench-routing-auto"),
         "i7": ("cable-to-tray-snap-auto", "LEAFCABLETOTRAYAUTO", "cable-to-tray-snap-auto"),
         "i8": ("insert-schedules", "InsertSchedules", "insert-schedules"),
         "i9": ("cable-export", "CableExport", "cable-export"),
         "i14": ("lbd-placement", "AddLBD", "lbd-add"),
         "i15": ("lbd-placement", "LEAFPLACELBD", "lbd-place"),
         "i16": ("homerun-adjust", "HomerunAdjust", "homerun-adjust"),
         "i20": ("cable-to-tray-snap", "LEAFCABLETOTRAY", "cable-to-tray-snap"),
         "i21": ("devices-pattern-place", "LEAFDEVICESPATTERN", "devices-pattern-place")}
STEP_IDS = tuple(STEPS)
# G35 declared divergences among these steps.
DECLARED = frozenset({"i6", "i9"})
# G22 answers (command line) and G30a form_values (dialogs), verbatim from G35.
STEP_ANSWERS = {step: [] for step in STEPS}
STEP_ANSWERS.update({"i8": ["22000,5500"], "i15": ["BA99"], "i20": ["BA99"]})
STEP_FORM_VALUES = {"i9": {"branch_string_export": "Export All"}}

# HOST INPUTS: the capture host's per-user settings, catalog rows, AutoCAD text layout and interactive
# picks the commands read, none of which the drawing state carries. Each is named by what it stands for
# and was measured from the capture (the committed plugin responses, the reopened states and the placed
# entities), not typed:
CAPTURE_HOST = {
    # G34b: the capture host's UseL2Collectors (host-settings.json of the same host), true; it relabels
    # the string schedule and selects the combiner / inverter schedule (ScheduleRegistry.cs:111-125).
    "UseL2Collectors": True,
    # No optimizer section in the captured equipment schedule (ScheduleRegistry.cs:73).
    "UseOptimizers": False,
    # InverterSelection resolved through the catalog: the recorded sizer request names this inverter
    # (server/tests/fixtures/w1_plugin_stringsizer_response.json, full_inverter_name), and the captured
    # equipment schedule prints these fields through SafeFormat.
    "InverterCatalogRecord": {"companyName": "Sungrow", "modelName": "SG250HX", "seriesName": "SG-HX",
                              "maxDCPower": "375", "maxDCVoltage": "1500", "minDCVoltageFeed": "500",
                              "mpptVoltageRangeMin": "500", "mpptVoltageRangeMax": "1500",
                              "numMpptTrackers": "12", "DCInputers": "24", "maxACPower": "250",
                              "nominalACVoltage": "800", "maxACCurrent": "180.5"},
    # ModuleSelection resolves to no catalog module on the capture host: every module column prints "-"
    # and no PV module section or cable sizing appears.
    "ModuleCatalogRecord": None,
    # LastStringSizerResponse's standard result (the global fallback, InsertSchedulesCmd.cs:284-317): the
    # recorded plugin response for this fixture (w1_plugin_stringsizer_response.json).
    "StringSizerStandard": {"Conditions": "P99.5 Voc", "max_module_voltage": 51.2558131874,
                            "string_design_voltage": 1500},
    # The plugin's in-memory cable index on a drawing freshly reopened from its file (G13) holds no
    # feeders: the reopened i7 state carries feeder cables and the captured schedules no feeder table.
    "SessionCableIndexHasFeeders": False,
    # The drawing's TEXTSIZE: the equipment table (15 rows of 2.5 text heights) and its gap (4 text
    # heights) moved the next table down exactly 8.3.
    "DrawingTextSize": 0.2,
    # AutoCAD's laid-out height of a table whose text wraps (the Mod/String lists): the captured
    # combiner table moved the string table down by this plus the 0.8 gap.
    "ScheduleLayoutHeights": {"COMBINER / INVERTER SCHEDULE": (5491.700000000001 - 5481.419963619829) - 0.2 * 4},
    # AddLBD's entity pick (G35 records no answer for it): the placed marker lies on the ray from the
    # picked device toward the pick, so its centre stands for the picked point.
    "AddLbdPick": [20052.05691895678, 3589.843580707701],
    # The pick point of the created feeder BA99 (G35's answer names a created handle, which never leaves
    # the adapter): LEAFPLACELBD's block lies on it, the closest point of BA99 to the pick.
    "CablePicks": {"BA99": [14596.33277007084, 3970.289594996278]},
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
    """(after state, printed lines, workbook or None) of one step's Studio engine."""
    answers, forms = STEP_ANSWERS[step], STEP_FORM_VALUES.get(step, {})
    if step == "i6":
        after, lines = outputs.trench_routing_auto(state, panel_groups, host)
    elif step == "i7":
        after, lines = outputs.cable_to_tray_snap_auto(state)
    elif step == "i8":
        after, lines = outputs.insert_schedules(state, host, answers)
    elif step == "i9":
        return outputs.cable_export(state, host, forms)
    elif step == "i14":
        after, lines = outputs.lbd_add(state, host)
    elif step == "i15":
        after, lines = outputs.lbd_place(state, host, answers)
    elif step == "i16":
        after, lines = outputs.homerun_adjust(state)
    elif step == "i20":
        after, lines = outputs.cable_to_tray_snap(state, host, answers)
    elif step == "i21":
        after, lines = outputs.devices_pattern_place(state)
    else:
        raise EvidenceError(f"step {step!r} is not one of {STEP_IDS}")
    return after, lines, None


def evidence_rows(step, before, after, lines, workbook):
    """The step's G35 rows (st.step_rows) plus the declared Studio-only rows: the trenches i6 drew and
    the workbook i9 wrote, each replacing the report row a no-change delta carries. Rows stay grouped by
    type in ascending order (G9)."""
    rows, settings = st.step_rows(step, before, after, lines)
    extra = outputs.trench_rows(before, after) if step == "i6" else []
    if step == "i9" and workbook is not None:
        extra = [outputs.file_row(workbook["text"])]
    if extra:
        rows = [r for r in rows if r["type"] != "report"] + extra
        rows.sort(key=lambda r: r["type"])            # stable: ids keep their order within a type
    return rows, settings


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
                     "engine": "server-builtin",  # the gate's engine vocabulary; the module is solar_inverter_outputs
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
    """({step: document}, workbook bytes or None) for every owned step (or only `only`), each from
    state-i(N-1).json."""
    states_dir = Path(states_dir)
    host = dict(CAPTURE_HOST if host is None else host)
    if only is not None and only not in STEPS:
        raise EvidenceError(f"step {only!r} is not one of {STEP_IDS}")
    try:
        fixture = st.digest(st.publish(st.load_state(states_dir / "state-i0.json")))
        out, workbook_bytes = {}, None
        for step in STEP_IDS:
            if only is not None and step != only:
                continue
            number = int(step[1:])
            before = st.load_state(states_dir / f"state-i{number - 1}.json")
            after, lines, workbook = run_engine(step, before, panel_groups, host)
            rows, settings = evidence_rows(step, before, after, lines, workbook)
            out[step] = build_document(step, fixture, before, after, rows, settings, revision)
            if workbook is not None:
                workbook_bytes = workbook["bytes"]
        return out, workbook_bytes
    except OSError as exc:
        raise EvidenceError(f"a state is unreadable: {exc}") from None
    except (st.InverterStateError, devices.InverterDeviceError, outputs.InverterOutputError) as exc:
        raise EvidenceError(str(exc)) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio G35 evidence for i6 i7 i8 i9 i14 i15 i16 i20 i21.")
    parser.add_argument("--states", type=Path, default=DEFAULT_STATES,
                        help="the folder holding state-iN.json (default the committed copies)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE,
                        help="the rooftop chain intake carrying the panel-group outlines")
    parser.add_argument("--step", choices=STEP_IDS)
    parser.add_argument("--revision", default=None, help="the git commit the evidence is bound to, if any")
    args = parser.parse_args(argv)
    try:
        docs, workbook = run_steps(args.states, load_intake_groups(args.intake), revision=args.revision,
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
        if workbook is not None:
            (args.out / WORKBOOK_NAME).write_bytes(workbook)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-inverter-outputs-evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
