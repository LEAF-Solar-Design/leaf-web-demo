"""List drawing-derived module candidates and count an explicit selection.

Candidates are not design-role claims. The user selects the intended set from
the listing; the normal job records that selection with the result.
"""
from __future__ import annotations

import json
import re
import math
from fractions import Fraction

MAX_EXACT_COUNT = 2**53 - 1


class InputValidationError(ValueError):
    """An actionable input diagnostic safe to return through the tool API."""


def _module_total(weights):
    total = sum(weights)
    if total > MAX_EXACT_COUNT:
        raise InputValidationError("Module count exceeds the JavaScript safe integer limit; select fewer sets or split the drawing.")
    return total


def _model(entity):
    return entity.get("space") != "paper" and entity.get("layout", "Model") in ("", "Model")


def _selection_region(params):
    bounds = params.get("region")
    legacy = params.get("selection_window")
    if bounds is not None and legacy is not None and bounds != legacy:
        raise ValueError("Conflicting region and legacy design window. Supply one selection region.")
    if bounds is None:
        bounds = legacy
    if bounds is None:
        return None
    if not isinstance(bounds, list) or len(bounds) != 4:
        raise ValueError("Design window region requires [minimum X, minimum Y, maximum X, maximum Y].")
    try:
        valid = all(type(value) in (int, float) and math.isfinite(value) for value in bounds)
    except OverflowError:
        valid = False
    if not valid or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("Design window region coordinates must be finite with minimums below maximums.")
    return bounds


def _point3(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError("Region containment requires complete finite instance geometry.")
    point = list(value) + ([0] if len(value) == 2 else [])
    try:
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in point)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Region containment requires complete finite instance geometry.")
    return point



def _unit_normal(value):
    point = _point3(value)
    scale = max(abs(v) for v in point)
    if scale == 0:
        raise ValueError("Region containment requires a nonzero plane normal.")
    scaled = [v / scale for v in point]
    magnitude = math.hypot(*scaled)
    return [v / magnitude for v in scaled]


def _definition_points(name, definitions, cache, ancestors):
    from dxf_intake import _ocs_to_wcs

    definition = definitions.get(name)
    if (not isinstance(definition, dict) or definition.get("nested_inserts_incomplete")
            or not (definition.get("complete") is True or definition.get("region_curve_controls")
                    or definition.get("nested_inserts"))):
        raise ValueError("Region containment requires a complete block definition. Re-import the drawing.")
    children = definition.get("children")
    if (not isinstance(children, list) or not children
            or definition.get("count", len(children)) != len(children)):
        raise ValueError("Region containment requires all block children. Re-import the drawing.")
    nested_inserts = definition.get("nested_inserts", [])
    curve_controls = definition.get("region_curve_controls", {})
    if not isinstance(nested_inserts, list) or not isinstance(curve_controls, dict):
        raise ValueError("Region containment requires valid block evidence. Re-import the drawing.")
    advanced_geometry = bool(nested_inserts or curve_controls)
    base = _point3(definition.get("measurement_base", definition.get("base"))
                   if advanced_geometry or len(ancestors) > 1 else definition.get("base"))
    refs = iter(nested_inserts)
    points = []
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError("Region containment requires complete finite instance geometry.")
        kind = child.get("kind")
        if kind == "OTHER" and child.get("type") == "INSERT":
            insert = next(refs, None)
            if (not isinstance(insert, dict)
                    or any(type(insert.get(key, 1)) is not int or insert.get(key, 1) != 1
                           for key in ("rows", "columns"))):
                raise ValueError("Region containment requires individual nested INSERT references.")
            child_points = _insert_points(insert, definitions, cache, ancestors)
        elif kind == "OTHER" and child.get("type") in ("HELIX", "SPLINE"):
            # An affine image of a positive-weight spline lies inside the
            # convex hull of the transformed control points.
            raw = curve_controls.get(str(index))
            if not isinstance(raw, list) or not 2 <= len(raw) <= 10000:
                raise ValueError("Region containment requires a complete curve control net. Re-import the drawing.")
            child_points = [_point3(point) for point in raw]
        elif kind in ("POINT", "LINE", "LWPOLYLINE"):
            raw = [child.get("pt")] if kind == "POINT" else child.get("pts")
            if not isinstance(raw, list) or len(raw) < (1 if kind == "POINT" else 2):
                raise ValueError("Region containment requires complete finite instance geometry.")
            child_points = [_point3(point) for point in raw]
        else:
            raise ValueError("Region containment requires complete straight-edge block geometry.")
        if kind == "LWPOLYLINE":
            normal = _unit_normal(child.get("nrm", [0, 0, 1]))
            elevation = _point3([child.get("elev", 0), 0, 0])[0]
            if child.get("thickness", 0) != 0 or any(child.get("bulges", [])):
                raise ValueError("Region containment requires complete straight-edge block geometry.")
            child_points = [_ocs_to_wcs([p[0], p[1], elevation], normal) for p in child_points]
        if (advanced_geometry or len(ancestors) > 1) and len(points) + len(child_points) > 10000:
            raise ValueError("Region containment exceeded the block geometry limit.")
        points.extend(tuple(p[i] - base[i] for i in range(3)) for p in child_points)
    exhausted = object()
    if next(refs, exhausted) is not exhausted:
        raise ValueError("Region containment requires consistent nested INSERT evidence. Re-import the drawing.")
    return points, advanced_geometry


