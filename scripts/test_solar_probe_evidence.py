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

The six terrain and irradiance capabilities added in S32 come from
scripts/solar_terrain_probes.py and their licensed CSVs are committed in the same
folder. Four of them start with the byte order mark Encoding.UTF8 writes, which
the CSV reader drops as encoding before it looks for the `#` preamble.

The four KML and LandXML capabilities added in S33 come from
scripts/solar_geo_formats_probes.py, and their eighteen licensed files are
committed in the same folder. Fifteen are XML documents, read through the XML
path: one row per file, keyed by its name, its lines verbatim.

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
harness_probes = load_module("solar_harness_bom_probes")
terrain_probes = load_module("solar_terrain_probes")
geo_probes = load_module("solar_geo_formats_probes")
# The XLSX reader comes from the module under test for the same reason the
# comparator does: one module object, one exception identity.
xlsx = normalizer.xlsx
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


# --------------------------------------------------------------------------- #
# S31 helpers: the harness plan (a PIPE-separated CSV) and the three workbooks.
# --------------------------------------------------------------------------- #
HARNESS_FILES = sorted(harness_probes.FILE_PROBE_TYPES)
HARNESS_WORKBOOKS = tuple(name for name in HARNESS_FILES if name.endswith(".xlsx"))
HARNESS_CSV = "leafharnessplan_demo.csv"
# The one OUTPUT cell each file's alteration moves. The CSV target is a column
# the planner computes; a workbook target is the Overview total, which no
# declared input names, so the fixture hash must stay put in both cases.
HARNESS_ALTERED = {HARNESS_CSV: ("csv-pipe", "TotalCableLengthM")}
for _name in HARNESS_WORKBOOKS:
    HARNESS_ALTERED[_name] = ("xlsx", ("Overview", 2, 1))
del _name


def harness_studio_file(folder, name):
    """Write Studio's own copy of one S31 file and return its path."""
    for path in harness_probes.write(harness_probes.FILE_DEMOS[name], folder):
        if path.name == name:
            return path
    raise AssertionError("demo did not write " + name)


def harness_evidence_for(path, name, side):
    return normalizer.build_evidence_from_file(
        path, capability=harness_probes.FILE_CAPABILITIES[name],
        probe_type=harness_probes.FILE_PROBE_TYPES[name], side=side, revision=REVISION)


def harness_verdict(plugin, studio, name):
    return compare.compare(plugin, studio, "exports",
                           capability=harness_probes.FILE_CAPABILITIES[name])


def harness_altered_file(folder, name):
    """Studio's file with ONE OUTPUT changed and every declared input untouched."""
    kind, target = HARNESS_ALTERED[name]
    path = folder / ("altered_" + name)
    if kind == "csv-pipe":
        lines = (folder / name).read_bytes().decode("ascii").replace("\r\n", "\n").split("\n")
        header = lines[0].split("|")
        cells = lines[1].split("|")
        column = header.index(target)
        cells[column] = "0.25" if cells[column] != "0.25" else "0.75"
        lines[1] = "|".join(cells)
        path.write_bytes("\r\n".join(lines).encode("ascii"))
        return path
    sheet_name, row_index, column = target
    sheets = xlsx.read_workbook_file(folder / name)
    altered = []
    for sheet in sheets:
        rows = []
        for row in sheet.rows:
            cells = list(row.cells)
            if sheet.name == sheet_name and row.index == row_index:
                cells[column] = 4242
            rows.append((row.index, cells))
        altered.append((sheet.name, rows))
    path.write_bytes(xlsx.write_workbook(altered))
    return path


# --------------------------------------------------------------------------- #
# S32 helpers: the six terrain and irradiance CSVs, four of them carrying the
# byte order mark Encoding.UTF8 writes.
# --------------------------------------------------------------------------- #
TERRAIN_FILES = sorted(terrain_probes.FILE_PROBE_TYPES)
# The one OUTPUT column each alteration moves in the first data row: the
# section's quantity, which no declared input names, so the fixture hash must
# stay put while the output hash moves.
TERRAIN_ALTERED = {name: normalizer.PROBE_SPECS[probe_type].sections[0].quantity_field
                   for name, probe_type in terrain_probes.FILE_PROBE_TYPES.items()}


