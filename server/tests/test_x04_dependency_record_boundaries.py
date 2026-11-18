"""Truncated records remain local to their own DXF group boundaries."""
import pytest

from drawing_dependency_report import from_pairs


@pytest.mark.parametrize("pairs, expected", [
    ([(0, "STYLE")], []),
    ([(0, "BLOCK"), (70, "4")], []),
    ([(0, "IMAGEDEF")], []),
    ([(0, "STYLE"), (3, "notes.shx")], [("font", "notes.shx", "font remapped")]),
    ([(0, "BLOCK"), (2, "External"), (70, "4")], [("xref", "External", "detached")]),
    ([(0, "IMAGEDEF"), (5, "A1")], [("image", "A1", "image omitted")]),
])
def test_truncated_and_unnamed_records_are_deterministic(pairs, expected):
    report = from_pairs(pairs)
    assert report == from_pairs(list(pairs))
    assert [(row["kind"], row["name"], row["disposition"])
            for row in report["dependencies"]] == expected


def test_adjacent_records_never_supply_each_others_missing_values():
    pairs = [(0, "STYLE"), (2, "NoFont"),
             (0, "IMAGEDEF"), (1, "image.png"),
             (0, "BLOCK"), (70, "4"),
             (0, "STYLE"), (3, "font.shx"),
             (0, "IMAGEDEF"),
             (0, "PDFDEFINITION"), (1, "underlay.pdf")]
    report = from_pairs(pairs)
    assert report == from_pairs(pairs)
    assert {(row["kind"], row["name"]) for row in report["dependencies"]} == {
        ("image", "image.png"), ("font", "font.shx"), ("underlay", "underlay.pdf")}