def _insert_points(entity, definitions, cache, ancestors=(), source_placements=None):
    from dxf_intake import _ocs_to_wcs

    name = entity.get("name")
    if not isinstance(name, str):
        raise ValueError("Region containment requires a named block reference.")
    if name not in definitions and ancestors:
        name = next((key for key in definitions
                     if isinstance(key, str) and key.casefold() == name.casefold()), name)
    if name in ancestors or len(ancestors) >= 32:
        raise ValueError("Region containment found an invalid or too deeply nested block reference.")
    cache_key = (name, bool(ancestors))
    if cache_key not in cache:
        cache[cache_key] = _definition_points(name, definitions, cache, (*ancestors, name))
    definition_points, advanced_geometry = cache[cache_key]
    if advanced_geometry or ancestors:
        if entity.get("measurement_placement_invalid"):
            raise ValueError("Region containment requires a valid instance transform. Re-import the drawing.")
        placement = entity.get("measurement_placement")
        if source_placements is not None:
            matches = source_placements.get(entity.get("handle"), [])
            if len(matches) > 1:
                raise ValueError("Region containment requires an unambiguous instance transform.")
            if matches:
                placement = matches[0]
        if isinstance(placement, dict):
            entity = dict(entity, **placement)
    origin = _point3([entity.get("x"), entity.get("y"), entity.get("z", 0)])
    scale = _point3(entity.get("scale", [1, 1, 1]))
    normal = _unit_normal(entity.get("nrm", [0, 0, 1]))
    rotation = _point3([entity.get("rot", 0), 0, 0])[0]
    if not all(scale):
        raise ValueError("Region containment requires a finite instance transform.")
    cosine, sine = math.cos(rotation), math.sin(rotation)
    points = []
    for point in definition_points:
        x, y, z = (point[i] * scale[i] for i in range(3))
        rotated = [x * cosine - y * sine, x * sine + y * cosine, z]
        world = _ocs_to_wcs(rotated, normal)
        points.append(_point3([origin[i] + world[i] for i in range(3)]))
    return points


