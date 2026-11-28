from drawing_dependency_report import from_pairs


def test_complex_linetype_shape_links_resolve_per_element():
    pairs = [
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "LTYPE"),
        (0, "LTYPE"), (5, "C1"), (2, "Annotation"),
        (49, ".25"), (74, "0"),
        (49, "0"), (74, "4"), (75, "7"), (340, "Aa"),
        (49, "0"), (74, "2"), (340, "BB"), (9, "ANNOTATION"),
        (49, "0"), (74, "5"), (75, "8"), (340, "bb"),
        (49, "0"), (74, "4"), (75, "9"), (340, "FF"),
        (49, "0"), (74, "4"), (75, "10"), (340, "AA"), (340, "BB"),
        (49, "0"), (74, "4"), (75, "11"),
        (49, "0"), (74, "4"), (75, "12"), (340, "CC"),
        (49, "0"), (74, "4"), (75, "13"), (340, "DD"),
        (0, "ENDTAB"),
        (0, "TABLE"), (2, "STYLE"),
        (0, "STYLE"), (5, "AA"), (70, "1"), (3, "primary.shx"),
        (0, "STYLE"), (5, "BB"), (70, "1"), (3, "alternate.shx"),
        (0, "STYLE"), (5, "CC"), (70, "1"), (3, "duplicate-a.shx"),
        (0, "STYLE"), (5, "cc"), (70, "1"), (3, "duplicate-b.shx"),
        (0, "STYLE"), (5, "DD"), (70, "0"), (3, "ordinary.shx"),
        (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "STYLE"), (5, "FF"), (70, "1"), (3, "out-of-section.shx"),
        (0, "ENDSEC"), (0, "EOF"),
    ]

    report = from_pairs(pairs)
    links = report["linetype_shape_links"]
    assert {(row["linetype"], row["linetype_handle"]) for row in links} == {("Annotation", "C1")}
    assert [
        (row["element_index"], row["shape_number"], row["style_handles"], row["status"], row["font"])
        for row in links
    ] == [
        (1, 7, ["AA"], "resolved", "primary.shx"),
        (3, 8, ["BB"], "resolved", "alternate.shx"),
        (4, 9, ["FF"], "dangling", None),
        (5, 10, ["AA", "BB"], "ambiguous", None),
        (6, 11, [], "missing_pointer", None),
        (7, 12, ["CC"], "ambiguous", None),
        (8, 13, ["DD"], "invalid_style", None),
    ]
    assert not report["external_files_opened"]


def test_text_only_linetype_does_not_add_shape_diagnostics():
    pairs = [(0, "LTYPE"), (49, "0"), (74, "2"), (340, "AA"), (9, "LABEL")]
    assert "linetype_shape_links" not in from_pairs(pairs)
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


def test_material_texture_inventory_uses_full_locator_and_saved_map_source():
    def material(handle, tags):
        return f"0\nMATERIAL\n5\n{handle}\n100\nAcDbMaterial\n{tags}"

    shared = "finish/Shared.png"
    first = material("A1", f"72\n1\n3\n{shared}\n271\n0\n72\n1\n3\nnormal/Shared.png\n")
    second = material("A2", f"77\n1\n4\n{shared}\n175\n0\n7\nscene.png\n")
    separate = material("A3", "179\n1\n8\nother/Shared.png\n9\nrefract.png\n273\n0\n")
    decoy = material("AA", "171\n1\n6\nwrong-section.png\n")
    raw = (
        f"0\nSECTION\n2\nENTITIES\n{decoy}0\nENDSEC\n"
        f"0\nSECTION\n2\nOBJECTS\n{first}{second}{separate}0\nENDSEC\n0\nEOF\n"
    ).encode()
    report = parse_dxf_bytes(raw)["dependency_report"]
    assert report["external_files_opened"] is False
    rows = report["dependencies"]
    assert all(row["kind"] == "material_texture" and row["disposition"] == "texture omitted"
               for row in rows)
    assert {row["name"]: row["material_maps"] for row in rows} == {
        shared: [{"material_handle": "A1", "channel": "diffuse"},
                 {"material_handle": "A2", "channel": "specular"}],
        "normal/Shared.png": [{"material_handle": "A1", "channel": "normal"}],
        "other/Shared.png": [{"material_handle": "A3", "channel": "bump"}],
        "refract.png": [{"material_handle": "A3", "channel": "refraction"}],
    }


