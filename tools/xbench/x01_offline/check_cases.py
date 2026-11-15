"""Independent arithmetic for synthetic X01 fixtures; never emits a grade.

Expected values are hand-authored JSON literals. This program only reads them.
All numerical equality and strict threshold comparisons use absolute EPS.
"""

import argparse
import copy
import itertools
import json
import math
from pathlib import Path
import sys


EPS = 1e-10
ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def compare(actual, expected, path):
    """Compare complete structures, including every named diagnostic and count."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and actual.keys() == expected.keys(),
                f"{path}: keys differ")
        for key in expected:
            compare(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected),
                f"{path}: list length differs")
        for index, (a, e) in enumerate(zip(actual, expected)):
            compare(a, e, f"{path}[{index}]")
    elif isinstance(expected, (int, float)):
        require(isinstance(actual, (int, float)) and math.isfinite(actual)
                and math.isfinite(expected) and abs(actual - expected) <= EPS,
                f"{path}: got {actual!r}, expected {expected!r}")
    else:
        require(actual == expected, f"{path}: got {actual!r}, expected {expected!r}")


def diagnostic(name, *ids):
    return {"name": name, "object_ids": list(ids)}


def ordered_diagnostics(items):
    return sorted(items, key=lambda d: (d["name"], d["object_ids"]))


def unique_objects(objects):
    result = {obj["id"]: obj for obj in objects}
    require(len(result) == len(objects), "duplicate object IDs")
    return result


def world_point(obj):
    x, y = obj["point"]
    for insert in obj.get("inserts", []):
        x = (x - insert["base"][0]) * insert["scale"][0]
        y = (y - insert["base"][1]) * insert["scale"][1]
        angle = math.radians(insert["rotation_deg"])
        c, s = math.cos(angle), math.sin(angle)
        x, y = c * x - s * y, s * x + c * y
        x += insert["insertion"][0]
        y += insert["insertion"][1]
    return [x, y]


def geometry(case):
    original = unique_objects(case["original"])
    export = unique_objects(case["export"])
    require(not (original.keys() & export.keys()), "side IDs must be separate")
    world = {key: world_point(obj) for side in (original, export)
             for key, obj in side.items()}
    distance = lambda a, b: math.dist(world[a], world[b])
    pairs = []
    for handle in sorted({o["handle"] for o in original.values()}):
        a = [key for key, obj in original.items() if obj["handle"] == handle]
        b = [key for key, obj in export.items() if obj["handle"] == handle]
        if len(a) == len(b) == 1:
            pairs.append([a[0], b[0]])
    used_o = {a for a, b in pairs}
    used_e = {b for a, b in pairs}
    edges = sorted((distance(a, b), a, b) for a in original for b in export
                   if a not in used_o and b not in used_e
                   and distance(a, b) <= 0.5 + EPS)
    for _, a, b in edges:
        if a not in used_o and b not in used_e:
            pairs.append([a, b])
            used_o.add(a)
            used_e.add(b)
    moved = [[a, b] for a, b in pairs if distance(a, b) > 0.5 + EPS]
    missing = sorted(original.keys() - used_o)
    extra = sorted(export.keys() - used_e)
    diagnostics = [diagnostic("MOVED", a, b) for a, b in moved]
    diagnostics += [diagnostic("MISSING", a) for a in missing]
    diagnostics += [diagnostic("EXTRA", b) for b in extra]
    return {"world": world, "pairs": sorted(pairs),
            "counts": {"original": len(original), "export": len(export),
                       "matched": len(pairs), "moved": len(moved),
                       "missing": len(missing), "extra": len(extra)},
            "diagnostics": ordered_diagnostics(diagnostics),
            "verdict": "FAIL" if diagnostics else "PASS"}


def rectangle(rect):
    require(len(rect) == 4 and all(math.isfinite(v) for v in rect),
            "invalid rectangle coordinates")
    require(rect[0] < rect[2] and rect[1] < rect[3], "empty rectangle")
    return rect


def contains(rect, x, y):
    return rect[0] < x < rect[2] and rect[1] < y < rect[3]


def fill_area(module, fills):
    """Integrate disjoint coordinate-grid cells; subtract holes per fill."""
    rects = [module]
    for fill in fills:
        outer = rectangle(fill["outer"])
        rects.append(outer)
        for hole in fill["holes"]:
            rectangle(hole)
            require(outer[0] <= hole[0] < hole[2] <= outer[2]
                    and outer[1] <= hole[1] < hole[3] <= outer[3],
                    "hole outside its outer rectangle")
            rects.append(hole)
    xs = sorted({r[i] for r in rects for i in (0, 2)})
    ys = sorted({r[i] for r in rects for i in (1, 3)})
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            x, y = (x0 + x1) / 2, (y0 + y1) / 2
            if contains(module, x, y) and any(
                    contains(f["outer"], x, y)
                    and not any(contains(h, x, y) for h in f["holes"])
                    for f in fills):
                area += (x1 - x0) * (y1 - y0)
    return area


def fill_coverage(case):
    module = case["module"]
    rect = rectangle(module["rect"])
    fills = case["fills"]
    unique_objects([module] + fills)
    module_area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    area = fill_area(rect, fills)
    per_fill = {f["id"]: fill_area(rect, [f]) for f in fills}
    failed = area / module_area > 0.05 + EPS
    offenders = sorted(key for key, value in per_fill.items() if value > EPS)
    return {"module_area": module_area, "fill_union_area_inside_module": area,
            "intersection_area": area, "fraction": area / module_area,
            "threshold": 0.05, "per_fill_intersection_areas": per_fill,
            "diagnostics": [diagnostic("FILL_OVERLAP", module["id"], *offenders)]
            if failed else [], "verdict": "FAIL" if failed else "PASS"}


def corners(obj):
    require(obj["width"] > 0 and obj["height"] > 0, "nonpositive box")
    angle = math.radians(obj["rotation_deg"])
    c, s = math.cos(angle), math.sin(angle)
    cx, cy = obj["center"]
    w, h = obj["width"] / 2, obj["height"] / 2
    return [(cx + c*x - s*y, cy + s*x + c*y)
            for x, y in [(-w, -h), (w, -h), (w, h), (-w, h)]]


def cross(a, b, p):
    return (b[0]-a[0]) * (p[1]-a[1]) - (b[1]-a[1]) * (p[0]-a[0])


def clip_polygon(subject, clip):
    """Sutherland-Hodgman clipping against CCW convex edges."""
    for a, b in zip(clip, clip[1:] + clip[:1]):
        incoming, subject = subject, []
        if not incoming:
            break
        previous = incoming[-1]
        dp = cross(a, b, previous)
        for current in incoming:
            dc = cross(a, b, current)
            # Use geometric half-planes without expanding edges by EPS.
            if (dc >= 0) != (dp >= 0):
                t = dp / (dp - dc)
                subject.append((previous[0] + t*(current[0]-previous[0]),
                                previous[1] + t*(current[1]-previous[1])))
            if dc >= 0:
                subject.append(current)
            previous, dp = current, dc
    return subject


def polygon_area(poly):
    if len(poly) < 3:
        return 0.0
    return abs(sum(a[0]*b[1] - a[1]*b[0]
                   for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def tag_overlaps(case):
    objects = unique_objects(case["objects"])
    diagnostics, pairs = [], []
    for obj in objects.values():
        require(obj["kind"] in ("tag", "module"), "unknown box kind")
        corners(obj)
        if obj["kind"] == "tag":
            require(isinstance(obj["line_count"], int) and obj["line_count"] > 0,
                    "tag requires positive line_count")
            if obj["paper_text_height_mm"] < 1.8 - EPS:
                diagnostics.append(diagnostic("TEXT_TOO_SMALL", obj["id"]))
    for a_id, b_id in itertools.combinations(sorted(objects), 2):
        a, b = objects[a_id], objects[b_id]
        if a["kind"] == b["kind"] == "module":
            continue
        area = polygon_area(clip_polygon(corners(a), corners(b)))
        fraction = area / min(a["width"]*a["height"], b["width"]*b["height"])
        pairs.append({"object_ids": [a_id, b_id], "intersection_area": area,
                      "smaller_box_fraction": fraction})
        if a["kind"] == b["kind"] == "tag":
            if fraction > 0.1 + EPS:
                diagnostics.append(diagnostic("TAG_OVERLAP", a_id, b_id))
        elif area > EPS:
            diagnostics.append(diagnostic("TAG_MODULE_INTERSECTION", a_id, b_id))
    failed = any(d["name"] in ("TAG_OVERLAP", "TEXT_TOO_SMALL")
                 for d in diagnostics)
    return {"pairs": pairs, "diagnostics": ordered_diagnostics(diagnostics),
            "verdict": "FAIL" if failed else "PASS"}


def check_case(case, calculate):
    compare(calculate(case), case["expected"], case["id"])


def load_sets():
    definitions = [("module_geometry_cases.json", "x01.offline.geometry.v1", 8, geometry),
                   ("fill_coverage_cases.json", "x01.offline.fill.v1", 7, fill_coverage),
                   ("tag_overlap_cases.json", "x01.offline.tags.v1", 8, tag_overlaps)]
    sets = []
    for filename, schema, minimum, calculate in definitions:
        with (ROOT / filename).open(encoding="utf-8") as stream:
            data = json.load(stream)
        require(data["schema"] == schema, f"{filename}: wrong schema")
        require(data["conventions"]["comparison_epsilon"] == EPS,
                f"{filename}: epsilon differs")
        require("units" in data, f"{filename}: missing units")
        require(len(data["cases"]) >= minimum, f"{filename}: too few cases")
        unique_objects(data["cases"])
        sets.append((data["cases"], calculate))
    return sets


def rejection_checks(sets):
    """In-memory mutations must be rejected; disk expectations stay untouched."""
    def rejects(case, calculate, label):
        try:
            check_case(case, calculate)
        except AssertionError:
            return
        raise AssertionError(f"negative control accepted: {label}")

    corrupt = copy.deepcopy(sets[0][0][0])
    first_id = next(iter(corrupt["expected"]["world"]))
    corrupt["expected"]["world"][first_id][0] += 1
    rejects(corrupt, geometry, "wrong world coordinate")
    trap = copy.deepcopy(next(c for c in sets[1][0]
                              if c["id"] == "overlapping_fills_union_trap"))
    trap["expected"]["intersection_area"] = 8
    trap["expected"]["fill_union_area_inside_module"] = 8
    trap["expected"]["fraction"] = 0.08
    trap["expected"]["diagnostics"] = [diagnostic("FILL_OVERLAP", "m/1", "f/a", "f/b")]
    trap["expected"]["verdict"] = "FAIL"
    rejects(trap, fill_coverage, "double-counted overlapping fills")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="validate literals and in-memory negative controls (also default)")
    parser.parse_args()
    try:
        sets = load_sets()
        for cases, calculate in sets:
            for case in cases:
                check_case(case, calculate)
        rejection_checks(sets)
    except (AssertionError, ValueError, TypeError, KeyError, OSError) as error:
        print(f"X01 offline cases: FAIL: {error}", file=sys.stderr)
        return 1
    counts = [len(cases) for cases, _ in sets]
    print(f"X01 offline cases: geometry={counts[0]} fill={counts[1]} "
          f"tag={counts[2]}; 2 negative controls; PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