def terrain_studio_file(folder, name):
    """Write Studio's own copy of one S32 file and return its path."""
    for path in terrain_probes.write(terrain_probes.FILE_DEMOS[name], folder):
        if path.name == name:
            return path
    raise AssertionError("demo did not write " + name)


def terrain_evidence_for(path, name, side):
    return normalizer.build_evidence_from_file(
        path, capability=terrain_probes.FILE_CAPABILITIES[name],
        probe_type=terrain_probes.FILE_PROBE_TYPES[name], side=side, revision=REVISION)


def terrain_verdict(plugin, studio, name):
    return compare.compare(plugin, studio, "exports",
                           capability=terrain_probes.FILE_CAPABILITIES[name])


def terrain_altered_file(folder, name):
    """Studio's file with ONE OUTPUT changed; its BOM, preamble and CRLFs kept."""
    text = (folder / name).read_bytes().decode("utf-8")
    bom = "﻿" if text.startswith("﻿") else ""
    lines = text[len(bom):].replace("\r\n", "\n").split("\n")
    start = 0
    while lines[start].startswith("#"):
        start += 1
    header = lines[start].split(",")
    cells = lines[start + 1].split(",")
    column = header.index(TERRAIN_ALTERED[name])
    cells[column] = "0.25" if cells[column] != "0.25" else "0.75"
    lines[start + 1] = ",".join(cells)
    path = folder / ("altered_" + name)
    path.write_bytes((bom + "\r\n".join(lines)).encode("utf-8"))
    return path


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
    """Every probe type's format has a reader, and nothing else is claimed."""
    assert sorted(normalizer.READERS) == sorted([
        "csv", "csv-pipe", "json-metrics", "json-probes", normalizer.XLSX_BOM_FORMAT,
        normalizer.KML_FORMAT, normalizer.LANDXML_FORMAT])
    assert all(spec.file_format in normalizer.READERS
               for spec in normalizer.PROBE_SPECS.values())
    assert normalizer.TEXT_CELL_FORMATS <= set(normalizer.READERS)
    assert normalizer.BINARY_FORMATS <= set(normalizer.READERS)
    # A text format is never binary, and the XLSX format NAMES the sheet list
    # its reader then checks, so the recorded `format` is a claim under test.
    assert not normalizer.TEXT_CELL_FORMATS & normalizer.BINARY_FORMATS
    assert normalizer.XLSX_BOM_FORMAT == "xlsx:" + "|".join(normalizer.XLSX_BOM_SHEETS)


def test_an_unknown_format_fails_closed():
    spec = normalizer.ProbeSpec("xlsx:Nope", [
        normalizer.Section(None, "t", "Key", (), "Cells")])
    with pytest.raises(compare.InputError):
        normalizer.build_evidence([{"Key": "a:1", "Cells": [1]}], capability="tracker-bom-xlsx-export",
                                  probe_type="unknown-probe-type", side="studio",
                                  revision=REVISION, survived_reopen=True,
                                  file_name="p.xlsx", file_sha256="0" * 64)
    assert spec.file_format not in normalizer.READERS


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


# --------------------------------------------------------------------------- #
# S31: rule E5's XLSX projection and the pipe-separated CSV
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", HARNESS_FILES)
def test_harness_evidence_is_comparator_valid(tmp_path, name):
    evidence = harness_evidence_for(harness_studio_file(tmp_path, name), name, "studio")
    compare.validate_evidence(evidence, "exports")
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert evidence["parameters"] == {"family": "exports",
                                      "capability": harness_probes.FILE_CAPABILITIES[name]}
    assert evidence["after"]["format"] == \
        normalizer.PROBE_SPECS[harness_probes.FILE_PROBE_TYPES[name]].file_format
    assert evidence["survived_reopen"] is True
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert evidence["entity_mapping"] == {identifier: identifier for identifier in ids}
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("name", HARNESS_FILES)
def test_harness_studio_compares_pass_against_the_committed_capture(tmp_path, name):
    """The licensed capture is committed here, so this is the real comparison."""
    studio = harness_evidence_for(harness_studio_file(tmp_path, name), name, "studio")
    licensed = harness_evidence_for(CALC_REFERENCE_DIR / name, name, "plugin")
    result = harness_verdict(licensed, studio, name)
    assert result["verdict"] == "pass", result["diffs"][:5]


