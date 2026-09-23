"""Tests for the solar parity oracle.

Every case builds its ledger and receipts under tmp_path. The only repo file any
test reads is the shipped seed ledger, and it is read, never written.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "solar_parity_status.py"
REPO_ROOT = SCRIPT.parent.parent
SEED_LEDGER = REPO_ROOT / "docs" / "parity" / "solar-ledger.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("solar_parity_status", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = _load_module()


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def row(identifier, **overrides):
    base = {
        "global": identifier,
        "class": "T",
        "maturity": "production",
        "capability": "draw-array",
        "capability_version": "1",
        "family": "layout",
        "interaction": "command",
        "engine": "browser",
        "wave": 0,
        "status": "resolved",
        "exclusion_rationale": "",
        "alias_of": "",
        "evidence": "command_surface.py:1",
    }
    base.update(overrides)
    return base


def receipt(capability, **overrides):
    doc = {
        "schema": "leaf.solar-parity-receipt.v1",
        "capability": capability,
        "capability_version": "1",
        "fixture": {"id": "fx-1", "sha256": "a" * 64},
        "plugin": {"build": "2026.9.1", "state": "committed", "receipt_sha256": "b" * 64},
        "studio": {"capability_version": "1", "engine": "browser"},
        "comparator": {"name": "exact-counts-by-layer", "version": "1", "verdict": "pass", "diffs": []},
        "synthetic_fields": [],
        "fallback_fields": [],
        "synthetic_flagged": False,
        "survived_reopen": True,
        "produced_at": "2026-09-17T00:00:00Z",
    }
    doc.update(overrides)
    return doc


DIVERGENCE_FINDING = "docs/parity/divergences/autofillrevert-handle-drift.md"
DECLARED_DIFFS = ("group 12: membership differs", "group 41: membership differs")
DIVERGENCE_SUMMARY = "AUTOFILLREVERT skips the groups AutoFill rebuilt under new handles"


def divergence(finding=DIVERGENCE_FINDING, diffs=DECLARED_DIFFS, summary=DIVERGENCE_SUMMARY):
    return {"finding": finding, "declared_diffs": list(diffs), "summary": summary}


def diverging_receipt(capability="draw-array", diffs=DECLARED_DIFFS, block=None, **overrides):
    """A failing receipt that declares its diffs as a known plugin defect."""
    doc = receipt(capability, **overrides)
    doc["comparator"] = dict(doc["comparator"], verdict="fail", diffs=list(diffs))
    doc["divergence"] = divergence() if block is None else block
    return doc


def write_finding(tmp_path, reference=DIVERGENCE_FINDING):
    path = tmp_path.joinpath(*reference.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# known plugin defect\n", encoding="utf-8")
    return path


def write_ledger(tmp_path, rows, expected=None, **overrides):
    doc = {
        "schema": "leaf.solar-parity-ledger.v1",
        "registrations_expected": len(rows) if expected is None else expected,
        "source": "test fixture",
        "rows": rows,
    }
    doc.update(overrides)
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def write_receipt(tmp_path, capability, doc, name="fx-1.json"):
    directory = tmp_path / "receipts" / capability
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def receipts_dir(tmp_path):
    directory = tmp_path / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run(ledger, receipts, *extra, as_json=False):
    """Run main() in process and return (exit code, parsed json or None)."""
    argv = ["--ledger", str(ledger), "--receipts", str(receipts), *extra]
    if as_json:
        argv.append("--json")
    return status.main(argv)


def json_result(capsys, ledger, receipts, *extra):
    code = run(ledger, receipts, *extra, as_json=True)
    out = capsys.readouterr().out
    return code, json.loads(out)


def finding_codes(result):
    return [item["code"] for item in result["findings"]]


# ---------------------------------------------------------------------------
# the seed and the happy path
# ---------------------------------------------------------------------------


def test_empty_seed_ledger_fails_row_count(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [], expected=394)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--require", "w0")
    assert code == 1
    assert finding_codes(result) == ["ROW_COUNT"]
    assert result["ok"] is False


def test_shipped_ledger_is_reconciled_and_wave_one_is_open():
    """The shipped ledger reconciles all 394 registrations (w0 passes) while W1 still owes receipts."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--require", "w0", "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    result = json.loads(proc.stdout)
    assert result["ok"] is True and result["findings"] == []
    assert result["counts"]["rows"] == 394
    ledger = json.loads(SEED_LEDGER.read_text(encoding="utf-8"))
    assert ledger["schema"] == "leaf.solar-parity-ledger.v1"
    assert ledger["registrations_expected"] == 394
    assert len(ledger["rows"]) == 394
    assert len({row["global"] for row in ledger["rows"]}) == 394
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--require", "w1", "--json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 1, proc.stderr
    result = json.loads(proc.stdout)
    assert result["counts"]["duty_rows_in_scope"] > 0
    assert all(code in {"RECEIPT_MISSING", "RECEIPT_FAIL", "RECEIPT_STALE"} for code in finding_codes(result))


