"""Exactly sixteen offline P-080 scenarios; every operational boundary is fake."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import json
import base64
import socket
import sys

import pytest

import probe_staging_async_job as p


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/p080_async_probe.json").read_bytes())
SHIPPED = next(t for t in json.loads((ROOT / "engine/registry.json").read_bytes())["tools"]
               if t["name"] == p.TOOL)
TENANT = "bbbbbbbb-2222-4333-8444-555555555555"
JOB = "cccccccc-2222-4333-8444-555555555555"
SOURCE = "a" * 40
DATABASE = "postgresql://SENTINEL:DB_SECRET@never-connect/staging"
SUBJECT = "auth0|protected-b-sentinel"
JWT = ".".join([
    base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "sentinel"}).encode()).decode().rstrip("="),
    base64.urlsafe_b64encode(json.dumps({"sub": SUBJECT, "exp": 10000}).encode()).decode().rstrip("="),
    "SENTINEL_SIGNATURE",
])
LIVE_HTTP = p.LiveAdapters.http
LIVE_CONNECT = p.LiveAdapters.connect
ENV = {"DATABASE_URL": DATABASE, "LEAF_RUNTIME_ENV": "staging",
       "LEAF_JOBS_STORE": "postgres", "LEAF_BROKER_STORE": "postgres",
       "LEAF_DRAWING_STORE": "postgres", "LEAF_BLOB_STORE": "filesystem",
       "LEAF_STORE_DIR": "/data/drawings"}


def args(**changes):
    values = dict(command="run", base_url=p.ORIGIN, expected_runtime_source=SOURCE,
                  run_id="offline-1", identity_receipt="identity.json",
                  out_dir="/evidence", ack=p.ACK, receipt_dir="/receipt")
    values.update(changes)
    return SimpleNamespace(**values)


class MemoryFS:
    """No temporary or staging files: even cleanup's Path operations stay here."""

    def __init__(self):
        self.files = {}
        self.dirs = set()
        self.links = set()
        self.escapes = set()
        self.removed = []

    def path(self, value):
        return MemoryPath(self, PurePosixPath(str(value)))

    def put(self, path, data):
        self.files[str(path)] = data
        self.dirs.update(str(parent) for parent in PurePosixPath(str(path)).parents)


class MemoryPath:
    def __init__(self, fs, value):
        self.fs, self.value = fs, value

    def __str__(self):
        return str(self.value)

    def __hash__(self):
        return hash((id(self.fs), self.value))

    def __eq__(self, other):
        return isinstance(other, MemoryPath) and self.fs is other.fs and self.value == other.value

    def __truediv__(self, value):
        return self.fs.path(self.value / str(value))

    @property
    def parents(self):
        return [self.fs.path(v) for v in self.value.parents]

    @property
    def parent(self):
        return self.fs.path(self.value.parent)

    def resolve(self, **kwargs):
        return self.fs.path("/escape") if str(self) in self.fs.escapes else self

    def is_relative_to(self, other):
        return self.value.is_relative_to(other.value)

    def relative_to(self, other):
        return self.value.relative_to(str(other))

    def is_symlink(self):
        return str(self) in self.fs.links

    def exists(self):
        return str(self) in self.fs.files or str(self) in self.fs.dirs

    def is_file(self):
        return str(self) in self.fs.files

    def read_bytes(self):
        return self.fs.files[str(self)]

    def rglob(self, pattern):
        assert pattern == "*"
        return [self.fs.path(v) for v in self.fs.files.keys() | self.fs.dirs
                if v.startswith(str(self) + "/")]

    def unlink(self):
        assert str(self) not in self.fs.links
        self.fs.removed.append(str(self))
        del self.fs.files[str(self)]

    def rmdir(self):
        assert not self.rglob("*")
        self.fs.dirs.remove(str(self))
        self.fs.removed.append(str(self))


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return self


