"""Offline coverage for W5's unwired local solar graph commit adapter."""
import copy
from contextlib import contextmanager
import json
import re
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import leaf_cloud_client as cloud
import solar_local_graph as local
from solar_graph_context import resolve_graph_context
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_solve_commit import seed, transfer
from test_w1_graph_versions import drawing, request_for, commit, TENANT, DRAWING


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("offline network forbidden")
    monkeypatch.setattr(cloud.requests.sessions.Session, "request", refuse)


@contextmanager
def held(backend):
    fence = store.acquire_checkout_fence(backend, "fixture-tenant", "solar", "fixture-owner", 300)
    try:
        yield fence
    finally:
        store.release_checkout(backend, "fixture-tenant", "solar", "fixture-owner")


def latest(backend):
    return store.load_manifest(backend, "fixture-tenant", "solar")["latest"]


def settings(value=3):
    return {"expected_rev": 0, "changes": {"panels_in_sequence": value}, "drawing_id": "solar"}


def run(backend, params=None, **overrides):
    kwargs = {"drawing_id": "solar", "source_version": 1, "holder": "fixture-owner",
              "fence": 1, "job_id": "local-job"}
    tool = overrides.pop("tool", "solar-settings")
    kwargs.update(overrides)
    return local.run_local_graph_commit(
        backend, "fixture-tenant", tool, settings() if params is None else params, **kwargs)


@contextmanager
def refused(code):
    with pytest.raises(GraphValidationError) as excinfo:
        yield
    assert excinfo.value.code == code


