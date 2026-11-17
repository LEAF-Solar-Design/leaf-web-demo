"""Synthetic spatial contracts; real report verification belongs to the caller."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benches import x05_layout_oracle as oracle


def row(identifier="1.1", optimizers="4", modules="7", marker="x", model="DemoA9", top=10):
    return [dict(text=text, x0=i * 30, x1=i * 30 + 20,
                 top=top, bottom=top + 10, upright=True)
            for i, text in enumerate((identifier, optimizers, marker, model, modules))]


def page(words, number=1):
    return {"page": number, "words": words}


def assert_unreadable(result, code):
    assert result.status == "UNREADABLE"
    assert code in result.uncertainties
    assert result.totals == dict.fromkeys(("modules", "optimizers", "inverters", "strings"))


def test_independent_odd_counts_multiple_inverters_and_unrelated_words():
    unrelated = [dict(text=text) for text in ("Synthetic", "report", "1.2", "revision")]
    pages = [page(unrelated + row() + row("1.2", "3", "5", marker="X", top=30)),
             page(row("2.1", "2", "9", marker="\u00d7"), 2)]
    original = deepcopy(pages)
    result = oracle.parse_legend_pages(pages)
    assert result.status == "READABLE"
    assert result.totals == {"modules": 21, "optimizers": 9, "inverters": 2, "strings": 3}
    assert [r.modules for r in result.strings] == [7, 5, 9]
    location = result.strings[0].provenance[0]
    assert location.page == 1 and location.word_indices == [4, 5, 6, 7, 8]
    assert location.bboxes == [[0, 10, 20, 20], [30, 10, 50, 20],
                               [60, 10, 80, 20], [90, 10, 110, 20], [120, 10, 140, 20]]
    assert result.to_dict() == oracle.parse_legend_pages(pages).to_dict()
    assert pages == original
    json.dumps(result.to_dict(), allow_nan=False)
    result.to_dict()["strings"][0]["provenance"][0]["bboxes"][0][0] = 999
    assert pages == original and location.bboxes[0][0] == 0


def test_identical_duplicates_retain_bounded_locations_without_double_counting():
    words = sum((row(top=10 + i * 20) for i in range(12)), [])
    result = oracle.parse_legend_pages([page(words), page(row(), 2)])
    assert result.status == "READABLE"
    assert result.totals == {"modules": 7, "optimizers": 4, "inverters": 1, "strings": 1}
    assert len(result.strings[0].provenance) == oracle.MAX_LOCATIONS
    assert result.strings[0].provenance[1].word_indices == [5, 6, 7, 8, 9]
    across_pages = oracle.parse_legend_pages([page(row()), page(row(), 2)])
    assert [p.page for p in across_pages.strings[0].provenance] == [1, 2]


@pytest.mark.parametrize("change", [{"optimizers": "5"}, {"modules": "8"}, {"model": "DemoB9"}])
def test_conflicting_duplicates_invalidate_all_totals(change):
    for words in (row() + row(**change), row(**change) + row()):
        result = oracle.parse_legend_pages([page(words)])
        assert_unreadable(result, "CONFLICTING_DUPLICATE")
        assert len(result.strings) == 2
        assert all(len(record.provenance) == 1 for record in result.strings)


@pytest.mark.parametrize("value", ["0", "-2", "+2", "02", "2.5", "2e1", "2,000", "NaN",
                                    "\u0662", "1000000000", ""])
@pytest.mark.parametrize("field", ["optimizers", "modules"])
def test_bad_counts_do_not_leave_authoritative_partial_rows(field, value):
    words = row() + row("2.1", **{field: value})
    assert_unreadable(oracle.parse_legend_pages([page(words)]), "INVALID_COUNT")


@pytest.mark.parametrize("identifier", ["0.1", "01.1", "1.0", "1.01", "\u0661.1", "1.2.3",
                                         "1000000000.1", "-1.1"])
def test_identifier_grammar(identifier):
    assert_unreadable(oracle.parse_legend_pages([page(row(identifier))]), "INVALID_IDENTIFIER")


@pytest.mark.parametrize("change", [{"marker": "*"}, {"model": "9Demo"}, {"model": "Demo-9"},
                                     {"model": "D" * 65}, {"model": "D\u00e9mo"}])
def test_exact_marker_and_model_grammar(change):
    assert_unreadable(oracle.parse_legend_pages([page(row(**change))]), "INVALID_ROW_GRAMMAR")


@pytest.mark.parametrize("field,value", [("x0", True), ("x0", float("nan")),
                                         ("x1", float("inf")), ("top", "10"),
                                         ("bottom", None), ("x1", -1), ("bottom", 10),
                                         ("x0", 1_000_001), ("upright", False), ("upright", 1)])
def test_invalid_box_and_orientation(field, value):
    words = row()
    words[0][field] = value
    assert_unreadable(oracle.parse_legend_pages([page(words)]), "INVALID_ROW_GEOMETRY")


def test_crossline_reversed_order_and_centerline_boundary():
    words = row()
    words[4].update(top=16, bottom=26)
    assert_unreadable(oracle.parse_legend_pages([page(words)]), "INVALID_ROW_GEOMETRY")
    words[4].update(top=15, bottom=25)
    assert oracle.parse_legend_pages([page(words)]).status == "READABLE"
    words[4].update(x0=90, x1=110)
    assert_unreadable(oracle.parse_legend_pages([page(words)]), "INVALID_ROW_GEOMETRY")


def test_truncated_and_empty_layouts():
    assert_unreadable(oracle.parse_legend_pages([page(row()[:4])]), "TRUNCATED_ROW")
    assert_unreadable(oracle.parse_legend_pages([]), "EMPTY_LEGEND")
    assert_unreadable(oracle.parse_legend_pages([page([{"text": "Unrelated"}])]), "EMPTY_LEGEND")


@pytest.mark.parametrize("pages,code", [(None, "INVALID_PAGES"),
    ([{"page": True, "words": []}], "INVALID_PAGE"),
    ([{"page": 0, "words": []}], "INVALID_PAGE"),
    ([page([]), page([])], "INVALID_PAGE"),
    ([{"page": 1, "words": None}], "INVALID_WORDS"),
    ([page([{"text": b"synthetic"}])], "NONTEXT_WORD"),
    ([page([{"text": "a" * 257}])], "TEXT_LIMIT")])
def test_input_contract(pages, code):
    assert_unreadable(oracle.parse_legend_pages(pages), code)


def test_bounds_without_large_fixtures(monkeypatch):
    monkeypatch.setattr(oracle, "MAX_PAGES", 1)
    assert_unreadable(oracle.parse_legend_pages([page(row()), page(row(), 2)]), "PAGE_LIMIT")
    monkeypatch.setattr(oracle, "MAX_WORDS", 4)
    assert_unreadable(oracle.parse_legend_pages([page(row())]), "WORD_LIMIT")
    monkeypatch.setattr(oracle, "MAX_WORDS", 20)
    monkeypatch.setattr(oracle, "MAX_ROWS", 1)
    assert_unreadable(oracle.parse_legend_pages([page(row() + row("2.1"))]), "ROW_LIMIT")


PAYLOAD = b"synthetic PDF stand-in"
PIN = hashlib.sha256(PAYLOAD).hexdigest()


def bind_reader(monkeypatch, extracted=None, failure=None):
    streams = []
    events = []
    extract = Mock(return_value=row() if extracted is None else extracted, side_effect=failure)

    def source_open(*args):
        stream = io.BytesIO(PAYLOAD)
        streams.append(stream)
        return stream

    class Document:
        pages = [SimpleNamespace(extract_words=extract)]

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *args):
            events.append("exit")

    def pdf_open(snapshot):
        assert snapshot.read() == PAYLOAD
        snapshot.seek(0)
        streams.append(snapshot)
        return Document()

    opener = Mock(side_effect=pdf_open)
    monkeypatch.setattr("builtins.open", source_open)
    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=opener))
    return extract, opener, events, streams, Document


def test_reader_pins_parameters_provenance_and_cleanup(monkeypatch):
    extract, opener, events, streams, _ = bind_reader(monkeypatch)
    result = oracle.read_legend_pdf("synthetic.pdf", PIN.upper(), len(PAYLOAD))
    assert result.status == "READABLE"
    assert result.source == {"bytes": 22, "sha256": PIN}
    assert result.totals == {"modules": 7, "optimizers": 4, "inverters": 1, "strings": 1}
    extract.assert_called_once_with(x_tolerance=0.1, y_tolerance=0.1,
                                    use_text_flow=True, extra_attrs=["upright"])
    assert opener.call_count == 1 and events == ["enter", "exit"]
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("digest,size", [("0" * 64, len(PAYLOAD)), (PIN, len(PAYLOAD) + 1)])
def test_mismatch_never_calls_pdf_extractor(monkeypatch, digest, size):
    _, opener, events, streams, _ = bind_reader(monkeypatch)
    result = oracle.read_legend_pdf("synthetic.pdf", digest, size)
    assert_unreadable(result, "SOURCE_MISMATCH")
    assert result.source == {"bytes": 22, "sha256": PIN}
    assert not opener.called and events == [] and streams[0].closed


@pytest.mark.parametrize("digest,size", [("bad", 1), ("z" * 64, 1), (PIN, True),
                                       (PIN, 0), (PIN, oracle.MAX_BYTES + 1), (None, 1)])
def test_invalid_pins_precede_source_read(monkeypatch, digest, size):
    opener = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr("builtins.open", opener)
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", digest, size), "INVALID_SOURCE_PIN")
    assert not opener.called


@pytest.mark.parametrize("payload,code", [(b"synthetic", "INVALID_WORDS"),
    ([{"text": None}], "NONTEXT_WORD"), ([], "EMPTY_LEGEND")])
def test_reader_bad_extraction_closes_document(monkeypatch, payload, code):
    _, _, events, streams, _ = bind_reader(monkeypatch, extracted=payload)
    result = oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD))
    assert_unreadable(result, code)
    assert events == ["enter", "exit"] and all(s.closed for s in streams)


def test_reader_exception_is_sanitized_and_closes_document(monkeypatch):
    _, _, events, streams, _ = bind_reader(monkeypatch, failure=ValueError("synthetic private detail"))
    result = oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD))
    assert_unreadable(result, "PDF_EXTRACTION_FAILED")
    assert "synthetic" not in json.dumps(result.to_dict())
    assert events == ["enter", "exit"] and all(s.closed for s in streams)


def test_reader_read_and_import_failures(monkeypatch):
    monkeypatch.setattr("builtins.open", Mock(side_effect=OSError("synthetic detail")))
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD)), "SOURCE_READ_FAILED")
    bind_reader(monkeypatch)
    monkeypatch.setitem(sys.modules, "pdfplumber", None)
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD)), "PDF_IMPORT_FAILED")


def test_reader_bounds_close_resources(monkeypatch):
    _, _, events, streams, document = bind_reader(monkeypatch)
    monkeypatch.setattr(oracle, "MAX_PAGES", 0)
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD)), "PAGE_LIMIT")
    assert events == ["enter", "exit"] and all(s.closed for s in streams)
    monkeypatch.setattr(oracle, "MAX_PAGES", 20)
    monkeypatch.setattr(oracle, "MAX_WORDS", 4)
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", PIN, len(PAYLOAD)), "WORD_LIMIT")
    monkeypatch.setattr(oracle, "MAX_BYTES", 10)
    assert_unreadable(oracle.read_legend_pdf("synthetic.pdf", PIN, 10), "BYTE_LIMIT")


def test_unrelated_dotted_diagram_labels_are_not_legend_candidates():
    diagram = row(marker="diagram")
    diagram[3].update(top=50, bottom=60)
    words = diagram + row("2.1")
    original = deepcopy(words)
    result = oracle.parse_legend_pages([page(words)])
    assert result.status == "READABLE"
    assert result.totals == {"modules": 7, "optimizers": 4, "inverters": 1, "strings": 1}
    assert words == original


def test_aligned_numeric_diagram_sequence_is_not_a_model_row():
    diagram = row(marker="9", model="8")
    result = oracle.parse_legend_pages([page(diagram + row("2.1"))])
    assert result.status == "READABLE"
    assert result.totals == {"modules": 7, "optimizers": 4, "inverters": 1, "strings": 1}
