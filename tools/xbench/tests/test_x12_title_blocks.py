from copy import deepcopy

import pytest

from tools.xbench.x12_title_blocks import compare_title_blocks


@pytest.fixture
def snapshot():
    return {
        "definitions": [{"name": "Title", "geometry": [{"type": "LINE", "end": [9, 0]}]}],
        "inserts": [{"id": "42", "block": "Title", "attributes": [
            {"tag": "PROJECT", "value": "PV"}, {"tag": "SHEET", "value": "1"},
        ]}],
    }


def test_unchanged_snapshot(snapshot):
    assert compare_title_blocks(snapshot, deepcopy(snapshot)) == []


def test_geometry_change(snapshot):
    after = deepcopy(snapshot)
    after["definitions"][0]["geometry"][0]["end"] = [8, 0]
    issue, = compare_title_blocks(snapshot, after)
    assert "definitions['Title'].geometry" in issue


def test_missing_insert(snapshot):
    after = deepcopy(snapshot)
    after["inserts"].clear()
    assert compare_title_blocks(snapshot, after) == ["title_blocks.inserts['42']: missing"]


def test_duplicate_tags(snapshot):
    after = deepcopy(snapshot)
    after["inserts"][0]["attributes"].append({"tag": "Project", "value": "Other"})
    with pytest.raises(ValueError, match=r"attributes\[2\].tag: duplicate"):
        compare_title_blocks(snapshot, after)


@pytest.mark.parametrize("reorder", [False, True])
def test_changed_attribute_values_or_order(snapshot, reorder):
    after = deepcopy(snapshot)
    attributes = after["inserts"][0]["attributes"]
    if reorder:
        attributes.reverse()
    else:
        attributes[0]["value"] = "Different"
    issue, = compare_title_blocks(snapshot, after)
    assert "inserts['42'].attributes" in issue
