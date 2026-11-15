"""Offline X09 comparisons and bounded plans. These do not prove a live run."""

import hashlib
import json
import re


def _json(value):
    # Reject values that JSON would silently coerce or Python compares loosely.
    if value is None or type(value) in (bool, str, int):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if type(value) is float:
        return json.dumps(value, allow_nan=False)
    if type(value) is list:
        return "[" + ",".join(_json(item) for item in value) + "]"
    if type(value) is dict and all(type(key) is str for key in value):
        return "{" + ",".join(_json(key) + ":" + _json(value[key])
                               for key in sorted(value)) + "}"
    raise ValueError("expected JSON values with string object keys")


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def compare_pair(expected, observed):
    """Compare an explicit oracle with output; never infer an assertion."""
    rule = expected.get("rule") if type(expected) is dict else None
    result = {"verdict": "UNREADABLE", "assertion": rule, "reason": "invalid oracle or output"}
    try:
        if rule == "sha256" and _hash(expected.get("value")):
            actual = hashlib.sha256(_json(observed).encode("utf-8")).hexdigest()
            matches = actual == expected["value"]
        elif rule == "count" and type(expected.get("value")) is int and expected["value"] >= 0:
            if type(observed) is not int or observed < 0:
                return result
            matches = observed == expected["value"]
        elif rule == "exact-values" and "value" in expected:
            matches = _json(observed) == _json(expected["value"])
        else:
            return result
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return result
    return {"verdict": "PASS" if matches else "FAIL", "assertion": rule,
            "reason": "golden compare"}


def plan_pairs(tools, drawings, day_index, max_pairs=20):
    """Plan at most 20 unique pairs; refuse impossible weekly coverage."""
    if type(day_index) is not int or day_index < 0:
        raise ValueError("day_index must be a nonnegative integer")
    if type(max_pairs) is not int or not 1 <= max_pairs <= 20:
        raise ValueError("max_pairs must be between 1 and 20")
    if type(tools) is not list or type(drawings) is not list or not tools or not drawings:
        raise ValueError("tools and drawings must be nonempty lists")
    tool_keys = []
    for tool in tools:
        if type(tool) is not dict or any(type(tool.get(k)) is not str or not tool[k].strip()
                                         for k in ("name", "version")):
            raise ValueError("tool identity is missing")
        if not _hash(tool.get("package_sha256")):
            raise ValueError("tool package hash is invalid")
        tool_keys.append((tool["name"], tool["version"], tool["package_sha256"]))
    drawing_keys = []
    for drawing in drawings:
        if type(drawing) is not dict or not _hash(drawing.get("sha256")):
            raise ValueError("drawing hash is invalid")
        drawing_keys.append(drawing["sha256"])
    if len(set(tool_keys)) != len(tool_keys) or len(set(drawing_keys)) != len(drawing_keys):
        raise ValueError("duplicate tool or drawing identity")
    size = len(tool_keys) * len(drawing_keys)
    if size > 7 * max_pairs:
        raise ValueError("weekly coverage exceeds nightly capacity")
    pairs = [(tool, drawing) for tool in sorted(tool_keys) for drawing in sorted(drawing_keys)]
    start = (day_index * max_pairs) % size
    return [{"name": tool[0], "version": tool[1], "package_sha256": tool[2],
             "drawing_sha256": drawing}
            for tool, drawing in (pairs[(start + offset) % size]
                                  for offset in range(min(max_pairs, size)))]
