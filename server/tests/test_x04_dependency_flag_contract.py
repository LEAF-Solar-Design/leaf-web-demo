"""Decoded dependency flags and duplicate codes use the first record value."""
import builtins
import io
import os
from copy import deepcopy
import pytest
from drawing_dependency_report import from_pairs


@pytest.mark.parametrize("flags", [0, 1, 2, 4, 8, 12, 16, 20, 64])
def test_block_and_style_bits_preserve_first_values_without_io(flags, monkeypatch):
    pairs = [(0, "BLOCK"), (70, str(flags)), (70, "0"), (1, "first.dwg"), (1, "second.dwg"),
             (0, "STYLE"), (70, str(flags)), (70, "0"), (3, "first.shx"), (3, "second.shx"),
             (4, "big.shx"), (4, "other.shx")]
    original = deepcopy(pairs)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Dependency discovery must not open external files")
    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", forbidden)
    report = from_pairs(pairs)
    assert report == from_pairs(pairs)
    rows = {(row["kind"], row["name"], row["disposition"]) for row in report["dependencies"]}
    expected = {("shape_font" if flags & 1 else "font", "first.shx", "font remapped"),
                ("shape_font", "big.shx", "font remapped")}
    if flags & 12:
        expected.add(("xref", "first.dwg", "detached"))
    assert rows == expected and pairs == original and calls == []