@pytest.mark.parametrize("name", HARNESS_FILES)
def test_harness_fixture_hash_is_inputs_only(tmp_path, name):
    harness_studio_file(tmp_path, name)
    original = harness_evidence_for(tmp_path / name, name, "studio")
    changed = harness_evidence_for(harness_altered_file(tmp_path, name), name, "studio")
    assert original["fixture_sha256"] == changed["fixture_sha256"]
    assert original["input_sha256"] == changed["input_sha256"]
    assert original["output_sha256"] != changed["output_sha256"]


@pytest.mark.parametrize("name", HARNESS_FILES)
def test_harness_one_altered_result_is_a_diff(tmp_path, name):
    harness_studio_file(tmp_path, name)
    good = harness_evidence_for(tmp_path / name, name, "studio")
    bad = harness_evidence_for(harness_altered_file(tmp_path, name), name, "plugin")
    result = harness_verdict(bad, good, name)
    assert result["verdict"] == "fail"
    assert all(path.startswith("after/rows/") for path in result["diffs"]), result["diffs"]
    assert len({path.split("/")[2] for path in result["diffs"]}) == 1, result["diffs"]


def test_xlsx_rows_are_keyed_by_sheet_and_row_index(tmp_path):
    """Rule E5: `<sheet>:<row index>`, in sheet order, non-empty rows only."""
    path = harness_studio_file(tmp_path, "leafbomxlsx_demo.xlsx")
    rows = harness_evidence_for(path, "leafbomxlsx_demo.xlsx", "studio")["after"]["rows"]
    ids = [row["id"]["entity_id"] for row in rows]
    assert ids[0] == "Overview:1"
    assert ids[6] == "Overview:7"
    assert ids[7] == "By Area:1"
    # The placeholder tab holds exactly one row, and the sheets keep their order.
    assert "Cable Tray:1" in ids and "Cable Tray:2" not in ids
    sheets = [identifier.rsplit(":", 1)[0] for identifier in ids]
    assert list(dict.fromkeys(sheets)) == list(normalizer.XLSX_BOM_SHEETS)
    assert {row["type"] for row in rows} == {"tracker-bom-sheet-row"}


def test_xlsx_cells_ride_verbatim_and_the_quantity_is_their_count(tmp_path):
    """Cells are VALUES, not text: an XLSX cell already carries its type."""
    path = harness_studio_file(tmp_path, "leafbomxlsx_demo.xlsx")
    rows = harness_evidence_for(path, "leafbomxlsx_demo.xlsx", "studio")["after"]["rows"]
    by_id = {row["id"]["entity_id"]: row for row in rows}
    assert by_id["Overview:2"]["fields"] == {
        "Key": "Overview:2", "Cells": ["Total rows (sections)", 3, "ea"]}
    assert by_id["Overview:2"]["quantity"] == {"kind": "float", "value": 3, "unit": "none"}
    assert by_id["Piling:2"]["fields"]["Cells"] == [1, 0, 0, 0, 0, 1.2]
    # A one-cell placeholder row is a real row with a count of one.
    assert by_id["Cable Tray:1"]["quantity"]["value"] == 1


def test_the_two_empty_fixtures_differ_only_in_the_cable_tray_row(tmp_path):
    """F1 passes null lists and F2 empty ones; the projection must keep them apart."""
    for name in ("leafbomxlsxempty_demo_f1.xlsx", "leafbomxlsxempty_demo_f2.xlsx"):
        harness_studio_file(tmp_path, name)
    rows = {}
    for name in ("leafbomxlsxempty_demo_f1.xlsx", "leafbomxlsxempty_demo_f2.xlsx"):
        rows[name] = {row["id"]["entity_id"]: row["fields"]["Cells"]
                      for row in harness_evidence_for(tmp_path / name, name,
                                                      "studio")["after"]["rows"]}
    f1, f2 = rows.values()
    assert set(f1) == set(f2)
    assert [key for key in f1 if f1[key] != f2[key]] == ["Cable Tray:1"]


def test_a_workbook_whose_sheets_are_not_the_named_list_is_refused(tmp_path):
    """The format string NAMES the tab contract, so a dropped tab is a refusal."""
    path = harness_studio_file(tmp_path, "leafbomxlsx_demo.xlsx")
    short = tmp_path / "short.xlsx"
    sheets = xlsx.read_workbook_file(path)
    short.write_bytes(xlsx.write_workbook(
        [(sheet.name, [(row.index, row.cells) for row in sheet.rows])
         for sheet in sheets[:-1]]))
    with pytest.raises(compare.InputError):
        harness_evidence_for(short, "leafbomxlsx_demo.xlsx", "studio")


