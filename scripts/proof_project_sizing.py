"""Private project-proof replay custody. Importing this module has no effects.

F1 supplies plans and barriers; F2 drives them from the managed project walk.
Nothing here provisions an account grant or exposes an HTTP control route.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import threading
import time
from contextvars import ContextVar
from types import MappingProxyType, SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
MANIFEST = "scripts/fixtures/string_sizer_replay_manifest.json"
MANIFEST_SHA256 = "2b467b2257225849a05b413af581c92c70e9e7ddc0b893c0d3779c8a479bc53e"
FIXTURE = "server/tests/fixtures/w1_string_length_recorded_response.json"
FIXTURE_SHA256 = "6e807695b9b36bbaf9007f3322d3570ea748853c746c33dcefcfdfe07c1c299a"
CATALOG = "scripts/fixtures/project_replay_module_catalog.json"
CATALOG_SHA256 = "a4a07e1b0aefc9353c2bbd5e21b774368a4c71bc053696670e997ec9d221c7fc"
FAULT_DECISION_TIMEOUT_S = 60.0
REPLACEMENT_READY_S = 30.0
MAX_FAULT_EVENTS = 256
TOOL = "solar-size-strings"
GRANT_REF = "project-replay-fixture"
CONFIG_KEY = "LEAF_PROJECT_REPLAY_CONFIG"
CONFIG_HASH_KEY = "LEAF_PROJECT_REPLAY_CONFIG_SHA256"
SCENARIOS = ("capture-recovery", "browser-response-discard", "service-response-drop")
PIN_FILES = (MANIFEST, FIXTURE, CATALOG, "scripts/proof_project_sizing.py", "scripts/proof_project.py",
             "scripts/proof_string_sizer.py", "scripts/start-leaf.py",
             "server/solar_sizing_client.py", "server/solar_project_jobs.py",
             "server/solar_project_graph.py", "server/solar_project_sizing.py")


class ReplayRefused(ValueError):
    def __init__(self):
        super().__init__("project replay refused")


def _require(condition):
    if not condition:
        raise ReplayRefused()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9:|_-]{1,128}", value) is not None


BROWSER_SCENARIOS = ("capture-recovery", "read-custody", "browser-response-discard", "service-response-drop")
BROWSER_FIELDS = {
    "capture-recovery": ("scenario", "organization_id", "project_id", "drawing_id", "input_version_id", "job_id",
                         "output_version_id", "exchange_job_id", "evidence_sha256", "panels_in_sequence"),
    "read-custody": ("scenario", "organization_id", "project_id", "drawing_id", "source_version_id", "groups_job_id",
                     "input_version_id", "frame_count", "tool", "request_sha256", "output_sha256"),
    "browser-response-discard": ("scenario", "organization_id", "project_id", "drawing_id", "input_version_id", "job_id",
                                 "output_version_id", "exchange_job_id", "evidence_sha256", "panels_in_sequence",
                                 "submissions", "same_submission"),
    "service-response-drop": ("scenario", "organization_id", "project_id", "drawing_id", "input_version_id", "job_id",
                              "status", "reason_code", "retryable"),
}


def validate_browser_record(value, scenario, organization_id):
    fields = BROWSER_FIELDS.get(scenario)
    _require(fields is not None and type(value) is dict and set(value) == set(fields))
    _require(value["scenario"] == scenario and value["organization_id"] == organization_id)
    for key in fields:
        if key.endswith("_id"):
            _require(_id(value[key]))
        if key.endswith("_sha256"):
            _require(_hash(value[key]))
        if key in {"frame_count", "panels_in_sequence"}:
            _require(type(value[key]) is int and 1 <= value[key] <= 9007199254740991)
    if "exchange_job_id" in value:
        _require(value["exchange_job_id"] == value["job_id"]
                 and value["input_version_id"] != value["output_version_id"])
    if scenario == "read-custody":
        _require(value["tool"] == "solar-string-data" and value["input_version_id"] != value["source_version_id"])
    if scenario == "browser-response-discard":
        _require(type(value["submissions"]) is int and value["submissions"] == 2 and value["same_submission"] is True)
    if scenario == "service-response-drop":
        _require(value["status"] == "failed" and value["reason_code"] == "SIZING_OUTCOME_UNKNOWN" and value["retryable"] is False)
    return {key: value[key] for key in fields}


def validate_browser_sizing(value, organization_id):
    _require(type(value) is dict and set(value) == set(BROWSER_SCENARIOS))
    records = {key: validate_browser_record(value[key], key, organization_id) for key in BROWSER_SCENARIOS}
    jobs = [records[key] for key in SCENARIOS]
    _require(len({row["project_id"] for row in jobs}) == 3 and len({row["job_id"] for row in jobs}) == 3)
    source, read = records["capture-recovery"], records["read-custody"]
    _require(read["project_id"] == source["project_id"] and read["drawing_id"] == source["drawing_id"]
             and read["source_version_id"] == source["output_version_id"])
    return records


def _endpoint(value):
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/string-length", value or "")
    _require(match is not None and int(match[1]) <= 65535)
    return value


def _local(environment):
    _require(environment.get("LEAF_RUNTIME_ENV") in {"local", "test"})
    _require(environment.get("LEAF_ENV", "local") in {"local", "test"})
    for key, value in environment.items():
        upper = key.upper()
        _require(not ("SYNTHETIC" in upper and value not in ("", "0", "false")))
        _require(upper not in {"SIZING_CLASSIFICATION", "SIZING_URL", "LEAF_CLOUD_GRANTS_FILE",
                              "LEAF_CLOUD_GRANT_SOURCE", "LEAF_CLOUD_M2M_GRANTS"})


@dataclass(frozen=True, repr=False)
class ReplayPlan:
    run_id: str
    run_root: Path
    root_identity: tuple[int, int]
    organization_id: str
    endpoint: str
    source_sha256: str
    source_files: tuple[tuple[str, str], ...]
    fixture_map: object = field(repr=False)
    token: str = field(repr=False)
    manifest_sha256: str = MANIFEST_SHA256
    fixture_sha256: str = FIXTURE_SHA256

    def __repr__(self):
        return "<private project replay plan>"

    def public(self):
        return dict(classification="synthetic", run_id=self.run_id, organization_id=self.organization_id,
                    source_sha256=self.source_sha256, endpoint_sha256=digest(self.endpoint.encode()),
                    manifest_sha256=self.manifest_sha256, fixture_sha256=self.fixture_sha256)


def _same_plan(a, b):
    return (type(a) is ReplayPlan and type(b) is ReplayPlan
            and a.public() == b.public() and a.run_root == b.run_root
            and tuple(a.root_identity) == tuple(b.root_identity) and a.token == b.token
            and a.source_files == b.source_files and dict(a.fixture_map) == dict(b.fixture_map))


def _load_parser():
    # Keep parser reuse independent of pytest's collection/import mode.
    name = "_project_replay_fixture_parser"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, REPO / "scripts/proof_string_sizer.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


def _fixtures(repo, loader=None):
    # load_manifest temporarily inserts server/ on sys.path. Restore it exactly.
    paths = sys.path[:]
    try:
        return (loader or _load_parser().load_manifest)(repo / MANIFEST, repo)
    finally:
        sys.path[:] = paths


def prepare_replay(*, run_id, run_root, root_identity, organization_id, endpoint,
                   source_sha256, source_files, environment, repo=REPO, read=None,
                   stat_root=None, load_manifest=None, token_factory=None):
    """Validate all custody before creating private material or a listener."""
    _local(environment)
    root, repo = Path(run_root), Path(repo)
    _require(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id or "") is not None)
    _require(root.is_absolute() and root != Path(root.anchor) and root == root.resolve())
    _require(not root.is_relative_to(repo) and not repo.is_relative_to(root))
    _require(environment.get("LEAF_PROOF_PROFILE") == "project")
    _require(environment.get("LEAF_PROOF_PROJECT_RUN_ID") == run_id)
    _require(Path(environment.get("LEAF_STORE_DIR", "")) == root / "drawings")
    _require(environment.get("LEAF_AUTH_LIVE") == "1" and bool(environment.get("DATABASE_URL")))
    _require(environment.get("LEAF_HARNESS_DATABASE_URL") == environment["DATABASE_URL"])
    _require(_id(organization_id) and organization_id != "demo-tenant")
    _require(environment.get("VITE_TENANT_ID") == organization_id)
    _endpoint(endpoint)
    _require(_hash(source_sha256))
    read = read or (lambda path: path.read_bytes())
    observed = stat_root(root) if stat_root else (root.stat().st_dev, root.stat().st_ino)
    _require(tuple(observed) == tuple(root_identity) and len(root_identity) == 2)
    pins = tuple(source_files)
    _require(len(pins) == len(PIN_FILES) and {name for name, _ in pins} == set(PIN_FILES))
    for name, pin in pins:
        _require(_hash(pin) and digest(read(repo / name)) == pin)
    _require(dict(pins)[MANIFEST] == MANIFEST_SHA256 and dict(pins)[FIXTURE] == FIXTURE_SHA256
             and dict(pins)[CATALOG] == CATALOG_SHA256)
    fixtures = _fixtures(repo, load_manifest)
    _require(len(fixtures) == 1)
    token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
    _require(type(token) is str and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is not None)
    return ReplayPlan(run_id, root, tuple(root_identity), organization_id, endpoint, source_sha256,
                      pins, MappingProxyType(dict(fixtures)), token)


def _validate_plan(plan, environment, *, read=None, stat_root=None, load_manifest=None):
    _require(type(plan) is ReplayPlan)
    checked = prepare_replay(run_id=plan.run_id, run_root=plan.run_root, root_identity=plan.root_identity,
        organization_id=plan.organization_id, endpoint=plan.endpoint, source_sha256=plan.source_sha256,
        source_files=plan.source_files, environment=environment, read=read, stat_root=stat_root,
        load_manifest=load_manifest, token_factory=lambda: plan.token)
    _require(checked.public() == plan.public() and dict(checked.fixture_map) == dict(plan.fixture_map))


def bootstrap_child(plan, role, environment, *, modules=None, read=None, stat_root=None, load_manifest=None):
    """Enable proof seams only in a validated, dedicated child interpreter."""
    owned = read_private_plan(environment)
    _require(owned.public() == plan.public() and owned.token == plan.token
             and dict(owned.fixture_map) == dict(plan.fixture_map))
    _require(role in {"app", "broker", "worker", "sizing-worker", "panels-worker", "groups-worker", "canonical"})
    _validate_plan(plan, environment, read=read, stat_root=stat_root, load_manifest=load_manifest)
    child = dict(environment)
    child["DATABASE_URL"] = environment["DATABASE_URL"]  # DROP_PROJECT_DSN
    child["LEAF_AUTH_LIVE"] = "1"
    production = modules is None
    if production:
        sys.path.insert(0, str(REPO / "server"))
        modules = SimpleNamespace(client=importlib.import_module("solar_sizing_client"),
            graph=importlib.import_module("solar_project_graph"),
            grants=importlib.import_module("leaf_cloud_grants"), requests=importlib.import_module("requests"))
    fixture_organization = plan.organization_id  # USE_DEMO_TENANT

    def resolve(grant_ref, organization_id):
        if grant_ref != GRANT_REF or organization_id != fixture_organization:
            raise modules.grants.CloudError("cloud_tenant_unauthorized", 403)
        return modules.grants.CloudGrant(fixture_organization, plan.token)

    class Transport:
        RequestException = modules.requests.RequestException

        @staticmethod
        @contextmanager
        def post(url, **kwargs):
            _require(url == plan.endpoint and kwargs.get("allow_redirects") is False)
            _require(kwargs.get("headers", {}).get("Authorization") == "Bearer " + plan.token)
            with modules.requests.Session() as session:
                session.trust_env = False
                with session.post(url, **kwargs) as response:
                    yield response

    modules.client.SIZING_URL = plan.endpoint
    modules.client.SIZING_CLASSIFICATION = "synthetic"  # LABEL_REPLAY_LIVE
    modules.client.resolve_grant = resolve
    modules.client.requests = Transport
    modules.graph.SYNTHETIC_SIZING_PROOF_ACCEPTED = True
    if role == "app":
        if production:
            modules.catalog = importlib.import_module("solar_module_catalog")
        if hasattr(modules, "catalog"):
            install_app_seams(plan, modules)
        if production:
            uvicorn = importlib.import_module("uvicorn")
            for name in ("h11_impl", "httptools_impl"):
                try:
                    implementation = importlib.import_module("uvicorn.protocols.http." + name)
                except ImportError:
                    continue
                patch_uvicorn_abort(implementation.RequestResponseCycle)
            run = uvicorn.run
            channel = FaultChannel(plan, "app")
            def run_app(app, *args, **kwargs):
                return run(submission_asgi_wrapper(app, channel=channel), *args, **kwargs)
            uvicorn.run = run_app
    elif role == "sizing-worker" and production:
        install_capture_seam(plan, modules.graph, FaultChannel(plan, role))
    return child


def _child_argv(role, args=()):
    return [sys.executable, "-B", str(Path(__file__).resolve()), "child", role, *args]


def run_replay_launcher(plan, launcher, environment, *, validate=None):
    """Wrap only start-leaf's closed spawn seam, restoring it on every exit."""
    (validate or _validate_plan)(plan, environment)
    original = launcher.spawn
    worker_id = "project:" + plan.run_id

    def spawn(name, cmd, cwd, env):
        (validate or _validate_plan)(plan, env)
        cmd, cwd = list(cmd), Path(cwd).resolve()
        if name in {"app", "broker"}:
            _require(cwd == REPO / "server" and cmd == [sys.executable, name + ".py"])
            cmd = _child_argv(name)
        elif name == "worker":
            expected = [sys.executable, "canonical_worker.py", "--tool", "solar-settings",
                        "--readiness", "--worker-id", worker_id]
            _require(cwd == REPO / "server" and cmd == expected)
            cmd = _child_argv("worker", cmd[2:])
        elif name == "web":
            _require(cwd == REPO / "web" and Path(cmd[0]).name.lower() in {"npm", "npm.cmd"})
            _require(cmd[1:] == ["run", "dev", "--", "--host", "127.0.0.1", "--port",
                                environment["WEB_PORT"], "--strictPort"])
        elif name == "harness":
            _require(cwd == REPO / "harness" and Path(cmd[0]).name.lower() in {"node", "node.exe"})
            _require(cmd[1:] == [str(REPO / "harness/dist/scripts/serve.js")])
        else:
            raise ReplayRefused()
        child = dict(env)
        if name in {"web", "harness"}:
            child.pop(CONFIG_KEY, None)
            child.pop(CONFIG_HASH_KEY, None)
        return original(name, cmd, cwd, child)

    launcher.spawn = spawn
    try:
        return launcher.main()
    finally:
        launcher.spawn = original


