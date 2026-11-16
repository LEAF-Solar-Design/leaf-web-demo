"""Synthetic label contracts only; these do not certify real PDF layouts."""

from decimal import Decimal, localcontext
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from benches.x05_oracle import parse_text, read_pdf
from benches import x05_oracle as oracle


REPORT = """Synthetic engineering report
Total Modules: 24
Total Optimizers: 12
Total Inverters: 1
Total Strings: 2
Total DC Power: 9600 W
String ID | Optimizers
A-1 | 6
A-2 | 6
"""


def codes(result):
    return {(issue.code, issue.field) for issue in result.uncertainties}


@pytest.mark.parametrize("payload", [b"synthetic bytes", ["synthetic"], {"text": "synthetic"}, [], {}])
def test_nontext_pdf_extraction_closes_context_and_has_no_totals(monkeypatch, payload):
    import sys

    events = []

    class Document:
        pages = [SimpleNamespace(extract_text=lambda: REPORT),
                 SimpleNamespace(extract_text=lambda: payload)]

        def __enter__(self):
            events.append("open")
            return self

        def __exit__(self, *args):
            events.append("close")

    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=lambda path: Document()))
    result = read_pdf("synthetic.pdf")
    assert result.status == "unreadable"
    assert ("pdf_extraction_nontext", "pdf") in codes(result)
    assert all(value is None for value in result.totals.values())
    assert result.strings == [] and events == ["open", "close"]


def test_conflicting_aliases_and_bom_values_are_order_independent():
    aliases = ["Total Modules: 24", "Panel Count: 25", "Modules: 24"]
    base = REPORT.replace("Total Modules: 24\n", "")
    for ordered in permutations(aliases):
        result = parse_text("\n".join(ordered) + "\n" + base)
        assert result.status == "unreadable" and result.totals["modules"] is None
        assert ("contradictory_totals", "modules") in codes(result)
    for labels in (["Module Quantity: 24", "Module Quantity: 25", "Module Quantity: 24"],
                   ["Module Power: 400 W", "Module Power: 401 W", "Module Power: 0.4 kW"]):
        field = "module_quantity" if "Quantity" in labels[0] else "module_power_kw"
        other = "Module Power: 400 W" if field == "module_quantity" else "Module Quantity: 24"
        for ordered in permutations(labels):
            result = parse_text(REPORT, "\n".join([*ordered, other]))
            assert result.status == "unreadable" and result.bom[field] is None
            assert ("contradictory_totals", field) in codes(result)


def test_large_invalid_tables_have_linear_diagnostics_and_bounded_output(monkeypatch):
    calls = 0
    original = oracle._number

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(oracle, "_number", counted)
    for count in (500, 2000):
        calls = 0
        text = REPORT.split("A-1 | 6")[0] + "\n".join(f"bad-{i} | invalid" for i in range(count))
        result = parse_text(text)
        assert result.status == "unreadable"
        assert ("invalid_number", "string_table") in codes(result)
        assert result.table_string_count is None and result.table_optimizer_count is None
        assert len(result.strings) <= oracle.MAX_DIAGNOSTIC_ROWS
        assert len(result.uncertainties) <= 3
        assert calls <= count + 10
        assert len(result.provenance["string_table"]) <= oracle.MAX_SNIPPETS_PER_FIELD
        assert all(len(snippet) <= oracle.MAX_SNIPPET for snippet in result.provenance["string_table"])


@pytest.mark.parametrize("separator", ["\f", "\n\f\n", "\n\n", "\n\f\nString ID | Optimizers\n\n"])
def test_supported_table_continuations_preserve_each_count(separator):
    result = parse_text(REPORT.replace("A-1 | 6\nA-2", "A-1 | 6" + separator + "A-2"))
    assert result.status == "readable"
    assert [(row.identifier, row.optimizers) for row in result.strings] == [
        ("A-1", Decimal(6)), ("A-2", Decimal(6))]
    assert result.table_optimizer_count == Decimal(12)