class Fake(p.LiveAdapters):
    """Exercise real orchestration, fixture validation and deletion over memory."""

    def __init__(self, monkeypatch, scenario="happy"):
        super().__init__(deepcopy(ENV))
        self.fs = MemoryFS()
        monkeypatch.setattr(p, "Path", self.fs.path)
        self.directory = self.fs.path("/receipt")
        self.clock = 1000.0
        self.states = deepcopy(FIXTURE["scenarios"][scenario])
        self.current = []
        self.secret_reads, self.ingests, self.posts, self.closes = [], [], [], []
        self.saved = []
        self.reserved = False
        self.lost = scenario in {"lost_ack", "unknown"}
        self.redirect = False
        self.binding_active = True
        self.entitled = self.broker_allowed = True
        self.foreign = self.active_admission = False
        self.duplicate = False
        self.row_changes = {}
        self.fetch_changes = {}
        self.cleanup_waits = 0
        self.close_at = None
        self.execution_allowed = True
        self.admission_live = self.ledger_live = False
        self.admission_status, self.ledger_status = 200, "ok"
        self.m, self.v = [], []
        self.audit = {"job": JOB, "admission": JOB + ":broker-run", "ledger": "immutable"}
        self.unrelated = {"customer-row": "unchanged", "customer-file": b"unchanged"}
        self.runtime_data = {"environment": "staging", "source": SOURCE,
                             "verified_at": 999, "database_sha256": p.sha(DATABASE)}
        self.identity_data = {"principal": "Leaf staging acceptance tenant B (protected, 20260728)",
                              "tenant_sha256": p.sha(TENANT), "subject_sha256": p.sha(SUBJECT),
                              "synthetic_provisioning_verified": True,
                              "runtime_source": SOURCE, "tool_sha256": p.sha(p.canonical(SHIPPED))}
        self.secret_data = {"tenant_id": TENANT, "subject": SUBJECT, "jwt": JWT,
                            "exp": 10000, "secret_value": "SENTINEL_SECRET_VALUE"}
        digest = "sha256:" + p.sha(p.canonical(SHIPPED))
        self.tools = [{**deepcopy(SHIPPED), "catalog_digest": digest, "tool_manifest_sha256": digest}]
        self.observer_started = False

    def now(self):
        return self.clock

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds
        if self.closes:
            self.cleanup_waits += 1
            assert self.fs.files  # Geometry still present while execution is active.
            if self.cleanup_waits == 2:
                self.current = [self.row("failed")]

    def new_uuid(self):
        return "d" * 32

    def reserve(self, out_dir, run_id):
        p.require(not self.reserved, "run_exists")
        self.reserved = True

    def read_json(self, path):
        assert path == "identity.json"
        return deepcopy(self.identity_data)

    def runtime(self, source, identity):
        p.validate_runtime_receipt(self.runtime_data, source, self.env, self.now())
        p.require(identity["runtime_source"] == source, "runtime_refused")

    def check_origin(self):
        p.require(not self.redirect, "redirect_refused")

    def secret(self, identifier):
        self.secret_reads.append(identifier)
        assert identifier == p.SECRET_ID
        return deepcopy(self.secret_data)

    def binding(self, tenant, subject):
        assert (tenant, subject) == (self.secret_data["tenant_id"], self.secret_data["subject"])
        return self.binding_active

    def broker_entitled(self, tenant):
        assert tenant == TENANT
        return self.broker_allowed

    def save(self, receipt):
        p.validate_receipt(receipt)
        self.last_saved = receipt
        self.saved.append(deepcopy(receipt))

    def http(self, method, path, token, body=None, correlation=None):
        assert token == JWT
        if self.redirect:
            raise p.Refusal("redirect_refused")
        if path == "/api/session/identity":
            return 200, {"tenant_id": self.secret_data["tenant_id"], **self.fetch_identity}
        if path == "/api/entitlements":
            return 200, {"entitlements": {"run_read": self.entitled}}
        if path == "/api/tools":
            return 200, {"tools": self.tools}
        if path == "/api/run":
            assert method == "POST" and self.observer_started
            assert self.last_saved["submission_started"] is True
            self.posts.append((deepcopy(body), correlation))
            if self.lost:
                raise OSError("SENTINEL_RAW_EXCEPTION https://signed.example/?signature=SECRET")
            return 202, {"job_id": JOB, "status": "submitted", "raw": JWT}
        if path.endswith("/close"):
            assert method == "POST" and path == "/api/jobs/" + JOB + "/close"
            assert self.last_saved["close_started"] is True
            self.closes.append(JOB)
            return 200, {"closed": True}
        assert method == "GET" and path == "/api/jobs/" + JOB
        row = deepcopy(self.current[0])
        row["result"] = row.pop("result_json")
        row.update(self.fetch_changes)
        row["ignored_arbitrary_field"] = JWT
        return 200, row

    fetch_identity = {}

    def absent(self, tenant, drawing):
        return not self.m and not self.v and not self.fs.files

    def seed(self):
        drawing = "p080-r3-" + "d" * 32
        prefix, blob = self.paths(TENANT, drawing)
        self.fs.put(blob, p.FIXTURE_BYTES)
        self.fs.put(prefix / "manifest.json", b'{"authority":"postgres"}')
        self.fs.put(self.directory / "fixture.json", p.FIXTURE_BYTES)
        self.m = [{"head": 1, "latest": 1, "checkout_holder": None, "checkout_expires_at": None}]
        self.v = [{"version": 1, "state": "ready", "content_sha256": p.FIXTURE_SHA,
                   "byte_count": len(p.FIXTURE_BYTES),
                   "object_key": str(blob.relative_to("/data/drawings"))}]

    def ingest(self, tenant, drawing, data):
        assert self.last_saved["drawing"] == drawing
        assert self.last_saved["fixture_sha256"] == p.sha(data)
        assert data == p.canonical(FIXTURE["intake"])
        self.ingests.append((tenant, drawing, data))
        self.seed()

    def fixture_rows(self, tenant, drawing, conn=None):
        assert tenant == TENANT and drawing == "p080-r3-" + "d" * 32
        return self.m, self.v

    def row(self, status):
        row = {"job_id": JOB, "tenant_id": TENANT, "tool": p.TOOL,
               "dwg": "p080-r3-" + "d" * 32, "dwg_version": 1,
               "status": status, "progress": "queued" if status == "submitted" else status,
               "attempt": 0 if status == "submitted" else 1,
               "started_at": None if status == "submitted" else 1001,
               "finished_at": 1002 if status in {"complete", "failed"} else None,
               "lease_owner": "SENTINEL_LEASE" if status == "running" else None,
               "lease_expires_at": 1100 if status == "running" else None,
               "heartbeat_at": 1001 if status == "running" else None,
               "terminal_fingerprint": "SENTINEL_FINGERPRINT" if status == "complete" else None,
               "result_json": {"ok": True, "result": deepcopy(FIXTURE["expected"]),
                               "execution_provenance": {"attempt": 1, "execution_path": "local"}},
               "provenance_json": {"attempt": 1, "execution_path": "local"},
               "org_id": None, "project_id": None, "idempotency_key": "p080-r3:offline-1"}
        row.update(deepcopy(self.row_changes))
        return row

    def start_observer(self, *arguments):
        assert not self.posts
        self.observer_started = True

    def stop_observer(self):
        pass

    def jobs(self, tenant, drawing, correlation):
        assert tenant == TENANT and correlation == "p080-r3:offline-1"
        if self.states:
            self.current = [self.row(self.states.pop(0))]
            if self.duplicate:
                self.current.append({**self.current[0], "job_id": "e" * 8 + JOB[8:]})
        return deepcopy(self.current)

    def read_jobs(self, *arguments):
        return deepcopy(self.current)

    def execution_valid(self, tenant, job_id):
        assert tenant == TENANT and job_id == JOB
        return self.execution_allowed and super().execution_valid(tenant, job_id)

    def quiescent(self, tenant, job_id):
        assert tenant == TENANT and job_id == JOB
        return (len(self.current) == 1 and self.current[0]["status"] in {"complete", "failed"}
                and not self.active_admission)

    def connect(self):
        return FakeConnection()

    def query(self, sql, params=(), connection=None):
        if "FROM broker_run_admissions" in sql:
            assert params == (TENANT, JOB + ":broker-run")
            return [{"state": "terminal", "aps_live": self.admission_live,
                     "terminal_at": 1002, "reserved_usd": 0, "http_status": self.admission_status,
                     "result_json": {"ok": True, "result": p.EXPECTED}}]
        if "FROM broker_usage_ledger" in sql:
            assert params == (JOB + ":broker-run",)
            return [{"tenant_id": TENANT, "job_id": JOB, "aps_live": self.ledger_live,
                     "tool": p.TOOL, "engine_op": "measure_panel_area", "status": self.ledger_status}]
        if "FROM jobs WHERE spine_ref" in sql or "FROM aps_completion_receipts" in sql:
            assert params == (JOB,)
            return []
        assert (TENANT, "p080-r3-" + "d" * 32) == params
        if sql.startswith("DELETE FROM drawing_store_manifests"):
            assert not self.active_admission and not self.foreign
            assert self.last_saved["removal_started"] is True
            assert not self.paths(*params)[1].exists()
            self.m, self.v = [], []
            return [{"drawing_id": params[1]}]
        assert sql.startswith("SELECT head FROM drawing_store_manifests")
        return deepcopy(self.m)

    def foreign_references(self, *arguments):
        return self.foreign

    def _sync_dir(self, path):
        pass

    def open_receipt(self, directory):
        assert directory == "/receipt"

    def read_receipt(self):
        return deepcopy(self.saved[-1])

    def cleanup_identity_receipt(self):
        return deepcopy(self.identity_data)