@dataclass(frozen=True)
class FaultBinding:
    run_id: str
    organization_id: str
    project_id: str
    job_id: str


@dataclass(repr=False)
class FaultSchedule:
    plan: ReplayPlan
    next_index: int = field(default=0, init=False)
    active: object = field(default=None, init=False)
    lock: object = field(default_factory=threading.RLock, repr=False)


@dataclass(repr=False)
class FaultBarrier:
    schedule: FaultSchedule
    scenario: str
    binding: FaultBinding
    consumed: bool = False

    def consume(self, binding):
        with self.schedule.lock:
            _require(binding == self.binding and not self.consumed and self.schedule.active is self)
            self.consumed = True

    def complete(self):
        with self.schedule.lock:
            _require(self.consumed and self.schedule.active is self)
            self.schedule.active = None
            self.schedule.next_index += 1

    def capture_response(self, binding, *, committed, captured_worker, current_worker,
                         stop, launch_replacement, register, validate_identity):
        """Called AFTER the real capture_response transaction returns committed.

        A worker-side hook must block that attempt at this barrier until the
        supervisor proves its termination. PostgreSQL alone reclaims the lease.
        """
        _require(self.scenario == SCENARIOS[0] and committed is True)
        _require(captured_worker == current_worker)
        self.consume(binding)
        _require(stop(captured_worker) is True)
        replacement = launch_replacement()
        register(replacement)  # Custody precedes validation, including refusal.
        _require(validate_identity(replacement, captured_worker) is True)  # SKIP_REPLACEMENT_IDENTITY
        self.complete()
        return replacement

    async def discard_browser_response(self, binding, message, send, *, admitted):
        """ASGI send wrapper seam: admission is independently observed, never invented."""
        _require(self.scenario == SCENARIOS[1] and binding == self.binding)
        if self.consumed and self.schedule.active is not self:
            return await send(message)  # The real same-job retry receives its response.
        if message.get("type") == "http.response.start" and 200 <= message.get("status", 0) < 300:
            _require(admitted(binding) is True)
            self.consume(binding)
            self.complete()
            # Raising aborts the actual response; no fabricated 202 reaches the browser.
            raise ConnectionAbortedError("project proof response discarded")
        await send(message)

    def service_response(self, binding, body, *, content_length, record, close):
        _require(self.scenario == SCENARIOS[2])
        _require(type(body) is bytes and type(content_length) is int and len(body) == content_length)
        _require(digest(body) in self.schedule.plan.fixture_map)
        self.consume(binding)
        record(digest(body))
        _require(close() is True)
        self.complete()


def arm_scenario(schedule, scenario, *, run_id, organization_id, project_id, job_id):
    with schedule.lock:
        _require(schedule.active is None and schedule.next_index < len(SCENARIOS))
        _require(scenario == SCENARIOS[schedule.next_index])
        _require(run_id == schedule.plan.run_id and organization_id == schedule.plan.organization_id)
        _require(_id(project_id) and _id(job_id))
        barrier = FaultBarrier(schedule, scenario, FaultBinding(run_id, organization_id, project_id, job_id))
        schedule.active = barrier
        return barrier


def capture_response_hook(capture, *, binding_for, committed, pause):
    """Worker-side barrier adapter; never synthesize exchange states or attempts.

    F2's private driver supplies the committed observation and pause channel.
    The supervisor side consumes FaultBarrier.capture_response, then reclaims
    through the normal worker. A failed capture never reaches the pause.
    """
    def wrapped(scope, lease, **kwargs):
        result = capture(scope, lease, **kwargs)
        binding = binding_for(scope, lease)
        _require(committed(scope, lease) is True)
        pause(binding, lease.worker_id)
        return result
    return wrapped


def submission_asgi_wrapper(app, *, barrier=None, binding_for=None, admitted=None, channel=None):
    """F2 selects the real sizing submission scope; all admission remains in app."""
    if channel is not None:
        _require(barrier is None and binding_for is None and admitted is None)
        return channel_submission_wrapper(app, channel)
    async def wrapped(scope, receive, send):
        binding = binding_for(scope)
        if binding is None:
            return await app(scope, receive, send)
        _require(binding == barrier.binding)
        async def wrapped_send(message):
            return await barrier.discard_browser_response(binding, message, send, admitted=admitted)
        return await app(scope, receive, wrapped_send)
    return wrapped


def verify_sizing_run(plan, attachment, *, observe):
    """Compare a bounded attachment to independently read canonical custody.

    F2 supplies the managed-walk attachment and the database observation. A
    browser assertion is never accepted as that independent observation.
    """
    fields = {"run_id", "organization_id", "project_id", "job_id", "output_version_id",
              "exchange_job_id", "classification", "endpoint_sha256", "fixture_sha256",
              "evidence_sha256", "request_sha256", "response_sha256"}
    _require(type(attachment) is dict and set(attachment) == fields)
    _require(attachment["run_id"] == plan.run_id and attachment["organization_id"] == plan.organization_id)
    _require(all(_id(attachment[key]) for key in ("project_id", "job_id", "output_version_id", "exchange_job_id")))
    _require(attachment["job_id"] == attachment["exchange_job_id"])
    _require(attachment["classification"] == "synthetic")
    _require(attachment["endpoint_sha256"] == digest(plan.endpoint.encode()))
    _require(attachment["fixture_sha256"] == plan.fixture_sha256 and _hash(attachment["evidence_sha256"]))
    fixture = plan.fixture_map.get(attachment["request_sha256"])
    _require(fixture is not None and attachment["response_sha256"] == fixture[2])
    observed = observe(attachment["organization_id"], attachment["project_id"], attachment["job_id"],
                       attachment["output_version_id"])
    _require(type(observed) is dict and observed == attachment)
    return dict(attachment)


def validate_public_replay(value, *, run_id, organization_id, source_sha256):
    fields = {"classification", "run_id", "organization_id", "source_sha256", "endpoint_sha256",
              "manifest_sha256", "fixture_sha256"}
    _require(type(value) is dict and set(value) == fields)
    _require(value["classification"] == "synthetic" and value["run_id"] == run_id)
    _require(value["organization_id"] == organization_id and organization_id != "demo-tenant")
    _require(value["source_sha256"] == source_sha256 and _hash(source_sha256))
    _require(_hash(value["endpoint_sha256"]) and value["manifest_sha256"] == MANIFEST_SHA256)
    _require(value["fixture_sha256"] == FIXTURE_SHA256)


