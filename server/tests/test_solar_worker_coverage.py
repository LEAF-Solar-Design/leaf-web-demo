"""Project Solar admission requires live coverage only on worker-required stacks."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import dependency_health
import deps
import solar_project_admission as policy
import solar_project_context as project
import test_sip_r5_routes as routes
from test_sip_r5_routes import lane  # noqa: F401


@pytest.fixture(autouse=True)
def reset_coverage_cache():
    dependency_health.reset_worker_coverage_cache()
    yield
    dependency_health.reset_worker_coverage_cache()


@pytest.fixture
def coverage(monkeypatch):
    monkeypatch.setenv("LEAF_CANONICAL_WORKER_REQUIRED", " YeS ")
    monkeypatch.setenv("LEAF_READINESS_TIMEOUT_S", "0.2")
    monkeypatch.setenv("LEAF_WORKER_HEARTBEAT_MAX_AGE_S", "30")
    # The readiness tool is deliberately different from the submitted tool.
    monkeypatch.setenv("LEAF_CANONICAL_WORKER_TOOL", "string-autofill-opt")
    monkeypatch.delenv("LEAF_EXPECTED_WORKER_REVISION", raising=False)
    state = SimpleNamespace(rows=[], calls=[], failure=None)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, args=None):
            state.calls.append((sql, args))
            if "canonical_worker_heartbeats" in sql:
                assert "WHERE tool_name = %s" in sql
                assert "ORDER BY observed_at DESC LIMIT 1" in sql
                if state.failure == "query":
                    raise ConnectionError("private-store-marker")
                self.tool = args[0]
                self.with_revision = "SELECT source_revision, observed_at " in sql
            else:
                assert sql == "SELECT set_config('statement_timeout', %s, true)"
                assert args == ("200ms",)

        def fetchone(self):
            matching = [row for row in state.rows if row[0] == self.tool]
            if not matching:
                return None
            latest = max(matching, key=lambda row: row[2])
            return latest[1:] if self.with_revision else latest[2:]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    def connect(url, **kwargs):
        assert url == "postgresql://coverage-test"
        assert kwargs == {"connect_timeout": 1}
        if state.failure == "connect":
            raise ConnectionError("private-store-marker")
        return Connection()

    def database_parts(timeout):
        assert timeout == 0.2
        return object(), SimpleNamespace(connect=connect), "postgresql://coverage-test", 200

    monkeypatch.setattr(dependency_health, "_database_parts", database_parts)
    return state


def heartbeat(coverage, tool="solar-settings", *, age=0, revision="sha256:" + "a" * 64,
              naive=False):
    observed = datetime.now(timezone.utc) - timedelta(seconds=age)
    if naive:
        observed = observed.replace(tzinfo=None)
    coverage.rows.append((tool, revision, observed))


@pytest.mark.parametrize("covered", [False, True])
def test_worker_coverage_cache_reuses_heartbeat_query(coverage, monkeypatch, covered):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(dependency_health.time, "monotonic", lambda: clock.now)
    if covered:
        heartbeat(coverage)

    assert dependency_health.canonical_worker_covers("solar-settings") is covered
    clock.now += 4.999
    assert dependency_health.canonical_worker_covers("solar-settings") is covered
    queries = [sql for sql, _args in coverage.calls if "canonical_worker_heartbeats" in sql]
    assert len(queries) == 1


@pytest.mark.parametrize("covered", [False, True])
def test_worker_coverage_cache_expires_and_never_caches_store_failure(
        coverage, monkeypatch, covered):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(dependency_health.time, "monotonic", lambda: clock.now)
    if covered:
        heartbeat(coverage)

    def query_count():
        return sum("canonical_worker_heartbeats" in sql for sql, _args in coverage.calls)

    assert dependency_health.canonical_worker_covers("solar-settings") is covered
    assert query_count() == 1
    clock.now += 5.0
    assert dependency_health.canonical_worker_covers("solar-settings") is covered
    assert query_count() == 2

    clock.now += 5.0
    coverage.failure = "query"
    for expected_count in (3, 4):
        with pytest.raises(dependency_health.DependencyUnavailable):
            dependency_health.canonical_worker_covers("solar-settings")
        assert query_count() == expected_count
    coverage.failure = None
    assert dependency_health.canonical_worker_covers("solar-settings") is covered
    assert query_count() == 5


@pytest.mark.parametrize("missing", ["absent", "stale", "future", "other-tool"])
def test_uncovered_tool_refused_without_insert(lane, coverage, monkeypatch, missing):
    if missing == "stale":
        heartbeat(coverage, age=31)
    elif missing == "future":
        heartbeat(coverage, age=-10)
    elif missing == "other-tool":
        heartbeat(coverage, tool="solar-panels-from-drawing")

    response = routes.run(lane)

    routes.refusal(response, "SIP_R4_WORKER_UNAVAILABLE", status=503, retry=True)
    assert not lane.records and not lane.a.accepted
    assert "insert" not in lane.a.events
    assert coverage.calls[-1][1] == ("solar-settings",)


@pytest.mark.parametrize("tool", ["solar-settings", "solar-panels-from-drawing"])
@pytest.mark.parametrize("naive", [False, True])
def test_covered_tool_submits_unchanged(lane, coverage, monkeypatch, tool, naive):
    params = None
    changes = {}
    if tool == "solar-panels-from-drawing":
        context = routes.policy_cases.r3b.chain(lane.s, 2)[-1][2]
        routes.policy_cases.r4.at(lane.a, monkeypatch, context, tool)
        params = {"expected_rev": context.graph["rev"]}
        changes["dwg"] = str(context.parent_version_id)
    heartbeat(coverage, tool, naive=naive)
    monkeypatch.setenv("LEAF_CANONICAL_WORKER_REQUIRED", "1")
    monkeypatch.setenv("LEAF_EXPECTED_WORKER_REVISION",
                       "760d3888018f762e0ab8dbe289dae8a2871216ea")
    assert dependency_health.canonical_worker_covers(tool) is True

    response = routes.run(lane, params, name=tool, **changes)

    assert response.status_code == 202
    assert response.json() == deps.tenant_echo({
        "job_id": str(lane.s.job), "status": "submitted",
        "error": None, "degraded_mode": False}, lane.tenant)
    assert len(lane.records) == len(lane.a.accepted) == 1
    assert lane.a.events.count("insert") == 1
    assert lane.a.accepted[0][3] == tool
    assert coverage.calls[-1][1] == (tool,)


def test_readiness_still_rejects_wrong_revision(coverage):
    heartbeat(coverage)
    env = {
        "DATABASE_URL": "postgresql://coverage-test",
        "LEAF_CANONICAL_WORKER_REQUIRED": "1",
        "LEAF_CANONICAL_WORKER_TOOL": "solar-settings",
        "LEAF_EXPECTED_WORKER_REVISION": "760d3888018f762e0ab8dbe289dae8a2871216ea",
        "LEAF_READINESS_TIMEOUT_S": "0.2",
    }
    assert dependency_health.canonical_worker_covers("solar-settings", env) is True
    worker = next(spec for spec in dependency_health.default_specs(env)
                  if spec.name == "worker")
    assert worker.required is True
    with pytest.raises(dependency_health.DependencyDegraded):
        worker.probe()


def test_coverage_never_reads_expected_revision(coverage):
    class CoverageEnv(dict):
        def get(self, key, default=None):
            assert key != "LEAF_EXPECTED_WORKER_REVISION"
            return super().get(key, default)

    heartbeat(coverage)
    env = CoverageEnv(LEAF_CANONICAL_WORKER_REQUIRED="1",
                      LEAF_READINESS_TIMEOUT_S="0.2")
    assert dependency_health.canonical_worker_covers("solar-settings", env) is True
    assert all("source_revision" not in sql for sql, _args in coverage.calls)


def test_existing_refusals_take_precedence(lane, coverage, monkeypatch):
    params = routes.body(lane)["params"]
    response = routes.run(lane, {**params, "surprise": 1})
    routes.refusal(response, "INVALID_SETTINGS_REQUEST",
                   message="Canonical Solar request was refused.")
    assert not coverage.calls

    @contextmanager
    def drained():
        yield {"refused": True}

    monkeypatch.setattr(project.write_loop, "drawing_mutation_refusal_guard", drained)
    routes.refusal(routes.run(lane), "SIP_R1_WRITES_DRAINED", status=503, retry=True)
    assert not coverage.calls
    assert not lane.records and not lane.a.accepted


@pytest.mark.parametrize("required", [None, "", "0", "false", "no", "off"])
def test_worker_not_required_keeps_today(lane, coverage, monkeypatch, required):
    if required is None:
        monkeypatch.delenv("LEAF_CANONICAL_WORKER_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("LEAF_CANONICAL_WORKER_REQUIRED", required)
    coverage.failure = "connect"

    response = routes.run(lane)

    assert response.status_code == 202
    assert response.json() == deps.tenant_echo({
        "job_id": str(lane.s.job), "status": "submitted",
        "error": None, "degraded_mode": False}, lane.tenant)
    assert len(lane.records) == len(lane.a.accepted) == 1
    assert not coverage.calls


def test_catalog_names_uncovered_tool(lane, coverage):
    tool = lane.tools["solar-settings"]
    preview = routes.policy_cases.request(lane.a)
    assert preview["status_code"] == 503
    for admission in (None, preview):
        state = policy.project_catalog_availability(tool, enabled=True, admission=admission)
        assert state["runnable"] is False
        assert state["engine_ready"] is False
        assert state["refusal_reasons"] == ["SIP_R4_WORKER_UNAVAILABLE"]
        assert state["engine_reason"] == "SIP_R4_WORKER_UNAVAILABLE"
    state = routes.states(routes.preview(lane))["solar-settings"]
    assert state["runnable"] is False
    assert state["refusal_reasons"] == ["SIP_R4_WORKER_UNAVAILABLE"]
    assert not lane.records and not lane.a.accepted


@pytest.mark.parametrize("failure", ["connect", "query"])
def test_worker_lookup_store_failure_is_not_missing_coverage(lane, coverage, failure):
    coverage.failure = failure

    response = routes.run(lane)

    routes.refusal(response, "SIP_R1_STORE_UNAVAILABLE", status=503, retry=True)
    assert "private-store-marker" not in response.text
    assert not lane.records and not lane.a.accepted
    state = policy.project_catalog_availability(lane.tools["solar-settings"], enabled=True)
    assert state["runnable"] is False
    assert state["refusal_reasons"] == ["SIP_R1_STORE_UNAVAILABLE"]
