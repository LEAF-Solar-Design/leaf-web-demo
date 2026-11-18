"""Saved X04 intake replays through the trusted tool without reopening source."""
import builtins
from copy import deepcopy
import io
import json
import os
from pathlib import Path

from dxf_intake import parse_dxf_bytes
from test_x04_minsert_arrays import selected


def test_saved_array_and_dependency_intake_replays_without_source(tmp_path, monkeypatch):
    raw = ("0\nSECTION\n2\nTABLES\n0\nSTYLE\n2\nNotes\n3\nnotes.shx\n0\nENDSEC\n"
           "0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n70\n0\n10\n0\n20\n0\n"
           "0\nLINE\n10\n0\n20\n0\n11\n1\n21\n0\n0\nENDBLK\n0\nENDSEC\n"
           "0\nSECTION\n2\nENTITIES\n0\nINSERT\n5\nA\n8\nEast\n2\nPanel\n10\n0\n20\n0\n70\n3\n71\n2\n"
           "0\nINSERT\n5\nB\n8\nWest\n2\nPanel\n10\n10\n20\n0\n0\nENDSEC\n0\nEOF\n").encode()
    source = tmp_path / "synthetic-source.dxf"
    source.write_bytes(raw)
    intake = parse_dxf_bytes(source.read_bytes(), source_name=str(source))
    original = deepcopy(intake)
    before = selected(intake)
    assert before["ok"] and before["result"]["count"] == 7
    assert before["overlay"]["highlight_handles"] == ["A", "B"]
    assert intake["dependency_report"]["dependencies"] == [
        {"kind": "font", "name": "notes.shx", "disposition": "font remapped", "remapped_to": "sans-serif"}]
    saved = tmp_path / "intake.json"
    saved.write_text(json.dumps(intake, allow_nan=False), encoding="utf-8")
    source.unlink()
    attempted = []

    def guarded(opener):
        def open_file(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve() == source.resolve():
                attempted.append(str(file))
                raise AssertionError("Replay tried to reopen the original source")
            return opener(file, *args, **kwargs)
        return open_file

    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", guarded(module.open))
    restored = json.loads(saved.read_text(encoding="utf-8"))
    after = selected(restored)
    assert after["ok"] and after["result"] == before["result"]
    assert after["overlay"] == before["overlay"]
    assert restored["dependency_report"] == intake["dependency_report"]
    assert restored == original == intake
    assert attempted == []