def validate_sizing_attachment(attachment, facts):
    """Receipt-side shape; database verification precedes serialization."""
    fields = {"run_id", "organization_id", "project_id", "job_id", "output_version_id",
              "exchange_job_id", "classification", "endpoint_sha256", "fixture_sha256",
              "evidence_sha256", "request_sha256", "response_sha256"}
    _require(type(attachment) is dict and set(attachment) == fields)
    for key in ("run_id", "organization_id", "classification", "endpoint_sha256", "fixture_sha256"):
        _require(attachment[key] == facts[key])
    _require(all(_id(attachment[key]) for key in ("project_id", "job_id", "output_version_id", "exchange_job_id")))
    _require(attachment["job_id"] == attachment["exchange_job_id"])
    _require(all(_hash(attachment[key]) for key in ("evidence_sha256", "request_sha256", "response_sha256")))


def observe_database_sizing(plan, attachment, *, graph, conn):
    """Independent job/exchange/publication read; browser booleans are not evidence."""
    from uuid import UUID
    def observe(organization_id, project_id, job_id, output_version_id):
        job = conn.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (job_id,)).fetchone()
        _require(job is not None and job["status"] == "succeeded" and job["tool_name"] == TOOL)
        _require(str(job["org_id"]) == organization_id and str(job["project_id"]) == project_id)
        _require(job["request_tenant_id"] == organization_id and str(job["output_version_id"]) == output_version_id)
        scope, lease = graph._exchange_authority(job)
        exchange = graph._exchanges().get_exchange(scope, lease, conn=conn)
        _require(exchange is not None and exchange["state"] == "published")
        _require(str(exchange["output_version_id"]) == output_version_id)
        for kind in ("request", "response", "evidence"):
            _require(digest(exchange[kind + "_bytes"]) == exchange[kind + "_sha256"])
        record = json.loads(exchange["evidence_bytes"])
        _require(record["classification"] == exchange["classification"] == "synthetic")
        _require(record["producer"]["service_identity"] == plan.endpoint)
        _require(record["identity"]["organization_id"] == organization_id)
        _require(record["identity"]["project_id"] == project_id and record["identity"]["job_id"] == job_id)
        commit = job["result"]["graph_commit"]
        expected = {key: commit[key] for key in ("schema", "attempt", "tool", "tool_manifest_sha256",
            "parameters", "parent_intake_sha256", "before_graph_sha256", "graph_project_id", "initialized")}
        context = job["execution_context"]
        expected.update(organization_id=organization_id, project_id=project_id,
            drawing_id=str(context["drawing_id"]), parent_version_id=str(job["input_version_id"]),
            actor_binding_id=str(context["actor_binding_id"]), checkout_fence=str(context["checkout_fence"]),
            job_id=job_id, sizing_exchange={"job_id": job_id, "evidence_sha256": exchange["evidence_sha256"]})
        proof = graph.project_graph_job_provenance(UUID(job_id), commit, expected=expected, conn=conn)
        _require(proof["output_version_id"] == output_version_id)
        return dict(run_id=plan.run_id, organization_id=organization_id, project_id=project_id,
            job_id=job_id, output_version_id=output_version_id, exchange_job_id=str(scope.job_id),
            classification=record["classification"], endpoint_sha256=digest(plan.endpoint.encode()),
            fixture_sha256=plan.fixture_sha256, evidence_sha256=exchange["evidence_sha256"],
            request_sha256=exchange["request_sha256"], response_sha256=exchange["response_sha256"])
    return verify_sizing_run(plan, attachment, observe=observe)


OBSERVATION_FIELDS = {"job_id", "organization_id", "project_id", "project_name", "tool", "status", "attempt",
    "lease_owner", "lease_expires_at", "reason_code", "retryable", "output_version_id", "jobs_in_project",
    "exchange_state", "sender_worker", "sender_attempt", "published_worker", "published_attempt",
    "exchange_output_version_id", "classification", "response_sha256", "version_count", "versions_before_job"}


def validate_observation(value):
    _require(type(value) is dict and set(value) == OBSERVATION_FIELDS)
    _require(all(_id(value[key]) for key in ("job_id", "organization_id", "project_id")))
    _require(type(value["project_name"]) is str and len(value["project_name"]) <= 256 and value["tool"] == TOOL)
    _require(type(value["status"]) is str and value["status"] in {"queued", "running", "succeeded", "failed", "cancelled"})
    for key in ("attempt", "jobs_in_project", "version_count", "versions_before_job"):
        _require(type(value[key]) is int and 0 <= value[key] <= 1024)
    for key in ("lease_owner", "output_version_id", "sender_worker", "published_worker", "exchange_output_version_id"):
        _require(value[key] is None or _id(value[key]))
    for key in ("sender_attempt", "published_attempt"):
        _require(value[key] is None or type(value[key]) is int and 1 <= value[key] <= 3)
    _require(value["classification"] is None or value["classification"] == "synthetic")
    _require(value["exchange_state"] is None or type(value["exchange_state"]) is str
             and value["exchange_state"] in {"prepared", "send_started", "response_stored", "published", "refused", "outcome_unknown"})
    _require(value["response_sha256"] is None or _hash(value["response_sha256"]))
    _require(value["retryable"] is None or type(value["retryable"]) is bool)
    _require(value["reason_code"] is None or _id(value["reason_code"]))
    _require(value["lease_expires_at"] is None or type(value["lease_expires_at"]) is str and len(value["lease_expires_at"]) <= 64)
    return value


def observe_replay_job(plan, query, *, conn):
    _require(type(query) is dict and (set(query) == {"job_id"} and _id(query["job_id"])
             or query == {"query": "send_started"}))
    where = "j.job_id=%s" if "job_id" in query else "e.state='send_started'"
    parameters = (plan.organization_id, query["job_id"]) if "job_id" in query else (plan.organization_id,)
    rows = conn.execute("SELECT j.*, p.name AS project_name, e.state AS exchange_state, e.sender_worker, "
        "e.sender_attempt, e.published_worker, e.published_attempt, e.output_version_id AS exchange_output_version_id, "
        "e.classification, e.response_sha256, "
        "(SELECT count(*) FROM jobs k WHERE k.org_id=j.org_id AND k.project_id=j.project_id "
        "AND k.tool_name='solar-size-strings' AND k.deleted_at IS NULL) AS jobs_in_project, "
        "(SELECT count(*) FROM drawing_versions v WHERE v.org_id=j.org_id AND v.project_id=j.project_id "
        "AND v.deleted_at IS NULL) AS version_count, "
        "(SELECT count(*) FROM drawing_versions v WHERE v.org_id=j.org_id AND v.project_id=j.project_id "
        "AND v.deleted_at IS NULL AND v.created_at <= j.created_at) AS versions_before_job "
        "FROM jobs j JOIN projects p ON p.project_id=j.project_id AND p.org_id=j.org_id "
        "LEFT JOIN project_sizing_exchanges e ON e.job_id=j.job_id AND e.org_id=j.org_id AND e.project_id=j.project_id "
        "WHERE j.org_id=%s AND j.tool_name='solar-size-strings' AND j.deleted_at IS NULL AND " + where,
        parameters).fetchall()
    _require(len(rows) == 1)
    row = rows[0]
    error = row.get("error") or {}
    result = dict(job_id=str(row["job_id"]), organization_id=str(row["org_id"]), project_id=str(row["project_id"]),
        project_name=row["project_name"], tool=row["tool_name"], status=row["status"], attempt=row["attempt"],
        lease_owner=row["lease_owner"], lease_expires_at=None if row["lease_expires_at"] is None else row["lease_expires_at"].isoformat(),
        reason_code=error.get("reason_code"), retryable=error.get("retryable"),
        output_version_id=None if row["output_version_id"] is None else str(row["output_version_id"]),
        jobs_in_project=row["jobs_in_project"], exchange_state=row["exchange_state"], sender_worker=row["sender_worker"],
        sender_attempt=row["sender_attempt"], published_worker=row["published_worker"], published_attempt=row["published_attempt"],
        exchange_output_version_id=None if row["exchange_output_version_id"] is None else str(row["exchange_output_version_id"]),
        classification=row["classification"], response_sha256=row["response_sha256"], version_count=row["version_count"],
        versions_before_job=row["versions_before_job"])
    return validate_observation(result)


def _driver_facts(plan, browser, driver):
    _require(type(driver) is dict and set(driver) == {*SCENARIOS, "service_posts_final"}
             and type(driver["service_posts_final"]) is int and driver["service_posts_final"] == 3)
    prefix = f"project:{plan.run_id}:sizing:"
    expected = {
        SCENARIOS[0]: dict(job_id=browser[SCENARIOS[0]]["job_id"], sender_worker_id=prefix + "1", published_worker_id=prefix + "2", service_posts=1),
        SCENARIOS[1]: dict(job_id=browser[SCENARIOS[1]]["job_id"], submissions=2, same_submission=True, service_posts=2),
        SCENARIOS[2]: dict(job_id=browser[SCENARIOS[2]]["job_id"], sender_worker_id=prefix + "2", reclaim_worker_id=prefix + "3", service_posts=3),
    }
    _require(all(type(driver[key]) is dict and driver[key] == expected[key] for key in SCENARIOS))
    _require(driver[SCENARIOS[1]]["same_submission"] is True)
    for index, key in enumerate(SCENARIOS):
        _require(type(driver[key]["service_posts"]) is int and driver[key]["service_posts"] == index + 1)
    _require(type(driver[SCENARIOS[1]]["submissions"]) is int)
    return driver


