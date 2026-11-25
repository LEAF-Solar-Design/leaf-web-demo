"""S2 parity: the converter port reproduces the plugin's golden parse of the SolarEdge fixture.

Every section is compared by digest against docs/parity/evidence/solaredge/golden-digests.json
(see scripts/solaredge_golden_digest.py for the shared canonicalization). A small hand-built
page pins the pipeline end to end, the plugin quirks the port keeps, and the bounded refusals.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
from pathlib import Path

import pytest

import solar_solaredge_parse as sp
import solar_solaredge_pdf as pdf
from solar_solaredge_parse import SolarEdgeParseError
from solar_solaredge_pdf import PagePrimitives, extract_primitives
from solar_solaredge_parse_worker import verdict_bytes


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "solaredge_1to1_demo.pdf"
DIGESTS = REPO_ROOT / "docs" / "parity" / "evidence" / "solaredge" / "golden-digests.json"
S2_SECTIONS = ["panels", "optimizers", "grids", "matrices", "panel_layout", "inverter_strings",
               "all_string_infos", "inverter_pdf_colors"]


def _load_digest_module():
    spec = importlib.util.spec_from_file_location(
        "solaredge_golden_digest", REPO_ROOT / "scripts" / "solaredge_golden_digest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIGEST = _load_digest_module()


@pytest.fixture(scope="module")
def golden():
    return json.loads(DIGESTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_parse(golden):
    if not FIXTURE.exists():
        pytest.skip(f"fixture PDF absent: {FIXTURE}")
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == golden["pdf_sha256"], "fixture PDF differs from the golden's"
    parse, matrices = sp.parse_primitives(extract_primitives(data), golden["base_name"])
    return sp.parse_to_golden(parse, matrices)


@pytest.mark.parametrize("section", S2_SECTIONS)
def test_section_equals_golden(fixture_parse, golden, section):
    want = golden["sections"][section]
    assert len(fixture_parse[section]) == want["count"]
    assert DIGEST.digest(fixture_parse[section]) == want["sha256"]


@pytest.mark.parametrize("scalar", ["legend_threshold", "has_position_keywords", "optimizer_ratio"])
def test_scalar_equals_golden(fixture_parse, golden, scalar):
    assert DIGEST.canonical(fixture_parse[scalar]) == golden["scalars"][scalar]


def test_fixture_headline_figures(fixture_parse):
    assert len(fixture_parse["panels"]) == 3526
    assert len(fixture_parse["optimizers"]) == 3526
    assert len(fixture_parse["grids"]) == 14
    assert len(fixture_parse["all_string_infos"]) == 116
    assert sorted(fixture_parse["inverter_strings"], key=int) == [str(i) for i in range(1, 14)]


# ---------------------------------------------------------------- hand-built page

RED = (1.0, 0.0, 0.0)
PANEL_FILL = (0.9, 0.95, 1.0)
H = 1000.0


def _panel_path(left, bottom):
    return (True, True, (left, bottom, left + 30.0, bottom + 60.0), PANEL_FILL, None)


def _optimizer_path(cx, cy_user):
    return (False, True, (cx - 5, cy_user - 5, cx + 5, cy_user + 5), None, RED)


def _word(text, x, y_user, width=5.0):
    letters = []
    for ch in text:
        w = 2.0 if ch == "." else width
        letters.append((ch, x, y_user, w))
        x += w
    return letters


def _page(with_second_grid=True):
    """Grid A: panels 0 and 1 side by side, strung 1.1; grid B: panel 2 alone, string 1.2."""
    paths = [_panel_path(100, 500), _panel_path(140, 500),
             _optimizer_path(115, 530), _optimizer_path(155, 530)]
    curves = [(115.0, 470.0, 125.0, 470.0, 145.0, 470.0, 155.0, 470.0, RED)]
    letters = _word("1.1", 112, 528) + _word("7", 152, 528) + _word("Legend", 940, 900)
    if with_second_grid:
        paths += [_panel_path(600, 100), _optimizer_path(615, 130)]
        curves.append((615.0, 870.0, 630.0, 870.0, 640.0, 870.0, 650.0, 870.0, RED))
        letters += _word("1.2", 612, 128)
    return PagePrimitives(page_width=1000.0, page_height=H, lines=[], curves=curves,
                          letters=letters, paths=paths)


def test_hand_built_page_end_to_end():
    parse, matrices = sp.parse_primitives(_page(), "demo")
    out = sp.parse_to_golden(parse, matrices)
    assert out["legend_threshold"] == 920.0
    assert [(p["cx"], p["cy"]) for p in out["panels"]] == [(115.0, 470.0), (155.0, 470.0),
                                                           (615.0, 870.0)]
    assert [(o["panel_ids"], o["label"]) for o in out["optimizers"]] == [([0], "1.1"), ([1], "7"),
                                                                       ([2], "1.2")]
    assert out["grids"] == [[0, 1], [2]]
    assert out["optimizer_ratio"] == {"type": "1:1", "frequency": 1, "two_panel": 0,
                                      "one_panel": 3}
    first = out["matrices"][0]
    assert first["Dwgname"] == "demo_grid1.dwg"
    assert first["Sequences"] == [2] and first["Modify"] == [2, 0]
    # Rows are emitted rotated 180 degrees: the last column first.
    assert [(c["Id"], c["Seq"], c["InverterId"], c["StringInputNumber"])
            for c in first["Rows"][0]["Panels"]] == [("0001", 2, 1, 1), ("0000", 1, 1, 1)]
    assert out["inverter_strings"]["1"][0]["PanelSeqs"] == [1, 2]
    assert [s["Label"] for s in out["inverter_strings"]["1"]] == ["1.1", "1.2"]
    assert out["inverter_pdf_colors"] == {"1": [1.0, 0.0, 0.0]}


def test_single_grid_page_keeps_the_plugin_quirk_of_no_rows():
    # MergeElectricallyConnectedGrids returns early for one grid and never assigns rows, so the
    # plugin emits a matrix with no Rows. The port keeps that.
    parse, matrices = sp.parse_primitives(_page(with_second_grid=False), "demo")
    assert len(matrices) == 1
    assert matrices[0]["Rows"] == [] and matrices[0]["Sequences"] == [2]
    assert {p.row for p in parse.panels} == {-1}


# ---------------------------------------------------------------- .NET semantics and bounds

def test_dotnet_sort_orders_and_is_stable_up_to_sixteen():
    rng = random.Random(7)
    for n in range(0, 200):
        items = [(rng.randrange(5), i) for i in range(n)]
        got = list(items)
        sp._dotnet_sort(got, lambda a, b: sp._cmp(a[0], b[0]))
        assert [k for k, _ in got] == sorted(k for k, _ in items)
        assert sorted(got) == sorted(items)
        if n <= 16 and n != 3:
            assert got == sorted(items, key=lambda kv: kv[0])


def test_dotnet_int_matches_int_parse():
    assert sp._dotnet_int("12") == 12
    assert sp._dotnet_int("12\n") == 12
    for bad in ("\u0663", "2147483648", ""):
        with pytest.raises(SolarEdgeParseError):
            sp._dotnet_int(bad)


def test_refuses_over_work_budget(monkeypatch):
    monkeypatch.setattr(sp, "MAX_WORK", 3)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "WORK_BUDGET_EXCEEDED"


def test_refuses_too_many_panels(monkeypatch):
    monkeypatch.setattr(sp, "MAX_PANELS", 2)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "TOO_MANY_PANELS"


def test_refuses_too_many_optimizers(monkeypatch):
    monkeypatch.setattr(sp, "MAX_OPTIMIZERS", 2)
    with pytest.raises(SolarEdgeParseError) as err:
        sp.parse_primitives(_page())
    assert err.value.code == "TOO_MANY_OPTIMIZERS"


def _stamp_layout(x, y):
    return f"""
    .9 .95 1 rg 0 0 0 RG {x} {y} 30 60 re B
    1 0 0 RG {x + 10} {y + 25} 10 10 re S
    {x + 15} {y + 30} m {x + 22} {y + 31} {x + 30} {y + 31} {x + 35} {y + 30} c S
    0 0 0 rg BT /F1 6 Tf {x + 12} {y + 28} Td (1.1) Tj ET
    """.encode()


def _stamp_report(content, *, stamp=None, rect=b"0 0 1000 1000", matrix=b"", state=False,
                  flags=4, subtype=b"Stamp"):
    font = b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"

    def stream(data, attrs=b""):
        return b"<< /Length %d %s >>\nstream\n%s\nendstream" % (len(data), attrs, data)

    annots = b"/Annots [5 0 R]" if stamp is not None else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 1000] /Contents 4 0 R "
        b"/Resources << %s >> %s >>" % (font, annots),
        stream(content),
    ]
    if stamp is not None:
        appearance = b"<< /On 6 0 R /Off 7 0 R >>" if state else b"6 0 R"
        state_name = b"/AS /On" if state else b""
        objects.append(b"<< /Type /Annot /Subtype /%s /F %d /Rect [%s] /AP << /N %s >> %s >>"
                       % (subtype, flags, rect, appearance, state_name))
        form = b"/Type /XObject /Subtype /Form /BBox [0 0 1000 1000] /Resources << %s >> %s" % (font, matrix)
        objects.append(stream(stamp, form))
        if state:
            objects.append(stream(_stamp_layout(750, 750), form))
    return _vector_pdf(objects)


def _vector_pdf(objects, version=b"1.4"):
    pdf = bytearray(b"%PDF-" + version + b"\n")
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(pdf)


@pytest.mark.parametrize("rect,matrix,state,offset", [
    (b"0 0 1000 1000", b"", False, (0, 0)),
    (b"50 25 1050 1025", b"/Matrix [2 0 0 3 15 -20]", False, (50, 25)),
    (b"0 0 1000 1000", b"", True, (0, 0)),
], ids=["direct-normal", "transformed-normal", "chosen-normal-state"])
def test_printable_stamp_import_matches_flattened_layout(rect, matrix, state, offset):
    source = _stamp_layout(100, 500) + _stamp_layout(700, 100)
    flat = _stamp_layout(100 + offset[0], 500 + offset[1]) + _stamp_layout(700 + offset[0], 100 + offset[1])
    expected = json.loads(verdict_bytes(_stamp_report(flat)))
    assert len(expected["matrices"]) == 2
    assert json.loads(verdict_bytes(_stamp_report(b"", stamp=source, rect=rect, matrix=matrix, state=state))) == expected


@pytest.mark.parametrize("flags,subtype", [(0, b"Stamp"), (6, b"Stamp"), (4, b"FreeText")])
def test_nonprinting_hidden_and_other_annotations_do_not_supply_panels(flags, subtype):
    base = _stamp_layout(100, 500)
    assert verdict_bytes(_stamp_report(base, stamp=_stamp_layout(700, 100), flags=flags, subtype=subtype)) == (
        verdict_bytes(_stamp_report(base))
    )


@pytest.mark.parametrize("stamp", [
    _stamp_layout(1000, 500),
    b"0 0 10 10 re W n\n" + _stamp_layout(100, 500),
], ids=["outside-bbox", "clip-operator"])
def test_stamp_refuses_clipped_layout_instead_of_importing_hidden_panels(stamp):
    verdict = json.loads(verdict_bytes(_stamp_report(_stamp_layout(100, 500), stamp=stamp)))
    assert verdict["refusal"] == "REPORT_PDF_UNSUPPORTED"


def test_stamp_content_counts_toward_the_page_content_budget(monkeypatch):
    base = _stamp_layout(100, 500)
    stamp = _stamp_layout(700, 100)
    monkeypatch.setattr(pdf, "MAX_CONTENT_BYTES", max(len(base), len(stamp)))
    assert "matrices" in json.loads(verdict_bytes(_stamp_report(base)))
    verdict = json.loads(verdict_bytes(_stamp_report(base, stamp=stamp)))
    assert verdict["refusal"] == "REPORT_RESOURCE_LIMIT"


def _layer_report(content, membership=b"5 0 R", *, form=None):
    font = b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"

    def stream(data, attrs=b""):
        return b"<< /Length %d %s >>\nstream\n%s\nendstream" % (len(data), attrs, data)

    xobjects = b"/XObject << /Panel 8 0 R >>" if form is not None else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [5 0 R 6 0 R] "
        b"/D << /BaseState /OFF /ON [5 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 1000] /Contents 4 0 R "
        b"/Resources << %s %s /Properties << /Visible %s /Hidden 6 0 R >> >> >>"
        % (font, xobjects, membership),
        stream(content),
        b"<< /Type /OCG /Name (On) >>",
        b"<< /Type /OCG /Name (Off) >>",
        b"<< /Type /OCMD /OCGs [5 0 R 6 0 R] /P /AnyOn >>",
    ]
    if form is not None:
        objects.append(stream(form, b"/Subtype /Form /BBox [0 0 1000 1000] /Resources << %s >>" % font))
    return _vector_pdf(objects, version=b"1.5")


@pytest.mark.parametrize("membership", [
    b"5 0 R",
    b"7 0 R",
    b"<< /Type /OCMD /VE [/And 5 0 R [/Not 6 0 R]] >>",
], ids=["registered-group", "membership-policy", "nested-expression"])
def test_import_uses_visible_layout_and_wiring_from_default_layers(membership):
    base = _stamp_layout(100, 500)
    hidden = _stamp_layout(700, 100)
    layered = b"/OC /Visible BDC\n" + base + b"EMC\n/OC /Hidden BDC\n" + hidden + b"EMC\n"
    assert verdict_bytes(_layer_report(layered, membership)) == verdict_bytes(_stamp_report(base))


def test_reused_layer_form_matches_visible_page_space_wiring():
    form = _stamp_layout(100, 500)
    layered = _layer_report(
        b"/OC /Hidden BDC /Panel Do EMC\n"
        b"/OC /Visible BDC q 1 0 0 1 400 -100 cm /Panel Do Q EMC", form=form,
    )
    assert verdict_bytes(layered) == verdict_bytes(_stamp_report(_stamp_layout(500, 400)))


def test_hidden_text_still_advances_the_visible_text_matrix():
    before = b"BT /F1 10 Tf 100 500 Td "
    hidden = b"/OC /Hidden BDC (hidden) Tj EMC "
    after = b"(visible) Tj ET"
    layered = pdf.extract_primitives(_layer_report(before + hidden + after))
    control = pdf.extract_primitives(_stamp_report(before + b"(hidden) Tj " + after))
    assert layered.letters == control.letters[6:]


@pytest.mark.parametrize("membership", [
    b"<< /Type /OCMD /VE [/Or 5 0 R [/Bogus 6 0 R]] >>",
    b"<< /Type /OCMD /OCGs [5 0 R 7 0 R] /P /AnyOn >>",
    b"<< /Type /OCMD /VE [/Not 5 0 R 6 0 R] >>",
    b"<< /Type /OCMD /VE 5 0 R >>",
])
def test_invalid_layer_membership_is_refused_even_after_a_visible_operand(membership):
    report = _layer_report(b"/OC /Visible BDC\n" + _stamp_layout(100, 500) + b"EMC", membership)
    assert json.loads(verdict_bytes(report))["refusal"] == "REPORT_PDF_UNSUPPORTED"
