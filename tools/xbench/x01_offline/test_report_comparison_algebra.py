"""Bounded algebra controls for saved synthetic reports, never baseline adoption.

Four authored controls use three checker rows, at most four diagnostics per
scope and three provenance entries. Scratch reports are at most 16 KiB each.
Failure messages retain the exact input JSON as a replayable counterexample.
"""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from .compare_offline_reports import compare_reports
except ImportError:
    from compare_offline_reports import compare_reports


CHECKERS = ("check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py")


def report(observed="PASS"):
    rows = [dict(checker=name, verdict="PASS", exit_code=0, timeout=False,
                 duration_seconds=0.25, diagnostics=[], output="synthetic λ",
                 output_truncated=False) for name in CHECKERS]
    rows[0].update(verdict=observed, exit_code={"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[observed])
    return dict(schema="leaf.xbench.x01.offline-run.v1", evidence_kind="synthetic_offline",
                live_grade=False, verdict=observed, checks=rows, diagnostics=[], provenance=[])


def provenance(name, digit="a"):
    return dict(name=name, sha256_before=digit * 64, sha256_after=digit * 64)


class ComparisonAlgebraTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="x01 algebra λ ")
        self.addCleanup(scratch.cleanup)
        self.paths = (Path(scratch.name) / "old ~ λ.json", Path(scratch.name) / "new λ.json")

    def compare(self, old, new):
        raw_inputs = []
        for path, value in zip(self.paths, (old, new)):
            self.assertEqual(len(value["checks"]), 3)
            self.assertLessEqual(len(value["provenance"]), 3)
            for items in [value["diagnostics"]] + [row["diagnostics"] for row in value["checks"]]:
                self.assertLessEqual(len(items), 4)
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.assertLessEqual(len(raw), 16384)
            path.write_bytes(raw)
            raw_inputs.append(raw)
        self.counterexample = "\nOLD.json=" + raw_inputs[0].decode("utf-8") + "\nNEW.json=" + raw_inputs[1].decode("utf-8")
        hashes = [hashlib.sha256(raw).hexdigest() for raw in raw_inputs]
        with patch("subprocess.Popen", side_effect=AssertionError("child launch")), \
                patch("os.system", side_effect=AssertionError("shell launch")):
            try:
                result = compare_reports(*self.paths)
                for _ in range(2):
                    self.assertEqual(compare_reports(*self.paths), result, self.counterexample)
            except Exception as error:
                self.fail(repr(error) + self.counterexample)
        for path, raw, digest in zip(self.paths, raw_inputs, hashes):
            self.assertEqual(path.read_bytes(), raw, self.counterexample)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, self.counterexample)
        self.assertEqual(result["schema"], "leaf.xbench.x01.offline-report-comparison.v1")
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)
        self.assertIs(result["valid"], True, self.counterexample)
        self.assertEqual(result["verdict"], "PASS", self.counterexample)
        self.assertEqual(result["diagnostics"], [], self.counterexample)
        return result

    def assert_no_changes(self, result, observed):
        self.assertEqual(result["observed_verdicts"], dict(old=observed, new=observed), self.counterexample)
        for field in ("added_diagnostics", "removed_diagnostics", "provenance_changes"):
            self.assertEqual(result[field], [], self.counterexample)
        expected = [dict(checker=name, old_verdict=observed if name == CHECKERS[0] else "PASS",
                         new_verdict=observed if name == CHECKERS[0] else "PASS", changed=False,
                         transition=(observed if name == CHECKERS[0] else "PASS") + "->" +
                         (observed if name == CHECKERS[0] else "PASS")) for name in CHECKERS]
        self.assertEqual(result["checks"], expected, self.counterexample)

    def assert_diagnostics(self, actual, expected):
        # Independent expected payloads and paths; no comparator canonicalizer or
        # diagnostic extractor supplies this oracle. Hash the authored full JSON.
        records = []
        for checker, payload in expected:
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":"), allow_nan=False)
            identifier = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            prefix = "/parent" if checker is None else "/checks/" + checker
            records.append(dict(scope="parent" if checker is None else "checker", checker=checker,
                                object_id=identifier, path=prefix + "/diagnostics/" + identifier,
                                canonical_json=canonical, diagnostic=payload))
        self.assertEqual(sorted(actual, key=lambda row: row["path"]),
                         sorted(records, key=lambda row: row["path"]), self.counterexample)

    def test_identity_and_collection_reordering(self):
        for observed in ("PASS", "FAIL", "UNREADABLE"):
            with self.subTest(observed=observed):
                old = report(observed)
                old["diagnostics"] = [dict(name="備考/λ~", object_ids=["屋根/~one"]), dict(name="OTHER")]
                old["checks"][0]["diagnostics"] = [
                    dict(name="SAME", object_ids=["tag/α"]),
                    dict(name="SAME", object_ids=["tag/β"],
                         details={"slash/key~": {"quoted\"\\\n": [1, 2, 1]}})]
                old["provenance"] = [provenance("vector:λ/~"), provenance("checker:β", "b")]
                identity = self.compare(old, old)
                self.assert_no_changes(identity, observed)
                new = copy.deepcopy(old)
                new["checks"].reverse()
                new["provenance"].reverse()
                new["diagnostics"].reverse()
                for row in new["checks"]:
                    row["diagnostics"].reverse()
                    row["diagnostics"] = [dict(reversed(list(item.items()))) for item in row["diagnostics"]]
                reordered = self.compare(old, new)
                self.assert_no_changes(reordered, observed)
                self.assertEqual(reordered, identity, self.counterexample)

    def test_diagnostic_add_remove_inverse_and_scope(self):
        old, new = report("FAIL"), report("UNREADABLE")
        removed = dict(name="SAME/λ~", object_ids=["tag/α", "tag/~β"],
                       **{"key/~\"\\": {"rows": [["outer", "inner"], [2, 1, 2]]}})
        added = copy.deepcopy(removed)
        added["object_ids"] = ["tag/γ", "tag/~β"]
        common = dict(name="SAME/λ~", object_ids=["unchanged"])
        old["checks"][0]["diagnostics"] = [removed, common]
        new["checks"][0]["diagnostics"] = [common, added, copy.deepcopy(added)]
        # The same payload in another scope is a separate addition.
        new["diagnostics"] = [removed]
        forward = self.compare(old, new)
        self.assert_diagnostics(forward["removed_diagnostics"], [(CHECKERS[0], removed)])
        self.assert_diagnostics(forward["added_diagnostics"], [(CHECKERS[0], added), (None, removed)])
        self.assertEqual(forward["provenance_changes"], [], self.counterexample)
        self.assertEqual(forward["checks"][0], dict(checker=CHECKERS[0], old_verdict="FAIL",
                         new_verdict="UNREADABLE", changed=True, transition="FAIL->UNREADABLE"), self.counterexample)
        reverse = self.compare(new, old)
        self.assert_diagnostics(reverse["added_diagnostics"], [(CHECKERS[0], removed)])
        self.assert_diagnostics(reverse["removed_diagnostics"], [(CHECKERS[0], added), (None, removed)])
        self.assertEqual(reverse["added_diagnostics"], forward["removed_diagnostics"], self.counterexample)
        self.assertEqual(reverse["removed_diagnostics"], forward["added_diagnostics"], self.counterexample)
        self.assertNotEqual(forward["added_diagnostics"][0]["object_id"],
                            forward["removed_diagnostics"][0]["object_id"], self.counterexample)

    def test_nested_lists_preserve_order_and_multiplicity(self):
        for field in ("object_ids", "occurrence_path", "nested/λ~"):
            with self.subTest(field=field):
                old = report()
                before = dict(name="ORDER", **{field: [["outer", "inner"], [3, 1, 3]]})
                after = dict(name="ORDER", **{field: [["inner", "outer"], [3, 1]]})
                old["checks"][1]["diagnostics"] = [before]
                new = copy.deepcopy(old)
                new["checks"][1]["diagnostics"] = [after]
                result = self.compare(old, new)
                self.assert_diagnostics(result["removed_diagnostics"], [(CHECKERS[1], before)])
                self.assert_diagnostics(result["added_diagnostics"], [(CHECKERS[1], after)])
                self.assertNotEqual(result["removed_diagnostics"][0]["object_id"],
                                    result["added_diagnostics"][0]["object_id"], self.counterexample)

    def test_provenance_add_remove_and_change_inverse(self):
        old, new = report(), report()
        removed, added = provenance("removed:λ/~"), provenance("added:β", "b")
        before, after = provenance("changed:屋根", "c"), provenance("changed:屋根", "d")
        old["provenance"] = [removed, before]
        new["provenance"] = [after, added]
        forward = self.compare(old, new)
        expected = [dict(name=added["name"], change="added", old=None, new=added),
                    dict(name=before["name"], change="changed", old=before, new=after),
                    dict(name=removed["name"], change="removed", old=removed, new=None)]
        expected.sort(key=lambda item: item["name"])
        self.assertEqual(forward["provenance_changes"], expected, self.counterexample)
        reverse = self.compare(new, old)
        inverse = [dict(name=added["name"], change="removed", old=added, new=None),
                   dict(name=before["name"], change="changed", old=after, new=before),
                   dict(name=removed["name"], change="added", old=None, new=removed)]
        inverse.sort(key=lambda item: item["name"])
        self.assertEqual(reverse["provenance_changes"], inverse, self.counterexample)
        for result in (forward, reverse):
            self.assertEqual(result["added_diagnostics"], [], self.counterexample)
            self.assertEqual(result["removed_diagnostics"], [], self.counterexample)


if __name__ == "__main__":
    unittest.main(verbosity=2)
