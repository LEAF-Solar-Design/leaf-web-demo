"""Focused contracts for selected-union partitioning; no runner integration."""

import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import selected_shards as helper


class SelectedShardsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def timings(self, seconds, **changes):
        document = {"schema": "leaf.ci.suite-seconds.v1", "seconds": seconds}
        document.update(changes)
        path = self.root / "timings.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def assert_exact(self, plan, selected):
        self.assertEqual(plan["schema"], "leaf.ci.selected-shards.v1")
        self.assertEqual(plan["selected_ids"], selected)
        flattened = [suite_id for shard in plan["shards"] for suite_id in shard]
        self.assertCountEqual(flattened, selected)
        self.assertEqual(len(flattened), len(set(flattened)))
        for shard in plan["shards"]:
            self.assertEqual(shard, [suite_id for suite_id in selected if suite_id in shard])
        json.dumps(plan, allow_nan=False)

    def test_overlapping_substring_union_is_resolved_before_partition(self):
        catalog = ["web-extra", "server", "web", "server-web", "other"]
        selected = catalog[:-1]
        for index in range(3):
            plan = helper.make_plan(catalog, ["web", "server", "web-extra", "web"], 3, index)
            self.assert_exact(plan, selected)
            self.assertEqual(plan["shard_ids"], plan["shards"][index])
            self.assertEqual(plan["algorithm"], "equal-count")

    def test_lpt_exact_assignment_and_catalog_order(self):
        catalog = ["e", "c", "a", "d", "b", "zz"]
        path = self.timings({"a": 9.0, "b": 8.0, "c": 7.0, "d": 6.0, "e": 5.0})
        plan = helper.make_plan(catalog, ["a", "b", "c", "d", "e"], 2, 1, path)
        self.assert_exact(plan, catalog[:-1])
        self.assertEqual(plan["algorithm"], "lpt")
        self.assertEqual(plan["shards"], [["e", "a", "d"], ["c", "b"]])
        self.assertEqual(plan["shard_ids"], ["c", "b"])

    def test_ties_and_permutations_preserve_membership_and_restore_catalog_order(self):
        for weighted in (False, True):
            path = self.timings(dict.fromkeys("abcd", 1.0)) if weighted else None
            for permutation in itertools.permutations("abcd"):
                catalog = list(permutation)
                plan = helper.make_plan(catalog, list("dcba"), 2, 0, path)
                self.assert_exact(plan, catalog)
                self.assertEqual(plan["shards"], [
                    [suite_id for suite_id in catalog if suite_id in "ac"],
                    [suite_id for suite_id in catalog if suite_id in "bd"],
                ])
                self.assertEqual(plan, helper.make_plan(catalog, list("abcd"), 2, 0, path))

    def test_mixed_zero_positive_weights_use_deterministic_lpt(self):
        path = self.timings({"a": 4.0, "b": 2.0, "c": 0, "d": 0.0})
        for permutation in itertools.permutations("abcd"):
            catalog = list(permutation)
            plan = helper.make_plan(catalog, list("dcba"), 2, 1, path)
            self.assert_exact(plan, catalog)
            self.assertEqual(plan["algorithm"], "lpt")
            self.assertEqual(plan["shards"], [
                [suite_id for suite_id in catalog if suite_id == "a"],
                [suite_id for suite_id in catalog if suite_id in "bcd"],
            ])
            self.assertEqual(plan["shard_ids"], plan["shards"][1])
            self.assertEqual(plan, helper.make_plan(catalog, list("abcd"), 2, 1, path))

    def test_all_zero_selected_weights_use_equal_count(self):
        catalog = ["e", "c", "a", "d", "b"]
        for unselected_weight in (0.0, 10.0):
            with self.subTest(unselected_weight=unselected_weight):
                seconds = dict.fromkeys(catalog, 0.0)
                seconds["unselected"] = unselected_weight
                path = self.timings(seconds)
                plan = helper.make_plan(catalog, list("abcde"), 2, 0, path)
                self.assert_exact(plan, catalog)
                self.assertEqual(plan["algorithm"], "equal-count")
                self.assertEqual(plan["shards"], [["e", "c", "a"], ["d", "b"]])

    def test_absent_unreadable_malformed_and_incomplete_timings_fall_back(self):
        catalog = ["e", "c", "a", "d", "b"]
        bad_path = self.root / "bad.json"
        bad_path.write_text("{broken", encoding="utf-8")
        invalid_utf8 = self.root / "invalid-utf8.json"
        invalid_utf8.write_bytes(b"\xff")
        paths = [None, self.root / "missing.json", self.root, bad_path, invalid_utf8,
                 True, 0, {}]
        for path in paths:
            with self.subTest(path=path):
                plan = helper.make_plan(catalog, list("abcde"), 2, 0, path)
                self.assert_exact(plan, catalog)
                self.assertEqual(plan["algorithm"], "equal-count")
                self.assertEqual(plan["shards"], [["e", "c", "a"], ["d", "b"]])
        documents = [None, [], {}, {"schema": "wrong", "seconds": dict.fromkeys(catalog, 1.0)},
                     {"schema": "leaf.ci.suite-seconds.v1", "seconds": []},
                     {"schema": "leaf.ci.suite-seconds.v1", "seconds": {"a": 10.0}}]
        for document in documents:
            with self.subTest(document=document):
                bad_path.write_text(json.dumps(document), encoding="utf-8")
                plan = helper.make_plan(catalog, list("abcde"), 2, 0, bad_path)
                self.assert_exact(plan, catalog)
                self.assertEqual(plan["algorithm"], "equal-count")
                self.assertEqual(plan["shards"], [["e", "c", "a"], ["d", "b"]])

    def test_invalid_weight_anywhere_rejects_whole_document(self):
        for weight in (float("nan"), float("inf"), -float("inf"), True, False,
                       -1, "2.0", None, [], {}, 10 ** 400):
            for invalid_id in ("a", "unselected"):
                with self.subTest(weight=weight, invalid_id=invalid_id):
                    path = self.timings({"a": 4.0, "b": 1.0, invalid_id: weight})
                    plan = helper.make_plan(["b", "a"], ["a", "b"], 2, 0, path)
                    self.assert_exact(plan, ["b", "a"])
                    self.assertEqual(plan["algorithm"], "equal-count")
                    self.assertEqual(plan["shards"], [["a"], ["b"]])

    def test_more_shards_than_suites_preserves_empty_shards(self):
        for path in (None, self.timings({"a": 2.0, "b": 1.0})):
            for index in range(5):
                plan = helper.make_plan(["b", "a"], ["a", "b"], 5, index, path)
                self.assert_exact(plan, ["b", "a"])
                self.assertEqual(plan["shards"], [["a"], ["b"], [], [], []])
                self.assertEqual(plan["shard_ids"], plan["shards"][index])

    def test_invalid_selection_and_shard_arguments_raise_value_error(self):
        cases = [([], ["a"], 1, 0), ("a", ["a"], 1, 0),
                 (["a", "a"], ["a"], 1, 0), ([1], ["a"], 1, 0),
                 ([""], ["a"], 1, 0), (["a\0"], ["a"], 1, 0),
                 (["a"], [], 1, 0), (["a"], "a", 1, 0),
                 (["a"], [""], 1, 0), (["a"], [None], 1, 0),
                 (["a"], ["a", "dead"], 1, 0), (["a"], ["dead"], 1, 0)]
        cases += [(["a"], ["a"], count, 0) for count in (0, -1, True, 1.0, "1", None)]
        cases += [(["a"], ["a"], 2, index) for index in (-1, 2, True, 0.0, "0", None)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                helper.make_plan(*args)

    def test_partition_guard_rejects_missing_extra_and_duplicate_ids(self):
        for shards in ([["a"]], [["a", "a"]], [["a", "b", "c"]], [["a"], ["a", "b"]]):
            with self.subTest(shards=shards), self.assertRaises(ValueError):
                helper._validate_partition(["a", "b"], shards)

    def cli(self, raw, catalog=None, timings=None):
        catalog_path = self.root / "catalog.json"
        if catalog is None:
            catalog = {"schema": "leaf.ci.catalog.v1", "suites": [
                {"id": "web-é-extra"}, {"id": "server"}, {"id": "web-é"}, {"id": "other"}]}
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        only_path = self.root / "only-args.nul"
        only_path.write_bytes(raw)
        output = self.root / "plan.json"
        command = [sys.executable, "-B", str(Path(helper.__file__).resolve()), "plan",
                   "--catalog", str(catalog_path), "--only-args", str(only_path),
                   "--shard-count", "2", "--shard-index", "0", "--output", str(output)]
        if timings is not None:
            command += ["--timings", str(timings)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        return result, output

    def test_cli_utf8_nul_tokens_union_and_optional_timings(self):
        raw = "--only\0web-é\0--only\0server\0--only\0web-é-extra\0".encode("utf-8")
        for path in (None, self.timings({"web-é-extra": 9.0, "server": 4.0, "web-é": 3.0})):
            result, output = self.cli(raw, timings=path)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assert_exact(plan, ["web-é-extra", "server", "web-é"])
            self.assertEqual(plan["shard_ids"], plan["shards"][0])
            self.assertEqual(plan["algorithm"], "lpt" if path else "equal-count")
            self.assertEqual(list(self.root.glob(".plan.json.*.tmp")), [])

    def test_cli_malformed_tokens_fail_without_publishing(self):
        for raw in (b"", b"--only\0a", b"--only\0", b"--only\0\0", b"--other\0a\0",
                    b"--only\0server\0junk\0", b"--only\0server\0\0",
                    b"--only\0\xff\0", b"--only\0dead\0"):
            with self.subTest(raw=raw):
                result, output = self.cli(raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertFalse(output.exists())

    def test_cli_rejects_malformed_catalog(self):
        for catalog in ([], {}, {"schema": "wrong", "suites": [{"id": "server"}]},
                        {"schema": "leaf.ci.catalog.v1", "suites": ["server"]},
                        {"schema": "leaf.ci.catalog.v1", "suites": [{}]},
                        {"schema": "leaf.ci.catalog.v1", "suites": [{"id": "server"}] * 2}):
            with self.subTest(catalog=catalog):
                result, output = self.cli(b"--only\0server\0", catalog=catalog)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)
                self.assertFalse(output.exists())

    def test_atomic_replace_failure_preserves_existing_output_and_cleans_temp(self):
        output = self.root / "plan.json"
        output.write_text("previous", encoding="utf-8")
        with mock.patch.object(helper.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                helper._write_plan(output, helper.make_plan(["a"], ["a"], 1, 0))
        self.assertEqual(output.read_text(encoding="utf-8"), "previous")
        self.assertEqual(list(self.root.glob(".plan.json.*.tmp")), [])

    def test_frozen_snapshot_metadata_and_recorded_means(self):
        path = Path(helper.__file__).with_name("selected-suite-seconds.json")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "leaf.ci.suite-seconds.v1")
        self.assertEqual(document["source_sha256"],
                         "8d9495274225a0cc577447b21b24d3031ee313cb1f965a1bec1b48c5e7d47aa4")
        self.assertEqual(document["window_start_utc"], "2026-11-13T16:55:14.172924+00:00")
        self.assertEqual(document["window_end_utc"], "2026-11-14T16:55:14.172924+00:00")
        self.assertEqual(document["window_basis"], "jobs.enqueued_at")
        self.assertEqual(len(document["entries"]), 766)
        self.assertEqual(len(document["seconds"]), 766)
        self.assertEqual(document["seconds"]["web-version-restore-proof"], round(30750.3 / 106, 3))
        self.assertEqual(document["entries"]["web-version-restore-proof"], 106)
        self.assertEqual(set(document["entries"]), set(document["seconds"]))
        observed_zero = {"server-integration-turn-grants-postgres",
                         "server-integration-project-turn-postgres", "harness-container-smoke"}
        self.assertEqual(set(document["observed_zero_seconds"]), observed_zero)
        self.assertEqual({suite_id for suite_id, seconds in document["seconds"].items()
                          if seconds == 0.0}, observed_zero)
        for suite_id in observed_zero:
            self.assertEqual(document["seconds"][suite_id], 0.0)
        self.assertIn("GATE_RESULT", " ".join(document["evidence_notes"]))
        self.assertIn("prediction only", " ".join(document["evidence_notes"]))
        catalog = list(document["seconds"])
        plan = helper.make_plan(catalog, catalog, 13, 0, path)
        self.assert_exact(plan, catalog)
        self.assertEqual(plan["algorithm"], "lpt")
        tier = json.loads(path.with_name("pr-tier.json").read_text(encoding="utf-8"))
        catalog = tier["suite_ids"]
        self.assertEqual(len(catalog), 108)
        self.assertIn("server-integration-turn-grants-postgres", catalog)
        plan = helper.make_plan(catalog, catalog, 13, 0, path)
        self.assert_exact(plan, catalog)
        self.assertEqual(plan["algorithm"], "lpt")
