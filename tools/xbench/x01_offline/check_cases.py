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
MAX_CASE_FILE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_CASES_PER_SET = 256
MAX_JSON_INTEGER_DIGITS = 309


class CaseFileError(ValueError):
    """Read, syntax and resource failures, distinct from semantic case errors."""

    def __init__(self, filename, reason):
        self.diagnostic = dict(name="UNREADABLE_CASE_SET", filename=filename,
                               reason=reason)
        super().__init__(f"UNREADABLE_CASE_SET: {filename}: {reason}")


class GeometryNumericError(ValueError):
    """An arithmetic intermediate cannot be represented as a finite number."""

    def __init__(self, ids, field_path):
        self.diagnostic = dict(name="NONFINITE_GEOMETRY", object_ids=list(ids),
                               field_path=field_path)
        super().__init__(f"NONFINITE_GEOMETRY: {list(ids)}: {field_path}")


def finite_geometry(value, ids, field_path):
    try:
        valid = math.isfinite(value)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise GeometryNumericError(ids, field_path)
    return value


def load_case_file(path):
    """Bound bytes and lexical container depth before invoking the JSON parser."""
    filename = path.name

    def fail(reason):
        raise CaseFileError(filename, reason)

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CASE_FILE_BYTES + 1)
    except OSError:
        fail("READ_ERROR")
    except MemoryError:
        fail("RESOURCE_LIMIT")
    if len(raw) > MAX_CASE_FILE_BYTES:
        fail("FILE_TOO_LARGE")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        fail("INVALID_UTF8")
    if text.startswith("\ufeff"):
        fail("UTF8_BOM")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                fail("JSON_TOO_DEEP")
        elif char in "]}":
            depth -= 1

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def floating(token):
        value = float(token)
        if not math.isfinite(value):
            fail("NONFINITE_JSON_NUMBER")
        return value

    def integer(token):
        if len(token.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
            fail("JSON_INTEGER_TOO_LARGE")
        value = int(token)
        try:
            valid = math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            fail("JSON_INTEGER_TOO_LARGE")
        return value

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_float=floating,
                          parse_int=integer,
                          parse_constant=lambda token: fail("NONFINITE_JSON_NUMBER"))
    except CaseFileError:
        raise
    except (ValueError, UnicodeError):
        fail("MALFORMED_JSON")
    except (MemoryError, RecursionError):
        fail("RESOURCE_LIMIT")
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        if len(data["cases"]) > MAX_CASES_PER_SET:
            fail("TOO_MANY_CASES")
    return data


class CaseInputError(ValueError):
    """Malformed fixture with stable, machine-readable location."""

    def __init__(self, case_id, field_path, name="INVALID_CASE_INPUT"):
        self.case_id, self.field_path, self.name = case_id, field_path, name
        self.diagnostic = dict(name=name, case_id=case_id, field_path=field_path)
        super().__init__(f"{name}: {case_id}: {field_path}")


