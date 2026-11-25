"""Capped exhaustive matching control, independent of production traversal."""

import copy
from fractions import Fraction
import itertools
from pathlib import Path
import random
import unittest

import check_cases as checker


FILES = ("module_geometry_cases.json", "fill_coverage_cases.json", "tag_overlap_cases.json")


def occurrence(oid, x, handle="ambiguous"):
    return dict(id=oid, handle=handle, point=[x, 0])


def oracle(case):
    original, exported = case["original"], case["export"]
    assert len(original) <= 6 and len(exported) <= 6
    forced = []
    for handle in {o["handle"] for o in original}:
        a = [o for o in original if o["handle"] == handle]
        b = [o for o in exported if o["handle"] == handle]
        if len(a) == len(b) == 1:
            forced.append((a[0]["id"], b[0]["id"]))
    left = [o for o in original if o["id"] not in {a for a, b in forced}]
    right = [o for o in exported if o["id"] not in {b for a, b in forced}]
    limit = Fraction(0.5 + checker.EPS) ** 2
    valid = {(a["id"], b["id"]) for a in left for b in right
             if sum((Fraction(x) - Fraction(y)) ** 2
                    for x, y in zip(a["point"], b["point"])) <= limit}
    # Enumerate subsets and injective assignments; no augmenting paths or
    # production distances participate in the expected cardinality.
    for size in range(min(len(left), len(right)), -1, -1):
        for subset in itertools.combinations(left, size):
            for assignment in itertools.permutations(right, size):
                if all((a["id"], b["id"]) in valid for a, b in zip(subset, assignment)):
                    return len(forced) + size, set(forced), valid


class MatchingOracle(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent
        self.raw = {f: (self.root / f).read_bytes() for f in FILES}

    def tearDown(self):
        for f, raw in self.raw.items():
            self.assertEqual((self.root / f).read_bytes(), raw)

    def assert_case(self, case):
        count, forced, valid = oracle(case)
        result = checker.geometry(case)
        pairs = {tuple(p) for p in result["pairs"]}
        self.assertEqual(len(pairs), count, case)
        self.assertTrue(forced <= pairs)
        self.assertTrue(pairs <= forced | valid)
        self.assertEqual(len({a for a, b in pairs}), count)
        self.assertEqual(len({b for a, b in pairs}), count)
        missing = sorted(o["id"] for o in case["original"] if o["id"] not in {a for a, b in pairs})
        extra = sorted(o["id"] for o in case["export"] if o["id"] not in {b for a, b in pairs})
        self.assertEqual([d["object_ids"][0] for d in result["diagnostics"] if d["name"] == "MISSING"], missing)
        self.assertEqual([d["object_ids"][0] for d in result["diagnostics"] if d["name"] == "EXTRA"], extra)
        for reverse_o, reverse_e in itertools.product((False, True), repeat=2):
            permuted = copy.deepcopy(case)
            if reverse_o:
                permuted["original"].reverse()
            if reverse_e:
                permuted["export"].reverse()
            self.assertEqual(checker.geometry(permuted), result)
        return result

    def test_trap_and_unique_priority(self):
        trap = dict(original=[occurrence("a", 0), occurrence("b", .3)],
                    export=[occurrence("x", .1), occurrence("y", -.4)])
        self.assertEqual(self.assert_case(trap)["pairs"], [["a", "y"], ["b", "x"]])
        trap["original"][0]["handle"] = trap["export"][0]["handle"] = "forced"
        trap["export"][0]["point"] = [100, 0]
        result = self.assert_case(trap)
        self.assertIn(dict(name="MOVED", object_ids=["a", "x"]), result["diagnostics"])

    def test_bounded_exhaustive_controls(self):
        rng = random.Random(0xA061)
        for index in range(48):
            case = {side: [occurrence(prefix + str(i), rng.randrange(-8, 9) / 8,
                                     rng.choice(("same", "same", "other")))
                           for i in range(rng.randrange(7))]
                    for side, prefix in (("original", "o"), ("export", "e"))}
            with self.subTest(index=index):
                self.assert_case(case)

    def test_tolerance_and_legacy_greedy(self):
        for x in (.5, .5 + checker.EPS / 2, .5 + checker.EPS * 2):
            self.assert_case(dict(original=[occurrence("a", 0, "o")],
                                  export=[occurrence("b", x, "e")]))
        case = dict(original=[occurrence("a", 0), occurrence("b", 1)],
                    export=[occurrence("x", .1), occurrence("y", 1.1)])
        self.assertEqual(self.assert_case(case)["pairs"], [["a", "x"], ["b", "y"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
