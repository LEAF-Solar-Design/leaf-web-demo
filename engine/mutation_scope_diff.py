"""Fails closed on malformed input, compares every entity by handle, and treats an entity outside the declared scope that differs as a defect.

Block definitions are never in scope in v1.
Unsupported kinds are compared as raw records; a change is always a defect
regardless of scope, since they have no handle the scope could name.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import export_fidelity as _fidelity
import dxf_intake as _dxf_intake


@dataclass(frozen=True)
class MutationScope:
    touched_handles: frozenset[str]
    removed_handles: frozenset[str]
    added_layers: frozenset[str]

    @classmethod
    def from_iterables(cls, touched=(), removed=(), added_layers=()):
        return cls(
            frozenset(handle.upper() for handle in touched),
            frozenset(handle.upper() for handle in removed),
            frozenset(added_layers),
        )


def _raw_records(raw: bytes) -> dict:
    """Keep all groups, including repeats, in ENTITIES and BLOCKS records.

    The fidelity style reader discards geometry and the block ledger discards
    nonnumeric groups, so neither can supply these complete records.
    """
    if len(raw) > _fidelity.MAX_OUTPUT_BYTES:
        raise ValueError(f"raw scan exceeds {_fidelity.MAX_OUTPUT_BYTES} bytes")
    lines = raw.splitlines()
    if len(lines) % 2:
        raise ValueError("odd number of DXF lines")
    records = {"ENTITIES": [], "BLOCKS": {}}
    section = None
    section_pending = False
    record = None
    block_name = ""
    pairs = iter(lines)
    for code_line, value_line in zip(pairs, pairs):
        code = int(code_line.strip())
        value = _fidelity._decode_value(value_line)
        if code == 0:
            if record is not None:
                if section == "ENTITIES":
                    records[section].append(record)
                else:
                    if record["type"] == "BLOCK":
                        block_name = next((v for c, v in record["groups"] if c == 2), "")
                    records[section].setdefault(block_name, []).append(record)
                    if record["type"] == "ENDBLK":
                        block_name = ""
            record = None
            kind = value.strip().upper()
            if kind == "SECTION":
                section_pending = True
                section = None
            elif kind in ("ENDSEC", "EOF"):
                section = None
                section_pending = False
            elif section in records:
                record = {"type": kind, "groups": []}
        elif section_pending and code == 2:
            section = value.strip().upper()
            section_pending = False
        elif record is not None:
            record["groups"].append((code, value))
    if record is not None:
        raise ValueError("unterminated DXF record")
    return records


def _raw_value(code: int, value: str):
    # DXF numeric groups; names, handles and other strings stay exact strings.
    if any(low <= code <= high for low, high in (
        (10, 99), (110, 149), (160, 179), (210, 239), (270, 299),
        (370, 389), (400, 409), (420, 429), (440, 459), (1010, 1071),
    )):
        number = float(value)
        if not _fidelity._same(number, number):
            raise ValueError("nonfinite DXF numeric value")
        return number
    return value


def _same_block_record_strict(a, b) -> bool:
    return a["type"] == b["type"] and _fidelity._same(
        [(code, _raw_value(code, value)) for code, value in a["groups"]],
        [(code, _raw_value(code, value)) for code, value in b["groups"]],
    )


def _unsupported_records(records):
    supported = {
        "polylines": ("LWPOLYLINE", "POLYLINE", "VERTEX", "SEQEND"),
        "circles": ("CIRCLE",), "arcs": ("ARC",), "texts": ("TEXT", "MTEXT"),
        "dimensions": ("DIMENSION",), "inserts": ("INSERT",),
        "mleaders": ("MULTILEADER",),
    }
    supported_types = {kind for field in _fidelity.ENTITY_KINDS for kind in supported[field]}
    canonical = Counter()
    for record in records:
        if record["type"] in supported_types:
            continue
        groups = []
        for code, value in record["groups"]:
            value = _raw_value(code, value)
            groups.append((code, round(value, 6) if isinstance(value, float) else value))
        canonical[(record["type"], tuple(sorted(groups)))] += 1
    return canonical


def compare_scoped(before: bytes, after: bytes, scope: MutationScope, *, source_name: str) -> dict:
    parsed_before = _dxf_intake.parse_dxf_bytes(before, source_name=source_name)
    if parsed_before.get("parseErrors"):
        raise ValueError(f"before: {parsed_before['parseErrors'][0]}")
    parsed_after = _dxf_intake.parse_dxf_bytes(after, source_name=source_name)
    if parsed_after.get("parseErrors"):
        raise ValueError(f"after: {parsed_after['parseErrors'][0]}")
    left = _fidelity._ledger(parsed_before)
    right = _fidelity._ledger(parsed_after)
    # Retain the shared ledger's duplicate block-handle validation.
    _fidelity._block_ledger(before)
    _fidelity._block_ledger(after)
    raw_left, raw_right = _raw_records(before), _raw_records(after)
    block_left, block_right = raw_left["BLOCKS"], raw_right["BLOCKS"]
    blocks_changed = set()
    for name in block_left.keys() | block_right.keys():
        a, b = block_left.get(name, []), block_right.get(name, [])
        if len(a) != len(b) or not all(
            _same_block_record_strict(x, y) for x, y in zip(a, b)
        ):
            blocks_changed.add(name)
    unsupported_left = _unsupported_records(raw_left["ENTITIES"])
    unsupported_right = _unsupported_records(raw_right["ENTITIES"])
    unsupported_changed = unsupported_left != unsupported_right
    unsupported_kinds = sorted({record[0] for record in unsupported_left.keys() | unsupported_right.keys()})
    source, output = left["keyed"], right["keyed"]
    untouched = source.keys() - scope.touched_handles - scope.removed_handles
    changed = {
        handle for handle in source.keys() & output.keys()
        if not _fidelity._same(
            _fidelity._without_properties(source[handle]),
            _fidelity._without_properties(output[handle]),
        )
    }
    untouched_changed = sorted(untouched & changed)
    unexpected_removed = sorted(source.keys() - output.keys() - scope.removed_handles)
    unexpected_added = sorted(
        handle for handle in output.keys() - source.keys()
        if output[handle][1].get("layer") not in scope.added_layers
    )
    expected_removed_missing = sorted(scope.removed_handles & output.keys())
    handleless_changed = any(
        not _fidelity._same(
            _fidelity._without_properties(left["unkeyed"][kind]),
            _fidelity._without_properties(right["unkeyed"][kind]),
        )
        for kind in _fidelity.ENTITY_KINDS
    )
    return {
        "ok": not (
            untouched_changed or unexpected_removed or unexpected_added
            or expected_removed_missing or handleless_changed or blocks_changed
            or unsupported_changed
        ),
        "source_name": source_name,
        "untouched_total": len(untouched),
        "untouched_changed": untouched_changed,
        "unexpected_removed": unexpected_removed,
        "unexpected_added": unexpected_added,
        "expected_removed_missing": expected_removed_missing,
        "touched_changed": sorted(scope.touched_handles & changed),
        "handleless_changed": handleless_changed,
        "blocks_changed": sorted(blocks_changed),
        "unsupported_kinds": unsupported_kinds,
        "unsupported_changed": unsupported_changed,
        "kind_counts_before": left["counts"],
        "kind_counts_after": right["counts"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--touched", default="")
    parser.add_argument("--removed", default="")
    parser.add_argument("--added-layer", action="append", default=[])
    parser.add_argument("--source-name")
    args = parser.parse_args(argv)
    scope = MutationScope.from_iterables(
        touched=(handle.strip() for handle in args.touched.split(",") if handle.strip()),
        removed=(handle.strip() for handle in args.removed.split(",") if handle.strip()),
        added_layers=args.added_layer,
    )
    try:
        result = compare_scoped(
            args.before.read_bytes(), args.after.read_bytes(), scope,
            source_name=args.source_name if args.source_name is not None else args.before.name,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
