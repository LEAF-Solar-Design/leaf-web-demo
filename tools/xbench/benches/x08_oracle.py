"""Independent, offline window oracle using native module bounding boxes only."""

from bisect import bisect_right
from math import isfinite


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("coordinates must be finite numbers")
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("coordinates must be finite numbers")
    return value


def _vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("invalid coordinate vector")
    return [_number(item) for item in value]


def _rect(value, positive=False):
    bounds = _vector(value, 4)
    x0, y0, x1, y1 = bounds
    if x0 > x1 or y0 > y1 or (positive and (x0 == x1 or y0 == y1)):
        raise ValueError("invalid bounds ordering or extent")
    return bounds


def _handle(value, seen):
    if not isinstance(value, str) or not value.strip() or value in seen:
        raise ValueError("module handles must be nonempty and unique")
    seen.add(value)
    return value


def modules_from_native_dump(payload, module_layer, expected_count):
    """Validate a native dump and return every module on the exact layer.

    The declared count covers all entities and must match their list length.
    Only the selected module count must match expected_count. Invalid selected
    entities are errors, never omissions.
    """
    if not isinstance(module_layer, str) or not module_layer:
        raise ValueError("module_layer must be a nonempty string")
    if type(expected_count) is not int or expected_count < 0:
        raise ValueError("expected_count must be a nonnegative integer")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("native dump was not successful")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("missing native result")
    count = result.get("count")
    entities = result.get("entities")
    if not isinstance(entities, list):
        raise ValueError("missing native entities")
    if type(count) is not int or count < 0 or count != len(entities):
        raise ValueError("native count does not match entities length")
    modules, seen = [], set()
    for row in entities:
        if not isinstance(row, dict) or "layer" not in row:
            raise ValueError("entity is missing its layer")
        if row["layer"] != module_layer:
            continue
        if row.get("type") != "BlockReference":
            raise ValueError("selected entity is not a BlockReference")
        handle = _handle(row.get("handle"), seen)
        bbox = row.get("bbox")
        if not isinstance(bbox, dict):
            raise ValueError("selected entity is missing its bbox")
        low = _vector(bbox.get("min"), 3)
        high = _vector(bbox.get("max"), 3)
        if any(a > b for a, b in zip(low, high)):
            raise ValueError("native bbox min exceeds max")
        bounds = _rect([low[0], low[1], high[0], high[1]], positive=True)
        modules.append({"handle": handle, "bounds": bounds})
    if len(modules) != expected_count:
        raise ValueError("selected module count does not match expected_count")
    return modules


def _modules(modules):
    rows, seen = [], set()
    for module in modules:
        if not isinstance(module, dict):
            raise ValueError("invalid module")
        rows.append({"handle": _handle(module.get("handle"), seen),
                     "bounds": _rect(module.get("bounds"), positive=True)})
    return rows


def contained_handles(modules, rect):
    """Return sorted handles whose entire XY bounds lie in rect, inclusively."""
    x0, y0, x1, y1 = _rect(rect)
    return sorted(module["handle"] for module in _modules(modules)
                  if x0 <= module["bounds"][0]
                  and y0 <= module["bounds"][1]
                  and module["bounds"][2] <= x1
                  and module["bounds"][3] <= y1)


def _sample_edges(edges, limit):
    edges = sorted(set(edges))
    if len(edges) <= limit:
        return edges
    return [edges[i * (len(edges) - 1) // (limit - 1)] for i in range(limit)]


def guarded_window_rect(modules, rect, expected_handles):
    """Give rounded native edges room without changing the independent set.

    The native CSV records six decimal places. Drawing geometry can retain
    more precision, so an exactly coincident drag edge is not a robust input.
    Only native bounds determine this margin; rendered selections never do.
    """
    rows = _modules(modules)
    rect = _rect(rect, positive=True)
    expected = sorted(expected_handles)
    if contained_handles(rows, rect) != expected:
        raise ValueError("window does not match independent handles")
    margin = min(rect[2] - rect[0], rect[3] - rect[1]) * 0.02
    for _ in range(24):
        candidate = [rect[0] - margin, rect[1] - margin,
                     rect[2] + margin, rect[3] + margin]
        if all(isfinite(v) for v in candidate) and contained_handles(rows, candidate) == expected:
            return candidate
        margin /= 2
    return rect


def choose_window(modules, target=500, tolerance=10):
    """Find an exact full-bounds window or conservatively refuse the layout.

    Per orientation, search at most 64 bands: whole Y, 31 Y-max prefixes,
    and 32 Y-min suffixes. Each band tries at most three X-max cutoffs near
    the desired ranks. Thus at most 384 rectangles across both orientations
    are considered, with no Cartesian search over source edges. Edge ties
    are counted in full, and every returned set is recomputed from all rows.
    """
    if type(target) is not int or target <= 0:
        raise ValueError("target must be a positive integer")
    if type(tolerance) is not int or tolerance < 0:
        raise ValueError("tolerance must be a nonnegative integer")
    rows = _modules(modules)
    lower, upper = max(1, target - tolerance), target + tolerance
    if len(rows) < lower:
        raise ValueError("too few modules for a window")
    for transpose in (False, True):
        bounds = [([b[1], b[0], b[3], b[2]] if transpose else b)
                  for b in (row["bounds"] for row in rows)]
        xlow = min(b[0] for b in bounds)
        ylow = min(b[1] for b in bounds)
        yhigh = max(b[3] for b in bounds)
        bands = [(ylow, yhigh)]
        bands.extend((ylow, edge) for edge in
                     _sample_edges([b[3] for b in bounds], 31))
        bands.extend((edge, yhigh) for edge in
                     _sample_edges([b[1] for b in bounds], 32))
        for bottom, top in dict.fromkeys(bands):
            rights = sorted(b[2] for b in bounds if bottom <= b[1] and b[3] <= top)
            if len(rights) < lower:
                continue
            ranks = (target, lower, upper)
            cutoffs = dict.fromkeys(rights[min(rank, len(rights)) - 1] for rank in ranks)
            for right in cutoffs:
                count = bisect_right(rights, right)
                if not lower <= count <= upper:
                    continue
                rect = [xlow, bottom, right, top]
                if transpose:
                    rect = [bottom, xlow, top, right]
                handles = contained_handles(rows, rect)
                if lower <= len(handles) <= upper:
                    return {"rect": guarded_window_rect(rows, rect, handles), "expected_handles": handles}
    raise ValueError("bounded search found no suitable full-bounds window")
