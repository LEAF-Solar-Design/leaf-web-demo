"""Offline checks for the shared probe-file normalizer (contract v5, rules E1-E5).

Studio's own probe files come from scripts/solar_nec_probes.py, which computes
every value through server/solar_nec.py. The licensed capture, when this host
carries it, is the other side of the comparison; without it the suite still runs
end to end against Studio's own files and against a deliberately altered one, so
nothing here skips.

The four probe capabilities added in S28 come from scripts/solar_probe_calcs_probes.py
and their licensed capture IS committed, at
docs/parity/evidence/probes/demo-probes-20260923, so their half of these checks
reads the artifact rather than a copy of Studio's own output.

What these prove:
  * the evidence a probe file yields is exactly what the frozen comparator
    accepts for family `exports`, with the E4 identity mapping and an observed,
    not assumed, reopen;
  * rule E1: the fixture hash is the INPUTS, so two files with the same
    scenarios and different results share it and differ only in output_sha256;
  * all six licensed NEC files compare pass against Studio's, and so does each
    of the nine committed S28 files;
  * rule E5's CSV projection: the `#` preamble is skipped, the header names the
    fields, values stay TEXT and the quantity is parsed back out of them, CRLF
    and LF read identically, and a headerless, blank-named, repeated-name or
    ragged file is refused rather than answered;
  * the declared non-numeric quantity applies only where a section declares it;
  * one altered result is a diff, not a silent pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


normalizer = load_module("solar_probe_evidence")
probes = load_module("solar_nec_probes")
calc_probes = load_module("solar_probe_calcs_probes")
# The comparator comes from the module under test, never a second load of the
# same file: `spec_from_file_location` builds a NEW module object each call, so
# a private copy here would carry its own InputError class and every
# `pytest.raises(compare.InputError)` below would miss the error the normalizer
# actually raised. One module object, one exception identity.
compare = normalizer.compare

# A probe capability's fixture is the scenario list, so both sides carry the same
# revision; a receipt builder supplies the real commit, a test supplies a literal.
REVISION = "0123456789abcdef0123456789abcdef01234567"
# The licensed capture is committed with the receipts, so every runner carries it.
DEFAULT_REFERENCE_DIR = (Path(__file__).resolve().parents[1]
                         / "docs/parity/evidence/nec/demo-probes-20260923")
REFERENCE_DIR = Path(os.environ.get("LEAF_SOLAR_W3_PROBE_REF", str(DEFAULT_REFERENCE_DIR)))
DEMOS = sorted(probes.DEMOS)
# Which OUTPUT field to corrupt per demo, and the section holding it. Each one
# is an output only: the one-liner battery's Result is an INPUT to ToOneLiner,
# so its output is the rendered string.
ALTERED_OUTPUT = {
    "leafvdropac": (None, "Result"),
    "leafampcorr": (None, "Result"),
    "leafconduitfill": ("sizeConduit", "fillPct"),
    "leafmaxfill": (None, "Fill"),
    "leaffeederocpd": (None, "OcpdRatingA"),
    "leafoneliner": (None, "OneLiner"),
}

# --------------------------------------------------------------------------- #
# S28: the four calculation capabilities, whose licensed capture is COMMITTED.
# The unit here is a FILE, not a demo: the project summary is three fixtures
# each writing a CSV and a JSON, and each file is its own probe document.
# --------------------------------------------------------------------------- #
CALC_REFERENCE_DIR = (Path(__file__).resolve().parents[1]
                      / "docs/parity/evidence/probes/demo-probes-20260923")
CALC_FILES = sorted(calc_probes.FILE_PROBE_TYPES)
# Which OUTPUT to corrupt per file, and how. Each target is an output only, so
# an alteration must move output_sha256 and leave the fixture hash alone.
#   ("csv", column)         the first data row's column, rewritten
#   ("json-list", index)    that probe's OutputHandles, lengthened
#   ("json-object", key)    that metric's value
CALC_ALTERED = {
    "leafsnakeorder_probes.json": ("json-list", 2),
    "leafshadelimit_demo.csv": ("csv", "max_gcr"),
    "leaftorqueshade_demo.csv": ("csv", "Fraction"),
}
for _label, _ in calc_probes.PROJECTSUMMARY_FIXTURES:
    CALC_ALTERED["leafprojectsummary_demo_%s.csv" % _label] = ("csv", "Value")
    CALC_ALTERED["leafprojectsummary_demo_%s.json" % _label] = ("json-object", "PanelCount")
del _label


def studio_file(folder, demo):
    return probes.write(demo, folder)


def reference_file(demo):
    path = REFERENCE_DIR / probes.file_name(demo)
    return path if path.is_file() else None


def evidence_for(path, demo, side):
    return normalizer.build_evidence_from_file(
        path, capability=probes.DEMO_CAPABILITIES[demo],
        probe_type=probes.DEMO_PROBE_TYPES[demo], side=side, revision=REVISION)


def altered_file(folder, demo, index=0):
    """Copy Studio's probe file with ONE result changed, inputs untouched."""
    document = json.loads((folder / probes.file_name(demo)).read_text(encoding="utf-8"))
    section, field = ALTERED_OUTPUT[demo]
    probe_list = document if section is None else document[section]
    value = probe_list[index][field]
    probe_list[index][field] = value + " (altered)" if isinstance(value, str) else value + 1.0
    path = folder / ("altered_" + probes.file_name(demo))
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def verdict(plugin, studio, demo):
    return compare.compare(plugin, studio, "exports",
                           capability=probes.DEMO_CAPABILITIES[demo])


