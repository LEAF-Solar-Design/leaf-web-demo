"""Executable L4 contracts. Every case owns and removes a temporary Git repo.

All pass/fail judgments use child return codes and structured artifacts, never
log-word searches. These tests need Git and the verifier's existing pytest.
"""

import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import select_tests as selector
import pytest_selection as plugin
import trace_reads


class SelectionContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="selection-contract-",
                                                     dir=Path(__file__).resolve().parents[2])
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.inputs = self.work / "trusted"
        self.inputs.mkdir()
        self.out = self.work / "out"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.upper().startswith(("GIT_", "PYTEST_", "LEAF_")) and
                    k not in ("PR_BASE_SHA", "BASE_SHA", "PYTHONPATH")}
        self.env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TERMINAL_PROMPT="0")
        self.git("init", "--initial-branch=main")
        self.git("config", "user.email", "selector@example.invalid")
        self.git("config", "user.name", "Selector contract")
        self.git("config", "core.autocrlf", "false")
        for path, text in {
            "src/a.py": "good\n", "src/b.py": "good\n", "src/idle.py": "good\n",
            "server/fixture.json": '{"value":"good"}\n',
            "native/input.json": '"good"\n', "data/old.json": '"old"\n',
            "data/new.json": '"known"\n', "quarantine.txt": "tests/test_b.py::test_b\n",
            "scripts/run-all-gates.py": "# trusted catalog producer fixture\n",
        }.items():
            self.put(path, text)
        self.put("tests/test_a.py", "from pathlib import Path\nimport json\n"
                 "def test_a():\n"
                 "    assert Path('src/a.py').read_text().strip() == 'good'\n"
                 "    assert json.loads(Path('server/fixture.json').read_text())['value'] == 'good'\n")
        for name in ("b", "idle"):
            self.put("tests/test_" + name + ".py", "from pathlib import Path\n"
                     "def test_" + name + "():\n"
                     "    Path('ran_" + name + "').write_text('yes')\n"
                     "    assert Path('src/" + name + ".py').read_text().strip() == 'good'\n")
        self.put("tests/test_mandatory.py", "def test_mandatory():\n    assert True\n")
        self.put("tests/test_native.py", "import subprocess\nimport sys\n"
                 "def test_native():\n"
                 "    child = subprocess.run([sys.executable, '-c', "
                 "\"import json; from pathlib import Path; "
                 "assert json.loads(Path('native/input.json').read_text()) == 'good'\"], "
                 "capture_output=True)\n"
                 "    assert child.returncode == 0\n")
        owned = Path(__file__).resolve().parents[1]
        for name in ("select_tests.py", "pytest_selection.py", "trace_reads.py"):
            self.put("scripts/ci/" + name, (owned / name).read_text(encoding="utf-8"))
            (self.inputs / name).write_bytes((owned / name).read_bytes())
        self.catalog = {"schema": "leaf.ci.test-catalog.v1", "kind": "pytest",
                        "capture_sha": "capture-fixture", "suites": []}
        self.mapping = {"schema": "leaf.ci.test-selection-map.v1", "repo": "fixture/repo",
                        "selection_enabled": True, "phase": "active",
                        "mandatory_suite_ids": ["mandatory"], "always_select_suite_ids": [],
                        "force_full_rules": [], "suites": {}}
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=1)
        self.mapping["window"] = {"id": "fixture-window", "start": start.isoformat(),
                                  "end": (start + dt.timedelta(days=7)).isoformat(),
                                  "fixed_at": (start - dt.timedelta(hours=1)).isoformat()}
        reads = {"a": ["src/a.py", "server/fixture.json", "data/old.json", "data/new.json"],
                 "b": ["src/b.py"], "idle": ["src/idle.py"],
                 "mandatory": ["tests/test_mandatory.py"], "native": ["native/input.json"]}
        for sid, paths in reads.items():
            nodeid = "tests/test_" + sid + ".py::test_" + sid
            self.catalog["suites"].append({"id": sid, "module": nodeid.split("::")[0],
                                            "test_ids": [nodeid], "cwd": ".",
                                            "command": ["python", "-m", "pytest"],
                                            "skip_rule": None, "floor": 0,
                                            "trace_kind": "native" if sid == "native" else "python",
                                            "classification": "mapped", "python_only": sid != "native"})
            self.mapping["suites"][sid] = {
                "mappable": sid != "native", "full_run_ids": ["run-a", "run-b", "run-c"],
                "test_ids": [nodeid], "read_paths": paths, "evidence_refs": ["trace-fixture"],
                "collection_stable": True, "children_complete": True, "capture_complete": True,
                "generated_inputs_resolved": True, "shared_state_closure_resolved": True,
                "shared_state_dependencies": [], "generated_rules": []}
        self.mapping["catalog_sha256"] = selector.catalog_info(self.catalog)[1]
        self.put(selector.MAP_PATH, json.dumps(self.mapping, sort_keys=True) + "\n")
        self.git("add", ".")
        self.git("commit", "-m", "trusted baseline")
        self.trusted = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "feature")
        self.pr = next(n for n in range(1, 100) if hashlib.sha256(
            ("fixture/repo:" + str(n)).encode()).digest()[0] % 2 == 1)
        self.refresh_inputs()

    def git(self, *args):
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(self.repo), *args],
                                capture_output=True, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode("utf-8").strip()

    def put(self, path, text):
        dest = self.repo / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-m", "candidate change")
        return self.git("rev-parse", "HEAD")

    def change(self, path="src/a.py", text="good\n\n"):
        self.put(path, text)
        return self.commit()

    def rebase_fixture(self):
        """Publish a new fake trusted baseline before constructing the candidate."""
        self.mapping["catalog_sha256"] = selector.catalog_info(self.catalog)[1]
        self.put(selector.MAP_PATH, json.dumps(self.mapping, sort_keys=True) + "\n")
        self.commit()
        self.trusted = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/heads/main", self.trusted)
        self.refresh_inputs()

    def refresh_inputs(self):
        (self.inputs / "map.json").write_text(json.dumps(self.mapping, sort_keys=True) + "\n",
                                              encoding="utf-8", newline="\n")
        (self.inputs / "catalog.json").write_text(json.dumps(self.catalog), encoding="utf-8")

    def evidence(self):
        head = self.git("rev-parse", "HEAD")
        return {"provider": "github", "initiator": "GitHub-Hook", "provider_bound": True,
                "build_id": "fixture-build", "evidence_ref": "trusted-producer/event",
                "event": "PULL_REQUEST_UPDATED", "repo": "fixture/repo", "pr_number": self.pr,
                "source_version": "pr/" + str(self.pr), "head_sha": head,
                "head_ref": "refs/heads/feature", "target_ref": "refs/heads/main",
                "target_sha": self.git("rev-parse", "main"),
                "merge_gate": {"provider_bound": True, "kind": "github-merge-queue",
                               "queue_protected": True, "webhook_filter": True, "full_status": True,
                               "evidence_ref": "trusted-producer/gate", "head_sha": head,
                               "repo": "fixture/repo", "revoked_map_shas": [],
                               "map_sha": self.git("rev-parse", self.trusted + ":" + selector.MAP_PATH)}}

    def choose(self, event=None, env=None, expected_rc=0):
        event = self.evidence() if event is None else event
        event_path = self.inputs / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        command = [sys.executable, "-I", "-B", str(self.inputs / "select_tests.py"), "decide",
                   "--repo", str(self.repo), "--trusted-sha", self.trusted,
                   "--head-sha", self.git("rev-parse", "HEAD"),
                   "--map", str(self.inputs / "map.json"), "--catalog", str(self.inputs / "catalog.json"),
                   "--event-evidence", str(event_path), "--out-dir", str(self.out)]
        result = subprocess.run(command, capture_output=True, env=env or self.env, timeout=30)
        self.assertEqual(result.returncode, expected_rc, result.stderr.decode(errors="replace"))
        return json.loads((self.out / "decision.json").read_text(encoding="utf-8"))

    def assert_full(self, decision, reason=None):
        self.assertEqual(decision["execution_mode"], "full")
        self.assertFalse(decision["apply_filter"])
        if reason:
            self.assertIn(reason, decision["reasons"])
        self.assertEqual((self.out / "only-args.nul").read_bytes(), b"")

    def run_pytest(self, extra=()):
        evidence = self.work / ("pytest-evidence-" + str(len(list(self.work.glob("pytest-evidence-*")))))
        env = dict(self.env, PYTHONPATH=str(self.inputs))
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--rootdir", str(self.repo),
             "-p", "pytest_selection", "--leaf-selection", str(self.out / "decision.json"),
             "--leaf-catalog", str(self.inputs / "catalog.json"), "--leaf-repo", str(self.repo),
             "--leaf-output", str(evidence), *extra, "tests"], cwd=self.repo, env=env,
            capture_output=True, timeout=60)
        completions = list(evidence.glob("completion-*.json"))
        self.assertEqual(len(completions), 1, result.stderr.decode(errors="replace"))
        completion = json.loads(completions[0].read_text(encoding="utf-8"))
        return result, completion, evidence

    def make_web(self):
        self.catalog["kind"] = "web"
        self.catalog["runner_path"] = "scripts/run-all-gates.py"
        self.catalog["runner_blob_sha"] = self.git("rev-parse", self.trusted + ":scripts/run-all-gates.py")
        self.rebase_fixture()

    def test_mixed_mapped(self):
        self.put("src/a.py", "good\n\n")
        self.change("src/b.py", "good\n\n")
        decision = self.choose()
        self.assertEqual(decision["execution_mode"], "selected")
        self.assertEqual(decision["expanded_suite_ids"], ["a", "b", "mandatory", "native"])
        self.assertEqual(decision["changed_paths"], ["src/a.py", "src/b.py"])

    def test_cross_area_read(self):
        self.change("server/fixture.json", '{"value":"bad"}\n')
        decision = self.choose()
        self.assertEqual(decision["execution_mode"], "selected")
        self.assertIn("a", decision["expanded_suite_ids"])
        result, completion, evidence = self.run_pytest()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(row["nodeid"] == "tests/test_a.py::test_a" and row["outcome"] == "failed"
                            for row in completion["attempts"]), completion["attempts"])
        docs = [json.loads(path.read_text()) for path in evidence.glob("readsets/a/*/*.json")
                if not path.name.endswith(".misses.json")]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["nodeids"], ["tests/test_a.py::test_a"])
        self.assertEqual(docs[0]["test_ids"], ["a::tests/test_a.py::test_a"])
        self.assertIn("server/fixture.json", [row["path"] for row in docs[0]["reads"]])
        self.assertEqual(trace_reads.repo_relative_path(self.repo, self.repo / "tests/test_a.py"),
                         "tests/test_a.py")
        self.assertEqual(trace_reads.repo_nodeid(self.repo, "tests/test_a.py::test_a[x/y]"),
                         "tests/test_a.py::test_a[x/y]")

    def test_rename(self):
        self.git("mv", "data/old.json", "data/unknown.json")
        self.commit()
        decision = self.choose()
        self.assert_full(decision, "unknown_path")
        self.assertEqual(decision["changed_paths"], ["data/old.json", "data/unknown.json"])

    def test_deletion(self):
        (self.repo / "data/old.json").unlink()
        self.commit()
        decision = self.choose()
        self.assertEqual(decision["execution_mode"], "selected")
        self.assertIn("a", decision["expanded_suite_ids"])

    def test_unknown_path(self):
        self.change("docs/new.md", "ownership is not observed dependency evidence\n")
        self.assert_full(self.choose(), "unknown_path")

    def test_shared_fixture(self):
        self.change("tests/conftest.py", "# shared fixture changed\n")
        self.assert_full(self.choose(), "policy_path_changed")

    def test_missing_base(self):
        self.change()
        event = self.evidence()
        event["target_ref"] = "refs/heads/missing"
        self.assert_full(self.choose(event), "git_unreadable")
        # Ambiguous merge bases cannot become an arbitrary eligible base.
        event_path = self.inputs / "event.json"
        event_path.write_text(json.dumps(self.evidence()), encoding="utf-8")
        actual_run = selector.Git.run
        def two_bases(instance, *args):
            if args[0] == "merge-base":
                return ((self.trusted + "\n") * 2).encode()
            return actual_run(instance, *args)
        with mock.patch.object(selector.Git, "run", two_bases):
            decision = selector.decide(self.repo, self.trusted, self.git("rev-parse", "HEAD"),
                                       self.inputs / "map.json", event_path,
                                       catalog_path=self.inputs / "catalog.json", environ={})
        self.assertEqual(decision["reasons"], ["missing_or_ambiguous_base"])

    def test_empty_diff(self):
        self.assert_full(self.choose(), "empty_diff")
        self.mapping["mandatory_suite_ids"] = []
        self.rebase_fixture()
        self.change()
        self.assert_full(self.choose(), "missing_mandatory_id")
        with self.assertRaisesRegex(selector.InvalidInput, "empty_selection"):
            selector.expand_only([], ["a"])

    def test_unmatched_only(self):
        self.make_web()
        self.change()
        decision = self.choose()
        self.assertEqual(decision["execution_mode"], "selected")
        decision["proposed_suite_ids"] = ["does-not-exist"]
        selector.write_json(self.out / "decision.json", decision)
        result = subprocess.run([sys.executable, "-I", "-B", str(self.inputs / "select_tests.py"),
                                 "emit-web-args", "--decision", str(self.out / "decision.json"),
                                 "--catalog", str(self.inputs / "catalog.json"),
                                 "--out", str(self.out / "only-args.nul")],
                                env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        decision = json.loads((self.out / "decision.json").read_text())
        self.assert_full(decision, "unmatched_only")

    def test_substring_collision(self):
        self.mapping["suites"]["a-extra"] = self.mapping["suites"].pop("b")
        next(row for row in self.catalog["suites"] if row["id"] == "b")["id"] = "a-extra"
        self.make_web()
        self.change()
        decision = self.choose()
        # 'a' matches a, mandatory and native. The actual substring union is recorded.
        expected = sorted({sid for sid in decision["catalog_suite_ids"]
                           if any(value in sid for value in decision["proposed_suite_ids"])})
        self.assertEqual(decision["expanded_suite_ids"], expected)
        self.assertNotIn("a-extra", decision["proposed_suite_ids"])
        self.assertIn("a-extra", decision["expanded_suite_ids"])
        data = selector.validated_web_args(decision, self.catalog)
        words = data.split(b"\x00")[:-1]
        self.assertTrue(all(word == b"--only" for word in words[::2]))
        self.assertEqual([word.decode() for word in words[1::2]], decision["proposed_suite_ids"])
        self.assertEqual(selector.expand_only(["auth"], ["auth", "auth-envelope"]),
                         ["auth", "auth-envelope"])

    def test_collection_drift(self):
        self.change()
        self.choose()
        self.put("tests/test_a.py", (self.repo / "tests/test_a.py").read_text() +
                 "\ndef test_added():\n    assert True\n")
        result, completion, _ = self.run_pytest()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(completion["decision"]["execution_mode"], "full")
        self.assertIn("collection_drift", completion["decision"]["reasons"])
        self.assertTrue((self.repo / "ran_idle").exists())
        baseline = self.choose()
        ids = baseline["expected_collection_ids"]
        for changed in (ids[:-1], [tid + "[changed]" for tid in ids]):
            with self.subTest(ids=changed):
                revised = plugin.validate_collection_or_choose_full(changed, baseline, self.catalog)
                self.assertFalse(revised["apply_filter"])

    def test_catalog_change(self):
        self.change()
        for field, value in (("cwd", "elsewhere"), ("command", ["false"]),
                             ("floor", 9), ("skip_rule", "skip-everything")):
            changed = copy.deepcopy(self.catalog)
            changed["suites"][0][field] = value
            (self.inputs / "catalog.json").write_text(json.dumps(changed))
            self.assert_full(self.choose(), "catalog_change")

    def test_hostile_policy(self):
        self.change(selector.MAP_PATH, '{"suites":{}}\n')
        decision = self.choose()
        self.assert_full(decision, "policy_path_changed")
        self.assertEqual(decision["map_sha"], self.git("rev-parse", self.trusted + ":" + selector.MAP_PATH))
        # Passing candidate bytes directly also fails the frozen-blob check.
        (self.inputs / "map.json").write_text('{"suites":{}}\n')
        self.assert_full(self.choose(), "untrusted_input")

    def test_selector_self_rewrite(self):
        sentinel = self.work / "sentinel"
        self.change("scripts/ci/select_tests.py", "from pathlib import Path\nPath(" +
                    repr(str(sentinel)) + ").write_text('candidate executed')\n")
        # Extract the trusted commit blob, never the modified working-tree helper.
        trusted_source = self.git("show", self.trusted + ":scripts/ci/select_tests.py") + "\n"
        (self.inputs / "select_tests.py").write_text(trusted_source, encoding="utf-8")
        self.assert_full(self.choose(), "policy_path_changed")
        self.assertFalse(sentinel.exists())

    def test_non_pr_event(self):
        self.change()
        for event_name in ("PUSH", "MANUAL", "MERGE_GROUP", "FORGE", "unknown"):
            event = self.evidence()
            event["event"] = event_name
            self.assert_full(self.choose(event), "non_pr_event")
        event = self.evidence()
        event["head_ref"] = "refs/heads/gh-readonly-queue/main/pr-1"
        self.assert_full(self.choose(event), "merge_group")
        event = self.evidence()
        event["source_version"] = "pr/99999"
        self.assert_full(self.choose(event), "contradictory_event")

    def test_terraform_override_env(self):
        self.change()
        for name in ("PR_BASE_SHA", "BASE_SHA"):
            for value in ("", self.trusted):
                with self.subTest(name=name, value=value):
                    self.assert_full(self.choose(env=dict(self.env, **{name: value})), "base_override_env")

    def test_selector_exception(self):
        self.change()
        (self.inputs / "map.json").unlink()
        self.assert_full(self.choose(), "unreadable_input")
        (self.inputs / "map.json").write_text("not json")
        self.assert_full(self.choose(), "unreadable_input")
        self.refresh_inputs()
        event_path = self.inputs / "event.json"
        event_path.write_text(json.dumps(self.evidence()))
        with mock.patch.object(selector, "decide", side_effect=RuntimeError("synthetic exception")):
            rc = selector.main(["decide", "--repo", str(self.repo), "--trusted-sha", self.trusted,
                                "--head-sha", self.git("rev-parse", "HEAD"), "--map", str(self.inputs / "map.json"),
                                "--out-dir", str(self.out)])
        self.assertEqual(rc, 1)
        self.assert_full(json.loads((self.out / "decision.json").read_text()), "selector_exception")

    def test_selected_failure_no_rerun(self):
        # A green baseline proves the failure belongs to the committed mutation.
        self.change()
        self.choose()
        baseline, _, _ = self.run_pytest()
        self.assertEqual(baseline.returncode, 0)
        self.change("src/a.py", "bad\n")
        self.assertEqual(self.choose()["execution_mode"], "selected")
        result, completion, _ = self.run_pytest()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(completion["decision"]["execution_mode"], "selected")
        self.assertFalse((self.repo / "ran_idle").exists())
        phases = [r for r in completion["attempts"] if r["nodeid"] == "tests/test_a.py::test_a"]
        self.assertEqual([r["phase"] for r in phases], ["setup", "call", "teardown"])
        self.assertEqual({r["attempt"] for r in phases}, {1})

    def test_retry_first_failure_kept(self):
        self.change()
        decision = self.choose()
        nodeid = "tests/test_a.py::test_a"
        recorder = plugin.SelectionPlugin.__new__(plugin.SelectionPlugin)
        recorder.capture = trace_reads.Capture(self.repo)
        recorder.worker = "main"
        recorder.nodeids = {nodeid: nodeid}
        recorder.attempt_numbers = {}
        recorder.attempts = []
        ledger = self.work / "retry-attempts.jsonl"
        with ledger.open("ab", buffering=0) as stream:
            recorder.attempt_stream = stream
            for outcome in ("failed", "passed"):
                for phase in ("setup", "call", "teardown"):
                    recorder.pytest_runtest_logreport(types.SimpleNamespace(
                        nodeid=nodeid, when=phase, outcome=outcome if phase == "call" else "passed"))
        attempts = [json.loads(line) for line in ledger.read_text().splitlines()]
        evidence = plugin.shadow_evidence(attempts, decision["selected_test_ids"],
                                          decision["assigned_arm"], "full")
        self.assertEqual(evidence["full_failed_ids"], [nodeid])
        self.assertTrue(evidence["contained"])
        self.assertEqual(attempts[-1]["outcome"], "passed")
        self.assertEqual(len(attempts), 6)
        self.assertEqual([r["attempt"] for r in attempts], [1, 1, 1, 2, 2, 2])

    def test_trace_failure(self):
        self.mapping["suites"]["idle"]["capture_complete"] = False
        self.mapping["suites"]["b"]["full_run_ids"] = ["same-run", "same-run", "same-run"]
        self.rebase_fixture()
        self.change()
        decision = self.choose()
        self.assertIn("idle", decision["expanded_suite_ids"])
        self.assertIn("b", decision["expanded_suite_ids"])
        capture = trace_reads.Capture(self.repo).install()
        self.addCleanup(capture.close)
        capture.begin_module("tests/test_a.py", "tests/test_a.py::test_a")
        with mock.patch.object(capture, "record_path", side_effect=ValueError("bad path")):
            capture.audit("open", ("src/a.py", "r", 0))
        capture.end_module()
        doc = trace_reads.write_readset(self.work / "broken-readset.json", capture=capture,
                                       suite_id="a", module="tests/test_a.py", complete=True,
                                       python_only=True, run_id="run", source_sha=self.trusted,
                                       source_tree="tree", capture_sha="capture", catalog_sha256="catalog")
        self.assertFalse(doc["capture_complete"])
        self.assertIn("audit_record_error", doc["incomplete_reasons"])

    def test_new_read_edge(self):
        self.change()
        self.choose()
        capture = trace_reads.Capture(self.repo).install()
        self.addCleanup(capture.close)
        capture.begin_module("a")
        (self.repo / "server/fixture.json").read_text()
        capture.end_module()
        doc = capture.document("a", complete=True, python_only=True, run_id="run",
                               source_sha=self.trusted, source_tree="tree", capture_sha="capture",
                               catalog_sha256=self.mapping["catalog_sha256"])
        misses = trace_reads.new_read_edges(doc, [], "map-identity")
        self.assertTrue(any(row["path"] == "server/fixture.json" for row in misses))
        self.assertTrue(all(row["revocation_requested"] for row in misses))
        # Simulate authenticated collector readback, not candidate re-enablement.
        event = self.evidence()
        event["merge_gate"]["revoked_map_shas"] = [event["merge_gate"]["map_sha"]]
        self.assert_full(self.choose(event), "map_revoked")

    def test_quarantine(self):
        self.put("src/a.py", "good\n\n")
        self.change("src/b.py", "good\n\n")
        self.choose()
        quarantine_before = (self.repo / "quarantine.txt").read_bytes()
        result, _, evidence = self.run_pytest(("--deselect=tests/test_b.py::test_b",))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        manifest = json.loads(next(evidence.glob("collection-*.json")).read_text())
        self.assertEqual(manifest["quarantine_deselected_ids"], ["tests/test_b.py::test_b"])
        self.assertIn("tests/test_idle.py::test_idle", manifest["selection_deselected_ids"])
        self.assertNotIn("tests/test_b.py::test_b", manifest["selection_deselected_ids"])
        self.assertFalse((self.repo / "ran_b").exists())
        self.assertEqual((self.repo / "quarantine.txt").read_bytes(), quarantine_before)

    def test_trusted_disable_switch(self):
        self.mapping["selection_enabled"] = False
        self.rebase_fixture()
        self.change()
        decision = self.choose()
        self.assertEqual(decision["assigned_arm"], "selected")
        self.assert_full(decision, "policy_disabled")
        result, completion, _ = self.run_pytest()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(completion["full_run_complete"])
        self.assertTrue((self.repo / "ran_idle").exists())

    def test_refute_cross_process(self):
        self.change()
        baseline = self.choose()
        result, _, _ = self.run_pytest()
        self.assertEqual(result.returncode, 0)
        self.change("native/input.json", '"bad"\n')
        decision = self.choose()
        self.assertIn("native", baseline["expanded_suite_ids"])
        self.assertIn("native", decision["expanded_suite_ids"])
        result, completion, evidence = self.run_pytest()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(row["nodeid"] == "tests/test_native.py::test_native" and
                            row["outcome"] == "failed" for row in completion["attempts"]))
        docs = [json.loads(p.read_text()) for p in evidence.glob("readsets/native/*/*.json")
                if not p.name.endswith(".misses.json")]
        self.assertTrue(docs)
        self.assertTrue(all(not doc["capture_complete"] for doc in docs))
        self.assertTrue(any("child-requires-trace" in doc["incomplete_reasons"] for doc in docs))

    def test_refute_policy_self_replacement(self):
        sentinel = self.work / "policy-sentinel"
        malicious = "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('bad')\n"
        self.put("scripts/ci/select_tests.py", malicious)
        self.put(selector.MAP_PATH, '{"mandatory_suite_ids":[],"suites":{}}\n')
        self.put("json.py", malicious)
        self.commit()
        decision = self.choose()
        self.assert_full(decision, "policy_path_changed")
        self.assertFalse(sentinel.exists())
        self.assertEqual(decision["trusted_sha"], self.trusted)
        self.assertEqual(decision["map_sha"], self.git("rev-parse", self.trusted + ":" + selector.MAP_PATH))

    def test_refute_shadow_retries_fallbacks(self):
        self.change("docs/unread.md", "fallback remains in assigned arm\n")
        decision = self.choose()
        self.assert_full(decision, "unknown_path")
        self.assertEqual(decision["assigned_arm"], "selected")
        nodeid = "a::tests/test_a.py::test_a[case-1]"
        attempts = [{"nodeid": nodeid, "attempt": 1, "phase": "call", "outcome": "failed"},
                    {"nodeid": nodeid, "attempt": 2, "phase": "call", "outcome": "passed"}]
        evidence = plugin.shadow_evidence(attempts, [nodeid], decision["assigned_arm"],
                                          decision["execution_mode"])
        self.assertEqual(evidence["assigned_arm"], "selected")
        self.assertEqual(evidence["execution_mode"], "full")
        self.assertTrue(evidence["contained"])
        self.assertEqual(evidence["full_failed_ids"], [nodeid])
        omitted = plugin.shadow_evidence(attempts, [nodeid.replace("case-1", "case-2")], "selected", "full")
        self.assertFalse(omitted["contained"])
        self.assertEqual(attempts[-1]["outcome"], "passed")


if __name__ == "__main__":
    unittest.main()
