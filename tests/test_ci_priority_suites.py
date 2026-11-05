"""Exercise the PR ordering hint in isolation from installs and the gate."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".codebuild/ci.sh"
BASE = "29a36416a7ac484e4d219143e455301c1750dcc5"
BASH = shutil.which("bash")
BEGIN = "# LEAF_PRIORITY_SUITES_BEGIN\n"
END = "# LEAF_PRIORITY_SUITES_END\n"


def priority_block():
    return CI_PATH.read_text(encoding="utf-8").split(BEGIN, 1)[1].split(END, 1)[0]


def suite(sid, path="unrelated.py", cwd="."):
    return {"id": sid, "cwd": cwd, "label": "server/tests/test_changed.py",
            "command": ["<PYTHON>", "-m", "pytest", path]}


def hint_counts(result):
    lines = [line.removeprefix("PRIORITY_HINT ") for line in result.stdout.splitlines()
             if line.startswith("PRIORITY_HINT ")]
    assert len(lines) == 1
    counts = json.loads(lines[0])
    assert lines[0] == json.dumps(counts, separators=(",", ":"))
    return counts


class Build:
    def __init__(self, root):
        self.repo = root / "checkout with spaces"
        self.repo.mkdir()
        self.selection = root / "trusted-selection"
        self.selection.mkdir()
        self.output = root / "gate-results" / "priority-suites.json"
        self.git("init", "--quiet")
        self.git("config", "user.name", "CI fixture")
        self.git("config", "user.email", "ci@example.invalid")
        self.commit({"server/tests/test_changed.py": "before\n",
                     "src/shared.py": "before\n"})
        self.trusted = self.head
        self.declarations([suite("direct", "tests/test_changed.py", "server"),
                           suite("trailer"), suite("mapped")],
                          {"mapped": {"read_paths": ["src/shared.py"]}})

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, changes=None, message="candidate"):
        for name, content in (changes or {}).items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "--cleanup=verbatim", "-m", message)
        self.head = self.git("rev-parse", "HEAD")

    def declarations(self, suites, mapping=None):
        for name, payload in (
                ("catalog.json", {"schema": "leaf.ci.test-catalog.v1", "suites": suites}),
                ("test-selection-map.json", {"schema": "leaf.ci.test-selection-map.v1",
                                             "suites": mapping or {}})):
            (self.selection / name).write_text(json.dumps(payload), encoding="utf-8")

    def run(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEBUILD_", "LEAF_", "GIT_"))
               and key not in ("BASH_ENV", "trusted_sha_override")}
        env.update(selection_dir=str(self.selection), CODEBUILD_SRC_DIR=str(self.repo),
                   TRUSTED_SHA=self.trusted, HEAD_SHA=self.head,
                   CODEBUILD_SOURCE_VERSION="pr/7", CODEBUILD_WEBHOOK_EVENT="PULL_REQUEST_UPDATED",
                   CODEBUILD_WEBHOOK_HEAD_REF="refs/heads/feature",
                   CODEBUILD_WEBHOOK_BASE_REF="refs/heads/main",
                   LEAF_PRIORITY_SUITES_FILE="ambient-value", trusted_sha_override="0")
        env.update(overrides)
        body = priority_block().replace("/tmp/gate-results", str(self.output.parent))
        result = subprocess.run(
            [BASH, "-c", "set -euo pipefail\n" + body
             + '\nprintf "PRIORITY_EXPORT=%s\\n" "${LEAF_PRIORITY_SUITES_FILE-UNSET}"\n'],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        hint = json.loads(self.output.read_text()) if self.output.is_file() else None
        if hint is not None:
            assert set(hint) == {"suite_ids"}
            assert isinstance(hint["suite_ids"], list)
            assert all(isinstance(sid, str) for sid in hint["suite_ids"])
            hint_counts(result)
        else:
            assert "PRIORITY_HINT " not in result.stdout
        expected_export = str(self.output) if hint is not None else "UNSET"
        assert "PRIORITY_EXPORT=" + expected_export + "\n" in result.stdout
        return result, hint


@pytest.fixture
def build(tmp_path):
    if not BASH:
        pytest.skip("bash is absent; priority block execution needs bash")
    return Build(tmp_path)


def test_trusted_argv_then_trailer_then_map_order(build):
    rows = [suite("mapped"), suite("trailer"),
            suite("direct", "tests/test_changed.py::test_case", "server"),
            suite("web-direct", "src/model.test.js", "web"),
            suite("parent-direct", "<REPO>/platform/tests/test_changed.py", ".."),
            suite("label-only"), suite("substring", "tests/test_changed.py.extra", "server"),
            suite("directory", "tests", "server"),
            {"id": "snippet", "cwd": "server", "command": [
                "<PYTHON>", "-c", "run('tests/test_changed.py')"]}]
    build.declarations(rows, {"mapped": {"mappable": False, "read_paths": ["src/shared.py"]},
                              "direct": {"read_paths": ["src/shared.py"]},
                              "unknown-map-suite": {"read_paths": ["src/shared.py"]}})
    build.commit({"server/tests/test_changed.py": "after\n", "src/shared.py": "after\n",
                  "web/src/model.test.js": "changed\n",
                  "platform/tests/test_changed.py": "changed\n",
                  # Candidate declarations are deliberately unrelated to trusted inputs.
                  "scripts/ci/test-selection-map.json": "not trusted JSON\n"},
                 "candidate\n\nLeaf-Impact-Suites: trailer, direct, unknown, bad/id\n")
    result, hint = build.run()
    assert hint["suite_ids"] == ["direct", "web-direct", "parent-direct", "trailer", "mapped"]
    assert hint_counts(result) == dict(argv=3, trailer=1, read_paths=1, unknown=2,
                                       malformed=1, duplicates=2, over_limit=0)
    assert "INFO:" not in result.stdout


@pytest.mark.parametrize("path", ["test space.py", "test_line\nbreak.py", "test_cr\r.py",
                                  "$(touch PATH_EXECUTED).py"])
def test_changed_paths_are_whole_data_tokens(build, path):
    build.declarations([suite("direct", "tests/" + path, "server"),
                        suite("wrong-path", "tests/" + path.splitlines()[0] + ".extra", "server")])
    build.commit({"server/tests/" + path: "changed\n"})
    _, hint = build.run()
    assert hint["suite_ids"] == ["direct"]
    assert not (build.repo / "PATH_EXECUTED").exists()


def test_trailers_are_inert_physical_lines_with_bounded_valid_ids(build):
    build.commit(message=(
        "candidate\n\nLeaf-Impact-Suites: trailer, unknown, BAD, bad/id, "
        "$(touch SUBSTITUTION), `touch BACKTICK`, direct\r, " + "x" * 65 + "\n"
        " mapped\nLeaf-Impact-Suites:\nmapped\n"
        "Leaf-Impact-Suites: trailer\nLEAF_PRIORITY_SUITES\ntouch HEREDOC\n"
        "Leaf-Impact-Suites: mapped\u2028direct\n"))
    result, hint = build.run()
    assert hint["suite_ids"] == ["trailer"]
    counts = hint_counts(result)
    assert counts["unknown"] == 1
    assert counts["malformed"] == 8
    assert counts["duplicates"] == 1
    assert not any((build.repo / name).exists()
                   for name in ("SUBSTITUTION", "BACKTICK", "HEREDOC"))


def test_deduplication_and_cap_preserve_source_precedence(build):
    ids = [f"suite-{index:03}" for index in range(205)]
    rows = [suite(sid) for sid in ids[:-1]] + [
        suite(ids[-1], "tests/test_changed.py", "server")]
    build.declarations(rows, {ids[-2]: {"read_paths": ["src/shared.py"]}})
    build.commit({"server/tests/test_changed.py": "changed\n", "src/shared.py": "changed\n"},
                 "candidate\n\nLeaf-Impact-Suites: " + ",".join(
                     ids + [ids[0], ids[-1], "unknown", "invalid/id"]) + "\n")
    result, hint = build.run()
    assert hint["suite_ids"] == [ids[-1], *ids[:199]]
    assert len(set(hint["suite_ids"])) == 200
    assert hint_counts(result) == dict(argv=1, trailer=199, read_paths=0, unknown=1,
                                       malformed=1, duplicates=4, over_limit=5)


def test_empty_diff_and_no_hints_write_empty_advisory_list(build):
    build.commit()
    result, hint = build.run()
    assert hint == {"suite_ids": []}
    assert hint_counts(result) == dict(
        argv=0, trailer=0, read_paths=0, unknown=0, malformed=0, duplicates=0, over_limit=0)


def test_written_hint_is_accepted_by_runner(build, monkeypatch, capsys):
    runner_path = ROOT / "scripts/run-all-gates.py"
    definitions = ast.parse(runner_path.read_text(encoding="utf-8")).body
    if not any(isinstance(node, ast.FunctionDef) and node.name == "load_priority_suites"
               for node in definitions):
        pytest.skip("runner slice not on this base")
    spec = importlib.util.spec_from_file_location("leaf_priority_gate_runner", runner_path)
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    previous = list(sys.path)
    try:
        spec.loader.exec_module(runner)
    finally:
        sys.path[:] = previous
    build.commit({"server/tests/test_changed.py": "after\n", "src/shared.py": "after\n"},
                 "candidate\n\nLeaf-Impact-Suites: trailer\n")
    _, hint = build.run()
    expected = ["direct", "trailer", "mapped"]
    assert hint == {"suite_ids": expected}
    monkeypatch.setenv("LEAF_PRIORITY_SUITES_FILE", str(build.output))
    suites = [runner.Suite(sid, sid, "script", build.repo, ["unused"], None)
              for sid in ("mapped", "direct", "trailer")]
    assert runner.load_priority_suites(suites) == frozenset(expected)
    assert "PRIORITY ignored" not in capsys.readouterr().out


def test_diff_uses_merge_base_and_ignores_trusted_branch_only_changes(build):
    build.commit({"server/tests/test_changed.py": "candidate change\n"})
    candidate = build.head
    build.git("checkout", "--quiet", "--detach", build.trusted)
    build.commit({"src/shared.py": "trusted branch only\n"})
    build.trusted = build.head
    build.git("checkout", "--quiet", "--detach", candidate)
    build.head = candidate
    _, hint = build.run()
    assert hint["suite_ids"] == ["direct"]


@pytest.mark.parametrize("name", ["catalog.json", "test-selection-map.json"])
@pytest.mark.parametrize("failure", ["missing", "unreadable", "malformed", "oversized"])
def test_input_failure_removes_stale_hint_and_leaves_export_unset(build, name, failure):
    path = build.selection / name
    if failure in ("missing", "unreadable"):
        path.unlink()
        if failure == "unreadable":
            path.mkdir()
    else:
        path.write_text("{" if failure == "malformed" else " " * (8 * 1024 * 1024 + 1))
    build.output.parent.mkdir()
    build.output.write_text('{"suite_ids":["stale"]}')
    result, hint = build.run()
    assert hint is None
    assert result.stdout == ("INFO: priority suite hints unavailable; existing gate order retained\n"
                             "PRIORITY_EXPORT=UNSET\n")
    assert not result.stderr
    assert list(build.output.parent.iterdir()) == []


@pytest.mark.parametrize("declaration", [
    {"id": "BAD", "command": [], "cwd": "."},
    {"id": "direct", "command": "tests/test_changed.py", "cwd": "."},
    {"id": "direct", "command": [123], "cwd": "."},
])
def test_invalid_catalog_falls_back(build, declaration):
    build.declarations([declaration])
    result, hint = build.run()
    assert hint is None
    assert result.stdout.count("INFO:") == 1


def test_invalid_read_paths_fall_back_even_after_valid_argv_match(build):
    build.declarations([suite("direct", "tests/test_changed.py", "server")],
                       {"direct": {"read_paths": "src/shared.py"}})
    build.commit({"server/tests/test_changed.py": "changed\n"})
    result, hint = build.run()
    assert hint is None
    assert result.stdout.count("INFO:") == 1


def test_write_failure_leaves_export_unset(build):
    build.output.parent.write_text("not a directory\n")
    result, hint = build.run()
    assert hint is None
    assert result.stdout.count("INFO:") == 1
    assert not result.stderr


@pytest.mark.parametrize("overrides", [
    {"CODEBUILD_SOURCE_VERSION": "main", "CODEBUILD_WEBHOOK_EVENT": "PUSH",
     "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/main"},
    {"CODEBUILD_SOURCE_VERSION": "a" * 40, "CODEBUILD_WEBHOOK_EVENT": ""},
    {"CODEBUILD_SOURCE_VERSION": "pr/7", "trusted_sha_override": "1"},
    {"CODEBUILD_SOURCE_VERSION": "a" * 40, "CODEBUILD_WEBHOOK_HEAD_REF": "refs/pull/7/head"},
    {"CODEBUILD_SOURCE_VERSION": "pr/0"},
    {"CODEBUILD_WEBHOOK_EVENT": "PUSH"},
    {"CODEBUILD_WEBHOOK_EVENT": ""},
])
def test_main_pin_and_other_non_pr_builds_write_nothing(build, overrides):
    build.commit(message="Leaf-Impact-Suites: trailer\n")
    result, hint = build.run(**overrides)
    assert hint is None
    assert not build.output.parent.exists()
    assert result.stdout == "PRIORITY_EXPORT=UNSET\n"
    assert not result.stderr


@pytest.mark.parametrize("provider", [
    {"CODEBUILD_INITIATOR": "states/leaf-forge-ci-relay"},
    {"CODEBUILD_BUILD_ID": "device:dv-0123456789abcdef"},
])
def test_existing_relay_and_device_pr_detection_is_supported(build, provider):
    build.commit(message="Leaf-Impact-Suites: trailer\n")
    _, hint = build.run(CODEBUILD_SOURCE_VERSION=build.head,
                        CODEBUILD_WEBHOOK_HEAD_REF="refs/pull/7/head", **provider)
    assert hint["suite_ids"] == ["trailer"]


@pytest.mark.parametrize("overrides", [{"HEAD_SHA": "invalid"}, {"HEAD_SHA": "f" * 40},
                                        {"TRUSTED_SHA": "f" * 40}])
def test_git_input_errors_fall_back_without_export(build, overrides):
    result, hint = build.run(**overrides)
    assert hint is None
    assert result.stdout.count("INFO:") == 1
    assert not result.stderr


def test_gate_argv_and_every_existing_block_are_byte_identical_to_base():
    original = subprocess.check_output(["git", "show", f"{BASE}:.codebuild/ci.sh"], cwd=ROOT)
    current = CI_PATH.read_bytes()
    assert current.count(BEGIN.encode()) == current.count(END.encode()) == 1
    without_hint = re.sub(rb"# LEAF_PRIORITY_SUITES_BEGIN\n.*?# LEAF_PRIORITY_SUITES_END\n\n",
                          b"", current, count=1, flags=re.DOTALL)
    assert without_hint == original
    prefix = b'python scripts/run-all-gates.py --jobs "${LEAF_GATE_JOBS:-auto}"'
    assert [line for line in current.splitlines(keepends=True) if line.startswith(prefix)] == [
        line for line in original.splitlines(keepends=True) if line.startswith(prefix)]
    block = priority_block()
    assert block.count("python -I -B - ") == 1
    assert "<<'LEAF_PRIORITY_SUITES'" in block
    assert not re.search(r"\beval\b|shell\s*=\s*True", block)
    assert current.index(END.encode()) < current.index(b"# Capture helpers")