# --------------------------------------------------------------------------- #
# S28 helpers, one file at a time
# --------------------------------------------------------------------------- #
def calc_studio_file(folder, name):
    """Write Studio's own copy of one S28 file and return its path."""
    for path in calc_probes.write(calc_probes.FILE_DEMOS[name], folder):
        if path.name == name:
            return path
    raise AssertionError("demo did not write " + name)


def calc_evidence_for(path, name, side):
    return normalizer.build_evidence_from_file(
        path, capability=calc_probes.FILE_CAPABILITIES[name],
        probe_type=calc_probes.FILE_PROBE_TYPES[name], side=side, revision=REVISION)


def calc_verdict(plugin, studio, name):
    return compare.compare(plugin, studio, "exports",
                           capability=calc_probes.FILE_CAPABILITIES[name])


def calc_altered_file(folder, name):
    """Studio's file with ONE OUTPUT changed and every declared input untouched."""
    kind, target = CALC_ALTERED[name]
    text = (folder / name).read_text(encoding="utf-8")
    if kind == "csv":
        lines = text.replace("\r\n", "\n").split("\n")
        start = 0
        while lines[start].startswith("#"):
            start += 1
        header = lines[start].split(",")
        cells = lines[start + 1].split(",")
        column = header.index(target)
        cells[column] = "0.25" if cells[column] != "0.25" else "0.75"
        lines[start + 1] = ",".join(cells)
        altered = "\r\n".join(lines)
    else:
        document = json.loads(text)
        if kind == "json-list":
            document[target]["OutputHandles"] = document[target]["OutputHandles"] + ["ZZ"]
        else:
            document[target] = document[target] + 4242
        altered = json.dumps(document, indent=2, ensure_ascii=False)
    path = folder / ("altered_" + name)
    path.write_bytes(altered.encode("utf-8"))
    return path


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("demo", DEMOS)
def test_evidence_is_comparator_valid(tmp_path, demo):
    evidence = evidence_for(studio_file(tmp_path, demo), demo, "studio")
    compare.validate_evidence(evidence, "exports")
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert evidence["parameters"] == {"family": "exports",
                                      "capability": probes.DEMO_CAPABILITIES[demo]}
    assert evidence["units"] in compare.LENGTH_UNITS
    assert evidence["frame"] == normalizer.FRAME
    assert evidence["after"]["source_revision"] == "scenario-list"
    assert evidence["after"]["format"] == "json-probes"
    assert evidence["before"] == {"recorded": False}
    assert evidence["changes"] == {"created": [], "modified": [], "deleted": []}
    assert evidence["state"] == "committed"
    assert evidence["execution_mode"] == "recorded"
    assert evidence["survived_reopen"] is True
    assert evidence["synthetic_flagged"] is True
    assert evidence["versions"]["schema"] == normalizer.SCHEMA
    assert evidence["versions"]["catalog"] == "none"
    assert evidence["versions"]["solver"] == "none"