def observe_sizing_execution(plan, attachment, *, graph, conn):
    from uuid import UUID
    _require(type(attachment) is dict and set(attachment) == {"browser", "driver"})
    browser = validate_browser_sizing(attachment["browser"], plan.organization_id)
    driver = _driver_facts(plan, browser, attachment["driver"])
    output = {}
    prefix = f"project:{plan.run_id}:sizing:"
    for index, scenario in enumerate(SCENARIOS):
        record = browser[scenario]
        observed = observe_replay_job(plan, {"job_id": record["job_id"]}, conn=conn)
        _require(observed["organization_id"] == record["organization_id"] and observed["project_id"] == record["project_id"]
                 and observed["project_name"] == f"PP3B {plan.run_id} sizing {('recovery', 'submission', 'service')[index]}"
                 and observed["jobs_in_project"] == 1 and observed["classification"] == "synthetic")
        job = conn.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (record["job_id"],)).fetchone()
        context = job["execution_context"]
        _require(str(job["input_version_id"]) == record["input_version_id"]
                 and str(context["drawing_id"]) == record["drawing_id"] and job["request_tenant_id"] == plan.organization_id)
        if scenario == SCENARIOS[2]:
            _require(observed["status"] == record["status"] == "failed"
                and observed["reason_code"] == record["reason_code"] == "SIZING_OUTCOME_UNKNOWN"
                and observed["retryable"] is record["retryable"] is False
                and observed["output_version_id"] is observed["exchange_output_version_id"] is None
                and observed["exchange_state"] == "outcome_unknown" and observed["sender_worker"] == prefix + "2"
                and observed["sender_attempt"] == 1 and observed["attempt"] == 2
                and job["provenance"]["worker_id"] == prefix + "3" and job["lease_owner"] is job["lease_expires_at"] is None
                and observed["version_count"] == observed["versions_before_job"])
            output[scenario] = dict(run_id=plan.run_id, organization_id=plan.organization_id, project_id=record["project_id"],
                job_id=record["job_id"], status="failed", reason_code="SIZING_OUTCOME_UNKNOWN", retryable=False,
                exchange_state="outcome_unknown", sender_worker_id=prefix + "2", reclaim_worker_id=prefix + "3",
                job_attempt=2, output_version_id=None, service_posts=3, service_posts_final=driver["service_posts_final"])
            continue
        scope, lease = graph._exchange_authority(job)
        exchange = graph._exchanges().get_exchange(scope, lease, conn=conn)
        _require(exchange is not None)
        base = dict(run_id=plan.run_id, organization_id=plan.organization_id, project_id=record["project_id"],
            job_id=record["job_id"], output_version_id=record["output_version_id"], exchange_job_id=record["exchange_job_id"],
            classification="synthetic", endpoint_sha256=digest(plan.endpoint.encode()), fixture_sha256=plan.fixture_sha256,
            evidence_sha256=record["evidence_sha256"], request_sha256=exchange["request_sha256"], response_sha256=exchange["response_sha256"])
        base = observe_database_sizing(plan, base, graph=graph, conn=conn)
        binding = dict(organization_id=plan.organization_id, project_id=record["project_id"], drawing_id=record["drawing_id"])
        stored, version = graph._stored_context(binding, UUID(record["output_version_id"]), conn)
        value = stored.graph
        _require(value["settings"]["panels_in_sequence"] == record["panels_in_sequence"] == 27
                 and value["settings"]["voc_cold"]["passes"] is True and len(value["panels"]) == 4 and not value["frames"]
                 and value["settings"]["extra"]["string_sizing"]["project_exchange"] == {
                     "job_id": record["job_id"], "evidence_sha256": record["evidence_sha256"]}
                 and observed["output_version_id"] == record["output_version_id"]
                 and version.provenance["parent_version_id"] == record["input_version_id"])
        graph.require_project_sizing(stored, conn=conn)
        if scenario == SCENARIOS[0]:
            _require(observed["attempt"] == 2 and job["provenance"]["worker_id"] == prefix + "2"
                     and exchange["sender_worker"] == prefix + "1" and exchange["sender_attempt"] == 1
                     and exchange["published_worker"] == prefix + "2" and exchange["published_attempt"] == 2)
            base.update(panels_in_sequence=27, job_attempt=2, sender_worker_id=prefix + "1", published_worker_id=prefix + "2", service_posts=1)
        else:
            _require(record["submissions"] == 2 and record["same_submission"] is True
                     and observed["version_count"] == observed["versions_before_job"] + 1)
            base.update(panels_in_sequence=27, submissions=2, same_submission=True, jobs_in_project=1, service_posts=2)
        output[scenario] = base
    return output


REFUSALS = {"missing-reference": "SIP_R3_SERVICE_EVIDENCE_REQUIRED", "altered-values": "SIP_R3_PROOF_REJECTED",
            "foreign-reference": "SIP_R3_PROOF_REJECTED"}


def validate_custody_record(record, *, run_id, organization_id):
    fields = {"run_id", "organization_id", "project_id", "source_version_id", "input_version_id", "groups_job_id",
        "frame_count", "request_sha256", "output_sha256", "refusals", "control_read_passed", "inventories_unchanged"}
    _require(type(record) is dict and set(record) == fields and record["run_id"] == run_id
             and record["organization_id"] == organization_id)
    _require(all(_id(record[key]) for key in fields if key.endswith("_id")))
    _require(record["source_version_id"] != record["input_version_id"] and type(record["frame_count"]) is int
             and 1 <= record["frame_count"] <= 9007199254740991 and _hash(record["request_sha256"])
             and _hash(record["output_sha256"]) and record["refusals"] == REFUSALS
             and record["control_read_passed"] is True and record["inventories_unchanged"] is True)


def validate_sizing_execution_evidence(value, facts):
    _require(type(value) is dict and set(value) == set(SCENARIOS))
    base_fields = {"run_id", "organization_id", "project_id", "job_id", "output_version_id", "exchange_job_id",
                   "classification", "endpoint_sha256", "fixture_sha256", "evidence_sha256", "request_sha256", "response_sha256"}
    extras = {
        SCENARIOS[0]: {"panels_in_sequence", "job_attempt", "sender_worker_id", "published_worker_id", "service_posts"},
        SCENARIOS[1]: {"panels_in_sequence", "submissions", "same_submission", "jobs_in_project", "service_posts"},
    }
    prefix = f"project:{facts['run_id']}:sizing:"
    for scenario in SCENARIOS[:2]:
        row = value[scenario]
        _require(type(row) is dict and set(row) == base_fields | extras[scenario])
        validate_sizing_attachment({key: row[key] for key in base_fields}, facts)
        _require(row["classification"] == "synthetic" and type(row["panels_in_sequence"]) is int and row["panels_in_sequence"] == 27
                 and type(row["service_posts"]) is int and row["service_posts"] == SCENARIOS.index(scenario) + 1)
    recovery, submission, unknown = (value[key] for key in SCENARIOS)
    _require(type(recovery["job_attempt"]) is int and recovery["job_attempt"] == 2
             and recovery["sender_worker_id"] == prefix + "1" and recovery["published_worker_id"] == prefix + "2")
    _require(type(submission["submissions"]) is int and submission["submissions"] == 2 and submission["same_submission"] is True
             and type(submission["jobs_in_project"]) is int and submission["jobs_in_project"] == 1)
    fields = {"run_id", "organization_id", "project_id", "job_id", "status", "reason_code", "retryable", "exchange_state",
              "sender_worker_id", "reclaim_worker_id", "job_attempt", "output_version_id", "service_posts", "service_posts_final"}
    _require(type(unknown) is dict and set(unknown) == fields and unknown["run_id"] == facts["run_id"]
             and unknown["organization_id"] == facts["organization_id"] and _id(unknown["project_id"]) and _id(unknown["job_id"])
             and unknown["status"] == "failed" and unknown["reason_code"] == "SIZING_OUTCOME_UNKNOWN" and unknown["retryable"] is False
             and unknown["exchange_state"] == "outcome_unknown" and unknown["output_version_id"] is None
             and unknown["sender_worker_id"] == prefix + "2" and unknown["reclaim_worker_id"] == prefix + "3")
    _require(all(type(unknown[key]) is int and unknown[key] == expected for key, expected in (
        ("job_attempt", 2), ("service_posts", 3), ("service_posts_final", 3))))
    _require(len({value[key]["project_id"] for key in SCENARIOS}) == 3 and len({value[key]["job_id"] for key in SCENARIOS}) == 3)


def validate_sizing_evidence(value, facts):
    _require(type(value) is dict and set(value) == set(BROWSER_SCENARIOS))
    validate_sizing_execution_evidence({key: value[key] for key in SCENARIOS}, facts)
    recovery = value[SCENARIOS[0]]
    custody = value["read-custody"]
    validate_custody_record(custody, run_id=facts["run_id"], organization_id=facts["organization_id"])
    _require(custody["project_id"] == recovery["project_id"] and custody["source_version_id"] == recovery["output_version_id"])
    _require(custody["groups_job_id"] not in {value[key]["job_id"] for key in SCENARIOS})


def validate_execution_binding(value, attachment, organization_id):
    browser = validate_browser_sizing(attachment["browser"], organization_id)
    _require(type(value) is dict and set(value) == set(SCENARIOS))
    for scenario in SCENARIOS:
        row, reported = value[scenario], browser[scenario]
        fields = ("organization_id", "project_id", "job_id", "status", "reason_code", "retryable") if scenario == SCENARIOS[2] else (
            "organization_id", "project_id", "job_id", "output_version_id", "exchange_job_id", "evidence_sha256", "panels_in_sequence")
        _require(type(row) is dict and all(row.get(key) == reported[key] for key in fields))
        for key, expected in attachment["driver"][scenario].items():
            _require(row.get(key) == expected and type(row.get(key)) is type(expected))
    _require(value[SCENARIOS[2]]["service_posts_final"] == attachment["driver"]["service_posts_final"])


