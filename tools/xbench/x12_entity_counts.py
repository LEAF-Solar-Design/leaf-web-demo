"""Compare model-space counts with an explicit Studio layer allowlist."""

from .x12_evidence import field, name, rows


def _counts(value, path):
    result = {}
    seen = set()
    for location, row in rows(value, path):
        key = (name(row, "type", location), name(row, "layer", location))
        folded = tuple(part.casefold() for part in key)
        if folded in seen:
            raise ValueError(f"{location}: duplicate type/layer {key!r}")
        seen.add(folded)
        result[key] = field(
            row, "count", location, lambda v: type(v) is int and v >= 0, "nonnegative integer"
        )
    return result


def compare_entity_counts(before, after, *, original_layers, studio_layers):
    """Allow gains only on explicitly named layers absent from the source."""
    old = _counts(before, "before.entity_counts")
    new = _counts(after, "after.entity_counts")
    original = {layer.casefold() for layer in original_layers}
    original.update(layer.casefold() for _, layer in old)
    additions = set(studio_layers)
    issues = []
    # Dict union preserves evidence order and visits each key once.
    for entity_type, layer in old | new:
        key = entity_type, layer
        expected = old.get(key, 0)
        actual = new.get(key, 0)
        if expected == actual:
            continue
        if actual > expected and layer in additions and layer.casefold() not in original:
            continue
        issues.append(
            f"entity_counts[{entity_type!r}, {layer!r}].count: expected {expected}, got {actual}"
        )
    return issues