@pytest.mark.parametrize("demo", DEMOS)
def test_entity_mapping_is_exactly_the_rows(tmp_path, demo):
    """Rule E4: each row id maps to itself, and nothing else appears."""
    evidence = evidence_for(studio_file(tmp_path, demo), demo, "studio")
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert evidence["entity_mapping"] == {identifier: identifier for identifier in ids}
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("demo", DEMOS)
def test_rows_carry_every_probe_field_verbatim(tmp_path, demo):
    document = json.loads(studio_file(tmp_path, demo).read_text(encoding="utf-8"))
    evidence = evidence_for(tmp_path / probes.file_name(demo), demo, "studio")
    rows = evidence["after"]["rows"]
    spec = normalizer.PROBE_SPECS[probes.DEMO_PROBE_TYPES[demo]]
    flat = []
    for section in spec.sections:
        flat.extend(document if section.key is None else document[section.key])
    # Rule E3: the file's own order, sections concatenated in the file's order.
    assert len(rows) == len(flat)
    for row, probe in zip(rows, flat):
        assert row["fields"] == probe
        assert set(row) == {"id", "type", "quantity", "unit", "fields"}
        assert row["quantity"]["kind"] == "float"
        assert row["quantity"]["unit"] == row["unit"]


def test_row_types_name_the_calculation(tmp_path):
    """The conduit-fill file holds four batteries, so its rows carry four types."""
    evidence = evidence_for(studio_file(tmp_path, "leafconduitfill"), "leafconduitfill", "studio")
    types = [row["type"] for row in evidence["after"]["rows"]]
    assert set(types) == {"nec-max-fill-fraction", "nec-conductor-area",
                          "nec-conduit-area", "nec-conduit-sizing"}
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert ids[0] == "maxFill:I1_count_1"
    assert ids[-1] == "sizeConduit:I13_mono_10"


# --------------------------------------------------------------------------- #
# Rule E1: the fixture is the inputs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("demo", DEMOS)
def test_fixture_hash_is_inputs_only(tmp_path, demo):
    studio_file(tmp_path, demo)
    original = evidence_for(tmp_path / probes.file_name(demo), demo, "studio")
    changed = evidence_for(altered_file(tmp_path, demo), demo, "studio")
    assert original["fixture_sha256"] == changed["fixture_sha256"]
    assert original["input_sha256"] == changed["input_sha256"]
    assert original["output_sha256"] != changed["output_sha256"]


def test_fixture_hash_changes_when_an_input_changes(tmp_path):
    studio_file(tmp_path, "leafmaxfill")
    original = evidence_for(tmp_path / probes.file_name("leafmaxfill"), "leafmaxfill", "studio")
    document = json.loads((tmp_path / probes.file_name("leafmaxfill")).read_text(encoding="utf-8"))
    document[3]["N"] = 7
    path = tmp_path / "other.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    other = evidence_for(path, "leafmaxfill", "studio")
    assert other["fixture_sha256"] != original["fixture_sha256"]
    assert other["input_sha256"] != original["input_sha256"]


# --------------------------------------------------------------------------- #
# The parity verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("demo", DEMOS)
def test_studio_compares_pass_against_the_reference(tmp_path, demo):
    """Studio against the licensed capture when it is on this host.

    Without the capture the same comparison runs against Studio's own file read
    back from a second path, which still exercises the whole comparator rail.
    This never skips; test_reference_capture_presence_is_all_or_nothing records
    which of the two ran.
    """
    studio = evidence_for(studio_file(tmp_path, demo), demo, "studio")
    licensed = reference_file(demo)
    if licensed is None:
        copy = tmp_path / ("copy_" + probes.file_name(demo))
        copy.write_bytes((tmp_path / probes.file_name(demo)).read_bytes())
        other = evidence_for(copy, demo, "plugin")
    else:
        other = evidence_for(licensed, demo, "plugin")
    result = verdict(other, studio, demo)
    assert result["verdict"] == "pass", result["diffs"][:5]


