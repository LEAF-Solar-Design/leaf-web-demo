"""Describe two explicit saved synthetic reports without executing any checker.

Comparison PASS means both reports validate, including when observed verdicts
are FAIL or change. It never means live grade, baseline adoption, source custody,
or that a checker's conclusions are true. Inputs must remain quiescent during
reading. No default paths, source reads, child processes, or report writes.

Diagnostics use set semantics for duplicate identical top-level diagnostic
objects within each scope. Diagnostic object keys are sorted recursively;
nested lists preserve order, including object_ids and occurrence_path.
Canonical full JSON, not a name or digest, is the comparison key.
Content SHA256 IDs and logical JSON-pointer paths are stable under reordering;
these paths identify comparison objects, not source-array offsets.
"""

import argparse
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import validate_offline_report as validator
except ImportError:
    import validate_offline_report as validator


SCHEMA = "leaf.xbench.x01.offline-report-comparison.v1"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _pointer(value):
    return value.replace("~", "~0").replace("/", "~1")


def _diagnostics(report):
    found = {}
    scopes = [(None, report.get("diagnostics", []))]
    scopes.extend((row["checker"], row["diagnostics"]) for row in report["checks"])
    for checker, items in scopes:
        for item in items:
            canonical = _canonical(item)
            key = _json(canonical)
            object_id = "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
            scope = "/parent" if checker is None else "/checks/" + _pointer(checker)
            found[(scope, key)] = dict(
                scope="parent" if checker is None else "checker", checker=checker,
                object_id=object_id, path=scope + "/diagnostics/" + object_id,
                canonical_json=key, diagnostic=canonical)
    return found