@pytest.mark.parametrize("payload", [b"", b"not a zip", b"PK\x03\x04 truncated"])
def test_an_unreadable_workbook_is_refused_not_answered(tmp_path, payload):
    path = tmp_path / "probe.xlsx"
    path.write_bytes(payload)
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(
            path, capability="tracker-bom-xlsx-export", probe_type="tracker-bom-xlsx",
            side="studio", revision=REVISION)


def test_a_binary_format_is_read_as_bytes(tmp_path):
    """A zip has no text decoding; decoding it first would corrupt rather than fail."""
    assert normalizer.XLSX_BOM_FORMAT in normalizer.BINARY_FORMATS
    path = harness_studio_file(tmp_path, "leafharness_bom_demo.xlsx")
    raw = path.read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert harness_evidence_for(path, "leafharness_bom_demo.xlsx", "studio")["after"]["rows"]


def test_the_pipe_separated_csv_splits_on_pipes_not_commas(tmp_path):
    """Read with the comma reader this file is ONE column holding the whole line."""
    path = harness_studio_file(tmp_path, HARNESS_CSV)
    rows = harness_evidence_for(path, HARNESS_CSV, "studio")["after"]["rows"]
    assert len(rows) == 23
    by_id = {row["id"]["entity_id"]: row for row in rows}
    assert by_id["A1"]["fields"] == {
        "Label": "A1", "Type": "EndOfRow", "ModuleCount": "10", "ModulePitchM": "1",
        "TrunkTapFromStartM": "0", "StringMaxAmps": "10", "IsTrackerRow": "True",
        "ResultType": "OK", "CableGaugeAwg": "10", "Connectors": "10",
        "TotalCableLengthM": "45", "Drops": "0;1;2;3;4;5;6;7;8;9"}
    assert by_id["A1"]["quantity"] == {"kind": "float", "value": 45.0, "unit": "none"}
    assert {row["type"] for row in rows} == {"harness-cable-plan"}


def test_an_error_rows_blank_quantity_is_the_declared_non_numeric_case(tmp_path):
    path = harness_studio_file(tmp_path, HARNESS_CSV)
    rows = harness_evidence_for(path, HARNESS_CSV, "studio")["after"]["rows"]
    by_id = {row["id"]["entity_id"]: row for row in rows}
    assert by_id["E1"]["quantity"]["value"] == 0.0
    assert by_id["E1"]["fields"]["TotalCableLengthM"] == ""
    # The refusal message is an OUTPUT and compares byte-exact under `fields`.
    assert by_id["E1"]["fields"]["Drops"].startswith("Parallel trunk harness")
    assert by_id["E3"]["fields"]["ResultType"] == "ERROR_AMPS"


def test_the_harness_plan_fixture_is_its_declared_inputs_only(tmp_path):
    """Rule E1: an output column must never reach the fixture projection."""
    path = harness_studio_file(tmp_path, HARNESS_CSV)
    rows, fixture = normalizer.project(
        normalizer.READERS["csv-pipe"](path.read_bytes().decode("ascii")),
        "harness-cable-plan")
    assert len(rows) == len(fixture) == 23
    assert set(fixture[0]["inputs"]) == {
        "Type", "ModuleCount", "ModulePitchM", "TrunkTapFromStartM",
        "StringMaxAmps", "IsTrackerRow"}


