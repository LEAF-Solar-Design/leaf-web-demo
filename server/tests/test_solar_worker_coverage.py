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


# Monitor cases stay in this registered suite; the admission tests above are unchanged.
import builtins
from copy import deepcopy
import importlib.util
from io import StringIO
import json
import re
import sqlite3
from uuid import UUID

import solar_submitted_monitor as submitted_monitor


_MONITOR_PRIVATE = "private-monitor-credential-marker"
_MONITOR_TABLES = (
    "jobs", "canonical_worker_heartbeats", "live_projects", "orgs",
    "live_project_authority_modes", "tenant_authority_modes",
)


def _monitor_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@pytest.fixture
def monitor_store(monkeypatch):
    """Execute the production SELECT, adapting only named parameter placeholders.

    Transaction controls and the driver clock are recorded separately. SQLite's
    result timestamps are decoded to the aware datetimes psycopg returns.
    """
    db = sqlite3.connect(":memory:")
    assert db.execute("SELECT '{\"schema\":\"ok\"}'->>'schema'").fetchone() == ("ok",)
    db.executescript("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, tool_name TEXT, created_at TEXT, updated_at TEXT,
            deleted_at TEXT, kind TEXT, execution_context TEXT, request_tenant_id TEXT,
            org_id TEXT, project_id TEXT, status TEXT, attempt INTEGER, max_attempts INTEGER,
            params TEXT, result TEXT, error TEXT, lease_owner TEXT, lease_expires_at TEXT
        );
        CREATE TABLE live_projects (org_id TEXT, project_id TEXT, PRIMARY KEY(org_id, project_id));
        CREATE TABLE orgs (org_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE tenant_authority_modes (org_id TEXT PRIMARY KEY, authority_mode TEXT);
        CREATE TABLE live_project_authority_modes (
            org_id TEXT, project_id TEXT, authority_mode TEXT, PRIMARY KEY(org_id, project_id)
        );
        CREATE TABLE canonical_worker_heartbeats (
            worker_id TEXT PRIMARY KEY, tool_name TEXT, runtime TEXT,
            source_revision TEXT, source_sha256 TEXT, observed_at TEXT
        );
    """)
    state = SimpleNamespace(
        db=db, now=datetime(2026, 10, 26, 12, tzinfo=timezone.utc),
        failure=None, corrupt=None, calls=[], connects=[], connections=[], cursors=[],
        mutations=[], helper_calls=[], fetch_sizes=[], serial=0, row_override=None,
    )
    controls = [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SET LOCAL statement_timeout = '2000ms'",
        "SET LOCAL lock_timeout = '500ms'",
        "SELECT transaction_timestamp()",
    ]

    def forbidden(*args, **kwargs):
        state.helper_calls.append((args, kwargs))
        raise AssertionError("monitor called a job/worker mutation helper")

    def guard_helpers():
        for name in (
            "connection", "submit_project_graph_job", "claim_project_graph_job",
            "heartbeat_project_graph_job", "fail_project_graph_job",
            "complete_project_graph_job", "complete_project_solve_job", "claim_next",
            "heartbeat", "complete_solve", "fail_or_retry", "record_worker_heartbeat",
        ):
            monkeypatch.setattr(routes.canonical_jobs, name, forbidden)

    class Cursor:
        def __init__(self):
            self.closed = False
            self.commands = []
            self.rows = []
            state.cursors.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def execute(self, sql, args=None):
            state.calls.append((sql, deepcopy(args)))
            self.commands.append(sql)
            if re.search(r"\b(UPDATE|INSERT|DELETE|CREATE|ALTER|DROP|FOR\s+UPDATE)\b", sql, re.I):
                state.mutations.append(sql)
                raise AssertionError("write or lock in monitor query")
            position = len(self.commands) - 1
            if position < len(controls):
                assert sql == controls[position] and args is None
                if position == 3 and state.failure == "clock":
                    raise ConnectionError(_MONITOR_PRIVATE)
                return
            assert position == 4 and sql == submitted_monitor.SELECTION_SQL
            if state.failure in {"select", "timeout"}:
                raise TimeoutError(_MONITOR_PRIVATE)
            # No replacement of predicates, arithmetic, operators, or SQL structure.
            adapted = re.sub(r"%\((\w+)\)s", r":\1", sql)
            bound = {key: _monitor_iso(value) if isinstance(value, datetime) else value
                     for key, value in args.items()}
            selected = db.execute(adapted, bound).fetchall()
            self.rows = [(job, tool, datetime.fromisoformat(created))
                         for job, tool, created in selected]
            if state.corrupt and self.rows:
                row = list(self.rows[-1])
                index, value = state.corrupt
                row[index] = value
                self.rows[-1] = tuple(row)
            if state.row_override is not None:
                self.rows = state.row_override

        def fetchone(self):
            assert self.commands == controls
            return (state.now,)

        def fetchmany(self, size):
            state.fetch_sizes.append(size)
            assert len(self.rows) <= size
            if state.failure == "fetch":
                def partial_decode():
                    for row in self.rows[:3]:
                        yield row
                    raise ValueError(_MONITOR_PRIVATE)
                return partial_decode()
            return self.rows

    class Connection:
        def __init__(self):
            self.closed = False
            state.connections.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True
            if state.failure == "transaction_exit":
                raise ConnectionError(_MONITOR_PRIVATE)

        def cursor(self):
            if state.failure == "cursor":
                raise ConnectionError(_MONITOR_PRIVATE)
            return Cursor()

    def connect(url, **kwargs):
        state.connects.append((url, kwargs))
        assert url == "postgresql://" + _MONITOR_PRIVATE
        assert kwargs == {"connect_timeout": 2, "prepare_threshold": None}
        if state.failure == "connect":
            raise ConnectionError(_MONITOR_PRIVATE)
        return Connection()

    def job(*, age=301, **changes):
        state.serial += 1
        row = {
            "job_id": str(UUID(int=state.serial)), "tool_name": "solar-settings",
            "created_at": _monitor_iso(state.now - timedelta(seconds=age)),
            "updated_at": _monitor_iso(state.now), "deleted_at": None, "kind": "run",
            "execution_context": json.dumps({"schema": "leaf.solar-project-graph-job.v1",
                                              "private": _MONITOR_PRIVATE}),
            "request_tenant_id": _MONITOR_PRIVATE, "org_id": "org", "project_id": "project",
            "status": "queued", "attempt": 0, "max_attempts": 3,
            "params": _MONITOR_PRIVATE, "result": _MONITOR_PRIVATE, "error": _MONITOR_PRIVATE,
            "lease_owner": _MONITOR_PRIVATE, "lease_expires_at": _monitor_iso(state.now),
        }
        row.update(changes)
        keys = ", ".join(row)
        db.execute(f"INSERT INTO jobs ({keys}) VALUES ({', '.join('?' for _ in row)})",
                   tuple(row.values()))
        return row

    def worker(tool="solar-settings", *, age=0, worker_id=None):
        worker_id = worker_id or str(db.execute(
            "SELECT count(*) FROM canonical_worker_heartbeats").fetchone()[0])
        db.execute("INSERT INTO canonical_worker_heartbeats VALUES (?, ?, ?, ?, ?, ?)",
                   (worker_id, tool, _MONITOR_PRIVATE, "sha256:" + "a" * 64,
                    "a" * 64, _monitor_iso(state.now - timedelta(seconds=age))))

    def snapshot():
        return deepcopy({table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                         for table in _MONITOR_TABLES})

    def scan(argv=None, *, environ=None, stdout=None):
        guard_helpers()
        before = snapshot()
        out, err = StringIO() if stdout is None else stdout, StringIO()

        class RuntimeEnv(dict):
            def get(self, key, default=None):
                assert key in {"LEAF_CANONICAL_WORKER_REQUIRED", "DATABASE_URL"}
                return super().get(key, default)

        class NoHostEnvironment(dict):
            def get(self, *args):
                raise AssertionError("monitor accessed host credentials")

        original_import = builtins.__import__
        forbidden_imports = []

        def offline_import(name, *args, **kwargs):
            if name.split(".")[0] in {"psycopg", "dotenv", "leaf_platform", "db"}:
                forbidden_imports.append(name)
                raise AssertionError("monitor bypassed its injected driver")
            return original_import(name, *args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(builtins, "__import__", offline_import)
            context.setattr(submitted_monitor, "os", SimpleNamespace(environ=NoHostEnvironment()))
            code = submitted_monitor.main(
                ["--once"] if argv is None else argv,
                environ=RuntimeEnv({"LEAF_CANONICAL_WORKER_REQUIRED": " YeS ",
                                    "DATABASE_URL": "postgresql://" + _MONITOR_PRIVATE,
                                    "LEAF_EXPECTED_WORKER_REVISION": "760d3888018f762e0ab8dbe289dae8a2871216ea"})
                if environ is None else environ, connect=connect, stdout=out, stderr=err)
        assert not forbidden_imports
        assert snapshot() == before
        assert not state.mutations and not state.helper_calls
        assert all(resource.closed for resource in state.connections + state.cursors)
        text = out.getvalue()
        assert _MONITOR_PRIVATE not in text + err.getvalue()
        diagnostics, summaries = [], []
        for line in ([] if argv == ["--help"] else text.splitlines()):
            if line.startswith(submitted_monitor.DIAGNOSTIC_PREFIX):
                diagnostics.append(json.loads(line[len(submitted_monitor.DIAGNOSTIC_PREFIX):]))
            else:
                assert line.startswith(submitted_monitor.SUMMARY_PREFIX)
                summaries.append(json.loads(line[len(submitted_monitor.SUMMARY_PREFIX):]))
        return SimpleNamespace(code=code, diagnostics=diagnostics, summaries=summaries,
                               out=text, err=err.getvalue())

    db.execute("INSERT INTO live_projects VALUES ('org', 'project')")
    db.execute("INSERT INTO orgs VALUES ('org', 'active')")
    db.execute("INSERT INTO tenant_authority_modes VALUES ('org', 'postgres_canonical')")
    state.job, state.worker, state.scan = job, worker, scan
    state.snapshot, state.connect, state.controls = snapshot, connect, controls
    try:
        yield state
    finally:
        db.close()


def _monitor_summary(result, state, *, count=0, truncated=False, observed=True):
    assert len(result.summaries) == 1
    summary = result.summaries[0]
    assert set(summary) == {"schema", "state", "observed_at", "diagnostic_count", "truncated"}
    assert summary["schema"] == "leaf.solar-submitted-monitor.v1"
    assert summary["state"] == state
    assert summary["diagnostic_count"] == count
    assert summary["truncated"] is truncated
    assert (summary["observed_at"] is not None) is observed
    return summary


def test_submitted_monitor_reports_loss_after_admission(lane, coverage, monitor_store):
    heartbeat(coverage)
    response = routes.run(lane)
    assert response.status_code == 202
    assert len(lane.records) == len(lane.a.accepted) == 1
    assert lane.a.events.count("insert") == 1
    admission_before = deepcopy(lane.records)
    # Accepted-call kwargs retain the fixture connection, whose identity matters.
    accepted_before, events_before = list(lane.a.accepted), list(lane.a.events)
    store = monitor_store
    admitted = store.job(age=0, job_id=str(lane.s.job), org_id=str(lane.s.org),
                         project_id=str(lane.s.project), request_tenant_id=str(lane.s.org))
    store.db.execute("INSERT INTO live_projects VALUES (?, ?)",
                     (str(lane.s.org), str(lane.s.project)))
    store.db.execute("INSERT INTO orgs VALUES (?, 'active')", (str(lane.s.org),))
    store.db.execute("INSERT INTO tenant_authority_modes VALUES (?, 'postgres_canonical')",
                     (str(lane.s.org),))
    store.worker()
    first_before = store.snapshot()
    first = store.scan()
    assert first.code == 0 and first.diagnostics == []
    _monitor_summary(first, "clear")
    assert store.snapshot() == first_before

    store.db.execute("DELETE FROM canonical_worker_heartbeats")
    store.now += timedelta(seconds=301)
    before = store.snapshot()
    result = store.scan()
    assert result.code == 1 and len(result.diagnostics) == 1
    item = result.diagnostics[0]
    assert (item["job_id"], item["tool_name"], item["age_seconds"]) == (
        admitted["job_id"], "solar-settings", 301)
    _monitor_summary(result, "uncovered", count=1)
    store.now += timedelta(seconds=1)
    repeated = store.scan()
    assert repeated.code == 1 and len(repeated.diagnostics) == 1
    assert repeated.diagnostics[0]["age_seconds"] == 302
    assert repeated.diagnostics[0]["observed_at"] != item["observed_at"]
    assert store.snapshot() == before
    assert lane.records == admission_before
    assert lane.a.accepted == accepted_before and lane.a.events == events_before


def test_submitted_monitor_age_boundary_and_retry_age(monitor_store):
    store = monitor_store
    young = store.job(age=299.999)
    boundary = store.job(age=300)
    old = store.job(age=301.999)
    future = store.job(age=-10)
    retry = store.job(age=601.9, attempt=1, updated_at=_monitor_iso(store.now))
    excluded = [young, future]
    for status in ("running", "failed", "succeeded", "cancelled"):
        excluded.append(store.job(age=1000, status=status,
                                  lease_expires_at=_monitor_iso(store.now - timedelta(seconds=30))))
    excluded.append(store.job(age=1000, attempt=3))
    result = store.scan()
    assert result.code == 1
    assert [(row["job_id"], row["age_seconds"]) for row in result.diagnostics] == [
        (retry["job_id"], 601), (old["job_id"], 301), (boundary["job_id"], 300)]
    assert not {row["job_id"] for row in result.diagnostics} & {row["job_id"] for row in excluded}


@pytest.mark.parametrize("ages,covered", [
    ([], False), ([31], False), ([-1], False), ([30], True), ([30.000001], False),
    ([0], True), ([-1, 0], True), ([31, 0], True), ([31, -1], False),
])
def test_submitted_monitor_exact_tool_and_any_fresh_worker(monitor_store, ages, covered):
    store = monitor_store
    first = store.job()
    other = store.job(tool_name="solar-panels-from-drawing")
    store.worker("solar-panels-from-drawing")
    store.worker("solar-panels-from-drawing", age=-1)
    # Prefix and unrelated readiness-tool heartbeats never cover solar-settings.
    store.worker("solar-settings-extra")
    store.worker("string-autofill-opt")
    for age in ages:
        store.worker(age=age)
    result = store.scan()
    assert result.code == (0 if covered else 1)
    assert [item["job_id"] for item in result.diagnostics] == ([] if covered else [first["job_id"]])
    assert other["job_id"] not in {item["job_id"] for item in result.diagnostics}
    # Every fixture revision uses the real writer's sha256 content identity, while
    # the injected expected revision is a deliberately different git SHA.


def test_submitted_monitor_population_is_canonical(monitor_store):
    store = monitor_store
    fallback = store.job()
    for changes in (
        {"kind": "other"}, {"execution_context": '{"schema":"wrong"}'},
        {"execution_context": "{}"}, {"execution_context": None},
        {"tool_name": "solar-settings-extra"}, {"tool_name": "string-autofill-opt"},
        {"request_tenant_id": None}, {"deleted_at": _monitor_iso(store.now)},
        {"project_id": "not-live"}, {"org_id": "inactive"}, {"org_id": "missing-org"},
        {"org_id": "legacy"}, {"org_id": "default-legacy"},
        {"project_id": "project-legacy"},
    ):
        store.job(**changes)
    for org, status in (("inactive", "inactive"), ("legacy", "active"),
                        ("default-legacy", "active"), ("project-override", "active")):
        store.db.execute("INSERT INTO orgs VALUES (?, ?)", (org, status))
        store.db.execute("INSERT INTO live_projects VALUES (?, 'project')", (org,))
    store.db.execute("INSERT INTO live_projects VALUES ('missing-org', 'project')")
    store.db.execute("INSERT INTO tenant_authority_modes VALUES ('inactive', 'postgres_canonical')")
    store.db.execute("INSERT INTO tenant_authority_modes VALUES ('missing-org', 'postgres_canonical')")
    store.db.execute("INSERT INTO tenant_authority_modes VALUES ('legacy', 'legacy_sqlite')")
    store.db.execute("INSERT INTO tenant_authority_modes VALUES ('project-override', 'legacy_sqlite')")
    store.db.execute("INSERT INTO live_projects VALUES ('org', 'project-legacy')")
    store.db.execute("INSERT INTO live_project_authority_modes VALUES ('org', 'project-legacy', 'legacy_sqlite')")
    store.db.execute("INSERT INTO live_project_authority_modes VALUES ('project-override', 'project', 'postgres_canonical')")
    override = store.job(org_id="project-override")
    store.db.execute("INSERT INTO live_projects VALUES ('org', 'project-canonical')")
    store.db.execute("INSERT INTO live_project_authority_modes VALUES ('org', 'project-canonical', 'postgres_canonical')")
    explicit = store.job(project_id="project-canonical")
    result = store.scan()
    assert result.code == 1
    assert [row["job_id"] for row in result.diagnostics] == [
        fallback["job_id"], override["job_id"], explicit["job_id"]]


def test_submitted_monitor_reader_is_bounded_and_read_only(monitor_store):
    store = monitor_store
    store.job()
    result = store.scan(["--once", "--min-age-seconds", "301",
                         "--max-heartbeat-age-seconds", "25", "--limit", "7"])
    assert result.code == 1
    assert len(store.connects) == len(store.connections) == len(store.cursors) == 1
    assert store.connects[0][1] == {"connect_timeout": 2, "prepare_threshold": None}
    assert [sql for sql, _ in store.calls] == store.controls + [submitted_monitor.SELECTION_SQL]
    args = store.calls[-1][1]
    assert args["schema"] == "leaf.solar-project-graph-job.v1"
    assert {value for key, value in args.items() if key.startswith("tool_")} == submitted_monitor.SUPPORTED_TOOLS
    assert args["age_cutoff"] == store.now - timedelta(seconds=301)
    assert args["heartbeat_cutoff"] == store.now - timedelta(seconds=25)
    assert args["scan_time"] == store.now and args["row_limit"] == 8
    assert store.fetch_sizes == [8]
    store.failure = "timeout"
    failed = store.scan()
    assert failed.code == 2 and not failed.diagnostics
    _monitor_summary(failed, "unavailable", observed=False)
    assert len(store.connects) == 2  # No retry.


def test_submitted_monitor_qualifies_before_limit(monitor_store):
    store = monitor_store
    empty = store.scan(["--once", "--limit", "3"])
    assert empty.code == 0 and empty.diagnostics == []
    _monitor_summary(empty, "clear")
    for index in range(30):
        store.job(age=5000 + index, tool_name="solar-panels-from-drawing")
    store.worker("solar-panels-from-drawing", age=0)
    store.worker("solar-panels-from-drawing", age=1)
    store.worker("solar-panels-from-drawing", age=-1)
    # Creation order differs from chronological order, including a job-id tie break.
    newest = store.job(age=301)
    oldest = store.job(age=400)
    tied = store.job(age=400)
    full = store.scan(["--once", "--limit", "3"])
    assert full.code == 1
    assert [item["job_id"] for item in full.diagnostics] == [
        oldest["job_id"], tied["job_id"], newest["job_id"]]
    _monitor_summary(full, "uncovered", count=3)
    store.job(age=300)
    truncated = store.scan(["--once", "--limit", "3"])
    assert truncated.code == 3 and truncated.diagnostics == full.diagnostics
    _monitor_summary(truncated, "truncated", count=3, truncated=True)
    assert store.fetch_sizes == [4, 4, 4]


@pytest.mark.parametrize("failure", [
    "connect", "cursor", "clock", "select", "timeout", "fetch", "transaction_exit",
    "uuid", "uuid-type", "tool", "timestamp", "naive", "clock-timestamp", "clock-naive",
    "future-row", "duplicate",
])
def test_submitted_monitor_unavailable_is_not_uncovered(monitor_store, failure):
    store = monitor_store
    for age in (600, 500, 400, 301):
        store.job(age=age)
    if failure in {"uuid", "uuid-type", "tool", "timestamp", "naive", "future-row"}:
        store.corrupt = {
            "uuid": (0, _MONITOR_PRIVATE), "uuid-type": (0, None),
            "tool": (1, _MONITOR_PRIVATE), "timestamp": (2, _MONITOR_PRIVATE),
            "naive": (2, store.now.replace(tzinfo=None)),
            "future-row": (2, store.now + timedelta(seconds=1)),
        }[failure]
    elif failure == "clock-timestamp":
        store.now = _MONITOR_PRIVATE
    elif failure == "clock-naive":
        store.now = store.now.replace(tzinfo=None)
    elif failure == "duplicate":
        row = (str(UUID(int=1234)), "solar-settings", store.now - timedelta(seconds=400))
        store.row_override = [row, row]
    else:
        store.failure = failure
    result = store.scan()
    assert result.code == 2 and result.diagnostics == []
    _monitor_summary(result, "unavailable", observed=False)
    assert "clear" not in result.out and "uncovered" not in result.out
    assert result.err == ""
    assert len(store.connects) == 1


@pytest.mark.parametrize("argv", [
    [], ["--help"], ["--once", "--surprise", _MONITOR_PRIVATE],
    *[["--once", "--min-age-seconds=" + value] for value in (
        "0", "-1", "nan", "NaN", "inf", "-Infinity", "86401", "0.999", _MONITOR_PRIVATE)],
    *[["--once", "--max-heartbeat-age-seconds=" + value] for value in (
        "0", "-1", "nan", "Infinity", "301", "0.999", _MONITOR_PRIVATE)],
    *[["--once", "--limit=" + value] for value in (
        "0", "-1", "nan", "Infinity", "1.5", "5001", _MONITOR_PRIVATE)],
])
def test_submitted_monitor_requires_explicit_invocation(monitor_store, monkeypatch, argv):
    store = monitor_store
    environment_reads = []

    class NoEnvironment(dict):
        def get(self, key, default=None):
            environment_reads.append(key)
            raise AssertionError("argument handling must not read runtime configuration")

    # Re-execute the real module import while database/bootstrap imports are forbidden.
    original_import = builtins.__import__
    forbidden_imports = []

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"psycopg", "dotenv", "leaf_platform", "db"}:
            forbidden_imports.append(name)
            raise AssertionError("import attempted runtime configuration access")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", guarded_import)
        spec = importlib.util.spec_from_file_location("_submitted_monitor_import", submitted_monitor.__file__)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        result = store.scan(argv, environ=NoEnvironment())
    assert not forbidden_imports
    assert not environment_reads
    assert result.code == (0 if argv == ["--help"] else 2)
    assert not store.connects and not result.diagnostics and not result.summaries
    if argv != ["--help"]:
        assert "usage:" in result.err and len(result.err) < 1000


@pytest.mark.parametrize("required", [None, "", "0", "false", "no", "off"])
def test_submitted_monitor_disabled_without_configuration(monitor_store, required):
    class DisabledEnv(dict):
        def get(self, key, default=None):
            assert key == "LEAF_CANONICAL_WORKER_REQUIRED"
            return super().get(key, default)

    env = DisabledEnv() if required is None else DisabledEnv(LEAF_CANONICAL_WORKER_REQUIRED=required)
    result = monitor_store.scan(environ=env)
    assert result.code == 0 and not monitor_store.connects and not result.diagnostics
    _monitor_summary(result, "disabled", observed=False)


@pytest.mark.parametrize("required", ["1", "true", " YES ", "On"])
@pytest.mark.parametrize("url", [None, "", "  ", "invalid-runtime-configuration"])
def test_submitted_monitor_missing_or_invalid_runtime_configuration(monitor_store, required, url):
    class RuntimeEnv(dict):
        def get(self, key, default=None):
            assert key in {"LEAF_CANONICAL_WORKER_REQUIRED", "DATABASE_URL"}
            return super().get(key, default)

    env = RuntimeEnv(LEAF_CANONICAL_WORKER_REQUIRED=required)
    if url is not None:
        env["DATABASE_URL"] = url
    result = monitor_store.scan(environ=env)
    assert result.code == 2 and not result.diagnostics
    _monitor_summary(result, "unavailable", observed=False)
    assert len(monitor_store.connects) == (1 if url == "invalid-runtime-configuration" else 0)


def test_submitted_monitor_allowlisted_output_and_sink_failure(monitor_store):
    store = monitor_store
    # A non-UTC database clock still encodes all receipts in UTC.
    store.now = store.now.astimezone(timezone(timedelta(hours=5, minutes=30)))
    store.job(age=301.999)
    result = store.scan()
    assert result.code == 1
    item, = result.diagnostics
    assert set(item) == {"schema", "reason", "job_id", "tool_name", "age_seconds", "observed_at"}
    assert item["schema"] == "leaf.solar-submitted-job-diagnostic.v1"
    assert item["reason"] == "no_live_covering_worker"
    assert item["job_id"] == str(UUID(item["job_id"]))
    assert item["tool_name"] == "solar-settings"
    assert type(item["age_seconds"]) is int and item["age_seconds"] == 301
    assert item["observed_at"] == "2026-10-26T12:00:00Z"
    summary = _monitor_summary(result, "uncovered", count=1)
    assert summary["observed_at"] == item["observed_at"]

    class BrokenOutput(StringIO):
        def write(self, text):
            raise OSError(_MONITOR_PRIVATE)

    failed = store.scan(stdout=BrokenOutput())
    assert failed.code == 2 and not failed.summaries and not failed.diagnostics

    class ShortOutput(StringIO):
        def write(self, text):
            return 0

    short = store.scan(stdout=ShortOutput())
    assert short.code == 2 and not short.summaries and not short.diagnostics

    class BreakAfterDiagnostic(StringIO):
        def write(self, text):
            if text.startswith(submitted_monitor.SUMMARY_PREFIX):
                raise OSError(_MONITOR_PRIVATE)
            return super().write(text)

    failed_late = store.scan(stdout=BreakAfterDiagnostic())
    assert failed_late.code == 2 and len(failed_late.diagnostics) == 1
    assert not failed_late.summaries

    class BrokenFlush(StringIO):
        def flush(self):
            raise OSError(_MONITOR_PRIVATE)

    assert store.scan(stdout=BrokenFlush()).code == 2
