"""Off-host selection contracts; git/process outcomes are bounded fixtures."""
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "vitest_related_policy", Path(__file__).with_name("vitest_related_selection.py"))
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)
BASE = "a" * 40
SOURCES = {"web/src/view.jsx": "export const View = () => null",
           "web/src/plain.js": "export const plain = 1"}


def test_base_required_even_with_changed_paths(tmp_path):
    def forbidden(*a, **kw):
        pytest.fail("missing base must not invoke git")
    for env in ({}, {"LEAF_CHANGED_PATHS": '["web/src/plain.js"]'},
                {"TRUSTED_SHA": "origin/main"}, {"LEAF_VITEST_LAST_GREEN_MAIN": "HEAD~1"}):
        decision = policy.collect(tmp_path, env, run=forbidden)
        assert decision["mode"] == "full"
        assert decision["reason"] == "missing_base"


@pytest.mark.parametrize("env,revision,ancestry", [
    ({"TRUSTED_SHA": BASE}, BASE + "...HEAD", "--all"),
    ({"LEAF_VITEST_PR_BASE": BASE, "CODEBUILD_WEBHOOK_EVENT": "PULL_REQUEST_UPDATED"},
     BASE + "...HEAD", "--all"),
    ({"LEAF_VITEST_LAST_GREEN_MAIN": BASE}, BASE + "..HEAD", "--is-ancestor"),
    ({"TRUSTED_SHA": "b" * 40, "LEAF_VITEST_LAST_GREEN_MAIN": BASE,
      "CODEBUILD_WEBHOOK_HEAD_REF": "refs/heads/main"}, BASE + "..HEAD", "--is-ancestor"),
])
def test_explicit_comparison_and_inventory(tmp_path, env, revision, ancestry):
    target = tmp_path / "web/src/view.jsx"
    target.parent.mkdir(parents=True)
    target.write_text(SOURCES["web/src/view.jsx"], encoding="utf-8")
    calls = []

    def git(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 30 and kwargs["cwd"] == tmp_path
        assert "shell" not in kwargs
        if "rev-parse" in argv:
            out = BASE + "\n"
        elif "merge-base" in argv:
            out = "c" * 40 + "\n" if "--all" in argv else ""
        elif "diff" in argv:
            out = "web/src/view.jsx\0"
        else:
            assert "ls-files" in argv
            out = "web/src/view.jsx\0"
        return SimpleNamespace(returncode=0, stdout=out)

    decision = policy.collect(tmp_path, env, run=git)
    assert decision["mode"] == "related"
    assert decision["inputs"] == ["src/view.jsx"]
    assert any(ancestry in call for call in calls)
    diff = next(call for call in calls if "diff" in call)
    assert revision in diff and "-z" in diff and "--no-renames" in diff
    assert "--changed" not in diff


@pytest.mark.parametrize("failure", ["rev-parse", "merge-base", "diff", "ls-files", "timeout", "ambiguous"])
def test_unavailable_git_inputs_fall_back(tmp_path, failure):
    def git(argv, **kw):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        if failure in argv:
            return SimpleNamespace(returncode=1, stdout="")
        if "rev-parse" in argv:
            out = BASE
        elif "merge-base" in argv:
            out = ("b" * 40 + "\n") * (2 if failure == "ambiguous" else 1)
        elif "diff" in argv:
            out = ""
        else:
            out = "web/src/missing.js\0"
        return SimpleNamespace(returncode=0, stdout=out)
    assert policy.collect(tmp_path, {"TRUSTED_SHA": BASE}, run=git)["mode"] == "full"


@pytest.mark.parametrize("path,reason", [
    ("web/src/deleted.js", "deleted_or_missing_path"),
    ("../outside.js", "unsafe_path"),
])
def test_local_diff_deleted_and_unsafe_paths(tmp_path, path, reason):
    def git(argv, **kw):
        if "rev-parse" in argv:
            out = BASE
        elif "merge-base" in argv:
            out = ""
        else:
            assert "diff" in argv  # Unsafe/deleted diff fails before inventory.
            out = path + "\0"
        return SimpleNamespace(returncode=0, stdout=out)
    decision = policy.collect(tmp_path, {"LEAF_VITEST_LAST_GREEN_MAIN": BASE}, run=git)
    assert decision["mode"] == "full" and decision["reason"] == reason


@pytest.mark.parametrize("path", [
    "../web/src/plain.js", "/web/src/plain.js", "web//src/plain.js",
    "web/src/../plain.js", "web/src/a\n.js", "web\\src\\plain.js", "-flag.js", "web/-flag.js",
    "web/src/a&echo.js", "web/src/%SECRET%.js",
])
def test_unsafe_paths_fall_back(path):
    assert policy.select(BASE, [path], SOURCES)["mode"] == "full"


@pytest.mark.parametrize("path", [
    "web/vitest.config.js", "web/vitest.setup.js", "web/package.json",
    "web/package-lock.json", "pnpm-lock.yaml", "web/src/__fixtures__/a.js",
    "web/src/mocks/a.js", "web/src/test-utils/a.js", "web/src/testing/a.js",
    "web/src/generated/input.js", "web/public/engine.wasm", "web/src/vendor/pin.js",
    "web/src/view.css", "web/src/icon.svg", "web/src/testTimeScale.js",
    "other/library.js", "scripts/test-harness.py",
])
def test_global_inputs_fall_back(path):
    assert policy.select(BASE, [path], {**SOURCES, path: ""} if path.startswith("web/")
                         and Path(path).suffix in policy.SOURCE_SUFFIXES else SOURCES)["mode"] == "full"


def test_missing_deleted_source_and_inventory_fall_back():
    assert policy.select(BASE, ["web/src/deleted.js"], SOURCES)["reason"] == "missing_source"
    assert policy.select(BASE, [], None)["reason"] == "inventory_unavailable"
    assert policy.select(BASE, [], {"web/src/a.js": None})["mode"] == "full"


@pytest.mark.parametrize("text", [
    "import { readFile as load } from 'node:fs/promises'",
    "const io = require('fs'); io.readFileSync(path)",
    "import * as disk from 'node:fs'; disk.readdir(path)",
    "const { existsSync: present } = require('fs')",
    "import { spawn as child } from 'node:child_process'",
    "const cp = require('child_process')",
    "new Worker(new URL('./worker.js', import.meta.url))",
    "const fixture = fileURLToPath(new URL('./fixture', import.meta.url))",
    "readFileSync(path)", "readdirSync(path)", "exists(path)",
])
def test_all_source_readers_and_shared_helpers_are_inputs(text):
    sources = {**SOURCES, "web/src/rules.test.js": text, "web/tools/shared.cjs": text}
    decision = policy.select(BASE, ["web/src/view.jsx"], sources)
    assert decision["mode"] == "related"
    assert decision["reader_count"] == 2
    assert decision["inputs"] == ["src/rules.test.js", "src/view.jsx", "tools/shared.cjs"]
    assert "src/plain.js" not in decision["inputs"]


def test_normal_sources_include_readers_and_external_sources_fall_back():
    sources = {**SOURCES, "web/src/read.js": "require('fs')"}
    assert policy.select(BASE, ["web/src/plain.js", "web/src/view.jsx"], sources)["mode"] == "related"
    assert policy.select(BASE, [], sources)["inputs"] == ["src/read.js"]
    decision = policy.select(BASE, ["scripts/shared.js"], sources)
    assert decision["mode"] == "full" and decision["reader_count"] == 1


def test_related_command_preserves_fixed_tests_flags_and_reporters():
    tail = ["--shard=2/3", "--exclude=src/stage.test.js", "--maxWorkers=2",
            "src/fail30.test.jsx", "--reporter=default", "--reporter=/tmp/leaf.mjs"]
    argv = policy.related_argv(["npm", "run", "test:unit", "--", *tail], ["src/a b.jsx"])
    assert argv == ["npx", "vitest", "related", "src/a b.jsx", "--run", "--passWithNoTests", *tail]
    assert "run" not in argv
    assert policy.related_argv(["npm.cmd", "run", "test:unit", "--"], [])[0] == "npx.cmd"


def test_empty_result_requires_explicit_valid_evidence():
    decision = policy.select(BASE, [], SOURCES)
    decision.pop("inputs")
    assert policy.valid_related_result(decision, 1)
    assert not policy.valid_related_result(decision, 0)
    decision["empty"] = True
    assert policy.valid_related_result(decision, 0)
    for executed in (-1, True, None):
        assert not policy.valid_related_result(decision, executed)
    assert not policy.valid_related_result({**decision, "mode": "full"}, 0)
    assert not policy.valid_related_result({**decision, "base": None}, 0)
