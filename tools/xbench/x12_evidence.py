"""Validation shared by the standalone X12 evidence comparisons."""

from collections.abc import Mapping


def rows(value, path):
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected an array")
    for i, row in enumerate(value):
        location = f"{path}[{i}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{location}: expected an object")
        yield location, row


def field(row, key, path, check, expected):
    if key not in row:
        raise ValueError(f"{path}.{key}: missing field")
    value = row[key]
    if not check(value):
        raise ValueError(f"{path}.{key}: expected {expected}")
    return value


def name(row, key, path):
    return field(row, key, path, lambda v: isinstance(v, str) and bool(v.strip()), "nonblank text")


def indexed(value, path, identity, parse):
    result = {}
    seen = set()
    for location, row in rows(value, path):
        key = name(row, identity, location)
        if key.casefold() in seen:
            raise ValueError(f"{location}.{identity}: duplicate {key!r}")
        seen.add(key.casefold())
        result[key] = parse(row, location)
    return result


def compare(before, after, label, fields):
    differences = []
    for key, old in before.items():
        path = f"{label}[{key!r}]"
        if key not in after:
            differences.append(f"{path}: missing")
            continue
        for attr in fields:
            new_value = after[key][attr]
            if old[attr] != new_value:
                differences.append(f"{path}.{attr}: expected {old[attr]!r}, got {new_value!r}")
    return differences
