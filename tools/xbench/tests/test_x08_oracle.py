"""Synthetic evidence for the independent native geometry oracle."""

from copy import deepcopy

import pytest

from benches.x08_oracle import (
    choose_window,
    contained_handles,
    modules_from_native_dump,
)


def entity(handle="A", layer="modules"):
    return {"handle": handle, "type": "BlockReference", "layer": layer,
            "bbox": {"min": [0, 0, 0], "max": [2, 3, 0]}}


def dump(rows, count=None):
    return {"ok": True, "result": {"entities": rows,
                                    "count": len(rows) if count is None else count}}


def module(handle, bounds):
    return {"handle": str(handle), "bounds": bounds}


def test_native_exact_layer_and_independent_bounds():
    payload = dump([entity(), entity("B", "Modules"), entity("C", "other")], 3)
    payload["result"]["entities"][2]["type"] = "Line"
    before = deepcopy(payload)
    result = modules_from_native_dump(payload, "modules", 1)
    assert result == [{"handle": "A", "bounds": [0, 0, 2, 3]}]
    result[0]["bounds"][0] = -1
    assert payload == before


def test_native_total_count_includes_other_layers():
    payload = dump([entity(), entity("B"), entity("C", "other")], 3)
    payload["result"]["entities"][2]["type"] = "Line"
    assert modules_from_native_dump(payload, "modules", 2) == [
        {"handle": "A", "bounds": [0, 0, 2, 3]},
        {"handle": "B", "bounds": [0, 0, 2, 3]},
    ]


def test_native_declared_count_cannot_only_count_selected_modules():
    payload = dump([entity(), entity("B"), entity("C", "other")], 2)
    with pytest.raises(ValueError):
        modules_from_native_dump(payload, "modules", 2)


@pytest.mark.parametrize("count", [-1, 0, 2, True, 1.0, None])
def test_declared_count_mismatch(count):
    payload = dump([entity()])
    payload["result"]["count"] = count
    with pytest.raises(ValueError):
        modules_from_native_dump(payload, "modules", 1)


def test_selected_count_mismatch():
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([entity(layer="other")], 1), "modules", 1)


@pytest.mark.parametrize("field,value", [
    ("handle", ""), ("handle", "  "), ("handle", 1),
    ("type", "Line"), ("type", None), ("bbox", None),
])
def test_invalid_selected_entity(field, value):
    row = entity()
    row[field] = value
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([row]), "modules", 1)


@pytest.mark.parametrize("field", ["handle", "type", "bbox", "layer"])
def test_missing_selected_fields(field):
    row = entity()
    del row[field]
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([row]), "modules", 1)


@pytest.mark.parametrize("side,axis,value", [
    ("min", 0, float("nan")), ("max", 1, float("inf")),
    ("min", 2, float("-inf")), ("max", 2, True),
    ("min", 0, False), ("max", 0, "2"),
    ("min", 0, 3), ("min", 1, 3), ("min", 2, 1),
    ("max", 0, 0),
])
def test_invalid_native_coordinates(side, axis, value):
    row = entity()
    row["bbox"][side][axis] = value
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([row]), "modules", 1)


@pytest.mark.parametrize("bbox", [{}, {"min": [0, 0], "max": [1, 1, 0]},
                                  {"min": None, "max": [1, 1, 0]}])
def test_missing_or_short_native_vectors(bbox):
    row = entity()
    row["bbox"] = bbox
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([row]), "modules", 1)


def test_duplicate_native_handles_rejected():
    with pytest.raises(ValueError):
        modules_from_native_dump(dump([entity(), entity()]), "modules", 2)


@pytest.mark.parametrize("payload", [None, {}, {"ok": False},
                                      {"ok": True, "result": {}},
                                      {"ok": True, "result": {"count": 0, "entities": None}}])
def test_invalid_dump_envelope(payload):
    with pytest.raises(ValueError):
        modules_from_native_dump(payload, "modules", 0)


def test_containment_uses_whole_bounds_and_includes_edges():
    rows = [module("Z", [0, 0, 4, 4]), module("A", [1, 1, 2, 2]),
            module("center_only", [-1, 1, 3, 3]),
            module("over_top", [1, 1, 2, 5])]
    assert contained_handles(rows, [0, 0, 4, 4]) == ["A", "Z"]


@pytest.mark.parametrize("rect", [[0, 0, 1], [2, 0, 1, 2], [0, 2, 1, 1],
                                   [0, 0, True, 2], [0, 0, float("nan"), 2]])
def test_invalid_rect(rect):
    with pytest.raises(ValueError):
        contained_handles([], rect)


def assert_exact(rows, result, target=500, tolerance=10):
    assert target - tolerance <= len(result["expected_handles"]) <= target + tolerance
    assert result["expected_handles"] == contained_handles(rows, result["rect"])


def test_large_regular_layout_deterministic_and_unmodified():
    rows = [module(f"m{i:04d}", [(i % 40) * 3, (i // 40) * 4,
                               (i % 40) * 3 + 2, (i // 40) * 4 + 3])
            for i in range(1200)]
    before = deepcopy(rows)
    result = choose_window(rows)
    assert_exact(rows, result)
    assert choose_window(list(reversed(rows))) == result
    assert rows == before


def test_large_irregular_layout_exact_set():
    rows = [module(i, [i * 2, (i * 17) % 73, i * 2 + 1 + (i % 3) / 10,
                       (i * 17) % 73 + 0.5]) for i in range(1301)]
    result = choose_window(rows, tolerance=0)
    assert_exact(rows, result, tolerance=0)
    assert choose_window(list(reversed(rows)), tolerance=0) == result


def test_y_band_can_resolve_tied_x_edges():
    rows = [module(i, [0, i * 2, 1, i * 2 + 1]) for i in range(1200)]
    assert_exact(rows, choose_window(rows))


def test_transposed_search_finds_exact_window():
    # Y bands are sampled; no sampled prefix contains exactly 500 rows.
    # Transposing makes every Y-max edge available as a right cutoff.
    rows = [module(i, [0, i * 2, 1, i * 2 + 1]) for i in range(1200)]
    result = choose_window(rows, tolerance=0)
    assert_exact(rows, result, tolerance=0)


def test_tied_edges_count_every_module_instead_of_truncating():
    rows = [module(i, [i // 12, 0, i // 12 + 0.5, 1]) for i in range(1200)]
    result = choose_window(rows)
    assert len(result["expected_handles"]) == 504
    assert_exact(rows, result)


def test_identical_overlaps_cannot_select_an_invented_subset():
    rows = [module(i, [0, 0, 1, 1]) for i in range(1000)]
    before = deepcopy(rows)
    with pytest.raises(ValueError):
        choose_window(rows)
    assert rows == before


@pytest.mark.parametrize("rows,target,tolerance", [
    ([], 500, 10), ([module("A", [0, 0, 1, 1])], 500, 10),
    ([], True, 0), ([], 0, 0), ([], 1, -1), ([], 1, True),
])
def test_impossible_or_invalid_search(rows, target, tolerance):
    with pytest.raises(ValueError):
        choose_window(rows, target, tolerance)