@pytest.fixture(autouse=True)
def no_live_boundaries(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("real transport forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    # An accidental construction of a real operations adapter fails before it
    # could read a receipt, load AWS/DB libraries, or touch the drawings mount.
    monkeypatch.setattr(p.LiveAdapters, "runtime", forbidden)
    monkeypatch.setattr(p.LiveAdapters, "connect", forbidden)
    monkeypatch.setattr(p.LiveAdapters, "secret", forbidden)
    monkeypatch.setattr(p.LiveAdapters, "http", forbidden)
    monkeypatch.setattr(p.LiveAdapters, "ingest", forbidden)
    monkeypatch.setattr(time_module(), "sleep", forbidden)


def time_module():
    import time
    return time


def run(fake):
    return p.Probe(fake).run(args(), fake.env)


def test_live_connect_sets_session_timeouts_without_startup_options(monkeypatch):
    calls = []
    connection = SimpleNamespace(execute=lambda sql: calls.append(("execute", sql)))
    dict_row = object()

    def connect(dsn, **kwargs):
        calls.append(("connect", dsn, kwargs))
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setitem(sys.modules, "psycopg.rows", SimpleNamespace(dict_row=dict_row))

    result = LIVE_CONNECT(p.LiveAdapters(ENV))
    calls.append(("returned", result))
    assert result is connection
    assert "options" not in calls[0][2]
    assert calls == [
        ("connect", DATABASE, {"autocommit": True, "connect_timeout": 5,
                               "row_factory": dict_row}),
        ("execute", "SELECT set_config('statement_timeout', '5000', false), "
                    "set_config('lock_timeout', '3000', false)"),
        ("returned", connection),
    ]


def test_live_broker_entitled_matches_broker_tier_defaults(monkeypatch):
    import entitlements

    adapter = p.LiveAdapters(ENV)
    assert entitlements.entitlements_for("guest").get("run_read") is not True
    for rows, expected in [
        ([], True),
        ([{"tier": None, "disabled": False}], True),
        ([{"tier": None, "disabled": True}], False),
        ([{"tier": None, "disabled": False}] * 2, False),
        ([{"tier": "guest", "disabled": False}], False),
    ]:
        def query(sql, params):
            assert sql == "SELECT tier, disabled FROM broker_tenants WHERE tenant_id=%s"
            assert params == (TENANT,)
            return rows

        monkeypatch.setattr(adapter, "query", query)
        assert adapter.broker_entitled(TENANT) is expected


def test_fixture_geometry_has_independent_expected_area():
    assert set(FIXTURE["intake"]) == {"polylines"}
    polygons = FIXTURE["intake"]["polylines"]
    assert polygons == [
        {"layer": "p080_r3", "closed": True, "pts": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        {"layer": "p080_r3", "closed": True, "pts": [[20, 0], [40, 0], [40, 10], [20, 10]]}]
    areas = []
    for polygon in polygons:
        points = polygon["pts"]
        areas.append(abs(sum(a[0] * b[1] - b[0] * a[1]
                             for a, b in zip(points, points[1:] + points[:1]))) / 2)
    assert areas == [100, 200]
    assert FIXTURE["expected"] == {"layer": "p080_r3", "count": 2, "total_area": 300.0,
                                   "avg_area": 150.0, "units": "drawing"}
    assert p.INTAKE == FIXTURE["intake"] and p.EXPECTED == FIXTURE["expected"]


def test_preflight_requires_exact_staging_origin_and_ack(monkeypatch):
    for origin in ["https://platform.leafdesign.ai", "http://platform-staging.leafdesign.ai",
                   "https://other.example", "https://user@platform-staging.leafdesign.ai",
                   p.ORIGIN + ":443", p.ORIGIN + "/", p.ORIGIN + "?wait=1"]:
        f = Fake(monkeypatch)
        with pytest.raises(p.Refusal):
            p.Probe(f).run(args(base_url=origin), f.env)
        assert not f.secret_reads and not f.saved and not f.ingests and not f.posts
    f = Fake(monkeypatch)
    with pytest.raises(p.Refusal, match="ack_required"):
        p.Probe(f).run(args(ack=None), f.env)
    assert not f.secret_reads and not f.reserved
    f.redirect = True
    assert run(f) == "failed"
    assert not f.secret_reads and not f.ingests and not f.posts and not f.saved
    calls = []

    class Session:
        response_status = 302

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            calls.append((method, url, kwargs))
            return SimpleNamespace(status_code=self.response_status, content=b"", json=lambda: {})

    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(Session=Session))
    with pytest.raises(p.Refusal, match="redirect_refused"):
        LIVE_HTTP(f, "GET", "/api/session/identity", JWT)
    assert calls[0][1] == p.ORIGIN + "/api/session/identity"
    assert calls[0][2]["verify"] is True and calls[0][2]["allow_redirects"] is False
    assert calls[0][2]["headers"] == {"Authorization": "Bearer " + JWT}
    Session.response_status = 401
    boundary = SimpleNamespace(http=lambda *a: LIVE_HTTP(f, *a))
    p.LiveAdapters.check_origin(boundary)
    assert calls[-1][2]["headers"] == {}
    Session.response_status = 200  # Auth-off deployments cannot attest identity.
    with pytest.raises(p.Refusal, match="origin_refused"):
        p.LiveAdapters.check_origin(boundary)
    Session.response_status = 202
    LIVE_HTTP(f, "POST", "/api/run", JWT, {"dwg_version": 1}, "p080-r3:offline-1")
    assert calls[-1][2]["headers"] == {
        "Authorization": "Bearer " + JWT, "Content-Type": "application/json",
        "Idempotency-Key": "p080-r3:offline-1"}
    assert "params" not in calls[-1][2] and calls[-1][1] == p.ORIGIN + "/api/run"
    LIVE_HTTP(f, "GET", "/api/health", None)
    assert calls[-1][1] == p.ORIGIN + "/api/health"
    assert calls[-1][2]["headers"] == {}


def test_identity_requires_protected_acceptance_b_binding(monkeypatch):
    for defect in ("tenant", "subject", "expiry", "inactive", "echo", "synthetic", "malformed", "missing", "token-subject"):
        f = Fake(monkeypatch)
        if defect in {"tenant", "subject"}:
            f.identity_data[defect + "_sha256"] = "0" * 64
        elif defect == "expiry":
            f.secret_data["exp"] = 1100
        elif defect == "inactive":
            f.binding_active = False
        elif defect == "echo":
            f.fetch_identity = {"tenant_id": "foreign"}
        elif defect == "synthetic":
            f.identity_data["synthetic_provisioning_verified"] = False
        elif defect == "missing":
            f.secret_data = None
        elif defect == "token-subject":
            f.secret_data["jwt"] = JWT.replace(JWT.split(".")[1],
                base64.urlsafe_b64encode(b'{"sub":"foreign","exp":2000}').decode().rstrip("="))
        else:
            f.secret_data["jwt"] = None
        assert run(f) == "failed"
        assert f.secret_reads == ["stg/leaf/acceptance-tenant-b"]
        assert not f.ingests and not f.posts and not f.saved


def test_runtime_requires_verified_source_and_postgres_authorities(monkeypatch):
    for defect in ("unattested", "source", "environment", "database", "jobs", "broker", "drawing", "mount"):
        f = Fake(monkeypatch)
        if defect == "unattested":
            f.runtime_data["verified_at"] = 0
        elif defect in {"source", "environment"}:
            f.runtime_data[defect] = "wrong"
        elif defect == "database":
            f.runtime_data["database_sha256"] = "wrong"
        elif defect in {"jobs", "broker", "drawing"}:
            f.env["LEAF_" + defect.upper() + "_STORE"] = "memory"
        else:
            f.env["LEAF_STORE_DIR"] = "/foreign"
        assert run(f) == "failed"
        assert not f.secret_reads and not f.saved and not f.ingests and not f.posts


@pytest.fixture
def ecs_component():
    def build(component):
        task_arn = f"arn:aws:ecs:{p.REGION}:{p.ACCOUNT}:task/staging/0123456789abcdef"
        definition_arn = f"arn:aws:ecs:{p.REGION}:{p.ACCOUNT}:task-definition/leaf-platform-{component}:1"
        entry = {"task": task_arn, "container": component,
                 "image_digest": "sha256:" + "1" * 64}
        receipt = {"database_secret_arn": f"arn:aws:secretsmanager:{p.REGION}:{p.ACCOUNT}:secret:stg/database-abcdef",
                   "efs_id": "fs-0123456789abcdef0"}
        environment = {key: ENV[key] for key in p.COMPONENT_KEYS[component]}
        if component == "broker":
            environment["LEAF_RUNTIME_ENV"] = "production"
        task = {"taskArn": task_arn, "taskDefinitionArn": definition_arn,
                "lastStatus": "RUNNING",
                "containers": [{"name": component, "imageDigest": entry["image_digest"]}]}
        definition = {
            "taskDefinitionArn": definition_arn,
            "containerDefinitions": [{
                "name": component,
                "environment": [{"name": key, "value": value}
                                for key, value in environment.items()],
                "secrets": [{"name": "DATABASE_URL", "valueFrom": receipt["database_secret_arn"]}],
                "mountPoints": [{"containerPath": "/data/drawings", "sourceVolume": "drawings",
                                 "readOnly": False}],
            }],
            "volumes": [{"name": "drawings", "efsVolumeConfiguration": {
                "fileSystemId": receipt["efs_id"]}}],
        }
        return task, definition, entry, receipt
    return build


@pytest.mark.parametrize("component", ["app", "broker"])
@pytest.mark.parametrize("labels", [None, {}, {p.REVISION_LABEL: SOURCE}])
def test_check_component_accepts_staging_components(ecs_component, component, labels):
    task, definition, entry, receipt = ecs_component(component)
    if labels is not None:
        definition["containerDefinitions"][0]["dockerLabels"] = labels
    if component == "broker":
        environment = {v["name"]: v["value"]
                       for v in definition["containerDefinitions"][0]["environment"]}
        assert environment["LEAF_RUNTIME_ENV"] == "production"
        assert "LEAF_JOBS_STORE" not in environment
    assert p.check_component(component, task, definition, entry, SOURCE, ENV, receipt) is None


@pytest.mark.parametrize("component,defect,reason", [
    ("app", "revision", "source_refused"),
    ("app", "digest", "source_refused"),
    ("broker", "drawing", "authority_refused"),
    ("app", "environment", "authority_refused"),
    ("app", "override", "authority_refused"),
    ("broker", "override", "authority_refused"),
    ("app", "database", "database_refused"),
    ("app", "efs", "mount_refused"),
    ("app", "readonly", "mount_refused"),
    ("app", "duplicate-mount", "mount_refused"),
    ("app", "stopped", "runtime_refused"),
    ("app", "account", "runtime_refused"),
    ("app", "region", "runtime_refused"),
    ("app", "missing-definition-container", "runtime_refused"),
    ("app", "duplicate-definition-container", "runtime_refused"),
    ("app", "missing-task-container", "source_refused"),
    ("app", "duplicate-task-container", "source_refused"),
])
def test_check_component_refusals(ecs_component, component, defect, reason):
    task, definition, entry, receipt = ecs_component(component)
    container = definition["containerDefinitions"][0]
    if defect == "revision":
        container["dockerLabels"] = {p.REVISION_LABEL: "b" * 40}
    elif defect == "digest":
        task["containers"][0]["imageDigest"] = "sha256:" + "2" * 64
    elif defect in {"drawing", "environment"}:
        key, value = (("LEAF_DRAWING_STORE", "memory") if defect == "drawing"
                      else ("LEAF_RUNTIME_ENV", "production"))
        for variable in container["environment"]:
            if variable["name"] == key:
                variable["value"] = value
    elif defect == "override":
        task["overrides"] = {"containerOverrides": [{"name": component, "environment": [
            {"name": "LEAF_DRAWING_STORE", "value": "memory"}]}]}
    elif defect == "database":
        container["secrets"][0]["valueFrom"] += "-other"
    elif defect == "efs":
        definition["volumes"][0]["efsVolumeConfiguration"]["fileSystemId"] = "fs-other"
    elif defect == "readonly":
        container["mountPoints"][0]["readOnly"] = True
    elif defect == "duplicate-mount":
        container["mountPoints"].append(deepcopy(container["mountPoints"][0]))
    elif defect == "stopped":
        task["lastStatus"] = "STOPPED"
    elif defect == "account":
        task["taskArn"] = task["taskArn"].replace(p.ACCOUNT, "000000000000")
    elif defect == "region":
        task["taskArn"] = task["taskArn"].replace(p.REGION, "us-west-2")
    elif defect == "missing-definition-container":
        definition["containerDefinitions"] = []
    elif defect == "duplicate-definition-container":
        definition["containerDefinitions"].append(deepcopy(container))
    elif defect == "missing-task-container":
        task["containers"] = []
    elif defect == "duplicate-task-container":
        task["containers"].append(deepcopy(task["containers"][0]))
    else:
        raise AssertionError(defect)
    with pytest.raises(p.Refusal, match="^" + reason + "$"):
        p.check_component(component, task, definition, entry, SOURCE, ENV, receipt)


def test_check_component_merges_overrides_for_matching_container(ecs_component):
    task, definition, entry, receipt = ecs_component("app")
    for variable in definition["containerDefinitions"][0]["environment"]:
        if variable["name"] == "LEAF_DRAWING_STORE":
            variable["value"] = "memory"
    task["overrides"] = {"containerOverrides": [
        {"name": "app", "environment": [{"name": "LEAF_DRAWING_STORE", "value": "postgres"}]},
        {"name": "sidecar", "environment": [{"name": "LEAF_DRAWING_STORE", "value": "memory"}]},
    ]}
    assert p.check_component("app", task, definition, entry, SOURCE, ENV, receipt) is None


@pytest.mark.parametrize("status,body,expected", [
    (200, {"source_sha": SOURCE}, True),
    (503, {"source_sha": SOURCE}, False),
    (201, {"source_sha": SOURCE}, False),
    (200, {"source_sha": "b" * 40}, False),
    (200, {}, False),
    (200, None, False),
    (200, [{"source_sha": SOURCE}], False),
    (200, SOURCE, False),
])
def test_served_source_ok(status, body, expected):
    assert p.served_source_ok(status, body, SOURCE) is expected


def test_entitlement_and_catalog_refusals_precede_submission(monkeypatch):
    for defect in ("read", "broker", "missing", "shadowed", "digest", "changed", "live"):
        f = Fake(monkeypatch)
        if defect == "read":
            f.entitled = False
        elif defect == "broker":
            f.broker_allowed = False
        elif defect == "missing":
            f.tools = []
        elif defect == "shadowed":
            f.tools.append(deepcopy(f.tools[0]))
        elif defect == "digest":
            f.tools[0]["catalog_digest"] = "sha256:" + "0" * 64
        elif defect == "changed":
            f.tools[0]["entry"] = "authored/unapproved.py"
            changed = {k: v for k, v in f.tools[0].items()
                       if k not in {"catalog_digest", "tool_manifest_sha256"}}
            f.tools[0]["catalog_digest"] = f.tools[0]["tool_manifest_sha256"] = "sha256:" + p.sha(p.canonical(changed))
        else:
            f.tools[0]["aps_live"] = True
        assert run(f) == "failed"
        assert not f.posts and not f.ingests


def test_seed_is_unique_synthetic_and_version_pinned(monkeypatch):
    f = Fake(monkeypatch)
    assert run(f) == "passed"
    assert len(f.ingests) == 1 and f.ingests[0] == (TENANT, "p080-r3-" + "d" * 32, p.FIXTURE_BYTES)
    request, key = f.posts[0]
    assert request == {"tool": p.TOOL, "params": {"layer": "p080_r3"}, "dwg": f.ingests[0][1],
                       "dwg_version": 1, "catalog_digest": f.tools[0]["catalog_digest"]}
    assert key == "p080-r3:offline-1" and "rooftop_demo" not in json.dumps(request)
    f = Fake(monkeypatch)
    f.seed()
    assert run(f) == "failed"
    assert not f.posts and not f.ingests and not f.fs.removed


def test_happy_path_creates_exactly_one_job_and_fetches_result(monkeypatch):
    f = Fake(monkeypatch)
    assert run(f) == "passed"
    receipt = f.saved[-1]
    assert len(f.posts) == 1 and receipt["job_id"] == JOB and receipt["acknowledged_202"]
    assert receipt["result_match"] and receipt["database_http_agree"] and receipt["broker_non_aps"]
    assert [s["status"] for s in receipt["samples"]] == ["submitted", "running", "complete"]
    assert receipt["samples"][0]["queued"] is True
    running = receipt["samples"][1]
    assert running["attempt"] == 1 and running["started_at"] == running["heartbeat_at"] == 1001
    assert running["lease_expires_at"] == 1100 and running["lease_owner_sha256"] == p.sha("SENTINEL_LEASE")
    assert receipt["cleanup_complete"] and not f.m and not f.v
    assert not f.paths(TENANT, receipt["drawing"])[0].exists()


def test_ambiguous_submit_recovers_by_read_without_reposting(monkeypatch):
    f = Fake(monkeypatch, "lost_ack")
    assert run(f) == "passed"
    assert len(f.posts) == 1 and f.saved[-1]["job_id"] == JOB
    assert not f.saved[-1]["acknowledged_202"] and f.saved[-1]["cleanup_complete"]


def test_ambiguous_submit_without_row_is_unknown(monkeypatch):
    f = Fake(monkeypatch, "unknown")
    assert run(f) == "submission_unknown"
    assert len(f.posts) == 1 and f.clock >= 1600
    assert f.saved[-1]["job_id"] is None and not f.saved[-1]["cleanup_complete"]
    assert f.m and f.v and not f.fs.removed
    # Even an acknowledgement cannot substitute for a missing database row.
    f = Fake(monkeypatch, "unknown")
    f.lost = False
    assert run(f) == "submission_unknown"
    assert len(f.posts) == 1 and f.saved[-1]["acknowledged_202"]
    assert not f.fs.removed


def test_duplicate_rows_or_existing_run_refuse_reexecution(monkeypatch):
    f = Fake(monkeypatch)
    f.duplicate = True
    assert run(f) == "cleanup_pending"
    assert f.saved[-1]["execution_outcome"] == "failed"
    assert f.saved[-1]["reason"] == "duplicate_submission"
    assert len(f.posts) == 1 and not f.fs.removed
    f = Fake(monkeypatch)
    f.reserved = True
    assert run(f) == "failed"
    assert not f.posts and not f.ingests and not f.saved


def test_missing_transition_is_reported_incomplete(monkeypatch):
    f = Fake(monkeypatch, "fast")
    assert run(f) == "completed_transition_evidence_incomplete"
    assert [s["status"] for s in f.saved[-1]["samples"]] == ["complete"]
    assert f.saved[-1]["samples"][0]["started_at"] == 1001
    assert len(f.posts) == 1  # Durable started_at never invents a running sample.


def test_retry_failure_or_result_mismatch_never_passes(monkeypatch):
    for defect in ("retry", "retrying", "degraded", "fallback", "failure", "conflict", "result", "fetch", "admission", "paid", "ledger", "provenance", "timestamp", "admission-status", "ledger-status"):
        f = Fake(monkeypatch, "failed" if defect == "failure" else "happy")
        if defect == "retry":
            f.row_changes["attempt"] = 2
        elif defect == "retrying":
            f.row_changes["retrying"] = True
        elif defect in {"degraded", "fallback"}:
            key = "degraded_mode" if defect == "degraded" else "fallback"
            f.row_changes["result_json"] = {"ok": True, "result": p.EXPECTED, key: True}
        elif defect == "conflict":
            f.row_changes["conflicts"] = 1
        elif defect == "result":
            f.row_changes["result_json"] = {"ok": True, "result": {**p.EXPECTED, "total_area": 301}}
        elif defect == "fetch":
            f.fetch_changes["tenant_id"] = "foreign"
        elif defect == "admission":
            f.execution_allowed = False
        elif defect == "paid":
            f.admission_live = True
        elif defect == "ledger":
            f.ledger_live = True
        elif defect == "provenance":
            f.row_changes["provenance_json"] = {"attempt": 1, "execution_path": "cloud"}
        elif defect == "timestamp":
            f.row_changes["heartbeat_at"] = "SENTINEL_INVALID_TIMESTAMP"
        elif defect == "admission-status":
            f.admission_status = 500
        elif defect == "ledger-status":
            f.ledger_status = "failed"
        assert run(f) != "passed"
        assert f.saved[-1]["execution_outcome"] == "failed" and len(f.posts) == 1


def test_cleanup_closes_only_owned_inflight_job_and_waits(monkeypatch):
    f = Fake(monkeypatch, "inflight")
    assert run(f) == "failed"
    assert f.closes == [JOB] and f.cleanup_waits == 2
    assert f.saved[-1]["cleanup_complete"] and len(f.posts) == 1
    assert p.Probe(f).cleanup(args(command="cleanup"), f.env) == "cleanup_complete"
    assert f.closes == [JOB] and len(f.posts) == 1
    f.seed()
    f.current[0]["job_id"] = "eeeeeeee" + JOB[8:]
    assert p.Probe(f).cleanup(args(command="cleanup"), f.env) == "cleanup_pending"
    assert f.closes == [JOB] and len(f.posts) == 1


def test_cleanup_is_scoped_idempotent_and_preserves_audit(monkeypatch):
    f = Fake(monkeypatch)
    before = deepcopy((f.audit, f.unrelated))
    assert run(f) == "passed"
    removed = list(f.fs.removed)
    assert len(removed) == 5  # version blob, store marker, v/, drawing/, source.
    assert p.Probe(f).cleanup(args(command="cleanup"), f.env) == "cleanup_complete"
    assert f.fs.removed == removed and (f.audit, f.unrelated) == before
    assert len(f.posts) == 1 and not f.closes


def test_cleanup_refuses_changed_or_unowned_resources(monkeypatch):
    for defect in ("symlink", "escape", "unexpected", "bytes", "hash", "version", "checkout", "foreign", "active", "intake"):
        f = Fake(monkeypatch)
        assert run(f) == "passed"
        f.seed()
        prefix, blob = f.paths(TENANT, "p080-r3-" + "d" * 32)
        if defect == "symlink":
            f.fs.links.add(str(blob))
        elif defect == "escape":
            f.fs.escapes.add(str(blob))
        elif defect == "unexpected":
            f.fs.put(prefix / "foreign.txt", b"unowned")
        elif defect == "bytes":
            f.fs.files[str(blob)] = b"changed"
        elif defect == "hash":
            f.v[0]["content_sha256"] = "0" * 64
        elif defect == "version":
            f.v.append({**f.v[0], "version": 2})
        elif defect == "checkout":
            f.m[0]["checkout_holder"] = "foreign"
        elif defect == "foreign":
            f.foreign = True
        elif defect == "intake":
            f.v[0]["intake_ref"] = "unowned/intake.json"
        else:
            f.active_admission = True
        before = deepcopy(f.fs.files)
        removed = list(f.fs.removed)
        assert p.Probe(f).cleanup(args(command="cleanup"), f.env) == "cleanup_pending"
        assert f.fs.files == before and f.fs.removed == removed
        assert f.m and f.v and len(f.posts) == 1


def test_evidence_is_sanitized_offline_and_gate_registered(monkeypatch, capsys):
    f = Fake(monkeypatch, "lost_ack")
    assert run(f) == "passed"
    evidence = json.dumps(f.saved)
    for sentinel in (JWT, DATABASE, SUBJECT, "SENTINEL_SECRET_VALUE", "SENTINEL_RAW_EXCEPTION",
                     "Authorization", "signature=", "SENTINEL_LEASE", "SENTINEL_FINGERPRINT"):
        assert sentinel not in evidence
    assert p.main(["run", "--jwt", JWT]) == 1
    output = capsys.readouterr()
    assert JWT not in output.out + output.err
    assert json.loads(output.out) == {"outcome": "failed", "reason": "probe_refused"}
    tainted = deepcopy(f.saved[-1])
    tainted["response_body"] = JWT
    with pytest.raises(p.Refusal, match="receipt_refused"):
        p.validate_receipt(tainted)
    registration = (ROOT / "scripts/run-all-gates.py").read_text()
    assert 'Suite("server-probe-staging-async-job"' in registration
    assert '_py_pytest("tests/test_probe_staging_async_job.py"), 22)' in registration
    selection = json.loads((ROOT / "scripts/ci/test-selection-map.json").read_text())
    assert "server-probe-staging-async-job" in selection["mandatory_suite_ids"]
    floors = (ROOT / "scripts/test_gate_runner.py").read_text()
    assert '"server-probe-staging-async-job": 22' in floors
    no_skip = floors.split("_OPERATOR_NO_SKIP_FILES = frozenset({", 1)[1].split("})", 1)[0]
    assert '"server-probe-staging-async-job"' in no_skip
    source = Path(__file__).read_text()
    assert source.count("\ndef test_") == 22
    assert not any(name in sys.modules for name in ("probe_staging_async_job_live",))