@pytest.mark.parametrize("tail", ["A-2", "A-2 |", "A-2 | 6 |", "A-2 6"])
def test_truncated_continuation_is_named_uncertainty(tail):
    result = parse_text(REPORT.replace("A-2 | 6", "\f\nString ID | Optimizers\n\n" + tail))
    assert result.status == "unreadable"
    assert result.table_string_count is None and result.table_optimizer_count is None
    assert any(issue.field == "string_table" for issue in result.uncertainties)


@pytest.mark.parametrize("rounding", ["ROUND_DOWN", "ROUND_UP", "ROUND_FLOOR", "ROUND_CEILING"])
def test_decimal_context_cannot_change_exact_units_or_bom(rounding):
    from decimal import getcontext

    with localcontext() as caller:
        caller.prec = 2
        caller.Emin, caller.Emax = -1, 1
        caller.rounding = rounding
        for signal in caller.traps:
            caller.traps[signal] = True
        snapshot = caller.copy()
        matched = parse_text(REPORT, "Module Quantity: 24\nModule Power: 400 W")
        assert matched.status == "readable"
        assert matched.totals["power_kw"] == Decimal("9.6")
        assert matched.table_optimizer_count == Decimal(12)
        conflict = parse_text(REPORT, "Module Quantity: 24\nModule Power: 401 W")
        assert ("bom_conflict", "power_kw") in codes(conflict)
        assert conflict.totals["power_kw"] is None
        kw = parse_text(REPORT.replace("9600 W", "9.600 kW"))
        assert kw.totals["power_kw"] == Decimal("9.600")
        current = getcontext()
        assert (current.prec, current.Emin, current.Emax, current.rounding, current.traps, current.flags) == (
            snapshot.prec, snapshot.Emin, snapshot.Emax, snapshot.rounding, snapshot.traps, snapshot.flags)


def test_summary_and_string_table_are_independent_explicit_evidence():
    result = parse_text(REPORT)
    assert result.status == "readable"
    assert result.totals == {"modules": Decimal(24), "optimizers": Decimal(12),
                             "inverters": Decimal(1), "strings": Decimal(2),
                             "power_kw": Decimal("9.6")}
    assert result.table_string_count == 2
    assert result.table_optimizer_count == Decimal(12)
    assert [(row.identifier, row.optimizers) for row in result.strings] == [
        ("A-1", Decimal(6)), ("A-2", Decimal(6))]
    assert isinstance(result.strings[0].optimizers, Decimal)
    assert "Total DC Power: 9600 W" in result.provenance["power_kw"]


def test_aliases_kw_and_labeled_strings_across_pages():
    text = """Panel Count = 24
Number of Optimizers: 12
Inverters: 1
String Count: 2
System Size: 9.600 kW
String INV1/S1: 6 optimizers
\f
String INV1/S2: 6 optimizers
"""
    result = parse_text(text)
    assert result.status == "readable"
    assert result.totals["power_kw"] == Decimal("9.600")
    assert result.table_string_count == 2


@pytest.mark.parametrize("text", ["", "Unrecognized layout", "Total Modules: 24"])
def test_missing_information_is_unreadable_without_zeros(text):
    result = parse_text(text)
    assert result.status == "unreadable"
    assert result.totals["power_kw"] is None
    assert result.table_optimizer_count is None
    assert result.table_string_count is None
    assert ("missing_total", "power_kw") in codes(result)


@pytest.mark.parametrize("raw", ["-1", "0", "1.5", "NaN", "Infinity", "1,000", "1e2", "unknown"])
def test_invalid_summary_count_cannot_be_repaired_by_valid_evidence(raw):
    result = parse_text(REPORT + f"Total Modules: {raw}\nTotal Modules: 24")
    assert result.status == "unreadable"
    assert result.totals["modules"] is None
    assert ("invalid_number", "modules") in codes(result)


