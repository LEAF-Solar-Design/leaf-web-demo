"""Count model-space panel INSERTs and reject ambiguous entity identities."""
def _count(entities, layer, block, inherited, active, budget, instances):
    seen = {}
    total = 0
    for entity in entities:
        budget[0] -= instances
        if budget[0] < 0 or len(active) >= 128:
            return None
        kind = entity.dxftype()
        attrs = entity.dxf
        handle = attrs.get("handle")
        if not isinstance(handle, str) or not handle:
            return None
        target = attrs.layer.casefold()
        name = attrs.name.casefold() if kind == "INSERT" else None
        rows = attrs.get("row_count", 1) if name is not None else 1
        cols = attrs.get("column_count", 1) if name is not None else 1
        if any(type(n) is not int or n <= 0 for n in (rows, cols)):
            return None
        signature = (kind, target, name, rows, cols)
        key = handle.upper()
        if key in seen:
            if seen[key] != signature:
                return None
            continue
        seen[key] = signature
        if name is None:
            continue
        effective = inherited if target == "0" else target
        doc = getattr(entity, "doc", None)
        definition = doc.blocks.get(name) if doc is not None else None
        if definition is None or name in active:
            return None
        child_instances = instances * rows * cols
        if child_instances > budget[0] + instances:
            return None
        nested = _count(definition, layer, block, effective, active | {name}, budget, child_instances)
        if nested is None:
            return None
        total += rows * cols * (nested + int(name == block and effective == layer))
    return total


def layer_count_quality(entities, layer, block, reported, *, expansion_budget=100000):
    if type(expansion_budget) is not int or expansion_budget < 1:
        return "INCOMPLETE"
    total = _count(entities, layer.casefold(), block.casefold(), "0", set(), [expansion_budget], 1)
    if total is None:
        return "INCOMPLETE"
    return "PASS" if type(reported) is int and reported == total else "FAIL"