def observe_read_custody(plan, attachment, evidence, *, graph, connection, environment, deadline):
    """Publish only run-owned successors, then exercise the unmodified read boundary."""
    from copy import deepcopy
    from uuid import UUID, NAMESPACE_URL, uuid5
    import deps
    import solar_project_read as reads
    from solar_sizing_power import frame_module_power
    _require(type(attachment) is dict and set(attachment) == {"browser", "driver"})
    browser = validate_browser_sizing(attachment["browser"], plan.organization_id)
    _driver_facts(plan, browser, attachment["driver"])
    record, recovered = browser["read-custody"], browser["capture-recovery"]
    org, project_id, drawing = map(UUID, (plan.organization_id, record["project_id"], record["drawing_id"]))
    actor = UUID(evidence.binding_id)
    tenant = deps.TenantContext(plan.organization_id, org_id=plan.organization_id, tier="hosted_pro",
                              subject="leaf-proof-project|" + plan.run_id)
    actual_org, actual_actor = graph.project._access(tenant, project_id, write=True)
    _require((actual_org, actual_actor) == (org, actor))
    binding = dict(organization_id=plan.organization_id, project_id=record["project_id"], drawing_id=record["drawing_id"])
    with connection(environment, deadline, read_only=False) as conn, conn.transaction(force_rollback=True):
        groups = conn.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (record["groups_job_id"],)).fetchone()
        _require(groups is not None and groups["status"] == "succeeded" and groups["tool_name"] == "solar-panel-groups"
            and str(groups["org_id"]) == plan.organization_id and str(groups["project_id"]) == record["project_id"]
            and str(groups["input_version_id"]) == recovered["output_version_id"]
            and str(groups["output_version_id"]) == record["input_version_id"]
            and groups["execution_context"]["drawing_id"] == record["drawing_id"])
        context, version = graph._stored_context(binding, UUID(record["input_version_id"]), conn)
        _require(version.provenance["parent_version_id"] == recovered["output_version_id"])
        commit = groups["result"]["graph_commit"]
        expected = {key: commit[key] for key in ("schema", "attempt", "tool", "tool_manifest_sha256", "parameters",
            "parent_intake_sha256", "before_graph_sha256", "graph_project_id", "initialized")}
        expected.update(**binding, parent_version_id=recovered["output_version_id"], job_id=record["groups_job_id"],
            actor_binding_id=groups["execution_context"]["actor_binding_id"], checkout_fence=str(groups["execution_context"]["checkout_fence"]))
        proof = graph.project_graph_job_provenance(UUID(record["groups_job_id"]), commit, expected=expected, conn=conn)
        _require(proof["output_version_id"] == record["input_version_id"])
        source, _ = graph._stored_context(binding, UUID(recovered["output_version_id"]), conn)
        value = context.graph
        _require(len(value["frames"]) == record["frame_count"] >= 1 and value["settings"] == source.graph["settings"])
        _require(all(frame["module_power_watts"] == frame_module_power(value, frame["panel_refs"], frame["electrical_zone_ref"])
                     for frame in value["frames"]))
        graph.require_project_sizing(context, conn=conn)
    trusted = reads.solar_tools.trusted_record("solar-string-data")
    catalog_digest = deps.catalog_tool_digest(trusted)
    def run(version_id):
        result = reads.run_project_read(tenant, project_id, drawing, version_id, "solar-string-data", {},
                                       catalog_digest=catalog_digest, current=False)
        _require(type(result) is dict and type(result.get("result")) is dict)
        return result["result"]
    observed = run(UUID(record["input_version_id"]))
    _require(all(observed[key] == record[key] for key in ("input_version_id", "tool", "request_sha256", "output_sha256")))
    backend = graph.project.write_loop.upload_backend_for_tenant(plan.organization_id)
    _require(Path(backend.root).resolve() == plan.run_root / "drawings")
    artifact_root = Path(backend.root) / f"tenants/{org}/projects/{project_id}/drawings/{drawing}/solar-artifacts"
    def inventory():
        with connection(environment, deadline) as conn, conn.transaction(force_rollback=True):
            rows = conn.execute("SELECT version_id FROM drawing_versions WHERE org_id=%s AND project_id=%s "
                "AND drawing_id=%s AND deleted_at IS NULL ORDER BY seq DESC", (org, project_id, drawing)).fetchall()
            _require(bool(rows))
        files = list(artifact_root.rglob("*")) if artifact_root.exists() else []
        _require(not any(path.is_symlink() for path in files))
        return (str(rows[0]["version_id"]), len(rows), sum(path.is_file() for path in files))
    before_all = inventory()
    _require(before_all[0] == record["input_version_id"])
    store = graph.project.graph_store()
    with connection(environment, deadline, read_only=False) as conn:
        lease = store.acquire_checkout(org, project_id, drawing, actor_binding_id=actor,
            holder="Project proof custody", ttl_s=300, expected_fence=None, conn=conn)
    try:
        parent = UUID(record["input_version_id"])
        for label in ("control", *REFUSALS):
            intake = deepcopy(context.intake)
            candidate = intake["solar_design_graph"]
            if label == "missing-reference":
                del candidate["settings"]["extra"]["string_sizing"]["project_exchange"]
            elif label == "altered-values":
                candidate["frames"][0]["module_power_watts"] += 1.0
            elif label == "foreign-reference":
                foreign = browser["browser-response-discard"]
                candidate["settings"]["extra"]["string_sizing"]["project_exchange"] = {
                    "job_id": foreign["job_id"], "evidence_sha256": foreign["evidence_sha256"]}
            intake["solar_design_graph_sha256"] = graph.local.digest(candidate)
            raw = graph.local.canonical_bytes(intake)
            pin = digest(raw)
            key = store.publication_intake_key(org, project_id, drawing, pin)
            backend.put_if_absent_or_verify(key, raw)
            request_id = uuid5(NAMESPACE_URL, f"project-proof:{plan.run_id}:custody:{label}")
            with connection(environment, deadline, read_only=False) as conn:
                version = store.publish_version(org, project_id, drawing, expected_parent_version_id=parent,
                    actor_binding_id=actor, expected_fence=lease.fence, request_id=request_id,
                    intake_sha256=pin, conn=conn, before_new=lambda: graph.project._load_intake(org, key, pin))
            parent = version.version_id
            before = inventory()
            if label == "control":
                control = run(parent)
                _require(control["tool"] == "solar-string-data" and control["input_version_id"] == str(parent))
            else:
                reason = None
                try:
                    run(parent)
                except Exception as exc:
                    reason = getattr(exc, "reason_code", None) or getattr(exc, "code", None)
                _require(reason == REFUSALS[label] and inventory() == before)
        after_all = inventory()
        _require(after_all[1] == before_all[1] + 4)
    finally:
        with connection(environment, deadline, read_only=False) as conn:
            store.release_checkout(org, project_id, drawing, actor_binding_id=actor, expected_fence=lease.fence, conn=conn)
    return dict(run_id=plan.run_id, organization_id=plan.organization_id, project_id=record["project_id"],
        source_version_id=record["source_version_id"], input_version_id=record["input_version_id"], groups_job_id=record["groups_job_id"],
        frame_count=record["frame_count"], request_sha256=record["request_sha256"], output_sha256=record["output_sha256"],
        refusals=dict(REFUSALS), control_read_passed=True, inventories_unchanged=True)


def close_replay(owner):
    """Uncertainty retains private material for the guardian; it never means success."""
    stopped = True
    for resource in reversed(owner.resources):
        try:
            if resource.stop(owner.deadline()) is not True:
                stopped = False
        except BaseException:
            stopped = False
    if not stopped:
        return False
    try:
        return owner.remove_private() is True
    except BaseException:
        return False


def _write_private(path, value):
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(payload)


_FAULT_FIELDS = {
    "capture": {"job_id", "organization_id", "project_id", "worker_id", "attempt", "response_sha256"},
    "submission": {"job_id", "status_code", "body_sha256", "idempotency_key_sha256", "organization_id", "project_id"},
    "service": {"request_sha256", "content_length"},
    "decision": {"event", "action"},
    "paused": {"event"}, "discarded": {"event"}, "held": {"event"}, "dropped": {"event"}, "failed": {"event"},
}
_FAULT_ACTIONS = {"continue", "pause", "pass", "discard", "answer", "hold", "drop"}


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def fault_message(plan, kind, seq, **payload):
    _require(kind in _FAULT_FIELDS and set(payload) == _FAULT_FIELDS[kind])
    _require(type(seq) is int and 1 <= seq <= MAX_FAULT_EVENTS * 4)
    value = dict(v=1, run_id=plan.run_id, kind=kind, seq=seq, **payload)
    value["mac"] = hmac.new(plan.token.encode(), _canonical_json(value), hashlib.sha256).hexdigest()
    raw = _canonical_json(value)
    validate_fault_message(plan, raw)
    return raw


def validate_fault_message(plan, raw):
    _require(type(raw) is bytes and 0 < len(raw) <= 4096)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json)
    except (ValueError, UnicodeError, RecursionError):
        raise ReplayRefused() from None
    _require(type(value) is dict and type(value.get("kind")) is str and value["kind"] in _FAULT_FIELDS)
    kind = value["kind"]
    _require(set(value) == {"v", "run_id", "kind", "seq", "mac"} | _FAULT_FIELDS[kind])
    _require(type(value["v"]) is int and value["v"] == 1 and value["run_id"] == plan.run_id
             and type(value["seq"]) is int and 1 <= value["seq"] <= MAX_FAULT_EVENTS * 4 and _hash(value["mac"]))
    unsigned = {key: item for key, item in value.items() if key != "mac"}
    _require(hmac.compare_digest(value["mac"], hmac.new(plan.token.encode(), _canonical_json(unsigned), hashlib.sha256).hexdigest()))
    for key in _FAULT_FIELDS[kind]:
        item = value[key]
        if key.endswith("_id"):
            _require(_id(item))
        if key.endswith("_sha256"):
            _require(_hash(item))
    if kind == "capture":
        _require(type(value["attempt"]) is int and 1 <= value["attempt"] <= 3)
    if kind == "submission":
        _require(type(value["status_code"]) is int and 200 <= value["status_code"] < 300)
    if kind == "service":
        _require(type(value["content_length"]) is int and 1 <= value["content_length"] <= 65536
                 and value["request_sha256"] in plan.fixture_map)
    if "event" in value:
        _require(type(value["event"]) is str and re.fullmatch(
            r"event-(?:app|sizing-worker|listener)-[1-9][0-9]*-[1-9][0-9]*", value["event"]) is not None)
    if kind == "decision":
        _require(type(value["action"]) is str and value["action"] in _FAULT_ACTIONS)
    return value


def write_fault_message(path, raw):
    _require(type(raw) is bytes and len(raw) <= 4096 and path.suffix == ".json")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_fault_message(plan, path):
    _require(path.parent == plan.run_root / "project-replay/faults" and path.suffix == ".json")
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_size <= 4096
             and (os.name == "nt" or not info.st_mode & 0o077))
    with path.open("rb") as stream:
        return validate_fault_message(plan, stream.read(4097))


class FaultChannel:
    def __init__(self, plan, role, *, clock=time.monotonic, wait=time.sleep, pid=None):
        _require(role in {"app", "sizing-worker", "listener", "supervisor"})
        self.plan, self.role, self.clock, self.wait = plan, role, clock, wait
        self.directory = plan.run_root / "project-replay/faults"
        self.pid, self.seq, self.decisions = pid or os.getpid(), 0, {}
        self.lock = threading.RLock()

    def event(self, kind, **payload):
        with self.lock:
            self.seq += 1
            _require(self.seq <= MAX_FAULT_EVENTS)
            stem = f"event-{self.role}-{self.pid}-{self.seq}"
            write_fault_message(self.directory / (stem + ".json"), fault_message(self.plan, kind, self.seq, **payload))
            return stem

    def decide(self, event, action):
        with self.lock:
            self.seq += 1
            write_fault_message(self.directory / ("decision-" + event + ".json"),
                                fault_message(self.plan, "decision", self.seq, event=event, action=action))

    def decision(self, event, allowed):
        deadline = self.clock() + FAULT_DECISION_TIMEOUT_S
        path = self.directory / ("decision-" + event + ".json")
        while self.clock() < deadline:
            if path.exists():
                value = read_fault_message(self.plan, path)
                _require(value["kind"] == "decision" and value["event"] == event)
                previous = self.decisions.get(event, 0)
                if value["seq"] > previous:
                    _require(value["action"] in allowed)
                    self.decisions[event] = value["seq"]
                    return value["action"]
                _require(value["seq"] == previous)
            self.wait(0.05)
        raise ReplayRefused()


