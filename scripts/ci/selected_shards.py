"""Partition the original --only union without importing the checkout runner."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile


def _strings(value, name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} must be a nonempty list of strings")
    if any(not isinstance(item, str) or not item or "\0" in item for item in value):
        raise ValueError(f"{name} must contain nonempty strings without NUL")
    return list(value)


def _timings(path, selected_ids):
    """Reject the whole prediction document if any weight is invalid."""
    if path is None:
        return None
    try:
        with open(os.fspath(path), encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, dict) or document.get("schema") != "leaf.ci.suite-seconds.v1":
            return None
        seconds = document.get("seconds")
        if not isinstance(seconds, dict):
            return None
        for suite_id, weight in seconds.items():
            if not isinstance(suite_id, str) or not suite_id:
                return None
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                return None
            if not math.isfinite(weight) or weight < 0:
                return None
        if any(suite_id not in seconds for suite_id in selected_ids):
            return None
        if not any(seconds[suite_id] > 0 for suite_id in selected_ids):
            return None
        return seconds
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def _validate_partition(selected_ids, shards):
    flattened = [suite_id for shard in shards for suite_id in shard]
    if (len(flattened) != len(selected_ids)
            or len(set(flattened)) != len(flattened)
            or set(flattened) != set(selected_ids)):
        raise ValueError("partition must cover the exact selected union once")


def make_plan(catalog_ids, patterns, shard_count, shard_index, timings_path=None):
    """Return a deterministic plan; shard indexes are zero based.

    LPT breaks equal durations by ascending ID and equal loads by shard index.
    Both algorithms restore catalog order inside every shard.
    """
    catalog_ids = _strings(catalog_ids, "catalog_ids")
    patterns = _strings(patterns, "patterns")
    if len(set(catalog_ids)) != len(catalog_ids):
        raise ValueError("catalog_ids contains duplicate IDs")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be a positive integer")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    for pattern in patterns:
        if not any(pattern in suite_id for suite_id in catalog_ids):
            raise ValueError(f"--only pattern matches no suite: {pattern!r}")
    selected_ids = [suite_id for suite_id in catalog_ids
                    if any(pattern in suite_id for pattern in patterns)]
    seconds = _timings(timings_path, selected_ids)
    shards = [[] for _ in range(shard_count)]
    if seconds is None:
        algorithm = "equal-count"
        for index, suite_id in enumerate(sorted(selected_ids)):
            shards[index % shard_count].append(suite_id)
    else:
        algorithm = "lpt"
        loads = [0.0] * shard_count
        for suite_id in sorted(selected_ids, key=lambda item: (-seconds[item], item)):
            index = min(range(shard_count), key=lambda item: (loads[item], item))
            shards[index].append(suite_id)
            loads[index] += seconds[suite_id]
    catalog_order = {suite_id: index for index, suite_id in enumerate(catalog_ids)}
    for shard in shards:
        shard.sort(key=catalog_order.__getitem__)
    _validate_partition(selected_ids, shards)
    return {
        "schema": "leaf.ci.selected-shards.v1",
        "selected_ids": selected_ids,
        "shards": shards,
        "shard_ids": list(shards[shard_index]),
        "algorithm": algorithm,
    }


def _catalog_ids(path):
    with open(path, encoding="utf-8") as source:
        catalog = json.load(source)
    if not isinstance(catalog, dict) or catalog.get("schema") != "leaf.ci.catalog.v1":
        raise ValueError("catalog must use schema leaf.ci.catalog.v1")
    rows = catalog.get("suites")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or "id" not in row for row in rows):
        raise ValueError("catalog suites must be rows containing id")
    return [row["id"] for row in rows]


def _only_patterns(path):
    raw = Path(path).read_bytes()
    if not raw or not raw.endswith(b"\0"):
        raise ValueError("only-args must be nonempty and NUL terminated")
    tokens = raw[:-1].decode("utf-8").split("\0")
    if len(tokens) % 2 or any(tokens[index] != "--only" for index in range(0, len(tokens), 2)):
        raise ValueError("only-args must alternate --only and value tokens")
    return _strings(tokens[1::2], "only-args values")


def _write_plan(path, plan):
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", suffix=".tmp",
                                         delete=False) as output:
            temporary = output.name
            json.dump(plan, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--catalog", required=True)
    plan_parser.add_argument("--only-args", required=True)
    plan_parser.add_argument("--timings")
    plan_parser.add_argument("--shard-count", required=True, type=int)
    plan_parser.add_argument("--shard-index", required=True, type=int)
    plan_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        plan = make_plan(_catalog_ids(args.catalog), _only_patterns(args.only_args),
                         args.shard_count, args.shard_index, args.timings)
        _write_plan(args.output, plan)
    except (OSError, ValueError, TypeError, OverflowError) as error:
        print(f"selected_shards: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
