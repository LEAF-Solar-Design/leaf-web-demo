"""ReciPDF job adapter: durable identity, captured validator and host evidence."""
from copy import deepcopy
import hashlib
import json
import sqlite3
import time
import uuid

import pytest

import campaign_capability_job as adapter
import customization_service
import deps
import jobs
import tool_loader
from job_pg_store import _record


SOURCE = '''def run(intake, params):
    return {"verified": True, "operation_id": intake["operation_id"],
            "input_sha256": intake["input_sha256"],
            "readback_sha256": intake["host_readback"]["readback_sha256"]}
'''


def context(tool=None, source=SOURCE):
    tool = tool or {"name": "campaign-host-enrollment", "version": "1", "entry": "validator.py"}
    return {**adapter.CONSTANTS, **{key: str(uuid.uuid4()) for key in adapter.IDS},
            "tenant_id": "tenant-test", "change_set_id": "change-test",
            "catalog_commit": "a" * 40, "effective_catalog_digest": "b" * 64,
            "tool_manifest_sha256": deps.catalog_tool_digest(tool),
            "tool_source_sha256": hashlib.sha256(source.encode()).hexdigest()}


def operation(ctx, jid, outcome="succeeded"):
    evidence = {"config_identity_before": None, "config_identity_after": "c" * 64,
                "readback_sha256": "d" * 64, "reason": "verified"}
    return {"operation_id": str(uuid.uuid4()), "job_id": jid,
            "enrollment_id": ctx["enrollment_id"], "link_id": ctx["link_id"],
            "profile_selector": ctx["profile_selector"],
            "input_sha256": adapter.input_sha256(jid, ctx),
            "stage": "readback", "outcome": outcome,
            "completed_stages": list(adapter.STAGES),
            "stage_evidence": {stage: {"outcome": "succeeded", "evidence": dict(evidence)}
                               for stage in adapter.STAGES}}


@pytest.fixture
def published(monkeypatch, tmp_path):
    import job_pg_store
    job_pg_store._db()  # Register the package, without opening a database connection.
    from leaf_platform import campaign_capabilities

    path = tmp_path / "validator.py"
    path.write_bytes(SOURCE.replace("\n", "\r\n").encode())
    tool = {"name": "campaign-host-enrollment", "version": "1", "entry": "validator.py",
            "params": {"type": "object", "additionalProperties": False}}
    ctx = context(tool)
    jid = str(uuid.uuid4())
    op = operation(ctx, jid)
    calls = []
    monkeypatch.setattr(customization_service, "effective_catalog_pin",
                        lambda tenant: {k: ctx[k] for k in ("catalog_commit", "effective_catalog_digest")})
    monkeypatch.setattr(deps, "effective_tools_with_provenance",
                        lambda tenant: [(tool, deps.TOOL_SOURCE_TENANT_REPO)])
    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tenant: tmp_path)
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "off")
    monkeypatch.setenv("LEAF_SANDBOX", "off")
    monkeypatch.setenv("LEAF_SANDBOX_TIMEOUT_S", "5")
    monkeypatch.setenv("JOB_LEASE_S", "60")
    monkeypatch.setenv("HEARTBEAT_STALE_S", "120")
    monkeypatch.setattr(campaign_capabilities, "ensure_operation",
                        lambda job, context: calls.append((job, deepcopy(context))))
    monkeypatch.setattr(campaign_capabilities, "read_operation", lambda *args: deepcopy(op))
    monkeypatch.setattr(jobs.broker_client, "run_via_broker",
                        lambda *a, **k: pytest.fail("drawing broker must not run"))
    return ctx, jid, tool, op, calls, path


def invoke(published, **kwargs):
    ctx, jid, tool, _, _, _ = published
    return adapter.run(jid, ctx, tool, kwargs.pop("params", {}),
                       kwargs.pop("heartbeat", lambda: True),
                       kwargs.pop("cancelled", lambda: False),
                       kwargs.pop("deadline", time.monotonic() + 20))