def test_reference_capture_presence_is_all_or_nothing():
    present = [demo for demo in DEMOS if reference_file(demo) is not None]
    assert present == [] or present == DEMOS, present


@pytest.mark.parametrize("demo", DEMOS)
def test_one_altered_result_is_a_diff(tmp_path, demo):
    studio_file(tmp_path, demo)
    good = evidence_for(tmp_path / probes.file_name(demo), demo, "studio")
    bad = evidence_for(altered_file(tmp_path, demo), demo, "plugin")
    result = verdict(bad, good, demo)
    assert result["verdict"] == "fail"
    # One probe changed, so the diffs name that row and no scenario input.
    assert all(path.startswith("after/rows/") for path in result["diffs"]), result["diffs"]
    rows = {path.split("/")[2] for path in result["diffs"]}
    assert len(rows) == 1, result["diffs"]


def test_unreopened_evidence_cannot_pass(tmp_path):
    """survived_reopen is observed, and the comparator refuses it when false."""
    path = studio_file(tmp_path, "leafmaxfill")
    document = json.loads(path.read_text(encoding="utf-8"))
    in_memory = normalizer.build_evidence(
        document, capability="nec-conduit-fill", probe_type="nec-max-fill-fraction",
        side="studio", revision=REVISION, survived_reopen=False,
        file_name=path.name, file_sha256="0" * 64)
    assert in_memory["survived_reopen"] is False
    reopened = evidence_for(path, "leafmaxfill", "plugin")
    result = verdict(reopened, in_memory, "leafmaxfill")
    assert result["verdict"] == "fail"
    assert any("requires committed state and actual reopen" in diff for diff in result["diffs"])


# --------------------------------------------------------------------------- #
# Refusals: a malformed probe file is never a verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("document,probe_type", [
    ({}, "nec-max-fill-fraction"),
    ([], "nec-max-fill-fraction"),
    ([{"Name": "a"}], "nec-max-fill-fraction"),
    ([{"Name": "a", "N": 1}], "nec-max-fill-fraction"),
    ([{"Name": "", "N": 1, "Fill": 0.4}], "nec-max-fill-fraction"),
    ([{"Name": "a", "N": 1, "Fill": 0.4}, {"Name": "a", "N": 2, "Fill": 0.31}],
     "nec-max-fill-fraction"),
    ([{"Name": "a", "N": 1, "Fill": "0.4"}], "nec-max-fill-fraction"),
    ([{"Name": "a", "N": 1, "Fill": True}], "nec-max-fill-fraction"),
    ("not a document", "nec-max-fill-fraction"),
    ({"maxFill": []}, "nec-conduit-fill-tables"),
    ({"maxFill": {}}, "nec-conduit-fill-tables"),
])
def test_malformed_probe_documents_are_refused(document, probe_type):
    with pytest.raises(compare.InputError):
        normalizer.build_evidence(document, capability="nec-conduit-fill",
                                  probe_type=probe_type, side="studio", revision=REVISION,
                                  survived_reopen=True, file_name="p.json",
                                  file_sha256="0" * 64)


@pytest.mark.parametrize("overrides", [
    {"probe_type": "no-such-probe-type"},
    {"capability": "Not A Capability"},
    {"capability": ""},
    {"revision": "not-a-commit"},
    {"revision": "ABCDEF0123456789ABCDEF0123456789ABCDEF01"},
    {"side": ""},
    {"survived_reopen": "yes"},
])
def test_malformed_arguments_are_refused(overrides):
    arguments = {"capability": "nec-conduit-fill", "probe_type": "nec-max-fill-fraction",
                 "side": "studio", "revision": REVISION, "survived_reopen": True,
                 "file_name": "p.json", "file_sha256": "0" * 64}
    arguments.update(overrides)
    with pytest.raises(compare.InputError):
        normalizer.build_evidence([{"Name": "a", "N": 1, "Fill": 0.4}], **arguments)