def test_seeded_context(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    result = resolve_graph_context(backend, "fixture-tenant", "solar")
    assert set(result) == {"resolved_version", "current_head", "representation", "graph",
                           "graph_sha256", "project_id", "local_commit_ready", "refusal_reason"}
    assert result["representation"] == "intake"
    assert result["resolved_version"] == result["current_head"] == 1
    assert result["local_commit_ready"] is True
    assert result["refusal_reason"] is None
    assert result["graph"] == graph and result["graph_sha256"] == digest(graph)
    assert result["project_id"] == "leaf:project:00000000-0000-4000-8000-000000000001"


def test_old_context(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        run(backend, fence=fence)
    result = resolve_graph_context(backend, "fixture-tenant", "solar", 1)
    assert result["resolved_version"] == 1 and result["current_head"] == 2
    assert result["local_commit_ready"] is False
    assert result["refusal_reason"] == "not_current_head"


def test_bundle_context(drawing, graph):
    backend, _ = drawing
    request = request_for(backend, graph)
    commit(drawing, request)
    result = resolve_graph_context(backend, TENANT, DRAWING)
    assert result["representation"] == "dwg-bundle"
    assert result["local_commit_ready"] is False
    assert result["refusal_reason"] == "licensed_graph_commit_required"
    assert result["graph"] == request["graph"]


def test_raw_dwg_context(drawing):
    backend, _ = drawing
    with refused("GRAPH_NOT_EMBEDDED"):
        resolve_graph_context(backend, TENANT, DRAWING)


def test_bundle_project_mismatch(drawing, graph):
    backend, _ = drawing
    commit(drawing, request_for(backend, graph))
    with refused("PROJECT_MISMATCH"):
        resolve_graph_context(backend, TENANT, DRAWING, project_id="another-project")


@pytest.mark.parametrize("defect,code", [
    ("digest", "GRAPH_DIGEST_MISMATCH"), ("missing", "GRAPH_NOT_EMBEDDED"),
    ("deep", "GRAPH_NOT_EMBEDDED"),
])
def test_bad_intake(graph, tmp_path, monkeypatch, defect, code):
    backend, intake = seed(tmp_path, monkeypatch, graph)
    if defect == "digest":
        intake["solar_design_graph_sha256"] = "0" * 64
    else:
        del intake["solar_design_graph"]
    _, key, _ = store.resolve_version_entry(backend, "fixture-tenant", "solar", 1)
    data = b"[" * 5000 + b"0" + b"]" * 5000 if defect == "deep" else json.dumps(intake).encode("utf-8")
    backend.put(key, data)
    with refused(code):
        resolve_graph_context(backend, "fixture-tenant", "solar")


@pytest.mark.parametrize("drawing_id,version,project_id", [
    ("missing", "head", None), ("solar", 77, None), ("solar", 0, None),
    ("solar", "1", None), ("solar", "head", "p" * 101),
])
def test_unavailable_context(graph, tmp_path, monkeypatch, drawing_id, version, project_id):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused("GRAPH_CONTEXT_UNAVAILABLE"):
        resolve_graph_context(backend, "fixture-tenant", drawing_id, version, project_id=project_id)


def test_project_mismatch(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused("PROJECT_MISMATCH"):
        resolve_graph_context(backend, "fixture-tenant", "solar", project_id="another-project")


def test_settings_commit(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    params = settings()
    before = copy.deepcopy(params)
    with held(backend) as fence:
        result = run(backend, params, fence=fence)
    assert set(result) == {"schema_version", "adapter", "tenant_id", "job_id", "tool",
                           "project_id", "drawing_id", "request_sha256", "new_version",
                           "before_graph_sha256", "graph_sha256", "intake_sha256",
                           "before_rev", "after_rev", "drawing_changed", "replayed"}
    assert result["schema_version"] == local.RESULT_SCHEMA
    assert result["adapter"] == local.ADAPTER_KIND
    assert result["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert result["before_rev"] == 0 and result["after_rev"] == 1
    assert result["replayed"] is False and result["drawing_changed"] is True
    assert result["before_graph_sha256"] == digest(graph)
    assert latest(backend) == 2
    _, _, entry = store.resolve_version_entry(backend, "fixture-tenant", "solar", 2)
    assert entry["workitem_id"] == "solar-graph:" + result["job_id"]
    assert entry["note"] == "solar-graph-commit:" + result["request_sha256"]
    assert params == before and params["drawing_id"] == "solar"


@pytest.mark.parametrize("field,value", [
    ("graph_sha256", "0" * 64), ("representation", "dwg-bundle"),
    ("resolved_version", 99),
])
def test_commit_readback_failed(graph, tmp_path, monkeypatch, field, value):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    real_resolve = local.resolve_graph_context
    calls = 0

    def changed_readback(*args, **kwargs):
        nonlocal calls
        result = real_resolve(*args, **kwargs)
        calls += 1
        if calls == 2:
            result = dict(result)
            result[field] = value
        return result

    monkeypatch.setattr(local, "resolve_graph_context", changed_readback)
    with held(backend) as fence:
        with refused("GRAPH_COMMIT_READBACK_FAILED"):
            run(backend, fence=fence)
    assert calls == 2


def test_commit_readback_exception_is_a_readback_failure(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    real_resolve = local.resolve_graph_context
    calls = 0

    def failed_readback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GraphValidationError("GRAPH_CONTEXT_UNAVAILABLE")
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(local, "resolve_graph_context", failed_readback)
    with held(backend) as fence:
        with refused("GRAPH_COMMIT_READBACK_FAILED"):
            run(backend, fence=fence)
    assert latest(backend) == 2


def test_commit_readback_compares_stored_bytes(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    real_publish = local.publish_version

    def changed_bytes(*args, **kwargs):
        receipt = real_publish(*args, **kwargs)
        _, key = store.resolve_version(backend, "fixture-tenant", "solar", receipt["version"])
        intake = json.loads(backend.get(key))
        intake["layers"] = ["changed"]
        backend.put(key, json.dumps(intake).encode("utf-8"))
        return receipt

    monkeypatch.setattr(local, "publish_version", changed_bytes)
    with held(backend) as fence:
        with refused("GRAPH_COMMIT_READBACK_FAILED"):
            run(backend, fence=fence)


def test_builtin_receives_graph_copy(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    builtin = local._load_builtin("solar-settings")
    real_run = builtin.run

    def mutating_run(input_graph, params):
        original = copy.deepcopy(input_graph)
        input_graph["rev"] = 999
        return real_run(copy.deepcopy(original), params)

    monkeypatch.setattr(builtin, "run", mutating_run)
    with held(backend) as fence:
        result = run(backend, fence=fence)
    assert result["new_version"]["version"] == 2
    assert result["before_rev"] == 0
    assert result["after_rev"] == 1


def test_correction_commit(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        result = run(backend, transfer(graph), tool="solar-correct-string", fence=fence)
    assert result["new_version"]["version"] == 2
    assert result["after_rev"] == 1 and result["tool"] == "solar-correct-string"


def test_replay_without_checkout(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        first = run(backend, fence=fence)
    replay = run(backend, fence=fence)
    assert replay["new_version"] == first["new_version"]
    assert replay["replayed"] is True and latest(backend) == 2


def test_stale_new_job(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        run(backend, fence=fence)
        with refused("STALE_GRAPH_REVISION"):
            run(backend, settings(4), fence=fence, job_id="another-job")
    assert latest(backend) == 2


@pytest.mark.parametrize("value", ["other", 7])
def test_routing_conflict(graph, tmp_path, monkeypatch, value):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    params = settings()
    params["drawing_id"] = value
    with refused("DRAWING_ID_CONFLICT"):
        run(backend, params)
    assert latest(backend) == 1


def test_bundle_never_computes(drawing, graph, monkeypatch):
    backend, fence = drawing
    commit(drawing, request_for(backend, graph))
    def fail(*args, **kwargs):
        pytest.fail("builtin reached for DWG")
    monkeypatch.setattr(local._load_builtin("solar-settings"), "run", fail)
    with refused("LICENSED_GRAPH_COMMIT_REQUIRED"):
        local.run_local_graph_commit(
            backend, TENANT, "solar-settings", {"expected_rev": 0, "changes": {"panels_in_sequence": 3}},
            drawing_id=DRAWING, source_version=2, holder="writer", fence=fence, job_id="dwg-job")


def test_cancel_never_publishes(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    def fail(*args, **kwargs):
        raise AssertionError("publication reached for cancel")
    monkeypatch.setattr(write_loop, "_put_bytes_version", fail)
    with refused("GRAPH_COMMIT_CANCELLED"):
        run(backend, {"expected_rev": 0, "cancel": True})
    assert latest(backend) == 1


@pytest.mark.parametrize("params,code", [
    ({"expected_rev": 0, "changes": {"not_a_setting": 1}}, "INVALID_SETTINGS_REQUEST"),
    ({"expected_rev": 5, "changes": {"panels_in_sequence": 3}}, "STALE_GRAPH_REVISION"),
])
def test_builtin_refusals(graph, tmp_path, monkeypatch, params, code):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused(code):
        run(backend, params)
    assert latest(backend) == 1


def test_empty_correction(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused("INVALID_CORRECTION"):
        run(backend, {"expected_rev": 0}, tool="solar-correct-string")
    assert latest(backend) == 1


@pytest.mark.parametrize("tool", ["solar-size-strings", "../solar_settings"])
def test_unknown_tool(tool):
    with refused("UNKNOWN_LOCAL_GRAPH_TOOL"):
        run(None, tool=tool)


@pytest.mark.parametrize("source_version", [True, 0, "1"])
def test_invalid_parent(source_version):
    with refused("INVALID_PARENT_VERSION"):
        run(None, source_version=source_version)


@pytest.mark.parametrize("tool,code", [
    ("solar-settings", "INVALID_SETTINGS_REQUEST"),
    ("solar-correct-string", "INVALID_CORRECTION"),
])
def test_params_must_be_dict(tool, code):
    with refused(code):
        run(None, [], tool=tool)


def test_checkout_required(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with refused("CHECKOUT_REQUIRED"):
        run(backend)
    assert latest(backend) == 1


def test_request_digest():
    args = ["solar-settings", "solar", 1, {"expected_rev": 0}]
    value = local.request_digest(*args)
    assert re.fullmatch("[0-9a-f]{64}", value)
    assert value == local.request_digest(*copy.deepcopy(args))
    assert value == digest(dict(zip(("tool", "drawing_id", "source_version", "params"), args)))
    for index, replacement in enumerate(["solar-correct-string", "other", 2, {"expected_rev": 1}]):
        changed = copy.deepcopy(args)
        changed[index] = replacement
        assert local.request_digest(*changed) != value


def test_packaged_source_only(tmp_path, monkeypatch):
    source = (SERVER / "solar_local_graph.py").read_text(encoding="utf-8")
    assert all(word not in source for word in ("tool_loader", "import_module", "exec("))
    assert "spec_from_file_location" in source
    monkeypatch.chdir(tmp_path)
    (tmp_path / "builtins").mkdir()
    (tmp_path / "builtins" / "solar_settings.py").write_text('raise RuntimeError("decoy loaded")\n')
    local._load_builtin.cache_clear()
    try:
        module = local._load_builtin("solar-settings")
        assert Path(module.__file__).resolve().parent == (SERVER / "builtins").resolve()
    finally:
        local._load_builtin.cache_clear()
