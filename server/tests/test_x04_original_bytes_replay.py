"""X04 synthetic pipeline preserves original bytes at every read operation."""
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
from dxf_intake import parse_dxf_bytes
from drawing_dependency_report import from_pairs
import tool_loader
from test_x04_minsert_arrays import TOOL, selected

RAW = ("0\nSECTION\n2\nTABLES\n0\nSTYLE\n2\nNotes\n3\nnotes.shx\n0\nENDSEC\n"
       "0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n70\n0\n10\n0\n20\n0\n"
       "0\nLINE\n10\n0\n20\n0\n11\n1\n21\n0\n0\nENDBLK\n0\nENDSEC\n"
       "0\nSECTION\n2\nENTITIES\n0\nINSERT\n5\nA\n8\nEast\n2\nPanel\n10\n0\n20\n0\n70\n3\n71\n2\n"
       "0\nINSERT\n5\nB\n8\nWest\n2\nPanel\n10\n10\n20\n0\n0\nENDSEC\n0\nEOF\n").encode()


def guard_source(monkeypatch, source):
    attempts = []
    def guard(opener):
        def open_file(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve() == source.resolve():
                attempts.append(str(file))
                raise AssertionError("Original source reopened")
            return opener(file, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", guard(module.open))
    return attempts


def test_bytes_and_sha_survive_each_pipeline_operation(tmp_path, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = guard_source(monkeypatch, tmp_path / "original.dxf")
    raw = bytearray(RAW)
    digest = hashlib.sha256(raw).hexdigest()
    checks = []
    def unchanged(stage):
        assert bytes(raw) == RAW and hashlib.sha256(raw).hexdigest() == digest
        checks.append(stage)
    intake = parse_dxf_bytes(raw, source_name=str(tmp_path / "original.dxf"))
    unchanged("intake")
    lines = raw.decode().splitlines()
    report = from_pairs([(int(lines[i]), lines[i+1]) for i in range(0, len(lines), 2)])
    assert report == intake["dependency_report"]
    unchanged("report")
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert listing["ok"] and len(listing["result"]["sets"]) == 2
    unchanged("listing")
    outcome = selected(intake)
    assert outcome["ok"] and outcome["result"]["count"] == 7
    unchanged("selection")
    saved = tmp_path / "saved-intake.json"
    saved.write_text(json.dumps(intake, allow_nan=False), encoding="utf-8")
    unchanged("save")
    replay = selected(json.loads(saved.read_text(encoding="utf-8")))
    assert replay["result"] == outcome["result"] and replay["overlay"] == outcome["overlay"]
    unchanged("replay")
    assert checks == ["intake", "report", "listing", "selection", "save", "replay"] and attempts == []