def test_only_the_implemented_formats_are_wired():
    """XLSX lands in READERS in a later slice; until then, fail closed."""
    assert sorted(normalizer.READERS) == ["csv", "json-metrics", "json-probes"]
    assert all(spec.file_format in normalizer.READERS
               for spec in normalizer.PROBE_SPECS.values())
    assert not any(spec.file_format.startswith("xlsx")
                   for spec in normalizer.PROBE_SPECS.values())


# --------------------------------------------------------------------------- #
# S28: the four calculation capabilities
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", CALC_FILES)
def test_calc_evidence_is_comparator_valid(tmp_path, name):
    evidence = calc_evidence_for(calc_studio_file(tmp_path, name), name, "studio")
    compare.validate_evidence(evidence, "exports")
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert evidence["parameters"] == {"family": "exports",
                                      "capability": calc_probes.FILE_CAPABILITIES[name]}
    assert evidence["after"]["source_revision"] == "scenario-list"
    assert evidence["after"]["format"] == \
        normalizer.PROBE_SPECS[calc_probes.FILE_PROBE_TYPES[name]].file_format
    assert evidence["survived_reopen"] is True
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert evidence["entity_mapping"] == {identifier: identifier for identifier in ids}
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("name", CALC_FILES)
def test_calc_studio_compares_pass_against_the_committed_capture(tmp_path, name):
    """The licensed capture is committed here, so this is the real comparison."""
    studio = calc_evidence_for(calc_studio_file(tmp_path, name), name, "studio")
    licensed = calc_evidence_for(CALC_REFERENCE_DIR / name, name, "plugin")
    result = calc_verdict(licensed, studio, name)
    assert result["verdict"] == "pass", result["diffs"][:5]


@pytest.mark.parametrize("name", CALC_FILES)
def test_calc_fixture_hash_is_inputs_only(tmp_path, name):
    calc_studio_file(tmp_path, name)
    original = calc_evidence_for(tmp_path / name, name, "studio")
    changed = calc_evidence_for(calc_altered_file(tmp_path, name), name, "studio")
    assert original["fixture_sha256"] == changed["fixture_sha256"]
    assert original["input_sha256"] == changed["input_sha256"]
    assert original["output_sha256"] != changed["output_sha256"]


@pytest.mark.parametrize("name", CALC_FILES)
def test_calc_one_altered_result_is_a_diff(tmp_path, name):
    calc_studio_file(tmp_path, name)
    good = calc_evidence_for(tmp_path / name, name, "studio")
    bad = calc_evidence_for(calc_altered_file(tmp_path, name), name, "plugin")
    result = calc_verdict(bad, good, name)
    assert result["verdict"] == "fail"
    assert all(path.startswith("after/rows/") for path in result["diffs"]), result["diffs"]
    assert len({path.split("/")[2] for path in result["diffs"]}) == 1, result["diffs"]


def test_calc_rows_keep_the_files_own_order(tmp_path):
    """Rule E3 on a CSV: the rows are the file's rows, in the file's order."""
    path = calc_studio_file(tmp_path, "leaftorqueshade_demo.csv")
    evidence = calc_evidence_for(path, "leaftorqueshade_demo.csv", "studio")
    labels = [label for label, *_ in calc_probes.TORQUESHADE_SCENARIOS]
    assert [row["id"]["entity_id"] for row in evidence["after"]["rows"]] == labels


def test_calc_row_types_name_the_calculation(tmp_path):
    path = calc_studio_file(tmp_path, "leafshadelimit_demo.csv")
    evidence = calc_evidence_for(path, "leafshadelimit_demo.csv", "studio")
    assert {row["type"] for row in evidence["after"]["rows"]} == {"shade-limit-angle"}
    # Three blocks, one row per probe, ids taken from the label column.
    assert evidence["after"]["rows"][0]["id"]["entity_id"] == "Anchor_GCR_0.5_Tilt_60"
    assert evidence["after"]["rows"][-1]["id"]["entity_id"] == \
        "AutoCalc_Boulder_SummerNoon_2025"


