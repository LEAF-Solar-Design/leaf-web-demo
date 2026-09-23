"""Studio's G32 design-profile evidence (f1 to f5).

The committed generate intake and the committed settings snapshot (both inputs only) run the
whole chain from an empty profile store: f1 creates Alpha (adopt No), f2 creates Beta, f3 swaps
back to A, f4 lists (read only), f5 deletes B. Covered: every step's capability, operation and
answers; the design-profile rows in stored order with the canonical settings text; the
active-prefix and profile-version report rows; f4's list rows and the read-only rule; the
comparator accepting every document against itself; one step equal to the same step of a full
run; the refusals; the untracked-intake CLI refusal; the snapshot carrying no storage names.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dev = _load("solar_design_profiles_evidence")
compare = dev.compare
profiles = dev.profiles
REVISION = "0123456789abcdef0123456789abcdef01234567"


def intake():
    return json.loads(dev.DEFAULT_INTAKE.read_text(encoding="utf-8"))


def snapshot():
    return json.loads(dev.DEFAULT_SETTINGS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run():
    return dev.run_steps(intake(), snapshot(), REVISION)


def rows_of(doc, row_type=None):
    return [r for r in doc["after"]["rows"] if row_type is None or r["type"] == row_type]


def reports(doc):
    return {r["name"]: r["value"] for r in rows_of(doc, "report")}


def profile_rows(doc):
    return [(r["id"]["entity_id"], r["name"], r["prefix"]) for r in rows_of(doc, "design-profile")]


def test_every_step_in_order_with_its_capability_and_answers(run):
    docs, _, _ = run
    assert list(docs) == ["f1", "f2", "f3", "f4", "f5"]
    assert {d["provenance"]["capability"] for d in docs.values()} == {"design-profile-manage"}
    assert [d["provenance"]["operation"] for d in docs.values()] == ["create", "create", "swap", "list", "delete"]
    assert [d["parameters"]["answers"] for d in docs.values()] == \
        [["Create", "Alpha", "No"], ["Create", "Beta"], ["Swap", "A"], ["List"], ["Delete", "B", "Yes"]]
    for doc in docs.values():
        assert doc["versions"]["engine"] == "server-builtin" and doc["versions"]["capability"] == "0"
        assert doc["provenance"]["fixture_kind"] == "generate"
        assert set(doc["parameters"]) == {"units_keyword", "grid_cells_long_axis", "active_preset", "answers"}
    assert len({d["fixture_sha256"] for d in docs.values()}) == 1
    assert len({d["input_sha256"] for d in docs.values()}) == 5


def test_f1_state_rows(run):
    docs, _, messages = run
    doc = docs["f1"]
    assert [r["id"]["entity_id"] for r in rows_of(doc)] == \
        ["design-profile-1", "report-active-prefix", "report-profile-version"]
    (row,) = rows_of(doc, "design-profile")
    assert (row["name"], row["prefix"], row["quantity"], row["unit"]) == ("Alpha", "A", 1, "each")
    assert row["settings"] == profiles.canonical_settings_text(snapshot())
    assert reports(doc) == {"active-prefix": "A", "profile-version": 1}
    assert messages["f1"] == ["Profile 'Alpha' created (prefix: A).", "  Active profile is now: Alpha"]


def test_f2_beta_settings_differ_from_alpha_only_by_name(run):
    docs, _, _ = run
    doc = docs["f2"]
    assert profile_rows(doc) == [("design-profile-1", "Alpha", "A"), ("design-profile-2", "Beta", "B")]
    alpha, beta = (json.loads(r["settings"]) for r in rows_of(doc, "design-profile"))
    assert alpha == snapshot() and beta == dict(snapshot(), Name="Beta")
    assert reports(doc) == {"active-prefix": "B", "profile-version": 1}


def test_f3_swap_back_to_a(run):
    docs, _, messages = run
    doc = docs["f3"]
    assert profile_rows(doc) == profile_rows(docs["f2"])
    assert [r["settings"] for r in rows_of(doc, "design-profile")] == \
        [r["settings"] for r in rows_of(docs["f2"], "design-profile")]
    assert reports(doc) == {"active-prefix": "A", "profile-version": 1}
    assert messages["f3"] == ["Swapped to profile 'Alpha' (prefix: A)."]


def test_f4_list_rows_and_read_only(run):
    docs, _, _ = run
    doc = docs["f4"]
    assert [r["id"]["entity_id"] for r in rows_of(doc)] == \
        ["report-profile-1", "report-profile-2", "report-profiles"]
    assert reports(doc) == {"profiles": 2, "profile-1": "A Alpha", "profile-2": "B Beta"}
    assert not rows_of(doc, "unexpected-change")


def test_f5_delete_b(run):
    docs, state, messages = run
    doc = docs["f5"]
    assert profile_rows(doc) == [("design-profile-1", "Alpha", "A")]
    assert reports(doc) == {"active-prefix": "A", "profile-version": 1}
    assert messages["f5"] == ["Profile 'Beta' deleted.", "  Active profile is now: Alpha"]
    assert state["settings"] == profiles.current_settings_from_preset(snapshot())


def test_read_only_rule_flags_a_moved_state(monkeypatch):
    state = dev.new_state(snapshot())
    real = profiles.leafprofile

    def moving(mgr, settings, answers, **kw):
        out = real(mgr, settings, answers, **kw)
        settings["NumMppt"] += 1
        return out

    monkeypatch.setattr(profiles, "leafprofile", moving)
    rows = dev.step_rows("f4", state)
    assert [r["id"] for r in rows] == ["report-profiles", "unexpected-change-1"]


def test_comparator_accepts_every_document_against_itself(run):
    docs, _, _ = run
    for doc in docs.values():
        result = compare.compare(doc, doc, "exports", capability=doc["versions"]["capability"])
        assert result["verdict"] == "pass", result["diffs"]


def test_one_step_equals_the_same_step_of_a_full_run(run):
    docs, _, _ = run
    only, _, _ = dev.run_steps(intake(), snapshot(), REVISION, only="f3")
    assert list(only) == ["f3"] and only["f3"] == docs["f3"]


def test_refusals():
    with pytest.raises(dev.EvidenceError):
        dev.run_steps(intake(), snapshot(), REVISION, only="f6")
    with pytest.raises(dev.EvidenceError):
        dev.run_steps(intake(), dict(snapshot(), NumMppt="12"), REVISION)
    with pytest.raises(dev.EvidenceError):
        dev.step_rows("f1", {"manager": None, "settings": {}})
    with pytest.raises(dev.EvidenceError, match="revision"):
        dev.build_document(intake(), "f1", "create", [], "HEAD")
    with pytest.raises(dev.EvidenceError):
        dev.report_row("Active Prefix", "A")
    with pytest.raises(ValueError):
        dev.run_steps(dict(intake(), extra=1), snapshot(), REVISION)


def test_cli_refuses_an_untracked_intake(tmp_path):
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(intake()), encoding="utf-8")
    out = tmp_path / "out"
    assert dev.main(["--intake", str(path), "--out-dir", str(out)]) == 2
    assert not out.exists()


def test_committed_snapshot_names_no_storage():
    text = dev.DEFAULT_SETTINGS.read_text(encoding="utf-8")
    assert not re.search(r'LEAF-PROPERTIES|LEAFSETTINGS|xrecord|regapp|"LEAF"', text, re.IGNORECASE)
    assert profiles.validate_preset(json.loads(text))["Name"] == "Alpha"
