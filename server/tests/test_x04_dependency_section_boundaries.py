"""Owning sections and terminal records bound the X04 decoded inventory."""
import builtins
import io
import os
from copy import deepcopy
import pytest
from drawing_dependency_report import from_pairs


def forbid_target_opens(monkeypatch):
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Dependency extraction must not open targets")
    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", forbidden)
    return attempts


def test_full_sections_exclude_phantom_dependencies(monkeypatch):
    attempts = forbid_target_opens(monkeypatch)
    pairs = [(0,"SECTION"),(2,"HEADER"),(0,"STYLE"),(3,"header-ghost.shx"),(0,"ENDSEC"),
        (0,"STYLE"),(3,"outside-ghost.shx"),
        (0,"SECTION"),(2,"TABLES"),(0,"IMAGEDEF"),(1,"table-ghost.png"),
        (0,"STYLE"),(3,"real.shx"),(0,"ENDSEC"),
        (0,"SECTION"),(2,"BLOCKS"),(0,"BLOCK"),(70,"4"),(1,"real.dwg"),(0,"ENDSEC"),
        (0,"SECTION"),(2,"ENTITIES"),(0,"IMAGEDEF"),(1,"entity-ghost.png"),
        (0,"ACAD_PROXY_ENTITY"),(5,"P1"),(0,"ENDSEC"),
        (0,"SECTION"),(2,"OBJECTS"),(0,"STYLE"),(3,"object-ghost.shx"),
        (0,"IMAGEDEF"),(1,"real.png"),(0,"ENDSEC"),(0,"EOF"),
        (0,"STYLE"),(3,"after-eof.shx")]
    snapshot = deepcopy(pairs)
    report = from_pairs(pairs)
    assert {(r["kind"],r["name"],r["disposition"]) for r in report["dependencies"]} == {
        ("font","real.shx","font remapped"),("xref","real.dwg","detached"),
        ("proxy","P1","proxy omitted"),("image","real.png","image omitted")}
    assert pairs == snapshot and attempts == []


@pytest.mark.parametrize("section,kind", [("TABLES","STYLE"),("BLOCKS","BLOCK"),("OBJECTS","IMAGEDEF")])
def test_endsec_and_eof_values_never_complete_previous_missing_names(monkeypatch, section, kind):
    attempts = forbid_target_opens(monkeypatch)
    pairs = [(0,"SECTION"),(2,section),(0,kind),(70,"4"),
             (0,"ENDSEC"),(1,"neighbor.png"),(3,"neighbor.shx"),
             (0,"EOF"),(0,"STYLE"),(3,"trailing.shx")]
    assert from_pairs(pairs)["dependencies"] == [] and attempts == []


def test_legacy_decoded_fragments_keep_records_but_stop_at_eof():
    report = from_pairs([(0,"STYLE"),(3,"first.shx"),(0,"EOF"),(0,"STYLE"),(3,"trailing.shx")])
    assert [r["name"] for r in report["dependencies"]] == ["first.shx"]