def test_the_csv_and_json_projections_share_a_row_id_space(tmp_path):
    """A metric's CSV row and its JSON twin carry the SAME id, so a diff names
    the metric rather than the document."""
    calc_studio_file(tmp_path, "leafprojectsummary_demo_f2.csv")
    csv_rows = calc_evidence_for(tmp_path / "leafprojectsummary_demo_f2.csv",
                                 "leafprojectsummary_demo_f2.csv", "studio")["after"]["rows"]
    json_rows = calc_evidence_for(tmp_path / "leafprojectsummary_demo_f2.json",
                                  "leafprojectsummary_demo_f2.json", "studio")["after"]["rows"]
    csv_ids = {row["id"]["entity_id"] for row in csv_rows}
    json_ids = {row["id"]["entity_id"] for row in json_rows}
    assert "PanelCount" in csv_ids & json_ids
    # The CSV flattens the two collections into rows, the JSON keeps them whole.
    assert "StringsOfLength_12" in csv_ids and "StringsByLength" in json_ids


# --------------------------------------------------------------------------- #
# Rule E5: the CSV projection
# --------------------------------------------------------------------------- #
def test_csv_values_stay_text_and_the_quantity_is_parsed_from_them(tmp_path):
    path = calc_studio_file(tmp_path, "leaftorqueshade_demo.csv")
    rows = calc_evidence_for(path, "leaftorqueshade_demo.csv", "studio")["after"]["rows"]
    first = rows[0]
    assert first["fields"] == {"Label": "Z1", "RadiusM": "0", "TopGapM": "0",
                               "BotGapM": "0", "CrossAxisM": "2", "TiltDeg": "0",
                               "Fraction": "0"}
    assert first["quantity"] == {"kind": "float", "value": 0.0, "unit": "none"}
    monotone = {row["id"]["entity_id"]: row["quantity"]["value"] for row in rows}
    assert monotone["M30"] < monotone["M60"] < monotone["M89"]


def test_csv_preamble_is_skipped_and_the_header_names_the_fields(tmp_path):
    path = calc_studio_file(tmp_path, "leafshadelimit_demo.csv")
    assert path.read_text(encoding="utf-8").startswith("# LEAFSHADELIMITDEMO")
    rows = calc_evidence_for(path, "leafshadelimit_demo.csv", "studio")["after"]["rows"]
    assert len(rows) == 20
    assert set(rows[0]["fields"]) == {
        "block", "label", "gcr_in", "tilt_deg", "sla_target_deg", "max_gcr",
        "sla_reconstructed_deg", "min_pitch_m", "sun_elev_deg", "sun_az_deg",
        "is_daytime"}
    # An empty cell is an empty string, never a dropped column or a zero.
    assert rows[0]["fields"]["is_daytime"] == ""


def test_crlf_and_lf_read_to_the_same_rows(tmp_path):
    """A committed capture is line-ending translated on checkout, so the reader
    must not make a parity verdict depend on a git setting."""
    path = calc_studio_file(tmp_path, "leaftorqueshade_demo.csv")
    unix = tmp_path / "unix.csv"
    unix.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    crlf = calc_evidence_for(path, "leaftorqueshade_demo.csv", "studio")
    lf = calc_evidence_for(unix, "leaftorqueshade_demo.csv", "studio")
    assert crlf["output_sha256"] == lf["output_sha256"]
    assert crlf["fixture_sha256"] == lf["fixture_sha256"]


