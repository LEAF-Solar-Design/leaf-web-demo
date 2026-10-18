"""Scope receipts over real ASCII DXF fixtures, mutated only in memory."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = PROJECT_ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import corpus_harness as harness  # noqa: E402
import export_fidelity as fidelity  # noqa: E402
import dxf_intake  # noqa: E402
from mutation_scope_diff import MutationScope, compare_scoped, main  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def drawings():
    paths = [*harness.discover_corpus(), PROJECT_ROOT / "web/public/sample.dxf"]
    assert len(paths) == 5
    originals = {path: path.read_bytes() for path in paths}
    yield originals
    for path, original in originals.items():
        assert path.read_bytes() == original, f"fixture changed: {path}"


@pytest.fixture
def entity_case(drawings):
    path = ENGINE_DIR / "corpus/01_closed_lwpolyline_single_layer.dxf"
    before = drawings[path]
    text = before.decode("ascii")
    block = re.search(r"(?m)^0\r?\nLWPOLYLINE\r?\n.*?(?=^0\r?\n)", text, re.DOTALL)
    assert block is not None
    handle = re.search(r"(?m)^5\r?\n([^\r\n]+)", block.group())
    coordinate = re.search(r"(?m)^10\r?\n([^\r\n]+)", block.group())
    assert handle is not None and coordinate is not None
    ledger = fidelity._ledger(dxf_intake.parse_dxf_bytes(before, source_name=path.name))
    old_handle = handle.group(1).upper()
    assert old_handle in ledger["keyed"]
    new_handle = format(max(int(h, 16) for h in ledger["keyed"]) + 1, "X")
    return {
        "before": before, "text": text, "block": block.group(),
        "handle": old_handle, "new_handle": new_handle,
        "coordinate": coordinate.group(1), "name": path.name,
    }


def _replace_block(case, block):
    assert case["text"].count(case["block"]) == 1
    return case["text"].replace(case["block"], block, 1).encode("ascii")


def _move(case, amount=1.0):
    block = re.sub(
        r"(?m)^(10\r?\n)[^\r\n]+",
        lambda match: match.group(1) + repr(float(case["coordinate"]) + amount),
        case["block"], count=1,
    )
    result = _replace_block(case, block)
    assert result != case["before"]
    return result


def _add(case):
    block = re.sub(
        r"(?m)^(5\r?\n)[^\r\n]+",
        lambda match: match.group(1) + case["new_handle"], case["block"], count=1,
    )
    block = re.sub(
        r"(?m)^(8\r?\n)[^\r\n]+",
        lambda match: match.group(1) + "LEAF_CANARY", block, count=1,
    )
    return _replace_block(case, case["block"] + block)


def _compare(case, after, **scope):
    return compare_scoped(
        case["before"], after, MutationScope.from_iterables(**scope), source_name=case["name"],
    )


def test_identity_is_ok_for_every_tracked_drawing(drawings):
    for path, before in drawings.items():
        ledger = fidelity._ledger(dxf_intake.parse_dxf_bytes(before, source_name=path.name))
        result = compare_scoped(before, before, MutationScope.from_iterables(), source_name=path.name)
        assert result == {
            "ok": True, "source_name": path.name, "untouched_total": len(ledger["keyed"]),
            "untouched_changed": [], "unexpected_removed": [], "unexpected_added": [],
            "expected_removed_missing": [], "touched_changed": [], "handleless_changed": False,
            "blocks_changed": [],
            "kind_counts_before": ledger["counts"], "kind_counts_after": ledger["counts"],
        }


def test_block_fixture_identity_is_ok():
    path = PROJECT_ROOT / "web/e2e/fixtures/block-fixture.dxf"
    before = path.read_bytes()
    result = compare_scoped(before, before, MutationScope.from_iterables(), source_name=path.name)
    assert result["ok"] is True
    assert result["blocks_changed"] == []


def test_block_definition_change_is_a_defect():
    path = PROJECT_ROOT / "web/e2e/fixtures/block-fixture.dxf"
    before = path.read_bytes()
    text = before.decode("ascii")
    blocks, entities = text.split("ENTITIES", 1)
    name = re.search(r"(?m)^0\r?\nBLOCK\r?\n.*?^2\r?\n([^\r\n]+)", blocks, re.DOTALL)
    endpoint = re.search(r"(?m)^11\r?\n([^\r\n]+)", blocks)
    assert name is not None and endpoint is not None
    replacement = endpoint.group().replace(endpoint.group(1), str(float(endpoint.group(1)) + 1))
    changed_blocks = blocks.replace(endpoint.group(), replacement, 1)
    after = (changed_blocks + "ENTITIES" + entities).encode("ascii")
    assert after != before
    result = compare_scoped(before, after, MutationScope.from_iterables(), source_name=path.name)
    assert result["ok"] is False
    assert result["blocks_changed"] == [name.group(1)]
    assert result["untouched_changed"] == []


def test_parse_errors_raise_value_error(tmp_path, capsys):
    path = PROJECT_ROOT / "web/e2e/fixtures/block-fixture.dxf"
    valid = path.read_bytes()
    blocks, entities = valid.decode("ascii").split("ENTITIES", 1)
    name = re.search(r"(?m)^2\r?\n([^\r\n]+)", entities)
    assert name is not None
    missing_name = name.group(1) + "_MISSING"
    malformed = (blocks + "ENTITIES" + entities.replace(name.group(), name.group().replace(
        name.group(1), missing_name), 1)).encode("ascii")
    assert malformed != valid
    errors = dxf_intake.parse_dxf_bytes(malformed, source_name=path.name)["parseErrors"]
    assert missing_name in errors[0]
    before_path, after_path = tmp_path / "before.dxf", tmp_path / "after.dxf"
    for side, before, after in (
        ("before", malformed, valid), ("after", valid, malformed),
        ("before", malformed, malformed),
    ):
        with pytest.raises(ValueError) as exc:
            compare_scoped(before, after, MutationScope.from_iterables(), source_name=path.name)
        assert str(exc.value) == f"{side}: {errors[0]}"
        before_path.write_bytes(before)
        after_path.write_bytes(after)
        assert main(["--before", str(before_path), "--after", str(after_path)]) == 2
        captured = capsys.readouterr()
        assert captured.err.strip() == f"{side}: {errors[0]}"
        assert not captured.out


def test_touched_vertex_move_is_ok_and_reported_informationally(entity_case):
    result = _compare(entity_case, _move(entity_case), touched=(entity_case["handle"],))
    assert result["ok"] is True
    assert result["touched_changed"] == [entity_case["handle"]]
    assert result["untouched_total"] == 0


def test_untouched_vertex_move_fails_and_names_the_handle(entity_case):
    result = _compare(entity_case, _move(entity_case))
    assert result["ok"] is False
    assert result["untouched_changed"] == [entity_case["handle"]]
    assert result["untouched_total"] == 1
    # A missing handle cannot silently exempt an entity from the oracle.
    handleless_before = re.sub(rb"(?m)^5\r?\n[^\r\n]+\r?\n", b"", entity_case["before"], count=1)
    handleless_after = re.sub(rb"(?m)^5\r?\n[^\r\n]+\r?\n", b"", _move(entity_case), count=1)
    control = compare_scoped(
        handleless_before, handleless_before, MutationScope.from_iterables(), source_name="unkeyed.dxf",
    )
    assert control["ok"] is True
    changed = compare_scoped(
        handleless_before, handleless_after, MutationScope.from_iterables(), source_name="unkeyed.dxf",
    )
    assert changed["ok"] is False
    assert changed["handleless_changed"] is True


def test_undeclared_removal_is_unexpected_removed(entity_case):
    result = _compare(entity_case, _replace_block(entity_case, ""))
    assert result["ok"] is False
    assert result["unexpected_removed"] == [entity_case["handle"]]


def test_declared_removal_is_ok(entity_case):
    result = _compare(entity_case, _replace_block(entity_case, ""), removed=(entity_case["handle"],))
    assert result["ok"] is True
    assert result["unexpected_removed"] == []
    assert result["untouched_total"] == 0


def test_declared_removal_still_present_is_reported(entity_case):
    result = _compare(entity_case, entity_case["before"], removed=(entity_case["handle"],))
    assert result["ok"] is False
    assert result["expected_removed_missing"] == [entity_case["handle"]]


def test_added_entity_on_declared_layer_is_ok(entity_case):
    result = _compare(entity_case, _add(entity_case), added_layers=("LEAF_CANARY",))
    assert result["ok"] is True
    assert result["unexpected_added"] == []
    assert result["kind_counts_after"]["polylines"] == 2


def test_added_entity_on_undeclared_layer_is_unexpected_added(entity_case):
    result = _compare(entity_case, _add(entity_case))
    assert result["ok"] is False
    assert result["unexpected_added"] == [entity_case["new_handle"]]


def test_handle_renumber_reads_as_remove_plus_add(entity_case):
    block = re.sub(
        r"(?m)^(5\r?\n)[^\r\n]+",
        lambda match: match.group(1) + entity_case["new_handle"], entity_case["block"], count=1,
    )
    result = _compare(entity_case, _replace_block(entity_case, block))
    assert result["ok"] is False
    assert result["unexpected_removed"] == [entity_case["handle"]]
    assert result["unexpected_added"] == [entity_case["new_handle"]]


def test_tolerance_drift_below_1e6_is_ok(entity_case):
    after = _move(entity_case, 1e-9)
    result = _compare(entity_case, after)
    assert result["ok"] is True
    assert result["untouched_changed"] == []
    before_ledger = fidelity._ledger(dxf_intake.parse_dxf_bytes(entity_case["before"]))
    after_ledger = fidelity._ledger(dxf_intake.parse_dxf_bytes(after))
    assert before_ledger["keyed"] != after_ledger["keyed"]


def test_case_insensitive_handles(entity_case):
    lower = entity_case["handle"].lower()
    assert lower != entity_case["handle"]
    scope = MutationScope.from_iterables(touched=(lower,), removed=(lower,))
    assert scope.touched_handles == scope.removed_handles == frozenset((entity_case["handle"],))
    result = _compare(entity_case, _move(entity_case), touched=(lower,))
    assert result["ok"] is True
    assert result["touched_changed"] == [entity_case["handle"]]


def test_malformed_input_raises_value_error(entity_case):
    valid = entity_case["before"]
    duplicate = _replace_block(entity_case, entity_case["block"] * 2)
    for malformed in (b"garbage", b"AutoCAD Binary DXF\r\n\x1a\x00", duplicate):
        for before, after in ((malformed, valid), (valid, malformed)):
            with pytest.raises(ValueError):
                compare_scoped(before, after, MutationScope.from_iterables(), source_name="bad.dxf")


def test_cli_exit_codes(entity_case, tmp_path, capsys):
    before = tmp_path / "before.dxf"
    after = tmp_path / "after.dxf"
    before.write_bytes(entity_case["before"])
    after.write_bytes(entity_case["before"])
    args = ["--before", str(before), "--after", str(after)]
    assert main(args) == 0
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["ok"] is True
    assert receipt["source_name"] == before.name
    assert captured.out.strip() == json.dumps(receipt, sort_keys=True)
    after.write_bytes(_move(entity_case))
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["untouched_changed"] == [entity_case["handle"]]
    assert main([*args, "--touched", entity_case["handle"].lower(), "--source-name", "named.dxf"]) == 0
    assert json.loads(capsys.readouterr().out)["source_name"] == "named.dxf"
    after.write_bytes(_replace_block(entity_case, ""))
    assert main([*args, "--removed", entity_case["handle"].lower()]) == 0
    capsys.readouterr()
    after.write_bytes(_add(entity_case))
    assert main([*args, "--added-layer", "OTHER", "--added-layer", "LEAF_CANARY"]) == 0
    capsys.readouterr()
    after.write_bytes(b"garbage")
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.err and not captured.out
    assert main(["--before", str(tmp_path / "missing.dxf"), "--after", str(after)]) == 2
    captured = capsys.readouterr()
    assert captured.err and not captured.out
    # Exercise the actual standalone script and its import path too.
    fixture = ENGINE_DIR / "corpus/01_closed_lwpolyline_single_layer.dxf"
    run = subprocess.run(
        [sys.executable, str(ENGINE_DIR / "mutation_scope_diff.py"),
         "--before", str(fixture), "--after", str(fixture)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["ok"] is True