def compare_reports(old_path, new_path):
    """Read/validate explicit paths once each, returning a descriptive result.

    Invalid report structures produce FAIL (exit 1); unreadable/invalid JSON
    produces UNREADABLE (exit 4), which takes precedence if both sides fail.
    Neither output verdict nor a checker transition is a live-grade assertion.
    """
    old, old_validation = validator.read_validated_report(old_path)
    new, new_validation = validator.read_validated_report(new_path)
    validations = dict(old=old_validation, new=new_validation)
    valid = all(item["valid"] for item in validations.values())
    verdict = ("PASS" if valid else "UNREADABLE" if any(
        item["verdict"] == "UNREADABLE" for item in validations.values()) else "FAIL")
    result = dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                  valid=valid, verdict=verdict, report_validation=validations,
                  diagnostics=[], observed_verdicts={}, checks=[],
                  provenance_changes=[], added_diagnostics=[], removed_diagnostics=[],
                  diagnostic_semantics="set per scope; nested list order preserved")
    for side, validation in validations.items():
        result["diagnostics"].extend(dict(item, side=side) for item in validation["diagnostics"])
        if "observed_verdict" in validation:
            result["observed_verdicts"][side] = validation["observed_verdict"]
    if not valid:
        return result
    old_rows = {row["checker"]: row for row in old["checks"]}
    new_rows = {row["checker"]: row for row in new["checks"]}
    for checker in sorted(old_rows):
        before, after = old_rows[checker]["verdict"], new_rows[checker]["verdict"]
        result["checks"].append(dict(checker=checker, old_verdict=before,
                                     new_verdict=after, changed=before != after,
                                     transition=before + "->" + after))
    old_sources = {entry["name"]: entry for entry in old.get("provenance", [])}
    new_sources = {entry["name"]: entry for entry in new.get("provenance", [])}
    for name in sorted(old_sources.keys() | new_sources.keys()):
        before, after = old_sources.get(name), new_sources.get(name)
        if before != after:
            result["provenance_changes"].append(dict(
                name=name, change="added" if before is None else
                "removed" if after is None else "changed",
                old=copy.deepcopy(before), new=copy.deepcopy(after)))
    old_diagnostics, new_diagnostics = _diagnostics(old), _diagnostics(new)
    for field, source, other in (("added_diagnostics", new_diagnostics, old_diagnostics),
                                 ("removed_diagnostics", old_diagnostics, new_diagnostics)):
        result[field] = [source[key] for key in sorted(source.keys() - other.keys())]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", nargs="?", type=Path)
    parser.add_argument("new", nargs="?", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ComparisonTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.old is None or args.new is None:
        parser.error("OLD.json and NEW.json are required unless --self-test is used")
    result = compare_reports(args.old, args.new)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="x01 compare ü ")
        self.addCleanup(scratch.cleanup)
        self.old_path = Path(scratch.name) / "old λ.json"
        self.new_path = Path(scratch.name) / "new λ.json"

    def report(self, verdict="PASS"):
        rows = [dict(checker=name, verdict="PASS", exit_code=0, timeout=False,
                     duration_seconds=0.25, diagnostics=[], output="OK",
                     output_truncated=False) for name in validator.CHECKERS]
        rows[0].update(verdict=verdict, exit_code={"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[verdict])
        return dict(schema=validator.REPORT_SCHEMA, evidence_kind="synthetic_offline",
                    live_grade=False, verdict=verdict, checks=rows, diagnostics=[])

    def provenance(self, name, before="a", after=None):
        return dict(name=name, sha256_before=before*64,
                    sha256_after=(before if after is None else after)*64)

    def check(self, old, new, verdict="PASS", code=0):
        # Fixture writes occur only in the embedded self-test, never the API.
        self.old_path.write_bytes(json.dumps(old).encode("utf-8"))
        self.new_path.write_bytes(json.dumps(new).encode("utf-8"))
        return self.check_paths(verdict, code)

    def check_paths(self, verdict, code):
        snapshots = {path: path.read_bytes() for path in (self.old_path, self.new_path)
                     if path.exists()}
        with patch("subprocess.Popen", side_effect=AssertionError("child launch")), \
                patch("os.system", side_effect=AssertionError("shell launch")):
            result = compare_reports(self.old_path, self.new_path)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([str(self.old_path), str(self.new_path)]), code)
            self.assertEqual(json.loads(output.getvalue()), result)
        for path, raw in snapshots.items():
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(raw).digest())
        self.assertEqual(result["verdict"], verdict, result)
        self.assertIs(result["valid"], verdict == "PASS")
        self.assertIs(result["live_grade"], False)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertEqual(result["schema"], SCHEMA)
        return result

    def test_equal_observed_fail_and_reordering(self):
        for observed in ("PASS", "FAIL", "UNREADABLE"):
            old = self.report(observed)
            old["checks"][0]["diagnostics"] = [
                dict(name="TAG_OVERLAP", object_ids=["tag/a", "tag/b"],
                     details={"values": [3, 1, 3], "other": {"x": 1, "y": 2}}),
                dict(name="TEXT_TOO_SMALL", object_ids=["tag/c"])]
            old["diagnostics"] = [dict(name="PARENT_NOTE", object_ids=["source/a"]),
                                   dict(name="SECOND_NOTE")]
            old["provenance"] = [self.provenance("vector:a"), self.provenance("checker:b")]
            baseline = self.check(old, old)
            new = copy.deepcopy(old)
            diagnostic = new["checks"][0]["diagnostics"][0]
            diagnostic["details"]["other"] = {"y": 2, "x": 1}
            new["checks"][0]["diagnostics"].reverse()
            new["checks"].reverse()
            new["provenance"].reverse()
            new["diagnostics"].reverse()
            result = self.check(old, new)
            self.assertEqual(result, baseline)
            self.assertEqual(result["observed_verdicts"], dict(old=observed, new=observed))
            self.assertFalse(result["added_diagnostics"])
            self.assertFalse(result["removed_diagnostics"])
            self.assertFalse(result["provenance_changes"])

    def test_nested_payload_order_is_significant(self):
        for field in ("occurrence_path", "object_ids", "values"):
            old = self.report()
            item = dict(name="LAYER_HIDDEN", **{field: ["outer", "inner", "tag"]})
            old["checks"][0]["diagnostics"] = [item]
            new = copy.deepcopy(old)
            new_item = new["checks"][0]["diagnostics"][0]
            new_item[field].reverse()
            result = self.check(old, new)
            self.assertEqual(len(result["added_diagnostics"]), 1)
            self.assertEqual(len(result["removed_diagnostics"]), 1)
            added, removed = result["added_diagnostics"][0], result["removed_diagnostics"][0]
            self.assertEqual(added["diagnostic"][field], ["tag", "inner", "outer"])
            self.assertEqual(removed["diagnostic"][field], ["outer", "inner", "tag"])
            self.assertNotEqual(added["object_id"], removed["object_id"])
            inverse = self.check(new, old)
            self.assertEqual(inverse["removed_diagnostics"], result["added_diagnostics"])
            self.assertEqual(inverse["added_diagnostics"], result["removed_diagnostics"])

    def test_pass_to_fail_full_objects_and_parent_identity(self):
        old, new = self.report(), self.report("FAIL")
        old["checks"][0]["diagnostics"] = [dict(name="TAG_OVERLAP", object_ids=["tag/old"])]
        new["checks"][0]["diagnostics"] = [dict(name="TAG_OVERLAP", object_ids=["tag/new"])]
        new["checks"][1]["diagnostics"] = copy.deepcopy(new["checks"][0]["diagnostics"])
        new["diagnostics"] = copy.deepcopy(new["checks"][0]["diagnostics"])
        result = self.check(old, new)
        self.assertEqual(result["observed_verdicts"], dict(old="PASS", new="FAIL"))
        row = next(row for row in result["checks"] if row["checker"] == validator.CHECKERS[0])
        self.assertEqual(row["transition"], "PASS->FAIL")
        self.assertTrue(row["changed"])
        added, removed = result["added_diagnostics"], result["removed_diagnostics"]
        self.assertEqual(len(added), 3)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["diagnostic"]["object_ids"], ["tag/old"])
        self.assertEqual({item["checker"] for item in added}, {None, *validator.CHECKERS[:2]})
        self.assertEqual(len({item["path"] for item in added}), 3)
        for item in added:
            self.assertEqual(item["diagnostic"]["object_ids"], ["tag/new"])
            self.assertEqual(json.loads(item["canonical_json"]), item["diagnostic"])
            self.assertIn(item["object_id"], item["path"])
            self.assertNotEqual(item["object_id"], removed[0]["object_id"])
        swapped = self.check(new, old)
        self.assertEqual(swapped["removed_diagnostics"], added)

    def test_duplicate_set_and_nested_multiset_semantics(self):
        old = self.report()
        item = dict(name="NAMED", object_ids=["a", "b"], details={"counts": [1, 1, 2]})
        old["checks"][0]["diagnostics"] = [item]
        new = copy.deepcopy(old)
        new["checks"][0]["diagnostics"].append(copy.deepcopy(item))
        result = self.check(old, new)
        self.assertEqual(result["added_diagnostics"], [])
        self.assertEqual(result["removed_diagnostics"], [])
        new["checks"][0]["diagnostics"][0]["details"]["counts"].pop()
        result = self.check(old, new)
        self.assertEqual(len(result["added_diagnostics"]), 1)
        self.assertEqual(result["removed_diagnostics"], [])

    def test_provenance_changed_added_removed_valid_shapes(self):
        old, new = self.report(), self.report()
        old["provenance"] = [self.provenance("changed"), self.provenance("removed")]
        new["provenance"] = [self.provenance("changed", "b", "c"), self.provenance("added", "d")]
        # A within-run hash change requires a parent source diagnostic and an
        # observed UNREADABLE aggregate; this is still a valid comparison.
        new.update(verdict="UNREADABLE", diagnostics=[dict(name="INPUT_CHANGED", input="changed")])
        result = self.check(old, new)
        changes = {item["name"]: item for item in result["provenance_changes"]}
        self.assertEqual({name: item["change"] for name, item in changes.items()},
                         dict(changed="changed", removed="removed", added="added"))
        self.assertEqual(changes["changed"]["old"]["sha256_before"], "a"*64)
        self.assertEqual(changes["changed"]["new"]["sha256_before"], "b"*64)
        self.assertEqual(changes["changed"]["new"]["sha256_after"], "c"*64)
        self.assertIsNone(changes["added"]["old"])
        self.assertIsNone(changes["removed"]["new"])
        self.assertEqual(result["added_diagnostics"][0]["scope"], "parent")
        legacy = self.check(self.report(), old)
        self.assertEqual(len(legacy["provenance_changes"]), 2)

    def test_invalid_live_duplicate_and_missing_checker(self):
        for mode in ("schema", "live", "nested_live", "duplicate", "missing", "provenance"):
            bad = self.report()
            if mode == "schema":
                bad["schema"] = "unknown"
            elif mode == "live":
                bad["live_grade"] = True
            elif mode == "nested_live":
                bad["diagnostics"] = [dict(name="LIVE", nested=dict(live_grade=True))]
            elif mode == "duplicate":
                bad["checks"][1] = copy.deepcopy(bad["checks"][0])
            elif mode == "missing":
                bad["checks"].pop()
            else:
                bad["provenance"] = [self.provenance("duplicate"), self.provenance("duplicate")]
            for side in ("old", "new"):
                with self.subTest(mode=mode, side=side):
                    result = self.check(bad if side == "old" else self.report(),
                                        bad if side == "new" else self.report(), "FAIL", 1)
                    self.assertTrue(result["diagnostics"])
                    self.assertEqual({d["side"] for d in result["diagnostics"]}, {side})
                    self.assertEqual(result["checks"], [])

    def test_missing_malformed_duplicate_keys_and_no_paths(self):
        self.old_path.write_text(json.dumps(self.report()), encoding="utf-8")
        result = self.check_paths("UNREADABLE", 4)
        self.assertEqual(result["diagnostics"][0]["side"], "new")
        for raw in (b"{", b'{"schema":"a","schema":"b"}', b'\xff'):
            self.new_path.write_bytes(raw)
            result = self.check_paths("UNREADABLE", 4)
            self.assertEqual(result["diagnostics"][0]["side"], "new")
        self.assertEqual(compare_reports(None, None)["verdict"], "UNREADABLE")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    sys.exit(main())
