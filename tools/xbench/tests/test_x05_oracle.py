"""Synthetic label contracts only; these do not certify real PDF layouts."""

from decimal import Decimal, localcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from benches.x05_oracle import parse_text, read_pdf


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
