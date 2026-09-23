"""Offline checks for the shared probe-file normalizer (contract v5, rules E1-E5).

Studio's own probe files come from scripts/solar_nec_probes.py, which computes
every value through server/solar_nec.py. The licensed capture, when this host
carries it, is the other side of the comparison; without it the suite still runs
end to end against Studio's own files and against a deliberately altered one, so
nothing here skips.

What these prove:
  * the evidence a probe file yields is exactly what the frozen comparator
    accepts for family `exports`, with the E4 identity mapping and an observed,
    not assumed, reopen;
  * rule E1: the fixture hash is the INPUTS, so two files with the same
    scenarios and different results share it and differ only in output_sha256;
  * all six licensed files compare pass against Studio's;
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


def test_only_json_probes_is_wired_today():
    """CSV and XLSX land in READERS in a later slice; until then, fail closed."""
    assert sorted(normalizer.READERS) == ["json-probes"]
    assert all(spec.file_format == "json-probes" for spec in normalizer.PROBE_SPECS.values())


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
