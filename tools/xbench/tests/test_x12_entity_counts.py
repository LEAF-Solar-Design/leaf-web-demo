import json
from pathlib import Path

import pytest

from tools.xbench.x12_entity_counts import compare_entity_counts
from tools.xbench.x12_layers import compare_layers


def row(count, layer="Roof", kind="LINE"):
    return {"type": kind, "layer": layer, "count": count}


def compare(before, after):
    return compare_entity_counts(
        before, after, original_layers=["Roof", "Empty"],
        studio_layers=["Studio-Panels", "Empty"],
    )


def test_additions_on_named_new_studio_layers_are_allowed():
    assert compare([row(2)], [row(2), row(3, "Studio-Panels", "INSERT")]) == []


@pytest.mark.parametrize("after", [
    [], [row(3)], [row(2), row(1, "Empty")], [row(2), row(1, "Studio-Other")],
    [row(1), row(1, "Studio-Panels")], [row(2), row(1, kind="INSERT")],
])
def test_missing_or_unpermitted_counts(after):
    issue, = compare([row(2)], after)
    assert issue.startswith("entity_counts[") and "].count: expected" in issue


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_invalid_count(bad):
    with pytest.raises(ValueError, match=r"after.entity_counts\[0\].count"):
        compare([], [row(bad)])


def test_duplicate_counts_are_refused():
    with pytest.raises(ValueError, match="duplicate"):
        compare([], [row(1), row(1, "ROOF")])


def test_malformed_evidence_repro():
    path = Path(__file__).resolve().parents[1] / "fixtures/x12/malformed-evidence.json"
    examples = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match=r"after.layers\[0\].plot: missing field"):
        compare_layers([], examples["missing_layer_field"])
    with pytest.raises(ValueError, match=r"after.layers\[1\].name: duplicate"):
        compare_layers([], examples["duplicate_layers"])
    with pytest.raises(ValueError, match="nonnegative integer"):
        compare([], examples["negative_count"])
    with pytest.raises(json.JSONDecodeError):
        json.loads(examples["truncated_json"])