def _within_region(entity, key, bounds, dimensions, definitions, cache, source_placements):
    if key == "inserts":
        if dimensions != (1, 1):
            raise ValueError("Design windows require individual INSERT references. Count an INSERT array as a whole set.")
        points = _insert_points(entity, definitions, cache, source_placements=source_placements)
    else:
        points = [_point3(point) for point in entity["pts"]]
    left, bottom = min(p[0] for p in points), min(p[1] for p in points)
    right, top = max(p[0] for p in points), max(p[1] for p in points)
    if bounds[0] <= left and right <= bounds[2] and bounds[1] <= bottom and top <= bounds[3]:
        return True
    if right <= bounds[0] or left >= bounds[2] or top <= bounds[1] or bottom >= bounds[3]:
        return False
    raise ValueError("Ambiguous region boundary overlaps a module instance. Move the boundary clear of its complete bounds.")


def _four_corners(entity):
    closed = entity.get("closed")
    # Saved closure accepts booleans and the exact integer flag, never truthiness.
    if not (closed is True or (type(closed) is int and closed == 1)):
        return False
    points = entity.get("pts") or []
    if len(points) == 5 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) != 4:
        return False
    try:
        bulges = entity.get("bulges", [])
        if not isinstance(bulges, (list, tuple)) or any(not math.isfinite(v) or v != 0 for v in bulges):
            return False
        thickness = entity.get("thickness", 0)
        if not math.isfinite(thickness) or thickness != 0:
            return False
        normal = entity.get("normal", [0, 0, 1])
        if len(normal) != 3 or not all(math.isfinite(v) for v in normal) or not any(normal):
            return False
        if any(len(p) < 2 or any(not math.isfinite(v) for v in p) for p in points):
            return False
        xyz = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in points]
        local3 = [tuple(p[j] - xyz[0][j] for j in range(3)) for p in xyz]
        a, b = local3[1:3]
        normal = (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])
        magnitude = math.hypot(*normal)
        if not math.isfinite(magnitude) or magnitude == 0:
            return False
        scale = max(abs(v) for p in local3 for v in p)
        # Floating roundoff allowance only, relative to the local extent.
        distance = math.fsum((local3[3][j] / scale) * (normal[j] / magnitude) for j in range(3))
        if not math.isfinite(distance) or abs(distance) > 64 * math.ulp(1.0):
            return False
        drop = max(range(3), key=lambda j: abs(normal[j]))
        points = [tuple(p[j] for j in range(3) if j != drop) for p in xyz]
        if len({(p[0], p[1]) for p in points}) != 4:
            return False
        turns = []
        for i in range(4):
            a, b, c = points[i - 1], points[i], points[(i + 1) % 4]
            turn = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if not math.isfinite(turn) or turn == 0:
                return False
            turns.append(turn > 0)
        if len(set(turns)) != 1:
            return False
        origin_x, origin_y = points[0][:2]
        local = [(p[0] - origin_x, p[1] - origin_y) for p in points]
        area = sum(local[i][0] * local[(i + 1) % 4][1]
                   - local[(i + 1) % 4][0] * local[i][1] for i in range(4))
        return math.isfinite(area) and abs(area) > 0
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


def _named_panel_sets(sets, scope):
    """Use explicit block roles only. An unresolved group leaves the picker open."""
    if scope not in ("roof_layers", "all_layers"):
        raise ValueError("Unknown panel layer scope.")
    found = []
    block_names = set()
    for group in sets:
        layer_words = re.findall(r"[a-z0-9]+", group["layer"].casefold())
        if scope == "roof_layers" and not {"roof", "rooftop"}.intersection(layer_words):
            continue
        words = re.findall(r"[a-z0-9]+", group["block"].casefold())
        if group["entity_type"] == "INSERT" and re.fullmatch(
            r"(?:(?:pv|solar|photovoltaic) )?(?:panels?|modules?)", " ".join(words)
        ):
            block_names.add(group["block"])
            found.append(group["set_id"])
        elif words not in (["outline"], ["roof", "outline"], ["roof", "boundary"]):
            return []
    return found if len(block_names) == 1 else []


