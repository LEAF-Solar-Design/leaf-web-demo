"""The SolarEdge PDF parse as a child process, so its wall time can be bounded by termination.

solar_solaredge_report.parse_source starts this file as a script with the running interpreter
(``sys.executable -B <this file>``), supplies the stored PDF bytes as a temporary-file stdin, and
reads one verdict from its stdout. The parent owns the deadline: when it expires the parent kills
this process and waits for it to exit, so a slow or hung parse never outlives its request and
never keeps a parse slot. A thread cannot give that guarantee, because a timed-out thread keeps
running.

Protocol (leaf.solar-solaredge-parse-verdict.v1), fails closed:
- stdin: the PDF bytes, at most MAX_INPUT_BYTES (the import source cap). More is refused.
- stdout: exactly one UTF-8 JSON object, then exit status 0. Either
  {"schema": VERDICT_SCHEMA, "matrices": [...]} (the parser's GenerateMatrixJsons output,
  unchanged: dicts, lists, strings, ints and null) or
  {"schema": VERDICT_SCHEMA, "refusal": "REPORT_PDF_UNSUPPORTED"} or the known
  resource-limit refusal REPORT_RESOURCE_LIMIT.
  An encoded verdict longer than MAX_OUTPUT_BYTES is replaced by the refusal.
- stderr carries nothing the parent reads.

Importing this module is cheap and has no side effects: the parser modules (and pdfminer) are
imported only inside verdict_bytes, so the parent can import the protocol constants and
decode_verdict without loading the parser. No clock, no network, no file I/O besides stdin and
stdout.
"""

import json
import sys

VERDICT_SCHEMA = "leaf.solar-solaredge-parse-verdict.v1"
PARSE_BASE_NAME = "solaredge"
REFUSAL = "REPORT_PDF_UNSUPPORTED"
RESOURCE_REFUSAL = "REPORT_RESOURCE_LIMIT"
# The import source cap (solar_import_sources.MAX_IMPORT_PDF_BYTES); a stored source never exceeds it.
MAX_INPUT_BYTES = 16_777_216
# The 1 MB C14 fixture encodes to 422,852 bytes; 64 MiB leaves two orders of magnitude.
MAX_OUTPUT_BYTES = 67_108_864


def _refusal_bytes(reason=REFUSAL):
    return json.dumps({"schema": VERDICT_SCHEMA, "refusal": reason},
                      separators=(",", ":")).encode("utf-8")


