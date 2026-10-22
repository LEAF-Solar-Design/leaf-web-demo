"""Structural graph diff contracts independent of persistence."""
import copy

from lab_diff import structural_diff


def test_equal_objects_and_binary64_numbers_have_no_diff():
    assert structural_diff({"b": 1.0, "a": -0.0}, {"a": 0, "b": 1}) == []


def test_add_remove_replace_and_pointer_escaping():
    assert structural_diff({"a/b": {"~value": 1}, "old": True},
                           {"a/b": {"~value": 2}, "new": None}) == [
        {"op": "replace", "path": "/a~1b/~0value", "before": 1, "after": 2},
        {"op": "add", "path": "/new", "after": None},
        {"op": "remove", "path": "/old", "before": True},
    ]


def test_array_order_is_significant():
    assert structural_diff(["a", "b"], ["b", "a"]) == [
        {"op": "replace", "path": "/0", "before": "a", "after": "b"},
        {"op": "replace", "path": "/1", "before": "b", "after": "a"},
    ]


def test_array_length_change_replaces_array():
    assert structural_diff({"pipes": []}, {"pipes": [1]}) == [
        {"op": "replace", "path": "/pipes", "before": [], "after": [1]},
    ]


def test_root_type_change_is_explicit():
    assert structural_diff(True, 1) == [
        {"op": "replace", "path": "", "before": True, "after": 1},
    ]


def test_diff_does_not_mutate_inputs():
    before = {"settings": {"risk": [1, 2]}}
    after = {"settings": {"risk": [1, 3]}}
    originals = copy.deepcopy((before, after))
    assert structural_diff(before, after)[0]["path"] == "/settings/risk/1"
    assert (before, after) == originals


def test_diff_key_order_matches_canonical_utf16():
    assert [change["path"] for change in structural_diff({}, {"\ue000": 1, "\U00010000": 2})] == [
        "/\U00010000", "/\ue000",
    ]
