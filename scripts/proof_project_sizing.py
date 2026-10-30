"""Private project-proof replay custody. Importing this module has no effects.

F1 supplies plans and barriers; F2 drives them from the managed project walk.
Nothing here provisions an account grant or exposes an HTTP control route.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
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
from types import MappingProxyType, SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
MANIFEST = "scripts/fixtures/string_sizer_replay_manifest.json"
MANIFEST_SHA256 = "2b467b2257225849a05b413af581c92c70e9e7ddc0b893c0d3779c8a479bc53e"
FIXTURE = "server/tests/fixtures/w1_string_length_recorded_response.json"
FIXTURE_SHA256 = "6e807695b9b36bbaf9007f3322d3570ea748853c746c33dcefcfdfe07c1c299a"
TOOL = "solar-size-strings"
GRANT_REF = "project-replay-fixture"
CONFIG_KEY = "LEAF_PROJECT_REPLAY_CONFIG"
CONFIG_HASH_KEY = "LEAF_PROJECT_REPLAY_CONFIG_SHA256"
SCENARIOS = ("capture-recovery", "browser-response-discard", "service-response-drop")
PIN_FILES = (MANIFEST, FIXTURE, "scripts/proof_project_sizing.py", "scripts/proof_project.py",
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
    _require(dict(pins)[MANIFEST] == MANIFEST_SHA256 and dict(pins)[FIXTURE] == FIXTURE_SHA256)
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
    _require(role in {"app", "broker", "worker", "sizing-worker", "canonical"})
    _validate_plan(plan, environment, read=read, stat_root=stat_root, load_manifest=load_manifest)
    child = dict(environment)
    child["DATABASE_URL"] = environment["DATABASE_URL"]  # DROP_PROJECT_DSN
    child["LEAF_AUTH_LIVE"] = "1"
    if modules is None:
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


def submission_asgi_wrapper(app, *, barrier, binding_for, admitted):
    """F2 selects the real sizing submission scope; all admission remains in app."""
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

    def __repr__(self):
        return "<owned project replay>"

    def deadline(self):
        return self.effects.clock.monotonic() + self.p.CLEANUP_TIMEOUT_S

    def remove_private(self):
        for path in reversed(self.created):
            if path.is_dir():
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

    def heartbeat(self, deadline):
        ready_deadline = min(deadline, self.effects.clock.monotonic() + 5.0)
        while True:
            _require(self.worker.identity == self.worker_identity and self.worker.poll() is None)
            _require(self.effects.clock.monotonic() < ready_deadline)
            beat = self.effects.canonical_reader(self.p.CanonicalRequest("heartbeat", TOOL, self.environment,
                deadline=ready_deadline, worker_id=self.worker_id))
            if beat is not None:
                break
            self.effects.clock.wait(0.05)
        d = self.descriptor
        _require(type(beat) is self.p.HeartbeatProof and (beat.worker_id, beat.tool_name, beat.runtime,
            beat.source_revision, beat.source_sha256) == (self.worker_id, TOOL, d.runtime, d.source_revision, d.source_sha256))
        age = (self.effects.clock.utc_now() - beat.observed_at).total_seconds()
        _require(0 <= age <= self.p.HEARTBEAT_MAX_AGE_S)
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
            self.heartbeat(deadline)
            return True
        # launch_worker uses the supervisor's launcher: scope and guardian own
        # the new process before its first instruction, including bad identity.
        return barrier.capture_response(binding, committed=committed, captured_worker=captured_identity,
            current_worker=self.worker_identity, stop=lambda identity: old.stop(deadline),
            launch_replacement=lambda: self.launch_worker(deadline),
            register=lambda child: _require(child in self.resources), validate_identity=validate)

    def close(self):
        return close_replay(self)


def _listener_main(environment, *, fault_driver=None, record_request=None):
    """Only POST /string-length exists; there is deliberately no fault-control route."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    plan = read_private_plan(environment)
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
                self.connection.shutdown(2)
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
        child = bootstrap_child(plan, role, environment)
        _require(role in {"app", "broker", "worker", "sizing-worker"})
        if role in {"app", "broker"}:
            _require(not child_args)
            entry = role + ".py"
        else:
            tool = "solar-settings" if role == "worker" else TOOL
            prefix = "project:" + plan.run_id + ("" if role == "worker" else ":sizing:")
            _require(len(child_args) == 5 and child_args[:4] == ["--tool", tool, "--readiness", "--worker-id"])
            _require(child_args[4] == prefix if role == "worker" else
                     re.fullmatch(re.escape(prefix) + r"[1-9][0-9]*", child_args[4]) is not None)
            entry = "canonical_worker.py"
        os.environ.clear()
        os.environ.update(child)
        sys.argv = [str(REPO / "server" / entry), *child_args]
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
