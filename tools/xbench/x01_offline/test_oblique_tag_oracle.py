"""Bounded synthetic oblique oracle; no production geometry builds expectations.

Exact inside vertices and segment intersections are ordered by a monotone hull,
then integrated by shoelace. At most 64 cases, four rectangles, six pairs and
16 segment pairs per intersection; hull input has at most 24 points.
"""

import copy
from fractions import Fraction as F
import itertools
import math
import unittest

try:
    from . import check_cases as checker
except ImportError:
    import check_cases as checker


EPS = F("1e-10")
ROTATIONS = ((F(4, 5), F(3, 5)), (F(5, 13), F(12, 13)),
             (F(3, 5), F(-4, 5)))


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def orientation(a, b, p):
    return cross(sub(b, a), sub(p, a))


def exact_corners(obj):
    c, s = obj["rotation"]
    x, y = obj["center"]
    w, h = obj["width"] / 2, obj["height"] / 2
    return [(x + c*u - s*v, y + s*u + c*v)
            for u, v in ((-w, -h), (w, -h), (w, h), (-w, h))]


def edges(poly):
    return zip(poly, poly[1:] + poly[:1])


def inside(point, poly):
    return all(orientation(a, b, point) >= 0 for a, b in edges(poly))


def segment_intersection(a, b, c, d):
    u, v, offset = sub(b, a), sub(d, c), sub(c, a)
    denominator = cross(u, v)
    if not denominator:
        # Collinear overlap endpoints are already collected as inside vertices.
        return []
    t, s = cross(offset, v) / denominator, cross(offset, u) / denominator
    if 0 <= t <= 1 and 0 <= s <= 1:
        return [(a[0] + t*u[0], a[1] + t*u[1])]
    return []


def hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return points
    halves = []
    for sequence in (points, points[::-1]):
        half = []
        for point in sequence:
            while len(half) >= 2 and orientation(half[-2], half[-1], point) <= 0:
                half.pop()
            half.append(point)
        halves.append(half[:-1])
    return halves[0] + halves[1]


def intersection_area(a, b):
    a, b = exact_corners(a), exact_corners(b)
    points = [p for p in a if inside(p, b)] + [p for p in b if inside(p, a)]
    for (p, q), (r, s) in itertools.product(edges(a), edges(b)):
        points.extend(segment_intersection(p, q, r, s))
    assert len(points) <= 24
    poly = hull(points)
    return abs(sum((cross(p, q) for p, q in edges(poly)), F(0))) / 2


def box(identifier, center=(0, 0), width=2, height=2, rotation=ROTATIONS[0],
        kind="tag", text="1.8"):
    return dict(id=identifier, center=tuple(map(F, center)), width=F(width),
                height=F(height), rotation=rotation, kind=kind, text=F(text))


def production_case(case):
    objects = []
    for obj in case["objects"]:
        c, s = obj["rotation"]
        objects.append(dict(id=obj["id"], kind=obj["kind"],
                            center=list(map(float, obj["center"])),
                            width=float(obj["width"]), height=float(obj["height"]),
                            rotation_deg=math.degrees(math.atan2(float(s), float(c))),
                            line_count=1 if obj["kind"] == "tag" else 0,
                            paper_text_height_mm=float(obj["text"])))
    return dict(id=case["id"], objects=objects)


def expected(case):
    objects = {obj["id"]: obj for obj in case["objects"]}
    diagnostics, pairs = [], []
    for obj in objects.values():
        if obj["kind"] == "tag" and obj["text"] < F("1.8") - EPS:
            diagnostics.append(dict(name="TEXT_TOO_SMALL", object_ids=[obj["id"]]))
    for a_id, b_id in itertools.combinations(sorted(objects), 2):
        a, b = objects[a_id], objects[b_id]
        if a["kind"] == b["kind"] == "module":
            continue
        area = intersection_area(a, b)
        fraction = area / min(o["width"] * o["height"] for o in (a, b))
        pairs.append(dict(object_ids=[a_id, b_id], intersection_area=float(area),
                          smaller_box_fraction=float(fraction)))
        if a["kind"] == b["kind"] == "tag":
            name = "TAG_OVERLAP" if fraction > F("0.1") + EPS else None
        else:
            name = "TAG_MODULE_INTERSECTION" if area > EPS else None
        if name:
            diagnostics.append(dict(name=name, object_ids=[a_id, b_id]))
    diagnostics.sort(key=lambda d: (d["name"], d["object_ids"]))
    return dict(pairs=pairs, diagnostics=diagnostics,
                verdict="FAIL" if any(d["name"] in ("TAG_OVERLAP", "TEXT_TOO_SMALL")
                                      for d in diagnostics) else "PASS")


