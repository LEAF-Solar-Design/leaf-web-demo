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
  {"schema": VERDICT_SCHEMA, "refusal": "REPORT_PDF_UNSUPPORTED"}.
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
# The import source cap (solar_import_sources.MAX_IMPORT_PDF_BYTES); a stored source never exceeds it.
MAX_INPUT_BYTES = 16_777_216
# The 1 MB C14 fixture encodes to 422,852 bytes; 64 MiB leaves two orders of magnitude.
MAX_OUTPUT_BYTES = 67_108_864


def _refusal_bytes():
    return json.dumps({"schema": VERDICT_SCHEMA, "refusal": REFUSAL},
                      separators=(",", ":")).encode("utf-8")


def verdict_bytes(data):
    """The encoded verdict for one PDF. Never raises for a parser failure: every one is the refusal."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_INPUT_BYTES:
        return _refusal_bytes()
    try:
        import solar_solaredge_pdf
        import solar_solaredge_parse
        matrices = solar_solaredge_parse.parse_primitives(
            solar_solaredge_pdf.extract_primitives(bytes(data)), PARSE_BASE_NAME)[1]
        payload = json.dumps({"schema": VERDICT_SCHEMA, "matrices": matrices},
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, LookupError, TypeError, ArithmeticError, RecursionError, MemoryError):
        return _refusal_bytes()
    if len(payload) > MAX_OUTPUT_BYTES:
        return _refusal_bytes()
    return payload


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


def decode_verdict(returncode, out):
    """The parent's reading of one finished child: the matrices, or None for any refusal.

    Anything but exit status 0 with exactly one well-formed verdict object is a refusal: a crash,
    a signal, an oversized or truncated stream, invalid UTF-8 or JSON, a wrong schema, extra keys.
    """
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
    if set(value) == {"schema", "matrices"} and type(value["matrices"]) is list:
        return value["matrices"]
    return None


def main():
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    sys.stdout.buffer.write(verdict_bytes(data))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