def capture_handshake(channel, payload, *, exit_process=os._exit):
    try:
        event = channel.event("capture", **payload)
        if channel.decision(event, {"continue", "pause"}) == "continue":
            return
        channel.event("paused", event=event)
        deadline = channel.clock() + 120.0
        while channel.clock() < deadline:
            channel.wait(min(0.05, deadline - channel.clock()))
    except Exception:
        pass
    exit_process(70)
    raise ReplayRefused()


_ABORT_RESPONSE = ContextVar("project_proof_abort_response", default=False)


class _DiscardResponse(ConnectionAbortedError):
    pass


def patch_uvicorn_abort(cycle):
    original = cycle.send_500_response
    async def send_500_response(self):
        if _ABORT_RESPONSE.get():
            self.transport.abort()
            return
        return await original(self)
    cycle.send_500_response = send_500_response


async def _async_decision(channel, event, allowed):
    import asyncio
    return await asyncio.to_thread(channel.decision, event, allowed)


async def _receive_before(receive, deadline):
    import asyncio
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return await asyncio.wait_for(receive(), timeout=remaining)


def channel_submission_wrapper(app, channel, *, decide=_async_decision):
    async def wrapped(scope, receive, send):
        if scope.get("type") == "http":
            # Chromium transparently resends a POST whose REUSED socket closes before any response byte, which
            # would hide a discarded response from the controller (measured on the mini, 10-31). Close every
            # proof-host connection after its response so no socket is ever reused.
            inner_send = send

            async def send(message):
                if message.get("type") == "http.response.start":
                    message = dict(message, headers=[*[(k, v) for k, v in message.get("headers", [])
                                                        if k.lower() != b"connection"], (b"connection", b"close")])
                return await inner_send(message)
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/api/run":
            return await app(scope, receive, send)
        messages, body = [], bytearray()
        receive_deadline = time.monotonic() + 30.0
        complete = False
        try:
            while len(messages) < 1024:
                message = await _receive_before(receive, receive_deadline)
                messages.append(message)
                if message.get("type") != "http.request":
                    break
                body.extend(message.get("body", b""))
                complete = not message.get("more_body", False)
                if len(body) > 1048576 or complete:
                    break
        except TimeoutError:
            _ABORT_RESPONSE.set(True)
            raise _DiscardResponse("project proof request timed out") from None
        replay_index = 0
        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(messages):
                message = messages[replay_index]
                replay_index += 1
                return message
            return await receive()
        try:
            parsed = json.loads(body) if complete and len(body) <= 1048576 else None
            sizing = type(parsed) is dict and parsed.get("tool") == TOOL
        except (ValueError, UnicodeError, RecursionError):
            sizing = False
        if not sizing:
            return await app(scope, replay_receive, send)
        held = []
        async def hold(message):
            held.append(message)
            _require(sum(len(item.get("body", b"")) for item in held) <= 1048576 and len(held) <= 256)
        event = None
        try:
            await app(scope, replay_receive, hold)
            start = held[0]
            if start.get("type") != "http.response.start" or not 200 <= start.get("status", 0) < 300:
                for message in held:
                    await send(message)
                return
            data = json.loads(b"".join(message.get("body", b"") for message in held[1:]))
            def header(name):
                values = [value for key, value in scope.get("headers", []) if key.lower() == name]
                _require(len(values) == 1)
                return values[0].decode("ascii")
            event = channel.event("submission", job_id=data["job_id"], status_code=start["status"],
                body_sha256=digest(bytes(body)), idempotency_key_sha256=digest(header(b"idempotency-key").encode()),
                organization_id=header(b"x-org-id"), project_id=header(b"x-project-id"))
            # The private channel's filesystem wait must not block other ASGI requests.
            action = await decide(channel, event, {"pass", "discard"})
            if action == "discard":
                channel.event("discarded", event=event)
                _ABORT_RESPONSE.set(True)
                raise _DiscardResponse("project proof response discarded")
            for message in held:
                await send(message)
        except _DiscardResponse:
            raise
        except Exception:
            if event is not None:
                channel.event("failed", event=event)
            _ABORT_RESPONSE.set(True)
            raise _DiscardResponse("project proof response refused") from None
    return wrapped


def install_app_seams(plan, modules):
    catalog = modules.catalog
    catalog.CATALOG_URL = plan.endpoint + "/modules"
    def resolve(grant_ref, organization_id):
        if grant_ref != GRANT_REF or organization_id != plan.organization_id:
            raise modules.grants.CloudError("cloud_tenant_unauthorized", 403)
        return modules.grants.CloudGrant(plan.organization_id, plan.token)
    @contextmanager
    def get(url, **kwargs):
        _require(url == catalog.CATALOG_URL and kwargs.get("allow_redirects") is False
                 and kwargs.get("headers", {}).get("Authorization") == "Bearer " + plan.token)
        with modules.requests.Session() as session:
            session.trust_env = False
            with session.get(url, **kwargs) as response:
                yield response
    catalog.cloud = SimpleNamespace(resolve_grant=resolve, CloudError=modules.grants.CloudError)
    catalog.requests = SimpleNamespace(get=get, Timeout=modules.requests.Timeout,
                                       RequestException=modules.requests.RequestException)


def install_capture_seam(plan, graph, channel):
    exchanges = graph._exchanges()
    local = threading.local()
    original = exchanges.capture_response
    def binding_for(scope, lease):
        local.scope, local.lease = scope, lease
        return FaultBinding(plan.run_id, str(scope.org_id), str(scope.project_id), str(scope.job_id))
    def committed(scope, lease):
        with exchanges.db.connection() as conn:
            exchange = exchanges.get_exchange(scope, lease, conn=conn)
            local.exchange = exchange
            return (exchange is not None and exchange["state"] == "response_stored"
                and exchange["sender_worker"] == lease.worker_id and exchange["sender_attempt"] == lease.attempt
                and digest(exchange["response_bytes"]) == exchange["response_sha256"])
    def pause(binding, worker_id):
        capture_handshake(channel, dict(job_id=binding.job_id, organization_id=binding.organization_id,
            project_id=binding.project_id, worker_id=worker_id, attempt=local.lease.attempt,
            response_sha256=local.exchange["response_sha256"]))
    exchanges.capture_response = capture_response_hook(original, binding_for=binding_for, committed=committed, pause=pause)


def read_private_plan(environment):
    root = Path(environment.get("LEAF_STORE_DIR", "")).parent
    path = Path(environment.get(CONFIG_KEY, ""))
    _require(path == root / "project-replay/private.json" and path.resolve() == path)
    try:
        info = os.lstat(path)
    except OSError:
        raise ReplayRefused() from None
    _require(stat.S_ISREG(info.st_mode) and (os.name == "nt" or not info.st_mode & 0o077))
    _require(info.st_size <= 262144)
    raw = path.read_bytes()
    _require(digest(raw) == environment.get(CONFIG_HASH_KEY))
    value = json.loads(raw)
    _require(set(value) == {"run_id", "run_root", "root_identity", "organization_id", "endpoint",
                           "source_sha256", "source_files", "token"})
    owner = json.loads((root / "ownership.json").read_bytes())
    _require(owner.get("run_id") == value["run_id"] and owner.get("run_root") == str(root))
    _require(value["run_root"] == str(root))
    return prepare_replay(**{**{k: v for k, v in value.items() if k != "token"},
                            "source_files": tuple(map(tuple, value["source_files"]))},
                          environment=environment, token_factory=lambda: value["token"])


