"""Production-shaped uploaded-DWG live-read producer contract.

This fixture freezes the wire sequence used by the protected Terraform
acceptance without making an HTTP, APS, or AWS call. It uses the real upload
store, catalog tool, broker request, Activity script resolver, and terminal
envelope shape. Every reachable non-retryable BAD_PARAMS exit after job
creation must carry a stable reason_code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

import broker
import broker_client
import catalog
import deps
import guest_uploads
import jobs
import session_store
import store
import write_loop
from routers.mcp_gateway import AttachmentExchangeRequest
from routers.sessions import CreateSessionRequest, MessageRequest


TENANT = "11111111-1111-4111-8111-111111111111"
DRAWING = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
TURN = "44444444-4444-4444-8444-444444444444"
JOB = "55555555-5555-4555-8555-555555555555"
WORKER = "66666666-6666-4666-8666-666666666666"
PROJECTION_SHA256 = "d6312575858c07d044a9c881a84035998fcdd3a7b1d979ceddff6735de63adb3"
BAD_PARAMS_MESSAGES = {
    "broker_event_key_required": (
        "ledger_event_key is required when LEAF_BROKER_STORE=postgres"
    ),
    "broker_event_key_invalid": (
        "broker run event key is not valid for this request"
    ),
    "tool_params_invalid": "params schema: missing field",
    "uploaded_resolution_invalid": "unexpected resolver failure",
    "live_activity_unavailable": (
        "tool 'count-by-layer' has no usable live (APS) implementation "
        "(its resolved Activity script is empty/unreadable); live "
        "(APS_LIVE=1) runs of this tool are not supported; run with "
        "aps_live=false"
    ),
}


def _error_fixtures():
    fixtures = {}
    for reason_code, message in BAD_PARAMS_MESSAGES.items():
        envelope, status = broker._classified_bad_params(
            reason_code, message, tool="count-by-layer"
        )
        assert status == 400
        fixtures[reason_code] = envelope
    return fixtures


def _frozen_projection(reason_code):
    error = _error_fixtures()[reason_code]["error"]
    return {
        "job": {
            "job_id": JOB,
            "tenant_id": TENANT,
            "tool": "count-by-layer",
            "dwg": DRAWING,
            "dwg_version": 1,
            "status": "failed",
            "result": None,
            "error": error,
        },
        "events": [
            {
                "v": 1,
                "session_id": SESSION,
                "turn_id": TURN,
                "seq": 1,
                "type": "tool_call",
                "data": {
                    "tool": "run_capability",
                    "args_summary": "tool=count-by-layer params={}",
                },
            },
            {
                "v": 1,
                "session_id": SESSION,
                "turn_id": TURN,
                "seq": 2,
                "type": "job_linked",
                "data": {"job_id": JOB, "tool": "count-by-layer"},
            },
            {
                "v": 1,
                "session_id": SESSION,
                "turn_id": TURN,
                "seq": 3,
                "type": "turn_complete",
                "data": {},
            },
        ],
    }


def test_cross_repository_projection_hash_is_frozen():
    encoded = json.dumps(
        [_frozen_projection(reason) for reason in BAD_PARAMS_MESSAGES],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == PROJECTION_SHA256


def _ready_upload(tmp_path: Path):
    backend = store.InMemoryBackend()
    source = (broker.PROJECT_ROOT / "data" / "rooftop_demo.dwg").read_bytes()
    local = tmp_path / "uploaded.dwg"
    local.write_bytes(source)
    store.ingest_drawing(backend, TENANT, str(local), drawing_id=DRAWING)
    intake_ref = write_loop.publish_intake_cache(
        backend,
        TENANT,
        DRAWING,
        1,
        source,
        {"layers": [], "polylines": []},
    )
    marker = guest_uploads.new_marker(
        filename="uploaded.dwg",
        data=source,
        tenant_kind="account",
        source_ext=".dwg",
    )
    marker.update(
        status="ready",
        extracted_version=1,
        intake_ref=intake_ref,
        intake_sha256=hashlib.sha256(backend.get(intake_ref)).hexdigest(),
    )
    guest_uploads.write_marker(backend, TENANT, DRAWING, marker)
    return backend, source, marker


def _tool():
    tool = deps.find_tool("count-by-layer", TENANT)
    assert tool is not None
    assert tool["aps_live"] is True
    assert broker.validate_params(tool, {}) == []
    return tool


def _live_runtime_authorized(tool, source):
    view = deps.catalog_tool_view(tool)
    return catalog.live_aps_runtime_authorized(
        view,
        aps_live_enabled=True,
        trusted_live_catalog_digests={
            deps.catalog_tool_digest(row)
            for row in deps.load_engine_registry_tools()
            if row.get("aps_live") is True
        },
        tool_source=source,
        operator_owned_engine_source=deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
    )


def test_effective_canonical_row_is_the_only_live_aps_authority(monkeypatch):
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda _tenant: None)
    rows = deps.effective_tools_with_provenance(TENANT)
    tool, source = next(
        (row, row_source) for row, row_source in rows
        if row.get("name") == "count-by-layer"
    )

    assert source == deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE
    assert _live_runtime_authorized(tool, source) is True
    spec = importlib.util.spec_from_file_location(
        "p6_live_aps_da_client", Path(__file__).parents[2] / "da" / "client.py"
    )
    assert spec is not None and spec.loader is not None
    da = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(da)
    assert broker._live_script_is_nonempty(tool, da) is True


def test_same_name_tenant_python_row_cannot_enter_live_aps(
    monkeypatch, tmp_path,
):
    tenant_root = tmp_path / "effective-tenant"
    tenant_root.mkdir()
    (tenant_root / "registry.json").write_text(json.dumps({"tools": [{
        "name": "count-by-layer",
        "version": "tenant-shadow",
        "kind": "script",
        "engine_op": "count_by_layer",
        "entry": "tools/count-by-layer/tool.py",
        "aps_live": True,
        "capabilities": ["drawing.read"],
        "params": {"type": "object", "properties": {}},
    }]}), encoding="utf-8")
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda _tenant: tenant_root)

    rows = deps.effective_tools_with_provenance(TENANT)
    tool, source = next(
        (row, row_source) for row, row_source in rows
        if row.get("name") == "count-by-layer"
    )

    assert source == deps.TOOL_SOURCE_TENANT_REPO
    assert tool["entry"] == "tools/count-by-layer/tool.py"
    assert _live_runtime_authorized(tool, source) is False
    assert "script" not in tool and "engine_script" not in tool


@pytest.mark.parametrize(
    ("effective_source", "expected_live"),
    [
        (deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE, True),
        (deps.TOOL_SOURCE_TENANT_REPO, False),
    ],
)
def test_run_route_derives_live_aps_from_effective_row_authority(
    monkeypatch, effective_source, expected_live,
):
    from routers import jobs as jobs_router

    canonical = next(
        row for row in deps.load_engine_registry_tools()
        if row.get("name") == "count-by-layer"
    )
    effective = canonical if expected_live else {
        "name": "count-by-layer",
        "version": "tenant-shadow",
        "kind": "script",
        "engine_op": "count_by_layer",
        "entry": "tools/count-by-layer/tool.py",
        "aps_live": True,
        "capabilities": ["drawing.read"],
        "params": {"type": "object", "properties": {}},
    }
    monkeypatch.setattr(jobs_router.deps, "APS_LIVE", True)
    monkeypatch.setattr(
        jobs_router.deps, "find_tool", lambda *_args: effective
    )
    monkeypatch.setattr(
        jobs_router.deps,
        "effective_tools_with_provenance",
        lambda *_args: [(effective, effective_source)],
    )
    captured = {}

    def fake_submit_job(*_args, aps_live=None, **_kwargs):
        captured["aps_live"] = aps_live
        return "effective-authority-job"

    monkeypatch.setattr(jobs_router.jobs, "submit_job", fake_submit_job)
    req = jobs_router.RunRequest(
        tool="count-by-layer",
        params={},
        dwg=DRAWING,
        catalog_digest=jobs_router.deps.catalog_tool_digest(effective),
    )

    response = jobs_router.run(
        req,
        wait=0,
        tenant_id=TENANT,
        x_org_id=None,
        x_project_id=None,
        idempotency_key=None,
        authorization=None,
    )

    assert response.status_code == 202
    assert captured == {"aps_live": expected_live}


def _request(tool=None, *, event_key=f"{JOB}:broker-run"):
    return broker.BrokerRunRequest(
        tenant_id=TENANT,
        tool=tool or _tool(),
        params={},
        dwg=DRAWING,
        dwg_version=1,
        aps_live=True,
        ledger_event_key=event_key,
        job_id=JOB,
    )


def _install_happy_runtime(monkeypatch, tmp_path):
    backend, source, marker = _ready_upload(tmp_path)
    monkeypatch.setattr(
        write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_broker_store_mode", lambda: "legacy")
    monkeypatch.setattr(broker, "tenant_disabled", lambda _tenant: False)
    monkeypatch.setattr(broker, "_cap_preflight", lambda *_args: None)
    monkeypatch.setattr(broker, "_run_quota_preflight", lambda *_args: None)
    monkeypatch.setenv("LEAF_ALLOW_LIVE_APS_IN_TESTS", "1")
    da = broker._get_da()
    assert da is not None
    monkeypatch.setattr(da, "ensure_tool_activity", lambda _tool: None)
    monkeypatch.setattr(broker, "_get_da", lambda: da)
    monkeypatch.setattr(
        broker,
        "_run_live_tool",
        lambda _da, local, _tool, _params, **_kwargs: {
            "ok": True,
            "result": {"counts": {"Panels": 2345}, "total": 2345},
            "error": None,
            "degraded_mode": False,
            "source_sha256": hashlib.sha256(Path(local).read_bytes()).hexdigest(),
        },
    )
    return source, marker


def _body(response):
    return json.loads(response.body)


def test_exact_producer_to_transcript_shapes_are_coherent(monkeypatch, tmp_path):
    source, marker = _install_happy_runtime(monkeypatch, tmp_path)

    session = CreateSessionRequest(
        drawing_id=DRAWING,
        model="claude-sonnet-5",
        policy="auto_approve_reads",
    ).model_dump()
    attachment = AttachmentExchangeRequest(
        session_id=SESSION,
        authority_session_id=SESSION,
        authority_turn_id=TURN,
        subscription_mount_id="subscription-mount-1",
        runner_profile_id="standard-services",
    ).model_dump()
    message = MessageRequest(
        text=(
            "Acceptance check: call run_capability exactly once with tool "
            "count-by-layer and params {} against this session's uploaded drawing."
        ),
        model="claude-sonnet-5",
    ).model_dump(exclude_none=True)

    response = broker.broker_run(_request())
    result = _body(response)
    job = {
        "job_id": JOB,
        "tenant_id": TENANT,
        "tool": "count-by-layer",
        "dwg": DRAWING,
        "dwg_version": 1,
        "status": "complete",
        "result": result,
    }
    transcript = [
        {
            "seq": 1,
            "turn_id": TURN,
            "type": "tool_call",
            "data": {
                "tool": "run_capability",
                "args_summary": "tool=count-by-layer params={}",
            },
        },
        {
            "seq": 2,
            "turn_id": TURN,
            "type": "job_linked",
            "data": {"job_id": JOB, "tool": "count-by-layer"},
        },
        {"seq": 3, "turn_id": TURN, "type": "turn_complete", "data": {}},
    ]

    assert response.status_code == 200
    assert marker["status"] == "ready" and marker["extracted_version"] == 1
    assert session == {
        "drawing_id": DRAWING,
        "model": "claude-sonnet-5",
        "policy": "auto_approve_reads",
    }
    assert attachment["authority_turn_id"] == TURN
    assert message["text"].endswith("uploaded drawing.")
    assert job["status"] == "complete" and job["dwg_version"] == 1
    assert job["result"]["result"] == {
        "counts": {"Panels": 2345}, "total": 2345}
    assert job["result"]["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert [event["type"] for event in transcript] == [
        "tool_call", "job_linked", "turn_complete"]


@pytest.mark.parametrize(
    ("failure", "reason_code"),
    [
        ("params", "tool_params_invalid"),
        ("resolver", "uploaded_resolution_invalid"),
        ("activity", "live_activity_unavailable"),
    ],
)
def test_reachable_live_bad_params_are_machine_classified(
        monkeypatch, tmp_path, failure, reason_code):
    _install_happy_runtime(monkeypatch, tmp_path)
    tool = _tool()
    if failure == "params":
        monkeypatch.setattr(
            broker, "validate_params", lambda _tool, _params: ["missing field"])
    elif failure == "resolver":
        monkeypatch.setattr(
            broker,
            "_resolve_live_read_dwg",
            lambda _req: (_ for _ in ()).throw(ValueError("unexpected resolver failure")),
        )
    else:
        monkeypatch.setattr(broker, "_live_script_is_nonempty", lambda *_args: False)

    response = broker.broker_run(_request(tool=tool))
    envelope = _body(response)
    error = envelope["error"]

    assert response.status_code == 400
    assert envelope == _error_fixtures()[reason_code]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["retryable"] is False
    assert error["reason_code"] == reason_code


def test_degraded_second_upload_read_is_machine_classified(monkeypatch, tmp_path):
    _install_happy_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(broker, "_get_da", lambda: None)
    monkeypatch.setattr(
        write_loop,
        "backend_for_tenant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyError("second upload-store read failed")
        ),
    )

    response = broker.broker_run(_request())
    error = _body(response)["error"]

    assert response.status_code == 400
    assert error["error_code"] == "BAD_PARAMS"
    assert error["retryable"] is False
    assert error["reason_code"] == "uploaded_resolution_invalid"


class _AdmissionStore:
    def __init__(self, status):
        self.status = status

    def admit_run(self, *_args, **_kwargs):
        return {"status": self.status}


@pytest.mark.parametrize(
    ("event_key", "admission", "reason_code"),
    [
        (None, None, "broker_event_key_required"),
        (f"{JOB}:broker-run", "collision", "broker_event_key_invalid"),
        (f"{JOB}:broker-run", "mismatch", "broker_event_key_invalid"),
    ],
)
def test_postgres_event_key_refusals_are_machine_classified(
        monkeypatch, tmp_path, event_key, admission, reason_code):
    _install_happy_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(broker, "_broker_store_mode", lambda: "postgres")
    monkeypatch.setattr(broker, "_get_usage", lambda: None)
    monkeypatch.setattr(broker, "_tenant_tier", lambda _tenant: "standard")
    if admission is not None:
        monkeypatch.setattr(
            broker, "_postgres_store", lambda: _AdmissionStore(admission))

    response = broker.broker_run(_request(event_key=event_key))
    envelope = _body(response)
    error = envelope["error"]

    assert response.status_code == 400
    assert envelope == _error_fixtures()[reason_code]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["retryable"] is False
    assert error["reason_code"] == reason_code


def test_app_job_runner_uses_one_stable_broker_event_key():
    source = Path(jobs.__file__).read_text(encoding="utf-8")
    assert 'ledger_event_key=f"{job_id}:broker-run"' in source
    assert source.count('ledger_event_key=f"{job_id}:broker-run"') == 1


class _InertExecutor:
    def submit(self, _fn, *_args, **_kwargs):
        return None


@pytest.mark.parametrize("reason_code", tuple(BAD_PARAMS_MESSAGES))
def test_broker_error_survives_durable_job_and_transcript_projection(
        monkeypatch, request, tmp_path, reason_code):
    jobs.reset_connection()
    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(
        jobs,
        "_executors",
        {jobs.LANE_FAST: _InertExecutor(), jobs.LANE_SLOW: _InertExecutor()},
    )
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *_a, **_k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *_a, **_k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *_a, **_k: None)
    request.addfinalizer(jobs.reset_connection)

    if session_store._conn is not None:
        session_store._conn.close()
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)

    def reset_session_store():
        if session_store._conn is not None:
            session_store._conn.close()
            session_store._conn = None

    request.addfinalizer(reset_session_store)

    real_uuid4 = session_store.uuid.uuid4
    monkeypatch.setattr(
        session_store.uuid, "uuid4", lambda: uuid.UUID(SESSION)
    )
    session = session_store.get_or_create_session(
        TENANT, DRAWING, model="claude-sonnet-5"
    )
    monkeypatch.setattr(session_store.uuid, "uuid4", real_uuid4)
    assert session["session_id"] == SESSION

    generated = iter((uuid.UUID(JOB), uuid.UUID(WORKER)))
    monkeypatch.setattr(jobs.uuid, "uuid4", lambda: next(generated))
    tool = _tool()
    job_id = jobs.submit_job(
        TENANT, tool, {}, DRAWING, True, dwg_version=1
    )
    assert job_id == JOB
    monkeypatch.setattr(
        broker_client,
        "run_via_broker",
        lambda *_args, **_kwargs: _error_fixtures()[reason_code],
    )
    jobs._run_job(job_id, TENANT, tool, {}, DRAWING, True, dwg_version=1)
    monkeypatch.setattr(jobs.uuid, "uuid4", real_uuid4)

    record = jobs.get_job(job_id)
    assert record is not None
    session_store.append_event(
        SESSION,
        TURN,
        "tool_call",
        {
            "tool": "run_capability",
            "args_summary": "tool=count-by-layer params={}",
        },
    )
    session_store.append_event(
        SESSION,
        TURN,
        "job_linked",
        {"job_id": JOB, "tool": "count-by-layer"},
    )
    session_store.append_event(SESSION, TURN, "turn_complete", {})

    projection = {
        "job": {
            key: record[key]
            for key in (
                "job_id", "tenant_id", "tool", "dwg", "dwg_version",
                "status", "result", "error",
            )
        },
        "events": session_store.recent_events(SESSION, 10),
    }
    assert projection == _frozen_projection(reason_code)
