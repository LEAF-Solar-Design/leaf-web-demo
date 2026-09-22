#!/usr/bin/env python3
"""Bounded semantic evidence comparator for the Solar parity program.

Adapter contract: solar-w1-comparison.v1.schema.json. Entity references use
{"entity_id": "local-id"}; quantities use {"kind", "value", "unit"}.
Other values are exact, including array order. No geometry or identity is
inferred from a display label. Inputs must be sanitized producer receipts.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

SCHEMA = "leaf.solar-w1-comparison.v1"
NAME = "solar-w1-semantic"
VERSION = "1"
MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 100000
MAX_DEPTH = 40
MAX_DIFFS = 200
# The bounds above judge a document under COMPARISON and never move. An adapter
# instead scans its producer's OWN graph, which is far larger than the evidence
# that graph yields. Measured on data/rooftop_demo.dwg (2345 panels, 11 groups):
# the Studio graph is 107,135 nodes and 2,565,614 canonical bytes, while the
# groups evidence it produces is 7,233 nodes and 288,503 bytes. These input
# bounds hold a 10x drawing and still refuse a runaway producer; a bound that
# cannot refuse is the defect they guard against.
MAX_INPUT_NODES = 1200000
MAX_INPUT_BYTES = 32 * 1024 * 1024
IO_TIMEOUT = 5
LENGTH_UNITS = {"mm": "1", "cm": "10", "m": "1000", "in": "25.4", "ft": "304.8"}
ANGLE_UNITS = {"deg": "1", "rad": str(180 / math.pi)}
FAMILIES = {
    "count": ("counts", "identities"),
    "settings": ("settings",),
    "zones": ("zones",),
    "groups": ("groups",),
    "panels": ("panels",),
    "strings": ("strings", "unassigned_panels", "duplicate_panels", "length_distribution"),
    "solve": ("added_panels", "removed_panels", "feasible", "objective", "chosen_candidate", "accepted", "reverted", "constraints"),
    "autofill": ("added_panels", "removed_panels", "feasible", "objective", "chosen_candidate", "accepted", "reverted", "constraints"),
    "reoptimization": ("added_panels", "removed_panels", "feasible", "objective", "chosen_candidate", "accepted", "reverted", "constraints"),
    "devices": ("devices", "unassigned_circuits"),
    "routing": ("connectivity", "routes", "conductor_choice", "constraints"),
    "schedules": ("rows", "source_revision", "format"),
    "bom": ("rows", "source_revision", "format"),
    "exports": ("rows", "source_revision", "format"),
    "imports": ("source_sha256", "objects", "transforms", "rejects", "provenance"),
    "terrain": ("surface_inputs", "crs", "elevations", "sample_points", "result_units"),
    "shade": ("surface_inputs", "crs", "elevations", "sun", "time", "sample_points", "result_units"),
}
SOLVERS = {"solve", "autofill", "reoptimization"}
RECORD_FIELDS = {
    "zones": {"id", "name", "membership", "boundaries", "elevation", "equipment_config", "sizing_provenance"},
    "groups": {"id", "name", "membership", "boundaries", "elevation", "equipment_config", "sizing_provenance"},
    "panels": {"id", "geometry"},
    "strings": {"id", "ordered_membership", "polarity"},
    "devices": {"id", "model", "transform", "string_to_input", "capacities", "loads"},
    "routes": {"id", "geometry", "length"},
    "rows": {"id", "type", "quantity", "unit"},
}
# Quantities cannot weaken a safety or discrete decision, even nested ones.
EXACT_FIELDS = {
    "counts", "membership", "ordered_membership", "connectivity", "polarity",
    "feasible", "valid", "validity", "accepted", "reverted", "chosen_candidate",
    "constraints", "thresholds", "safety_thresholds", "rounding", "rounding_decisions",
    "model", "capacities", "loads", "string_to_input", "conductor_choice",
    "equipment_config", "sizing_provenance", "quantities",
}
EVIDENCE_KEYS = {
    "fixture_sha256", "input_sha256", "output_sha256", "revision", "versions",
    "parameters", "units", "frame", "entity_mapping", "before", "after", "changes",
    "warnings", "rejected_inputs", "provenance", "elapsed_ms", "execution_mode",
    "state", "survived_reopen", "synthetic_fields", "fallback_fields", "synthetic_flagged",
}


class InputError(ValueError):
    """Malformed evidence is never a parity verdict."""


def _require(condition, message):
    if not condition:
        raise InputError(message)


def _bounded(value, depth=0, budget=None):
    if budget is None:
        budget = [MAX_NODES]
    budget[0] -= 1
    _require(budget[0] >= 0 and depth <= MAX_DEPTH, "payload exceeds structural limit")
    if isinstance(value, dict):
        for key, item in value.items():
            _require(isinstance(key, str) and len(key) <= 256, "invalid object key")
            _bounded(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, budget)
    elif isinstance(value, str):
        _require(len(value) <= 16384, "string exceeds limit")
    elif type(value) in (int, float):
        _require(abs(value) <= 1e100 and math.isfinite(value), "invalid numeric value")
    else:
        _require(value is None or type(value) is bool, "not a JSON value")


def semantic_hash(value):
    """Hash canonical JSON before mapping or unit conversion, never rounded data."""
    _bounded(value)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    _require(len(data.encode("utf-8")) <= MAX_BYTES, "payload exceeds byte limit")
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def scan_input(value):
    """Bound a producer's own graph under the INPUT bounds and hash it canonically.

    The input-scan path only; a document under comparison keeps MAX_NODES,
    MAX_BYTES and MAX_DEPTH. Every other refusal is unchanged: depth,
    non-finite and out-of-range numbers, oversized strings, invalid keys and
    non-JSON values all fail closed here exactly as they do in semantic_hash.
    """
    _bounded(value, budget=[MAX_INPUT_NODES])
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    _require(len(data.encode("utf-8")) <= MAX_INPUT_BYTES, "input payload exceeds byte limit")
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x for x in value)


def validate_evidence(raw, family):
    _bounded(raw)
    _require(isinstance(raw, dict) and set(raw) == EVIDENCE_KEYS, "invalid evidence fields")
    for key in ("fixture_sha256", "input_sha256", "output_sha256"):
        _require(isinstance(raw[key], str) and re.fullmatch(r"[0-9a-f]{64}", raw[key]), "invalid " + key)
    _require(isinstance(raw["revision"], str) and raw["revision"], "revision is required")
    versions = raw["versions"]
    _require(isinstance(versions, dict) and set(versions) == {"schema", "producer", "capability", "engine", "catalog", "solver"}, "invalid versions")
    _require(all(isinstance(v, str) and v for v in versions.values()), "versions must be explicit (use none if inapplicable)")
    _require(isinstance(raw["parameters"], dict), "parameters must be an object")
    _require(raw["units"] in LENGTH_UNITS, "unknown drawing units")
    _require(isinstance(raw["frame"], dict) and set(raw["frame"]) == {"coordinate_system", "transform", "elevation_datum", "crs"}, "invalid frame")
    frame = raw["frame"]
    _require(all(isinstance(frame[k], str) and frame[k] for k in ("coordinate_system", "elevation_datum", "crs")), "frame labels required")
    _require(isinstance(frame["transform"], list) and len(frame["transform"]) == 16 and all(type(x) in (int, float) for x in frame["transform"]), "frame transform must have 16 numbers")
    mapping = raw["entity_mapping"]
    _require(isinstance(mapping, dict) and all(k and isinstance(v, str) and v for k, v in mapping.items()), "invalid entity mapping")
    _require(len(set(mapping.values())) == len(mapping), "entity mapping must be one-to-one")
    for key in ("before", "after", "provenance"):
        _require(isinstance(raw[key], dict) and raw[key], key + " must be a nonempty object")
    _require(set(FAMILIES[family]) <= set(raw["after"]), "missing family semantic fields")
    after = raw["after"]
    for key in set(after) & set(RECORD_FIELDS):
        _require(isinstance(after[key], list), key + " must be an array")
        for item in after[key]:
            _require(isinstance(item, dict) and RECORD_FIELDS[key] <= set(item), "missing " + key + " record fields")
            _require(isinstance(item["id"], dict) and set(item["id"]) == {"entity_id"}, "record id must be an entity reference")
            for field in ("membership", "ordered_membership"):
                if field in item:
                    _require(isinstance(item[field], list) and all(isinstance(x, dict) and set(x) == {"entity_id"} for x in item[field]), "membership must contain entity references")
        record_ids = [item["id"]["entity_id"] for item in after[key]]
        _require(all(isinstance(x, str) for x in record_ids) and len(set(record_ids)) == len(record_ids), "duplicate or invalid record id")
    if family == "count":
        _require(isinstance(after["counts"], dict) and all(type(v) is int and v >= 0 for v in after["counts"].values()), "counts must be nonnegative integers")
        _require(isinstance(after["identities"], list) and all(isinstance(x, dict) and set(x) == {"entity_id"} for x in after["identities"]), "identities must be entity references")
        ids = [x["entity_id"] for x in after["identities"]]
        _require(all(isinstance(x, str) for x in ids) and len(set(ids)) == len(ids), "duplicate or invalid count identity")
        _require(sum(after["counts"].values()) == len(ids), "count transport identities disagree with counts")
    if family in SOLVERS:
        for key in ("feasible", "accepted", "reverted"):
            _require(type(after[key]) is bool, "solver decisions must be boolean")
        _require(isinstance(after["constraints"], dict) and after["constraints"], "solver constraints required")
    changes = raw["changes"]
    _require(isinstance(changes, dict) and set(changes) == {"created", "modified", "deleted"}, "invalid changes")
    for ids in changes.values():
        _require(_strings(ids) and len(set(ids)) == len(ids) and all(x in mapping for x in ids), "changes require unique mapped ids")
    _require(not (set(changes["created"]) & set(changes["deleted"])), "created and deleted overlap")
    for key in ("warnings", "rejected_inputs", "synthetic_fields", "fallback_fields"):
        _require(_strings(raw[key]), "invalid " + key)
    for key in ("survived_reopen", "synthetic_flagged"):
        _require(type(raw[key]) is bool, "invalid " + key)
    _require(type(raw["elapsed_ms"]) in (int, float) and raw["elapsed_ms"] >= 0, "invalid elapsed time")
    _require(raw["execution_mode"] in ("live", "recorded", "synthetic", "fallback"), "invalid execution mode")
    _require(raw["state"] in ("committed", "pending", "failed"), "invalid completion state")
    _require(semantic_hash(raw["after"]) == raw["output_sha256"], "output hash mismatch")


def _number(value):
    _require(type(value) in (int, float), "quantity must be numeric")
    return Decimal(str(value))


def _normalize(value, mapping, exact=False):
    if isinstance(value, list):
        return [_normalize(x, mapping, exact) for x in value]
    if not isinstance(value, dict):
        return value
    if "entity_id" in value:
        _require(set(value) == {"entity_id"} and isinstance(value["entity_id"], str) and value["entity_id"] in mapping, "unmapped entity reference")
        return {"entity_id": mapping[value["entity_id"]]}
    if "kind" in value:
        _require(isinstance(value["kind"], str) and value["kind"] in ("coordinate", "length", "angle", "float"), "unknown quantity kind")
        _require(set(value) == {"kind", "value", "unit"}, "invalid quantity fields")
        kind, unit = value["kind"], value["unit"]
        _require(isinstance(unit, str) and unit, "quantity unit required")
        if kind in ("coordinate", "length"):
            _require(unit in LENGTH_UNITS, "unknown length unit")
            scale, normalized_unit = Decimal(LENGTH_UNITS[unit]), "mm"
        elif kind == "angle":
            _require(unit in ANGLE_UNITS, "unknown angle unit")
            scale, normalized_unit = Decimal(ANGLE_UNITS[unit]), "deg"
        else:
            scale, normalized_unit = Decimal(1), unit
        values = value["value"] if kind == "coordinate" else [value["value"]]
        _require(isinstance(values, list) and (kind != "coordinate" or len(values) in (2, 3)), "invalid coordinate")
        return (kind, normalized_unit, tuple(_number(v) * scale for v in values), exact)
    return {k: _normalize(v, mapping, exact or k in EXACT_FIELDS or any(term in k.lower() for term in ("threshold", "rounding", "valid", "decision", "limit"))) for k, v in value.items()}


def compare(plugin, studio, family, *, capability, objective_bound=None, prerequisites=None):
    """Return the receipt's {name, version, verdict, diffs} block.

    Alternative solves are opt-in with a frozen absolute objective bound. Only
    chosen_candidate and objective may differ; constraints and all decisions
    remain exact. Prerequisites are executable count and fixed-string documents.
    """
    _require(family in FAMILIES, "unknown capability family")
    _require(isinstance(capability, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", capability), "invalid capability")
    for evidence in (plugin, studio):
        validate_evidence(evidence, family)
    diffs = []

    def diff(path, reason):
        if len(diffs) < MAX_DIFFS:
            diffs.append(path + ": " + reason)

    def walk(a, b, path):
        if type(a) is not type(b):
            diff(path, "type differs")
        elif isinstance(a, dict):
            if a.keys() != b.keys():
                diff(path, "fields differ")
            for key in sorted(a.keys() & b.keys()):
                walk(a[key], b[key], path + "/" + key)
        elif isinstance(a, list):
            if len(a) != len(b):
                diff(path, "length differs")
            for i, (left, right) in enumerate(zip(a, b)):
                walk(left, right, path + "/" + str(i))
        elif isinstance(a, tuple):
            if a[:2] != b[:2] or len(a[2]) != len(b[2]) or a[3] != b[3]:
                diff(path, "quantity declaration differs")
                return
            for left, right in zip(a[2], b[2]):
                tolerance = Decimal(0)
                if not a[3]:
                    if a[0] in ("coordinate", "length"):
                        tolerance = Decimal("1")
                    elif a[0] == "angle":
                        tolerance = Decimal("0.01")
                    else:
                        tolerance = max(Decimal("0.000001"), Decimal("0.00000001") * max(abs(left), abs(right)))
                if abs(left - right) > tolerance:
                    diff(path, "quantity differs")
                    break
        elif a != b:
            diff(path, "value differs")

    for key in ("fixture_sha256", "input_sha256", "revision", "parameters", "frame"):
        walk(plugin[key], studio[key], key)
    for key in ("schema", "catalog", "solver"):
        walk(plugin["versions"][key], studio["versions"][key], "versions/" + key)
    walk(sorted(plugin["entity_mapping"].values()), sorted(studio["entity_mapping"].values()), "entity_mapping")
    for label, evidence in (("plugin", plugin), ("studio", studio)):
        if evidence["state"] != "committed" or not evidence["survived_reopen"]:
            diff(label, "requires committed state and actual reopen")
        if evidence["execution_mode"] in ("synthetic", "fallback"):
            diff(label, "execution mode is not production evidence")
        if (evidence["synthetic_fields"] or evidence["fallback_fields"]) and not evidence["synthetic_flagged"]:
            diff(label, "unflagged synthetic or fallback fields")
    for key in ("warnings", "rejected_inputs"):
        walk(plugin[key], studio[key], key)
    for key in ("created", "modified", "deleted"):
        left = sorted(plugin["entity_mapping"][x] for x in plugin["changes"][key])
        right = sorted(studio["entity_mapping"][x] for x in studio["changes"][key])
        walk(left, right, "changes/" + key)
    left = _normalize(plugin["after"], plugin["entity_mapping"])
    right = _normalize(studio["after"], studio["entity_mapping"])
    walk(_normalize(plugin["before"], plugin["entity_mapping"]), _normalize(studio["before"], studio["entity_mapping"]), "before")
    if family in SOLVERS:
        _require(isinstance(prerequisites, list) and len(prerequisites) == 2, "solver requires count then fixed-string evidence")
        for document, expected in zip(prerequisites, ("count", "strings")):
            _require(isinstance(document, dict) and document.get("family") == expected, "prerequisites out of order")
            _require(document.get("prerequisites", []) == [], "nested prerequisites forbidden")
            if compare_document(document)["verdict"] != "pass":
                diff("prerequisites/" + expected, "comparison failed")
            if document["plugin"]["fixture_sha256"] != plugin["fixture_sha256"]:
                diff("prerequisites/" + expected, "fixture differs")
            if expected == "strings":
                for side in ("plugin", "studio"):
                    strings = document[side]["after"]["strings"]
                    _require(isinstance(strings, list) and len(strings) == 1 and isinstance(strings[0], dict) and isinstance(strings[0].get("ordered_membership"), list) and strings[0]["ordered_membership"], "fixed-string prerequisite requires one ordered string")
    else:
        _require(not prerequisites and objective_bound is None, "solver options on non-solver family")
    if objective_bound is not None:
        _require(type(objective_bound) in (int, float) and math.isfinite(objective_bound) and 0 <= objective_bound <= 1e100, "invalid frozen objective bound")
        _require(isinstance(plugin["parameters"].get("objective_unit"), str) and plugin["parameters"]["objective_unit"], "alternative solve requires declared objective unit")
        # Compare constraints before allowing an alternative candidate.
        walk(left["constraints"], right["constraints"], "after/constraints")
        _require(type(plugin["after"]["objective"]) in (int, float) and type(studio["after"]["objective"]) in (int, float), "alternative objective must be a scalar in the declared result unit")
        if abs(_number(plugin["after"]["objective"]) - _number(studio["after"]["objective"])) > _number(objective_bound):
            diff("after/objective", "frozen objective bound exceeded")
        left = {k: v for k, v in left.items() if k not in ("objective", "chosen_candidate")}
        right = {k: v for k, v in right.items() if k not in ("objective", "chosen_candidate")}
    walk(left, right, "after")
    return {"name": NAME, "version": VERSION, "verdict": "fail" if diffs else "pass", "diffs": diffs}


def compare_document(document):
    _bounded(document)
    required = {"schema", "capability", "family", "plugin", "studio"}
    _require(isinstance(document, dict) and required <= set(document) <= required | {"objective_bound", "prerequisites"}, "invalid comparison document")
    _require(document["schema"] == SCHEMA, "unknown comparison schema")
    return compare(document["plugin"], document["studio"], document["family"], capability=document["capability"], objective_bound=document.get("objective_bound"), prerequisites=document.get("prerequisites"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def load_evidence(path, *, max_bytes=MAX_BYTES, scan=None):
    """Isolate regular-file reads so stalled private storage has a hard deadline.

    Defaults are the comparison bounds; load_input_graph is the only caller
    that widens them, and it widens both the read cap and the scan together.
    """
    reader = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "assert p.is_file(); "
        "f=p.open('rb'); data=f.read(int(sys.argv[2])+1); "
        "assert len(data)<=int(sys.argv[2]); sys.stdout.buffer.write(data)"
    )
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", reader, str(Path(path)), str(max_bytes)], capture_output=True, timeout=IO_TIMEOUT, check=True)
        value = json.loads(proc.stdout.decode("utf-8"), object_pairs_hook=_unique_object)
        (scan or _bounded)(value)
        return value
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, RecursionError) as exc:
        raise InputError("evidence unreadable or invalid") from exc


def load_input_graph(path):
    """Read a producer's own graph under the input bounds, never the comparison ones."""
    return load_evidence(path, max_bytes=MAX_INPUT_BYTES, scan=scan_input)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-input", required=True, help="named private plugin evidence path")
    parser.add_argument("--studio-input", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    parser.add_argument("--prerequisites", help="JSON array: count document, fixed-string document")
    parser.add_argument("--objective-bound", type=float)
    args = parser.parse_args(argv)
    try:
        result = compare(load_evidence(args.plugin_input), load_evidence(args.studio_input), args.family, capability=args.capability, objective_bound=args.objective_bound, prerequisites=load_evidence(args.prerequisites) if args.prerequisites else None)
    except (InputError, TypeError, KeyError, RecursionError):
        print("invalid comparison evidence", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