class ReplayRuntime:
    """Production capabilities are lazy; tests inject a runtime instead."""
    def __init__(self, prepared, effects, scope, supervisor):
        self.prepared, self.effects, self.scope, self.p = prepared, effects, scope, supervisor
        self.resources, self.created = [], []
        self.plan = None
        self.worker = self.worker_identity = self.descriptor = None
        self.generation = 0
        self.auxiliary = []
        self.last_heartbeats = {}
        self.pending, self.seen, self.writer_sequences = {}, set(), {}
        self.jobs, self.submissions, self.facts = [], {}, {}
        self.schedule = self.channel = None
        self.discard_ack = self.retry_event = None
        self.stopped_for_drop = False

    def __repr__(self):
        return "<owned project replay>"

    def deadline(self):
        return self.effects.clock.monotonic() + self.p.CLEANUP_TIMEOUT_S

    def remove_private(self):
        for path in reversed(self.created):
            if path.is_dir():
                if path.name == "faults":
                    for child in path.iterdir():
                        _require(child.is_file() and not child.is_symlink() and child.suffix in {".json", ".tmp"})
                        child.unlink()
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        self.created.clear()
        return True

    def start(self, environment, deadline):
        # This is reached only by the managed supervisor, never at import time.
        import socket
        prepared, p = self.prepared, self.p
        plan = prepared.plan
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        files = {item.path: item.sha256 for item in plan.source.files}
        self.plan = prepare_replay(run_id=plan.config.run_id, run_root=plan.config.run_root,
            root_identity=plan.reservation.root_identity, organization_id=prepared.binding.organization_id,
            endpoint=f"http://127.0.0.1:{port}/string-length",
            source_sha256=plan.source.tested_source_manifest_sha256,
            source_files=tuple((name, files[name]) for name in PIN_FILES), environment=environment)
        private = getattr(self.scope, "private_values", None)
        if private is not None:
            private.add(self.plan.token)
        directory = plan.config.run_root / "project-replay"
        p._private_directory(directory)
        self.created.append(directory)
        faults = directory / "faults"
        p._private_directory(faults)
        self.created.append(faults)
        self.schedule = FaultSchedule(self.plan)
        self.channel = FaultChannel(self.plan, "supervisor")
        config = directory / "private.json"
        value = {name: getattr(self.plan, name) for name in ("run_id", "organization_id", "endpoint",
                  "root_identity", "source_sha256", "source_files", "token")}
        value["run_root"] = str(self.plan.run_root)
        self.created.append(config)
        pin = _write_private(config, value)
        self.environment = dict(environment, **{CONFIG_KEY: str(config), CONFIG_HASH_KEY: pin})
        ready = directory / "ready.json"
        self.created.append(ready)
        self.created.append(directory / "requests.jsonl")
        listener = self.effects.process_launcher(p.ProcessSpec("sizing-replay",
            (sys.executable, "-B", str(Path(__file__).resolve()), "listener"), REPO,
            self.environment, deadline), scope=self.scope)
        self.resources.append(listener)
        while not ready.exists():
            _require(listener.poll() is None and self.effects.clock.monotonic() < deadline)
            self.effects.clock.wait(0.05)
        _require(json.loads(ready.read_bytes()) == self.plan.public())
        self.descriptor = self.effects.canonical_reader(p.CanonicalRequest(tool_name=TOOL))
        _require(self.descriptor.tool == TOOL and _hash(self.descriptor.source_sha256)
                 and self.descriptor.source_revision == "sha256:" + self.descriptor.source_sha256)
        for role, tool, label in (("panels-worker", "solar-panels-from-drawing", "panels"),
                                  ("groups-worker", "solar-panel-groups", "groups")):
            descriptor = self.effects.canonical_reader(p.CanonicalRequest(tool_name=tool))
            _require(descriptor.tool == tool and _hash(descriptor.source_sha256)
                     and descriptor.source_revision == "sha256:" + descriptor.source_sha256)
            worker_id = f"project:{self.plan.run_id}:{label}:1"
            argv = tuple(_child_argv(role, ("--tool", tool, "--readiness", "--worker-id", worker_id)))
            child = self.effects.process_launcher(p.ProcessSpec(role, argv, REPO / "server", self.environment, deadline), scope=self.scope)
            self.resources.append(child)
            identity = child.identity
            _require(identity.role == role and identity.start_id)
            self.auxiliary.append((child, identity, worker_id, tool, descriptor))
        return self.environment

    def launcher_argv(self, argv):
        return (sys.executable, "-B", str(Path(__file__).resolve()), "launcher", *argv[3:])

    def launch_worker(self, deadline):
        self.generation += 1
        self.worker_id = f"project:{self.plan.run_id}:sizing:{self.generation}"
        argv = tuple(_child_argv("sizing-worker", ("--tool", TOOL, "--readiness", "--worker-id", self.worker_id)))
        self.worker = self.effects.process_launcher(self.p.ProcessSpec("sizing-worker", argv, REPO / "server",
            self.environment, deadline), scope=self.scope)
        self.resources.append(self.worker)
        self.worker_identity = self.worker.identity
        _require(self.worker_identity.role == "sizing-worker" and self.worker_identity.start_id)
        return self.worker

    def _worker_heartbeat(self, child, identity, worker_id, tool, descriptor, deadline, ready_s, *, loop_only=False):
        now = self.effects.clock.monotonic()
        _require(child.identity == identity and child.poll() is None)
        cached = self.last_heartbeats.get(worker_id)
        if loop_only and cached is not None and now - cached[0] < 2.0:
            return cached[1]
        ready_deadline = min(deadline, now + ready_s)
        while True:
            _require(child.identity == identity and child.poll() is None)
            _require(self.effects.clock.monotonic() < ready_deadline)
            beat = self.effects.canonical_reader(self.p.CanonicalRequest("heartbeat", tool, self.environment,
                deadline=ready_deadline, worker_id=worker_id))
            if beat is not None:
                break
            self.effects.clock.wait(min(2.0, max(0, ready_deadline - self.effects.clock.monotonic())))
        d = descriptor
        _require(type(beat) is self.p.HeartbeatProof and (beat.worker_id, beat.tool_name, beat.runtime,
            beat.source_revision, beat.source_sha256) == (worker_id, tool, d.runtime, d.source_revision, d.source_sha256))
        age = (self.effects.clock.utc_now() - beat.observed_at).total_seconds()
        _require(0 <= age <= self.p.HEARTBEAT_MAX_AGE_S)
        self.last_heartbeats[worker_id] = (self.effects.clock.monotonic(), beat)
        return beat

    def heartbeat(self, deadline, *, ready_s=5.0):
        return self._heartbeat(deadline, ready_s=ready_s, loop_only=False)

    def loop_heartbeat(self, deadline):
        return self._heartbeat(deadline, ready_s=5.0, loop_only=True)

    def _heartbeat(self, deadline, *, ready_s, loop_only):
        beat = None
        if self.stopped_for_drop:
            _require(self.generation == 2 and self.schedule.active is not None
                     and self.schedule.active.scenario == SCENARIOS[2]
                     and any(kind == "held" for kind, index, payload in self.pending.values()))
        else:
            beat = self._worker_heartbeat(self.worker, self.worker_identity, self.worker_id, TOOL,
                                          self.descriptor, deadline, ready_s, loop_only=loop_only)
        for child, identity, worker_id, tool, descriptor in self.auxiliary:
            self._worker_heartbeat(child, identity, worker_id, tool, descriptor, deadline, 5.0, loop_only=loop_only)
        return beat

    def recover(self, barrier, binding, *, committed, captured_identity, deadline):
        _require(_same_plan(barrier.schedule.plan, self.plan))
        _require(barrier.binding == binding)
        _require(binding.run_id == self.plan.run_id and binding.organization_id == self.plan.organization_id)
        old, old_id = self.worker, self.worker_id
        _require(old.identity == self.worker_identity)
        def validate(child, captured):
            if child.identity == captured or self.worker_id == old_id or child is old:
                return False
            self.heartbeat(deadline, ready_s=REPLACEMENT_READY_S)
            return True
        # launch_worker uses the supervisor's launcher: scope and guardian own
        # the new process before its first instruction, including bad identity.
        return barrier.capture_response(binding, committed=committed, captured_worker=captured_identity,
            current_worker=self.worker_identity, stop=lambda identity: old.stop(deadline),
            launch_replacement=lambda: self.launch_worker(deadline),
            register=lambda child: _require(child in self.resources), validate_identity=validate)

    def service_posts(self):
        path = self.plan.run_root / "project-replay/requests.jsonl"
        if not path.exists():
            return 0
        _require(path.lstat().st_size <= 65536 and not path.is_symlink())
        lines = path.read_bytes().splitlines()
        _require(len(lines) <= 3)
        for line in lines:
            value = json.loads(line, object_pairs_hook=_unique_json)
            _require(set(value) == {"request_sha256", "response_sha256", "classification", "endpoint_sha256", "fixture_sha256"})
            fixture = self.plan.fixture_map.get(value["request_sha256"])
            _require(fixture is not None and value["response_sha256"] == fixture[2]
                     and value["classification"] == "synthetic" and value["fixture_sha256"] == self.plan.fixture_sha256
                     and value["endpoint_sha256"] == digest(self.plan.endpoint.encode()))
        return len(lines)

    def observe(self, query, deadline):
        value = self.effects.canonical_reader(self.p.CanonicalRequest("replay-observe", TOOL, self.environment,
            deadline=deadline, sizing_attachment=query))
        validate_observation(value)
        _require(value["organization_id"] == self.plan.organization_id and value["tool"] == TOOL)
        return value

    def _bound(self, value, index):
        _require(0 <= index < 3 and value["job_id"] == self.jobs[index])
        suffix = ("recovery", "submission", "service")[index]
        _require(value["project_name"] == f"PP3B {self.plan.run_id} sizing {suffix}")
        return value

    def _arm(self, scenario, observed):
        return arm_scenario(self.schedule, scenario, run_id=self.plan.run_id,
            organization_id=observed["organization_id"], project_id=observed["project_id"], job_id=observed["job_id"])

    def _decision(self, stem, action, observed):
        _require(observed["organization_id"] == self.plan.organization_id)
        self.channel.decide(stem, action)

    def drive(self, deadline):
        _require(self.effects.clock.monotonic() < deadline)
        self._settle_retry(deadline)
        directory = self.plan.run_root / "project-replay/faults"
        events = list(directory.glob("event-*.json"))
        _require(len(events) <= MAX_FAULT_EVENTS)
        # Per-writer sequence order; app admission must be observed before service/capture.
        candidates = []
        for path in events:
            if path.stem in self.seen:
                continue
            match = re.fullmatch(r"event-(app|sizing-worker|listener)-([1-9][0-9]*)-([1-9][0-9]*)", path.stem)
            _require(match is not None)
            candidates.append(({"app": 0, "listener": 1, "sizing-worker": 2}[match[1]], int(match[3]), path, match))
        for _, _, path, match in sorted(candidates, key=lambda item: (item[0], item[1]))[:32]:
            value = read_fault_message(self.plan, path)
            _require(value["kind"] != "decision" and value["seq"] == int(match[3]))
            kind = value["kind"]
            expected_role = {"capture": "sizing-worker", "paused": "sizing-worker", "submission": "app",
                             "discarded": "app", "service": "listener", "held": "listener", "dropped": "listener"}.get(kind)
            _require(expected_role == match[1])
            writer = match[1] + "-" + match[2]
            _require(value["seq"] == self.writer_sequences.get(writer, 0) + 1)
            if self._drive_event(path.stem, value, deadline) is False:
                continue
            self.writer_sequences[writer] = value["seq"]
            self.seen.add(path.stem)
        self._settle_retry(deadline)

    def _settle_retry(self, deadline):
        if self.retry_event is not None and self.schedule.active is not None:
            _require(self.schedule.active.scenario == SCENARIOS[1])
            observed = self._bound(self.observe({"job_id": self.jobs[1]}, deadline), 1)
            if observed["exchange_state"] == "published":
                _require(self.service_posts() == 2)
                self.facts[SCENARIOS[1]] = dict(job_id=self.jobs[1], submissions=2, same_submission=True, service_posts=2)
                self.schedule.active.complete()
                self.retry_event = None

    def _drive_event(self, stem, value, deadline):
        kind = value["kind"]
        if kind == "submission":
            job = value["job_id"]
            if job not in self.jobs:
                self._settle_retry(deadline)
                index = len(self.jobs)
                # An admitted successor can reach the channel before this loop has observed the previous
                # publication. Leave it pending until that independently observed settlement completes.
                if index == 2 and self.schedule.next_index == 1 and self.retry_event is not None:
                    return False
                _require(index < 3 and self.schedule.next_index == index and self.schedule.active is None)
                observed = self.observe({"job_id": job}, deadline)
                self.jobs.append(job)
                self._bound(observed, index)
                _require(value["organization_id"] == observed["organization_id"] and value["project_id"] == observed["project_id"])
                self.submissions[job] = value
                if index == 1:
                    barrier = self._arm(SCENARIOS[1], observed)
                    barrier.consume(barrier.binding)
                    self.pending[stem] = (kind, index, value)
                    self._decision(stem, "discard", observed)
                else:
                    self._decision(stem, "pass", observed)
            else:
                index = self.jobs.index(job)
                _require(index == 1 and self.discard_ack is not None and self.retry_event is None
                         and SCENARIOS[1] not in self.facts)
                observed = self._bound(self.observe({"job_id": job}, deadline), 1)
                original = self.submissions[job]
                _require(all(value[key] == original[key] for key in ("organization_id", "project_id", "body_sha256", "idempotency_key_sha256")))
                self._decision(stem, "pass", observed)
                self.retry_event = stem
        elif kind == "capture":
            if value["job_id"] not in self.jobs:
                return False
            index = self.jobs.index(value["job_id"])
            observed = self._bound(self.observe({"job_id": value["job_id"]}, deadline), index)
            _require(observed["exchange_state"] == "response_stored" and observed["sender_worker"] == value["worker_id"]
                     and observed["sender_attempt"] == value["attempt"] and observed["response_sha256"] == value["response_sha256"]
                     and value["organization_id"] == observed["organization_id"] and value["project_id"] == observed["project_id"])
            if index == 0:
                _require(self.generation == 1 and value["worker_id"] == self.worker_id and value["attempt"] == 1)
                self._arm(SCENARIOS[0], observed)
                self.pending[stem] = (kind, index, value)
                self._decision(stem, "pause", observed)
            else:
                _require(index == 1 and self.generation == 2 and value["worker_id"] == self.worker_id)
                self._decision(stem, "continue", observed)
        elif kind == "service":
            if not self.jobs:
                return False
            observed = self.observe({"query": "send_started"}, deadline)
            if observed["job_id"] not in self.jobs:
                return False
            index = self.jobs.index(observed["job_id"])
            self._bound(observed, index)
            _require(observed["exchange_state"] == "send_started" and observed["sender_worker"] == self.worker_id
                     and observed["sender_attempt"] == 1 and value["request_sha256"] in self.plan.fixture_map)
            if index == 2:
                _require(self.generation == 2)
                barrier = self._arm(SCENARIOS[2], observed)
                barrier.consume(barrier.binding)
                self.pending[stem] = (kind, index, value)
                self._decision(stem, "hold", observed)
            else:
                self._decision(stem, "answer", observed)
        else:
            original = value["event"]
            _require(original in self.pending)
            primary, index, payload = self.pending[original]
            observed = self._bound(self.observe({"job_id": self.jobs[index]}, deadline), index)
            if kind == "paused":
                _require(primary == "capture" and index == 0 and observed["exchange_state"] == "response_stored"
                         and observed["sender_worker"] == self.worker_id and observed["sender_attempt"] == 1)
                old_id = self.worker_id
                barrier = self.schedule.active
                self.recover(barrier, barrier.binding, committed=True, captured_identity=self.worker_identity, deadline=deadline)
                _require(self.generation == 2 and self.service_posts() == 1)
                self.facts[SCENARIOS[0]] = dict(job_id=self.jobs[0], sender_worker_id=old_id,
                                              published_worker_id=self.worker_id, service_posts=1)
                del self.pending[original]
            elif kind == "discarded":
                _require(primary == "submission" and index == 1 and self.discard_ack is None)
                self.discard_ack = original
                del self.pending[original]
            elif kind == "held":
                _require(primary == "service" and index == 2 and observed["exchange_state"] == "send_started"
                         and observed["sender_worker"] == self.worker_id and self.generation == 2)
                _require(self.worker.identity == self.worker_identity and self.worker.stop(deadline) is True)
                self.stopped_for_drop = True
                self._decision(original, "drop", observed)
                self.pending[original] = ("held", index, payload)
            elif kind == "dropped":
                _require(primary == "held" and index == 2 and self.service_posts() == 3)
                old, old_identity, old_id = self.worker, self.worker_identity, self.worker_id
                child = self.launch_worker(deadline)
                _require(child is not old and child.identity != old_identity and self.worker_id != old_id)
                self.stopped_for_drop = False
                self.heartbeat(deadline, ready_s=REPLACEMENT_READY_S)
                _require(self.generation == 3)
                self.facts[SCENARIOS[2]] = dict(job_id=self.jobs[2], sender_worker_id=old_id,
                    reclaim_worker_id=self.worker_id, service_posts=3)
                self.schedule.active.complete()
                del self.pending[original]
            else:
                raise ReplayRefused()
        return True

    def complete(self):
        _require(self.service_posts() == 3)
        self.facts["service_posts_final"] = 3
        return (self.schedule.next_index == 3 and self.schedule.active is None and not self.pending
                and not self.stopped_for_drop and len(self.jobs) == 3 and set(self.facts) == {*SCENARIOS, "service_posts_final"}
                and all(path.stem in self.seen for path in (self.plan.run_root / "project-replay/faults").glob("event-*.json")))

    def close(self):
        return close_replay(self)


