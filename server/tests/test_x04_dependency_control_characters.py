"""Decoded intake names retain control characters as distinct JSON strings."""
import builtins
from copy import deepcopy
import io
import json
import os
from drawing_dependency_report import from_pairs


def test_control_characters_survive_decoded_intake_and_strict_json_without_io(monkeypatch):
    # CR/LF are already record delimiters in textual DXF; test the decoded-pair
    # intake seam rather than fabricate a multiline raw DXF value.
    names = ["font.shx", "font\0.shx", "font\r.shx", "font\n.shx", "font\t.shx"]
    pairs = [(code, value) for name in names for code, value in [(0, "STYLE"), (3, name)]]
    original = deepcopy(pairs)
    accesses = []
    def forbidden(*args, **kwargs):
        accesses.append(args)
        raise AssertionError("Dependency discovery must not open files")
    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", forbidden)
    first = from_pairs(pairs)
    replayed = from_pairs(json.loads(json.dumps(pairs, allow_nan=False)))
    assert first == replayed == json.loads(json.dumps(first, allow_nan=False))
    assert {row["name"] for row in first["dependencies"]} == set(names)
    assert len(first["dependencies"]) == len(names)
    assert all(row["disposition"] == "font remapped" for row in first["dependencies"])
    assert pairs == original and accesses == []
