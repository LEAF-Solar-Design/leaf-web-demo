from drawing_dependency_report import from_pairs
from dxf_intake import parse_dxf_bytes
import pytest


def test_reports_dependency_dispositions_without_following_paths():
    pairs = [(0, "BLOCK"), (2, "External"), (70, "4"), (1, "../external.dwg"),
             (0, "STYLE"), (2, "Notes"), (3, "notes.shx"), (4, "shapes.shx"),
             (0, "IMAGEDEF"), (1, "background.png"),
             (0, "PDFDEFINITION"), (1, "reference.pdf"),
             (0, "ACAD_PROXY_ENTITY"), (5, "A1")]
    report = from_pairs(pairs)
    rows = {row["name"]: row for row in report["dependencies"]}
    assert len(rows) == 6
    assert rows["../external.dwg"]["disposition"] == "detached"
    assert rows["notes.shx"]["remapped_to"] == "sans-serif"
    assert rows["shapes.shx"]["kind"] == "shape_font"
    assert rows["background.png"]["disposition"] == "image omitted"
    assert rows["reference.pdf"]["disposition"] == "underlay omitted"
    assert rows["A1"]["disposition"] == "proxy omitted"
    assert report["external_files_opened"] is False
    assert report["scope"] == "local_dxf_records"


def test_keeps_distinct_dependency_kinds_and_deduplicates_repeated_fonts():
    report = from_pairs([(0, "STYLE"), (3, "font.shx"),
                         (0, "STYLE"), (3, "font.shx"),
                         (0, "STYLE"), (70, "1"), (3, "font.shx")])
    assert len(report["dependencies"]) == 2


def test_normal_intake_adds_report_and_keeps_geometry():
    raw = ("0\nSECTION\n2\nTABLES\n0\nSTYLE\n2\nNotes\n3\nnotes.shx\n"
           "0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n5\nAA\n8\nDesign\n"
           "10\n1\n20\n2\n11\n3\n21\n4\n0\nENDSEC\n0\nEOF\n").encode()
    intake = parse_dxf_bytes(raw)
    assert intake["polylines"][0]["pts"] == [[1.0, 2.0, 0.0], [3.0, 4.0, 0.0]]
    assert intake["dependency_report"]["dependencies"][0]["name"] == "notes.shx"


def test_empty_dependencies_are_an_explicit_empty_report():
    assert from_pairs([])["dependencies"] == []


@pytest.mark.parametrize("path", [r"\\unavailable-host\share\reference.dat",
                                r"C:\not-present\reference.dat", "../../reference.dat"])
def test_dependency_paths_are_reported_without_any_filesystem_access(path, monkeypatch):
    import builtins
    import io
    import os

    attempts = []

    def forbidden_open(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Dependency inventory must not open referenced paths")

    pairs = [(0, "BLOCK"), (70, "4"), (1, path),
             (0, "STYLE"), (3, path), (4, path),
             (0, "IMAGEDEF"), (1, path),
             (0, "DGNDEFINITION"), (1, path)]
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden_open)
        guard.setattr(io, "open", forbidden_open)
        guard.setattr(os, "open", forbidden_open)
        report = from_pairs(pairs)
    assert attempts == []
    assert {(row["kind"], row["name"], row["disposition"])
            for row in report["dependencies"]} == {
        ("xref", path, "detached"), ("font", path, "font remapped"),
        ("shape_font", path, "font remapped"), ("image", path, "image omitted"),
        ("underlay", path, "underlay omitted")}


def test_unicode_records_reorder_stably_deduplicate_by_kind_and_preserve_input():
    from copy import deepcopy

    shared = '\u5c4b\u6839,"shared"\\name'
    records = [[(0, "STYLE"), (3, shared)], [(0, "IMAGEDEF"), (1, shared)],
               [(0, "STYLE"), (70, "1"), (3, shared)],
               [(0, "PDFDEFINITION"), (1, "\u53c2\u7167.pdf")],
               [(0, "STYLE"), (3, shared)]]
    pairs = [pair for record in records for pair in record]
    reordered = [pair for record in reversed(records) for pair in record]
    before, reordered_before = deepcopy(pairs), deepcopy(reordered)
    report = from_pairs(pairs)
    assert report == from_pairs(reordered)
    assert pairs == before and reordered == reordered_before
    assert [(row["kind"], row["name"]) for row in report["dependencies"]] == [
        ("font", shared), ("image", shared), ("shape_font", shared), ("underlay", "\u53c2\u7167.pdf")]


def test_sequential_intakes_have_drawing_local_reports_without_mutating_prior_output():
    from copy import deepcopy

    def drawing(font, object_record):
        return (f"0\nSECTION\n2\nTABLES\n0\nSTYLE\n2\nNotes\n3\n{font}\n"
                f"0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n{object_record}"
                "0\nENDSEC\n0\nEOF\n").encode()

    first = parse_dxf_bytes(drawing("first.shx", "0\nIMAGEDEF\n5\nA1\n1\nfirst.png\n"))["dependency_report"]
    preserved = deepcopy(first)
    empty = parse_dxf_bytes(b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")["dependency_report"]
    second = parse_dxf_bytes(drawing("second.shx", "0\nPDFDEFINITION\n5\nB1\n1\nsecond.pdf\n"))["dependency_report"]
    assert first == preserved
    assert empty["dependencies"] == []
    assert {row["name"] for row in first["dependencies"]} == {"first.shx", "first.png"}
    assert {row["name"] for row in second["dependencies"]} == {"second.shx", "second.pdf"}