@pytest.mark.parametrize("raw", ["9600", "NaN kW", "-9.6 kW", "9,6 kW", "9.6 MW"])
def test_power_requires_valid_number_and_recognized_unit(raw):
    result = parse_text(REPORT.replace("9600 W", raw))
    assert result.totals["power_kw"] is None
    assert ("invalid_number", "power_kw") in codes(result)


def test_conflicting_alias_totals_are_not_last_value_wins():
    for extra in ("Total Panels: 25", "Total Panels: 25\nTotal Modules: 24"):
        result = parse_text(REPORT + extra)
        assert result.totals["modules"] is None
        assert ("contradictory_totals", "modules") in codes(result)
    assert parse_text(REPORT + "Total Panels: 24").status == "readable"


@pytest.mark.parametrize("old,new,field", [("Total Strings: 2", "Total Strings: 3", "strings"),
                                         ("Total Optimizers: 12", "Total Optimizers: 13", "optimizers")])
def test_table_disagreement_invalidates_explicit_total(old, new, field):
    result = parse_text(REPORT.replace(old, new))
    assert result.status == "unreadable"
    assert result.totals[field] is None
    assert ("table_total_conflict", field) in codes(result)


def test_duplicate_ids_do_not_become_table_counts():
    result = parse_text(REPORT.replace("A-2", "A-1"))
    assert ("duplicate_identifier", "string_table") in codes(result)
    assert result.table_string_count is None
    assert result.table_optimizer_count is None
    assert len(result.strings) == 2  # Preserve both observations, never deduplicate silently.


@pytest.mark.parametrize("row", ["A-2 | 1.5", "A-2 | NaN", "A-2 | -6", "bad! | 6", "A-2 | 6 | extra"])
def test_invalid_table_rows_do_not_fabricate_counts(row):
    result = parse_text(REPORT.replace("A-2 | 6", row))
    assert result.status == "unreadable"
    assert result.table_optimizer_count is None
    assert result.table_string_count is None


def test_rows_never_fill_missing_summary():
    result = parse_text(REPORT.replace("Total Optimizers: 12\n", ""))
    assert result.totals["optimizers"] is None
    assert result.table_optimizer_count == Decimal(12)
    assert result.status == "unreadable"


def test_bom_is_separate_and_does_not_fill_missing_pdf_power():
    bom = "Module Power: 400 W\nModule Quantity: 24"
    result = parse_text(REPORT, bom)
    assert result.status == "readable"
    assert result.bom == {"module_power_kw": Decimal("0.4"), "module_quantity": Decimal(24)}
    missing = parse_text(REPORT.replace("Total DC Power: 9600 W\n", ""), bom)
    assert missing.totals["power_kw"] is None
    assert missing.status == "unreadable"


def test_bom_cannot_override_contradictory_pdf_evidence():
    result = parse_text(REPORT + "System Power: 10 kW", "Module Power: 400 W\nModule Quantity: 24")
    assert result.totals["power_kw"] is None
    assert ("contradictory_totals", "power_kw") in codes(result)
    conflict = parse_text(REPORT, "Module Power: 500 W\nModule Quantity: 25")
    assert conflict.totals["modules"] is None
    assert conflict.totals["power_kw"] is None
    assert ("bom_conflict", "modules") in codes(conflict)
    assert ("bom_conflict", "power_kw") in codes(conflict)


def test_invalid_and_conflicting_bom_evidence():
    result = parse_text(REPORT, "Module Power: NaN W\nModule Quantity: 24\nModule Quantity: 25")
    assert result.bom == {"module_power_kw": None, "module_quantity": None}
    assert ("invalid_number", "module_power_kw") in codes(result)
    assert ("contradictory_totals", "module_quantity") in codes(result)


def test_provenance_is_bounded():
    result = parse_text(REPORT + "\n".join("Total Modules:" + " " * size + "24" for size in range(250, 270)))
    assert len(result.provenance["modules"]) <= 5
    assert all(len(snippet) <= 200 for snippets in result.provenance.values() for snippet in snippets)


