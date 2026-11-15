"""Exercise selected partitions through the real runner without real suites."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parent
IDS = ["web-z", "api-a", "web-a", "web-shared", "other"]
PATTERNS = ["web-", "shared", "api-"]
SELECTED = IDS[:-1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def context(tmp_path, monkeypatch):
    g = load_module("selected_runner_test", SCRIPTS / "run-all-gates.py")
    helper = load_module("selected_helper_test", SCRIPTS / "ci/selected_shards.py")
    suites = [g.Suite(sid, sid, "pytest", SCRIPTS, ["fake"], 1) for sid in IDS]
    ran, migrated = [], []
    monkeypatch.setattr(g, "build_suites", lambda: suites)
    monkeypatch.setattr(g, "migrate_gate_database", lambda ss: migrated.append([s.id for s in ss]))
    monkeypatch.setattr(g, "record_attempt", lambda *args: None)
    monkeypatch.setattr(g, "print_scoreboard", lambda *args: None)

    def execute(suite, log_dir, attempt):
        ran.append(suite.id)
        return g.Result(suite, "PASS", "1", 0.01, counts={"got": 1, "passed": 1})

    monkeypatch.setattr(g, "run_suite_guarded", execute)

    def invoke(flags):
        ran.clear()
        migrated.clear()
        result = tmp_path / "result.json"
        result.unlink(missing_ok=True)
        monkeypatch.setattr(sys, "argv", ["runner", "--log-dir", str(tmp_path),
                                         "--result-json", str(result), *flags])
        rc = g.main()
        return rc, list(ran), json.loads(result.read_text()) if result.exists() else None

    return g, helper, suites, invoke, migrated


def flags(index=0, count=3, path=None, patterns=PATTERNS):
    args = [item for pattern in patterns for item in ("--only", pattern)]
    args += ["--shard-count", str(count), "--shard-index", str(index)]
    if path is not None:
        args += ["--selected-shard-plan", str(path)]
    return args


@pytest.mark.parametrize("weighted", [False, True])
@pytest.mark.parametrize("count", [1, 3, 7])
def test_helper_plan_runs_exact_once_union_in_catalog_order(context, tmp_path, weighted, count):
    g, helper, suites, invoke, migrated = context
    timings = tmp_path / "seconds.json"
    timings.write_text(json.dumps({"schema": "leaf.ci.suite-seconds.v1",
                                   "seconds": dict(zip(IDS, [9, 2, 6, 1, 3]))}))
    seen = []
    for index in range(count):
        plan = helper.make_plan(IDS, PATTERNS, count, index, timings if weighted else None)
        assert plan["algorithm"] == ("lpt" if weighted else "equal-count")
        if count == 3:
            assert plan["shards"] == (
                [["web-z"], ["web-a"], ["api-a", "web-shared"]] if weighted else
                [["web-z", "api-a"], ["web-a"], ["web-shared"]])
        if count == 7:
            assert plan["shards"][4:] == [[], [], []]
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        rc, ran, result = invoke(flags(index, count, path))
        assert rc == 0
        assert ran == plan["shards"][index] == result["suite_ids"]
        assert migrated == [ran]
        assert all(result[key] == "selected" for key in ("mode", "selection_mode", "execution_mode"))
        seen.extend(ran)
    assert len(seen) == len(set(seen)) == len(SELECTED)
    assert set(seen) == set(SELECTED)


def corrupt(plan, kind):
    if kind == "schema": plan["schema"] = "wrong"
    elif kind == "shape": return []
    elif kind == "selection": plan["selected_ids"] = ["other"]
    elif kind == "selected-order": plan["selected_ids"].reverse()
    elif kind == "selected-duplicate": plan["selected_ids"].append(plan["selected_ids"][0])
    elif kind == "count": plan["shards"].append([])
    elif kind == "coordinate-count": plan["shard_count"] = 9
    elif kind == "coordinate-index": plan["shard_index"] = 9
    elif kind == "local": plan["shard_ids"] = ["other"]
    elif kind == "duplicate": plan["shards"][1].append(plan["shards"][0][0])
    elif kind == "within-duplicate": plan["shards"][0].append(plan["shards"][0][0])
    elif kind == "unknown": plan["shards"][1].append("unknown")
    elif kind == "incomplete": plan["shards"][1].pop()
    elif kind == "order": plan["shards"][0].reverse()
    elif kind == "empty-zero": plan["shards"][1].extend(plan["shards"][0]); plan["shards"][0] = []
    elif kind == "algorithm": plan["algorithm"] = "candidate-magic"
    elif kind == "list-shape": plan["shards"][1] = "web-a"
    return plan


@pytest.mark.parametrize("kind", ["missing", "broken", "schema", "shape", "selection",
    "selected-order", "selected-duplicate", "count", "coordinate-count", "coordinate-index",
    "local", "duplicate", "within-duplicate", "unknown", "incomplete", "order", "empty-zero", "algorithm", "list-shape",
    "no-plan"])
@pytest.mark.parametrize("index", [0, 1])
def test_bad_plan_falls_back_only_on_zero(context, tmp_path, capsys, kind, index):
    g, helper, suites, invoke, migrated = context
    path = tmp_path / "plan.json"
    plan = helper.make_plan(IDS, PATTERNS, 2, index)
    if kind != "missing":
        path.write_text("{" if kind == "broken" else json.dumps(corrupt(plan, kind)))
    rc, ran, result = invoke(flags(index, 2, None if kind == "no-plan" else path))
    assert "conservative fallback" in capsys.readouterr().out
    if index == 0:
        assert rc == 0 and ran == SELECTED
        assert result["mode"] == result["execution_mode"] == result["selection_mode"] == "selected"
    else:
        assert rc == 2 and not ran and result is None and not migrated


def test_dead_substring_rejected_before_plan_or_migration(context, tmp_path, capsys):
    *_, invoke, migrated = context
    rc, ran, result = invoke(flags(path=tmp_path / "missing", patterns=["web", "typo"]))
    assert rc == 2 and not ran and result is None and not migrated
    assert "no suites match --only 'typo'" in capsys.readouterr().out


def test_ordinary_selection_and_full_partition_unchanged(context):
    g, helper, suites, invoke, migrated = context
    rc, ran, result = invoke(["--only", "web-", "--only", "shared"])
    assert rc == 0 and ran == [sid for sid in IDS if "web-" in sid]
    assert result["mode"] == "selected"
    rc, ran, result = invoke([])
    assert rc == 0 and ran == IDS and "mode" not in result
    rc, ran, result = invoke(["--shard-count", "3", "--shard-index", "1"])
    assert rc == 0 and ran == [s.id for s in g.partition_suites(suites, 3)[1]]
    assert "mode" not in result


@pytest.mark.parametrize("extra", [[], ["--only", "web"],
    ["--only", "web", "--shard-index", "0"],
    ["--only", "web", "--shard-count", "2", "--shard-index", "2"],
    ["--list"], ["--verify-shard-results", "unused"],
    ["--verify-gate-proof", "unused", "--expect-tree", "abc"],
    ["--only", "web", "--shard-count", "1", "--shard-index", "0", "--tier-file", "unused"]])
def test_plan_never_silently_ignored(context, tmp_path, extra):
    *_, invoke, migrated = context
    rc, ran, result = invoke(["--selected-shard-plan", str(tmp_path / "plan"), *extra])
    assert rc == 2 and not ran and result is None and not migrated


@pytest.mark.parametrize("key", ["mode", "execution_mode", "selection_mode"])
def test_full_fanin_rejects_selected_even_when_union_is_full(
        context, tmp_path, key, capsys, monkeypatch):
    g, helper, suites, invoke, migrated = context
    # Use an ordinary full-size union: every fake ID contains either '-' or 'other'.
    plan = helper.make_plan(IDS, ["-", "other"], 1, 0)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    rc, ran, result = invoke(flags(0, 1, path, ["-", "other"]))
    assert rc == 0 and ran == IDS
    assert g.verify_shard_results(tmp_path) == 1
    assert "mode=selected cannot prove a full gate" in capsys.readouterr().out
    # Each independent marker is sufficient to keep full proof fail-closed.
    for marker in ("mode", "execution_mode", "selection_mode"):
        result.pop(marker, None)
    (tmp_path / "result.json").write_text(json.dumps(result))
    assert g.verify_shard_results(tmp_path) == 0
    result[key] = "selected"
    (tmp_path / "result.json").write_text(json.dumps(result))
    assert g.verify_shard_results(tmp_path) == 1
    proof = tmp_path / "proof.json"

    def forbidden_emit(*args, **kwargs):
        pytest.fail("selected artifacts reached full proof emission")

    monkeypatch.setattr(g, "emit_gate_proof", forbidden_emit)
    monkeypatch.setattr(sys, "argv", ["runner", "--verify-shard-results", str(tmp_path),
                                     "--emit-proof", str(proof)])
    assert g.main() == 1
    assert not proof.exists()


def test_selected_invariants_registered_in_existing_selftest(context):
    # Inspect the real catalog rather than the fake execution catalog.
    real = load_module("selected_registration_test", SCRIPTS / "run-all-gates.py")
    suite = next(s for s in real.build_suites() if s.id == "gate-runner-selftest")
    assert {"ci/tests/test_selected_shards.py", "test_selected_gate_shards.py",
            "../tests/test_ci_selected_shards.py"} <= set(suite.argv)