@pytest.mark.parametrize("setup, layout_device, duplicates, status", [
    ("office", "Shared.pc3", False, "matched"),
    ("Office", "../local/Override.pc3", False, "matched"),
    ("Missing", "Local.pc3", False, "unresolved"),
    ("", "Direct.pc3", False, "direct"),
    ("Office", "Local.pc3", True, "ambiguous"),
])
def test_real_intake_binds_layout_plot_settings_in_object_subclasses(
    setup, layout_device, duplicates, status
):
    def layout(name, device, page_setup, handle):
        return (f"0\nLAYOUT\n5\n{handle}\n100\nAcDbPlotSettings\n"
                f"1\n{page_setup}\n2\n{device}\n7\nLocal.stb\n"
                f"100\nAcDbLayout\n1\n{name}\n2\nDecoy.pc3\n7\nDecoy.ctb\n")

    named = ("0\nPLOTSETTINGS\n5\nAB\n100\nAcDbPlotSettings\n"
             "1\nOffice\n2\nShared.pc3\n7\nShared.ctb\n")
    unused = named.replace("AB\n", "AD\n").replace("Office\n", "Reserve\n")
    objects = layout("Roof", layout_device, setup, "A1") + named + unused
    objects += layout("Model", "ModelOnly.pc3", setup, "A2")
    if duplicates:
        objects += named.replace("AB\n", "AC\n").replace("Shared.pc3", "Other.pc3")
    raw = ("0\nSECTION\n2\nENTITIES\n" + layout("Decoy", "Bad.pc3", setup, "A3") +
           "0\nENDSEC\n0\nSECTION\n2\nOBJECTS\n" + objects + "0\nENDSEC\n0\nEOF\n").encode()
    report = parse_dxf_bytes(raw)["dependency_report"]
    assert report["plot_layout_bindings"] == [{
        "layout": "Roof",
        "page_setup": setup or None,
        "layout_resources": {"plot_device": layout_device, "plot_style": "Local.stb"},
        "setup_status": status,
        "setup_resources": ({"plot_device": "Shared.pc3", "plot_style": "Shared.ctb"}
                            if status == "matched" else None),
    }]
    expected = {layout_device, "Local.stb", "Shared.pc3", "Shared.ctb"}
    if duplicates:
        expected.add("Other.pc3")
    by_name = {row["name"]: row for row in report["dependencies"]}
    assert set(by_name) == expected
    assert by_name["Shared.pc3"]["page_setups"] == ["Office", "Reserve"]
    assert by_name["Shared.ctb"]["page_setups"] == ["Office", "Reserve"]
    assert "page_setups" not in by_name["Local.stb"]
    assert report["external_files_opened"] is False


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
        {"kind": kind, "name": name, "disposition": "reported",
         "page_setups": ["Setup name.pc3"]} for kind, name in expected
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


@pytest.mark.parametrize("font", [
    "Synthetic Panel Sans", "\u5c4b\u6839 Sans", "  Literal Family  ",
    "Panel\vSans", "Detail\fBold", "Roof\u2028Notes",
])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
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
             (0, "DGNDEFINITION"), (1, path),
             (0, "MATERIAL"), (5, "AA"), (100, "AcDbMaterial"), (72, "1"), (3, path),
             (0, "LIGHT"), (5, "BB"), (100, "AcDbLight"), (290, "0"), (280, "1"),
             (290, "1"), (300, path)]
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
        ("underlay", path, "underlay omitted"), ("material_texture", path, "texture omitted"),
        ("photometric_web", path, "reported")}


def test_light_web_requires_its_subclass_and_an_enabled_web_flag():
    def light(handle, *tags):
        return [(0, "LIGHT"), (5, handle), *tags]

    path = (300, "fixtures/shared.ies")
    light_subclass = (100, "AcDbLight")
    status = (290, "1")
    enabled = (290, "1")
    disabled = (290, "0")
    pairs = [(0, "SECTION"), (2, "ENTITIES")]
    pairs += light("A", light_subclass, status, path)
    pairs += light("B", light_subclass, status, disabled, path)
    pairs += light("C", light_subclass, status, (100, "AcDbEntity"), enabled, path)
    pairs += light("D", status, enabled, path)
    pairs += light("E", light_subclass, disabled, (280, "1"), enabled, path)
    pairs += light("E", light_subclass, status, (1, "1"), (295, "0"), enabled, path)
    pairs += light("F", light_subclass, status, enabled, (300, ""))
    pairs += [(0, "ENDSEC"), (0, "SECTION"), (2, "OBJECTS")]
    pairs += light("G", light_subclass, status, enabled, path)
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    assert from_pairs(pairs)["dependencies"] == [{
        "kind": "photometric_web", "name": path[1], "disposition": "reported",
        "light_handles": ["E"],
    }]


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