def test_cli_writes_xlsx_evidence(tmp_path):
    path = harness_studio_file(tmp_path, "leafbomxlsx_demo.xlsx")
    output = tmp_path / "evidence.json"
    code = normalizer.main(["--probe-file", str(path),
                            "--capability", "tracker-bom-xlsx-export",
                            "--probe-type", "tracker-bom-xlsx", "--side", "studio",
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


# --------------------------------------------------------------------------- #
# S32: the six terrain and irradiance capabilities, licensed captures COMMITTED
# --------------------------------------------------------------------------- #
def test_terrain_captures_are_committed():
    """A subset check: this block's own six files, never the folder's whole list."""
    for name in TERRAIN_FILES:
        assert (CALC_REFERENCE_DIR / name).is_file(), name


@pytest.mark.parametrize("name", TERRAIN_FILES)
def test_terrain_evidence_is_comparator_valid(tmp_path, name):
    evidence = terrain_evidence_for(terrain_studio_file(tmp_path, name), name, "studio")
    compare.validate_evidence(evidence, "exports")
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert evidence["parameters"] == {"family": "exports",
                                      "capability": terrain_probes.FILE_CAPABILITIES[name]}
    assert evidence["after"]["format"] == "csv"
    assert evidence["survived_reopen"] is True
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert evidence["entity_mapping"] == {identifier: identifier for identifier in ids}
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("name", TERRAIN_FILES)
def test_terrain_studio_compares_pass_against_the_committed_capture(tmp_path, name):
    """The licensed capture is committed here, so this is the real comparison."""
    studio = terrain_evidence_for(terrain_studio_file(tmp_path, name), name, "studio")
    licensed = terrain_evidence_for(CALC_REFERENCE_DIR / name, name, "plugin")
    result = terrain_verdict(licensed, studio, name)
    assert result["verdict"] == "pass", result["diffs"][:5]


@pytest.mark.parametrize("name", TERRAIN_FILES)
def test_terrain_fixture_hash_is_inputs_only(tmp_path, name):
    terrain_studio_file(tmp_path, name)
    original = terrain_evidence_for(tmp_path / name, name, "studio")
    changed = terrain_evidence_for(terrain_altered_file(tmp_path, name), name, "studio")
    assert original["fixture_sha256"] == changed["fixture_sha256"]
    assert original["input_sha256"] == changed["input_sha256"]
    assert original["output_sha256"] != changed["output_sha256"]


@pytest.mark.parametrize("name", TERRAIN_FILES)
def test_terrain_one_altered_result_is_a_diff(tmp_path, name):
    terrain_studio_file(tmp_path, name)
    good = terrain_evidence_for(tmp_path / name, name, "studio")
    bad = terrain_evidence_for(terrain_altered_file(tmp_path, name), name, "plugin")
    result = terrain_verdict(bad, good, name)
    assert result["verdict"] == "fail"
    assert all(path.startswith("after/rows/") for path in result["diffs"]), result["diffs"]
    assert len({path.split("/")[2] for path in result["diffs"]}) == 1, result["diffs"]


def test_a_byte_order_mark_is_encoding_not_content(tmp_path):
    """Encoding.UTF8's BOM must neither hide the `#` preamble nor rename a column."""
    path = terrain_studio_file(tmp_path, "leafcapacity_demo.csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf# LEAFCAPACITYITERATEDEMO")
    rows = terrain_evidence_for(path, "leafcapacity_demo.csv", "studio")["after"]["rows"]
    assert len(rows) == 10
    assert sorted(rows[0]["fields"]) == sorted(["gcr", "tilt_deg", "pitch_m", "row_count",
                                                "module_count", "dc_kwp", "acres_used"])
    assert rows[0]["fields"]["gcr"] == "0.5000"
    assert rows[0]["quantity"] == {"kind": "float", "value": 705.6, "unit": "kWp"}


def test_terrain_rows_keep_the_files_own_order(tmp_path):
    """Rule E3: the horizon rows are its azimuths, in the file's order."""
    path = terrain_studio_file(tmp_path, "leafhorizon_demo.csv")
    rows = terrain_evidence_for(path, "leafhorizon_demo.csv", "studio")["after"]["rows"]
    assert [row["id"]["entity_id"] for row in rows] == \
        ["%d.000" % (10 * index) for index in range(36)]
    assert rows[0]["fields"]["horizon_elevation_deg"] == "2.862405"


def test_terrain_row_types_name_the_calculation(tmp_path):
    expected = {
        "leafheatmap_demo.csv": ("cut-fill-heatmap-cell", 49),
        "leafhorizon_demo.csv": ("horizon-profile-sample", 36),
        "leafpoa_demo.csv": ("plane-of-array-irradiance", 18),
        "leafgradingpad_demo.csv": ("grading-pad-toe-corner", 8),
        "leafcrosssection_demo.csv": ("terrain-cross-section-sample", 100),
        "leafcapacity_demo.csv": ("capacity-iteration-scenario", 10),
    }
    for name, (row_type, count) in expected.items():
        rows = terrain_evidence_for(terrain_studio_file(tmp_path, name), name,
                                    "studio")["after"]["rows"]
        assert {row["type"] for row in rows} == {row_type}, name
        assert len(rows) == count, name


# --------------------------------------------------------------------------- #
# S33: the KML and LandXML capabilities, licensed captures COMMITTED. Fifteen of
# the eighteen files are XML documents and take the XML path.
# --------------------------------------------------------------------------- #
GEO_FILES = sorted(geo_probes.FILE_PROBE_TYPES)
GEO_XML_FILES = [name for name in GEO_FILES
                 if normalizer.PROBE_SPECS[geo_probes.FILE_PROBE_TYPES[name]].file_format
                 in normalizer.NAMED_FORMATS]


def geo_studio_file(folder, name):
    """Write Studio's own copy of one S33 file and return its path.

    The import report is written after the export it re-reads, as --all does.
    """
    if geo_probes.FILE_DEMOS[name] == "leafkmlimport":
        geo_probes.write("leafkmlexport", folder)
    for path in geo_probes.write(geo_probes.FILE_DEMOS[name], folder):
        if path.name == name:
            return path
    raise AssertionError("demo did not write " + name)


def geo_evidence_for(path, name, side):
    return normalizer.build_evidence_from_file(
        path, capability=geo_probes.FILE_CAPABILITIES[name],
        probe_type=geo_probes.FILE_PROBE_TYPES[name], side=side, revision=REVISION)


def geo_verdict(plugin, studio, name):
    return compare.compare(plugin, studio, "exports",
                           capability=geo_probes.FILE_CAPABILITIES[name])


def geo_altered_file(folder, name):
    """Studio's file with ONE OUTPUT changed, written under the SAME name.

    An XML file's row id is its name, so the altered copy lives in a subfolder
    rather than under a new name. An XML document gains a comment after its
    declaration, which keeps it well-formed and moves exactly one line; a CSV
    has the section's quantity changed in its first data row, which no declared
    input names. BOM, preamble and CRLFs are kept.
    """
    text = (folder / name).read_bytes().decode("utf-8")
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom):]
    if name in GEO_XML_FILES:
        body = body.replace("?>", "?><!--altered-->", 1)
    else:
        lines = body.replace("\r\n", "\n").split("\n")
        start = 0
        while lines[start].startswith("#"):
            start += 1
        header = lines[start].split(",")
        cells = lines[start + 1].split(",")
        column = header.index(normalizer.PROBE_SPECS[
            geo_probes.FILE_PROBE_TYPES[name]].sections[0].quantity_field)
        cells[column] = "0.25" if cells[column] != "0.25" else "0.75"
        lines[start + 1] = ",".join(cells)
        body = "\r\n".join(lines)
    altered = folder / "altered"
    altered.mkdir(exist_ok=True)
    path = altered / name
    path.write_bytes((bom + body).encode("utf-8"))
    return path


