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


def _server_graph_tools():
    path = Path(__file__).resolve().parents[1] / "solar_project_tools.py"
    spec = importlib.util.spec_from_file_location("solar_project_tools_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SUPPORTED_TOOLS


def test_launcher_project_worker_selection(monkeypatch):
    configured = {"DATABASE_URL": "postgresql://local/leaf"}
    tools = _server_graph_tools()
    assert _launcher_module().project_worker_tools() == tools and len(tools) == 13
    for tool in sorted(tools):
        with monkeypatch.context() as patch:
            launcher, spawned = _fake_launcher(patch, ["--canonical-worker-tool", tool], configured)
            assert launcher.main() == 0
            assert [item[0] for item in spawned] == ["broker", "worker", "app"]
            worker = next(item for item in spawned if item[0] == "worker")
            assert worker[1] == [launcher.sys.executable, "canonical_worker.py", "--tool", tool, "--readiness"]
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


def test_launcher_project_worker_refusals(monkeypatch, capsys, tmp_path):
    configured = {"DATABASE_URL": "postgresql://local/leaf"}
    for argv, env, message in (
        (["--canonical-worker-tool", "solar-settings"], {}, "requires a platform database"),
        (["--canonical-worker-tool", "solar-settings"],
         {**configured, "LEAF_CANONICAL_WORKER_TOOL": "arlo-design"}, "conflicts"),
        (["--canonical-worker-id", "w1"], configured, "requires --canonical-worker-tool"),
        *[(["--canonical-worker-tool", tool], configured, "Invalid canonical worker tool")
          for tool in ("", "Solar-settings", "bad tool", "a" * 65, "a\n",
                       # A padded graph tool name is refused as written, never trimmed into one.
                       " solar-settings", "solar-settings ", " solar-settings ",
                       "\tsolar-settings", "solar-settings\t")],
        # Well-formed names the worker cannot run with --readiness: an unknown name, a
        # name one character off a graph tool, and the worker's two non-graph adapters.
        *[(["--canonical-worker-tool", tool], env, "Unsupported canonical worker tool")
          for tool in ("solar-not-a-tool", "solar-setting", "solar-settings-x", "string-autofill",
                       "arlo-design")
          for env in (configured, {})],
        (["--canonical-worker-tool", "solar-not-a-tool", "--canonical-worker-id", "bad id"],
         configured, "Unsupported canonical worker tool"),
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
    # An unreadable or malformed tool table refuses every selection before any child.
    broken = {
        "missing": None,
        "raises": "raise RuntimeError('boom')\n",
        "absent": "OTHER = frozenset(('solar-settings',))\n",
        "set": "SUPPORTED_TOOLS = {'solar-settings'}\n",
        "empty": "SUPPORTED_TOOLS = frozenset()\n",
        "mixed": "SUPPORTED_TOOLS = frozenset(('solar-settings', 7))\n",
        "exits": "raise SystemExit(7)\n",
        # Any other way of leaving the table's code is a failure too, never an escape.
        "generator-exit": "raise GeneratorExit('boom')\n",
        # A member the launcher could never select makes the whole table unavailable.
        "empty-member": "SUPPORTED_TOOLS = frozenset(('', 'solar-settings'))\n",
        "only-empty-member": "SUPPORTED_TOOLS = frozenset(('',))\n",
        "padded-member": "SUPPORTED_TOOLS = frozenset(('solar-settings', 'solar-settings '))\n",
        "upper-member": "SUPPORTED_TOOLS = frozenset(('solar-settings', 'Solar-settings'))\n",
        "long-member": "SUPPORTED_TOOLS = frozenset(('solar-settings', 'a' * 65))\n",
        # Exact types only: a subclass of the table or of a name is not the table.
        "subclass": "class F(frozenset):\n    pass\nSUPPORTED_TOOLS = F(('solar-settings',))\n",
        "str-subclass": "class S(str):\n    pass\nSUPPORTED_TOOLS = frozenset((S('solar-settings'),))\n",
    }
    for name, source in broken.items():
        directory = tmp_path / name
        directory.mkdir()
        if source is not None:
            (directory / "solar_project_tools.py").write_text(source, encoding="utf-8")
        with monkeypatch.context() as patch:
            launcher, spawned = _fake_launcher(patch, ["--canonical-worker-tool", "solar-settings"], configured)
            patch.setattr(launcher, "SERVER_DIR", directory)
            patch.setattr(launcher.sys, "dont_write_bytecode", False)
            assert launcher.project_worker_tools() is None, name
            assert launcher.main() == 2
            assert spawned == []
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == "Canonical worker tool table is unavailable.\n", name
            assert not (directory / "__pycache__").exists()
    # An operator interrupt while the table loads is never swallowed as a table failure.
    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    (interrupted / "solar_project_tools.py").write_text("raise KeyboardInterrupt\n", encoding="utf-8")
    with monkeypatch.context() as patch:
        launcher, spawned = _fake_launcher(patch, ["--canonical-worker-tool", "solar-settings"], configured)
        patch.setattr(launcher, "SERVER_DIR", interrupted)
        with pytest.raises(KeyboardInterrupt):
            launcher.project_worker_tools()
        with pytest.raises(KeyboardInterrupt):
            launcher.main()
        assert spawned == []
        capsys.readouterr()
    # Reading a good table writes nothing beside it, even with bytecode caching on,
    # whether the selection is then refused or the table is only read.
    tools = _server_graph_tools()
    copied = tmp_path / "copied"
    copied.mkdir()
    table = Path(__file__).resolve().parents[1] / "solar_project_tools.py"
    (copied / "solar_project_tools.py").write_bytes(table.read_bytes())
    with monkeypatch.context() as patch:
        launcher, spawned = _fake_launcher(patch, ["--canonical-worker-tool", "solar-not-a-tool"], configured)
        patch.setattr(launcher, "SERVER_DIR", copied)
        patch.setattr(launcher.sys, "dont_write_bytecode", False)
        assert launcher.project_worker_tools() == tools
        assert launcher.main() == 2
        assert spawned == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "Unsupported canonical worker tool.\n"
        assert sorted(item.name for item in copied.iterdir()) == ["solar_project_tools.py"]
    # No selection never reads the table, so a broken table cannot stop a default boot.
    with monkeypatch.context() as patch:
        launcher, spawned = _fake_launcher(patch, [], configured)
        patch.setattr(launcher, "project_worker_tools", lambda: pytest.fail("table read without a selection"))
        assert launcher.main() == 0
        assert [item[0] for item in spawned] == ["broker", "worker", "app"]
        capsys.readouterr()


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
