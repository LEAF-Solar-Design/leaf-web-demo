"""Read-only solar execution binds durable requests to stored graph versions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import broker_client
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_local_read as local
import solar_solve_results
import solar_tools
import store
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held, run as commit
from test_w1_local_graph_broker import rails  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_solve_commit import seed

TENANT = "fixture-tenant"
TOOL = "solar-select-by-zone"
JOB = "read-job"
SELECTED = {"status": "selected", "panel_refs": [
    "leaf:panel:00000000-0000-4000-8000-%012d" % number for number in (1, 2, 3)]}
OUTPUT_SHA = "507886d2ecd51d59fbeb788048a180d4989a4183cad10771c1ee43f5589672f2"
REQUEST_SHA = "df0bc999488ca7eaffc5192d55b11f175876fd4124f0fd64bbfe50ac91402b03"
RESULT_KEYS = {
    "schema_version", "adapter", "tenant_id", "job_id", "tool", "project_id", "drawing_id",
    "request_sha256", "source_version", "representation", "graph_sha256", "output",
    "output_sha256", "output_bytes", "drawing_changed",
}


def params():
    return {"drawing_id": "solar", "zone_name": "Roof"}


def forbidden(*args, **kwargs):
    pytest.fail("read execution must not write, check out, or dispatch dynamically")


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    value, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: value)
    return value


def run(backend, **overrides):
    args = dict(tenant_id=TENANT, tool=TOOL, params=params(), drawing_id="solar",
                source_version=1, job_id=JOB)
    args.update(overrides)
    return local.run_local_graph_read(backend, **args)


def proof(result, backend, **overrides):
    args = dict(params=params(), tenant_id=TENANT, job_id=JOB, tool=TOOL,
                source_version=1, backend=backend)
    args.update(overrides)
    return local.graph_read_provenance(result, **args)


def expected_proof(result):
    return {"execution_mode": "local_graph_read", "adapter": "local-graph-read",
            "request_sha256": REQUEST_SHA, "graph_sha256": result["graph_sha256"],
            "source_version": 1, "output_sha256": OUTPUT_SHA}


def test_read_returns_bound_output_and_publishes_nothing(backend, graph, monkeypatch):
    # K1: no write API or checkout may be reached.
    before = store.load_manifest(backend, TENANT, "solar")
    monkeypatch.setattr(solar_local_graph, "publish_version", forbidden)
    monkeypatch.setattr(solar_solve_results, "publish_version", forbidden)
    monkeypatch.setattr(backend, "put", forbidden)
    monkeypatch.setattr(store, "authorize_checkout", forbidden)
    monkeypatch.setattr(store, "acquire_checkout_fence", forbidden)
    result = run(backend)
    assert result == {
        "schema_version": "leaf.solar-graph-read.v1", "adapter": "local-graph-read",
        "tenant_id": TENANT, "job_id": JOB, "tool": TOOL,
        "project_id": graph["project"]["id"], "drawing_id": "solar",
        "request_sha256": REQUEST_SHA, "source_version": 1, "representation": "intake",
        "graph_sha256": digest(graph), "output": SELECTED, "output_sha256": OUTPUT_SHA,
        "output_bytes": 186, "drawing_changed": False,
    }
    after = store.load_manifest(backend, TENANT, "solar")
    assert after == before
    assert after["head"] == after["latest"] == 1


def test_read_output_isolates_mutating_builtin_arguments(backend, monkeypatch):
    context = local.resolve_graph_context(backend, TENANT, "solar", 1)
    original_graph = copy.deepcopy(context["graph"])
    caller_params = {**params(), "selection": {"labels": ["original"]}}
    original_params = copy.deepcopy(caller_params)

    def mutate(graph, builtin_params):
        graph["electrical_zones"][0]["panel_refs"].clear()
        builtin_params["selection"]["labels"].append("mutated")
        builtin_params["zone_name"] = "changed"
        return {"status": "mutated"}

    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(run=mutate))
    output, data = local._read_output(TOOL, context["graph"], caller_params)
    assert output == {"status": "mutated"}
    assert data == canonical_bytes(output)
    assert context["graph"] == original_graph
    assert caller_params == original_params
    assert local.resolve_graph_context(backend, TENANT, "solar", 1)["graph"] == original_graph


def test_read_result_keys_are_exact(backend):
    assert set(run(backend)) == RESULT_KEYS
    assert len(RESULT_KEYS) == 15


def test_read_proof_rederives_output(backend, monkeypatch):
    # K2 and K5: source and request are re-resolved on every proof.
    result = run(backend)
    original_resolve = local.resolve_graph_context
    original_output = local._read_output
    calls = []

    def resolve(*args, **kwargs):
        calls.append(("resolve", args[1:]))
        return original_resolve(*args, **kwargs)

    def output(*args):
        calls.append(("output", args[0], copy.deepcopy(args[2])))
        return original_output(*args)

    monkeypatch.setattr(local, "resolve_graph_context", resolve)
    monkeypatch.setattr(local, "_read_output", output)
    assert proof(result, backend) == expected_proof(result)
    assert calls == [("resolve", (TENANT, "solar", 1)), ("output", TOOL, {"zone_name": "Roof"})]
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(result, backend, params={"drawing_id": "solar", "zone_name": "Attic"})
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other"), ("adapter", "other"), ("tool", "solar-settings"),
    ("tenant_id", "other"), ("job_id", "other"), ("drawing_id", "other"),
    ("source_version", 2), ("representation", "dwg-bundle"), ("graph_sha256", "0" * 64),
    ("request_sha256", "0" * 64), ("output_sha256", "0" * 64), ("output_bytes", 187),
    ("drawing_changed", True), ("extra", 1), ("missing", None),
    ("project_id", "other"), ("source_version", True),
])
def test_read_proof_rejects_mutations(backend, field, value):
    # K4 includes an added and a removed key.
    result = run(backend)
    if field == "missing":
        del result["output"]
    else:
        result[field] = value
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(result, backend)


def test_read_proof_rejects_a_forged_output_with_matching_digest(backend):
    # K3: a self-consistent forged receipt is still not evidence.
    result = run(backend)
    result["output"] = {"status": "selected", "panel_refs": SELECTED["panel_refs"][:1]}
    result["output_sha256"] = digest(result["output"])
    result["output_bytes"] = len(canonical_bytes(result["output"]))
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(result, backend)


@pytest.mark.parametrize("size,code", [(1048565, None), (1048566, "READ_OUTPUT_LIMIT_EXCEEDED")])
def test_read_output_cap_boundary(backend, monkeypatch, size, code):
    # K8 and K9 pin canonical bytes, including the object syntax.
    monkeypatch.setattr(local, "_load_builtin",
                        lambda tool: SimpleNamespace(run=lambda graph, params: {"blob": "x" * size}))
    if code:
        with pytest.raises(GraphValidationError) as exc:
            run(backend)
        assert exc.value.code == code
    else:
        result = run(backend)
        assert result["output_bytes"] == 1048576 == local.MAX_OUTPUT_BYTES
        assert proof(result, backend)["output_sha256"] == digest(result["output"])


@pytest.mark.parametrize("output", [[], {"v": float("nan")}])
def test_read_output_must_be_a_json_object(backend, monkeypatch, output):
    # K10
    monkeypatch.setattr(local, "_load_builtin",
                        lambda tool: SimpleNamespace(run=lambda graph, params: output))
    with pytest.raises(GraphValidationError) as exc:
        run(backend)
    assert exc.value.code == "READ_OUTPUT_INVALID"


@pytest.mark.parametrize("error,code", [(KeyError("x"), "LOCAL_GRAPH_READ_FAILED"),
                                        (GraphValidationError("ZONE_X"), "ZONE_X")])
def test_read_builtin_failure_is_named(backend, monkeypatch, error, code):
    # K11
    def fail(graph, params):
        raise error
    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(run=fail))
    with pytest.raises(GraphValidationError) as exc:
        run(backend)
    assert exc.value.code == code


def test_read_builtin_without_history_gets_two_positional_arguments(backend, monkeypatch):
    # (e) Every builtin that does not declare READS_VERSION_HISTORY is called exactly as before.
    calls = []

    def spy(*args, **kwargs):
        calls.append((len(args), kwargs))
        return {"status": "spied"}

    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(run=spy))
    result = run(backend)
    assert proof(result, backend)["output_sha256"] == digest({"status": "spied"})
    assert calls == [(2, {}), (2, {})]


def _history_builtin(monkeypatch, asks):
    """A builtin that declares the history need and asks the lookup for each version in asks."""
    def spy(graph, params, version_graph_sha256=None):
        return {"answers": [version_graph_sha256(version) for version in asks]}

    monkeypatch.setattr(local, "_load_builtin", lambda tool: SimpleNamespace(
        READS_VERSION_HISTORY=True, run=spy))


def test_read_history_lookup_answers_this_drawing_only(backend, graph, monkeypatch):
    # (f) The real graph_sha256 for an existing version; None for a missing, bool or zero version.
    _history_builtin(monkeypatch, [1, 99])
    result = run(backend)
    assert result["output"] == {"answers": [digest(graph), None]}
    assert result["output"]["answers"][0] == result["graph_sha256"]
    assert proof(result, backend)["output_sha256"] == digest(result["output"])
    _history_builtin(monkeypatch, [True, 0])
    assert run(backend)["output"] == {"answers": [None, None]}


def test_read_history_lookup_is_bounded(backend, monkeypatch):
    # (g) A third lookup in one read fails closed, in the read and in its terminal proof.
    _history_builtin(monkeypatch, [1, 1])
    result = run(backend)
    _history_builtin(monkeypatch, [1, 1, 1])
    with pytest.raises(GraphValidationError) as exc:
        run(backend)
    assert exc.value.code == "READ_HISTORY_LIMIT_EXCEEDED"
    with pytest.raises(ValueError, match="^graph read terminal proof rejected$"):
        proof(result, backend)


@pytest.mark.parametrize("tool", ["solar-settings", "solar-unregistered"])
def test_read_refuses_unknown_and_commit_tools(backend, tool):
    # K12 checks admission and the loader independently.
    with pytest.raises(GraphValidationError) as exc:
        run(backend, tool=tool)
    assert exc.value.code == "UNKNOWN_LOCAL_GRAPH_READ_TOOL"
    with pytest.raises(GraphValidationError) as exc:
        local._load_builtin(tool)
    assert exc.value.code == "UNKNOWN_LOCAL_GRAPH_READ_TOOL"


@pytest.mark.parametrize("override,code", [
    ({"params": []}, "INVALID_ZONE_SELECTION"),
    ({"params": {"zone_name": 1e15}}, "INVALID_NUMERIC_PARAM"),
    ({"source_version": 0}, "INVALID_SOURCE_VERSION"),
    ({"source_version": True}, "INVALID_SOURCE_VERSION"),
    ({"source_version": "1"}, "INVALID_SOURCE_VERSION"),
    ({"params": {"drawing_id": "other", "zone_name": "Roof"}}, "DRAWING_ID_CONFLICT"),
    ({"params": {**params(), "initialize": {}}}, "READ_SEED_UNSUPPORTED"),
])
def test_read_request_refusals_are_named(backend, override, code):
    # K13
    with pytest.raises(GraphValidationError) as exc:
        run(backend, **override)
    assert exc.value.code == code


def test_read_accepts_a_non_head_version(backend):
    # K6: advance the real manifest with the existing commit adapter.
    with held(backend) as fence:
        commit(backend, fence=fence)
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2
    result = run(backend)
    assert result["source_version"] == 1
    assert result["output"] == SELECTED
    assert proof(result, backend) == expected_proof(result)


def test_read_accepts_a_dwg_bundle_representation(backend, monkeypatch):
    # K7: the resolver's commit-only refusal is irrelevant to this kind.
    resolve = local.resolve_graph_context

    def bundle(*args, **kwargs):
        return dict(resolve(*args, **kwargs), representation="dwg-bundle",
                    local_commit_ready=False, refusal_reason="licensed_graph_commit_required")

    monkeypatch.setattr(local, "resolve_graph_context", bundle)
    result = run(backend)
    assert result["representation"] == "dwg-bundle"
    assert result["output"] == SELECTED
    assert proof(result, backend) == expected_proof(result)


def submission():
    return dict(tenant_id=TENANT, tool=solar_tools.trusted_record(TOOL), params=params(),
                dwg="solar", aps_live=False, dwg_version=1,
                checkout_holder=store.ANONYMOUS_HOLDER, checkout_fence=None)


def test_read_admission_needs_no_checkout(isolated_jobs, monkeypatch):
    # R1
    class QueuedExecutor:
        def submit(self, *args, **kwargs):
            pass

    args = submission()
    monkeypatch.setattr(store, "authorize_checkout", forbidden)
    monkeypatch.setattr(jobs, "_executors", {
        jobs.lane_for(args["tool"], False): QueuedExecutor()})
    job_id = jobs.submit_job(**args)
    rec = jobs.get_job(job_id)
    assert rec["status"] == "submitted"
    assert rec["dwg_version"] == 1
    row = jobs._query("SELECT execution_json FROM jobs WHERE job_id = ?", (job_id,))[0]
    execution = json.loads(row["execution_json"])
    assert execution["graph_read"] == {"tenant_id": TENANT}
    assert "graph_commit" not in execution
    assert execution["checkout_holder"] == store.ANONYMOUS_HOLDER
    assert execution["checkout_fence"] is None


@pytest.mark.parametrize("override", [
    {"aps_live": True}, {"dwg_version": None}, {"dwg_version": 0}, {"dwg_version": True},
    {"params": {"drawing_id": 7, "zone_name": "Roof"}},
    {"params": {"drawing_id": "other", "zone_name": "Roof"}},
    {"params": {"drawing_id": "solar", "zone_name": 1e15}},
])
def test_read_submission_refused_before_insert(isolated_jobs, override):
    # R2
    args = submission()
    args.update(override)
    with pytest.raises(ValueError):
        jobs.submit_job(**args)
    assert not jobs._query("SELECT job_id FROM jobs")


def broker_call(rails, **overrides):
    broker = rails[0]
    args = dict(tenant_id=TENANT, tool=solar_tools.trusted_record(TOOL), params=params(),
                dwg="solar", dwg_version=1, job_id=JOB, aps_live=False,
                checkout_holder=None, checkout_fence=None)
    args.update(overrides)
    response = broker._broker_run(broker.BrokerRunRequest(**args))
    return response.status_code, json.loads(response.body)


def test_broker_read_branch(rails, monkeypatch):
    # R3 uses the real broker while its commit fixture holds a checkout.
    monkeypatch.setattr(rails[0], "run_tool_dynamic", forbidden)
    monkeypatch.setattr(store, "authorize_checkout", forbidden)
    status, body = broker_call(rails)
    assert status == 200
    assert body["ok"] is True
    assert body["degraded_mode"] is False
    assert body["result"] == run(rails[1])
    assert set(body["result"]) == RESULT_KEYS
    assert store.load_manifest(rails[1], TENANT, "solar")["head"] == 1


@pytest.mark.parametrize("mode,reason", [
    ("aps", "local_graph_read_invalid"), ("source", "local_graph_read_invalid"),
    ("version", "local_graph_read_invalid"), ("entry", "local_graph_read_invalid"),
    ("file", "file_only_request_invalid"),
])
def test_broker_read_forbidden_modes(rails, mode, reason):
    # R4
    overrides = {}
    if mode == "aps":
        overrides["aps_live"] = True
    elif mode == "source":
        overrides["test_source"] = "def run(graph, params): return {}"
    elif mode == "file":
        overrides.update(file_only=True, test_source="def run(graph, params): return {}")
    else:
        tool = solar_tools.trusted_record(TOOL)
        tool[mode] = "9.9.9" if mode == "version" else "elsewhere.py"
        overrides["tool"] = tool
    status, body = broker_call(rails, **overrides)
    assert status == 400
    assert body["ok"] is False
    assert body["error"]["reason_code"] == reason


@pytest.mark.parametrize("override,reason", [
    ({"job_id": None}, "JOB_IDENTITY_MISSING"),
    ({"dwg_version": None}, "INVALID_SOURCE_VERSION"),
    ({"params": {"drawing_id": "other", "zone_name": "Roof"}}, "DRAWING_ID_CONFLICT"),
    ({"entity_scope": {"drawing_id": "solar", "base_version": 1,
                      "base_source_sha256": "a" * 64, "allowed_handles": ["A1"]}},
     "entity_scope_mutation_unsupported"),
    ({"params": {"zone_name": 7}}, "tool_params_invalid"),
])
def test_broker_read_refusals(rails, override, reason):
    # R5
    status, body = broker_call(rails, **override)
    assert status == 400
    assert body["ok"] is False
    assert body["error"]["reason_code"] == reason


def client_args():
    return dict(tenant_id=TENANT, tool=solar_tools.trusted_record(TOOL), params=params(),
                dwg="solar", aps_live=False, job_id=JOB, dwg_version=1)


@pytest.mark.parametrize("mutation", [
    "valid", "schema_version", "adapter", "tenant_id", "job_id", "tool",
    "source_version", "output", "output_sha256", "ok", "nonjson",
])
def test_broker_client_read_receipt(backend, monkeypatch, mutation):
    # R6
    body = {"ok": True, "result": run(backend)}
    if mutation in {"schema_version", "adapter", "tenant_id", "job_id", "tool", "output_sha256"}:
        body["result"][mutation] = "x"
    elif mutation == "source_version":
        body["result"][mutation] = 2
    elif mutation == "output":
        body["result"][mutation] = []
    elif mutation == "ok":
        body["ok"] = "yes"

    class Reply:
        status_code = 200

        def json(self):
            if mutation == "nonjson":
                raise ValueError("not JSON")
            return body

    monkeypatch.setattr(broker_client.requests, "post", lambda *a, **k: Reply())
    if mutation == "valid":
        assert broker_client.run_via_broker(**client_args()) is body
    else:
        with pytest.raises(broker_client.BrokerReceiptRejected):
            broker_client.run_via_broker(**client_args())


@pytest.mark.parametrize("override", [
    {"aps_live": True}, {"file_only": True}, {"test_source": "fixture"}, {"job_id": None},
    {"dwg_version": None}, {"dwg_version": 0}, {"dwg_version": True},
    {"params": {"drawing_id": "other"}},
])
def test_read_client_refuses_before_post(monkeypatch, override):
    monkeypatch.setattr(broker_client.requests, "post", forbidden)
    args = client_args()
    args.update(override)
    with pytest.raises(ValueError):
        broker_client.run_via_broker(**args)


@pytest.mark.parametrize("mutation", [
    "valid", "aps", "cloud", "fallback", "job", "context", "provenance", "version",
])
def test_read_terminal_context(backend, mutation):
    result = run(backend)
    provenance = {"attempt": 1, "execution_path": "local", **proof(result, backend)}
    execution = {"tool": solar_tools.trusted_record(TOOL), "aps_live": False, "dwg_version": 1,
                 "graph_read": {"tenant_id": TENANT}}
    job_id = JOB
    if mutation == "aps":
        execution["aps_live"] = True
    elif mutation == "cloud":
        provenance["execution_path"] = "cloud"
    elif mutation == "fallback":
        provenance["fallback"] = True
    elif mutation == "job":
        job_id = None
    elif mutation == "context":
        execution.pop("graph_read")
    elif mutation == "provenance":
        provenance["output_sha256"] = "0" * 64
    elif mutation == "version":
        execution.pop("dwg_version")
    args = ("complete", {"ok": True, "result": result}, provenance, 1, execution)
    if mutation == "valid":
        jobs._validate_terminal_context(*args, job_id=job_id, durable_params=params())
    else:
        with pytest.raises(ValueError):
            jobs._validate_terminal_context(*args, job_id=job_id, durable_params=params())


def test_read_kind_is_excluded_from_local_fallback():
    # R13
    assert not jobs._allows_local_fallback(dict(solar_tools.trusted_record(TOOL),
                                                allow_local_fallback=True))


def test_commit_and_cloud_predicates_ignore_the_read_kind():
    # R13
    assert availability.is_local_graph_read({"name": TOOL})
    assert not availability.is_local_graph_read({"name": "solar-settings"})
    assert not availability.is_local_graph_read({"name": "solar-solve-proposal"})
    assert not availability.is_local_graph_commit({"name": TOOL})
    assert not availability.is_cloud_proposal({"name": TOOL})
    with pytest.raises(TypeError, match="tool record must be a mapping"):
        availability.is_local_graph_read("x")


def test_read_source_has_no_dynamic_import():
    source = Path(local.__file__).read_text(encoding="utf-8")
    assert all(word not in source for word in (
        "tool_loader", "import_module", "exec(", "publish_version", "solar-select-by-zone"))