def test_geo_captures_are_committed():
    """A subset check: this block's own eighteen files, never the folder's whole list."""
    assert len(GEO_FILES) == 18 and len(GEO_XML_FILES) == 15
    for name in GEO_FILES:
        assert (CALC_REFERENCE_DIR / name).is_file(), name


@pytest.mark.parametrize("name", GEO_FILES)
def test_geo_evidence_is_comparator_valid(tmp_path, name):
    evidence = geo_evidence_for(geo_studio_file(tmp_path, name), name, "studio")
    compare.validate_evidence(evidence, "exports")
    assert set(evidence) == compare.EVIDENCE_KEYS
    assert evidence["parameters"] == {"family": "exports",
                                      "capability": geo_probes.FILE_CAPABILITIES[name]}
    assert evidence["after"]["format"] == normalizer.PROBE_SPECS[
        geo_probes.FILE_PROBE_TYPES[name]].file_format
    assert evidence["survived_reopen"] is True
    ids = [row["id"]["entity_id"] for row in evidence["after"]["rows"]]
    assert evidence["entity_mapping"] == {identifier: identifier for identifier in ids}
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("name", GEO_FILES)
def test_geo_studio_compares_pass_against_the_committed_capture(tmp_path, name):
    """The licensed capture is committed here, so this is the real comparison."""
    studio = geo_evidence_for(geo_studio_file(tmp_path, name), name, "studio")
    licensed = geo_evidence_for(CALC_REFERENCE_DIR / name, name, "plugin")
    result = geo_verdict(licensed, studio, name)
    assert result["verdict"] == "pass", result["diffs"][:5]


