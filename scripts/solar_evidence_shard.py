#!/usr/bin/env python3
"""Paired evidence shards for a step too large for one comparison document (contract G19).

The frozen comparator bounds the COMBINED comparison document (both sides together) at
100,000 nodes. A step whose two sides each fit but together do not (terrain-import t1, a
90 x 150 grid plus its mesh; piling-generate t4, 1,197 pile sets) is compared in n paired
shards instead, never by loosening the comparator.

Plan (from row ids and grid dimensions of each side, never from values):
  frame        rows 500 per shard, the i-th frame row in shard i // 500
  pile-set     rows 250 per shard, the i-th pile-set row in shard i // 250
  terrain-grid bands of 30 grid rows, band b in shard b; `elevations` holds that band
  terrain-mesh bands of 30 cell rows, band b in shard b; `cell_colors` holds that band
  anything else (markers, removed, off-grid faces) in shard 1
A banded row keeps its id and every other field whole (`rows`, `cols`, `extent`) and
gains `band_start` and `band_rows`. n is the larger of the two sides' plans; a shard one
side lacks is emitted with empty rows, so a row-count difference surfaces as diffs.

Each shard keeps the step document's envelope and recomputes only `after`,
`entity_mapping` (exactly the ids the shard references, G8: its row ids and every
{"entity_id": ...} inside a row, such as a pile set's `frame`) and `output_sha256` (the
comparator's canonical hash of the shard's `after`, the rule every producer applies).

Proof built in: each side's shards are recombined and must reproduce that side's `after`
byte for byte in canonical JSON; every shard must pass the comparator's evidence
validation and every pair must fit its combined document bound. Any failure is a named
ShardError and nothing is written. Inputs over the comparator's INPUT bounds are refused.
Every pass is linear in the document size (one walk per row, dict lookups for bands).
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = sys.modules.get("solar_w1_compare") or _load("solar_w1_compare", HERE / "solar_w1_compare.py")

FAMILY = "exports"
ROWS_PER_SHARD = {"frame": 500, "pile-set": 250}
BAND_ROWS = 30
# type -> (list field, whether the band unit is a cell row (rows - 1) rather than a grid row)
BANDED = {"terrain-grid": ("elevations", False), "terrain-mesh": ("cell_colors", True)}
BAND_KEYS = ("band_start", "band_rows")


class ShardError(ValueError):
    """A named refusal: no shard is returned or written."""


def _int(value, what):
    if type(value) is not int or value < 0:
        raise ShardError(f"{what} must be a nonnegative integer")
    return value


def _rows(doc, side):
    if not isinstance(doc, dict) or not isinstance(doc.get("after"), dict):
        raise ShardError(f"{side} evidence must be an object with an `after` object")
    rows = doc["after"].get("rows")
    if not isinstance(rows, list):
        raise ShardError(f"{side} after.rows must be a list")
    return rows


def _row_id(row, side):
    if not isinstance(row, dict) or not isinstance(row.get("type"), str):
        raise ShardError(f"{side} row must be an object with a string type")
    ref = row.get("id")
    if not isinstance(ref, dict) or set(ref) != {"entity_id"} or not isinstance(ref["entity_id"], str):
        raise ShardError(f"{side} row id must be an entity reference")
    if any(key in row for key in BAND_KEYS):
        raise ShardError(f"{side} row {ref['entity_id']} is already a band; shard a step document, not a shard")
    return ref["entity_id"]


def _geometry(row, side):
    """(unit rows, elements per unit row, list field) of a banded row, from its dimensions only."""
    field, cells = BANDED[row["type"]]
    rows = _int(row.get("rows"), f"{side} {row['type']} rows")
    cols = _int(row.get("cols"), f"{side} {row['type']} cols")
    if not isinstance(row.get(field), list):
        raise ShardError(f"{side} {row['type']} {field} must be a list")
    if cells:
        rows, cols = max(rows - 1, 0), max(cols - 1, 0)
    return rows, cols, field


def _band_count(unit_rows):
    return max(1, -(-unit_rows // BAND_ROWS))


def _side_plan(doc, side):
    """One side's shards: a list of [row position, band index or None] per shard."""
    rows = _rows(doc, side)
    shards = [[]]
    seen = {}
    ids = set()

    def put(index, entry):
        while len(shards) <= index:
            shards.append([])
        shards[index].append(entry)

    for pos, row in enumerate(rows):
        rid = _row_id(row, side)
        if rid in ids:
            raise ShardError(f"{side} duplicate row id {rid}")
        ids.add(rid)
        kind = row["type"]
        if kind in BANDED:
            unit_rows, _, _ = _geometry(row, side)
            for band in range(_band_count(unit_rows)):
                put(band, [pos, band])
        elif kind in ROWS_PER_SHARD:
            i = seen.get(kind, 0)
            seen[kind] = i + 1
            put(i // ROWS_PER_SHARD[kind], [pos, None])
        else:
            put(0, [pos, None])
    return shards


def plan(plugin_doc, studio_doc):
    """The paired shard plan: {"shards": n, "plugin": [...], "studio": [...]}, each side
    padded to n shards (an empty list is an empty shard). Values are never read."""
    sides = {"plugin": _side_plan(plugin_doc, "plugin"), "studio": _side_plan(studio_doc, "studio")}
    n = max(len(s) for s in sides.values())
    for s in sides.values():
        s.extend([] for _ in range(n - len(s)))
    return {"shards": n, **sides}


def _band(row, band, side):
    unit_rows, width, field = _geometry(row, side)
    start = band * BAND_ROWS
    last = band == _band_count(unit_rows) - 1
    values = row[field]
    # The last band takes every remaining element, so a list longer or shorter than its
    # declared dimensions still lands exactly once and surfaces as a length diff.
    chunk = values[start * width:] if last else values[start * width:(start + BAND_ROWS) * width]
    out = dict(row)
    out[field] = chunk
    out["band_start"] = start
    out["band_rows"] = max(0, min(BAND_ROWS, unit_rows - start))
    return out


def _references(value, found):
    """Every entity id referenced anywhere in value (iterative, one visit per node)."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "entity_id" in item and isinstance(item["entity_id"], str):
                found.add(item["entity_id"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _shard_doc(doc, rows, side, k):
    mapping = doc.get("entity_mapping")
    if not isinstance(mapping, dict):
        raise ShardError(f"{side} entity_mapping must be an object")
    after = {key: value for key, value in doc["after"].items() if key != "rows"}
    after["rows"] = rows
    refs = _references(rows, set())
    _references(doc.get("before"), refs)
    changes = doc.get("changes")
    if isinstance(changes, dict):
        for ids in changes.values():
            if isinstance(ids, list):
                refs.update(x for x in ids if isinstance(x, str))
    missing = sorted(x for x in refs if x not in mapping)  # O(refs), never O(mapping) per shard
    if missing:
        raise ShardError(f"{side} shard {k} references unmapped entities: {missing[:5]}")
    shard = {key: copy.deepcopy(value) for key, value in doc.items()
             if key not in ("after", "entity_mapping", "output_sha256")}
    shard["after"] = after
    shard["entity_mapping"] = {ref: mapping[ref] for ref in sorted(refs)}
    try:
        shard["output_sha256"] = compare.semantic_hash(after)
    except compare.InputError as exc:
        raise ShardError(f"{side} shard {k} exceeds the comparator's document bounds: {exc}") from None
    return shard


def recombine(shard_afters):
    """Rebuild one side's `after` from its shards' `after` objects, in shard order.

    Rows come back grouped by type in the order the types first appear (shard 1 carries
    the first chunk of every type, so that is the step's own type order), each type's
    rows in shard order, and a banded row's bands joined in band_start order. Refuses a
    gap, an overlap, or a band whose whole fields disagree with its first band."""
    if not shard_afters:
        raise ShardError("no shards to recombine")
    head = {key: value for key, value in shard_afters[0].items() if key != "rows"}
    order, per_type, bands = [], {}, {}
    for k, after in enumerate(shard_afters, 1):
        if not isinstance(after, dict) or not isinstance(after.get("rows"), list):
            raise ShardError(f"shard {k} after.rows must be a list")
        if _canonical({key: v for key, v in after.items() if key != "rows"}) != _canonical(head):
            raise ShardError(f"shard {k} after fields other than rows differ from shard 1")
        for row in after["rows"]:
            if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                raise ShardError(f"shard {k} holds a row without a type")
            kind = row["type"]
            if kind not in per_type:
                order.append(kind)
                per_type[kind] = []
            if "band_start" in row:
                rid = row.get("id", {}).get("entity_id") if isinstance(row.get("id"), dict) else None
                if rid not in bands:
                    bands[rid] = []
                    per_type[kind].append((True, rid))
                bands[rid].append(row)
            else:
                per_type[kind].append((False, row))
    rows = []
    for kind in order:
        for banded, item in per_type[kind]:
            rows.append(_join(item, bands[item]) if banded else item)
    return {**head, "rows": rows}


def _join(rid, parts):
    kind = parts[0]["type"]
    if kind not in BANDED:
        raise ShardError(f"row {rid} of type {kind} cannot be banded")
    field = BANDED[kind][0]
    parts = sorted(parts, key=lambda p: (p.get("band_start") if type(p.get("band_start")) is int else -1))
    whole = {key: value for key, value in parts[0].items() if key not in (field, *BAND_KEYS)}
    expected, values = 0, []
    for part in parts:
        start, count = part.get("band_start"), part.get("band_rows")
        if type(start) is not int or type(count) is not int or count < 0 or start != expected:
            raise ShardError(f"row {rid} bands are not contiguous from 0 (band at {start!r}, expected {expected})")
        if not isinstance(part.get(field), list):
            raise ShardError(f"row {rid} band at {start} has no {field} list")
        if _canonical({key: v for key, v in part.items() if key not in (field, *BAND_KEYS)}) != _canonical(whole):
            raise ShardError(f"row {rid} band at {start} disagrees with its first band outside {field}")
        values.extend(part[field])
        expected = start + count
    whole[field] = values
    return whole


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def prove(doc, shards, side="evidence"):
    """Refuse unless the shards recombine to doc's `after` exactly and each carries the
    comparator's hash of its own `after`."""
    for k, shard in enumerate(shards, 1):
        try:
            ok = isinstance(shard, dict) and shard.get("output_sha256") == compare.semantic_hash(shard.get("after"))
        except compare.InputError:
            ok = False
        if not ok:
            raise ShardError(f"{side} shard {k} output_sha256 does not match its after")
    try:
        rebuilt = compare.scan_input(recombine([s["after"] for s in shards]))
    except compare.InputError as exc:
        raise ShardError(f"{side} recombined after is out of bounds: {exc}") from None
    if rebuilt != compare.scan_input(doc["after"]):
        raise ShardError(f"{side} shards do not recombine to the step's after")


def _check_input(doc, side):
    _rows(doc, side)
    try:
        digest = compare.scan_input(doc["after"])
        compare.scan_input(doc)
    except compare.InputError as exc:
        raise ShardError(f"{side} evidence exceeds the comparator's input bounds: {exc}") from None
    if doc.get("output_sha256") != digest:
        raise ShardError(f"{side} output_sha256 does not match its after")


def split(plugin_doc, studio_doc):
    """[(plugin_shard, studio_shard), ...] per G19, proven and comparator-valid."""
    docs = {"plugin": plugin_doc, "studio": studio_doc}
    for side, doc in docs.items():
        _check_input(doc, side)
    layout = plan(plugin_doc, studio_doc)
    n = layout["shards"]
    built = {}
    for side, doc in docs.items():
        rows = doc["after"]["rows"]
        shards = []
        for k, entries in enumerate(layout[side], 1):
            shard_rows = [rows[pos] if band is None else _band(rows[pos], band, side) for pos, band in entries]
            shards.append(_shard_doc(doc, shard_rows, side, k))
        prove(doc, shards, side)
        for k, shard in enumerate(shards, 1):
            try:
                compare.validate_evidence(shard, FAMILY)
                compare._normalize(shard["after"], shard["entity_mapping"])
            except compare.InputError as exc:
                raise ShardError(f"{side} shard {k} of {n} refused by the comparator: {exc}") from None
        built[side] = shards
    pairs = list(zip(built["plugin"], built["studio"]))
    for k, (left, right) in enumerate(pairs, 1):
        try:
            compare._bounded({"schema": compare.SCHEMA, "capability": "shard", "family": FAMILY,
                              "plugin": left, "studio": right})
        except compare.InputError as exc:
            raise ShardError(f"shard pair {k} of {n} exceeds the comparator's document bound: {exc}") from None
    return pairs


def _write(path, doc):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_canonical(doc) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin", required=True, help="plugin evidence for one step")
    parser.add_argument("--studio", required=True, help="Studio evidence for the same step")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        pairs = split(compare.load_input_graph(args.plugin), compare.load_input_graph(args.studio))
    except (ShardError, compare.InputError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = len(pairs)
    files = []
    for k, (left, right) in enumerate(pairs, 1):
        for side, doc in (("plugin", left), ("studio", right)):
            path = out / f"{side}-part{k}of{n}.json"
            _write(path, doc)
            files.append(str(path))
    print(json.dumps({"shards": n, "files": files}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