def _listener_main(environment, *, fault_driver=None, record_request=None):
    """Only POST /string-length exists; there is deliberately no fault-control route."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    plan = read_private_plan(environment)
    raw_catalog = (REPO / CATALOG).read_bytes()
    _require(digest(raw_catalog) == CATALOG_SHA256)
    catalog = json.loads(raw_catalog)["response"]
    if fault_driver is None and record_request is None:
        channel = FaultChannel(plan, "listener")
        def fault_driver(plan, body, length, record, close):
            try:
                event = channel.event("service", request_sha256=digest(body), content_length=length)
                if channel.decision(event, {"answer", "hold"}) == "answer":
                    return False
                channel.event("held", event=event)
                _require(channel.decision(event, {"drop"}) == "drop")
                record(digest(body))
                close()
                channel.event("dropped", event=event)
                return True
            except Exception:
                close()
                return True
    count = 0
    def record(request_sha256):
        nonlocal count
        _require(count < 1024)
        count += 1
        fixture = plan.fixture_map[request_sha256]
        event = dict(request_sha256=request_sha256, response_sha256=fixture[2],
                     classification="synthetic", endpoint_sha256=digest(plan.endpoint.encode()),
                     fixture_sha256=plan.fixture_sha256)
        if record_request is not None:
            record_request(event)
        else:
            descriptor = os.open(plan.run_root / "project-replay/requests.jsonl",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((json.dumps(event, sort_keys=True) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            from urllib.parse import urlsplit, parse_qs
            from copy import deepcopy
            self.close_connection = True
            parts = urlsplit(self.path)
            _require(parts.path == "/string-length/modules" and not parts.fragment)
            _require(self.headers.get_all("Authorization") == ["Bearer " + plan.token])
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
            _require(set(query) == {"q", "limit"} and all(len(values) == 1 for values in query.values()))
            q, limit = query["q"][0], query["limit"][0]
            tokens = re.findall(r"[a-z0-9]+", q.lower())
            _require(2 <= len(q) <= 64 and 1 <= len(tokens) <= 6 and re.fullmatch(r"[0-9]{1,2}", limit) is not None
                     and 1 <= int(limit) <= 50)
            result = deepcopy(catalog)
            module = result["modules"][0]
            result["modules"] = [module] if all(token in module["key"].lower() for token in tokens) else []
            result["truncated"] = False
            raw = _canonical_json(result)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            self.close_connection = True
            self.connection.settimeout(5)
            _require(self.path == "/string-length")
            _require(self.headers.get_all("Authorization") == ["Bearer " + plan.token])
            _require(self.headers.get_all("Content-Type") == ["application/json"])
            _require(self.headers.get_all("Transfer-Encoding") is None)
            lengths = self.headers.get_all("Content-Length", [])
            _require(len(lengths) == 1 and re.fullmatch(r"[0-9]{1,5}", lengths[0]) is not None)
            length = int(lengths[0])
            _require(0 < length <= 65536)
            body = self.rfile.read(length)
            _require(len(body) == length)
            fixture = plan.fixture_map.get(digest(body))
            _require(fixture is not None)
            def close():
                try:
                    self.connection.shutdown(2)  # The killed sender's peer may already be gone (ENOTCONN).
                except OSError:
                    pass
                self.connection.close()
                return True
            # A private F2 driver may consume the armed service-drop barrier.
            # The HTTP client has no way to select or arm this callable.
            if fault_driver is not None and fault_driver(plan, body, length, record, close) is True:
                return
            record(digest(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(fixture[1])))
            self.send_header("X-Leaf-Synthetic-Replay", "synthetic")
            self.end_headers()
            self.wfile.write(fixture[1])

    class Server(HTTPServer):
        def handle_error(self, request, address):
            pass  # Never emit private handler frames, bodies or authorization.

    port = int(plan.endpoint.split(":")[2].split("/")[0])
    with Server(("127.0.0.1", port), Handler) as server:
        _write_private(plan.run_root / "project-replay/ready.json", plan.public())
        server.serve_forever(poll_interval=0.1)


def main():
    import runpy
    try:
        mode, *args = sys.argv[1:]
        environment = dict(os.environ)
        if mode == "listener" and not args:
            return _listener_main(environment)
        plan = read_private_plan(environment)
        if mode == "launcher":
            spec = importlib.util.spec_from_file_location("_project_replay_launcher", REPO / "scripts/start-leaf.py")
            launcher = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = launcher
            spec.loader.exec_module(launcher)
            sys.argv = [str(REPO / "scripts/start-leaf.py"), *args]
            return run_replay_launcher(plan, launcher, environment)
        _require(mode == "child" and bool(args))
        role, *child_args = args
        _require(role in {"app", "broker", "worker", "sizing-worker", "panels-worker", "groups-worker"})
        if role in {"app", "broker"}:
            _require(not child_args)
            entry = role + ".py"
        else:
            validate_worker_argv(plan, role, child_args)
            entry = "canonical_worker.py"
        child = bootstrap_child(plan, role, environment)
        os.environ.clear()
        os.environ.update(child)
        sys.argv = [str(REPO / "server" / entry), *child_args]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    except Exception:
        return 1


def validate_worker_argv(plan, role, args):
    roles = {"worker": ("solar-settings", ""), "sizing-worker": (TOOL, ":sizing:"),
             "panels-worker": ("solar-panels-from-drawing", ":panels:"),
             "groups-worker": ("solar-panel-groups", ":groups:")}
    _require(role in roles)
    tool, suffix = roles[role]
    _require(len(args) == 5 and list(args[:4]) == ["--tool", tool, "--readiness", "--worker-id"])
    prefix = "project:" + plan.run_id + suffix
    _require(args[4] == prefix if role == "worker" else
             re.fullmatch(re.escape(prefix) + (r"[1-3]" if role == "sizing-worker" else "1"), args[4]) is not None)


if __name__ == "__main__":
    raise SystemExit(main())
