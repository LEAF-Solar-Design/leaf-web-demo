"""Fails closed on malformed input, compares every entity by handle, and treats an entity outside the declared scope that differs as a defect.

Block definitions are never in scope in v1.
"""
from __future__ import annotations

import argparse
import json
import sys
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


def compare_scoped(before: bytes, after: bytes, scope: MutationScope, *, source_name: str) -> dict:
    parsed_before = _dxf_intake.parse_dxf_bytes(before, source_name=source_name)
    if parsed_before.get("parseErrors"):
        raise ValueError(f"before: {parsed_before['parseErrors'][0]}")
    parsed_after = _dxf_intake.parse_dxf_bytes(after, source_name=source_name)
    if parsed_after.get("parseErrors"):
        raise ValueError(f"after: {parsed_after['parseErrors'][0]}")
    left = _fidelity._ledger(parsed_before)
    right = _fidelity._ledger(parsed_after)
    block_left, block_right = _fidelity._block_ledger(before), _fidelity._block_ledger(after)
    blocks_changed = set()
    for handle in block_left["keyed"].keys() | block_right["keyed"].keys():
        a, b = block_left["keyed"].get(handle), block_right["keyed"].get(handle)
        if a is None or b is None or not (
            _fidelity._same_block_record(a, b) and _fidelity._same_block_record(b, a)
        ):
            blocks_changed.update(record["block"] for record in (a, b) if record is not None)
    for group in block_left["unkeyed"].keys() | block_right["unkeyed"].keys():
        a, b = block_left["unkeyed"].get(group, []), block_right["unkeyed"].get(group, [])
        if len(a) != len(b) or not all(
            _fidelity._same_block_record(x, y) and _fidelity._same_block_record(y, x)
            for x, y in zip(a, b)
        ):
            blocks_changed.add(group[0])
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
