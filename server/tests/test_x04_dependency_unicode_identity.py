"""Literal dependency identity is not case, slash or Unicode normalization."""
import builtins
import io
import json
import os
import unicodedata
from drawing_dependency_report import from_pairs


def test_distinct_unicode_case_and_slash_references_remain_literal(monkeypatch):
    names = ["café.shx", unicodedata.normalize("NFD", "café.shx"),
             "Panel.shx", "panel.shx", "fonts/a.shx", "fonts\\a.shx"]
    pairs = [(code, value) for name in names for code,value in [(0, "STYLE"), (3, name)]]
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Dependency discovery must not open targets")
    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", forbidden)
    report = from_pairs(pairs)
    restored = json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False))
    assert restored == report
    assert [row["name"] for row in restored["dependencies"]] == sorted(names)
    assert len(restored["dependencies"]) == len(set(names)) == 6
    assert all(row["disposition"] == "font remapped" for row in restored["dependencies"])
    assert attempts == []
