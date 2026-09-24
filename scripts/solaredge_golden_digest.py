#!/usr/bin/env python3
"""Digest the plugin's SolarEdge golden dump into docs/parity/evidence/solaredge/golden-digests.json.

The golden (15 MB, produced by Branch2025 tools/solaredge-golden from the plugin's own converter
on data/solaredge_1to1_demo.pdf) is not committed. Its per-section digests are, and the server
tests recompute the same digests from the Python port, so equality of digests is equality of
sections under ONE canonicalization, defined here and imported by the tests:

- The golden's path colours arrive as PdfPig strings ("RGB: (r, g, b)"); they become [r, g, b].
  Dictionary sections are compared by key (the dumper sorted the keys; order is not state).
- Every float is rounded to 9 decimals (``round(x, 9)``, -0 becomes 0); a rounded float with an
  integral value is written as that integer, so .NET's "72" and Python's 72.0 agree. Integers,
  strings, booleans and null are exact, and list order is exact.
- A section's digest is sha256 over ``json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=True, allow_nan=False)`` of its canonical value.

Usage:
  python scripts/solaredge_golden_digest.py <golden.json> [--pdf data/solaredge_1to1_demo.pdf]
      [--out docs/parity/evidence/solaredge/golden-digests.json] [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SCHEMA = "leaf.solaredge-golden-digests.v1"
BASE_NAME = "solaredge_1to1_demo"
S1_SECTIONS = ("lines", "curves", "letters", "paths")
S2_SECTIONS = ("panels", "optimizers", "grids", "matrices", "panel_layout", "inverter_strings",
               "all_string_infos", "inverter_pdf_colors")
SCALARS = ("page_width", "page_height", "legend_threshold", "has_position_keywords",
           "optimizer_ratio")
MAX_GOLDEN_BYTES = 256 * 1024 * 1024
_RGB = re.compile(r"^RGB: \(([^,]+), ([^,]+), ([^,]+)\)$")


def canonical(value):
    """The one canonical form both sides are digested in (see module doc)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in a digested section")
        rounded = round(value, 9)
        if rounded == 0:
            return 0
        if rounded.is_integer() and abs(rounded) < 2**53:
            return int(rounded)
        return rounded
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def digest(value):
    text = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _golden_color(text):
    if text is None:
        return None
    match = _RGB.match(text)
    if not match:
        raise ValueError(f"unexpected golden colour {text[:40]!r}")
    return [float(match.group(i)) for i in (1, 2, 3)]


def normalize_golden(golden):
    """Golden-only shape fixes: PdfPig colour strings to [r, g, b]."""
    out = dict(golden)
    out["paths"] = [dict(p, fill=_golden_color(p["fill"]), stroke=_golden_color(p["stroke"]))
                    for p in golden["paths"]]
    return out


def section_digests(sections):
    """{"sections": {name: {count, sha256}}, "scalars": {...}} for a golden-shaped dict."""
    result = {"sections": {}, "scalars": {}}
    for name in S1_SECTIONS + S2_SECTIONS:
        value = sections[name]
        result["sections"][name] = {"count": len(value), "sha256": digest(value)}
    for name in SCALARS:
        result["scalars"][name] = canonical(sections[name])
    return result


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(golden_path, pdf_path=None):
    path = Path(golden_path)
    if path.stat().st_size > MAX_GOLDEN_BYTES:
        raise SystemExit(f"golden larger than {MAX_GOLDEN_BYTES} bytes")
    golden = json.loads(path.read_text(encoding="utf-8"))
    if golden.get("schema") != "solaredge-golden-v1":
        raise SystemExit(f"unexpected golden schema {golden.get('schema')!r}")
    record = {
        "schema": SCHEMA,
        "golden_schema": golden["schema"],
        "pdfpig": golden.get("pdfpig"),
        "base_name": BASE_NAME,
        "golden_sha256": _sha256_file(path),
        "pdf_sha256": _sha256_file(pdf_path) if pdf_path else None,
        "canonicalization": ("sha256 of json.dumps(sort_keys, compact, ascii) after rounding floats "
                             "to 9 decimals, -0 to 0, integral floats to ints; golden path colour "
                             "strings parsed to [r,g,b]; list order exact"),
    }
    record.update(section_digests(normalize_golden(golden)))
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("golden")
    parser.add_argument("--pdf")
    parser.add_argument("--out", default="docs/parity/evidence/solaredge/golden-digests.json")
    parser.add_argument("--check", action="store_true",
                        help="compare against --out instead of writing it")
    args = parser.parse_args(argv)
    record = build(args.golden, args.pdf)
    text = json.dumps(record, indent=2, sort_keys=False) + "\n"
    out = Path(args.out)
    if args.check:
        same = out.exists() and out.read_text(encoding="utf-8") == text
        print("digests match" if same else "digests DIFFER")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
