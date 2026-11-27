"""Exercise the trusted CI PR merge block in isolated Git repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


CI_PATH = Path(__file__).resolve().parents[1] / ".codebuild/ci.sh"
BASH = shutil.which("bash")
BEGIN = "  # LEAF_PR_MERGE_BEGIN"
END = "  # LEAF_PR_MERGE_END"


class TestPrMergeScriptShape(unittest.TestCase):
    def test_block_placement_and_runner_source(self):
        script = CI_PATH.read_text(encoding="utf-8")
        lines = script.splitlines()
        self.assertEqual(lines.count(BEGIN), 1)
        self.assertEqual(lines.count(END), 1)
        start = lines.index(BEGIN)
        finish = lines.index(END)
        self.assertEqual(lines[start - 1], "  # LEAF_DOCS_ONLY_END")
        self.assertLess(start, finish)
        self.assertTrue(lines[finish + 1].startswith(
            '  if [[ "$trusted_sha_override" != 1 && -n "${LEAF_LOADER_TRUSTED_SHA:-}"'))
        self.assertIn(
            '      candidate_runner_blob="$(git --no-replace-objects rev-parse '
            '"HEAD:scripts/run-all-gates.py" 2>/dev/null || true)"', lines)
        self.assertNotIn('"$HEAD_SHA:scripts/run-all-gates.py"', script)


@unittest.skipUnless(BASH, "bash is required")
class TestPrMerge(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        home = root / "home"
        home.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("CODEBUILD_", "LEAF_", "GIT_"))
                    and key not in ("BASH_ENV", "ENV", "HEAD_SHA", "TRUSTED_SHA",
                                    "trusted_sha_override")}
        self.env.update(HOME=home.as_posix(), GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "CI fixture")
        self.git("config", "user.email", "ci-fixture@invalid")
        self.git("config", "core.autocrlf", "false")
        self.write("common.txt", "original\n")
        self.base = self.commit("A")
        self.git("checkout", "-qb", "pr")
        self.write("p.txt", "PR change\n")
        self.head = self.commit("P")
        self.git("checkout", "-q", "main")
        self.write("m.txt", "main change\n")
        self.trusted = self.commit("M")
        self.git("checkout", "-q", self.head)
        script = CI_PATH.read_text(encoding="utf-8")
        self.block = script.split(BEGIN + "\n", 1)[1].split("\n" + END, 1)[0]

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, name, content):
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def run_block(self, event="PULL_REQUEST_UPDATED", **overrides):
        env = dict(self.env, HEAD_SHA=self.head, TRUSTED_SHA=self.trusted,
                   trusted_sha_override="0")
        if event is not None:
            env["CODEBUILD_WEBHOOK_EVENT"] = event
        env.update(overrides)
        return subprocess.run([BASH, "-c", "set -euo pipefail\n" + self.block],
                              cwd=self.repo, env=env, capture_output=True,
                              text=True, timeout=150)

    def assert_receipt(self, result, state):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tree = self.git("rev-parse", "HEAD^{tree}")
        self.assertIn(
            f"LEAF_PR_MERGE state={state} head={self.head} "
            f"base={self.trusted} tree={tree}", result.stdout.splitlines())

    def test_stale_pr_merges_current_main(self):
        result = self.run_block()
        self.assert_receipt(result, "merged")
        self.assertEqual(self.git("show", "-s", "--format=%P", "HEAD").split(),
                         [self.head, self.trusted])
        self.assertEqual((self.repo / "p.txt").read_text(), "PR change\n")
        self.assertEqual((self.repo / "m.txt").read_text(), "main change\n")

    def test_pr_already_contains_main(self):
        self.git("checkout", "-q", "main")
        self.write("p.txt", "PR change\n")
        self.head = self.commit("PR on current main")
        result = self.run_block()
        self.assert_receipt(result, "up_to_date")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_non_pr_events_leave_head_unchanged(self):
        for event in (None, "PUSH"):
            with self.subTest(event=event):
                result = self.run_block(event=event)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_trusted_sha_override_leaves_head_unchanged(self):
        result = self.run_block(trusted_sha_override="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_disabled_merge_leaves_head_unchanged(self):
        result = self.run_block(LEAF_PR_MERGE="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_conflict_aborts_merge_and_fails(self):
        self.write("common.txt", "PR version\n")
        self.head = self.commit("conflicting PR")
        self.git("checkout", "-q", "main")
        self.write("common.txt", "main version\n")
        self.trusted = self.commit("conflicting main")
        self.git("checkout", "-q", self.head)
        result = self.run_block()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"LEAF_PR_MERGE state=conflict head={self.head} "
                      f"base={self.trusted}", result.stdout)
        self.assertIn("does not merge cleanly with main", result.stderr)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
