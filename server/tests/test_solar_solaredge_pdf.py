"""S1 parity: page-one primitives of the SolarEdge fixture equal the plugin's PdfPig golden.

The golden dump is not committed; its per-section digests are
(docs/parity/evidence/solaredge/golden-digests.json, made by scripts/solaredge_golden_digest.py
with the canonicalization both sides share). The small synthetic PDFs below pin the feed rules
and the bounded refusals.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import solar_solaredge_pdf as pdf
from solar_solaredge_pdf import SolarEdgePdfError


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "data" / "solaredge_1to1_demo.pdf"
DIGESTS = REPO_ROOT / "docs" / "parity" / "evidence" / "solaredge" / "golden-digests.json"


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
def fixture_sections(golden):
    if not FIXTURE.exists():
        pytest.skip(f"fixture PDF absent: {FIXTURE}")
    data = FIXTURE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == golden["pdf_sha256"], "fixture PDF differs from the golden's"
    return pdf.primitives_to_golden(pdf.extract_primitives(data))


@pytest.mark.parametrize("section", ["lines", "curves", "letters", "paths"])
def test_section_equals_golden(fixture_sections, golden, section):
    want = golden["sections"][section]
    assert len(fixture_sections[section]) == want["count"]
    assert DIGEST.digest(fixture_sections[section]) == want["sha256"]


def test_page_size_equals_golden(fixture_sections, golden):
    assert DIGEST.canonical(fixture_sections["page_width"]) == golden["scalars"]["page_width"]
    assert DIGEST.canonical(fixture_sections["page_height"]) == golden["scalars"]["page_height"]


# ---------------------------------------------------------------- synthetic PDFs

def _pdf(content, page_extra=b"", resources=b"", mediabox=b"0 0 200 100"):
    """A minimal one-page PDF with a correct xref table."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [" + mediabox + b"] /Contents 4 0 R "
        b"/Resources << " + resources + b" >> " + page_extra + b">>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


HELVETICA = b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"


def test_feed_a_is_raw_flipped_and_ignores_q_restore():
    prims = pdf.extract_primitives(_pdf(
        b"1 0 0 RG 10 10 m 20 30 l 40 50 60 70 80 90 c S "
        b"q 0 1 0 RG Q 5 5 m 6 6 l S 7 7 m 8 8 9 9 v S"))
    assert prims.page_height == 100.0
    assert prims.lines[0] == (10.0, 90.0, 20.0, 70.0, (1.0, 0.0, 0.0))
    assert prims.curves[0] == (20.0, 70.0, 40.0, 50.0, 60.0, 30.0, 80.0, 10.0, (1.0, 0.0, 0.0))
    # RG inside q..Q persists for feed A, as the converter never restores it.
    assert prims.lines[1][4] == (0.0, 1.0, 0.0)
    # v: the first control point is the current point.
    assert prims.curves[1] == (7.0, 93.0, 7.0, 93.0, 8.0, 92.0, 9.0, 91.0, (0.0, 1.0, 0.0))
    # Feed B does restore: the path painted after Q carries the stroke colour set before q.
    assert prims.paths[1][4] == (1.0, 0.0, 0.0)


def test_path_bbox_uses_bezier_extrema_not_control_points():
    prims = pdf.extract_primitives(_pdf(b"0 0 1 rg 10 10 m 10 40 40 44 40 20 c b"))
    filled, stroked, bbox, fill, stroke = prims.paths[0]
    assert filled and stroked
    ys = [pdf._bezier_value(10, 40, 44, 20, t / 100000) for t in range(100001)]
    assert bbox[:3] == (10.0, 10.0, 40.0)
    assert bbox[3] == pytest.approx(max(ys), abs=1e-6)
    assert bbox[3] < 44  # the control-point hull would reach 44
    assert fill == (0.0, 0.0, 1.0)
    assert stroke is None  # DeviceGray stroke: not an RGB colour


def test_bezier_with_zero_leading_coefficient_keeps_end_points():
    # PdfPig solves the derivative as a quadratic and divides by 2a; with a == 0 both roots are
    # inf/NaN, so a symmetric bulge is not widened. Ported as read from PdfPig 0.1.13; the
    # fixture has no such curve, so the golden does not cover this branch.
    assert pdf._bezier_extent(10, 40, 40, 10) == (10, 10)


def test_letters_origin_and_width():
    prims = pdf.extract_primitives(_pdf(b"BT /F1 10 Tf 5 6 Td (AB) Tj ET", resources=HELVETICA))
    (a, ax, ay, aw), (b, bx, by, _bw) = prims.letters
    assert (a, b) == ("A", "B")
    assert (ax, ay) == (5.0, 6.0) and by == 6.0
    assert aw == pytest.approx(6.67, abs=1e-9)
    assert bx == pytest.approx(5.0 + 6.67, abs=1e-9)


@pytest.mark.parametrize("data, code", [
    (b"not a pdf", "NOT_A_PDF"),
    (b"%PDF-1.4\n garbage", "MALFORMED_PDF"),
    (_pdf(b"10 m"), "MISSING_OPERANDS"),
    (_pdf(b"/x 10 m"), "BAD_OPERAND"),
    (_pdf(b"/CS0 CS"), "COLORSPACE_UNSUPPORTED"),
    (_pdf(b"BT /F9 10 Tf (A) Tj ET"), "UNDEFINED_FONT"),
    (_pdf(b"", page_extra=b"/Rotate 90 "), "PAGE_ROTATION_UNSUPPORTED"),
    (_pdf(b"", mediabox=b"10 10 200 100"), "PAGE_BOX_UNSUPPORTED"),
])
def test_refuses_malformed_or_unsupported(data, code):
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(data)
    assert err.value.code == code


def test_refuses_oversize_pdf(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 64)
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(_pdf(b"1 0 0 RG"))
    assert err.value.code == "PDF_TOO_LARGE"


def test_refuses_too_many_operators(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_OPERATORS", 3)
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(_pdf(b"1 0 0 RG 1 1 m 2 2 l S"))
    assert err.value.code == "TOO_MANY_OPERATORS"


def test_refuses_too_many_pages(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 0)
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(_pdf(b""))
    assert err.value.code == "TOO_MANY_PAGES"


def test_refuses_too_many_paths_and_letters(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PATHS", 1)
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(_pdf(b"1 1 m 2 2 l S 3 3 m 4 4 l S"))
    assert err.value.code == "TOO_MANY_PATHS"
    monkeypatch.setattr(pdf, "MAX_LETTERS", 1)
    with pytest.raises(SolarEdgePdfError) as err:
        pdf.extract_primitives(_pdf(b"BT /F1 10 Tf (AB) Tj ET", resources=HELVETICA))
    assert err.value.code == "TOO_MANY_LETTERS"
