"""Explicit PR tier activation, conservative selection, and proof isolation."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    previous = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


@pytest.fixture
def generator():
    return load_module("pr_tier_generator_test", "scripts/ci/build_pr_tier.py")


@pytest.fixture
def runner():
    return load_module("pr_tier_runner_test", "scripts/run-all-gates.py")


def row(sid, target=None, cwd="server", kind="pytest", **extra):
    return dict(id=sid, cwd=cwd, kind=kind,
                command=["<PYTHON>", "-m", "pytest", target or ("tests/test_" + sid + ".py")], **extra)


def inputs():
    proposal = dict(schema="leaf.ci.pr-tier.v1", catchers=["catcher", "removed"],
                    hard_harm_sentinels=[], prerequisites=[], suite_ids=["catcher", "removed"])
    history = {"LEAF-Solar-Design/leaf-web-demo": dict(runs=331, logs=308, fail_files={
        "src/widget.test.js": 2, "test/operatorLoop.test.ts": 1})}
    catalog = dict(schema="leaf.ci.catalog.v1", catalog_sha256="a" * 64, suites=[
        row("catcher"), row("healthy"), row("web-owner", "src/widget.test.jsx", cwd="web"),
        row("harness-owner", "test/operatorLoop.test.ts", cwd="harness", kind="vitest"),
        row("web-version-restore-proof"), row("web-build"), row("harness-tsc-build"),
        row("ci-full-run-manifest-certificates"), row("sentinel"),
        row("unknown", "-c", kind="script"),
    ])
    return proposal, history, ["sentinel"], catalog


def build(generator, documents):
    return generator.build_document(*documents, {name: "b" * 64 for name in ("proposal", "fail30", "sentinels")})


def test_generator_is_deterministic_and_keeps_failure_owners(generator):
    documents = inputs()
    first = build(generator, documents)
    proposal, history, sentinels, catalog = documents
    catalog["suites"].reverse()
    proposal["suite_ids"].reverse()
    history[generator.REPO_SLUG]["fail_files"] = dict(reversed(list(
        history[generator.REPO_SLUG]["fail_files"].items())))
    second = build(generator, documents)
    assert first == second
    assert first["enabled"] is False
    assert first["suite_ids"] == sorted(first["suite_ids"])
    assert set(first["suite_ids"]) == {
        "catcher", "sentinel", "web-owner", "harness-owner", "unknown",
        "web-version-restore-proof", "web-build", "harness-tsc-build", "ci-full-run-manifest-certificates"}
    assert first["reasons"]["web-owner"] == ["fail30:web/src/widget.test"]
    assert first["reasons"]["unknown"] == ["unknown-ownership"]
    assert first["generated_from"]["catalog_sha256"] == "a" * 64
    assert set(first["generated_from"]["input_sha256s"]) == {"proposal", "fail30", "sentinels"}


@pytest.mark.parametrize("path", ["widget.test.js", "widget.test.jsx", "web/src/widget.test",
                                  "src/widget.test", "./web/src/widget.test.jsx",
                                  r"web\src\widget.test.js"])
def test_web_path_normalization(runner, path):
    assert runner.pr_tier_path(path) == "web/src/widget.test"
    assert runner.pr_tier_owns(row("web-owner", "src/widget.test.jsx", cwd="web"), path)
    assert not runner.pr_tier_owns(row("server-owner", "tests/widget.test.js"), path)


def test_generator_retains_unknown_new_and_transitive_prerequisites(generator):
    documents = inputs()
    proposal, _, _, catalog = documents
    proposal["catalog_suite_ids"] = [r["id"] for r in catalog["suites"]] + ["dep-one", "dep-two"]
    catalog["suites"].extend([row("new-suite"), row("dep-one", prerequisites=["dep-two"]), row("dep-two")])
    catalog["suites"][0]["prerequisites"] = ["dep-one"]
    result = build(generator, documents)
    assert {"new-suite", "dep-one", "dep-two", "unknown"} <= set(result["suite_ids"])
    assert result["reasons"]["new-suite"] == ["new-catalog-suite"]
    assert result["reasons"]["dep-two"] == ["prerequisite"]
    assert result["prerequisites"]["dep-one"] == ["dep-two"]


@pytest.mark.parametrize("bad", ["proposal", "fail30", "sentinels", "catalog", "dependency"])
def test_generator_refuses_malformed_inputs(generator, bad):
    documents = list(inputs())
    if bad == "proposal":
        documents[0]["suite_ids"] = ["catcher", 123]
    elif bad == "fail30":
        documents[1][generator.REPO_SLUG]["fail_files"]["src/widget.test.js"] = "two"
    elif bad == "sentinels":
        documents[2] = {"wrong": "shape"}
    elif bad == "catalog":
        documents[3]["suites"].append(documents[3]["suites"][0])
    else:
        documents[3]["suites"][0]["prerequisites"] = ["missing"]
    with pytest.raises(ValueError):
        build(generator, documents)


@pytest.mark.parametrize("malformed", ["{", '{"suite_ids":[],"suite_ids":[]}', "[]"])
def test_generator_refusal_leaves_existing_output_untouched(generator, tmp_path, monkeypatch, malformed):
    proposal, history, sentinels, catalog = inputs()
    paths = [tmp_path / name for name in ("proposal.json", "fail30.json", "sentinels.json")]
    for path, document in zip(paths, (proposal, history, sentinels)):
        path.write_text(json.dumps(document), encoding="utf-8")
    paths[0].write_text(malformed, encoding="utf-8")
    output = tmp_path / "tier.json"
    output.write_text("previous policy", encoding="utf-8")
    monkeypatch.setattr(generator.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(catalog)))
    assert generator.main(["--proposal", str(paths[0]), "--fail30", str(paths[1]),
                           "--sentinels", str(paths[2]), "--output", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == "previous policy"


@pytest.mark.parametrize("enabled", [False, True])
def test_generator_cli_requires_explicit_activation(generator, tmp_path, monkeypatch, capsys, enabled):
    proposal, history, sentinels, catalog = inputs()
    paths = [tmp_path / name for name in ("proposal.json", "fail30.json", "sentinels.json")]
    for path, document in zip(paths, (proposal, history, sentinels)):
        path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(generator.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, stdout=json.dumps(catalog)))
    argv = ["--proposal", str(paths[0]), "--fail30", str(paths[1]),
            "--sentinels", str(paths[2]), "--stdout"]
    if enabled:
        argv.append("--enabled")
    assert generator.main(argv) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["enabled"] is enabled
    assert document == generator.generate(*paths, catalog=catalog, enabled=enabled)
    document["enabled"] = False
    assert document == generator.generate(*paths, catalog=catalog)


def stub_suites(runner):
    return [runner.Suite(sid, sid, "pytest", ROOT / "server",
                         [sys.executable, "-m", "pytest", "tests/test_" + sid + ".py"], 1)
            for sid in ("tier-member", "changed", "healthy", "new-suite", "web-build",
                        "harness-tsc-build", "ci-full-run-manifest-certificates", "dependency", "priority")]


def policy_for(suites, enabled=True):
    return dict(schema="leaf.ci.pr-tier.v1", enabled=enabled, suite_ids=["tier-member"],
                catalog_suite_ids=[s.id for s in suites if s.id != "new-suite"],
                prerequisites={"changed": ["dependency"]})


@pytest.mark.parametrize("event", ["PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED",
                                   "PUSH", "PULL_REQUEST_MERGED", ""])
@pytest.mark.parametrize("policy", ["enabled", "disabled", "missing", "malformed", "schema", "flag", "ids"])
def test_active_check_shares_runner_validation(generator, runner, tmp_path, monkeypatch, capsys, event, policy):
    monkeypatch.delenv("LEAF_CI_FULL", raising=False)
    path = tmp_path / "policy.json"
    document = policy_for(stub_suites(runner), enabled=policy != "disabled")
    if policy == "schema":
        document["schema"] = "other"
    elif policy == "flag":
        document["enabled"] = "true"
    elif policy == "ids":
        document["suite_ids"] = ["absent-from-recorded-catalog"]
    if policy != "missing":
        path.write_text("{" if policy == "malformed" else json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("CODEBUILD_WEBHOOK_EVENT", event)
    monkeypatch.setattr(generator, "generate", lambda *a, **kw: pytest.fail("active check generated a policy"))
    usable = runner.load_pr_tier(path, quiet=True) is not None
    assert usable == (policy == "enabled")
    assert generator.main(["--active", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ("1\n" if usable and event in {
        "PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED"} else "0\n")
    assert captured.err == ""


@pytest.mark.parametrize("event", ["PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED"])
@pytest.mark.parametrize("full", [None, "1", "0", "true", ""])
def test_active_check_honors_only_explicit_full(generator, runner, tmp_path, monkeypatch, capsys, event, full):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_for(stub_suites(runner))), encoding="utf-8")
    monkeypatch.delenv("LEAF_CI_FULL", raising=False)
    if full is not None:
        monkeypatch.setenv("LEAF_CI_FULL", full)
    monkeypatch.setenv("CODEBUILD_WEBHOOK_EVENT", event)
    if full == "1":
        monkeypatch.setattr(generator, "load_runner", lambda: pytest.fail("full run checked tier policy"))
    assert generator.main(["--active", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ("0\n" if full == "1" else "1\n")
    assert captured.err == ""


def invoke(runner, monkeypatch, tmp_path, policy=None, flags=(), jobs=1, suites=None, observed=None):
    suites = stub_suites(runner) if suites is None else suites
    monkeypatch.setattr(runner, "build_suites", lambda: suites)
    monkeypatch.setattr(runner, "migrate_gate_database", lambda suites: None)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: 1.0)
    ran = []

    def fake_run(suite, log_dir, attempt):
        ran.append(suite.id)
        if observed is not None:
            observed.append(suite)
        return runner.Result(suite, "PASS", "1", 0.1, counts={"got": 1, "passed": 1})

    monkeypatch.setattr(runner, "run_suite_guarded", fake_run)
    result_path = tmp_path / "result.json"
    argv = ["run-all-gates.py", "--jobs", str(jobs), "--retry", "0", "--log-dir", str(tmp_path),
            "--result-json", str(result_path), *flags]
    if policy is not None:
        tier_path = ROOT / "scripts/ci/pr-tier.json" if policy == "committed" else tmp_path / "tier-policy.json"
        if policy not in ("missing", "committed"):
            tier_path.write_text(json.dumps(policy) if isinstance(policy, dict) else policy, encoding="utf-8")
        argv.extend(["--tier-file", str(tier_path)])
    monkeypatch.setattr(sys, "argv", argv)
    rc = runner.main()
    return rc, ran, json.loads(result_path.read_text()) if result_path.exists() else None


@pytest.mark.parametrize("policy", ["disabled", "missing", "{", "[]", "bad-flag", "bad-ids"])
@pytest.mark.parametrize("flags", [(), ("--only", "healthy"), ("--shard-count", "2", "--shard-index", "0")])
def test_unavailable_or_disabled_tier_matches_existing_run(runner, monkeypatch, tmp_path, capsys, policy, flags):
    monkeypatch.setenv("LEAF_CHANGED_PATHS", "not-json-must-not-be-read")
    before_rc, before_ids, before_data = invoke(runner, monkeypatch, tmp_path, flags=flags)
    before_output = capsys.readouterr().out
    document = policy
    if policy in ("disabled", "bad-flag", "bad-ids"):
        document = policy_for(stub_suites(runner), enabled=False if policy == "disabled" else True)
        if policy == "bad-flag":
            document["enabled"] = "true"
        if policy == "bad-ids":
            document["suite_ids"] = "tier-member"
    after_rc, after_ids, after_data = invoke(runner, monkeypatch, tmp_path, document, flags=flags)
    after_output = capsys.readouterr().out
    info = [line for line in after_output.splitlines(keepends=True) if line.startswith("INFO:")]
    assert len(info) == 1
    assert after_output.replace(info[0], "", 1) == before_output
    assert after_rc == before_rc == 0
    assert after_ids == before_ids
    assert after_data == before_data


@pytest.mark.parametrize("jobs", [1, 2])
def test_enabled_tier_adds_changed_new_priority_and_prerequisites(
        runner, monkeypatch, tmp_path, capsys, jobs):
    monkeypatch.setenv("LEAF_CHANGED_PATHS", '["server/tests/test_changed.py"]')
    monkeypatch.setenv("LEAF_PRIORITY_SUITES", '["priority"]')
    document = policy_for(stub_suites(runner))
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, document, jobs=jobs)
    assert rc == 0
    assert set(ran) == {"tier-member", "changed", "new-suite", "priority", "dependency",
                        "web-build", "harness-tsc-build", "ci-full-run-manifest-certificates"}
    assert data["mode"] == data["execution_mode"] == data["selection_mode"] == "tier"
    assert data["selection"].startswith("mode=tier ")
    assert all(entry["mode"] == "tier" for entry in data["results"])
    attempts = [json.loads(line) for path in (tmp_path / "attempts").glob("*.jsonl")
                for line in path.read_text().splitlines()]
    assert attempts and all(row["mode"] == "tier" for row in attempts)
    output = capsys.readouterr().out
    assert "mode=tier" in output
    assert "omitting catalog suites outside tier and changed tests: healthy" in output
    assert "PROVEN:" not in output


def test_unknown_changed_paths_conservatively_runs_everything_in_tier_mode(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_CHANGED_PATHS", "not-json")
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, policy_for(stub_suites(runner)))
    assert rc == 0
    assert set(ran) == {s.id for s in stub_suites(runner)}
    assert data["mode"] == "tier"


@pytest.mark.parametrize("changed", [["server/site.py"], ["platform/auth.py"],
                                    ["docs/ci.md", "README.md"]])
def test_non_web_pr_bounds_all_real_web_shards_without_changing_catalog(
        runner, monkeypatch, changed):
    suites = runner.build_suites()
    fingerprint = runner.catalog_fingerprint(suites)
    document = json.loads((ROOT / "scripts/ci/pr-tier.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("LEAF_CHANGED_PATHS", json.dumps(changed))
    monkeypatch.delenv("LEAF_PRIORITY_SUITES", raising=False)
    observed = runner.select_pr_tier(suites, document)
    ran = [s.id for s in observed]
    assert set(document["suite_ids"]) <= set(ran)
    assert set(runner.PR_TIER_PREREQUISITES) <= set(ran)
    originals = {s.id: s for s in suites}
    bounded = []
    for suite in observed:
        original = originals[suite.id]
        if suite.id in {"web-vitest-1", "web-vitest-2", "web-vitest-3"}:
            identities = [reason.removeprefix("fail30:") for reason in document["reasons"][suite.id]
                          if reason.startswith("fail30:")]
            paths = suite.argv[len(original.argv):]
            assert len(paths) == len(identities) == 22
            assert {runner.pr_tier_path("web/" + path) for path in paths} == set(identities)
            assert all((ROOT / "web" / path).is_file() for path in paths)
            assert suite.argv[:len(original.argv)] == original.argv
            assert suite == runner.replace(original, argv=suite.argv)
            assert suite is not original
            bounded.append(suite.id)
        else:
            assert suite is original
    assert len(bounded) == 3
    assert fingerprint == runner.catalog_fingerprint(suites)


@pytest.fixture
def web_catcher_case(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "REPO", tmp_path)
    monkeypatch.setattr(runner, "WEB", tmp_path / "web")
    source = tmp_path / "web/src"
    source.mkdir(parents=True)
    (source / "catcher.test.js").write_text("", encoding="utf-8")
    suites = [runner.Suite(f"web-vitest-{shard}", "web tests", "vitest", runner.WEB,
                           ["npm", "run", "test:unit", "--", f"--shard={shard}/3", "--maxWorkers=2"], 1)
              for shard in range(1, 4)]
    suites += [runner.Suite("harness-vitest", "harness tests", "vitest", tmp_path / "harness",
                            ["npm", "test"], 1)]
    document = dict(schema=runner.PR_TIER_SCHEMA, enabled=True,
                    suite_ids=[s.id for s in suites], catalog_suite_ids=[s.id for s in suites],
                    reasons={s.id: ["fail30:web/src/catcher.test", "proposal"] for s in suites})
    monkeypatch.setenv("LEAF_CHANGED_PATHS", '["server/site.py"]')
    monkeypatch.delenv("LEAF_PRIORITY_SUITES", raising=False)
    return suites, document, source


@pytest.mark.parametrize("changed", [
    ["web/src/app.js"], ["web/README.md"], ["scripts/ci.md"], [".github/workflows/ci.yml"],
    [".codebuild/ci.sh"], ["package.json"], ["package-lock.json"], ["pyproject.toml"],
    ["vitest.config.js"], ["tsconfig.json", "server/site.py"], ["main.py"],
    ["tests/test_gate.py"], ["unknown/path.md"], ["server/../web/src/app.js"],
    ["/server/site.py"], ["./server/site.py"], ["server//site.py"], ["server/"],
    [r"server\site.py"], ["server/site.py\n"], ["server/.config"], [], [None], [123],
    "not-json", None,
])
def test_web_catchers_keep_full_commands_for_closure_or_unknown_paths(
        runner, monkeypatch, web_catcher_case, changed):
    suites, document, _ = web_catcher_case
    if changed is None:
        def unavailable():
            raise ValueError("PR changed paths unavailable")
        monkeypatch.setattr(runner, "pr_tier_changed_paths", unavailable)
    else:
        monkeypatch.setenv("LEAF_CHANGED_PATHS", changed if isinstance(changed, str) else json.dumps(changed))
    selected = runner.select_pr_tier(suites, document)
    assert selected == suites
    assert all(actual is original for actual, original in zip(selected, suites))


@pytest.mark.parametrize("bad", ["missing", "mapping", "no-suite", "empty", "string", "number",
                                 "no-catchers", "empty-catcher", "escape", "absolute", "non-test",
                                 "extension", "absent", "ambiguous", "mixed", "symlink"])
def test_invalid_catcher_metadata_keeps_original_web_command(runner, web_catcher_case, bad):
    suites, document, source = web_catcher_case
    if bad == "missing":
        document.pop("reasons")
    elif bad == "mapping":
        document["reasons"] = []
    elif bad == "no-suite":
        document["reasons"] = {}
    else:
        reasons = {
            "empty": [], "string": "fail30:web/src/catcher.test", "number": [123],
            "no-catchers": ["proposal"], "empty-catcher": ["fail30:"],
            "escape": ["fail30:web/src/../catcher.test"],
            "absolute": ["fail30:/web/src/catcher.test"],
            "non-test": ["fail30:web/src/catcher"],
            "extension": ["fail30:web/src/catcher.test.js"],
            "absent": ["fail30:web/src/missing.test"],
            "mixed": ["fail30:web/src/catcher.test", "fail30:web/src/missing.test"],
        }.get(bad, ["fail30:web/src/catcher.test"])
        for suite in suites:
            document["reasons"][suite.id] = reasons
    if bad == "ambiguous":
        (source / "catcher.test.jsx").write_text("", encoding="utf-8")
    elif bad == "symlink":
        outside = source.parents[1] / "outside.test.js"
        outside.write_text("", encoding="utf-8")
        (source / "catcher.test.js").unlink()
        try:
            (source / "catcher.test.js").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
    selected = runner.select_pr_tier(suites, document)
    assert selected == suites
    assert all(actual is original for actual, original in zip(selected, suites))


def test_web_catcher_selection_keeps_new_priority_and_prerequisite_suites(
        runner, monkeypatch, web_catcher_case):
    suites, document, _ = web_catcher_case
    extras = [runner.Suite(sid, sid, "pytest", runner.REPO / "server",
                           [sys.executable, "-m", "pytest", f"tests/test_{sid}.py"], 1)
              for sid in ("new-suite", "priority", "dependency")]
    document["catalog_suite_ids"].extend(["priority", "dependency"])
    document["prerequisites"] = {"web-vitest-1": ["dependency"]}
    monkeypatch.setenv("LEAF_PRIORITY_SUITES", '["priority"]')
    selected = runner.select_pr_tier(suites + extras, document)
    assert [s.id for s in selected] == [s.id for s in suites + extras]
    assert all(s.argv[-1] == "src/catcher.test.js" for s in selected[:3])
    assert all(s is original for s, original in zip(selected[3:], (suites + extras)[3:]))


def test_full_mode_never_resolves_or_bounds_catchers(runner, monkeypatch, tmp_path, web_catcher_case):
    suites, _, _ = web_catcher_case
    monkeypatch.setattr(runner, "pr_tier_web_catchers", lambda *a: pytest.fail("full mode bounded catchers"))
    observed = []
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, suites=suites, observed=observed)
    assert rc == 0 and ran == [s.id for s in suites]
    assert all(s is original for s, original in zip(observed, suites))
    assert "mode" not in data


def test_bounded_catchers_keep_actual_counts_and_original_catalog_fingerprint(
        runner, monkeypatch, tmp_path, web_catcher_case):
    suites, document, _ = web_catcher_case
    fingerprint = runner.catalog_fingerprint(suites)
    observed = []
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, document, suites=suites, observed=observed)
    assert rc == 0 and ran == [s.id for s in suites]
    assert all(s.argv[-1] == "src/catcher.test.js" for s in observed[:3])
    assert data["catalog_fingerprint"] == fingerprint == runner.catalog_fingerprint(suites)
    assert data["executed_total"] == len(ran)
    assert all(entry["got"] == "1" and entry["executed"] == 1 for entry in data["results"])


def test_pr_diff_uses_base_and_both_rename_paths(runner, monkeypatch):
    monkeypatch.delenv("LEAF_CHANGED_PATHS", raising=False)
    monkeypatch.setenv("TRUSTED_SHA", "c" * 40)
    calls = []

    def diff(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="server/tests/old.py\0server/tests/new.py\0")

    monkeypatch.setattr(runner.subprocess, "run", diff)
    assert runner.pr_tier_changed_paths() == ["server/tests/old.py", "server/tests/new.py"]
    assert "--no-renames" in calls[0][0]
    assert "c" * 40 + "...HEAD" in calls[0][0]


def test_tier_that_runs_entire_catalog_still_cannot_mint_full_proof(runner, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEAF_CHANGED_PATHS", "[]")
    suites = stub_suites(runner)
    document = policy_for(suites)
    document["suite_ids"] = document["catalog_suite_ids"]
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, document)
    assert rc == 0 and len(ran) == len(suites)
    proof = tmp_path / "proof.json"
    monkeypatch.setattr(runner, "emit_gate_proof", lambda *a, **kw: pytest.fail("tier minted proof"))
    monkeypatch.setattr(sys, "argv", ["run-all-gates.py", "--verify-shard-results", str(tmp_path),
                                    "--emit-proof", str(proof)])
    assert runner.main() == 1
    assert "mode=tier cannot prove a full gate" in capsys.readouterr().out
    assert not proof.exists()


def test_enabled_tier_cannot_be_narrowed_with_only(runner, monkeypatch, tmp_path, capsys):
    rc, ran, _ = invoke(runner, monkeypatch, tmp_path, policy_for(stub_suites(runner)),
                        flags=["--only", "healthy"])
    assert rc == 2 and not ran
    assert "mode=tier cannot combine" in capsys.readouterr().out


def test_ci_final_receipt_uses_actual_tier_result(tmp_path):
    source = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8")
    block = source.split("# LEAF_TIER_RESULT_BEGIN\n", 1)[1].split("# LEAF_TIER_RESULT_END", 1)[0]
    finalize = source.split("<<'LEAF_SELECTION_FINALIZE'\n", 1)[1].split(
        "\nLEAF_SELECTION_FINALIZE", 1)[0]
    manifest = finalize[finalize.index('if sys.argv[3] == "1" and '):]
    assert 'detail.get("selection_source") != "pr-tier"' in manifest
    result_path = tmp_path / "gate-result.json"

    def redirected_path(path):
        assert path == "/tmp/gate-results/gate-result.json"
        return result_path

    for mode in (None, "tier"):
        payload = dict(suite_ids=["tier-member", "changed"])
        if mode:
            payload["mode"] = mode
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        detail = dict(execution_mode="full", selection_mode="shadow", executed_suite_ids=["all"])
        namespace = dict(json=json, Path=redirected_path, detail=detail)
        exec(compile(block, "ci-tier-result", "exec"), namespace)
        namespace.update(sys=SimpleNamespace(argv=["finalize", str(tmp_path), "0", "1"]),
                         out=tmp_path, subprocess=SimpleNamespace(
                             run=lambda *a, **kw: pytest.fail("tier invoked full-run manifest")))
        if mode == "tier":
            assert detail["execution_mode"] == detail["selection_mode"] == "selected"
            assert detail["selection_source"] == "pr-tier"
            assert detail["executed_suite_ids"] == payload["suite_ids"]
            assert detail["expanded_suite_ids"] == payload["suite_ids"]
            # Even traced tiers must skip the manifest producer entirely.
            exec(compile(manifest, "ci-tier-manifest", "exec"), namespace)
            assert not (tmp_path / "full-run.json").exists()
        else:
            assert detail == dict(execution_mode="full", selection_mode="shadow", executed_suite_ids=["all"])
            # A traced ordinary run still enters manifest production and requires its helper.
            with pytest.raises(ValueError, match="manifest_helper_unavailable"):
                exec(compile(manifest, "ci-full-manifest", "exec"), namespace)


def test_ci_tier_activation_precedes_startup_and_preserves_selection_mode():
    source = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8")
    predicate = 'pr_tier_active="$(python3 -I -B scripts/ci/build_pr_tier.py --active scripts/ci/pr-tier.json)"'
    export = "export LEAF_SELECTION_EXECUTION_MODE=selected"
    startup = 'echo "LEAF_EVENT $(python3 -I -c '
    assert source.count(predicate) == 1
    assert source.index(predicate) < source.index(export) < source.index(startup)
    activation = source.split("# LEAF_PR_TIER_ACTIVATION_BEGIN\n", 1)[1].split(
        "# LEAF_PR_TIER_ACTIVATION_END", 1)[0]
    assert "selection_mode=" not in activation
    gate = source.split("gate_tier_args=()\n", 1)[1].split('\necho "LEAF_EVENT gate ', 1)[0]
    assert 'if [[ "$pr_tier_active" == 1 ]]; then' in gate
    assert "CODEBUILD_WEBHOOK_EVENT" not in gate


@pytest.mark.skipif(not shutil.which("bash"), reason="CI startup execution requires bash")
@pytest.mark.parametrize("event,policy,selection_mode,expected", [
    ("PULL_REQUEST_CREATED", "enabled", "full", "selected"),
    ("PULL_REQUEST_UPDATED", "enabled", "full", "selected"),
    ("PULL_REQUEST_REOPENED", "enabled", "full", "selected"),
    ("PUSH", "enabled", "full", "full"),
    ("", "enabled", "full", "full"),
    ("PULL_REQUEST_MERGED", "enabled", "full", "full"),
    ("PULL_REQUEST_UPDATED", "disabled", "full", "full"),
    ("PULL_REQUEST_UPDATED", "missing", "full", "full"),
    ("PULL_REQUEST_UPDATED", "malformed", "full", "full"),
    ("PULL_REQUEST_UPDATED", "invalid-ids", "full", "full"),
    ("PUSH", "enabled", "selected", "selected"),
    ("PULL_REQUEST_UPDATED", "disabled", "shadow", "shadow"),
])
def test_ci_startup_reports_actual_execution_mode(runner, tmp_path, event, policy, selection_mode, expected):
    source = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8")
    activation = source.split("# LEAF_PR_TIER_ACTIVATION_BEGIN\n", 1)[1].split(
        "# LEAF_PR_TIER_ACTIVATION_END", 1)[0]
    path = tmp_path / "policy.json"
    document = policy_for(stub_suites(runner), enabled=policy != "disabled")
    if policy == "invalid-ids":
        document["suite_ids"] = "tier-member"
    if policy != "missing":
        path.write_text("{" if policy == "malformed" else json.dumps(document), encoding="utf-8")
    activation = activation.replace("--active scripts/ci/pr-tier.json", "--active " + shlex.quote(path.as_posix()))
    start = source.index('echo "LEAF_EVENT $(python3 -I -c ')
    startup = source[start:source.index('echo "LEAF_T start setup ', start)]
    script = tmp_path / "startup.sh"
    script.write_text('set -euo pipefail\nselection_mode=' + selection_mode + "\n" + activation + startup
                      + '\nprintf "selection_mode=%s\\n" "$selection_mode"\n', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "LEAF_CI_FULL")}
    env["CODEBUILD_WEBHOOK_EVENT"] = event
    result = subprocess.run([shutil.which("bash"), script.as_posix()], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("LEAF_EVENT event=")
    assert f"mode={expected}" in lines[0].split()
    assert lines[1] == "selection_mode=" + selection_mode


@pytest.mark.skipif(not shutil.which("bash"), reason="CI gate argument execution requires bash")
@pytest.mark.parametrize("event", ["PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED",
                                   "PUSH", "pin", "PULL_REQUEST_MERGED"])
def test_ci_committed_policy_runs_pr_tier_but_keeps_main_and_pin_full(
        runner, monkeypatch, tmp_path, capsys, event):
    source = (ROOT / ".codebuild/ci.sh").read_text(encoding="utf-8")
    activation = source.split("# LEAF_PR_TIER_ACTIVATION_BEGIN\n", 1)[1].split(
        "# LEAF_PR_TIER_ACTIVATION_END", 1)[0]
    block = source.split("gate_tier_args=()\n", 1)[1].split('\necho "LEAF_EVENT gate ', 1)[0]
    env = {key: value for key, value in os.environ.items()
           if key not in ("BASH_ENV", "CODEBUILD_WEBHOOK_EVENT", "LEAF_PROOF_TRUSTED_SHA", "LEAF_CI_FULL")}
    if event == "pin":
        env["LEAF_PROOF_TRUSTED_SHA"] = "c" * 40
    else:
        env["CODEBUILD_WEBHOOK_EVENT"] = event
    command = ('set -euo pipefail\nselection_mode=full\n' + activation + '\ngate_tier_args=()\n'
               + block + '\nprintf "%s\\n" "${gate_tier_args[@]}"')
    result = subprocess.run([shutil.which("bash"), "-c", command], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    is_pr = event.startswith("PULL_REQUEST_") and event != "PULL_REQUEST_MERGED"
    assert result.stdout.split() == (["--tier-file", "scripts/ci/pr-tier.json"] if is_pr else [])

    catalog = runner.selection_catalog(ROOT)
    # Keep real catalog ownership while replacing suite execution with inert results.
    suites = [runner.Suite(r["id"], r["label"], r["kind"], ROOT / r["cwd"], r["command"], 1)
              for r in catalog["suites"]]
    document = json.loads((ROOT / "scripts/ci/pr-tier.json").read_text(encoding="utf-8"))
    assert document["enabled"] is True
    monkeypatch.setenv("LEAF_CHANGED_PATHS", '["server/tests/test_autofill_adapter.py"]')
    monkeypatch.setenv("LEAF_PRIORITY_SUITES", '["server-auth"]')
    monkeypatch.setattr(runner, "emit_gate_proof", lambda *a, **kw: pytest.fail("tier minted proof"))
    rc, ran, data = invoke(runner, monkeypatch, tmp_path, "committed" if is_pr else None, suites=suites)
    assert rc == 0
    if is_pr:
        assert {"server-adapter-autofill", "server-auth"}.isdisjoint(document["suite_ids"])
        expected = set(document["suite_ids"]) | {"server-adapter-autofill", "server-auth"}
        assert set(ran) == expected < set(document["catalog_suite_ids"])
        assert set(runner.PR_TIER_PREREQUISITES) <= set(ran)
        assert data["mode"] == data["execution_mode"] == data["selection_mode"] == "tier"
        assert all(entry["mode"] == "tier" for entry in data["results"])
        proof = tmp_path / "proof.json"
        monkeypatch.setattr(sys, "argv", ["run-all-gates.py", "--verify-shard-results", str(tmp_path),
                                        "--emit-proof", str(proof)])
        assert runner.main() == 1
        assert "mode=tier cannot prove a full gate" in capsys.readouterr().out
        assert not proof.exists()
    else:
        assert set(ran) == set(document["catalog_suite_ids"])
        assert "mode" not in data
        assert "tier" not in data["selection"]


def test_committed_policy_is_enabled_and_bound_to_unchanged_catalog(generator, runner):
    document = json.loads((ROOT / "scripts/ci/pr-tier.json").read_text(encoding="utf-8"))
    catalog = runner.selection_catalog(ROOT)
    assert document["enabled"] is True
    assert document["generated_from"]["catalog_sha256"] == catalog["catalog_sha256"]
    assert document["catalog_suite_ids"] == sorted(row["id"] for row in catalog["suites"])
    assert "web-version-restore-proof" in document["suite_ids"]
    # Evidence inputs are copied into the task worktree, not shipped with CI.
    paths = [ROOT / name for name in ("pr-tier-proposal.json", "fail30.json", "sentinels-strict.json")]
    if all(path.is_file() for path in paths):
        assert generator.generate(*paths, catalog=catalog, enabled=True) == document


def test_owned_ci_tests_pass_with_ambient_explicit_full():
    if os.environ.get("LEAF_CI_FULL") == "1":
        pytest.skip("Already running with ambient explicit full")
    result = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "tests/test_pr_tier.py", "tests/test_codebuild_ci_script.py",
         "-q", "-p", "no:cacheprovider", "-k", "not ambient_explicit_full"],
        cwd=ROOT, env=dict(os.environ, LEAF_CI_FULL="1"),
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout[-10000:] + result.stderr[-10000:]