@pytest.mark.parametrize("elements", [2, 8, 32])
def test_missing_explicit_line_linetypes_are_reported_with_references(elements):
    from io import StringIO

    import ezdxf

    doc = ezdxf.new("R2018")
    pattern = f"Project_Dash_{elements}"
    doc.linetypes.new(pattern, dxfattribs={
        "pattern": [3.0 * (elements // 2), *([2.0, -1.0] * (elements // 2))],
    })
    model = doc.modelspace()
    line = model.add_line((0, 0), (10, 0), dxfattribs={"linetype": pattern.lower()})
    polyline = model.add_lwpolyline([(0, 2), (10, 2)], dxfattribs={"linetype": pattern.lower()})
    model.add_line((0, 4), (10, 4), dxfattribs={"linetype": "Continuous"})
    model.add_line((0, 6), (10, 6), dxfattribs={"linetype": "BYBLOCK"})
    model.add_line((0, 8), (10, 8))

    def missing_linetypes():
        payload = StringIO()
        doc.write(payload)
        report = parse_dxf_bytes(payload.getvalue().encode("utf-8"))["dependency_report"]
        assert report["external_files_opened"] is False
        return [row for row in report["dependencies"] if row["kind"] == "linetype"]

    assert missing_linetypes() == []
    doc.linetypes.remove(pattern)
    assert missing_linetypes() == [{
        "kind": "linetype", "name": pattern.lower(), "disposition": "missing definition",
        "references": [
            {"entity_type": "LINE", "handle": line.dxf.handle},
            {"entity_type": "LWPOLYLINE", "handle": polyline.dxf.handle},
        ],
    }]


@pytest.mark.parametrize(("elements", "request_text"), [
    (2, "Flag an exported drawing if its lines name a pattern that the exported file no longer defines."),
    (8, "Report undefined linetypes in this exported DXF."),
    (32, "Check for missing line pattern definitions in this drawing."),
])
def test_authored_export_linetype_check_uses_real_intake(elements, request_text):
    from io import StringIO

    import ezdxf

    from intake_dxf import intake_to_dxf
    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    doc = ezdxf.new("R2018")
    name = f"Export_Dash_{elements}"
    doc.linetypes.new(name, dxfattribs={
        "pattern": [3.0 * (elements // 2), *([2.0, -1.0] * (elements // 2))],
    })
    model = doc.modelspace()
    first = model.add_lwpolyline([(0, 0), (10, 0)], dxfattribs={"linetype": name})
    second = model.add_lwpolyline([(0, 2), (10, 2)], dxfattribs={"linetype": name})
    model.add_lwpolyline([(0, 4), (10, 4)], dxfattribs={"linetype": "Continuous"})
    payload = StringIO()
    doc.write(payload)
    original = parse_dxf_bytes(payload.getvalue().encode("utf-8"))
    exported = parse_dxf_bytes(intake_to_dxf(original))

    manifest, code, _ = author_tool(request_text)
    assert manifest["engine_op"] == "missing_linetype_definitions"
    assert manifest["capabilities"] == ["drawing.read"]

    def run(intake):
        status, payload = _run_source_in_sandbox(code, "authored.py", intake, {})
        assert status == "ok", payload
        result, overlay = payload
        assert overlay is None
        return result

    assert run(original) == {"status": "clean", "missing_linetype_count": 0, "missing_linetypes": []}
    assert run(exported) == {
        "status": "missing_definitions", "missing_linetype_count": 1,
        "missing_linetypes": [{
            "kind": "linetype", "name": name, "disposition": "missing definition",
            "references": [
                {"entity_type": "LWPOLYLINE", "handle": first.dxf.handle},
                {"entity_type": "LWPOLYLINE", "handle": second.dxf.handle},
            ],
        }],
    }
    original["dependency_report"].pop("explicit_line_linetypes_checked")
    assert run(original)["status"] == "unavailable"
    assert author_tool("Flag panels near the roof edge.")[0]["engine_op"] == "highlight_near_edge"


@pytest.mark.parametrize(("case", "prompt", "status", "definitions"), [
    ("valid", "Show which panel shares a valid material and which panel points to an ambiguous material definition.",
     "resolved", ["Edge"]),
    ("missing", "Identify missing material links on panel outlines.", "missing", []),
    ("ambiguous", "List conflicting material definitions referenced by panels.", "ambiguous", ["Edge", "Other"]),
])
def test_authored_panel_material_report_on_saved_dxf(case, prompt, status, definitions):
    from io import StringIO

    import ezdxf

    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    doc = ezdxf.new("R2018")
    doc.layers.new("Panels")
    shared = doc.materials.new("Shared").dxf.handle
    edge = doc.materials.new("Edge").dxf.handle
    target = "FFFF" if case == "missing" else edge
    points = [(0, 0), (2, 0), (2, 1), (0, 1)]
    model = doc.modelspace()
    first = model.add_lwpolyline(points, close=True, dxfattribs={"layer": "Panels", "material_handle": shared})
    second = model.add_polyline2d(points, close=True, dxfattribs={"layer": "Panels", "material_handle": shared})
    third = model.add_lwpolyline(points, close=True, dxfattribs={"layer": "Panels", "material_handle": target})
    model.add_lwpolyline(points, dxfattribs={"material_handle": shared})
    doc.layout().add_lwpolyline(points, close=True, dxfattribs={"material_handle": shared})
    duplicate = doc.materials.new("Other").dxf.handle if case == "ambiguous" else None
    payload = StringIO()
    doc.write(payload)
    raw = payload.getvalue()
    if duplicate:
        record = f"  0\nMATERIAL\n  5\n{duplicate}\n"
        assert raw.count(record) == 1
        raw = raw.replace(record, f"  0\nMATERIAL\n  5\n{edge}\n")
    intake = parse_dxf_bytes(raw.encode("utf-8"))
    assert intake["dependency_report"]["external_files_opened"] is False

    manifest, code, _ = author_tool(prompt)
    assert manifest["engine_op"] == "panel_material_links"
    assert manifest["capabilities"] == ["drawing.read"]
    sandbox_status, payload = _run_source_in_sandbox(code, "authored.py", intake, {})
    assert sandbox_status == "ok", payload
    result, overlay = payload
    assert overlay is None
    assert result == {
        "status": "reported",
        "scope": "direct_material_links_on_closed_model_space_outlines",
        "links": [
            {"entity_handle": first.dxf.handle, "layer": "Panels", "material_handle": shared,
             "status": "shared", "definitions": ["Shared"]},
            {"entity_handle": second.dxf.handle, "layer": "Panels", "material_handle": shared,
             "status": "shared", "definitions": ["Shared"]},
            {"entity_handle": third.dxf.handle, "layer": "Panels", "material_handle": target,
             "status": status, "definitions": definitions},
        ],
    }
    sandbox_status, payload = _run_source_in_sandbox(code, "authored.py", {"dependency_report": {}}, {})
    assert sandbox_status == "ok" and payload[0]["status"] == "unavailable"


@pytest.mark.parametrize(("prompt", "devices", "expected_status"), [
    ("Identify which sheets share a valid setup and which point to a missing or conflicting one.",
     ["East.pc3"], "matched"),
    ("List layouts with missing page setups.", [], "unresolved"),
    ("Show sheets with conflicting plot setup bindings.", ["East.pc3", "Other.pc3"], "ambiguous"),
])
def test_authored_sheet_plot_setup_report_on_saved_dxf(prompt, devices, expected_status):
    from io import StringIO

    import ezdxf

    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    doc = ezdxf.new("R2018")
    for sheet, setup in (("West", "Office"), ("Center", "office"), ("East", "Edge")):
        doc.layouts.new(sheet).dxf_layout.dxf.page_setup_name = setup
    doc.objects.new_entity("PLOTSETTINGS", {
        "page_setup_name": "Office", "plot_configuration_file": "Shared.pc3",
    })
    for device in devices:
        doc.objects.new_entity("PLOTSETTINGS", {
            "page_setup_name": "Edge", "plot_configuration_file": device,
        })
    saved = StringIO()
    doc.write(saved)
    intake = parse_dxf_bytes(saved.getvalue().encode("utf-8"))
    manifest, code, _ = author_tool(prompt)
    assert manifest["engine_op"] == "sheet_plot_setups"
    assert manifest["capabilities"] == ["drawing.read"]

    status, payload = _run_source_in_sandbox(code, "authored.py", intake, {})
    assert status == "ok", payload
    result, overlay = payload
    assert overlay is None
    assert result["scope"] == "saved_sheet_plot_settings"
    assert result["shared_setups"] == [{"page_setup": "Office", "sheets": ["West", "Center"]}]
    bindings = {binding["layout"]: binding for binding in result["bindings"]}
    assert "Model" not in bindings
    assert bindings["Layout1"]["setup_status"] == "direct"
    assert bindings["Center"]["page_setup"] == "office"
    assert bindings["East"]["page_setup"] == "Edge"
    assert bindings["East"]["setup_status"] == expected_status
    assert (bindings["East"]["setup_resources"] is not None) == (expected_status == "matched")

    status, payload = _run_source_in_sandbox(code, "authored.py", {"dependency_report": {}}, {})
    assert status == "ok" and payload[0]["status"] == "unavailable"


@pytest.mark.parametrize(("case", "prompt", "expected_status", "fonts"), [
    ("resolved", "Report text leader font bindings.", "resolved", ["notes.shx"]),
    ("missing", "Which multileaders have a dangling style?", "missing_style", []),
    ("ambiguous", "List which notes have a missing style and which have two competing font definitions.",
     "ambiguous", ["notes.shx", "other.shx"]),
    ("blank", "Check notes for missing font definitions.", "missing_font", [None]),
])
def test_authored_note_style_report_on_saved_dxf(case, prompt, expected_status, fonts):
    from io import StringIO

    import ezdxf
    from ezdxf.math import Vec2
    from ezdxf.render.mleader import ConnectionSide

    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    doc = ezdxf.new("R2018")
    style = doc.styles.new("Notes", {"font": "notes.shx"})
    other = doc.styles.new("Other", {"font": "other.shx"})
    layout = doc.modelspace() if case == "resolved" else doc.layouts.new("East")
    builder = layout.add_multileader_mtext(dxfattribs={"layer": "Notes-East"})
    builder.set_content("Service disconnect", char_height=1, style="Notes")
    builder.add_leader_line(ConnectionSide.left, [Vec2(0, 0), Vec2(2, 0)])
    builder.build(Vec2(10, 5))
    note, = layout.query("MULTILEADER")
    block = doc.blocks.new("NoteTemplate")
    block_note = block.add_multileader_mtext()
    block_note.set_content("Template note", char_height=1, style="Other")
    block_note.add_leader_line(ConnectionSide.left, [Vec2(0, 0), Vec2(2, 0)])
    block_note.build(Vec2(10, 5))
    output = StringIO()
    doc.write(output)
    source = output.getvalue()
    style_handle = style.dxf.handle
    if case == "missing":
        marker = f"343\n{style_handle}\n"
        assert source.count(marker) == 1
        style_handle = "BADF00D"
        source = source.replace(marker, f"343\n{style_handle}\n")
    if case == "ambiguous":
        marker = f"  5\n{other.dxf.handle}\n"
        assert source.count(marker) == 1
        source = source.replace(marker, f"  5\n{style_handle}\n")
    if case == "blank":
        marker = "  3\nnotes.shx\n"
        assert source.count(marker) == 1
        source = source.replace(marker, "  3\n\n")
    intake = parse_dxf_bytes(source.encode())
    assert intake["dependency_report"]["external_files_opened"] is False

    manifest, code, _ = author_tool(prompt)
    assert manifest["engine_op"] == "note_styles"
    assert manifest["capabilities"] == ["drawing.read"]
    status, payload = _run_source_in_sandbox(code, "authored.py", intake, {})
    assert status == "ok", payload
    result, overlay = payload
    assert result["scope"] == "drawing_text_multileader_styles"
    assert overlay is None
    row, = result["notes"]
    assert row["note_handle"] == note.dxf.handle
    assert row["layer"] == "Notes-East"
    assert row["layout"] == ("Model" if case == "resolved" else "East")
    assert row["style_handles"] == [style_handle]
    assert row["status"] == expected_status
    assert [candidate["font"] for candidate in row["definitions"]] == fonts

    status, payload = _run_source_in_sandbox(code, "authored.py", {"dependency_report": {}}, {})
    assert status == "ok" and payload[0]["status"] == "unavailable"


def test_authored_schedule_styles_distinguish_missing_cycles_and_competing_definitions():
    from dxf_intake import parse_dxf_bytes
    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    def table(handle, target):
        return [(0, "ACAD_TABLE"), (5, handle), (100, "AcDbEntity"), (8, "0"),
                (100, "AcDbTable"), (342, target), (91, 1), (92, 1),
                (171, 1), (1, "Schedule value"), (342, "B2")]

    def style(handle, description):
        return [(0, "TABLESTYLE"), (5, handle), (100, "AcDbTableStyle"), (3, description)]

    pairs = [(0, "SECTION"), (2, "ENTITIES")]
    for handle, target in [("A1", "B1"), ("A2", "C1"), ("A3", "A3"), ("A4", "B2"),
                           ("A5", "A6"), ("A6", "A5"), ("A7", "A5")]:
        pairs.extend(table(handle, target))
    pairs += [(0, "ENDSEC"), (0, "SECTION"), (2, "OBJECTS")]
    pairs += style("B1", "Valid") + style("B2", "Field") + style("B2", "Office")
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    intake = parse_dxf_bytes("".join(f"{code}\n{value}\n" for code, value in pairs).encode())

    prompt = "Identify which schedules have a missing style, a cycle or competing definitions."
    manifest, code, _ = author_tool(prompt)
    assert manifest["engine_op"] == "table_style_dependencies"
    assert manifest["capabilities"] == ["drawing.read"]
    status, payload = _run_source_in_sandbox(code, "authored.py", intake, {})
    assert status == "ok", payload
    report, overlay = payload
    assert overlay is None and report["scope"] == "drawing_schedule_styles"
    rows = {row["table_handle"]: row for row in report["schedules"]}
    assert {handle: row["status"] for handle, row in rows.items()} == {
        "A1": "resolved", "A2": "missing_style", "A3": "cyclic", "A4": "ambiguous",
        "A5": "cyclic", "A6": "cyclic", "A7": "invalid_style",
    }
    assert rows["A1"]["style_handles"] == ["B1"]
    assert [item["description"] for item in rows["A4"]["definitions"]] == ["Field", "Office"]
    assert intake["dependency_report"]["external_files_opened"] is False


def test_authored_tolerance_fonts_resolve_only_a_unique_text_style():
    from dxf_intake import parse_dxf_bytes
    from tool_loader import _run_source_in_sandbox
    from tools_fallback import author_tool

    pairs = [(0, "SECTION"), (2, "TABLES")]
    for handle, font in [("B1", "main.shx"), ("B2", "first.shx"), ("B2", "second.shx")]:
        pairs += [(0, "STYLE"), (5, handle), (2, font), (3, font)]
    for frame, pointer in [("A1", "B1"), ("A2", "B1"), ("A3", "B1"), ("A4", "FF"),
                           ("A5", "C1"), ("A6", "B2")]:
        pairs += [(0, "DIMSTYLE"), (100, "AcDbDimStyleTableRecord"), (2, frame), (340, pointer)]
    pairs += [(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (5, "C1")]
    for frame, glyph in [("A1", ""), ("A2", r"\Fglyph.ttf;"), ("A3", r"\FMAIN.SHX;"),
                         ("A4", ""), ("A5", ""), ("A6", "")]:
        pairs += [(0, "TOLERANCE"), (5, frame), (100, "AcDbFcf"), (3, frame.lower()), (1, glyph + "C7")]
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    intake = parse_dxf_bytes("".join(f"{code}\n{value}\n" for code, value in pairs).encode())
    manifest, source, _ = author_tool("Check each feature control frame for an inherited font or a glyph override.")
    assert manifest["engine_op"] == "tolerance_font_dependencies"
    assert manifest["capabilities"] == ["drawing.read"]
    status, payload = _run_source_in_sandbox(source, "authored.py", intake, {})
    assert status == "ok", payload
    report, overlay = payload
    assert overlay is None
    assert [(row["frame_handle"], row["status"], row["font"], row["uses_second_font"])
            for row in report["frames"]] == [
        ("A1", "resolved", "main.shx", False),
        ("A2", "resolved", "main.shx", True),
        ("A3", "resolved", "main.shx", False),
        ("A4", "missing_style", None, None),
        ("A5", "wrong_type", None, None),
        ("A6", "ambiguous", None, None),
    ]
    status, payload = _run_source_in_sandbox(source, "authored.py", {"dependency_report": {}}, {})
    assert status == "ok" and payload[0]["status"] == "unavailable"