def verdict_bytes(data, *, legacy=False):
    """The encoded verdict for one PDF. Never raises for a parser failure: every one is the refusal."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_INPUT_BYTES:
        return _refusal_bytes()
    try:
        import solar_solaredge_pdf
        import solar_solaredge_parse
        prefix = ('{"schema":"' + VERDICT_SCHEMA + '","matrices":[').encode("utf-8")
        fragments = []
        size = len(prefix) + len(b"]}")
        if size > MAX_OUTPUT_BYTES:
            return _refusal_bytes()
        offset = 0
        optimizer_total = cell_total = 0
        for number, page in enumerate(solar_solaredge_pdf.iter_page_primitives(bytes(data)), 1):
            parsed = solar_solaredge_parse.SolarEdgeParse(page)
            name = PARSE_BASE_NAME if number == 1 else f"{PARSE_BASE_NAME}_page{number}"
            matrices = (parsed.generate_matrix_jsons(name) if legacy else parsed.identity_matrices(name))
            optimizer_total, cell_total = _validate_identities(matrices, optimizer_total, cell_total)
            highest = offset - 1
            for matrix in matrices:
                for optimizer in matrix.get("Optimizers", ()):
                    occurrence = int(optimizer["id"].removeprefix("pdf-optimizer-")) + offset
                    optimizer["id"] = f"pdf-optimizer-{occurrence}"
                    highest = max(highest, occurrence)
                data_part = json.dumps(matrix, separators=(",", ":")).encode("utf-8")
                size += len(data_part) + bool(fragments)
                if size > MAX_OUTPUT_BYTES:
                    return _refusal_bytes()
                fragments.append(data_part)
            offset = highest + 1
        return prefix + b",".join(fragments) + b"]}"
    except (ValueError, LookupError, TypeError, ArithmeticError, RecursionError, MemoryError) as error:
        if isinstance(error, (MemoryError, RecursionError)) or getattr(error, "code", None) in {
                "RESOURCE_LIMIT", "PDF_RESOURCE_LIMIT", "PDF_TOO_LARGE", "CONTENT_TOO_LARGE",
                "TOO_MANY_PAGES", "TOO_MANY_OPERATORS", "TOO_MANY_PATHS",
                "TOO_MANY_SEGMENTS", "TOO_MANY_LETTERS"}:
            return _refusal_bytes(RESOURCE_REFUSAL)
        return _refusal_bytes()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate verdict key")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("non-finite verdict number")


def _check_numbers(value):
    if type(value) is bool:
        raise ValueError("boolean verdict number")
    if type(value) is float:
        import math
        if not math.isfinite(value):
            raise ValueError("non-finite verdict number")
    elif type(value) is dict:
        for item in value.values():
            _check_numbers(item)
    elif type(value) is list:
        for item in value:
            _check_numbers(item)


def _decode_envelope(returncode, out):
    if (type(returncode) is not int or returncode != 0
            or not isinstance(out, (bytes, bytearray)) or not 0 < len(out) <= MAX_OUTPUT_BYTES):
        return None
    try:
        value = json.loads(bytes(out).decode("utf-8"), parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
        _check_numbers(value)
    except (ValueError, RecursionError):
        return None
    if type(value) is not dict or value.get("schema") != VERDICT_SCHEMA:
        return None
    return value


def decode_verdict(returncode, out):
    """Read exact successful matrix envelopes; all refusals and malformed envelopes return None."""
    value = _decode_envelope(returncode, out)
    if value is None:
        return None
    if set(value) == {"schema", "matrices"} and type(value["matrices"]) is list:
        try:
            _validate_identities(value["matrices"])
        except (ValueError, TypeError, KeyError, IndexError):
            return None
        return value["matrices"]
    return None


def _validate_identities(matrices, count=0, cells=0):
    """Bound the additive identity data without changing the historical matrix protocol."""
    import re
    for matrix in matrices:
        if type(matrix) is not dict or "Optimizers" not in matrix:
            continue
        items = matrix["Optimizers"]
        if type(items) is not list:
            raise ValueError("invalid optimizer list")
        count += len(items)
        if count > 100_000:
            raise ValueError("optimizer limit")
        for item in items:
            if (type(item) is not dict or set(item) != {"id", "vendor_label", "cells"}
                    or type(item["id"]) is not str
                    or re.fullmatch(r"pdf-optimizer-[0-9]{1,10}", item["id"]) is None
                    or (item["vendor_label"] is not None and
                        (type(item["vendor_label"]) is not str or
                         re.fullmatch(r"[0-9]{1,32}", item["vendor_label"]) is None))
                    or type(item["cells"]) is not list):
                raise ValueError("invalid optimizer")
            cells += len(item["cells"])
            if cells > 200_000:
                raise ValueError("optimizer panel limit")
            for pos in item["cells"]:
                if (type(pos) is not list or len(pos) != 2
                        or any(type(n) is not int or n < 0 for n in pos)):
                    raise ValueError("invalid optimizer cell")
                r, c = pos
                rows = matrix["Rows"]
                if r >= len(rows) or c >= len(rows[r]["Panels"]) or rows[r]["Panels"][c]["Code"] != 1:
                    raise ValueError("optimizer cell is not a panel")
    return count, cells


def decode_refusal(returncode, out):
    """Only a known structured cause from a successful child is trustworthy."""
    value = _decode_envelope(returncode, out)
    if (value is not None and set(value) == {"schema", "refusal"}
            and value["refusal"] in (REFUSAL, RESOURCE_REFUSAL)):
        return value["refusal"]
    return None


def main(*, legacy=False):
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    sys.stdout.buffer.write(verdict_bytes(data, legacy=legacy))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(legacy=sys.argv[1:] == ["--legacy-matrices"]))