def test_actual_private_subprocess_uses_captured_normalized_source(published, monkeypatch):
    from leaf_platform import campaign_capabilities
    ctx, jid, tool, op, calls, path = published

    def read(*args):
        # Replacement after capture cannot change the code executed in the sandbox.
        path.write_text('raise RuntimeError("replacement must not execute")', encoding="utf-8")
        return deepcopy(op)

    monkeypatch.setattr(campaign_capabilities, "read_operation", read)
    assert tool_loader._sandbox_tier() == "off"
    result = invoke(published)
    assert result["ok"] is True
    assert result["tool"] == tool["name"]
    assert result["result"] == {"verified": True, "operation_id": op["operation_id"],
                                "input_sha256": op["input_sha256"], "readback_sha256": "d" * 64}
    assert calls == [(jid, ctx)]


@pytest.mark.parametrize("change", ["source", "catalog", "tool", "winner", "path", "params"])
def test_publication_or_params_fail_before_operation(published, monkeypatch, change):
    ctx, _, tool, _, calls, path = published
    if change == "source":
        path.write_text("wrong source", encoding="utf-8")
    elif change == "catalog":
        monkeypatch.setattr(customization_service, "effective_catalog_pin", lambda t: None)
    elif change == "tool":
        tool["version"] = "2"
    elif change == "winner":
        monkeypatch.setattr(deps, "effective_tools_with_provenance",
                            lambda t: [(tool, deps.TOOL_SOURCE_AUTHORED)])
    elif change == "path":
        tool["entry"] = "../validator.py"
        ctx["tool_manifest_sha256"] = deps.catalog_tool_digest(tool)
    result = invoke(published, params={"unexpected": True} if change == "params" else {})
    assert result["ok"] is False
    assert not calls