def scenarios():
    cases = []

    def add(name, *objects):
        cases.append(dict(id=name, objects=list(objects)))

    for i, rotation in enumerate(ROTATIONS):
        c, s = rotation
        for kind in ("tag", "module"):
            for label, u, v in (("identical", 0, 0), ("edge", 2, 0),
                                 ("corner", 2, 2), ("separated", 3, 0)):
                add(f"{i}_{kind}_{label}", box("tag/a", rotation=rotation),
                    box("other/b", (c*u-s*v, s*u+c*v), rotation=rotation, kind=kind))
        for label, fraction in (("below", F("0.1") - 2*EPS),
                                ("at", F("0.1")),
                                ("epsilon_band", F("0.1") + EPS/2),
                                ("above", F("0.1") + 2*EPS)):
            offset = 10 * (1 - fraction)
            add(f"{i}_threshold_{label}", box("tag/a", width=10, rotation=rotation),
                box("tag/b", (c*offset, s*offset), width=10, rotation=rotation))
        add(f"{i}_mixed", box("tag/a", (F(1, 4), F(-1, 2)), 4, 3, rotation),
            box("tag/b", (1, 1), 2, 3, ROTATIONS[(i+1) % 3]),
            box("module/a", (0, 0), 3, 2, ROTATIONS[(i+2) % 3], "module"),
            box("module/b", (0, 0), 3, 2, rotation, "module"))
    for label, text in (("below", F("1.8") - 2*EPS), ("at", F("1.8")),
                         ("band", F("1.8") - EPS/2)):
        add("text_" + label, box("tag/text", text=text))
    # Small positive areas straddle the module's absolute EPS policy, while
    # identical tags still fail on their unit fraction. Centers stay at zero
    # to avoid cancellation swamping these deliberately small areas.
    for label, height in (("below", F(1, 200000)), ("above", F(1, 50000))):
        add("module_epsilon_" + label,
            box("tag/a", width=F(1, 100000), height=height),
            box("tag/b", width=F(1, 100000), height=height),
            box("module/a", width=F(1, 100000), height=height, kind="module"))
    add("empty")
    add("modules_only", box("module/a", kind="module"), box("module/b", kind="module"))
    return cases


class ObliqueTagOracle(unittest.TestCase):
    def assert_result(self, case, actual):
        want = expected(case)
        context = f"{case['id']}: {[o['id'] for o in case['objects']]}"
        self.assertEqual(actual.keys(), want.keys(), context)
        self.assertEqual(actual["diagnostics"], want["diagnostics"], context)
        self.assertEqual(actual["verdict"], want["verdict"], context)
        self.assertEqual(len(actual["pairs"]), len(want["pairs"]), context)
        for got, target in zip(actual["pairs"], want["pairs"]):
            self.assertEqual(got.keys(), target.keys(), context)
            self.assertEqual(got["object_ids"], target["object_ids"], context)
            for field in ("intersection_area", "smaller_box_fraction"):
                value = target[field]
                # Positive values cannot disappear into a large absolute EPS.
                tolerance = min(2e-12, abs(value)*1e-9) if value else 2e-13
                self.assertTrue(math.isfinite(got[field]), context)
                self.assertAlmostEqual(got[field], value, delta=tolerance,
                                       msg=context + ": " + field)

    def test_exact_kernel_anchors(self):
        a = box("a")
        self.assertEqual(exact_corners(a)[0], (F(-1, 5), F(-7, 5)))
        self.assertEqual(intersection_area(a, a), 4)
        self.assertEqual(intersection_area(a, box("b", (F(4, 5), F(3, 5)))), 2)
        self.assertEqual(intersection_area(a, box("b", (F(8, 5), F(6, 5)))), 0)
        self.assertEqual(segment_intersection((F(0), F(0)), (F(2), F(2)),
                                             (F(0), F(2)), (F(2), F(0))), [(1, 1)])
        b = box("b", rotation=ROTATIONS[1])
        # Relative rotation is (cos, sin) = (56/65, 33/65). Four
        # corner triangles remove 48/77 from the square's area of four.
        self.assertEqual(intersection_area(a, b), F(260, 77))
        self.assertEqual(intersection_area(a, b), intersection_area(b, a))
        self.assertGreater(intersection_area(a, b), 0)
        self.assertLess(intersection_area(a, b), 4)

    def test_original_tag_vectors(self):
        # Replay authored expectations without changing or regenerating them.
        for cases, calculate in checker.load_sets():
            if calculate is checker.tag_overlaps:
                for case in cases:
                    with self.subTest(case=case["id"]):
                        checker.check_case(case, calculate)

    def test_bounded_scenarios(self):
        cases = scenarios()
        self.assertLessEqual(len(cases), 64)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertLessEqual(len(case["objects"]), 4)
                result = checker.tag_overlaps(production_case(case))
                self.assert_result(case, result)
                if "_threshold_" in case["id"]:
                    self.assertEqual(result["verdict"],
                                     "FAIL" if case["id"].endswith("above") else "PASS")

    def test_corruption_retains_source_ids(self):
        case = next(c for c in scenarios() if c["id"] == "0_mixed")
        result = checker.tag_overlaps(production_case(case))
        self.assert_result(case, result)
        for field in ("intersection_area", "smaller_box_fraction", "pair_ids", "diagnostic_ids"):
            bad = copy.deepcopy(result)
            if field == "pair_ids":
                bad["pairs"][0]["object_ids"][0] = "wrong/source"
            elif field == "diagnostic_ids":
                bad["diagnostics"][0]["object_ids"] = ["wrong/source"]
            else:
                bad["pairs"][0][field] = 0.0
            with self.subTest(field=field), self.assertRaises(AssertionError) as caught:
                self.assert_result(case, bad)
            for obj in case["objects"]:
                self.assertIn(obj["id"], str(caught.exception))

    def test_small_positive_area_cannot_be_hidden(self):
        case = next(c for c in scenarios() if c["id"] == "module_epsilon_below")
        result = checker.tag_overlaps(production_case(case))
        self.assert_result(case, result)
        bad = copy.deepcopy(result)
        bad["pairs"][0]["intersection_area"] = 0.0
        with self.assertRaises(AssertionError):
            self.assert_result(case, bad)


if __name__ == "__main__":
    unittest.main()
