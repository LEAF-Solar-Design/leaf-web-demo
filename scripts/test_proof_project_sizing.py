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