@pytest.mark.parametrize("return_source", [
    'return {"ok": True, "execution_provenance": {}}',
    'return {"verified": 1, "operation_id": intake["operation_id"], "input_sha256": intake["input_sha256"], "readback_sha256": intake["host_readback"]["readback_sha256"]}',
    'raise ValueError("secret stderr must not leak")',
])
def test_forged_or_failed_authored_return_is_not_authority(published, return_source):
    ctx, _, _, _, _, path = published
    source = "def run(intake, params):\n    " + return_source + "\n"
    path.write_text(source, encoding="utf-8")
    ctx["tool_source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    result = invoke(published)
    assert result["ok"] is False
    assert result["error"]["retryable"] is False
    assert "secret stderr" not in json.dumps(result)


@pytest.mark.parametrize("outcome", ["held", "failed"])
def test_terminal_host_failure_preserves_stage_observations(published, outcome):
    op = published[3]
    op["outcome"] = outcome
    op["completed_stages"] = ["apply"]
    before = deepcopy(op)
    result = invoke(published)
    assert not result["ok"] and not result["error"]["retryable"]
    assert op == before


@pytest.mark.parametrize("stop", ["closed", "lease", "deadline"])
def test_stopped_attempt_never_creates_operation(published, stop):
    options = {"cancelled": lambda: True} if stop == "closed" else (
        {"heartbeat": lambda: False} if stop == "lease" else {"deadline": time.monotonic() - 1})
    assert invoke(published, **options)["ok"] is False
    assert not published[4]


def test_wait_rechecks_cancellation_and_second_catalog_pin(published, monkeypatch):
    from leaf_platform import campaign_capabilities
    state = {"cancelled": False}
    op = published[3]
    op["outcome"] = None

    def read(*args):
        state["cancelled"] = True
        return deepcopy(op)

    monkeypatch.setattr(campaign_capabilities, "read_operation", read)
    monkeypatch.setattr(tool_loader, "_run_source_in_sandbox",
                        lambda *a: pytest.fail("cancelled validator started"))
    assert not invoke(published, cancelled=lambda: state["cancelled"])["ok"]
    assert len(published[4]) == 1


def test_catalog_drift_after_host_readback_cannot_start_validator(published, monkeypatch):
    calls = []
    ctx = published[0]

    def pin(tenant):
        calls.append(tenant)
        return ({k: ctx[k] for k in ("catalog_commit", "effective_catalog_digest")}
                if len(calls) == 1 else None)

    monkeypatch.setattr(customization_service, "effective_catalog_pin", pin)
    monkeypatch.setattr(tool_loader, "_run_source_in_sandbox", lambda *a: pytest.fail("drift executed"))
    assert not invoke(published)["ok"]
    assert len(published[4]) == 1


def test_closed_context_and_exact_success_evidence():
    ctx = context()
    assert adapter.validate_context(ctx) == ctx
    for invalid in (None, {**ctx, "machine_id": "host"}, {**ctx, "org_id": "foreign"}):
        with pytest.raises((ValueError, TypeError)):
            adapter.execution_context({"capability_provenance": invalid})
    jid = str(uuid.uuid4())
    op = operation(ctx, jid)
    assert adapter.validate_operation(jid, ctx, op) == op["stage_evidence"]["readback"]["evidence"]
    for mutation in ({"job_id": str(uuid.uuid4())}, {"input_sha256": "e" * 64},
                     {"completed_stages": ["readback"]}, {"enrollment_id": str(uuid.uuid4())}):
        with pytest.raises(ValueError):
            adapter.validate_operation(jid, ctx, {**op, **mutation})
    op["stage_evidence"]["readback"]["evidence"]["claim"] = "forbidden"
    with pytest.raises(ValueError):
        adapter.validate_operation(jid, ctx, op)


def test_submit_persists_context_before_enqueue_and_fingerprints_only_trusted_argument(monkeypatch):
    ctx = context()
    tool = {"name": ctx["tool_name"], "version": "1", "entry": "validator.py"}
    rows = []
    enqueued = []

    class Store:
        def submit(self, row):
            for old in rows:
                if old["idempotency_key"] == row["idempotency_key"]:
                    if old["submission_fingerprint"] != row["submission_fingerprint"]:
                        raise ValueError("different run input")
                    return old["job_id"], False
            rows.append(deepcopy(row))
            return row["job_id"], True

    class Executor:
        def submit(self, *args, **kwargs):
            assert json.loads(rows[-1]["execution"])["capability_provenance"] == ctx
            enqueued.append(args)

    monkeypatch.setattr(jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(jobs, "ensure_started", lambda: None)
    monkeypatch.setattr(jobs, "_pg_store", Store())
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: Executor()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    args = (ctx["tenant_id"], tool, {}, "", False)
    kwargs = {"org_id": ctx["org_id"], "project_id": ctx["project_id"],
              "idempotency_key": "key", "capability_provenance": ctx}
    first = jobs.submit_job(*args, **kwargs)
    assert jobs.submit_job(*args, **kwargs) == first
    assert len(enqueued) == 1
    with pytest.raises(ValueError, match="different run input"):
        jobs.submit_job(*args, **{**kwargs, "capability_provenance": {**ctx, "catalog_commit": "c" * 40}})
    assert json.loads(rows[0]["execution"])["capability_provenance"] == ctx
    with pytest.raises(ValueError):
        jobs.submit_job(*args, **{**kwargs, "checkout_holder": "forged"})
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "legacy")
    with pytest.raises(ValueError):
        jobs.submit_job(*args, **kwargs)


def test_sqlite_and_postgres_public_context_projection_matches():
    ctx = context()
    columns = {"job_id": str(uuid.uuid4()), "tenant_id": ctx["tenant_id"], "tool": ctx["tool_name"],
               "params_json": "{}", "dwg": "", "status": "submitted", "progress": "queued",
               "created_at": 1, "started_at": None, "updated_at": 1, "finished_at": None,
               "elapsed_ms": None, "result_json": None, "error_json": None, "attempt": 0,
               "lease_owner": None, "lease_expires_at": None, "heartbeat_at": None,
               "provenance_json": None, "org_id": ctx["org_id"], "project_id": ctx["project_id"],
               "authority_mode": "legacy_sqlite", "idempotency_key": "key", "dwg_version": None,
               "execution_json": json.dumps({"capability_provenance": ctx, "private": "hidden"})}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT " + ",".join("? AS " + key for key in columns), list(columns.values())).fetchone()
    assert jobs._row_to_record(row) == _record(columns)
    assert "execution_json" not in _record(columns)
    assert _record(columns)["capability_provenance"] == ctx
    for invalid in (None, {}, {**ctx, "catalog_commit": "invalid"}):
        columns["execution_json"] = json.dumps({"capability_provenance": invalid})
        row = conn.execute("SELECT " + ",".join("? AS " + key for key in columns), list(columns.values())).fetchone()
        with pytest.raises(ValueError, match="invalid capability"):
            jobs._row_to_record(row)
        with pytest.raises(ValueError):
            _record(columns)
    for key, foreign in (("tenant_id", "foreign"), ("org_id", str(uuid.uuid4())),
                         ("project_id", str(uuid.uuid4()))):
        columns["execution_json"] = json.dumps({"capability_provenance": {**ctx, key: foreign}})
        row = conn.execute("SELECT " + ",".join("? AS " + key for key in columns), list(columns.values())).fetchone()
        with pytest.raises(ValueError, match="scope mismatch"):
            jobs._row_to_record(row)
        with pytest.raises(ValueError, match="scope mismatch"):
            _record(columns)
    conn.close()


def test_manifest_and_params_cannot_create_privileged_context(monkeypatch, tmp_path):
    submitted = []

    class Executor:
        def submit(self, *args, **kwargs):
            submitted.append(args)

    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(jobs, "ensure_started", lambda: None)
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: Executor()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    ctx = context()
    tool = {"name": ctx["tool_name"], "capability_provenance": ctx}
    params = {"capability_provenance": ctx}
    try:
        jid = jobs.submit_job("ordinary", tool, params, "demo", False)
        assert jobs.capability_context(jid) is None
        assert "capability_provenance" not in jobs.get_job(jid)
        row = jobs._query("SELECT execution_json,submission_fingerprint FROM jobs WHERE job_id=?", (jid,))[0]
        assert "capability_provenance" not in json.loads(row["execution_json"])
        expected = {"tenantId": "ordinary", "orgId": None, "projectId": None,
                    "tool": tool, "params": params, "dwg": "demo", "apsLive": False,
                    "authorityMode": "legacy_sqlite", "dwgVersion": None}
        assert row["submission_fingerprint"] == hashlib.sha256(json.dumps(
            expected, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        assert len(submitted) == 1
    finally:
        jobs.reset_connection()


def test_terminal_validation_compares_durable_identity_and_store_readback(published):
    ctx, jid, _, op, _, _ = published
    readback = op["stage_evidence"]["readback"]["evidence"]
    result = {"ok": True, "result": {"verified": True, "operation_id": op["operation_id"],
              "input_sha256": op["input_sha256"], "readback_sha256": readback["readback_sha256"]}}
    provenance = {"attempt": 1, "execution_path": "local", "capability_provenance": ctx,
                  "host_readback": readback}
    execution = {"aps_live": False, "capability_provenance": ctx}
    jobs._validate_terminal_context("complete", result, provenance, 1, execution, job_id=jid)
    for changed in ({"capability_provenance": {**ctx, "link_id": str(uuid.uuid4())}},
                    {"host_readback": {**readback, "readback_sha256": "f" * 64}},
                    {"attempt": 2}):
        with pytest.raises(ValueError):
            jobs._validate_terminal_context("complete", result, {**provenance, **changed},
                                             1, execution, job_id=jid)
    op["outcome"] = "held"
    with pytest.raises(ValueError):
        jobs._validate_terminal_context("complete", result, provenance, 1, execution, job_id=jid)


@pytest.mark.parametrize("lost", ["closed", "lease"])
def test_owner_cannot_complete_after_adapter_loses_attempt(monkeypatch, lost):
    ctx = context()
    jid = str(uuid.uuid4())
    rec = {"status": "running", "progress": "host", "attempt": 1,
           "lease": {"owner": None, "expires_at": time.time() + 60}}

    def claim(job, owner):
        rec["lease"]["owner"] = owner
        return 1

    def run(*args):
        if lost == "closed":
            rec["progress"] = jobs.CLOSED_PROGRESS
        else:
            rec["lease"]["owner"] = "replacement"
        return {"ok": True}

    monkeypatch.setattr(jobs, "claim_lease", claim)
    monkeypatch.setattr(jobs, "capability_context", lambda job: dict(ctx))
    monkeypatch.setattr(jobs, "get_job", lambda job: deepcopy(rec))
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda job: None)
    monkeypatch.setattr(adapter, "run", run)
    monkeypatch.setattr(jobs, "complete_callback", lambda *a, **k: pytest.fail("lost owner completed"))
    monkeypatch.setattr(jobs.broker_client, "run_via_broker", lambda *a, **k: pytest.fail("broker called"))
    jobs._run_job(jid, ctx["tenant_id"], {"name": ctx["tool_name"]}, {}, "", False)
