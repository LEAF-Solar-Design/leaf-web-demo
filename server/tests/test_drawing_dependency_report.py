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


@pytest.mark.parametrize(
    "section,device,style,expected",
    [
        ("OBJECTS", "Office.pc3", "monochrome.ctb",
         [("plot_device", "Office.pc3"), ("plot_style", "monochrome.ctb")]),
        ("OBJECTS", r"C:\Plotters\Wide Format.pc3", "../styles/screened.stb",
         [("plot_device", r"C:\Plotters\Wide Format.pc3"),
          ("plot_style", "../styles/screened.stb")]),
        ("OBJECTS", "", "screened.stb", [("plot_style", "screened.stb")]),
        ("OBJECTS", "Office.pc3", "", [("plot_device", "Office.pc3")]),
        ("ENTITIES", "Decoy.pc3", "decoy.ctb", []),
    ],
)
def test_normal_intake_reports_plot_resources_without_resolving_them(
    section, device, style, expected, monkeypatch
):
    import builtins
    import io
    import os

    line = "0\nLINE\n5\nAA\n8\nDesign\n10\n1\n20\n2\n11\n3\n21\n4\n"
    settings = (
        "0\nPLOTSETTINGS\n5\nAB\n100\nAcDbPlotSettings\n"
        f"1\nSetup name.pc3\n2\n{device}\n4\nMedia name.ctb\n7\n{style}\n"
    )
    objects = settings if section == "OBJECTS" else ""
    entities = line + (settings if section == "ENTITIES" else "")
    raw = (
        f"0\nSECTION\n2\nENTITIES\n{entities}0\nENDSEC\n"
        f"0\nSECTION\n2\nOBJECTS\n{objects}0\nENDSEC\n0\nEOF\n"
    ).encode()

    def forbidden_open(*args, **kwargs):
        pytest.fail("Intake attempted to open a plot resource")

    with monkeypatch.context() as guard:
        for module in (builtins, io, os):
            guard.setattr(module, "open", forbidden_open)
        intake = parse_dxf_bytes(raw)

    report = intake["dependency_report"]
    assert report["scope"] == "local_dxf_records"
    assert report["external_files_opened"] is False
    assert report["dependencies"] == [
        {"kind": kind, "name": name, "disposition": "reported"} for kind, name in expected
    ]
    assert intake["polylines"][0]["pts"] == [[1.0, 2.0, 0.0], [3.0, 4.0, 0.0]]


@pytest.mark.parametrize("kind", ["MTEXT", "MULTILEADER", "TABLE", "ACAD_TABLE"])
def test_inline_font_controls_preserve_chunk_order_and_literal_names(kind):
    pairs = [(0, "SECTION"), (2, "ENTITIES"), (0, kind),
             (3, r"{\fSynthetic "), (3, "Panel"),
             (1, " Sans|b0|i0;Label} "), (304, r"\Fother.SHX;more"),
             (0, "ENDSEC"), (0, "EOF")]
    report = from_pairs(pairs)
    assert report["dependencies"] == [
        {"kind": "font", "name": "Synthetic Panel Sans",
         "disposition": "font remapped", "remapped_to": "sans-serif"},
        {"kind": "font", "name": "other.SHX",
         "disposition": "font remapped", "remapped_to": "sans-serif"}]


def test_inline_font_dependencies_reach_normal_reader_without_following_paths(monkeypatch):
    raw = ("0\nSECTION\n2\nENTITIES\n0\nMTEXT\n5\nA1\n8\nNotes\n"
           "10\n0\n20\n0\n40\n1\n1\n{\\f../missing.ttf;Label}\n"
           "0\nENDSEC\n0\nEOF\n").encode()
    import builtins

    def forbidden(*args, **kwargs):
        raise AssertionError("Inline font dependencies must not open referenced paths")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        intake = parse_dxf_bytes(raw)
    assert {row["name"] for row in intake["dependency_report"]["dependencies"]} == {"../missing.ttf"}
    assert intake["texts"]


def test_inline_fonts_ignore_nonfont_controls_wrong_sections_and_deduplicate():
    pairs = [(0, "SECTION"), (2, "TABLES"), (0, "MTEXT"),
             (1, r"\fWrong Section|b0;bad"), (0, "ENDSEC"),
             (0, "SECTION"), (2, "ENTITIES"),
             (0, "TEXT"), (1, r"\fLiteral DTEXT|b0;not formatting"),
             (0, "MTEXT"), (1, r"\fplainword;not a font \fRepeat|b0;yes \FRepeat|i0;again"),
             (0, "ENDSEC"), (0, "EOF")]
    assert [row["name"] for row in from_pairs(pairs)["dependencies"]] == ["Repeat"]


def test_inline_scanner_preserves_literal_paths_and_refuses_unterminated_controls():
    text = r"\fC:\Fonts\family.ttf;caption \fPanel Sans|b0;label"
    rows = from_pairs([(0, "MTEXT"), (1, text)])["dependencies"]
    assert [row["name"] for row in rows] == [r"C:\Fonts\family.ttf", "Panel Sans"]
    assert from_pairs([(0, "MTEXT"), (1, r"\f" * 10000)])["dependencies"] == []


@pytest.mark.parametrize("font", ["Synthetic Panel Sans", "\u5c4b\u6839 Sans", "  Literal Family  "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_normal_reader_keeps_literal_inline_font_chunks(font, newline):
    text = "{\\f" + font + "|b0|i0;Caption}"
    for boundary in range(1, len(text)):
        records = [(0, "SECTION"), (2, "ENTITIES"), (0, "MTEXT"),
                   (5, "A1"), (8, "Notes"), (10, "0"), (20, "0"), (40, "1"),
                   (3, text[:boundary]), (1, text[boundary:]),
                   (0, "ENDSEC"), (0, "EOF")]
        raw = "".join(str(code) + newline + value + newline for code, value in records).encode()
        observed = parse_dxf_bytes(raw)["dependency_report"]["dependencies"]
        assert [row["name"] for row in observed] == [font], (boundary, observed)


def test_literal_font_metadata_keeps_default_geometry_tokenization():
    from dxf_intake import _group_pairs

    raw = ("0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n A1 \n8\n Panels \n"
           "10\n 1 \n20\n 2 \n11\n 3 \n21\n 4 \n0\nENDSEC\n0\nEOF\n")
    assert _group_pairs("1\n  value  \n") == [(1, "value")]
    intake = parse_dxf_bytes(raw.encode())
    assert intake["polylines"][0]["pts"] == [[1.0, 2.0, 0.0], [3.0, 4.0, 0.0]]
    assert intake["polylines"][0]["layer"] == "Panels"


def test_literal_text_overrides_only_retain_inline_text_record_values():
    from dxf_intake import _group_pairs

    text = ("0\nSTYLE\n3\n font.shx \n0\nTEXT\n1\n label \n"
            "0\nMTEXT\n3\n{\\fA \n1\n Face|b0;text}\n0\nLINE\n10\n 1 \n")
    literal = {}
    pairs = _group_pairs(text, literal_text=literal)
    assert list(literal.values()) == ["{\\fA ", " Face|b0;text}"]
    assert pairs[1] == (3, "font.shx")
    assert pairs[3] == (1, "label")
    assert pairs[-1] == (10, "1")


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
