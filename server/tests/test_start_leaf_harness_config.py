"""Local launcher contract for the real Build sidecar."""

import importlib.util
from pathlib import Path


def _launcher_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "start-leaf.py"
    spec = importlib.util.spec_from_file_location("start_leaf_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_live_turn_budget_outlives_spine_and_authoring():
    launcher = _launcher_module()
    assert int(launcher.DEFAULT_TURN_MAX_S) > int(
        launcher.DEFAULT_SPINE_TURN_TIMEOUT_S
    )
    assert int(launcher.DEFAULT_SPINE_TURN_TIMEOUT_S) > int(
        launcher.DEFAULT_AUTHOR_TIMEOUT_S
    )