def _selected_layer_counts(selected_sets, multiplicities):
    """Count physical entities once within each selected layer."""
    by_layer = {}
    for group in selected_sets:
        by_layer.setdefault(group["layer"], set()).update(group["_keys"])
    assigned = set()
    counts = {}
    for layer, keys in sorted(by_layer.items()):
        if assigned.intersection(keys):
            raise InputValidationError("A selected entity belongs to more than one layer. Check the drawing before counting by layer.")
        assigned.update(keys)
        counts[layer] = _module_total(multiplicities[key][0] * multiplicities[key][1] for key in keys)
    return counts


def _rank_layers(counts, limit):
    """Include every layer tied with the layer in the final requested slot."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    cutoff = ordered[min(limit, len(ordered)) - 1][1]
    return [{"layer": layer, "count": count} for layer, count in ordered if count >= cutoff]


def _cumulative_shares(counts):
    total = _module_total(counts.values())
    running = 0
    rows = []
    for layer, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        running += count
        rows.append({"layer": layer, "numerator": running, "denominator": total,
                     "percentage": 100 * running / total})
    return rows

def _coverage(counts, percent, excluded_layers=None):
    from fractions import Fraction
    from itertools import combinations

    target = Fraction(str(percent))
    if target < 0:
        raise InputValidationError("Choose a nonnegative coverage percentage.")
    if excluded_layers is not None:
        excluded = {name.casefold() for name in excluded_layers}
        selected = {name.casefold() for name in counts}
        if excluded - selected:
            raise InputValidationError("An excluded layer is absent from the coverage selection. Check the layer names.")
        counts = {name: count for name, count in counts.items() if name.casefold() not in excluded}
        if not counts and target > 0:
            return {"reachable": False, "solutions": []}
    required = target * _module_total(counts.values()) / 100
    weights = sorted(counts.values(), reverse=True)
    size = 0
    covered = 0
    while size < len(weights) and covered < required:
        covered += weights[size]
        size += 1
    if covered < required:
        return {"reachable": False, "solutions": []}
    if math.comb(len(counts), size) > 100000:
        raise InputValidationError("Too many layer combinations to enumerate. Narrow the selected layers.")
    solutions = [list(group) for group in combinations(sorted(counts), size)
                 if sum(counts[layer] for layer in group) >= required]
    return {"reachable": True, "solutions": solutions}

def _compare_selections(intake, spec):
    from fractions import Fraction

    result, _ = _run(intake, {})
    field = spec["by"]
    if field not in ("block", "set_id"):
        raise InputValidationError("Choose listed block names or set IDs.")
    available = {group[field] for group in result["sets"]}
    operands = []
    for side in ("before", "after"):
        requested = set(spec[side])
        if requested - available:
            raise InputValidationError("A comparison selection is absent. Check the drawing's listed sets.")
        ids = [group["set_id"] for group in result["sets"] if group[field] in requested]
        operand, _ = _run(intake, {"set_ids": ids, "group_by": "layer"})
        operands.append((operand.get("counts", {}), operand["count"] or 0))
    (before, first), (after, second) = operands
    changes = {}
    for layer in sorted(before.keys() | after.keys()):
        value = (100 * (Fraction(after.get(layer, 0), second) - Fraction(before.get(layer, 0), first))
                 if first and second else None)
        changes[layer] = None if value is None else {"numerator": value.numerator,
                                                     "denominator": value.denominator}
    result["schema_version"] = 1
    result["share_change"] = {"totals": [first, second], "percentage_points": changes,
                              "defined": bool(first and second)}
    result["selection_required"] = False
    return result, None

def run(intake, params):
    params = params or {}
    excluded_tiers = params.get("exclude_highest_count_tiers")
    if excluded_tiers is not None and (
        type(excluded_tiers) is not int or excluded_tiers < 1 or params.get("rank_windows") is None
    ):
        raise InputValidationError("Choose a positive number of count tiers to exclude from each rank window.")
    if params.get("compare_selections") is not None:
        if any(v is not None for k, v in params.items() if k != "compare_selections"):
            raise InputValidationError("Use the two comparison selections on their own.")
        return _compare_selections(intake, params["compare_selections"])
    if params.get("rank_windows") is None:
        return _run(intake, params)
    windows = params["rank_windows"]
    if not isinstance(windows, list) or not 2 <= len(windows) <= 8 or _selection_region(params) is not None:
        raise InputValidationError("Choose two windows or an ordered series of up to eight windows.")
    for bounds in windows:
        if _selection_region({"region": bounds}) is None:
            raise InputValidationError("Choose complete windows for the comparison.")
    base, overlay = _run(intake, dict(params, group_by="layer"))
    base["schema_version"] = 1
    if not base["selected_sets"]:
        return base, overlay
    ids = [s["set_id"] for s in base["selected_sets"]]
    ranks = []
    for bounds in windows:
        scoped, _ = _run(
            intake, {"region": bounds, "set_ids": ids, "group_by": "layer",
                     "visible_layers_only": params.get("visible_layers_only", False)},
            allow_absent_in_window=True,
        )
        counts = scoped.get("counts", {})
        if excluded_tiers is not None:
            excluded_counts = set(sorted(set(counts.values()), reverse=True)[:excluded_tiers])
            counts = {layer: count for layer, count in counts.items() if count not in excluded_counts}
        rank_by_layer = {}
        previous = None
        for index, (layer, count) in enumerate(sorted(counts.items(), key=lambda item: -item[1]), 1):
            if count != previous:
                place = index
                previous = count
            rank_by_layer[layer] = place
        ranks.append(rank_by_layer)
    layers = sorted({layer for window_rank in ranks for layer in window_rank})
    window_ranks = {layer: [rank.get(layer) for rank in ranks] for layer in layers}
    base["window_ranks"] = window_ranks
    if len(windows) > 2:
        base["consistently_rising_layers"] = [
            layer for layer, places in window_ranks.items()
            if None not in places and all(previous > current for previous, current in zip(places, places[1:]))
        ]
    return base, overlay


def _run(intake, params, *, allow_absent_in_window=False):
    excluded_layers = params.get("coverage_excluded_layers")
    if excluded_layers is not None and (
        params.get("coverage_percent") is None
        or params.get("group_by") != "layer"
        or not isinstance(excluded_layers, list)
        or not excluded_layers
        or any(not isinstance(name, str) or not name.strip() for name in excluded_layers)
    ):
        raise InputValidationError("Supply named excluded layers with a coverage percentage and a layer breakdown.")
    top = params.get("top_layers")
    if "top_layers" in params and (type(top) is not int or top < 1):
        raise InputValidationError("Choose a positive integer for the number of top layers.")
    share_threshold = params.get("layer_share_above")
    if "layer_share_above" in params and (
        type(share_threshold) not in (float, int)
        or not 0 <= share_threshold <= 100
        or params.get("group_by") != "layer"
    ):
        raise InputValidationError("Choose a layer share threshold from 0 to 100 and group by layer.")
    count_threshold = params.get("layer_count_at_least")
    if "layer_count_at_least" in params and (
        type(count_threshold) is not int
        or count_threshold < 0
        or params.get("group_by") != "layer"
    ):
        raise InputValidationError("Choose a nonnegative integer layer count threshold and group by layer.")
    highlight_ranked = params.get("highlight_ranked_layers", False)
    if type(highlight_ranked) is not bool or (highlight_ranked and top is None):
        raise InputValidationError("Ranked-layer highlights require a top layer ranking.")
    window = _selection_region(params)
    if not isinstance(intake, dict):
        raise InputValidationError("Saved intake must be an object; re-import the drawing.")
    visible_only = params.get("visible_layers_only", False)
    if type(visible_only) is not bool:
        raise InputValidationError("Choose whether to count visible layers only.")
    hidden_names = intake.get("hidden_layers", []) if visible_only else []
    if not isinstance(hidden_names, list) or any(not isinstance(name, str) for name in hidden_names):
        raise InputValidationError("Saved drawing layer visibility is incompatible; re-import the drawing.")
    hidden_layers = {name.casefold() for name in hidden_names}
    for key in ("inserts", "polylines"):
        if key in intake and not isinstance(intake[key], list):
            raise InputValidationError(f"Saved intake {key} must be an array; re-import the drawing.")
        for row in intake.get(key, []):
            if not isinstance(row, dict) or (key == "polylines" and not isinstance(row.get("pts"), list)):
                raise InputValidationError(f"Saved intake {key} has an incompatible candidate row; re-import the drawing.")
            for field in ("layer", "name", "handle", "space", "layout"):
                if field in row and not isinstance(row[field], str):
                    raise InputValidationError(f"Saved intake {key} candidate {field} must be a string; re-import the drawing.")
    selected_ids = list(dict.fromkeys(params.get("set_ids") or
        ([params["set_id"]] if params.get("set_id") else [])))
    shares = params.get("layer_shares", False)
    cumulative = params.get("cumulative_shares", False)
    if type(cumulative) is not bool or (cumulative and params.get("group_by") != "layer"):
        raise InputValidationError("Cumulative shares require grouping by layer.")
    names = params.get("layer_names")
    if type(shares) is not bool or (shares and params.get("group_by") != "layer"):
        raise InputValidationError("Layer shares require grouping by layer.")
    if names is not None and (not isinstance(names, list) or not names or
            not all(isinstance(name, str) and name.strip() for name in names) or
            selected_ids or not shares or "panel_scope" not in params):
        raise InputValidationError("Choose named panel layers or explicit module sets.")
    auto_shares = shares and "panel_scope" in params and not selected_ids
    if auto_shares:
        listing, _ = run(intake, {"visible_layers_only": visible_only})
        candidates = listing["sets"]
        if names is not None:
            available = {group["layer"] for group in candidates}
            resolved = set()
            for name in names:
                name = name.strip()
                matches = {label for label in available if label.casefold() == name.casefold()}
                if name in available:
                    resolved.add(name)
                elif len(matches) == 1:
                    resolved.update(matches)
                else:
                    raise InputValidationError("A requested panel layer is absent or ambiguous. Check the layer names.")
            names = resolved
            candidates = [group for group in candidates if group["layer"] in names]
            if {group["layer"] for group in candidates} != set(names):
                raise InputValidationError("A requested panel layer is absent. Check the layer names.")
        selected_ids = _named_panel_sets(candidates, params["panel_scope"])
        if not selected_ids:
            return listing, None
    if window is not None and not selected_ids:
        raise ValueError("Select module sets before applying a design window region.")
    definitions = intake.get("blocks", {})
    source_placements = intake.get("beam_insert_placements", {})
    if window is not None and not isinstance(definitions, dict):
        raise ValueError("Region containment requires a block-definition map. Re-import the drawing.")
    geometry_cache = {}
    groups = {}
    multiplicities = {}
    for entity_type, key in (("INSERT", "inserts"), ("LWPOLYLINE", "polylines")):
        for index, entity in enumerate(intake.get(key) or []):
            if not _model(entity) or (key == "polylines" and not _four_corners(entity)):
                continue
            layer = entity.get("layer") or "0"
            if hidden_layers and layer.casefold() in hidden_layers:
                continue
            block = (entity.get("name") or "") if key == "inserts" else ""
            identity = (entity_type, layer, block)
            handle = str(entity.get("handle") or "").strip().upper().removeprefix("0X")
            if handle and all(c in "0123456789ABCDEF" for c in handle):
                handle = handle.lstrip("0") or "0"
            entity_key = ("handle", handle) if handle else ("row", key, index)
            dimensions = (entity.get("rows", 1), entity.get("columns", 1)) if key == "inserts" else (1, 1)
            if any(type(value) is not int or not 1 <= value <= 65535 for value in dimensions):
                raise InputValidationError("INSERT has invalid array multiplicity; rows and columns must be positive UInt16 integers.")
            selected = json.dumps(identity, ensure_ascii=True, separators=(",", ":")) in selected_ids
            if (window is not None and selected
                    and not _within_region(entity, key, window, dimensions, definitions, geometry_cache,
                                           source_placements)):
                continue
            group = groups.setdefault(identity, {"entity_type": entity_type,
                "layer": layer, "block": block, "count": 0, "handles": [], "_keys": set()})
            if entity_key in multiplicities and multiplicities[entity_key] != dimensions:
                raise InputValidationError("INSERT has conflicting array multiplicity; re-import the drawing.")
            multiplicities[entity_key] = dimensions
            if entity_key not in group["_keys"]:
                group["_keys"].add(entity_key)
                group["count"] = _module_total((group["count"], dimensions[0] * dimensions[1]))
                if handle:
                    group["handles"].append(handle)
    sets = []
    for identity, group in sorted(groups.items()):
        group["set_id"] = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
        sets.append(group)
    if not selected_ids and (params.get("group_by") == "layer" or top is not None) and "panel_scope" in params:
        selected_ids = _named_panel_sets(sets, params["panel_scope"])
    selected_sets = [s for s in sets if s["set_id"] in selected_ids]
    if (len(selected_sets) != len(selected_ids) and not allow_absent_in_window
            and not (auto_shares and window is not None)):
        if window is not None:
            raise ValueError("A selected module set has no candidates in this design window. Check the window and list the sets again.")
        raise ValueError("Selected module set is absent from this drawing; list the sets again.")
    selected = selected_sets[0] if len(selected_sets) == 1 else None
    visible_selection_empty = visible_only and not sets and not selected_ids
    breakdown = {}
    if selected_sets and (params.get("group_by") == "layer" or top is not None):
        counts = _selected_layer_counts(selected_sets, multiplicities)
        breakdown["counts"] = counts
        if cumulative:
            breakdown["cumulative_shares"] = _cumulative_shares(counts)
        if params.get("coverage_percent") is not None:
            breakdown["coverage"] = _coverage(counts, params["coverage_percent"], excluded_layers)
        if top is not None:
            breakdown["ranking"] = _rank_layers(counts, top)

        if shares or share_threshold is not None:
            total = _module_total(counts.values())
            layer_shares = [
                {"layer": layer, "numerator": count, "denominator": total,
                 "percentage": 100 * count / total}
                for layer, count in counts.items()
            ]
            if shares:
                breakdown["shares"] = layer_shares
            if share_threshold is not None:
                threshold = Fraction(str(share_threshold)) * total
                breakdown["layers_above_share"] = [
                    row for row in layer_shares if 100 * row["numerator"] > threshold
                ]

    def public_set(group):
        return {k: v for k, v in group.items() if k not in ("handles", "_keys")}

    highlighted_sets = selected_sets
    if highlight_ranked and selected_sets:
        ranked_layers = {row["layer"] for row in breakdown["ranking"]}
        highlighted_sets = [group for group in selected_sets if group["layer"] in ranked_layers]
    if share_threshold is not None and selected_sets:
        qualifying_layers = {row["layer"] for row in breakdown["layers_above_share"]}
        highlighted_sets = [group for group in highlighted_sets if group["layer"] in qualifying_layers]
    if count_threshold is not None and selected_sets:
        highlighted_sets = [
            group for group in highlighted_sets
            if breakdown["counts"][group["layer"]] >= count_threshold
        ]

    return ({"sets": [public_set(s) for s in sets],
             **({"region": list(window)} if window is not None else {}),
             "original_bytes_integrity": "unverified",
             "selection_required": not selected_sets and not visible_selection_empty,
             **breakdown,
             "selected_sets": [public_set(s) for s in selected_sets],
             "selected_set": public_set(selected) if selected else None,
             "count": _module_total(multiplicities[key][0] * multiplicities[key][1]
                          for key in set().union(*(s["_keys"] for s in selected_sets)))
                 if selected_sets or visible_selection_empty else None},
            {"highlight_handles": list(dict.fromkeys(h for s in highlighted_sets for h in s["handles"]))}
            if selected_sets else None)
