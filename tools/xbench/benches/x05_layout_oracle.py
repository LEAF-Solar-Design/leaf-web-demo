"""Independent, source-bound five-word legend reader.

READABLE certifies only the supported legend. Power and grid fields require
separately attributed references; this result is not a full X05 grade.
Word indices are zero-based; page numbers are one-based. No OCR fallback.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import io
import math
import re


MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 20
MAX_WORDS = 1_000_000
MAX_TEXT = 256
MAX_ROWS = 20_000
MAX_LOCATIONS = 8
MAX_COORD = 1_000_000
_POSITIVE = re.compile(r"[1-9][0-9]{0,8}\Z", re.ASCII)
_LABEL = re.compile(r"([1-9][0-9]{0,8})\.([1-9][0-9]{0,8})\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z", re.ASCII)
_MARKERS = {"x", "X", "\u00d7"}


@dataclass
class Location:
    page: int
    word_indices: list[int]
    bboxes: list[list[float]]


@dataclass
class LegendString:
    identifier: str
    optimizers: int
    model: str
    modules: int
    provenance: list[Location] = field(default_factory=list)


@dataclass
class LegendResult:
    status: str = "UNREADABLE"
    strings: list[LegendString] = field(default_factory=list)
    totals: dict = field(default_factory=lambda: dict.fromkeys(
        ("modules", "optimizers", "inverters", "strings")))
    uncertainties: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def _failure(code, source=None):
    return LegendResult(uncertainties=[code], source=dict(source or {}))


def _boxes(words):
    boxes = []
    for word in words:
        values = [word.get(key) for key in ("x0", "top", "x1", "bottom")]
        if any(type(v) not in (int, float) or not -MAX_COORD <= v <= MAX_COORD
               or not math.isfinite(v) for v in values):
            return None
        x0, top, x1, bottom = values
        if x1 < x0 or bottom <= top or word.get("upright") is not True:
            return None
        boxes.append(values)
    if any(a[0] >= b[0] for a, b in zip(boxes, boxes[1:])):
        return None
    centers = [(b[1] + b[3]) / 2 for b in boxes]
    if max(centers) - min(centers) > max(b[3] - b[1] for b in boxes) / 2:
        return None
    return boxes


def parse_legend_pages(pages):
    """Parse bounded content-order words without modifying caller data.

    Integers use canonical ASCII decimal spelling (1..999999999). Models
    are 1..64 ASCII alphanumerics starting with a letter, case-sensitive.
    Diagnostics contain fixed codes, never extracted text. Duplicate locations
    are capped; exceeding that cap does not change the deduplicated counts.
    """
    if not isinstance(pages, list):
        return _failure("INVALID_PAGES")
    if len(pages) > MAX_PAGES:
        return _failure("PAGE_LIMIT")
    total_words = 0
    seen_pages = set()
    for page in pages:
        if (not isinstance(page, dict) or type(page.get("page")) is not int
                or not 1 <= page["page"] <= MAX_PAGES
                or page["page"] in seen_pages):
            return _failure("INVALID_PAGE")
        seen_pages.add(page["page"])
        words = page.get("words")
        if not isinstance(words, list):
            return _failure("INVALID_WORDS")
        total_words += len(words)
        if total_words > MAX_WORDS:
            return _failure("WORD_LIMIT")
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                return _failure("NONTEXT_WORD")
            if len(word["text"]) > MAX_TEXT:
                return _failure("TEXT_LIMIT")

    result = LegendResult()
    rows = {}
    labels = {}
    issues = set()
    for page in pages:
        words = page["words"]
        index = 0
        while index < len(words):
            text = words[index]["text"]
            label = _LABEL.fullmatch(text)
            window = words[index:index + 5]
            tokens = [w["text"] for w in window]
            # Dotted tokens followed by a multiplication marker are candidates
            # even when their identifier or either count is malformed.
            candidate = (len(tokens) >= 3 and tokens[2] in _MARKERS and "." in text)
            candidate = candidate or (label is not None and len(tokens) >= 2
                                      and _POSITIVE.fullmatch(tokens[1]) is not None
                                      and len(window) == 5
                                      and (_MODEL.fullmatch(tokens[3]) is not None
                                           or re.fullmatch(r"[0-9]+", tokens[2]) is None)
                                      and _boxes(window) is not None)
            if not candidate:
                index += 1
                continue
            if len(tokens) != 5:
                issues.add("TRUNCATED_ROW")
                index += 1
                continue
            if not label:
                issues.add("INVALID_IDENTIFIER")
            elif not _POSITIVE.fullmatch(tokens[1]) or not _POSITIVE.fullmatch(tokens[4]):
                issues.add("INVALID_COUNT")
            elif tokens[2] not in _MARKERS or not _MODEL.fullmatch(tokens[3]):
                issues.add("INVALID_ROW_GRAMMAR")
            else:
                boxes = _boxes(window)
                if boxes is None:
                    issues.add("INVALID_ROW_GEOMETRY")
                else:
                    location = Location(page["page"], list(range(index, index + 5)), boxes)
                    values = (int(tokens[1]), tokens[3], int(tokens[4]))
                    key = (text, *values)
                    previous = rows.get(key)
                    if text in labels and labels[text] != values:
                        issues.add("CONFLICTING_DUPLICATE")
                    if previous is not None:
                        if len(previous.provenance) < MAX_LOCATIONS:
                            previous.provenance.append(location)
                    elif len(rows) >= MAX_ROWS:
                        issues.add("ROW_LIMIT")
                    else:
                        labels.setdefault(text, values)
                        rows[key] = LegendString(text, *values, provenance=[location])
                    index += 5
                    continue
            index += 1
    result.strings = sorted(rows.values(), key=lambda r: (
        *map(int, r.identifier.split(".")), r.optimizers, r.model, r.modules))
    if not rows:
        issues.add("EMPTY_LEGEND")
    result.uncertainties = sorted(issues)
    if not issues:
        result.status = "READABLE"
        result.totals = {
            "modules": sum(row.modules for row in result.strings),
            "optimizers": sum(row.optimizers for row in result.strings),
            "inverters": len({row.identifier.split(".")[0] for row in result.strings}),
            "strings": len(rows),
        }
    return result


def read_legend_pdf(path, expected_sha256, expected_bytes):
    """Verify a bounded byte snapshot before importing/executing pdfplumber.

    The same verified snapshot is extracted, avoiding a path-reopen race.
    Source provenance contains actual bytes/hash only, never a source path.
    """
    if (type(expected_bytes) is not int or not 1 <= expected_bytes <= MAX_BYTES
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None):
        return _failure("INVALID_SOURCE_PIN")
    source = {}
    snapshot = io.BytesIO()
    try:
        digest = hashlib.sha256()
        count = 0
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(min(65536, MAX_BYTES + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_BYTES:
                    snapshot.close()
                    return _failure("BYTE_LIMIT")
                digest.update(chunk)
                snapshot.write(chunk)
        source = {"bytes": count, "sha256": digest.hexdigest()}
        if count != expected_bytes or source["sha256"] != expected_sha256.lower():
            snapshot.close()
            return _failure("SOURCE_MISMATCH", source)
    except Exception:
        snapshot.close()
        return _failure("SOURCE_READ_FAILED")
    try:
        import pdfplumber
    except Exception:
        snapshot.close()
        return _failure("PDF_IMPORT_FAILED", source)
    try:
        snapshot.seek(0)
        with snapshot, pdfplumber.open(snapshot) as document:
            if len(document.pages) > MAX_PAGES:
                return _failure("PAGE_LIMIT", source)
            pages = []
            count = 0
            for number, page in enumerate(document.pages, 1):
                words = page.extract_words(x_tolerance=0.1, y_tolerance=0.1,
                                           use_text_flow=True, extra_attrs=["upright"])
                if not isinstance(words, list):
                    return _failure("INVALID_WORDS", source)
                count += len(words)
                if count > MAX_WORDS:
                    return _failure("WORD_LIMIT", source)
                pages.append({"page": number, "words": words})
            result = parse_legend_pages(pages)
            result.source = source
            return result
    except Exception:
        return _failure("PDF_EXTRACTION_FAILED", source)