def validate_case(case, calculate):
    cid = case.get("id", "<top-level>") if isinstance(case, dict) else "<top-level>"
    if not isinstance(cid, str) or not cid:
        cid = "<top-level>"

    def need(ok, path, name="INVALID_CASE_INPUT"):
        if not ok:
            raise CaseInputError(cid, path, name)

    def fields(obj, keys, path):
        need(isinstance(obj, dict), path)
        for key in keys:
            need(key in obj, f"{path}.{key}", "MISSING_FIELD")

    def text(value, path):
        need(isinstance(value, str) and bool(value), path)

    def number(value, path, positive=False):
        need(type(value) in (int, float), path)
        try:
            valid = math.isfinite(value)
        except OverflowError:
            valid = False
        need(valid and (not positive or value > 0), path)

    def array(value, path):
        need(isinstance(value, list), path)

    def vector(value, path, size=2):
        array(value, path)
        need(len(value) == size, path)
        for i, item in enumerate(value):
            number(item, f"{path}[{i}]")

    def rect(value, path):
        vector(value, path, 4)
        need(value[0] < value[2] and value[1] < value[3], path)

    seen = set()

    def object_id(obj, path):
        fields(obj, ["id"], path)
        text(obj["id"], path + ".id")
        need(obj["id"] not in seen, path + ".id", "DUPLICATE_OBJECT_ID")
        seen.add(obj["id"])

    fields(case, ["id", "expected"], "case")
    text(case["id"], "id")
    if calculate is geometry:
        fields(case, ["original", "export"], "case")
        for side in ("original", "export"):
            array(case[side], side)
            for i, obj in enumerate(case[side]):
                path = f"{side}[{i}]"
                object_id(obj, path)
                fields(obj, ["handle", "point"], path)
                text(obj["handle"], path + ".handle")
                vector(obj["point"], path + ".point")
                inserts = obj.get("inserts", [])
                array(inserts, path + ".inserts")
                for j, insert in enumerate(inserts):
                    ip = f"{path}.inserts[{j}]"
                    fields(insert, ["base", "scale", "insertion", "rotation_deg"], ip)
                    for key in ("base", "scale", "insertion"):
                        vector(insert[key], ip + "." + key)
                    need(all(v != 0 for v in insert["scale"]), ip + ".scale")
                    number(insert["rotation_deg"], ip + ".rotation_deg")
    elif calculate is fill_coverage:
        fields(case, ["module", "fills"], "case")
        object_id(case["module"], "module")
        fields(case["module"], ["rect"], "module")
        rect(case["module"]["rect"], "module.rect")
        array(case["fills"], "fills")
        for i, fill in enumerate(case["fills"]):
            path = f"fills[{i}]"
            object_id(fill, path)
            fields(fill, ["outer", "holes"], path)
            rect(fill["outer"], path + ".outer")
            array(fill["holes"], path + ".holes")
            for j, hole in enumerate(fill["holes"]):
                hp = f"{path}.holes[{j}]"
                rect(hole, hp)
                outer = fill["outer"]
                need(outer[0] <= hole[0] < hole[2] <= outer[2]
                     and outer[1] <= hole[1] < hole[3] <= outer[3], hp)
    elif calculate is tag_overlaps:
        fields(case, ["objects"], "case")
        array(case["objects"], "objects")
        for i, obj in enumerate(case["objects"]):
            path = f"objects[{i}]"
            object_id(obj, path)
            fields(obj, ["kind", "center", "width", "height", "rotation_deg", "line_count"], path)
            need(obj["kind"] in ("tag", "module"), path + ".kind")
            vector(obj["center"], path + ".center")
            for key in ("width", "height", "rotation_deg"):
                number(obj[key], path + "." + key, key != "rotation_deg")
            need(type(obj["line_count"]) is int and
                 obj["line_count"] >= (1 if obj["kind"] == "tag" else 0), path + ".line_count")
            if obj["kind"] == "tag":
                fields(obj, ["paper_text_height_mm"], path)
            if "paper_text_height_mm" in obj:
                number(obj["paper_text_height_mm"], path + ".paper_text_height_mm", True)

    def expected_tree(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                expected_tree(item, path + "." + key)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                expected_tree(item, f"{path}[{i}]")
        elif type(value) in (int, float):
            number(value, path)
        else:
            need(isinstance(value, str), path)

    def record(value, keys, path):
        fields(value, keys, path)
        need(value.keys() == set(keys), path)

    def strings(value, path, size=None):
        array(value, path)
        if size is not None:
            need(len(value) == size, path)
        for i, item in enumerate(value):
            text(item, f"{path}[{i}]")

    def mapping(value, path, validate):
        need(isinstance(value, dict), path)
        for key, item in value.items():
            text(key, path)
            validate(item, path + "." + key)

    expected_tree(case["expected"], "expected")
    # Validate the authored output schema independently of calculated values.
    # Variable maps and lists may disagree with the inputs/results: compare()
    # must report those disagreements as failed expectations, not bad inputs.
    expected = case["expected"]
    common = ["diagnostics", "verdict"]
    if calculate is geometry:
        record(expected, common + ["world", "pairs", "counts"], "expected")
        mapping(expected["world"], "expected.world", vector)
        array(expected["pairs"], "expected.pairs")
        for i, pair in enumerate(expected["pairs"]):
            strings(pair, f"expected.pairs[{i}]", 2)
        counts = ("original", "export", "matched", "moved", "missing", "extra")
        record(expected["counts"], counts, "expected.counts")
        for key in counts:
            number(expected["counts"][key], "expected.counts." + key)
    elif calculate is fill_coverage:
        numeric = ("module_area", "fill_union_area_inside_module", "intersection_area",
                   "fraction", "threshold")
        record(expected, common + list(numeric) + ["per_fill_intersection_areas"], "expected")
        for key in numeric:
            number(expected[key], "expected." + key)
        mapping(expected["per_fill_intersection_areas"],
                "expected.per_fill_intersection_areas", number)
    elif calculate is tag_overlaps:
        record(expected, common + ["pairs"], "expected")
        array(expected["pairs"], "expected.pairs")
        for i, pair in enumerate(expected["pairs"]):
            path = f"expected.pairs[{i}]"
            record(pair, ["object_ids", "intersection_area", "smaller_box_fraction"], path)
            strings(pair["object_ids"], path + ".object_ids", 2)
            for key in ("intersection_area", "smaller_box_fraction"):
                number(pair[key], path + "." + key)
    array(expected["diagnostics"], "expected.diagnostics")
    for i, item in enumerate(expected["diagnostics"]):
        path = f"expected.diagnostics[{i}]"
        record(item, ["name", "object_ids"], path)
        text(item["name"], path + ".name")
        strings(item["object_ids"], path + ".object_ids")
    text(expected["verdict"], "expected.verdict")


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
    ids = [obj["id"]]
    x = finite_geometry(x, ids, "point[0]")
    y = finite_geometry(y, ids, "point[1]")
    for index, insert in enumerate(obj.get("inserts", [])):
        path = f"inserts[{index}]"

        def finite(value, field):
            return finite_geometry(value, ids, path + "." + field)

        # Check each operation separately: a subsequent tiny scale or insert
        # must not conceal an overflowing subtraction or rotation sum.
        x = finite(x - insert["base"][0], "base.subtract[0]")
        y = finite(y - insert["base"][1], "base.subtract[1]")
        x = finite(x * insert["scale"][0], "scale[0]")
        y = finite(y * insert["scale"][1], "scale[1]")
        angle = finite(math.radians(insert["rotation_deg"]), "rotation_deg.radians")
        c, s = math.cos(angle), math.sin(angle)
        cx, sy = finite(c * x, "rotation.cos_x"), finite(s * y, "rotation.sin_y")
        sx, cy = finite(s * x, "rotation.sin_x"), finite(c * y, "rotation.cos_y")
        x = finite(cx - sy, "rotation.subtract[0]")
        y = finite(sx + cy, "rotation.add[1]")
        x = finite(x + insert["insertion"][0], "insertion[0]")
        y = finite(y + insert["insertion"][1], "insertion[1]")
    return [x, y]


def geometry(case):
    original = unique_objects(case["original"])
    export = unique_objects(case["export"])
    require(not (original.keys() & export.keys()), "side IDs must be separate")
    world = {key: world_point(obj) for side in (original, export)
             for key, obj in side.items()}
    def distance(a, b):
        # hypot avoids spurious overflow from squaring large finite deltas.
        dx = finite_geometry(world[a][0] - world[b][0], [a, b], "distance.delta[0]")
        dy = finite_geometry(world[a][1] - world[b][1], [a, b], "distance.delta[1]")
        return finite_geometry(math.hypot(dx, dy), [a, b], "distance")
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
    validate_case(case, calculate)
    compare(calculate(case), case["expected"], case["id"])


def load_sets(root=None):
    definitions = [("module_geometry_cases.json", "x01.offline.geometry.v1", 8, geometry),
                   ("fill_coverage_cases.json", "x01.offline.fill.v1", 7, fill_coverage),
                   ("tag_overlap_cases.json", "x01.offline.tags.v1", 8, tag_overlaps)]
    sets = []
    for filename, schema, minimum, calculate in definitions:
        def top(ok, path, name="INVALID_CASE_INPUT"):
            if not ok:
                raise CaseInputError("<top-level>", path, name)

        data = load_case_file((Path(root) if root is not None else ROOT).joinpath(filename))
        top(isinstance(data, dict), filename)
        top(data.get("schema") == schema, "schema", "INVALID_SCHEMA")
        units = {geometry: "inch", fill_coverage: {"length": "inch", "area": "square inch"},
                 tag_overlaps: {"length": "paper millimetre", "area": "square paper millimetre"}}
        top(data.get("units") == units[calculate], "units")
        conventions = data.get("conventions")
        top(isinstance(conventions, dict), "conventions")
        numeric = {"comparison_epsilon": EPS}
        texts = {geometry: ("point", "transform", "matching", "diagnostics"),
                 fill_coverage: ("rectangle", "scope", "coverage", "failure"),
                 tag_overlaps: ("box", "intersection", "failure", "epsilon_policy")}
        if calculate is geometry:
            numeric["tolerance_inch"] = 0.5
        elif calculate is fill_coverage:
            numeric["threshold"] = 0.05
        else:
            numeric.update(overlap_threshold=0.1, minimum_text_height_mm=1.8)
        for key, value in numeric.items():
            top(type(conventions.get(key)) in (int, float) and conventions[key] == value,
                "conventions." + key)
        for key in texts[calculate]:
            top(isinstance(conventions.get(key), str) and bool(conventions[key]), "conventions." + key)
        top(isinstance(data.get("cases"), list), "cases")
        seen = set()
        for case in data["cases"]:
            validate_case(case, calculate)
            if case["id"] in seen:
                raise CaseInputError(case["id"], "id", "DUPLICATE_CASE_ID")
            seen.add(case["id"])
        top(len(data["cases"]) >= minimum, "cases")
        sets.append((data["cases"], calculate))
    return sets


def rejection_checks(sets):
    """In-memory mutations must be rejected; disk expectations stay untouched."""
    def rejects(case, calculate, label):
        try:
            check_case(case, calculate)
        except (AssertionError, CaseInputError):
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
    parser.add_argument("--cases-dir", type=Path)
    args = parser.parse_args()
    case = {}
    try:
        sets = load_sets(args.cases_dir)
        for cases, calculate in sets:
            for case in cases:
                check_case(case, calculate)
        rejection_checks(sets)
    except (CaseFileError, GeometryNumericError) as error:
        print(json.dumps(dict(verdict="UNREADABLE", diagnostics=[error.diagnostic]), sort_keys=True))
        return 4
    except CaseInputError as error:
        print(json.dumps(dict(verdict="FAIL", diagnostics=[error.diagnostic]), sort_keys=True))
        return 1
    except (AssertionError, ValueError, TypeError, KeyError, OSError, OverflowError) as error:
        print(json.dumps(dict(verdict="FAIL", diagnostics=[dict(
            name="CASE_CHECK_FAILED", case_id=case.get("id", "<top-level>"),
            field_path="expected", message=str(error))]), sort_keys=True))
        return 1
    counts = [len(cases) for cases, _ in sets]
    print(f"X01 offline cases: geometry={counts[0]} fill={counts[1]} "
          f"tag={counts[2]}; 2 negative controls; PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
