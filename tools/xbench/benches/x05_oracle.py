"""Independent, deliberately strict text oracle; no product parser imports.

Recognized whole-line summaries (case insensitive), separated by ':' or '=':
Total Modules/Panels, Module/Panel Count, Number of Modules/Panels;
Total Optimizers, Optimizer Count, Number of Optimizers; the analogous
Inverters and Strings labels; Total Power, Total DC Power, DC Power, System
Power, System Size (power requires W or kW). Plural nouns alone also work.
Counts are positive integers; decimal power uses a dot, with no grouping.

String rows are either 'String <id>: <count> optimizers' or a two-column
pipe table headed 'String ID | Optimizers'. IDs contain letters, digits,
underscores, dots, slashes or hyphens. Blank/unrecognized lines end a table.
No other PDF layouts, implicit quantities, OCR or product output are supported.
Optional BOM lines recognize Module Power (W/kW) and Module Quantity; BOM
values are returned separately and never fill or replace PDF totals.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
import re


MAX_SNIPPET = 200
MAX_SNIPPETS_PER_FIELD = 5
COUNT_FIELDS = ("modules", "optimizers", "inverters", "strings")
REQUIRED_FIELDS = (*COUNT_FIELDS, "power_kw")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*\Z")
_LABELS = {}
for _singular, _plural, _field in (
    ("module", "modules", "modules"),
    ("panel", "panels", "modules"),
    ("optimizer", "optimizers", "optimizers"),
    ("inverter", "inverters", "inverters"),
    ("string", "strings", "strings"),
):
    for _label in (_plural, "total " + _plural, _singular + " count",
                   "number of " + _plural):
        _LABELS[_label] = _field
for _label in ("total power", "total dc power", "dc power", "system power",
               "system size"):
    _LABELS[_label] = "power_kw"


@dataclass(frozen=True)
class Uncertainty:
    code: str
    field: str
    detail: str


@dataclass(frozen=True)
class StringRow:
    identifier: str
    optimizers: Decimal | None


@dataclass
class OracleResult:
    totals: dict = field(default_factory=lambda: dict.fromkeys(REQUIRED_FIELDS))
    strings: list[StringRow] = field(default_factory=list)
    table_string_count: int | None = None
    table_optimizer_count: Decimal | None = None
    bom: dict = field(default_factory=lambda: dict.fromkeys(
        ("module_power_kw", "module_quantity")))
    uncertainties: list[Uncertainty] = field(default_factory=list)
    provenance: dict[str, list[str]] = field(default_factory=dict)

    @property
    def status(self):
        return "unreadable" if self.uncertainties else "readable"

    def issue(self, code, name, detail):
        issue = Uncertainty(code, name, detail)
        if issue not in self.uncertainties:
            self.uncertainties.append(issue)

    def record(self, name, line):
        snippets = self.provenance.setdefault(name, [])
        snippet = line[:MAX_SNIPPET]
        if snippet not in snippets and len(snippets) < MAX_SNIPPETS_PER_FIELD:
            snippets.append(snippet)


def _number(raw, *, power=False):
    """Return Decimal without float conversion, rejecting ambiguous notation."""
    unit = None
    if power:
        match = re.fullmatch(r"(.+?)\s*(kW|W)", raw, re.IGNORECASE)
        if not match:
            return None
        raw, unit = match.groups()
    raw = raw.strip()
    if len(raw) > 40 or not _NUMBER.fullmatch(raw):
        return None
    try:
        value = Decimal(raw)
        if not value.is_finite() or value <= 0:
            return None
        if not power and value != value.to_integral_value():
            return None
        with localcontext() as context:
            context.prec = 100
            return value / Decimal(1000) if unit and unit.lower() == "w" else value
    except InvalidOperation:
        return None


def _evidence(result, target, seen, name, raw, line, *, power=False):
    result.record(name, line)
    value = _number(raw, power=power)
    state = seen.setdefault(name, {"first": None, "invalid": False, "conflict": False})
    if value is None:
        state["invalid"] = True
        result.issue("invalid_number", name, "Expected a positive count or labeled power.")
    elif state["first"] is None:
        state["first"] = value
    elif value != state["first"]:
        state["conflict"] = True
    if state["conflict"]:
        result.issue("contradictory_totals", name, "Explicit values disagree.")
    target[name] = state["first"] if not state["invalid"] and not state["conflict"] else None


def _parse_bom(text, result):
    seen = {}
    labels = {"module power": "module_power_kw", "module quantity": "module_quantity"}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([^:=]+?)\s*[:=]\s*(.*?)\s*", line)
        if match and match[1].strip().lower() in labels:
            name = labels[match[1].strip().lower()]
            _evidence(result, result.bom, seen, name, match[2], line,
                      power=name == "module_power_kw")
    for name, value in result.bom.items():
        if value is None and name not in seen:
            result.issue("missing_bom", name, "No recognized BOM label.")
    quantity = result.bom["module_quantity"]
    module_power = result.bom["module_power_kw"]
    if quantity is not None and result.totals["modules"] is not None:
        if quantity != result.totals["modules"]:
            result.issue("bom_conflict", "modules", "BOM quantity disagrees with PDF.")
            result.totals["modules"] = None
    if quantity is not None and module_power is not None:
        with localcontext() as context:
            context.prec = 100
            bom_power = quantity * module_power
        if result.totals["power_kw"] is not None and bom_power != result.totals["power_kw"]:
            result.issue("bom_conflict", "power_kw", "BOM power disagrees with PDF.")
            result.totals["power_kw"] = None


def parse_text(text: str, bom_text: str | None = None) -> OracleResult:
    """Parse extracted report pages (form feeds allowed); never infer a total.

    Every required summary and a nonempty valid string table are needed for
    'readable'. Partial evidence remains available with named uncertainties.
    Provenance is bounded per field; it is evidence, not a full source copy.
    """
    result = OracleResult()
    seen = {}
    identifiers = set()
    table_mode = False
    table_bad = False
    for original in text.splitlines():
        line = original.strip()
        if re.fullmatch(r"String ID\s*\|\s*Optimizers", line, re.IGNORECASE):
            table_mode = True
            result.record("string_table", original)
            continue
        labeled = re.fullmatch(r"String\s+(\S+)\s*:\s*(.*?)\s+optimizers", line,
                               re.IGNORECASE)
        cells = line.split("|") if table_mode and "|" in line else None
        if labeled or cells is not None:
            if labeled:
                identifier, raw = labeled.groups()
            elif len(cells) == 2:
                identifier, raw = (cell.strip() for cell in cells)
            else:
                result.issue("invalid_string_row", "string_table", "Expected two columns.")
                table_bad = True
                continue
            result.record("string_table", original)
            value = _number(raw)
            if not _ID.fullmatch(identifier):
                result.issue("invalid_identifier", "string_table", "Invalid string identifier.")
                table_bad = True
            if identifier in identifiers:
                result.issue("duplicate_identifier", "string_table", "String identifier repeats.")
                table_bad = True
            identifiers.add(identifier)
            if value is None:
                result.issue("invalid_number", "string_table", "Invalid optimizer count.")
                table_bad = True
            result.strings.append(StringRow(identifier, value))
            continue
        if table_mode and line:
            # A non-pipe row is not silently counted as a valid table row.
            if re.match(r"\S+\s+\S+", line) and not re.search(r"[:=]", line):
                result.issue("unrecognized_table_row", "string_table", "Unsupported table row.")
                table_bad = True
        table_mode = False
        match = re.fullmatch(r"([^:=]+?)\s*[:=]\s*(.*?)", line)
        if match:
            name = _LABELS.get(match[1].strip().lower())
            if name:
                _evidence(result, result.totals, seen, name, match[2], original,
                          power=name == "power_kw")
    for name in REQUIRED_FIELDS:
        if name not in seen:
            result.issue("missing_total", name, "No recognized explicit PDF total.")
    if not result.strings:
        result.issue("missing_string_table", "string_table", "No recognized string rows.")
    elif not table_bad:
        result.table_string_count = len(result.strings)
        with localcontext() as context:
            context.prec = 100
            result.table_optimizer_count = sum((row.optimizers for row in result.strings), Decimal(0))
        for name, observed in (("strings", result.table_string_count),
                               ("optimizers", result.table_optimizer_count)):
            if result.totals[name] is not None and result.totals[name] != observed:
                result.issue("table_total_conflict", name, "PDF summary disagrees with string rows.")
                result.totals[name] = None
    if bom_text is not None:
        _parse_bom(bom_text, result)
    return result


def read_pdf(path, bom_text: str | None = None) -> OracleResult:
    """Extract text using runtime-only pdfplumber; no OCR/layout claims.

    Extraction failures and pages with no text make the result unreadable,
    even if other pages happen to contain a complete set of labels.
    """
    try:
        import pdfplumber
    except ImportError:
        result = parse_text("", bom_text)
        result.issue("pdf_dependency_unavailable", "pdf", "pdfplumber is required at runtime.")
        return result
    try:
        with pdfplumber.open(path) as document:
            pages = [page.extract_text() or "" for page in document.pages]
    except Exception:
        result = parse_text("", bom_text)
        result.issue("pdf_extraction_failed", "pdf", "PDF could not be read.")
        return result
    result = parse_text("\n\f\n".join(pages), bom_text)
    if not pages or any(not page.strip() for page in pages):
        result.issue("empty_pdf_page", "pdf", "At least one page has no extractable text.")
    return result