def test_decimal_arithmetic_does_not_depend_on_callers_precision():
    with localcontext() as context:
        context.prec = 2
        result = parse_text(REPORT.replace("9600 W", "9601 W"))
    assert result.totals["power_kw"] == Decimal("9.601")


def test_read_pdf_uses_runtime_extractor_without_real_files():
    class Document:
        pages = [SimpleNamespace(extract_text=lambda: REPORT)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch.dict("sys.modules", {"pdfplumber": SimpleNamespace(open=lambda path: Document())}):
        assert read_pdf("synthetic-unused.pdf").status == "readable"
        Document.pages.append(SimpleNamespace(extract_text=lambda: None))
        assert ("empty_pdf_page", "pdf") in codes(read_pdf("synthetic-unused.pdf"))


def test_read_pdf_failure_is_named_without_fabricated_values():
    def fail(path):
        raise ValueError("synthetic extraction failure")

    with patch.dict("sys.modules", {"pdfplumber": SimpleNamespace(open=fail)}):
        result = read_pdf("synthetic-unused.pdf")
    assert ("pdf_extraction_failed", "pdf") in codes(result)
    assert all(value is None for value in result.totals.values())


@pytest.mark.parametrize("repeats", [500, 1500])
def test_repeated_summaries_have_linear_numeric_bookkeeping(monkeypatch, repeats):
    operations = {"equal": 0, "hash": 0}
    original = oracle._number

    class CountedDecimal(Decimal):
        def __eq__(self, other):
            operations["equal"] += 1
            return super().__eq__(other)

        def __ne__(self, other):
            operations["equal"] += 1
            return super().__ne__(other)

        def __hash__(self):
            operations["hash"] += 1
            return super().__hash__()

    def counted(raw, **kwargs):
        value = original(raw, **kwargs)
        return CountedDecimal(value) if value is not None else None

    monkeypatch.setattr(oracle, "_number", counted)
    generated = REPORT + "\n" + "\n".join(
        "Total Modules:" + " " * (1 + index % 12) + "24" for index in range(repeats))
    result = parse_text(generated)
    assert result.status == "readable" and result.totals["modules"] == Decimal(24)
    assert sum(operations.values()) < repeats * 20
    assert len(result.provenance["modules"]) <= oracle.MAX_SNIPPETS_PER_FIELD
    assert all(len(s) <= oracle.MAX_SNIPPET for s in result.provenance["modules"])
    conflict = parse_text(generated + "\nTotal Panels: 25\nTotal Modules: 24")
    assert conflict.totals["modules"] is None
    assert ("contradictory_totals", "modules") in codes(conflict)
    invalid = parse_text(generated + "\nTotal Modules: NaN\nTotal Modules: 24")
    assert invalid.totals["modules"] is None
    assert ("invalid_number", "modules") in codes(invalid)


@pytest.mark.parametrize("mode", ["blank-middle", "later-error", "empty"])
def test_mocked_multi_page_failures_remain_named(mode):
    visited = []

    def extract(index, text):
        def read():
            visited.append(index)
            if text is RuntimeError:
                raise RuntimeError("synthetic later-page failure")
            return text
        return SimpleNamespace(extract_text=read)

    class Document:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    document = Document()
    document.pages = ([] if mode == "empty" else
                      [extract(0, REPORT), extract(1, "" if mode == "blank-middle" else RuntimeError),
                       extract(2, "Synthetic trailing page")])
    with patch.dict("sys.modules", {"pdfplumber": SimpleNamespace(open=lambda path: document)}):
        result = read_pdf("synthetic-unused.pdf")
    assert result.status == "unreadable"
    expected = "pdf_extraction_failed" if mode == "later-error" else "empty_pdf_page"
    assert (expected, "pdf") in codes(result)
    if mode in ("empty", "later-error"):
        assert all(value is None for value in result.totals.values())
        assert result.table_string_count is None and result.table_optimizer_count is None
    if mode == "later-error":
        assert visited == [0, 1]
