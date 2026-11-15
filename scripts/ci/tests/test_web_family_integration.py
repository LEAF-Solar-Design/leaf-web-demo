"""Focused web_family runner contracts; subprocesses are doubles, never suites."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SAFE = ["server/tests/test_catalog_digest_boundary.py"]


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "web_family_integration_runner", ROOT / "scripts/run-all-gates.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.delenv("LEAF_PRIORITY_SUITES", raising=False)
    monkeypatch.delenv("LEAF_PROCESS_CAPTURE_CONTEXT", raising=False)
    monkeypatch.delenv("LEAF_PROCESS_CAPTURE_OUT", raising=False)
    monkeypatch.delenv("TRUSTED_SHA", raising=False)
    return module


def tier_for(suites):
    ids = [s.id for s in suites]
    return {"suite_ids": ids, "catalog_suite_ids": ids, "prerequisites": {}}


def family_ids(g, suites):
    return {r["id"] for r in g.web_family_rows(suites)
            if r["cwd"] == "web" or r["id"] in
            {"studio-walk-regressions", "web-demo-gate"}}


def test_web_family_real_catalog_safe_backend_drops_entire_family(runner, monkeypatch):
    g = runner
    catalog = g.build_suites()
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: SAFE)
    selected = g.select_pr_tier(catalog, tier_for(catalog))
    ids = {s.id for s in selected}
    assert not ids & family_ids(g, catalog)
    assert "web-build" not in ids
    assert set(g.PR_TIER_PREREQUISITES) - {"web-build"} <= ids
    assert not g.selected_web_family_required(selected)
    # Partitioning cannot turn an unproven selection into a skip, and preserves
    # the proof across every local subset of the proven selection.
    for shard in g.partition_suites(selected, 3):
        assert not g.selected_web_family_required(shard)


@pytest.mark.parametrize("paths", [
    ["server/runtime/worker.py"], ["web/src/app.js"], ["new-root/a.py"],
    ["docs/DEMO-GOLDEN-PATH.md"], None, [], ["../ambiguous"], [None],
])
def test_web_family_runtime_unknown_malformed_retains(runner, monkeypatch, paths):
    g = runner
    catalog = g.build_suites()
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: paths)
    selected = g.select_pr_tier(catalog, tier_for(catalog))
    assert family_ids(g, catalog) <= {s.id for s in selected}
    assert g.selected_web_family_required(selected)


def test_web_family_priority_new_and_transitive_prerequisites(runner, monkeypatch):
    g = runner
    catalog = g.build_suites()
    extra = g.Suite("new-mocked-browser", "new", "script", g.WEB, ["node", "new.mjs"], None)
    tier = tier_for(catalog)
    # Dependencies through a non-web intermediate must still keep web-build.
    tier["prerequisites"] = {
        "ci-full-run-manifest-certificates": ["harness-tsc-build"],
        "harness-tsc-build": ["web-build"],
        "web-author-quota-gate": ["web-lifecycle-panel-proof"],
    }
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: SAFE)
    monkeypatch.setenv("LEAF_PRIORITY_SUITES", '["web-author-quota-gate"]')
    selected = g.select_pr_tier([*catalog, extra], tier)
    assert {extra.id, "web-author-quota-gate", "web-lifecycle-panel-proof", "web-build"} <= {
        s.id for s in selected}
    assert g.selected_web_family_required(selected)
    # A shard with only non-web rows cannot skip when any family was retained.
    assert g.selected_web_family_required([s for s in selected if s.cwd == g.SERVER])


def test_web_family_retained_historical_vitest_catcher(runner, monkeypatch):
    g = runner
    catalog = g.build_suites()
    tier = tier_for(catalog)
    target = next(p for p in (g.WEB / "src").rglob("*.test.js")
                  if not p.with_suffix(".jsx").exists())
    identity = target.relative_to(g.REPO).with_suffix("").as_posix()
    tier["reasons"] = {"web-vitest-1": ["fail30:" + identity]}
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: ["server/runtime/worker.py"])
    selected = g.select_pr_tier(catalog, tier)
    original = next(s for s in catalog if s.id == "web-vitest-1")
    catcher = next(s for s in selected if s.id == original.id)
    assert catcher.argv == [*original.argv, target.relative_to(g.WEB).as_posix()]
    assert catcher.expected == original.expected
    assert g.selected_web_family_required(selected)


@pytest.mark.parametrize("failure", [FileNotFoundError, SyntaxError, AttributeError])
def test_web_family_missing_unusable_helper_retains(runner, monkeypatch, capsys, failure):
    g = runner
    catalog = g.build_suites()
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: SAFE)
    def unavailable():
        raise failure("helper unavailable")
    monkeypatch.setattr(g, "load_web_family_decision", unavailable)
    selected = g.select_pr_tier(catalog, tier_for(catalog))
    assert {s.id for s in selected} == {s.id for s in catalog}
    assert g.selected_web_family_required(selected)
    assert "retained web family: helper unavailable" in capsys.readouterr().out


def test_web_family_ci_helper_uses_exact_trusted_base(runner, monkeypatch, tmp_path):
    g = runner
    source = (ROOT / "scripts/ci/web_family.py").read_text()
    monkeypatch.setenv("TRUSTED_SHA", "a" * 40)
    monkeypatch.setattr(g, "SCRIPTS_DIR", tmp_path / "candidate-helper-must-not-load")
    calls = []
    def show(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=source)
    monkeypatch.setattr(g.subprocess, "run", show)
    decision = g.load_web_family_decision()
    assert callable(decision)
    assert calls[0][0] == ["git", "--no-replace-objects", "show",
                          "a" * 40 + ":scripts/ci/web_family.py"]
    assert calls[0][1]["cwd"] == g.REPO
    assert calls[0][1]["check"] is True
    monkeypatch.setenv("TRUSTED_SHA", "HEAD")
    with pytest.raises(ValueError, match="trusted source"):
        g.load_web_family_decision()
    assert len(calls) == 1


@pytest.mark.parametrize("result", [(None, "bad"), (set(), "unproven"),
                                  (set(), "removed proven-unaffected web family IDs: all")])
def test_web_family_invalid_helper_result_retains(runner, monkeypatch, result):
    g = runner
    catalog = g.build_suites()
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: SAFE)
    monkeypatch.setattr(g, "load_web_family_decision", lambda: lambda *args, **kwargs: result)
    selected = g.select_pr_tier(catalog, tier_for(catalog))
    assert {s.id for s in selected} == {s.id for s in catalog}
    assert g.selected_web_family_required(selected)


def test_web_family_copied_trusted_list_needs_no_helper(tmp_path):
    copied = tmp_path / "run-all-gates.py"
    copied.write_bytes((ROOT / "scripts/run-all-gates.py").read_bytes())
    proc = subprocess.run([sys.executable, "-I", "-B", str(copied), "--list",
                           "--catalog-root", str(ROOT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["suites"]


def test_web_family_browser_admission_matches_resource_ids_and_locks(runner):
    g = runner
    import re
    source = (ROOT / "web/scripts/gate-browser-resources.mjs").read_text()
    admitted = set(re.findall(r"'([^']+)'", source.split('Object.freeze([', 1)[1].split('])', 1)[0]))
    assert admitted == set(g._BROWSER_NPM_PROOFS) | set(g._BROWSER_DIRECT_PROOFS)
    catalog = g.build_suites()
    for suite in catalog:
        assert g.isolated_browser_suite(suite) == (suite.id in admitted)
        if suite.id in admitted:
            assert g.browser_suite_env(suite) == {"LEAF_GATE_BROWSER_SUITE": suite.id}
            memory = {g.browser_memory_resource(suite)}
            assert g.conflict_resources(suite) == memory
            assert g.conflict_resources(replace(suite, uses_database=True)) == memory | {"postgres"}
            assert g.conflict_resources(replace(suite, db_gated=True, reset_authored=True)) == memory | {
                "postgres", "authored-store"}
    for sid in ("web-build", "web-demo-gate", "studio-walk-regressions", "web-page-service-smoke"):
        assert "web-tree" in g.conflict_resources(next(s for s in catalog if s.id == sid))
    assert g.conflict_resources(next(s for s in catalog if s.id == "web-vitest-1")) == {"web-vitest"}
    assert {"web-build", "web-demo-gate", "studio-walk-regressions"} <= g._PRELUDE_WRITERS


@pytest.mark.parametrize("memory,lanes", [(None, 1), (4 * 2**30, 1), (6 * 2**30, 2), (32 * 2**30, 2)])
def test_web_family_browser_memory_lanes_are_bounded(runner, monkeypatch, memory, lanes):
    g = runner
    monkeypatch.setattr(g, "_effective_mem_bytes", lambda: memory)
    admitted = [s for s in g.build_suites() if g.isolated_browser_suite(s)]
    resources = {g.browser_memory_resource(s) for s in admitted}
    assert resources == {f"browser-memory-{i}" for i in range(lanes)}


def test_web_family_all_web_writers_exclude_both_browser_reader_lanes(runner, monkeypatch):
    g = runner
    monkeypatch.setattr(g, "_effective_mem_bytes", lambda: 8 * 2**30)
    suites = g.build_suites()
    readers = [s for s in suites if g.isolated_browser_suite(s)]
    writers = [s for s in suites if "web-tree" in g.conflict_resources(s)]
    assert writers and readers
    for writer in writers:
        assert all(g.lane_resources(writer) & g.lane_resources(reader) for reader in readers)
    assert not g.lane_resources(readers[0]) & next(
        g.lane_resources(s) for s in readers
        if g.browser_memory_resource(s) != g.browser_memory_resource(readers[0]))


def test_web_family_browser_interval_records_real_spawn(runner, capsys):
    g = runner
    suite = next(s for s in g.build_suites() if s.id == "web-author-quota-gate")
    result = SimpleNamespace(returncode=0)
    assert g.run_browser_command(suite, lambda *a, **k: result,
                                env={"LEAF_GATE_BROWSER_SUITE": suite.id}) is result
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"GATE_BROWSER_BEGIN id={suite.id} monotonic_ns=")
    assert lines[1].startswith(f"GATE_BROWSER_END id={suite.id} monotonic_ns=")
    assert lines[1].endswith("exit=0")
    start = int(lines[0].split("monotonic_ns=")[1])
    end = int(lines[1].split("monotonic_ns=")[1].split()[0])
    assert end >= start


def test_web_family_browser_spoof_drift_stays_serialized(runner, monkeypatch):
    g = runner
    suite = next(s for s in g.build_suites() if s.id == "web-studio-panels-proof")
    variants = [replace(suite, id="unknown-browser"), replace(suite, cwd=g.HARNESS),
                replace(suite, argv=[*suite.argv, "--grep=other"]),
                replace(suite, argv=["node", "writer.mjs"]),
                replace(suite, argv=[a.replace("playwright.config.mjs", "playwright.local.config.mjs")
                                     for a in suite.argv])]
    for drift in variants:
        assert not g.isolated_browser_suite(drift)
        assert not g.browser_suite_env(drift)
        assert g.conflict_resources(drift)
    npm_suite = next(s for s in g.build_suites() if s.id == "web-author-quota-gate")
    original = Path.read_text
    def changed_package(path, *args, **kwargs):
        if path == g.WEB / "package.json":
            return '{"scripts":{"proof:author-quota-gate":"npm run build"}}'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", changed_package)
    assert not g.browser_suite_env(npm_suite)
    assert "web-tree" in g.conflict_resources(npm_suite)


@pytest.mark.parametrize("reporting_disabled", [False, True])
def test_web_family_child_environment_original_and_reporter_retry(
        runner, monkeypatch, tmp_path, reporting_disabled):
    g = runner
    monkeypatch.setenv("LEAF_GATE_BROWSER_SUITE", "ambient-spoof")
    monkeypatch.setenv("LEAF_CI_WEB_FAMILY_REQUIRED", "0")
    assert "LEAF_GATE_BROWSER_SUITE" not in g.clean_env()
    assert "LEAF_CI_WEB_FAMILY_REQUIRED" not in g.clean_env()
    monkeypatch.setattr(g, "suite_trace_env", lambda *args: {})
    monkeypatch.setattr(g, "reporting_command", lambda suite, argv, env: argv)
    calls = []
    def spawn(argv, **kwargs):
        calls.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(g.subprocess, "run", spawn)
    suites = [s for s in g.build_suites() if s.id in
              set(g._BROWSER_NPM_PROOFS) | set(g._BROWSER_DIRECT_PROOFS)]
    suites.append(replace(suites[0], id="unadmitted-browser"))
    for suite in suites:
        g.run_suite(suite, tmp_path, reporting_disabled=reporting_disabled)
        env = calls[-1]
        assert env.get("LEAF_GATE_BROWSER_SUITE") == (
            suite.id if g.isolated_browser_suite(suite) else None)
        assert "LEAF_CI_WEB_FAMILY_REQUIRED" not in env


@pytest.mark.parametrize("mode", ["safe", "retained", "full", "only", "default"])
def test_web_family_before_heavy_explicit_environment(runner, monkeypatch, tmp_path, mode):
    g = runner
    monkeypatch.setenv("LEAF_CI_WEB_FAMILY_REQUIRED", "0")
    catalog = g.build_suites()
    monkeypatch.setattr(g, "pr_tier_changed_paths", lambda: SAFE if mode == "safe" else ["web/src/a.js"])
    if mode in {"safe", "retained"}:
        selected = g.select_pr_tier(catalog, tier_for(catalog))
    elif mode == "only":
        selected = [next(s for s in catalog if s.cwd == g.SERVER)]
    else:
        selected = catalog
    calls = []
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(g.subprocess, "run", spawn)
    monkeypatch.setattr(g, "run_lanes", lambda *args: None)
    kwargs = {} if mode == "default" else {
        "web_family_required": g.selected_web_family_required(selected)}
    # No real suites: exercise the callback without fabricating suite verdicts.
    g.run_suites_parallel([], tmp_path, 1, 0, False,
                          before_heavy_command=["fake-heavy"], **kwargs)
    assert calls[0][0] == ["fake-heavy"]
    assert calls[0][1]["env"]["LEAF_CI_WEB_FAMILY_REQUIRED"] == ("0" if mode == "safe" else "1")


@pytest.mark.parametrize("flag,postgres,expected,web_commands", [
    ("0", "0", 0, False), ("0", "7", 1, False),
    ("1", "0", 0, True), ("00", "0", 0, True),
    ("true", "0", 0, True), (None, "0", 0, True),
])
@pytest.mark.skipif(os.name == "nt", reason="CI heavy setup is a POSIX shell contract; verify on Linux")
def test_web_family_executable_heavy_branch(tmp_path, flag, postgres, expected, web_commands):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable for executable CI shell contract")
    sentinel = tmp_path / "commands"
    # Shell functions intercept every browser/build/license command. The real
    # trusted heavy branch still executes its PostgreSQL predicate and waits.
    script = '''
npx() { echo "npx $*" >> "$WEB_FAMILY_SENTINEL"; }
python() { echo "python $*" >> "$WEB_FAMILY_SENTINEL"; }
npm() { echo "npm $*" >> "$WEB_FAMILY_SENTINEL"; }
timeout() { shift; shift; "$@"; }
source "$1" --heavy-prerequisites
'''
    env = {**os.environ, "CODEBUILD_SRC_DIR": ROOT.as_posix(),
           "WEB_FAMILY_SENTINEL": sentinel.as_posix(), "LEAF_CI_POSTGRES_SETUP_STATUS": postgres}
    for name in ("CI_SHARD_TOTAL", "CI_SHARD_INDEX", "LEAF_DEP_CACHE_HELPER", "LEAF_CI_CACHE_DIR",
                 "LEAF_CI_WEB_FAMILY_REQUIRED"):
        env.pop(name, None)
    if flag is not None:
        env["LEAF_CI_WEB_FAMILY_REQUIRED"] = flag
    proc = subprocess.run([bash, "--noprofile", "--norc", "-c", script,
                           "web-family-test", (ROOT / ".codebuild/ci.sh").as_posix()],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == expected, proc.stdout + proc.stderr
    commands = sentinel.read_text() if sentinel.exists() else ""
    if web_commands:
        assert "npx playwright install chromium" in commands
        assert "python -m playwright install chromium" in commands
        assert "npm run build" in commands
        assert "python scripts/check_license_fence.py ." in commands
    else:
        assert not commands
    if postgres != "0":
        assert "ephemeral PostgreSQL did not start" in proc.stderr
