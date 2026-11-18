"""Count model-space panel INSERTs and reject ambiguous entity identities."""
def layer_count_quality(entities, layer, block, reported):
    seen = {}
    total = 0
    for entity in entities:
        kind = entity.dxftype()
        attrs = entity.dxf
        handle = attrs.get("handle")
        if not isinstance(handle, str) or not handle:
            return "INCOMPLETE"
        target = attrs.layer.casefold()
        name = attrs.name.casefold() if kind == "INSERT" else None
        rows = attrs.get("row_count", 1) if name is not None else 1
        cols = attrs.get("column_count", 1) if name is not None else 1
        if any(type(n) is not int or n <= 0 for n in (rows, cols)):
            return "INCOMPLETE"
        signature = (kind, target, name, rows, cols)
        key = handle.upper()
        if key in seen:
            if seen[key] != signature:
                return "INCOMPLETE"
            continue
        seen[key] = signature
        if (target, name) == (layer.casefold(), block.casefold()):
            total += rows * cols
    return "PASS" if type(reported) is int and reported == total else "FAIL"
