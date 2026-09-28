"""Export fidelity fails closed, O(n) per drawing, bounded input and output.

Studio's no-change round-trip bar requires entity counts, real DXF handles,
layers and geometry to survive. Handle-less entities match in kind order;
numbers use absolute tolerance 1e-6. Blocks and source layers must survive.
Byte identity is recorded, never required. Style properties are excluded.
This is harness tooling with no flag and no production wiring.

Run: python engine/export_fidelity.py --adapter=acadrust
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sys
from pathlib import Path
from time import perf_counter

from corpus_harness import PROJECT_ROOT, EngineAdapter, _select_adapter
from dxf_intake import DxfParseError, parse_dxf_bytes

TRACKED_DIRS = ("engine/corpus", "vendor/acadrust-worker/fixtures", "web/e2e/fixtures", "web/public")
TRACKED_DRAWINGS = (
    "web/public/sample.dxf",
    "web/e2e/fixtures/block-fixture.dxf",
    "web/e2e/fixtures/distinctive-panel.dxf",
    "vendor/acadrust-worker/fixtures/one_line.dxf",
    "engine/corpus/01_closed_lwpolyline_single_layer.dxf",
    "engine/corpus/02_open_lwpolyline_two_layers.dxf",
    "engine/corpus/03_classic_polyline_vertex_seqend.dxf",
    "engine/corpus/04_empty_entities_section.dxf",
)
ENTITY_KINDS = ("polylines", "circles", "arcs", "texts", "dimensions", "inserts", "mleaders")
TRACKED_DRAWING_TIMEOUT_MS = 10000.0
COORD_TOLERANCE = 1e-6
MAX_LISTED_HANDLES = 20
MAX_DRAWING_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
TRACKED_RECEIPT_KEYS = (
    "drawing", "adapter", "ok", "timing_ms", "bytes_before", "bytes_after",
    "byte_identical", "entities_before", "entities_after", "source_handles",
    "handles_preserved", "handles_missing", "handles_changed", "unkeyed_before",
    "unkeyed_matched", "blocks_match", "block_records", "block_records_preserved",
    "block_records_missing", "block_records_changed", "layers_match",
    "source_untouched", "error",
)


def tracked_paths() -> list[Path]:
    return [PROJECT_ROOT / rel for rel in TRACKED_DRAWINGS]


def untracked_drawings() -> list[str]:
    tracked = set(TRACKED_DRAWINGS)
    return sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for directory in TRACKED_DIRS
        for path in (PROJECT_ROOT / directory).glob("*.dxf")
        if path.is_file() and path.relative_to(PROJECT_ROOT).as_posix() not in tracked
    )


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (
            math.isfinite(a) and math.isfinite(b)
            and abs(float(a) - float(b)) <= COORD_TOLERANCE
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _real_handle(entity) -> str | None:
    handle = str(entity.get("handle", "")).upper()
    return handle if re.fullmatch(r"[0-9A-F]+", handle) else None


def _without_properties(value):
    """Copy block trees without the writer's normalized style properties."""
    if isinstance(value, dict):
        return {k: _without_properties(v) for k, v in value.items() if k != "properties"}
    if isinstance(value, list):
        return [_without_properties(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_without_properties(v) for v in value)
    return value


def _ledger(intake) -> dict:
    counts = {}
    keyed = {}
    unkeyed = {}
    for kind in ENTITY_KINDS:
        entities = intake.get(kind) or []
        counts[kind] = len(entities)
        unkeyed[kind] = []
        for entity in entities:
            content = {key: value for key, value in entity.items() if key != "handle"}
            handle = _real_handle(entity)
            if handle is None:
                unkeyed[kind].append(content)
            else:
                if handle in keyed:
                    raise ValueError(f"duplicate handle {handle}")
                keyed[handle] = (kind, content)
    return {
        "counts": counts, "keyed": keyed, "unkeyed": unkeyed,
        "blocks": _without_properties(intake.get("blocks") or {}),
        "layers": list(intake["layers"]),
    }


def _block_ledger(raw: bytes) -> dict:
    """Read BLOCKS records without intake rounding or discarded child handles."""
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError(f"BLOCKS scan exceeds {MAX_OUTPUT_BYTES} bytes")
    lines = raw.splitlines()
    if len(lines) % 2:
        raise ValueError("odd number of DXF lines")
    keyed, unkeyed = {}, {}
    record = None
    block_name = ""
    in_blocks = False
    section_pending = False

    def keep_record():
        if record is None:
            return
        handle = record.get("handle")
        if handle is None:
            group = (record["block"], record["type"])
            unkeyed.setdefault(group, []).append(record)
        else:
            if handle in keyed:
                raise ValueError(f"duplicate handle {handle}")
            keyed[handle] = record

    pairs = iter(lines)
    for code_line, value_line in zip(pairs, pairs):
        code = int(code_line.strip())
        value = value_line.decode("latin-1").strip()
        if code == 0:
            keep_record()
            if record is not None and record["type"] == "ENDBLK":
                block_name = ""
            record = None
            if value == "SECTION":
                section_pending = True
                in_blocks = False
                block_name = ""
            elif value == "ENDSEC":
                in_blocks = False
                section_pending = False
                block_name = ""
            elif in_blocks:
                if value == "BLOCK":
                    block_name = ""
                record = {"type": value, "block": block_name, "numbers": {}}
        elif section_pending and code == 2:
            in_blocks = value == "BLOCKS"
            section_pending = False
        elif record is not None:
            if code == 2 and record["type"] == "BLOCK":
                block_name = value
                record["block"] = value
            elif code == 5 and "handle" not in record:
                record["handle"] = _real_handle({"handle": value})
            elif 10 <= code <= 59:
                record["numbers"].setdefault(code, []).append(float(value))
    keep_record()
    return {"keyed": keyed, "unkeyed": unkeyed}


def _same_block_record(source, output) -> bool:
    return (
        source["type"] == output["type"]
        and source["block"] == output["block"]
        and all(
            code in output["numbers"] and _same(values, output["numbers"][code])
            for code, values in source["numbers"].items()
        )
    )


def compare(before: bytes, after: bytes, *, source_name: str) -> dict:
    source = parse_dxf_bytes(before, source_name=source_name)
    output = parse_dxf_bytes(after, source_name=source_name)
    left, right = _ledger(source), _ledger(output)
    block_left, block_right = _block_ledger(before), _block_ledger(after)
    block_missing, block_changed = [], []
    block_preserved = 0
    for handle, record in block_left["keyed"].items():
        if handle not in block_right["keyed"]:
            block_missing.append(handle)
        elif not _same_block_record(record, block_right["keyed"][handle]):
            block_changed.append(handle)
        else:
            block_preserved += 1
    block_unkeyed_match = True
    for group, records in block_left["unkeyed"].items():
        candidates = block_right["unkeyed"].get(group, [])
        if len(candidates) < len(records) or not all(
            _same_block_record(a, b) for a, b in zip(records, candidates)
        ):
            block_unkeyed_match = False
    missing, changed = [], []
    preserved = 0
    for handle, entity in left["keyed"].items():
        if handle not in right["keyed"]:
            missing.append(handle)
        elif not _same(entity, right["keyed"][handle]):
            changed.append(handle)
        else:
            preserved += 1
    unkeyed_matched = 0
    for kind in ENTITY_KINDS:
        candidates = (
            {key: value for key, value in entity.items() if key != "handle"}
            for entity in (output.get(kind) or [])
            if _real_handle(entity) not in left["keyed"]
        )
        unkeyed_matched += sum(
            _same(a, b) for a, b in zip(left["unkeyed"][kind], candidates)
        )
    return {
        "byte_identical": before == after,
        "entities_before": left["counts"], "entities_after": right["counts"],
        "source_handles": len(left["keyed"]), "handles_preserved": preserved,
        # Fixed-size selection keeps sorted diagnostics linear in drawing size.
        "handles_missing": heapq.nsmallest(MAX_LISTED_HANDLES, missing),
        "handles_changed": heapq.nsmallest(MAX_LISTED_HANDLES, changed),
        "unkeyed_before": sum(len(v) for v in left["unkeyed"].values()),
        "unkeyed_matched": unkeyed_matched,
        "blocks_match": _same(left["blocks"], right["blocks"]),
        "block_records": len(block_left["keyed"]),
        "block_records_preserved": block_preserved,
        "block_records_missing": heapq.nsmallest(MAX_LISTED_HANDLES, block_missing),
        "block_records_changed": heapq.nsmallest(MAX_LISTED_HANDLES, block_changed),
        "layers_match": set(left["layers"]) <= set(right["layers"]),
        "_block_unkeyed_match": block_unkeyed_match,
    }


def run_drawing(adapter: EngineAdapter, rel: str) -> dict:
    t0 = perf_counter()
    path = PROJECT_ROOT / rel
    bytes_before = None
    sha_before = None
    source_untouched = False
    error = None
    result = {}
    try:
        bytes_before = path.stat().st_size
        if bytes_before > MAX_DRAWING_BYTES:
            error = f"drawing exceeds {MAX_DRAWING_BYTES} bytes"
            source_untouched = True
        else:
            original = path.read_bytes()
            sha_before = hashlib.sha256(original).digest()
            try:
                parse_dxf_bytes(original, source_name=path.name)
            except DxfParseError as exc:
                error = f"unparseable source: {exc}"
            if error is None:
                try:
                    output = adapter.round_trip(original)
                    if len(output) > MAX_OUTPUT_BYTES:
                        error = f"round-trip output exceeds {MAX_OUTPUT_BYTES} bytes"
                    else:
                        result = {"bytes_after": len(output), **compare(
                            original, output, source_name=path.name,
                        )}
                except DxfParseError as exc:
                    error = f"unparseable round-trip output: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    timing_ms = round((perf_counter() - t0) * 1000, 3)
    if sha_before is not None:
        try:
            sha_after = hashlib.sha256()
            with path.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(64 * 1024), b""):
                    sha_after.update(chunk)
            source_untouched = sha_before == sha_after.digest()
        except Exception as exc:
            source_untouched = False
            error = error or f"{type(exc).__name__}: {exc}"
    if error is not None:
        result = {}
    fields = {key: result.get(key) for key in TRACKED_RECEIPT_KEYS[5:-2]}
    ok = (
        error is None and source_untouched
        and timing_ms <= TRACKED_DRAWING_TIMEOUT_MS
        and fields["entities_before"] == fields["entities_after"]
        and fields["handles_preserved"] == fields["source_handles"]
        and fields["unkeyed_matched"] == fields["unkeyed_before"]
        and fields["block_records_preserved"] == fields["block_records"]
        and result["_block_unkeyed_match"]
        and fields["blocks_match"] and fields["layers_match"]
    )
    receipt = {
        "drawing": rel, "adapter": adapter.name, "ok": ok,
        "timing_ms": timing_ms, "bytes_before": bytes_before, **fields,
        "source_untouched": source_untouched, "error": error,
    }
    assert tuple(receipt) == TRACKED_RECEIPT_KEYS
    return receipt


def run_tracked(adapter) -> list[dict]:
    return [run_drawing(adapter, rel) for rel in TRACKED_DRAWINGS]


def main(argv=None) -> int:
    adapter = _select_adapter(argv if argv is not None else sys.argv[1:])
    receipts = run_tracked(adapter)
    print(json.dumps(receipts, indent=2))
    print()
    ok = all(receipt["ok"] for receipt in receipts)
    verdict = "ALL DRAWINGS OK" if ok else "FAILURES ABOVE"
    print(f"# RESULT: {verdict} ({len(receipts)} drawings, adapter={adapter.name!r})")
    for receipt in receipts:
        if not receipt["ok"]:
            print(f"  FAIL: {receipt['drawing']}: {receipt['error'] or receipt}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
