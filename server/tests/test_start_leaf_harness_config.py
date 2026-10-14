"""Local launcher contract for the real Build sidecar."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _launcher_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "start-leaf.py"
    spec = importlib.util.spec_from_file_location("start_leaf_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_launcher(monkeypatch, argv, env):
    launcher = _launcher_module()
    spawned = []
    monkeypatch.setattr(launcher.sys, "argv", ["start-leaf.py", "--no-web", *argv])
    monkeypatch.setattr(launcher.os, "environ", env)
    monkeypatch.setattr(launcher, "source_revision", lambda _: "a" * 40)
    monkeypatch.setattr(launcher, "pick_port", lambda name, preferred, *a: preferred)
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)
    monkeypatch.setattr(launcher, "_make_win_job", lambda: None)
    monkeypatch.setattr(launcher, "wait_healthy", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "cleanup", lambda: None)
    monkeypatch.setattr(launcher, "say", lambda *a: None)

    def spawn(name, argv, cwd, child_env):
        spawned.append((name, argv, cwd, dict(child_env)))
        return SimpleNamespace(name=name, proc=SimpleNamespace(poll=lambda: None))

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "spawn", spawn)
    monkeypatch.setattr(launcher.time, "sleep", stop)
    return launcher, spawned


def test_launcher_project_worker_selection(monkeypatch):
    for argv, env in (
        (["--canonical-worker-tool", "solar-settings", "--canonical-worker-id", "w1"],
         {"DATABASE_URL": "postgresql://local/leaf"}),
        (["--canonical-worker-tool", "solar-settings"],
         {"DATABASE_URL": "postgresql://local/leaf", "LEAF_CANONICAL_WORKER_TOOL": "solar-settings"}),
        (["--canonical-worker-tool", "solar-settings", "--canonical-worker-id=-w1"],
         {"DATABASE_URL": "postgresql://local/leaf"}),
        ([], {"DATABASE_URL": "postgresql://local/leaf"}),
    ):
        with monkeypatch.context() as patch:
            launcher, spawned = _fake_launcher(patch, argv, env)
            assert launcher.main() == 0
            assert [item[0] for item in spawned] == ["broker", "worker", "app"]
            worker = next(item for item in spawned if item[0] == "worker")
            expected = [launcher.sys.executable, "canonical_worker.py"]
            if argv:
                expected += ["--tool", "solar-settings", "--readiness"]
                if "--canonical-worker-id" in argv:
                    expected += ["--worker-id", "w1"]
                if "--canonical-worker-id=-w1" in argv:
                    expected += ["--worker-id=-w1"]
                assert all(item[3]["LEAF_CANONICAL_WORKER_TOOL"] == "solar-settings" for item in spawned)
            else:
                assert all("LEAF_CANONICAL_WORKER_TOOL" not in item[3] for item in spawned)
            assert worker[1] == expected


def test_launcher_project_worker_refusals(monkeypatch, capsys):
    configured = {"DATABASE_URL": "postgresql://local/leaf"}
    for argv, env, message in (
        (["--canonical-worker-tool", "solar-settings"], {}, "requires a platform database"),
        (["--canonical-worker-tool", "solar-settings"],
         {**configured, "LEAF_CANONICAL_WORKER_TOOL": "arlo-design"}, "conflicts"),
        (["--canonical-worker-id", "w1"], configured, "requires --canonical-worker-tool"),
        *[(["--canonical-worker-tool", tool], configured, "Invalid canonical worker tool")
          for tool in ("", "Solar-settings", "bad tool", "a" * 65, "a\n")],
        *[(["--canonical-worker-tool", "solar-settings", "--canonical-worker-id", value],
           configured, "valid canonical worker id") for value in ("", "bad id", "a" * 129)],
    ):
        with monkeypatch.context() as patch:
            launcher, spawned = _fake_launcher(patch, argv, env)
            assert launcher.main() == 2
            assert spawned == []
            captured = capsys.readouterr()
            assert captured.out == ""
            assert len(captured.err.splitlines()) == 1
            assert message in captured.err


def test_with_harness_keeps_repo_mutation_off_without_writer_lease_database():
    assert _launcher_module().harness_authoring_defaults({}) == ("0", "disabled")


def test_with_harness_enables_single_writer_when_database_is_configured():
    launcher = _launcher_module()
    configured = {
        "LEAF_HARNESS_DATABASE_URL": "postgresql://local/leaf",
    }
    assert launcher.harness_authoring_defaults(configured) == ("1", "singleton")
    assert launcher.harness_session_store_default(configured) == "postgres"
    assert launcher.agent_store_default(configured) == "postgres"


def test_with_harness_uses_file_sessions_without_database():
    launcher = _launcher_module()
    assert launcher.harness_session_store_default({}) == "file"
    assert launcher.agent_store_default({}) == "legacy"
    assert launcher.platform_database_configured({}) is False


def test_shared_database_enables_canonical_worker():
    launcher = _launcher_module()
    assert launcher.platform_database_configured({
        "DATABASE_URL": "postgresql://local/leaf",
    }) is True
    assert launcher.platform_database_configured({
        "LEAF_HARNESS_DATABASE_URL": "postgresql://local/leaf",
    }) is False


def test_source_revision_prefers_operator_value(monkeypatch):
    launcher = _launcher_module()
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("git must not run when revision is explicit")
        ),
    )
    assert launcher.source_revision({
        "LEAF_BUILD_REVISION": "a" * 40,
    }) == "a" * 40


def test_live_turn_budget_outlives_spine_and_authoring():
    launcher = _launcher_module()
    assert int(launcher.DEFAULT_TURN_MAX_S) > int(
        launcher.DEFAULT_SPINE_TURN_TIMEOUT_S
    )
    assert int(launcher.DEFAULT_SPINE_TURN_TIMEOUT_S) > int(
        launcher.DEFAULT_AUTHOR_TIMEOUT_S
    )
