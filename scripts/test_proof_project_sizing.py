"""R14-F1: injected effects only; no sockets, children, database or services."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


s = _load("_r14f1_sizing_under_test", Path(__file__).with_name("proof_project_sizing.py"))
_real_receive_before = s._receive_before
ROOT = Path("C:/fake/r14f1-owned") if sys.platform == "win32" else Path("/fake/r14f1-owned")
TOKEN = "private-fixture-credential-" + "x" * 32


@pytest.fixture(autouse=True)
def no_host_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("R14-F1 attempted a socket or subprocess")
    class DeniedSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            forbidden()
    monkeypatch.setattr(socket, "socket", DeniedSocket)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    async def receive_before(receive, deadline):
        return await receive()
    monkeypatch.setattr(s, "_receive_before", receive_before)
    # requests probes IPv6 by binding a socket during import. The pure parser
    # and product evidence prover need no transport, so inject that dependency.
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(RequestException=RuntimeError, post=forbidden))
    before = dict(sys.modules)
    yield
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).resolve().is_relative_to(s.REPO / "server"):
            if name in before:
                sys.modules[name] = before[name]
            else:
                sys.modules.pop(name, None)


def environment():
    return dict(LEAF_RUNTIME_ENV="local", LEAF_ENV="local", LEAF_PROOF_PROFILE="project",
        LEAF_PROOF_PROJECT_RUN_ID="r14f1-owned", LEAF_STORE_DIR=str(ROOT / "drawings"),
        LEAF_AUTH_LIVE="1", DATABASE_URL="postgresql://private:dsn@127.0.0.1:5555/leaf",
        LEAF_HARNESS_DATABASE_URL="postgresql://private:dsn@127.0.0.1:5555/leaf",
        VITE_TENANT_ID="seeded-org", WEB_PORT="5175")


def inputs():
    return dict(run_id="r14f1-owned", run_root=ROOT, root_identity=(1, 2), organization_id="seeded-org",
        endpoint="http://127.0.0.1:51234/string-length", source_sha256="a" * 64,
        source_files=tuple((name, s.digest((s.REPO / name).read_bytes())) for name in s.PIN_FILES),
        environment=environment(), stat_root=lambda root: (1, 2), token_factory=lambda: TOKEN)


def plan():
    return s.prepare_replay(**inputs())


def modules():
    class CloudError(ValueError):
        def __init__(self, classification, status):
            self.classification, self.status = classification, status
    return SimpleNamespace(client=SimpleNamespace(SIZING_CLASSIFICATION="live"),
        graph=SimpleNamespace(SYNTHETIC_SIZING_PROOF_ACCEPTED=False),
        grants=SimpleNamespace(CloudError=CloudError,
            CloudGrant=lambda org, token: SimpleNamespace(tenant_id=org, access_token=token)),
        requests=SimpleNamespace(RequestException=RuntimeError))


def private_plan(tmp_path, replay=None, env=None):
    root = tmp_path / "owned-run"
    root.mkdir(mode=0o700, parents=True)
    directory = root / "project-replay"
    directory.mkdir(mode=0o700)
    info = root.stat()
    replay = replace(replay or plan(), run_root=root, root_identity=(info.st_dev, info.st_ino))
    env = dict(env or environment(), LEAF_STORE_DIR=str(root / "drawings"))
    (root / "ownership.json").write_text(json.dumps({"run_id": replay.run_id, "run_root": str(root)}), encoding="utf-8")
    value = {name: getattr(replay, name) for name in ("run_id", "organization_id", "endpoint", "root_identity",
             "source_sha256", "source_files", "token")}
    value["run_root"] = str(root)
    path = directory / "private.json"
    pin = s._write_private(path, value)
    env.update({s.CONFIG_KEY: str(path), s.CONFIG_HASH_KEY: pin})
    return replay, env


def boot(tmp_path, replay=None, role="app", env=None, mods=None):
    mods = mods or modules()
    replay, env = private_plan(tmp_path, replay, env)
    return s.bootstrap_child(replay, role, env, modules=mods), mods


def attachment(replay):
    request, fixture = next(iter(replay.fixture_map.items()))
    return dict(run_id=replay.run_id, organization_id=replay.organization_id, project_id="project-one",
        job_id="job-one", output_version_id="output-one", exchange_job_id="job-one", classification="synthetic",
        endpoint_sha256=s.digest(replay.endpoint.encode()), fixture_sha256=replay.fixture_sha256,
        evidence_sha256="e" * 64, request_sha256=request, response_sha256=fixture[2])


def armed(scenario=s.SCENARIOS[0]):
    schedule = s.FaultSchedule(plan())
    for prior in s.SCENARIOS[:s.SCENARIOS.index(scenario)]:
        previous = s.arm_scenario(schedule, prior, run_id=schedule.plan.run_id,
            organization_id=schedule.plan.organization_id, project_id="previous-project", job_id="previous-job")
        previous.consume(previous.binding)
        previous.complete()
    barrier = s.arm_scenario(schedule, scenario, run_id=schedule.plan.run_id,
        organization_id=schedule.plan.organization_id, project_id="project-one", job_id="job-one")
    return schedule, barrier


def test_r14f1_isolated_authority(tmp_path):
    before = environment()
    child, _ = boot(tmp_path, env=before)
    assert child["DATABASE_URL"] == before["DATABASE_URL"]
    assert child["LEAF_HARNESS_DATABASE_URL"] == before["DATABASE_URL"]
    assert child["LEAF_AUTH_LIVE"] == "1" and child["VITE_TENANT_ID"] == "seeded-org"
    assert before == environment()


def test_r14f1_honest_classification(tmp_path):
    replay = plan()
    _, mods = boot(tmp_path, replay)
    assert mods.client.SIZING_CLASSIFICATION == "synthetic"
    assert mods.client.SIZING_URL == replay.endpoint
    assert mods.graph.SYNTHETIC_SIZING_PROOF_ACCEPTED is True
    facts = replay.public()
    assert facts["classification"] == "synthetic"
    assert facts["endpoint_sha256"] == s.digest(mods.client.SIZING_URL.encode())
    assert facts["fixture_sha256"] == s.digest((s.REPO / s.FIXTURE).read_bytes())
    # Execute the real product preparation function with injected storage,
    # request model and evidence builder. The new bootstrap supplies its label.
    import ast
    tree = ast.parse((s.REPO / "server/solar_project_jobs.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_cloud_prepare")
    exchange, labels = {}, []
    def prepare(scope, lease, **reservation):
        exchange.update(reservation, evidence_bytes=None)
    def capture(scope, lease, **response):
        exchange.update(response)
    def attach(scope, lease, **evidence):
        exchange.update(evidence)
    store = SimpleNamespace(get_exchange=lambda *args: exchange or None, prepare_exchange=prepare,
        resume_exchange=lambda *args: SimpleNamespace(action="send_ready"), begin_send=lambda *args: True,
        capture_response=capture, attach_evidence=attach, MAX_RESPONSE_BYTES=100000)
    raw = next(iter(replay.fixture_map.values()))[1]
    mods.client.SizingRequest = SimpleNamespace(model_validate=lambda value: value)
    mods.client.post_string_length = lambda request, grant, **kw: raw
    context = SimpleNamespace(graph={}, intake_sha256="a" * 64, organization_id="seeded-org")
    def evidence(*args, **kwargs):
        labels.append(kwargs["classification"])
        return {"classification": kwargs["classification"]}
    fake_sizing = SimpleNamespace(plan_sizing_request=lambda *args: dict(wire_bytes=b"request", wire_sha256="a" * 64,
        normalized_params={"grant_ref": s.GRANT_REF}, normalized_request={}), build_sizing_evidence=evidence)
    fake_graph = SimpleNamespace(_exchanges=lambda: store, _exchange_authority=lambda job: ("scope", "lease"),
        _sha=s.digest, _sizing_expected=lambda *args: dict(tool_manifest_sha256="a" * 64,
            identity={}, parent_version_id="parent", producer={}),
        local=SimpleNamespace(canonical_bytes=lambda value: json.dumps(value).encode()))
    namespace = dict(cloud=mods.client, graph=fake_graph, sizing=fake_sizing,
        platform_link=SimpleNamespace(platform_db=lambda: SimpleNamespace(run_transaction=lambda operation: operation(None))),
        _job_context=lambda *args: context, prepare_project_graph_job=lambda *args, **kwargs: "prepared")
    exec(compile(ast.Module(body=[function], type_ignores=[]), "product-cloud-prepare", "exec"), namespace)
    assert namespace["_cloud_prepare"]({"params": {}, "execution_context": {"tool_manifest_sha256": "a" * 64}},
                                       SimpleNamespace(lost=False)) == "prepared"
    assert exchange["classification"] == labels[0] == json.loads(exchange["evidence_bytes"])["classification"] == "synthetic"


def test_r14f1_seeded_fixture_custody(tmp_path):
    replay = plan()
    _, mods = boot(tmp_path, replay)
    # Both admission and send-time call this same product client resolver.
    for _ in ("admission", "send"):
        grant = mods.client.resolve_grant(s.GRANT_REF, "seeded-org")
        assert grant.tenant_id == "seeded-org" and grant.access_token == TOKEN
    for org in ("demo-tenant", "foreign-org"):
        with pytest.raises(mods.grants.CloudError):
            mods.client.resolve_grant(s.GRANT_REF, org)
    assert TOKEN not in repr(replay) + json.dumps(replay.public())
    assert environment()["DATABASE_URL"] not in json.dumps(replay.public())


def test_r14f1_ambient_flags_refused():
    for name in ("LEAF_SYNTHETIC_STRING_SIZING", "SYNTHETIC_SIZING_PROOF_ACCEPTED", "SIZING_CLASSIFICATION"):
        args = inputs()
        args["environment"][name] = "1"
        with pytest.raises(s.ReplayRefused):
            s.prepare_replay(**args)


def test_r14f1_foreign_root_refused():
    args = inputs()
    args["run_root"] = ROOT.parent / "foreign"
    with pytest.raises(s.ReplayRefused):
        s.prepare_replay(**args)
    args = inputs()
    args["stat_root"] = lambda root: (1, 99)
    with pytest.raises(s.ReplayRefused):
        s.prepare_replay(**args)


def test_r14f1_exact_loopback_refused():
    for endpoint in ("https://outside.invalid/string-length", "http://localhost:51234/string-length",
                     "http://127.0.0.2:51234/string-length", "http://127.0.0.1:65536/string-length",
                     "http://127.0.0.1:51234/string-length?control=drop", "http://u@127.0.0.1:51234/string-length"):
        args = inputs()
        args["endpoint"] = endpoint
        with pytest.raises(s.ReplayRefused):
            s.prepare_replay(**args)


def test_r14f1_source_and_fixture_pins():
    for name in (s.MANIFEST, s.FIXTURE, "scripts/start-leaf.py"):
        args = inputs()
        args["read"] = lambda path: b"substituted" if path == s.REPO / name else path.read_bytes()
        with pytest.raises(s.ReplayRefused):
            s.prepare_replay(**args)
    replay = plan()
    request, fixture = next(iter(replay.fixture_map.items()))
    assert request == "c2adcc2616b290a64eacbdc9f3857447d5de567a3d297bb23b87d7bdabe269b5"
    assert s.digest(fixture[1]) == fixture[2]


def test_r14f1_posture_identity_refused():
    for key, value in (("LEAF_RUNTIME_ENV", "production"), ("LEAF_ENV", "staging"),
                       ("LEAF_AUTH_LIVE", "0"), ("LEAF_PROOF_PROJECT_RUN_ID", "foreign-run"),
                       ("VITE_TENANT_ID", "demo-tenant"), ("LEAF_HARNESS_DATABASE_URL", "foreign")):
        args = inputs()
        args["environment"][key] = value
        with pytest.raises(s.ReplayRefused):
            s.prepare_replay(**args)


def test_r14f1_bootstrap_refuses_before_mutation(tmp_path):
    replay, mods = plan(), modules()
    with pytest.raises(s.ReplayRefused):
        boot(tmp_path, replace(replay, endpoint="https://outside.invalid/string-length"), mods=mods)
    assert mods.client.SIZING_CLASSIFICATION == "live"
    assert mods.graph.SYNTHETIC_SIZING_PROOF_ACCEPTED is False


def test_r14f1_bootstrap_requires_supervisor_custody(tmp_path):
    for case in ("no-config", "hash-mismatch", "foreign-owner"):
        replay, env = private_plan(tmp_path / case)
        mods = modules()
        if case == "no-config":
            Path(env[s.CONFIG_KEY]).unlink()
        elif case == "hash-mismatch":
            env[s.CONFIG_HASH_KEY] = "0" * 64
        else:
            (replay.run_root / "ownership.json").write_text(
                json.dumps({"run_id": "foreign-run", "run_root": str(replay.run_root)}), encoding="utf-8")
        with pytest.raises(s.ReplayRefused):
            s.bootstrap_child(replay, "app", env, modules=mods)
        assert mods.client.SIZING_CLASSIFICATION == "live"
        assert mods.graph.SYNTHETIC_SIZING_PROOF_ACCEPTED is False


def test_r14f1_transport_has_exact_destination(tmp_path):
    _, mods = boot(tmp_path)
    with pytest.raises(s.ReplayRefused):
        with mods.client.requests.post("http://127.0.0.1:9/string-length", allow_redirects=False):
            pytest.fail("foreign transport admitted")


def test_r14f1_launcher_closed_commands():
    replay, calls = plan(), []
    original = lambda *args: calls.append(args)
    launcher = SimpleNamespace(spawn=original)
    def main():
        launcher.spawn("app", [sys.executable, "app.py"], s.REPO / "server", environment())
        launcher.spawn("worker", [sys.executable, "canonical_worker.py", "--tool", "solar-settings",
            "--readiness", "--worker-id", "project:r14f1-owned"], s.REPO / "server", environment())
        with pytest.raises(s.ReplayRefused):
            launcher.spawn("app", [sys.executable, "foreign.py"], s.REPO / "server", environment())
        return 0
    launcher.main = main
    assert s.run_replay_launcher(replay, launcher, environment(), validate=lambda *args: None) == 0
    assert launcher.spawn is original and len(calls) == 2
    assert calls[0][1][-2:] == ["child", "app"]
    assert calls[1][1][-5:] == ["--tool", "solar-settings", "--readiness", "--worker-id", "project:r14f1-owned"]
    assert calls[0][3]["DATABASE_URL"] == environment()["DATABASE_URL"]


def test_r14f1_browser_has_no_replay_capability():
    replay, calls = plan(), []
    env = dict(environment(), **{s.CONFIG_KEY: str(ROOT / "project-replay/private.json"), s.CONFIG_HASH_KEY: "a" * 64})
    launcher = SimpleNamespace(spawn=lambda *args: calls.append(args))
    launcher.main = lambda: launcher.spawn("web", ["npm", "run", "dev", "--", "--host", "127.0.0.1",
        "--port", "5175", "--strictPort"], s.REPO / "web", env)
    s.run_replay_launcher(replay, launcher, env, validate=lambda *args: None)
    assert s.CONFIG_KEY not in calls[0][3] and s.CONFIG_HASH_KEY not in calls[0][3]
    assert TOKEN not in json.dumps(calls[0][3])


def test_r14f1_registered_replacement_identity():
    schedule, barrier = armed()
    events = []
    with pytest.raises(s.ReplayRefused):
        barrier.capture_response(barrier.binding, committed=True, captured_worker="owned-start",
            current_worker="owned-start", stop=lambda who: events.append(("stop", who)) or True,
            launch_replacement=lambda: "foreign-start", register=lambda child: events.append(("register", child)),
            validate_identity=lambda child, old: False)
    assert events == [("stop", "owned-start"), ("register", "foreign-start")]
    assert schedule.next_index == 0 and barrier.consumed


def test_r14f1_capture_after_commit_only():
    schedule, barrier = armed()
    events = []
    kwargs = dict(captured_worker="owned-start", current_worker="owned-start",
        stop=lambda who: events.append("stop") or True, launch_replacement=lambda: "replacement-start",
        register=lambda child: events.append("register"), validate_identity=lambda new, old: new != old)
    with pytest.raises(s.ReplayRefused):
        barrier.capture_response(barrier.binding, committed=False, **kwargs)
    assert not events and not barrier.consumed
    kwargs["current_worker"] = "foreign-start"
    with pytest.raises(s.ReplayRefused):
        barrier.capture_response(barrier.binding, committed=True, **kwargs)
    assert not events
    kwargs["current_worker"] = "owned-start"
    assert barrier.capture_response(barrier.binding, committed=True, **kwargs) == "replacement-start"
    assert events == ["stop", "register"] and schedule.next_index == 1


def test_r14f1_replacement_requires_termination():
    _, barrier = armed()
    with pytest.raises(s.ReplayRefused):
        barrier.capture_response(barrier.binding, committed=True, captured_worker="owned", current_worker="owned",
            stop=lambda who: False, launch_replacement=lambda: pytest.fail("launched before termination"),
            register=lambda child: None, validate_identity=lambda *args: True)


def test_r14f1_one_shot_serial_schedule():
    schedule, first = armed()
    args = dict(run_id=schedule.plan.run_id, organization_id="seeded-org", project_id="project-one", job_id="job-one")
    with pytest.raises(s.ReplayRefused):
        s.arm_scenario(schedule, s.SCENARIOS[1], **args)
    with pytest.raises(s.ReplayRefused):
        first.consume(replace(first.binding, job_id="foreign-job"))
    first.consume(first.binding)
    with pytest.raises(s.ReplayRefused):
        first.consume(first.binding)
    first.complete()
    with pytest.raises(s.ReplayRefused):
        s.arm_scenario(schedule, s.SCENARIOS[0], **args)
    for scenario in s.SCENARIOS[1:]:
        barrier = s.arm_scenario(schedule, scenario, **args)
        barrier.consume(barrier.binding)
        barrier.complete()
    with pytest.raises(s.ReplayRefused):
        s.arm_scenario(schedule, s.SCENARIOS[2], **args)


def test_r14f1_schedule_scope_refused():
    for field in ("run_id", "organization_id"):
        schedule = s.FaultSchedule(plan())
        args = dict(run_id=schedule.plan.run_id, organization_id="seeded-org", project_id="project-one", job_id="job-one")
        args[field] = "foreign"
        with pytest.raises(s.ReplayRefused):
            s.arm_scenario(schedule, s.SCENARIOS[0], **args)
        assert schedule.active is None
    _, barrier = armed()
    for field in ("run_id", "organization_id", "project_id", "job_id"):
        with pytest.raises(s.ReplayRefused):
            barrier.consume(replace(barrier.binding, **{field: "foreign"}))
    assert not barrier.consumed


def test_r14f1_recover_refuses_foreign_schedule():
    replay = plan()
    foreign = replace(replay, run_id="foreign-run", organization_id="foreign-org")
    schedule = s.FaultSchedule(foreign)
    barrier = s.arm_scenario(schedule, "capture-recovery", run_id=foreign.run_id,
        organization_id=foreign.organization_id, project_id="project-one", job_id="job-one")
    events = []
    identity = SimpleNamespace(role="sizing-worker", pid=900, start_id="owned-start")
    runtime = s.ReplayRuntime(None, None, None, None)
    runtime.plan = replay
    runtime.worker_id = "project:" + replay.run_id + ":sizing:1"
    runtime.worker_identity = identity
    runtime.worker = SimpleNamespace(identity=identity,
        stop=lambda deadline: events.append("stop") or True)
    runtime.launch_worker = lambda deadline: events.append("start")
    with pytest.raises(s.ReplayRefused):
        runtime.recover(barrier, barrier.binding, committed=True, captured_identity=identity, deadline=10)
    assert events == [] and schedule.next_index == 0 and not barrier.consumed
    assert schedule.active is barrier


def test_r14f1_recover_refuses_foreign_root():
    replay = plan()
    foreign = replace(replay, run_root=ROOT.parent / "foreign", root_identity=(1, 99),
                      token="foreign-fixture-credential-" + "y" * 32)
    assert foreign.public() == replay.public() and foreign.source_files == replay.source_files
    schedule = s.FaultSchedule(foreign)
    barrier = s.arm_scenario(schedule, "capture-recovery", run_id=foreign.run_id,
        organization_id=foreign.organization_id, project_id="project-one", job_id="job-one")
    events = []
    def launch(spec, *, scope):
        number = len(events) + 1
        identity = SimpleNamespace(role="sizing-worker", pid=900 + number, start_id="start" + str(number))
        events.append(("start", number))
        return SimpleNamespace(identity=identity,
            stop=lambda deadline: events.append(("stop", number)) or True)
    runtime = s.ReplayRuntime(None, SimpleNamespace(process_launcher=launch), None,
                             SimpleNamespace(ProcessSpec=lambda *args: args))
    runtime.plan = replay
    runtime.environment = environment()
    runtime.heartbeat = lambda deadline: None
    first = runtime.launch_worker(10)
    with pytest.raises(s.ReplayRefused):
        runtime.recover(barrier, barrier.binding, committed=True, captured_identity=first.identity, deadline=10)
    assert events == [("start", 1)] and runtime.worker is first and runtime.generation == 1
    assert schedule.next_index == 0 and not barrier.consumed and schedule.active is barrier


def test_r14f1_browser_discard_after_admission():
    _, barrier = armed(s.SCENARIOS[1])
    sent = []
    async def send(message):
        sent.append(message)
    message = {"type": "http.response.start", "status": 202}
    task = barrier.discard_browser_response(barrier.binding, message, send, admitted=lambda binding: False)
    with pytest.raises(s.ReplayRefused):
        task.send(None)
    assert not barrier.consumed and not sent
    task = barrier.discard_browser_response(barrier.binding, message, send, admitted=lambda binding: True)
    with pytest.raises(ConnectionAbortedError):
        task.send(None)
    assert barrier.consumed and not sent
    task = barrier.discard_browser_response(barrier.binding, message, send, admitted=lambda binding: True)
    with pytest.raises(StopIteration):
        task.send(None)
    assert sent == [message]


def test_r14f1_service_drop_consumes_complete_request():
    _, barrier = armed(s.SCENARIOS[2])
    fixture = json.loads((s.REPO / s.FIXTURE).read_bytes())
    parser = s._load_parser()
    paths = sys.path[:]
    try:
        client = parser.sizing_module()
        body = client.wire_bytes(client.SizingRequest.model_validate(fixture["request"]))
    finally:
        sys.path[:] = paths
    events = []
    kwargs = dict(record=lambda pin: events.append(("record", pin)), close=lambda: events.append(("close",)) or True)
    with pytest.raises(s.ReplayRefused):
        barrier.service_response(barrier.binding, body[:-1], content_length=len(body), **kwargs)
    assert not events and not barrier.consumed
    barrier.service_response(barrier.binding, body, content_length=len(body), **kwargs)
    assert events == [("record", s.digest(body)), ("close",)]


def test_r14f1_independent_sizing_evidence():
    replay = plan()
    evidence = attachment(replay)
    calls = []
    assert s.verify_sizing_run(replay, evidence, observe=lambda *ids: calls.append(ids) or dict(evidence)) == evidence
    assert calls == [("seeded-org", "project-one", "job-one", "output-one")]
    for value in (None, dict(evidence, output_version_id="substituted"), dict(evidence, classification="live")):
        with pytest.raises(s.ReplayRefused):
            s.verify_sizing_run(replay, value, observe=lambda *ids: evidence)


def test_r14f1_cleanup_uncertainty():
    events = []
    owner = SimpleNamespace(resources=[SimpleNamespace(stop=lambda deadline: events.append("stop") or False)],
        deadline=lambda: 5, remove_private=lambda: events.append("remove") or True)
    assert s.close_replay(owner) is False and events == ["stop"]
    owner.resources[0].stop = lambda deadline: events.append("stopped") or True
    assert s.close_replay(owner) is True and events[-2:] == ["stopped", "remove"]


def test_r14f1_product_prover_still_refuses(monkeypatch):
    # A separate ordinary product import; no bootstrap is applied to it.
    monkeypatch.syspath_prepend(str(s.REPO / "server/tests"))
    monkeypatch.syspath_prepend(str(s.REPO / "server"))
    helper = _load("_r14f1_product_evidence_fixture", s.REPO / "server/tests/test_sip_r14_evidence.py")
    source = helper.source.__wrapped__()
    recorded = helper.recorded.__wrapped__()
    case = helper.case.__wrapped__(source, recorded)
    assert helper.cloud.SIZING_CLASSIFICATION == "live"
    with pytest.raises(helper.GraphValidationError, match="SIP_R14_SYNTHETIC_EVIDENCE_REFUSED"):
        helper.project.prove_sizing_evidence(case["record"], case["request_bytes"], case["raw"], case["graph"],
                                            expected=case["expected"])


def test_r14f1_capture_hook_is_after_committed_store():
    _, barrier = armed()
    events = []
    def capture(scope, lease, **kwargs):
        events.extend(("capture_response", "commit"))
        return "stored"
    def committed(scope, lease):
        assert events == ["capture_response", "commit"]
        events.append("observe")
        return True
    hook = s.capture_response_hook(capture, binding_for=lambda *args: barrier.binding, committed=committed,
        pause=lambda binding, worker: events.append(("pause", binding.job_id, worker)))
    assert hook("scope", SimpleNamespace(worker_id="captured-worker"), response_bytes=b"bytes") == "stored"
    assert events == ["capture_response", "commit", "observe", ("pause", "job-one", "captured-worker")]
    def failed(*args, **kwargs):
        raise RuntimeError("capture rolled back")
    hook = s.capture_response_hook(failed, binding_for=lambda *args: pytest.fail("failed capture reached barrier"),
        committed=committed, pause=lambda *args: pytest.fail("paused rolled back capture"))
    with pytest.raises(RuntimeError):
        hook("scope", "lease")


def test_r14f1_asgi_wrapper_runs_real_admission():
    _, barrier = armed(s.SCENARIOS[1])
    events = []
    async def app(scope, receive, send):
        events.append("real-admission")
        await send({"type": "http.response.start", "status": 202})
        pytest.fail("discard returned control to response producer")
    async def send(message):
        events.append("browser-send")
    wrapped = s.submission_asgi_wrapper(app, barrier=barrier, binding_for=lambda scope: barrier.binding,
        admitted=lambda binding: events == ["real-admission"])
    task = wrapped({"type": "http", "method": "POST"}, None, send)
    with pytest.raises(ConnectionAbortedError):
        task.send(None)
    assert events == ["real-admission"] and barrier.consumed


def test_r14f1_private_bootstrap_roundtrip(tmp_path):
    replay, env = private_plan(tmp_path)
    path = Path(env[s.CONFIG_KEY])
    reread = s.read_private_plan(env)
    assert reread.public() == replay.public() and reread.token == replay.token
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(s.ReplayRefused):
        s.read_private_plan(env)


def test_r14f1_listener_fake_transport(monkeypatch):
    from email.message import Message
    import io
    replay = plan()
    calls = []
    raw_fixture = json.loads((s.REPO / s.FIXTURE).read_bytes())
    paths = sys.path[:]
    try:
        client = s._load_parser().sizing_module()
        body = client.wire_bytes(client.SizingRequest.model_validate(raw_fixture["request"]))
    finally:
        sys.path[:] = paths
    class Server:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 51234)
            self.handler = handler
        def __enter__(self): return self
        def __exit__(self, *args): calls.append("closed")
        def serve_forever(self, **kwargs):
            handler = self.handler()
            handler.path = "/string-length"
            handler.connection = SimpleNamespace(settimeout=lambda timeout: None)
            handler.headers = Message()
            for key, value in (("Authorization", "Bearer " + TOKEN), ("Content-Type", "application/json"),
                               ("Content-Length", str(len(body)))):
                handler.headers[key] = value
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.send_response = lambda status: calls.append(status)
            handler.send_header = lambda key, value: calls.append((key, value))
            handler.end_headers = lambda: None
            handler.do_POST()
            assert handler.wfile.getvalue() == next(iter(replay.fixture_map.values()))[1]
            handler.path = "/control"
            with pytest.raises(s.ReplayRefused):
                handler.do_POST()
    monkeypatch.setitem(sys.modules, "http.server", SimpleNamespace(BaseHTTPRequestHandler=object, HTTPServer=Server))
    monkeypatch.setattr(s, "read_private_plan", lambda env: replay)
    monkeypatch.setattr(s, "_write_private", lambda path, value: calls.append(value))
    s._listener_main(environment(), record_request=lambda event: calls.append(event))
    assert calls[0] == replay.public() and calls.count(200) == 1 and calls[-1] == "closed"
    assert ("X-Leaf-Synthetic-Replay", "synthetic") in calls


def fault_payload():
    return dict(job_id="job-one", organization_id="seeded-org", project_id="project-one", worker_id="worker-one",
                attempt=1, response_sha256="a" * 64)


def fault_directory(tmp_path):
    replay, env = private_plan(tmp_path)
    directory = replay.run_root / "project-replay/faults"
    directory.mkdir(mode=0o700)
    return replay, env, directory


def finish_coroutine(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("fake coroutine unexpectedly suspended")


def test_r14f2_message_mac_and_exact_shape():
    replay = plan()
    raw = s.fault_message(replay, "capture", 1, **fault_payload())
    value = s.validate_fault_message(replay, raw)
    assert value["seq"] == 1 and value["kind"] == "capture" and TOKEN.encode() not in raw
    for changed in (dict(value, extra=True), {key: item for key, item in value.items() if key != "attempt"},
                    dict(value, attempt=True), dict(value, mac="0" * 64)):
        with pytest.raises(s.ReplayRefused):
            s.validate_fault_message(replay, json.dumps(changed).encode())
    with pytest.raises(s.ReplayRefused):
        s.fault_message(replay, "capture", 1, **dict(fault_payload(), extra=True))


def test_r14f2_message_size_and_sequence_bounds():
    replay = plan()
    for raw in (b"", b"x" * 4097, b"{}", b'{"kind":"capture","kind":"capture"}'):
        with pytest.raises(s.ReplayRefused):
            s.validate_fault_message(replay, raw)
    for seq in (0, -1, True, 1025):
        with pytest.raises(s.ReplayRefused):
            s.fault_message(replay, "capture", seq, **fault_payload())


def test_r14f2_atomic_private_message(tmp_path, monkeypatch):
    replay, _, directory = fault_directory(tmp_path)
    path = directory / "event-sizing-worker-100-1.json"
    raw = s.fault_message(replay, "capture", 1, **fault_payload())
    events = []
    replace = s.os.replace
    def observe(source, destination):
        assert source.name.endswith(".tmp") and source.read_bytes() == raw and not destination.exists()
        events.append("replace")
        replace(source, destination)
    monkeypatch.setattr(s.os, "replace", observe)
    s.write_fault_message(path, raw)
    assert events == ["replace"] and s.read_fault_message(replay, path)["job_id"] == "job-one"
    assert not path.with_suffix(".tmp").exists()
    if s.os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_r14f2_tampered_and_foreign_messages_refused(tmp_path):
    replay, _, directory = fault_directory(tmp_path)
    path = directory / "event-sizing-worker-100-1.json"
    raw = s.fault_message(replay, "capture", 1, **fault_payload())
    path.write_bytes(raw.replace(b"job-one", b"job-two"))
    with pytest.raises(s.ReplayRefused):
        s.read_fault_message(replay, path)
    path.write_bytes(raw)
    with pytest.raises(s.ReplayRefused):
        s.read_fault_message(replace(replay, run_id="foreign-run"), path)
    with pytest.raises(s.ReplayRefused):
        s.read_fault_message(replay, directory.parent / path.name)


def test_r14f2_channel_count_and_decision_timeout(tmp_path):
    replay, _, directory = fault_directory(tmp_path)
    clock = SimpleNamespace(now=0.0)
    channel = s.FaultChannel(replay, "sizing-worker", clock=lambda: clock.now,
        wait=lambda seconds: setattr(clock, "now", clock.now + seconds), pid=100)
    channel.seq = 256
    with pytest.raises(s.ReplayRefused):
        channel.event("capture", **fault_payload())
    with pytest.raises(s.ReplayRefused):
        channel.decision("event-sizing-worker-100-1", {"continue"})
    assert 60 <= clock.now < 60.1 and not list(directory.iterdir())


def test_r14f2_channel_decision_replay_and_tamper(tmp_path):
    replay, _, directory = fault_directory(tmp_path)
    channel = s.FaultChannel(replay, "app", pid=100)
    event = channel.event("submission", job_id="job-one", status_code=202, body_sha256="a" * 64,
        idempotency_key_sha256="b" * 64, organization_id="seeded-org", project_id="project-one")
    supervisor = s.FaultChannel(replay, "supervisor", pid=200)
    supervisor.decide(event, "pass")
    assert channel.decision(event, {"pass"}) == "pass"
    path = directory / ("decision-" + event + ".json")
    path.write_bytes(path.read_bytes().replace(b'"pass"', b'"hold"'))
    with pytest.raises(s.ReplayRefused):
        channel.decision(event, {"pass"})


def test_r14f2_capture_continue_pause_and_timeout():
    events, exits = [], []
    clock = SimpleNamespace(now=0.0)
    channel = SimpleNamespace(event=lambda kind, **payload: events.append(kind) or "event-sizing-worker-1-1",
        decision=lambda event, allowed: "continue", clock=lambda: clock.now,
        wait=lambda seconds: setattr(clock, "now", clock.now + seconds))
    s.capture_handshake(channel, fault_payload(), exit_process=exits.append)
    assert events == ["capture"] and exits == []
    channel.decision = lambda event, allowed: "pause"
    with pytest.raises(s.ReplayRefused):
        s.capture_handshake(channel, fault_payload(), exit_process=exits.append)
    assert events[-2:] == ["capture", "paused"] and exits == [70] and clock.now == 120
    def timed_out(*args):
        raise s.ReplayRefused()
    channel.decision = timed_out
    with pytest.raises(s.ReplayRefused):
        s.capture_handshake(channel, fault_payload(), exit_process=exits.append)
    assert exits == [70, 70]


def submission_case(body=None, action="pass"):
    h = SimpleNamespace(events=[], sent=[], received=[], action=action)
    request = {"type": "http.request", "body": body if body is not None else b'{"tool":"solar-size-strings"}'}
    messages = [{"type": "http.response.start", "status": 202, "headers": [(b"connection", b"close")]},
                {"type": "http.response.body", "body": b'{"job_id":"job-one","status":"submitted"}'}]
    async def receive(): return request
    async def send(message): h.sent.append(message)
    async def app(scope, receive, send):
        h.received.append(await receive())
        for message in messages: await send(message)
    channel = SimpleNamespace(event=lambda kind, **payload: h.events.append((kind, payload)) or "event-app-100-1")
    async def decide(channel, event, allowed): return h.action
    scope = {"type": "http", "method": "POST", "path": "/api/run", "headers": [
        (b"x-org-id", b"seeded-org"), (b"x-project-id", b"project-one"), (b"idempotency-key", b"private-key")]}
    h.run = lambda: finish_coroutine(s.channel_submission_wrapper(app, channel, decide=decide)(scope, receive, send))
    h.request, h.messages, h.scope = request, messages, scope
    return h


def test_r14f2_submission_pass_sends_held_messages():
    h = submission_case()
    h.run()
    assert h.sent == h.messages and h.received == [h.request]
    kind, value = h.events[0]
    assert kind == "submission" and value["job_id"] == "job-one" and value["status_code"] == 202
    assert value["body_sha256"] == s.digest(h.request["body"]) and value["idempotency_key_sha256"] == s.digest(b"private-key")
    assert "private-key" not in json.dumps(h.events)


def test_r14f2_submission_discard_sends_no_bytes():
    h = submission_case(action="discard")
    reset = s._ABORT_RESPONSE.set(False)
    try:
        with pytest.raises(s._DiscardResponse):
            h.run()
        assert h.sent == [] and [event[0] for event in h.events] == ["submission", "discarded"]
        assert s._ABORT_RESPONSE.get() is True
    finally:
        s._ABORT_RESPONSE.reset(reset)


def test_r14f2_submission_non_sizing_invalid_and_oversize_untouched():
    for body in (b'{"tool":"solar-settings"}', b"invalid", b"[]", b"x" * 1048577):
        h = submission_case(body)
        h.run()
        assert h.events == [] and h.sent == h.messages and h.received == [h.request]


def test_r14f2_uvicorn_abort_is_context_local():
    calls = []
    class Cycle:
        transport = SimpleNamespace(abort=lambda: calls.append("abort"))
        async def send_500_response(self): calls.append("500")
    s.patch_uvicorn_abort(Cycle)
    reset = s._ABORT_RESPONSE.set(False)
    try:
        finish_coroutine(Cycle().send_500_response())
        s._ABORT_RESPONSE.set(True)
        finish_coroutine(Cycle().send_500_response())
        assert calls == ["500", "abort"]
    finally:
        s._ABORT_RESPONSE.reset(reset)


def listener_case(tmp_path, monkeypatch, operate):
    replay, env = private_plan(tmp_path)
    from email.message import Message
    import io
    class Server:
        def __init__(self, address, handler): self.handler = handler
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def serve_forever(self, **kwargs):
            handler = self.handler()
            handler.headers, handler.wfile = Message(), io.BytesIO()
            handler.headers["Authorization"] = "Bearer " + TOKEN
            handler.connection = SimpleNamespace(settimeout=lambda seconds: None, shutdown=lambda *args: None, close=lambda: None)
            handler.send_response = lambda status: None
            handler.send_header = lambda key, value: None
            handler.end_headers = lambda: None
            operate(handler, replay)
    monkeypatch.setitem(sys.modules, "http.server", SimpleNamespace(BaseHTTPRequestHandler=object, HTTPServer=Server))
    monkeypatch.setattr(s, "read_private_plan", lambda environment: replay)
    monkeypatch.setattr(s, "_write_private", lambda path, value: None)
    return replay, env


def test_r14f2_listener_catalog_exact_path_bearer_and_tokens(tmp_path, monkeypatch):
    def operate(handler, replay):
        handler.path = "/string-length/modules?q=JA_Solar_JAM72D40-595%2FMB&limit=20"
        handler.do_GET()
        result = json.loads(handler.wfile.getvalue())
        assert result["modules"][0]["key"] == "JA_Solar_JAM72D40-595/MB" and result["truncated"] is False
        handler.wfile.seek(0); handler.wfile.truncate()
        handler.path = "/string-length/modules?q=other&limit=20"
        handler.do_GET()
        assert json.loads(handler.wfile.getvalue())["modules"] == []
        for path in ("/modules?q=JA&limit=20", "/string-length/modules?q=JA&q=Solar&limit=20", "/string-length/modules?q=JA&limit=0"):
            handler.path = path
            with pytest.raises(s.ReplayRefused): handler.do_GET()
        handler.path = "/string-length/modules?q=JA&limit=20"
        handler.headers.replace_header("Authorization", "Bearer foreign")
        with pytest.raises(s.ReplayRefused): handler.do_GET()
    replay, env = listener_case(tmp_path, monkeypatch, operate)
    s._listener_main(env, record_request=lambda event: pytest.fail("catalog GET journaled as service POST"))
    assert not (replay.run_root / "project-replay/requests.jsonl").exists()


def test_r14f2_listener_hold_drop_journals_once(tmp_path, monkeypatch):
    events = []
    decisions = iter(("hold", "drop"))
    monkeypatch.setattr(s, "FaultChannel", lambda *args: SimpleNamespace(
        event=lambda kind, **payload: events.append(kind) or "event-listener-1-1",
        decision=lambda event, allowed: next(decisions)))
    def operate(handler, replay):
        import io
        raw = json.loads((s.REPO / s.FIXTURE).read_bytes())
        paths = sys.path[:]
        try:
            client = s._load_parser().sizing_module()
            body = client.wire_bytes(client.SizingRequest.model_validate(raw["request"]))
        finally:
            sys.path[:] = paths
        handler.path, handler.rfile = "/string-length", io.BytesIO(body)
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.do_POST()
        assert handler.wfile.getvalue() == b""
    replay, env = listener_case(tmp_path, monkeypatch, operate)
    s._listener_main(env)
    assert events == ["service", "held", "dropped"]
    assert len((replay.run_root / "project-replay/requests.jsonl").read_bytes().splitlines()) == 1


def test_r14f2_app_catalog_fixture_custody_and_transport():
    replay, mods, calls = plan(), modules(), []
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            assert self.trust_env is False
            calls.append(url)
            return Session()
    mods.catalog = SimpleNamespace()
    mods.requests.Session, mods.requests.Timeout = Session, TimeoutError
    s.install_app_seams(replay, mods)
    catalog = mods.catalog
    assert catalog.cloud.resolve_grant(s.GRANT_REF, replay.organization_id).access_token == TOKEN
    for grant, organization in (("foreign", replay.organization_id), (s.GRANT_REF, "foreign")):
        with pytest.raises(mods.grants.CloudError): catalog.cloud.resolve_grant(grant, organization)
    kwargs = dict(allow_redirects=False, headers={"Authorization": "Bearer " + TOKEN})
    with catalog.requests.get(replay.endpoint + "/modules", **kwargs): pass
    with pytest.raises(s.ReplayRefused):
        with catalog.requests.get("https://foreign.invalid", **kwargs): pass
    assert calls == [replay.endpoint + "/modules"]


def test_r14f2_auxiliary_worker_roles_and_closed_argv():
    replay = plan()
    for role, tool, suffix in (("panels-worker", "solar-panels-from-drawing", "panels"),
                               ("groups-worker", "solar-panel-groups", "groups")):
        args = ["--tool", tool, "--readiness", "--worker-id", f"project:{replay.run_id}:{suffix}:1"]
        s.validate_worker_argv(replay, role, args)
        for changed in (args + ["--foreign"], [*args[:4], args[4][:-1] + "2"], ["--tool", s.TOOL, *args[2:]]):
            with pytest.raises(s.ReplayRefused): s.validate_worker_argv(replay, role, changed)
    with pytest.raises(s.ReplayRefused): s.validate_worker_argv(replay, "foreign-worker", [])


def test_r14f2_catalog_is_a_required_source_pin():
    assert s.CATALOG in s.PIN_FILES and s.digest((s.REPO / s.CATALOG).read_bytes()) == s.CATALOG_SHA256
    args = inputs()
    args["read"] = lambda path: b"foreign" if path == s.REPO / s.CATALOG else path.read_bytes()
    with pytest.raises(s.ReplayRefused): s.prepare_replay(**args)


def driver_case():
    replay = plan()
    now, counter = SimpleNamespace(value=0.0), SimpleNamespace(value=0)
    def launch(spec, *, scope):
        counter.value += 1
        return SimpleNamespace(identity=SimpleNamespace(role="sizing-worker", pid=100 + counter.value, start_id=str(counter.value)),
                               stop=lambda deadline: True, poll=lambda: None)
    effects = SimpleNamespace(process_launcher=launch, clock=SimpleNamespace(monotonic=lambda: now.value))
    runtime = s.ReplayRuntime(None, effects, None, SimpleNamespace(ProcessSpec=lambda *args: args))
    runtime.plan, runtime.environment = replay, environment()
    runtime.schedule = s.FaultSchedule(replay)
    runtime.decisions = []
    runtime.channel = SimpleNamespace(decide=lambda event, action: runtime.decisions.append((event, action)))
    runtime.heartbeat = lambda deadline, **kwargs: None
    runtime.posts = 0
    runtime.service_posts = lambda: runtime.posts
    runtime.launch_worker(100)
    runtime.observed = dict(job_id="job-1", organization_id=replay.organization_id, project_id="project-1",
        project_name=f"PP3B {replay.run_id} sizing recovery", tool=s.TOOL, exchange_state=None)
    runtime.observe = lambda query, deadline: dict(runtime.observed)
    return runtime


def test_r14f2_driver_serial_binding_and_two_recoveries():
    runtime = driver_case()
    def submit(job, project):
        return dict(kind="submission", job_id=job, organization_id=runtime.plan.organization_id, project_id=project,
                    body_sha256="a" * 64, idempotency_key_sha256="b" * 64)
    runtime._drive_event("event-app-1-1", submit("job-1", "project-1"), 100)
    runtime.observed.update(exchange_state="response_stored", sender_worker=runtime.worker_id, sender_attempt=1, response_sha256="c" * 64)
    capture = dict(kind="capture", job_id="job-1", organization_id=runtime.plan.organization_id, project_id="project-1",
        worker_id=runtime.worker_id, attempt=1, response_sha256="c" * 64)
    runtime._drive_event("event-sizing-worker-101-1", capture, 100)
    runtime.posts = 1
    runtime._drive_event("event-sizing-worker-101-2", dict(kind="paused", event="event-sizing-worker-101-1"), 100)
    assert runtime.generation == 2 and runtime.facts[s.SCENARIOS[0]]["service_posts"] == 1
    runtime.observed.update(job_id="job-2", project_id="project-2", project_name=f"PP3B {runtime.plan.run_id} sizing submission")
    runtime._drive_event("event-app-1-2", submit("job-2", "project-2"), 100)
    runtime._drive_event("event-app-1-3", dict(kind="discarded", event="event-app-1-2"), 100)
    runtime._drive_event("event-app-1-4", submit("job-2", "project-2"), 100)
    runtime.posts = 2
    runtime.facts[s.SCENARIOS[1]] = dict(job_id="job-2", submissions=2, same_submission=True, service_posts=2)
    runtime.schedule.active.complete()
    runtime.retry_event = None
    runtime.observed.update(job_id="job-3", project_id="project-3", project_name=f"PP3B {runtime.plan.run_id} sizing service",
                            exchange_state="send_started", sender_worker=runtime.worker_id, sender_attempt=1)
    runtime._drive_event("event-app-1-5", submit("job-3", "project-3"), 100)
    request = next(iter(runtime.plan.fixture_map))
    runtime._drive_event("event-listener-1-1", dict(kind="service", request_sha256=request, content_length=100), 100)
    runtime._drive_event("event-listener-1-2", dict(kind="held", event="event-listener-1-1"), 100)
    runtime.posts = 3
    runtime._drive_event("event-listener-1-3", dict(kind="dropped", event="event-listener-1-1"), 100)
    assert runtime.generation == 3 and runtime.complete()
    assert [runtime.facts[key]["service_posts"] for key in s.SCENARIOS] == [1, 2, 3]
    assert [action for event, action in runtime.decisions] == ["pass", "pause", "discard", "pass", "pass", "hold", "drop"]


def test_r14f2_driver_foreign_out_of_order_and_changed_retry_refused():
    runtime = driver_case()
    payload = dict(kind="submission", job_id="job-1", organization_id="foreign", project_id="project-1",
                   body_sha256="a" * 64, idempotency_key_sha256="b" * 64)
    with pytest.raises(s.ReplayRefused): runtime._drive_event("event-app-1-1", payload, 100)
    runtime = driver_case()
    runtime.observed["project_name"] = "PP3B foreign sizing recovery"
    payload["organization_id"] = runtime.plan.organization_id
    with pytest.raises(s.ReplayRefused): runtime._drive_event("event-app-1-1", payload, 100)
    runtime = driver_case()
    runtime.schedule.next_index = 1
    with pytest.raises(s.ReplayRefused): runtime._drive_event("event-app-1-1", payload, 100)
    runtime = driver_case()
    runtime.jobs = ["job-0", "job-1"]
    runtime.discard_ack = "event-app-1-1"
    runtime.submissions["job-1"] = dict(payload, body_sha256="c" * 64)
    runtime.observed.update(job_id="job-1", project_name=f"PP3B {runtime.plan.run_id} sizing submission")
    with pytest.raises(s.ReplayRefused): runtime._drive_event("event-app-1-2", payload, 100)


def test_r14f2_driver_event_count_bound(tmp_path):
    replay, _, directory = fault_directory(tmp_path)
    runtime = driver_case()
    runtime.plan = replay
    for seq in range(1, 258):
        (directory / f"event-app-100-{seq}.json").write_bytes(b"{}")
    with pytest.raises(s.ReplayRefused): runtime.drive(100)


def test_r14f2_capture_bootstrap_reads_fresh_committed_digest():
    from contextlib import contextmanager
    events = []
    connection = object()
    @contextmanager
    def connect():
        events.append("new-transaction")
        yield connection
    lease = SimpleNamespace(worker_id="worker-one", attempt=1)
    scope = SimpleNamespace(org_id="seeded-org", project_id="project-one", job_id="job-one")
    def capture(scope, lease, **kwargs):
        events.append("capture-committed")
        return "stored"
    def get_exchange(scope, lease, *, conn):
        assert conn is connection and events == ["capture-committed", "new-transaction"]
        return dict(state="response_stored", sender_worker=lease.worker_id, sender_attempt=1,
                    response_bytes=b"response", response_sha256=s.digest(b"response"))
    exchanges = SimpleNamespace(capture_response=capture, get_exchange=get_exchange, db=SimpleNamespace(connection=connect))
    channel = SimpleNamespace(event=lambda kind, **payload: events.append((kind, payload)) or "event-sizing-worker-1-1",
                              decision=lambda event, allowed: "continue")
    s.install_capture_seam(plan(), SimpleNamespace(_exchanges=lambda: exchanges), channel)
    assert exchanges.capture_response(scope, lease, response_bytes=b"response", response_sha256=s.digest(b"response")) == "stored"
    assert events[2][0] == "capture" and events[2][1]["response_sha256"] == s.digest(b"response")


def test_r14f2_heartbeat_allows_only_owned_service_drop_gap():
    runtime = driver_case()
    # Exercise the real heartbeat method, while the driver owns a proved stop.
    runtime.generation = 2
    runtime.stopped_for_drop = True
    runtime.schedule.next_index = 2
    barrier = runtime._arm(s.SCENARIOS[2], runtime.observed)
    barrier.consume(barrier.binding)
    runtime.pending["event-listener-1-1"] = ("held", 2, {})
    assert s.ReplayRuntime.heartbeat(runtime, 100) is None
    runtime.pending.clear()
    with pytest.raises(s.ReplayRefused): s.ReplayRuntime.heartbeat(runtime, 100)


def test_r14f2_every_http_response_closes_connection():
    for method, path, body in (("POST", "/api/run", None), ("POST", "/api/run", b'{"tool":"solar-settings"}'),
                               ("GET", "/health", b""), ("POST", "/other", b"")):
        h = submission_case(body)
        h.scope.update(method=method, path=path)
        original = [(b"Connection", b"keep-alive"), (b"content-type", b"application/json"), (b"connection", b"upgrade")]
        h.messages[0]["headers"] = original[:]
        h.run()
        assert h.sent[0]["headers"] == [(b"content-type", b"application/json"), (b"connection", b"close")]
        assert h.messages[0]["headers"] == original and h.sent[1:] == h.messages[1:]
        assert bool(h.events) is (method == "POST" and path == "/api/run" and body is None)


def test_r14f2_listener_disconnected_sender_still_acknowledges_drop(tmp_path, monkeypatch):
    events, closed = [], []
    decisions = iter(("hold", "drop"))
    monkeypatch.setattr(s, "FaultChannel", lambda *args: SimpleNamespace(
        event=lambda kind, **payload: events.append(kind) or "event-listener-1-1",
        decision=lambda event, allowed: next(decisions)))
    def operate(handler, replay):
        import io
        raw = json.loads((s.REPO / s.FIXTURE).read_bytes())
        paths = sys.path[:]
        try:
            client = s._load_parser().sizing_module()
            body = client.wire_bytes(client.SizingRequest.model_validate(raw["request"]))
        finally:
            sys.path[:] = paths
        def shutdown(*args):
            closed.append("shutdown")
            raise OSError(57, "Socket is not connected")
        handler.connection.shutdown = shutdown
        handler.connection.close = lambda: closed.append("close")
        handler.path, handler.rfile = "/string-length", io.BytesIO(body)
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(body))
        handler.do_POST()
        assert handler.wfile.getvalue() == b""
    replay, env = listener_case(tmp_path, monkeypatch, operate)
    s._listener_main(env)
    assert closed == ["shutdown", "close"] and events == ["service", "held", "dropped"]
    assert len((replay.run_root / "project-replay/requests.jsonl").read_bytes().splitlines()) == 1


def test_r14f2_submission_buffer_caps_every_message():
    reads, entered, received, sent, events = [], [], [], [], []
    async def receive():
        message = {"type": "http.request", "body": b"", "more_body": True}
        reads.append(message)
        return message
    async def app(scope, receive, send):
        entered.append(len(reads))
        for _ in range(1025): received.append(await receive())
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})
    async def send(message): sent.append(message)
    channel = SimpleNamespace(event=lambda *args, **kwargs: events.append(args))
    finish_coroutine(s.channel_submission_wrapper(app, channel)(
        {"type": "http", "method": "POST", "path": "/api/run"}, receive, send))
    assert entered == [1024] and len(reads) == len(received) == 1025 and events == []
    assert sent[-1]["body"] == b"ok"


def test_r14f2_submission_receive_has_one_deadline(monkeypatch):
    now, deadlines, received = SimpleNamespace(value=0.0), [], []
    chunks = iter(({"type": "http.request", "body": b'{"tool":', "more_body": True},
                   {"type": "http.request", "body": b'"solar-settings"}'}))
    monkeypatch.setattr(s.time, "monotonic", lambda: now.value)
    async def receive(): return next(chunks)
    async def bounded(receive, deadline):
        deadlines.append(deadline)
        now.value += 10.0
        return await receive()
    monkeypatch.setattr(s, "_receive_before", bounded)
    async def app(scope, receive, send):
        received.extend((await receive(), await receive()))
    channel = SimpleNamespace(event=lambda *args, **kwargs: pytest.fail("non-sizing event"))
    finish_coroutine(s.channel_submission_wrapper(app, channel)(
        {"type": "http", "method": "POST", "path": "/api/run"}, receive, None))
    assert deadlines == [30.0, 30.0] and b"".join(item["body"] for item in received) == b'{"tool":"solar-settings"}'


def test_r14f2_submission_receive_timeout_aborts_without_bytes(monkeypatch):
    async def bounded(receive, deadline): raise TimeoutError()
    async def app(*args): pytest.fail("timed-out body reached admission")
    async def send(message): pytest.fail("timed-out body sent response bytes")
    monkeypatch.setattr(s, "_receive_before", bounded)
    channel = SimpleNamespace(event=lambda *args, **kwargs: pytest.fail("timed-out body emitted event"))
    reset = s._ABORT_RESPONSE.set(False)
    try:
        with pytest.raises(s._DiscardResponse):
            finish_coroutine(s.channel_submission_wrapper(app, channel)(
                {"type": "http", "method": "POST", "path": "/api/run"}, None, send))
        assert s._ABORT_RESPONSE.get() is True
    finally:
        s._ABORT_RESPONSE.reset(reset)


def test_r14f2_receive_before_uses_remaining_absolute_budget(monkeypatch):
    import asyncio
    now, budgets = SimpleNamespace(value=0.0), []
    monkeypatch.setattr(s.time, "monotonic", lambda: now.value)
    async def wait_for(awaitable, *, timeout):
        budgets.append(timeout)
        return await awaitable
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    async def receive(): return "chunk"
    assert finish_coroutine(_real_receive_before(receive, 30.0)) == "chunk"
    now.value = 29.0
    assert finish_coroutine(_real_receive_before(receive, 30.0)) == "chunk"
    now.value = 30.0
    with pytest.raises(TimeoutError): finish_coroutine(_real_receive_before(receive, 30.0))
    assert budgets == [30.0, 1.0]


def retry_driver_case(tmp_path):
    replay, _, directory = fault_directory(tmp_path)
    runtime = driver_case()
    runtime.plan, runtime.schedule = replay, s.FaultSchedule(replay)
    runtime.jobs, runtime.posts = ["job-1", "job-2"], 2
    runtime.schedule.next_index = 1
    retry = dict(runtime.observed, job_id="job-2", project_id="project-2",
        project_name=f"PP3B {replay.run_id} sizing submission", exchange_state="published")
    barrier = runtime._arm(s.SCENARIOS[1], retry)
    barrier.consume(barrier.binding)
    runtime.retry_event = "event-app-1-4"
    runtime.next_job = dict(runtime.observed, job_id="job-3", project_id="project-3",
        project_name=f"PP3B {replay.run_id} sizing service", exchange_state="prepared")
    runtime.observations = []
    def observe(query, deadline):
        runtime.observations.append(query["job_id"])
        return dict(retry if query["job_id"] == "job-2" else runtime.next_job)
    runtime.observe = observe
    path = directory / "event-app-100-1.json"
    s.write_fault_message(path, s.fault_message(replay, "submission", 1, job_id="job-3", status_code=202,
        organization_id=replay.organization_id, project_id="project-3", body_sha256="a" * 64, idempotency_key_sha256="b" * 64))
    return runtime, retry, path


def test_r14f2_driver_settles_published_retry_before_new_admission(tmp_path):
    runtime, retry, path = retry_driver_case(tmp_path)
    runtime.drive(100)
    assert runtime.observations == ["job-2", "job-3"]
    assert runtime.schedule.next_index == 2 and runtime.retry_event is None and path.stem in runtime.seen
    assert runtime.facts[s.SCENARIOS[1]] == dict(job_id="job-2", submissions=2, same_submission=True, service_posts=2)
    assert runtime.jobs == ["job-1", "job-2", "job-3"] and runtime.decisions == [(path.stem, "pass")]


def test_r14f2_driver_waits_for_retry_settlement(tmp_path):
    runtime, retry, path = retry_driver_case(tmp_path)
    retry["exchange_state"] = "response_stored"
    runtime.drive(100)
    assert runtime.jobs == ["job-1", "job-2"] and path.stem not in runtime.seen and runtime.decisions == []
    retry["exchange_state"] = "published"
    runtime.drive(100)
    assert runtime.jobs == ["job-1", "job-2", "job-3"] and path.stem in runtime.seen
    assert runtime.schedule.next_index == 2 and runtime.decisions == [(path.stem, "pass")]


def heartbeat_case():
    from datetime import datetime, timezone
    runtime = driver_case()
    del runtime.heartbeat
    runtime.p.HEARTBEAT_MAX_AGE_S = 30.0
    runtime.p.HeartbeatProof = SimpleNamespace
    runtime.p.CanonicalRequest = lambda purpose, tool, environment, **kwargs: SimpleNamespace(tool=tool, **kwargs)
    now, instant = SimpleNamespace(value=0.0), datetime(2026, 1, 1, tzinfo=timezone.utc)
    runtime.effects.clock.monotonic = lambda: now.value
    runtime.effects.clock.utc_now = lambda: instant
    descriptor = SimpleNamespace(runtime="python-3.12.14", source_sha256="a" * 64, source_revision="sha256:" + "a" * 64)
    runtime.descriptor = descriptor
    runtime.beats, runtime.reads = {}, []
    workers = [(runtime.worker, runtime.worker_identity, runtime.worker_id, s.TOOL, descriptor)]
    for label, tool in (("panels", "solar-panels-from-drawing"), ("groups", "solar-panel-groups")):
        identity = SimpleNamespace(role=label + "-worker", start_id=label)
        child = SimpleNamespace(identity=identity, poll=lambda: None)
        worker_id = f"project:{runtime.plan.run_id}:{label}:1"
        owned = (child, identity, worker_id, tool, descriptor)
        workers.append(owned)
        runtime.auxiliary.append(owned)
    for child, identity, worker_id, tool, descriptor in workers:
        runtime.beats[worker_id] = SimpleNamespace(worker_id=worker_id, tool_name=tool, runtime=descriptor.runtime,
            source_revision=descriptor.source_revision, source_sha256=descriptor.source_sha256, observed_at=instant)
    def read(request):
        runtime.reads.append(request.worker_id)
        return runtime.beats[request.worker_id]
    runtime.effects.canonical_reader = read
    return runtime, now


def test_r14f2_loop_heartbeat_throttles_each_worker_only():
    runtime, now = heartbeat_case()
    beat = runtime.loop_heartbeat(100)
    assert beat is runtime.beats[runtime.worker_id] and len(runtime.reads) == 3
    runtime.loop_heartbeat(100)
    now.value = 1.99
    runtime.loop_heartbeat(100)
    assert len(runtime.reads) == 3
    now.value = 2.0
    runtime.loop_heartbeat(100)
    assert len(runtime.reads) == 6 and len(set(runtime.reads)) == 3
    runtime.auxiliary[0][0].identity = SimpleNamespace(role="foreign", start_id="reused")
    with pytest.raises(s.ReplayRefused): runtime.loop_heartbeat(100)


def test_r14f2_heartbeat_revalidates_during_loop_cache():
    runtime, now = heartbeat_case()
    runtime.loop_heartbeat(100)
    runtime.heartbeat(100)
    runtime.heartbeat(100)
    assert len(runtime.reads) == 9
    for worker_id, beat in runtime.beats.items():
        original = beat.source_sha256
        beat.source_sha256 = "b" * 64
        with pytest.raises(s.ReplayRefused): runtime.heartbeat(100)
        beat.source_sha256 = original