def test_valid_three_row_ledger_passes_all_production(tmp_path, capsys):
    rows = [
        row("LEAFARRAY", capability="draw-array", wave=0),
        row("LEAFVIEW", **{"class": "V", "wave": None, "exclusion_rationale": "read-only view", "capability": "view-pan"}),
        row("LEAFALIAS", **{"class": "A", "alias_of": "LEAFARRAY", "wave": None, "capability": "draw-array"}),
    ]
    ledger = write_ledger(tmp_path, rows, expected=3)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--require", "all-production")
    assert code == 0, result["findings"]
    assert result["ok"] is True
    assert result["counts"]["duty_rows_in_scope"] == 1
    assert result["counts"]["duty_rows_passing"] == 1


def test_default_require_matches_all_production(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0
    assert result["require"] == "all-production"


# ---------------------------------------------------------------------------
# ledger-level findings, one test per code
# ---------------------------------------------------------------------------


def test_row_count_mismatch(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=2)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["ROW_COUNT"]


def test_duplicate_global(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY"), row("LEAFARRAY")], expected=2)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["DUPLICATE_GLOBAL"]
    assert result["findings"][0]["global"] == "LEAFARRAY"


def test_unresolved_row(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY", status="unresolved")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["UNRESOLVED"]


def test_alias_dangling_when_target_absent(tmp_path, capsys):
    rows = [
        row("LEAFARRAY"),
        row("LEAFALIAS", **{"class": "A", "alias_of": "LEAFGONE", "wave": None}),
    ]
    ledger = write_ledger(tmp_path, rows, expected=2)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["ALIAS_DANGLING"]


def test_alias_dangling_when_target_is_also_an_alias(tmp_path, capsys):
    rows = [
        row("LEAFARRAY"),
        row("LEAFA", **{"class": "A", "alias_of": "LEAFARRAY", "wave": None}),
        row("LEAFB", **{"class": "A", "alias_of": "LEAFA", "wave": None}),
    ]
    ledger = write_ledger(tmp_path, rows, expected=3)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["ALIAS_DANGLING"]
    assert result["findings"][0]["global"] == "LEAFB"


def test_exclusion_missing(tmp_path, capsys):
    rows = [row("LEAFVIEW", **{"class": "H", "wave": None, "exclusion_rationale": "   "})]
    ledger = write_ledger(tmp_path, rows, expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["EXCLUSION_MISSING"]


def test_wave_missing_on_production_duty_row(tmp_path, capsys):
    rows = [row("LEAFARRAY", wave=None)]
    ledger = write_ledger(tmp_path, rows, expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["WAVE_MISSING"]


def test_preview_row_owes_no_wave_and_no_receipt(tmp_path, capsys):
    rows = [row("LEAFPREVIEW", maturity="preview", wave=None, capability="preview-thing")]
    ledger = write_ledger(tmp_path, rows, expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# receipt findings, one test per code
# ---------------------------------------------------------------------------


def test_receipt_missing(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_MISSING"]
    assert result["findings"][0]["capability"] == "draw-array"


def test_receipt_fail(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array")
    doc["comparator"] = dict(doc["comparator"], verdict="fail", diffs=["area"])
    write_receipt(tmp_path, "draw-array", doc)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_FAIL"]


def test_receipt_stale_on_capability_version(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY", capability_version="2")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_STALE"]


def test_receipt_stale_when_studio_version_disagrees(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array")
    doc["studio"] = dict(doc["studio"], capability_version="0")
    write_receipt(tmp_path, "draw-array", doc)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_STALE"]


def test_receipt_synthetic(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array", synthetic_fields=["tilt"], synthetic_flagged=False)
    write_receipt(tmp_path, "draw-array", doc)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_SYNTHETIC"]


def test_receipt_fallback_fields_need_the_flag(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array", fallback_fields=["azimuth"]))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_SYNTHETIC"]


def test_synthetic_fields_are_fine_when_flagged(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(
        tmp_path,
        "draw-array",
        receipt("draw-array", synthetic_fields=["tilt"], synthetic_flagged=True),
    )
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0


def test_receipt_not_committed(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array")
    doc["plugin"] = dict(doc["plugin"], state="dirty")
    write_receipt(tmp_path, "draw-array", doc)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_NOT_COMMITTED"]


def test_receipt_no_reopen(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array", survived_reopen=False))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_NO_REOPEN"]


def test_failing_receipt_beside_a_passing_one_passes(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    bad = receipt("draw-array")
    bad["comparator"] = dict(bad["comparator"], verdict="fail", diffs=["area"])
    write_receipt(tmp_path, "draw-array", bad, name="fx-0.json")
    write_receipt(tmp_path, "draw-array", receipt("draw-array"), name="fx-1.json")
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0, result["findings"]


def test_two_rows_sharing_a_capability_need_one_receipt(tmp_path, capsys):
    rows = [row("LEAFARRAY"), row("LEAFARRAYX")]
    ledger = write_ledger(tmp_path, rows, expected=2)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0, result["findings"]
    assert result["counts"]["capabilities_in_scope"] == 1
    assert result["counts"]["duty_rows_in_scope"] == 2
    assert result["counts"]["duty_rows_passing"] == 2


def test_a_shared_capability_is_reported_once(tmp_path, capsys):
    rows = [row("LEAFARRAY"), row("LEAFARRAYX")]
    ledger = write_ledger(tmp_path, rows, expected=2)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_MISSING"]


# ---------------------------------------------------------------------------
# wave scoping
# ---------------------------------------------------------------------------


def test_wave_scoping_excludes_later_waves(tmp_path, capsys):
    rows = [row("LEAFLATER", wave=2, capability="late-thing")]
    ledger = write_ledger(tmp_path, rows, expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--require", "w1")
    assert code == 0, result["findings"]
    assert result["counts"]["duty_rows_in_scope"] == 0


def test_wave_scoping_includes_the_named_wave(tmp_path, capsys):
    rows = [row("LEAFLATER", wave=2, capability="late-thing")]
    ledger = write_ledger(tmp_path, rows, expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--require", "w2")
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_MISSING"]
    assert result["counts"]["duty_rows_in_scope"] == 1


def test_ledger_checks_run_over_all_rows_in_every_scope(tmp_path, capsys):
    """A wave-3 row's unresolved status still fails --require w0."""
    rows = [row("LEAFLATER", wave=3, capability="late-thing", status="unresolved")]
    ledger = write_ledger(tmp_path, rows, expected=1)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--require", "w0")
    assert code == 1
    assert finding_codes(result) == ["UNRESOLVED"]


# ---------------------------------------------------------------------------
# fail-closed inputs, exit 2
# ---------------------------------------------------------------------------


def test_malformed_json_exits_2(tmp_path, capsys):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{ not json", encoding="utf-8")
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_missing_ledger_exits_2(tmp_path, capsys):
    assert run(tmp_path / "nope.json", receipts_dir(tmp_path)) == 2
    assert capsys.readouterr().err.strip()


def test_unknown_enum_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY", **{"class": "Z"})], expected=1)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "unknown value" in capsys.readouterr().err


def test_unknown_row_field_exits_2(tmp_path, capsys):
    bad = row("LEAFARRAY")
    bad["surprise"] = "yes"
    ledger = write_ledger(tmp_path, [bad], expected=1)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "unknown field" in capsys.readouterr().err


def test_wrong_schema_string_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [], expected=0, schema="leaf.solar-parity-ledger.v0")
    assert run(ledger, receipts_dir(tmp_path)) == 2


def test_out_of_range_wave_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY", wave=9)], expected=1)
    assert run(ledger, receipts_dir(tmp_path)) == 2


def test_oversized_ledger_exits_2(tmp_path, capsys, monkeypatch):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    monkeypatch.setattr(status, "MAX_LEDGER_BYTES", 10)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "too large" in capsys.readouterr().err


def test_too_many_rows_exits_2(tmp_path, capsys, monkeypatch):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY"), row("LEAFARRAYX")], expected=2)
    monkeypatch.setattr(status, "MAX_ROWS", 1)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "exceeds the limit" in capsys.readouterr().err


def test_malformed_receipt_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    directory = tmp_path / "receipts" / "draw-array"
    directory.mkdir(parents=True)
    (directory / "fx-1.json").write_text("{ broken", encoding="utf-8")
    assert run(ledger, receipts_dir(tmp_path)) == 2


def test_receipt_missing_field_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array")
    del doc["survived_reopen"]
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2


def test_receipt_filed_under_the_wrong_capability_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("other-thing"))
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "filed under" in capsys.readouterr().err


def test_oversized_receipt_exits_2(tmp_path, capsys, monkeypatch):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    monkeypatch.setattr(status, "MAX_RECEIPT_BYTES", 10)
    assert run(ledger, receipts_dir(tmp_path)) == 2


def test_unknown_require_value_is_a_usage_error(tmp_path):
    ledger = write_ledger(tmp_path, [], expected=0)
    with pytest.raises(SystemExit) as excinfo:
        status.main(["--ledger", str(ledger), "--require", "w9"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# output shape
# ---------------------------------------------------------------------------


def test_json_output_is_one_object_and_matches_the_human_verdict(tmp_path, capsys):
    rows = [row("LEAFARRAY"), row("LEAFLATER", wave=4, capability="late-thing")]
    ledger = write_ledger(tmp_path, rows, expected=2)
    receipts = receipts_dir(tmp_path)

    human_code = run(ledger, receipts, "--require", "all-production")
    human = capsys.readouterr().out
    json_code, result = json_result(capsys, ledger, receipts, "--require", "all-production")

    assert human_code == json_code == 1
    assert result["ok"] is False
    assert "verdict: FAIL" in human
    assert len(human.splitlines()) <= status.MAX_REPORT_LINES
    for item in result["findings"]:
        assert item["code"] in human


def test_human_report_is_truncated_with_a_more_line(tmp_path, capsys):
    rows = [row(f"LEAF{n:03d}", status="unresolved", capability=f"cap-{n}", wave=0) for n in range(300)]
    ledger = write_ledger(tmp_path, rows, expected=len(rows))
    code = run(ledger, receipts_dir(tmp_path))
    out = capsys.readouterr().out
    assert code == 1
    assert len(out.splitlines()) <= status.MAX_REPORT_LINES
    assert "more" in out


def test_findings_are_sorted_by_code_then_name(tmp_path, capsys):
    rows = [
        row("LEAFB", status="unresolved", capability="cap-b"),
        row("LEAFA", status="unresolved", capability="cap-a"),
    ]
    ledger = write_ledger(tmp_path, rows, expected=2)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 1
    pairs = [(item["code"], item.get("capability") or item.get("global") or "") for item in result["findings"]]
    assert pairs == sorted(pairs)


def test_json_mode_prints_nothing_else(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", receipt("draw-array"))
    code = run(ledger, receipts_dir(tmp_path), as_json=True)
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["ok"] is True
    assert captured.err == ""


@pytest.mark.parametrize("name", ["exact-counts-by-layer", "solar-w1-semantic"])
def test_pass_with_diffs_is_not_a_passing_receipt(tmp_path, capsys, name):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    doc = comparison_receipt() if name == "solar-w1-semantic" else receipt("draw-array")
    doc["comparator"]["diffs"] = ["membership differs"]
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "passing comparator must have no diffs" in capsys.readouterr().err


def comparison_receipt():
    # Share the adapter fixtures without relying on the script directory in sys.path.
    spec = importlib.util.spec_from_file_location(
        "comparison_test_fixtures", SCRIPT.with_name("test_solar_w1_compare.py")
    )
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    comparison = fixtures.document()
    comparison["capability"] = "draw-array"
    doc = receipt("draw-array", comparison=comparison)
    doc["comparator"] = fixtures.compare.compare_document(comparison)
    return doc


def test_executable_comparison_receipt_passes(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    write_receipt(tmp_path, "draw-array", comparison_receipt())
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0, result["findings"]


def test_forged_comparator_verdict_is_rejected(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    doc = comparison_receipt()
    doc["comparison"]["studio"]["revision"] = "2"
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "disagrees with executable comparison" in capsys.readouterr().err


def test_unknown_comparator_name_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    doc = receipt("draw-array")
    doc["comparator"]["name"] = "geometry"
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "unknown value 'geometry'" in capsys.readouterr().err


def test_solar_w1_semantic_without_comparison_evidence_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    doc = comparison_receipt()
    del doc["comparison"]
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "requires comparison evidence" in capsys.readouterr().err


def test_comparison_cannot_relabel_its_fixture(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")])
    doc = comparison_receipt()
    doc["fixture"]["sha256"] = "c" * 64
    write_receipt(tmp_path, "draw-array", doc)
    assert run(ledger, receipts_dir(tmp_path)) == 2
    assert "fixture disagrees" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# declared divergences: a known plugin defect, never reproduced to pass
# ---------------------------------------------------------------------------


def test_declared_divergence_settles_the_capability(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt())
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 0, result["findings"]
    assert result["ok"] is True


def test_divergence_counts_and_json_name_the_capability(tmp_path, capsys):
    rows = [row("LEAFARRAY"), row("LEAFARRAYX")]
    ledger = write_ledger(tmp_path, rows, expected=2)
    write_receipt(tmp_path, "draw-array", diverging_receipt())
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 0, result["findings"]
    assert result["counts"]["capabilities_diverged"] == 1
    assert result["counts"]["duty_rows_diverged"] == 2
    assert result["counts"]["capabilities_passing"] == 1
    assert result["counts"]["duty_rows_passing"] == 2
    assert result["divergences"] == [
        {"capability": "draw-array", "finding": DIVERGENCE_FINDING, "summary": DIVERGENCE_SUMMARY}
    ]


def test_divergence_is_named_in_the_human_report(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt())
    write_finding(tmp_path)
    code = run(ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    out = capsys.readouterr().out
    assert code == 0
    assert "declared divergences" in out
    assert DIVERGENCE_FINDING in out
    assert "draw-array" in out
    assert "verdict: PASS" in out
    assert len(out.splitlines()) <= status.MAX_REPORT_LINES


def test_an_undeclared_diff_invalidates_the_divergence(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt(diffs=DECLARED_DIFFS + ("group 7: count differs",)))
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "undeclared diff" in result["findings"][0]["detail"]


def test_a_declared_diff_that_is_not_produced_invalidates_the_divergence(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt(diffs=DECLARED_DIFFS[:1]))
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "not produced" in result["findings"][0]["detail"]


def test_a_missing_finding_file_invalidates_the_divergence(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt())
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "names no committed file" in result["findings"][0]["detail"]


def test_a_finding_outside_the_divergences_directory_is_invalid(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    reference = "docs/parity/notes/autofillrevert-handle-drift.md"
    write_receipt(tmp_path, "draw-array", diverging_receipt(block=divergence(finding=reference)))
    write_finding(tmp_path, reference)  # the file exists; the directory is the refusal
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "is not under docs/parity/divergences/" in result["findings"][0]["detail"]


def test_a_divergence_on_a_passing_receipt_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = receipt("draw-array")
    doc["divergence"] = divergence()
    write_receipt(tmp_path, "draw-array", doc)
    write_finding(tmp_path)
    assert run(ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path)) == 2
    assert "requires comparator verdict 'fail'" in capsys.readouterr().err


def test_a_stale_divergence_receipt_is_invalid(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY", capability_version="2")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt())
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "RECEIPT_STALE" in result["findings"][0]["detail"]


def test_an_uncommitted_divergence_receipt_is_invalid(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    doc = diverging_receipt()
    doc["plugin"] = dict(doc["plugin"], state="dirty")
    write_receipt(tmp_path, "draw-array", doc)
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "RECEIPT_NOT_COMMITTED" in result["findings"][0]["detail"]


def test_a_divergence_receipt_that_did_not_survive_reopen_is_invalid(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt(survived_reopen=False))
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 1
    assert finding_codes(result) == ["RECEIPT_DIVERGENCE_INVALID"]
    assert "RECEIPT_NO_REOPEN" in result["findings"][0]["detail"]


def test_a_clean_pass_wins_over_a_declared_divergence(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt(), name="fx-0.json")
    write_receipt(tmp_path, "draw-array", receipt("draw-array"), name="fx-1.json")
    write_finding(tmp_path)
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path))
    assert code == 0, result["findings"]
    assert result["divergences"] == []
    assert result["counts"]["capabilities_diverged"] == 0
    assert result["counts"]["capabilities_passing"] == 1


def test_an_unknown_divergence_field_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    block = divergence()
    block["surprise"] = "yes"
    write_receipt(tmp_path, "draw-array", diverging_receipt(block=block))
    assert run(ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path)) == 2
    assert "unknown field" in capsys.readouterr().err


def test_an_empty_declared_diffs_list_exits_2(tmp_path, capsys):
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    write_receipt(tmp_path, "draw-array", diverging_receipt(block=divergence(diffs=())))
    assert run(ledger, receipts_dir(tmp_path), "--repo-root", str(tmp_path)) == 2
    assert "must name at least one diff" in capsys.readouterr().err


def test_the_divergences_readme_is_committed():
    text = (REPO_ROOT / "docs" / "parity" / "divergences" / "README.md").read_text(encoding="utf-8")
    assert "never a licence for Studio to be wrong" in text
    for phrase in ("declared_diffs", "RECEIPT_DIVERGENCE_INVALID", "committed"):
        assert phrase in text


def test_the_default_repo_root_is_this_checkout(tmp_path, capsys):
    """With no --repo-root, a finding resolves against the repo the gate runs from."""
    ledger = write_ledger(tmp_path, [row("LEAFARRAY")], expected=1)
    block = divergence(finding="docs/parity/divergences/README.md")
    write_receipt(tmp_path, "draw-array", diverging_receipt(block=block))
    code, result = json_result(capsys, ledger, receipts_dir(tmp_path))
    assert code == 0, result["findings"]
    assert result["counts"]["capabilities_diverged"] == 1