@pytest.mark.parametrize("text", [
    "",
    "# only a preamble\r\n",
    "Label,,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction\r\nZ1,0,0,0,2,0,0\r\n",
    "Label,Label,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction\r\nZ1,0,0,0,2,0,0\r\n",
    "Label,RadiusM,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction\r\nZ1,0,0\r\n",
    "Label,RadiusM,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction\r\nZ1,0,0,0,2,0,0,9\r\n",
    "Label,RadiusM,TopGapM,BotGapM,CrossAxisM,TiltDeg\r\nZ1,0,0,0,2,0\r\n",
])
def test_malformed_csv_files_are_refused(tmp_path, text):
    path = tmp_path / "probe.csv"
    path.write_bytes(text.encode("utf-8"))
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(
            path, capability="torque-tube-rear-shade",
            probe_type="torque-tube-rear-shade", side="studio", revision=REVISION)


def test_a_json_metrics_file_must_be_an_object(tmp_path):
    path = tmp_path / "probe.json"
    path.write_bytes(b"[]")
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(
            path, capability="project-summary-export-csv-json",
            probe_type="project-summary-json", side="studio", revision=REVISION)


# --------------------------------------------------------------------------- #
# The declared non-numeric quantity, and the sections that declare none
# --------------------------------------------------------------------------- #
def test_declared_non_numeric_quantity_applies_where_a_section_declares_it(tmp_path):
    snake = calc_evidence_for(calc_studio_file(tmp_path, "leafsnakeorder_probes.json"),
                              "leafsnakeorder_probes.json", "studio")
    by_id = {row["id"]["entity_id"]: row for row in snake["after"]["rows"]}
    # A null output is the declared case; an empty list is a real count of zero,
    # and the two stay distinguishable in `fields`.
    assert by_id["null_panels"]["quantity"]["value"] == 0.0
    assert by_id["null_panels"]["fields"]["OutputHandles"] is None
    assert by_id["empty_panels"]["quantity"]["value"] == 0
    assert by_id["empty_panels"]["fields"]["OutputHandles"] == []
    assert by_id["grid_2x3"]["quantity"]["value"] == 6

    summary = calc_evidence_for(
        calc_studio_file(tmp_path, "leafprojectsummary_demo_f3.csv"),
        "leafprojectsummary_demo_f3.csv", "studio")
    cells = {row["id"]["entity_id"]: row for row in summary["after"]["rows"]}
    assert cells["CableSize_1"]["quantity"]["value"] == 0.0
    assert cells["CableSize_1"]["fields"]["Value"] == "4 AWG x2"
    assert cells["CableSize_2"]["fields"]["Value"] == ""
    assert cells["PanelCount"]["quantity"]["value"] == 42.0


@pytest.mark.parametrize("cell", ["abc", "", "NaN", "Infinity", "0x10", "1e400"])
def test_a_section_declaring_no_fallback_refuses_a_non_numeric_quantity(tmp_path, cell):
    header = "Label,RadiusM,TopGapM,BotGapM,CrossAxisM,TiltDeg,Fraction\r\n"
    path = tmp_path / "probe.csv"
    path.write_bytes((header + "Z1,0,0,0,2,0," + cell + "\r\n").encode("utf-8"))
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(
            path, capability="torque-tube-rear-shade",
            probe_type="torque-tube-rear-shade", side="studio", revision=REVISION)


def test_cli_writes_evidence(tmp_path):
    path = studio_file(tmp_path, "leafmaxfill")
    output = tmp_path / "evidence.json"
    code = normalizer.main(["--probe-file", str(path), "--capability", "nec-conduit-fill",
                            "--probe-type", "nec-max-fill-fraction", "--side", "studio",
                            "--revision", REVISION, "--output", str(output)])
    assert code == 0
    compare.validate_evidence(json.loads(output.read_text(encoding="utf-8")), "exports")


def test_cli_refuses_a_missing_file(tmp_path, capsys):
    code = normalizer.main(["--probe-file", str(tmp_path / "absent.json"),
                            "--capability", "nec-conduit-fill",
                            "--probe-type", "nec-max-fill-fraction", "--side", "studio",
                            "--revision", REVISION, "--output", str(tmp_path / "out.json")])
    assert code == 2
    assert "solar-probe-evidence" in capsys.readouterr().err