@pytest.mark.parametrize("name", GEO_FILES)
def test_geo_fixture_hash_is_inputs_only(tmp_path, name):
    geo_studio_file(tmp_path, name)
    original = geo_evidence_for(tmp_path / name, name, "studio")
    changed = geo_evidence_for(geo_altered_file(tmp_path, name), name, "studio")
    assert original["fixture_sha256"] == changed["fixture_sha256"]
    assert original["input_sha256"] == changed["input_sha256"]
    assert original["output_sha256"] != changed["output_sha256"]


@pytest.mark.parametrize("name", GEO_FILES)
def test_geo_one_altered_result_is_a_diff(tmp_path, name):
    geo_studio_file(tmp_path, name)
    good = geo_evidence_for(tmp_path / name, name, "studio")
    bad = geo_evidence_for(geo_altered_file(tmp_path, name), name, "plugin")
    result = geo_verdict(bad, good, name)
    assert result["verdict"] == "fail"
    assert all(path.startswith("after/rows/") for path in result["diffs"]), result["diffs"]
    assert len({path.split("/")[2] for path in result["diffs"]}) == 1, result["diffs"]


def test_xml_lines_are_the_canonical_text(tmp_path):
    """BOM dropped and CRLF made LF, and nothing else: the lines rejoin to the file."""
    for name in ("leaflandxml_fixed.xml", "leafkmlexport_demo.kml"):
        raw = geo_studio_file(tmp_path, name).read_bytes().decode("utf-8")
        rows = geo_evidence_for(tmp_path / name, name, "studio")["after"]["rows"]
        assert len(rows) == 1
        assert rows[0]["id"] == {"entity_id": name}
        canonical = raw.lstrip("﻿").replace("\r\n", "\n")
        assert "\n".join(rows[0]["fields"]["Lines"]) == canonical
        assert rows[0]["fields"]["File"] == name


def test_xml_quantity_is_the_named_structural_count(tmp_path):
    expected = {"leafkmlexport_demo.kml": (3, "placemarks", "kml-document"),
                "leafkmlexportfmt_demo_F9_NULL_POLYGON_SKIP.xml": (2, "placemarks",
                                                                  "kml-document"),
                "leaflandxml_delaunay.xml": (800, "faces", "landxml-surface")}
    for name, (count, unit, row_type) in expected.items():
        row = geo_evidence_for(geo_studio_file(tmp_path, name), name,
                               "studio")["after"]["rows"][0]
        assert row["quantity"] == {"kind": "float", "value": count, "unit": unit}, name
        assert row["type"] == row_type


def test_xml_reader_checks_the_root_the_format_names(tmp_path):
    """A KML file read as LandXML is refused, never quietly counted as zero faces."""
    path = geo_studio_file(tmp_path, "leafkmlexport_demo.kml")
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(path, capability="landxml-export",
                                            probe_type="landxml-surface", side="studio",
                                            revision=REVISION)


def test_xml_reader_refuses_hostile_xml(tmp_path):
    path = tmp_path / "hostile.kml"
    path.write_bytes(b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">'
                     b'<!ENTITY b "&a;&a;&a;&a;">]><kml><name>&b;</name></kml>')
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(path, capability="kml-export",
                                            probe_type="kml-document", side="studio",
                                            revision=REVISION)


def test_xml_reader_refuses_a_line_past_the_comparators_string_bound(tmp_path):
    path = tmp_path / "long.kml"
    path.write_bytes(("<kml><name>" + "x" * 16384 + "</name></kml>").encode("utf-8"))
    with pytest.raises(compare.InputError):
        normalizer.build_evidence_from_file(path, capability="kml-export",
                                            probe_type="kml-document", side="studio",
                                            revision=REVISION)


def test_geo_csv_row_types_name_the_calculation(tmp_path):
    expected = {
        "leafkmlexportfmt_demo.csv": ("kml-export-format-fixture", 13),
        "leafkmlimport_demo.csv": ("kml-import-vertex", 12),
        "leafimportlandxml_demo.csv": ("landxml-import-point", 15),
    }
    for name, (row_type, count) in expected.items():
        rows = geo_evidence_for(geo_studio_file(tmp_path, name), name,
                                "studio")["after"]["rows"]
        assert {row["type"] for row in rows} == {row_type}, name
        assert len(rows) == count, name
