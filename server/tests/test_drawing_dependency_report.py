from drawing_dependency_report import from_pairs
from dxf_intake import parse_dxf_bytes


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
