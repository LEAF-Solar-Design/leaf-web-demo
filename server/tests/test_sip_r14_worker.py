"""R14-C worker seam proofs using real exchange APIs over an in-memory SQL boundary.

The double does not establish PostgreSQL transaction isolation or rollback guarantees.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import platform_link
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_project_sizing as sizing
import solar_sizing_client as cloud
from leaf_cloud_grants import CloudError, CloudGrant
from leaf_platform import canonical_jobs as jobs, project_sizing_store as store
import test_sip_r3a_graph as a3
import test_sip_r3b_tools as b3
import test_sip_r14_admission as admission
from test_sip_r14_evidence import SMALL


class Cursor:
    def __init__(self, world):
        self.w = world
        self.value = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def fetchone(self):
        return deepcopy(self.value)

    def execute(self, sql, args=None):
        w = self.w
        self.rowcount = 1
        self.value = None
        if sql.startswith("SELECT * FROM jobs"):
            self.value = w.rows.get(str(args[0]))
            return
        if sql == store.LOCK_JOB_SQL:
            row = w.rows.get(str(args["job_id"]))
            if row and row["attempt"] == args["attempt"] and (
                row.get("lease_owner") == args["worker"] or
                (row.get("provenance") or {}).get("worker_id") == args["worker"]):
                self.value = row
            return
        if sql in (store.READ_SQL, store.LOCK_EXCHANGE_SQL):
            e = w.exchanges.get(str(args["job_id"]))
            if e and all(str(e[k]) == str(args[k]) for k in (
                "org_id", "project_id", "drawing_id", "job_id", "actor_binding_id",
                "target_settings_id", "admitted_tenant")):
                self.value = e
            return
        if sql == store.PREPARE_SQL:
            key = str(args["job_id"])
            if key not in w.exchanges:
                e = {k: deepcopy(args[k]) for k in ("job_id", "org_id", "project_id", "drawing_id",
                    "actor_binding_id", "target_settings_id", "admitted_tenant", "request_bytes",
                    "request_sha256", "grant_ref", "classification")}
                e.update(state="prepared", sender_worker=None, sender_attempt=None,
                    response_bytes=None, response_sha256=None, evidence_bytes=None,
                    evidence_sha256=None, output_version_id=None, published_worker=None,
                    published_attempt=None, refusal_code=None)
                w.exchanges[key] = e
                self.value = e
            return
        if sql in (store.BEGIN_SEND_SQL, store.CAPTURE_SQL, store.ATTACH_EVIDENCE_SQL,
                   store.REFUSE_SQL, store.UNKNOWN_SQL, store.PUBLISH_SQL):
            e = w.exchanges[str(args["job_id"])]
            if sql == store.BEGIN_SEND_SQL:
                if e["state"] != "prepared" or w.send_loser:
                    self.rowcount = 0
                    return
                e.update(state="send_started", sender_worker=args["worker"], sender_attempt=args["attempt"])
                w.events.append("send_intent")
            elif sql == store.CAPTURE_SQL:
                e.update(state="response_stored", response_bytes=args["response_bytes"], response_sha256=args["response_sha256"])
                w.events.append("capture")
            elif sql == store.ATTACH_EVIDENCE_SQL:
                e.update(evidence_bytes=args["evidence_bytes"], evidence_sha256=args["evidence_sha256"])
                w.events.append("evidence")
            elif sql == store.REFUSE_SQL:
                e.update(state="refused", refusal_code=args["reason_code"])
            elif sql == store.UNKNOWN_SQL:
                e.update(state="outcome_unknown", refusal_code="SIZING_OUTCOME_UNKNOWN")
            else:
                e.update(state="published", output_version_id=args["output_version_id"],
                         published_worker=args["worker"], published_attempt=args["attempt"])
                w.events.append("published")
            self.value = e
            return
        if sql == store.OUTPUT_SQL:
            self.value = {"version_id": args["output_version_id"]} if args["output_version_id"] in w.s.versions else None
            return
        if sql.startswith("SELECT import_fingerprint"):
            w.s.execute(sql, args)
            self.value = w.s.fetchone()
            return
        if sql.startswith("INSERT INTO history_operations"):
            w.history.append(deepcopy(args))
            return
        if sql.startswith("UPDATE jobs SET status='succeeded'"):
            row = w.rows[str(args["job"])]
            row.update(status="succeeded", result=args["result"].obj, output_version_id=args["output"],
                provenance=args["provenance"].obj, terminal_fingerprint=args["fingerprint"],
                lease_owner=None, lease_expires_at=None)
            return
        raise AssertionError(sql)


class World:
    def __init__(self, monkeypatch, *, memory=None, parent_graph=None):
        self.patch = monkeypatch
        monkeypatch.setattr(a3, "uuid4", uuid4)
        self.s = memory or a3.memory.__wrapped__(monkeypatch)
        s = self.s
        self.events, self.rows, self.exchanges, self.history = [], {}, {}, []
        self.depth = 0
        self.send_loser = False
        self.raw = graph.local.canonical_bytes(SMALL)
        s.set_parent(b3.embedded(parent_graph or admission.source()))
        self.context = graph.resolve_project_graph_context(s.context())
        params = admission.parameters(self.context.graph)
        next(iter(params["requests"].values()))["zip_code"] = self.context.graph["project"]["zip_code"][:5]
        self.params = sizing.plan_sizing_request(params,
            self.context.graph, self.context.intake_sha256, b3.manifest(graph.SIZING_TOOL))["normalized_params"]
        self.owner = "w-" + uuid4().hex
        self.job_id = uuid4()
        self.row = {"job_id": str(self.job_id), "org_id": str(s.org), "project_id": str(s.project),
            "input_version_id": str(s.parent.version_id), "request_tenant_id": str(s.org),
            "kind": "run", "tool_name": graph.SIZING_TOOL, "params": self.params,
            "status": "running", "attempt": 1, "max_attempts": 3, "lease_owner": self.owner,
            "lease_expires_at": a3.NOW + timedelta(hours=1), "deleted_at": None,
            "result": None, "output_version_id": None,
            "execution_context": {"schema": jobs.PROJECT_GRAPH_JOB_SCHEMA,
                "authority_mode": "postgres_canonical", "execution_path": "local",
                "drawing_id": str(s.drawing), "actor_binding_id": str(s.actor),
                "checkout_fence": str(s.lease.fence), "tool_manifest_sha256": b3.manifest(graph.SIZING_TOOL),
                "parent_intake_sha256": self.context.intake_sha256, "initialized": False}}
        self.rows[str(self.job_id)] = self.row
        monkeypatch.setattr(platform_link.platform_db(), "run_transaction", self.transaction)
        monkeypatch.setattr(store.db, "run_transaction", self.transaction)
        @contextmanager
        def connection():
            yield self
        monkeypatch.setattr(store.db, "connection", connection)
        monkeypatch.setattr(jobs, "_graph_scope", lambda row, conn: None)
        monkeypatch.setattr(project.graph_store(), "_clock", lambda cur: a3.NOW)
        monkeypatch.setattr(jobs, "_insert_outbox", lambda *args: self.events.append("outbox"))
        monkeypatch.setattr(jobs, "claim_project_graph_job", lambda *args, **kw: deepcopy(self.row))
        monkeypatch.setattr(jobs, "fail_project_graph_job", self.fail)
        class Guard:
            lost = False
            def __init__(guard, *args):
                self.guard = guard
            def start(guard):
                self.events.append("guard_start")
            def close(guard):
                self.events.append("guard_close")
        monkeypatch.setattr(service, "GraphLeaseGuard", Guard)
        monkeypatch.setattr(cloud, "resolve_grant", self.grant)
        monkeypatch.setattr(cloud.requests, "post", self.post)

    def cursor(self):
        return Cursor(self)

    def transaction(self, operation):
        self.depth += 1
        try:
            result = operation(self)
        finally:
            self.depth -= 1
        self.events.append("commit")
        return result

    def grant(self, ref, tenant):
        assert tenant == str(self.s.org) and ref == "g1"
        self.events.append("grant")
        return CloudGrant(tenant, "t1")

    def post(self, *args, **kwargs):
        self.events.append("post_attempt")
        assert self.depth == 0, "Transport entered a retryable transaction"
        assert self.exchange["state"] == "send_started"
        assert "commit" in self.events[self.events.index("send_intent") + 1:]
        self.events.append("post")
        w = self
        class Response:
            status_code = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def iter_content(self, size):
                yield w.raw
        return Response()

    def fail(self, job_id, owner, attempt, error):
        self.events.append("failed")
        self.row.update(status="failed", error=error, provenance={"worker_id": owner})
        return True

    @property
    def exchange(self):
        return self.exchanges[str(self.job_id)]

    @property
    def authority(self):
        return graph._exchange_authority(self.row)

    def reserve(self, classification="live"):
        plan = sizing.plan_sizing_request(self.params, self.context.graph, self.context.intake_sha256,
                                         self.row["execution_context"]["tool_manifest_sha256"])
        return store.prepare_exchange(*self.authority, request_bytes=plan["wire_bytes"],
            request_sha256=plan["wire_sha256"], grant_ref="g1", classification=classification)

    def capture(self):
        self.reserve()
        assert store.begin_send(*self.authority)
        store.capture_response(*self.authority, response_bytes=self.raw, response_sha256=graph._sha(self.raw))

    def reclaim(self):
        self.owner = "w-" + uuid4().hex
        self.row.update(status="running", lease_owner=self.owner, attempt=self.row["attempt"] + 1)

    def run(self):
        assert service.run_once(self.owner, tool_name=graph.SIZING_TOOL)

    def completed(self):
        assert self.row["status"] == "succeeded", self.row.get("error", self.events)
        assert self.exchange["state"] == "published"
        return self.row["result"]["graph_commit"]


def test_R14C_09_send_order(monkeypatch):
    """R14C-09 Transport follows committed ownership outside callbacks; capture precedes evidence."""
    w = World(monkeypatch)
    w.run()
    w.completed()
    assert w.events.count("post") == 1
    positions = [w.events.index(x) for x in ("send_intent", "post", "capture", "evidence", "published")]
    assert positions == sorted(positions)


def test_R14C_10_send_loser(monkeypatch):
    """R14C-10 Losing the atomic send competition produces zero POSTs and no output."""
    w = World(monkeypatch)
    w.send_loser = True
    w.run()
    assert "post_attempt" not in w.events and w.exchange["state"] == "prepared"
    assert not w.history and w.row["status"] == "running"


def test_R14C_11_same_attempt_observation(monkeypatch):
    """R14C-11 Same-attempt send observation waits without a second POST."""
    w = World(monkeypatch)
    w.reserve()
    assert store.begin_send(*w.authority)
    # A wrong dispatcher branch must be observable even if begin_send still fences a resend.
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    w.run()
    assert w.row["status"] == "running" and "post" not in w.events
    assert w.exchange["state"] == "send_started" and not w.history


def test_R14C_12_reclaimed_lost_response(monkeypatch):
    """R14C-12 Reclaimed send intent durably settles nonretryable unknown, never output."""
    w = World(monkeypatch)
    w.reserve()
    assert store.begin_send(*w.authority)
    w.reclaim()
    w.run()
    assert w.exchange["state"] == "outcome_unknown"
    assert w.row["error"]["reason_code"] == "SIZING_OUTCOME_UNKNOWN"
    assert w.row["error"]["retryable"] is False and "post" not in w.events and not w.history


def test_R14C_13_send_time_grant(monkeypatch):
    """R14C-13 Revocation after reservation refuses before send intent or POST."""
    w = World(monkeypatch)
    w.reserve()
    def revoked(*args):
        raise CloudError("cloud_auth_missing", 401)
    monkeypatch.setattr(cloud, "resolve_grant", revoked)
    w.run()
    assert w.exchange["state"] == "refused" and w.exchange["refusal_code"] == "cloud_auth_missing"
    assert w.row["error"]["reason_code"] == "CLOUD_AUTH_MISSING"
    assert "post" not in w.events and "send_intent" not in w.events


def test_R14C_14_known_http_refusals(monkeypatch):
    """R14C-14 HTTP 401, 403 and other errors retain durable known refusal classifications."""
    for status, code in ((401, "cloud_auth_missing"), (403, "cloud_tenant_unauthorized"),
                         (400, "cloud_upstream_failure"), (503, "cloud_upstream_failure")):
        with monkeypatch.context() as patch:
            w = World(patch)
            post = w.post
            def response(*args, **kwargs):
                result = post(*args, **kwargs)
                result.status_code = status
                return result
            patch.setattr(cloud.requests, "post", response)
            w.run()
            assert w.exchange["state"] == "refused" and w.exchange["refusal_code"] == code
            assert w.row["error"]["reason_code"] == code.upper() and not w.history


def test_R14C_15_transport_ambiguity(monkeypatch):
    """R14C-15 Canonical delivery uncertainty stays distinct from legacy transport classification."""
    w = World(monkeypatch)
    def timeout(*args, **kwargs):
        raise cloud.requests.Timeout("private t1")
    monkeypatch.setattr(cloud.requests, "post", timeout)
    w.run()
    assert w.exchange["state"] == "outcome_unknown"
    assert w.row["error"]["reason_code"] == "SIZING_OUTCOME_UNKNOWN"
    assert w.row["error"]["retryable"] is False
    with pytest.raises(CloudError) as caught:
        cloud.post_string_length(cloud.SizingRequest.model_validate(next(iter(w.params["requests"].values()))), CloudGrant(str(w.s.org), "t1"))
    assert caught.value.classification == "cloud_upstream_failure"


def test_R14C_16_invalid_captured_response(monkeypatch):
    """R14C-16 Malformed, voltage-conflicting and power-invalid bytes are captured before refusal."""
    voltage = deepcopy(SMALL)
    voltage["simulation_results"]["standard"]["string_design_voltage"] = 601
    cold = deepcopy(SMALL)
    cold["voc"] = 100
    power = deepcopy(SMALL)
    power["pmp"] = 0
    for raw in (b"{bad", *[graph.local.canonical_bytes(x) for x in (voltage, cold, power)]):
        with monkeypatch.context() as patch:
            w = World(patch)
            w.raw = raw
            w.run()
            assert w.exchange["response_bytes"] == raw
            assert w.exchange["state"] == "refused"
            assert w.exchange["refusal_code"] == "cloud_response_invalid"
            assert w.row["error"]["reason_code"] == "CLOUD_RESPONSE_INVALID" and not w.history


def test_R14C_17_uncertain_capture(monkeypatch):
    """R14C-17 Capture uncertainty waits for durable readback; neither outcome resends."""
    for stored in (False, True):
        with monkeypatch.context() as patch:
            w = World(patch)
            capture = store.capture_response
            def uncertain(*args, **kwargs):
                if stored:
                    capture(*args, **kwargs)
                raise ConnectionError("uncertain")
            patch.setattr(store, "capture_response", uncertain)
            w.run()
            assert w.row["status"] == "running" and w.events.count("post") == 1
            patch.setattr(store, "capture_response", capture)
            w.reclaim()
            w.run()
            assert w.events.count("post") == 1
            assert w.row["status"] == ("succeeded" if stored else "failed")
            assert w.exchange["state"] == ("published" if stored else "outcome_unknown")


def test_R14C_18_reclaim_with_response(monkeypatch):
    """R14C-18 A new owner consumes stored bytes while retaining the original producer."""
    w = World(monkeypatch)
    w.capture()
    sender = w.owner
    w.reclaim()
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    monkeypatch.setattr(cloud.requests, "post", admission.forbidden)
    w.run()
    w.completed()
    producer = json.loads(w.exchange["evidence_bytes"])["producer"]
    assert producer["worker_id"] == sender and producer["attempt"] == 1
    assert w.exchange["published_worker"] == w.owner and w.exchange["published_attempt"] == 2


def test_R14C_19_publication_retry(monkeypatch):
    """R14C-19 Publication failure reuses response and evidence, publishing one output/history."""
    w = World(monkeypatch)
    complete = service.complete_project_graph_job
    def unavailable(*args, **kwargs):
        raise ConnectionError("commit unavailable")
    monkeypatch.setattr(service, "complete_project_graph_job", unavailable)
    w.run()
    assert w.exchange["state"] == "response_stored" and w.row["status"] == "running"
    before = w.exchange["response_bytes"], w.exchange["evidence_bytes"]
    monkeypatch.setattr(service, "complete_project_graph_job", complete)
    w.reclaim()
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    w.run()
    w.completed()
    assert (w.exchange["response_bytes"], w.exchange["evidence_bytes"]) == before
    # A settled exchange is observed without reopening reservation or publishing again.
    w.run()
    w.completed()
    assert w.events.count("post") == 1 and len(w.history) == 1 and len(w.s.versions) == 2


def test_R14C_20_lost_lease(monkeypatch):
    """R14C-20 Lease loss during transport or before completion prevents stale publication."""
    for when in ("transport", "close"):
        with monkeypatch.context() as patch:
            w = World(patch)
            if when == "transport":
                original = w.post
                def late(*args, **kwargs):
                    response = original(*args, **kwargs)
                    w.guard.lost = True
                    return response
                patch.setattr(cloud.requests, "post", late)
            else:
                guard = service.GraphLeaseGuard
                patch.setattr(guard, "close", lambda self: setattr(self, "lost", True))
                patch.setattr(service, "complete_project_graph_job",
                              lambda *args: w.events.append("stale_completion"))
            w.run()
            assert "stale_completion" not in w.events
            assert not w.history and len(w.s.versions) == 1 and w.row["status"] == "running"
            if when == "transport":
                assert w.exchange["response_bytes"] is None


def test_R14C_33_empty_success_response(monkeypatch):
    """R14C-33 An empty HTTP 200 settles as a known invalid response on its first attempt; reclaim never resends."""
    w = World(monkeypatch)
    w.raw = b""
    w.run()
    assert w.events.count("post") == 1
    assert w.exchange["state"] == "refused"
    assert w.exchange["refusal_code"] == "cloud_response_invalid"
    assert w.exchange["response_bytes"] is None
    assert w.row["status"] == "failed"
    assert w.row["error"]["reason_code"] == "CLOUD_RESPONSE_INVALID"
    assert w.row["error"]["retryable"] is False and not w.history
    w.reclaim()
    monkeypatch.setattr(cloud, "resolve_grant", admission.forbidden)
    monkeypatch.setattr(cloud.requests, "post", admission.forbidden)
    w.run()
    assert w.events.count("post") == 1
    assert w.exchange["state"] == "refused"
    assert w.row["error"]["reason_code"] == "CLOUD_RESPONSE_INVALID"
    assert not w.history and len(w.s.versions) == 1


class _B(bytes):
    pass


@pytest.mark.parametrize("response_type", (bytearray, memoryview, _B))
def test_R14C_37_response_must_be_exact_bytes(monkeypatch, response_type):
    """R14C-37 Bytes-like responses refuse before capture or publication."""
    w = World(monkeypatch)
    before = w.s.head
    raw = response_type(w.raw)
    monkeypatch.setattr(cloud, "post_string_length", lambda *args, **kwargs: raw)
    errors = []
    preparation_error = service._preparation_error
    def record_error(exc):
        assert isinstance(exc, CloudError)
        assert exc.classification == "cloud_response_invalid" and exc.status == 502
        errors.append(exc)
        return preparation_error(exc)
    monkeypatch.setattr(service, "_preparation_error", record_error)
    w.run()
    assert len(errors) == 1
    assert w.row["status"] == "failed"
    assert w.row["error"]["reason_code"] == "CLOUD_RESPONSE_INVALID"
    assert w.row["error"]["retryable"] is False
    assert w.exchange["state"] == "refused"
    assert w.exchange["refusal_code"] == "cloud_response_invalid"
    assert w.exchange["response_bytes"] is None
    assert not w.history and len(w.s.versions) == 1
    assert w.s.head == before


EXCHANGE_ERRORS = ((store.ExchangeInputError, "SIP_R14_EXCHANGE_INPUT_INVALID"),
                   (store.ExchangeNotFound, "SIP_R14_EXCHANGE_NOT_FOUND"),
                   (store.ExchangeConflict, "SIP_R14_EXCHANGE_CONFLICT"),
                   (store.ExchangeLeaseLost, "SIP_R14_EXCHANGE_NOT_OWNER"),
                   (store.ExchangeStateError, "SIP_R14_EXCHANGE_STATE_INVALID"))


def test_R14C_34_exchange_refusals_sanitized(monkeypatch):
    """R14C-34 Every exchange custody refusal reaches the job as one non-retryable evidence rejection."""
    for error_type, raw_code in EXCHANGE_ERRORS:
        assert error_type().reason_code == raw_code
        assert service._preparation_error(error_type()) == {
            "error_code": "GRAPH_JOB_FAILED", "message": "canonical Solar graph preparation failed",
            "retryable": False, "reason_code": "SIP_R14_EVIDENCE_REJECTED"}
        with monkeypatch.context() as patch:
            w = World(patch)
            def refuse(*args, **kwargs):
                raise error_type()
            patch.setattr(store, "resume_exchange", refuse)
            w.run()
            assert w.row["status"] == "failed" and "post_attempt" not in w.events
            assert w.row["error"]["reason_code"] == "SIP_R14_EVIDENCE_REJECTED"
            assert w.row["error"]["retryable"] is False
            assert raw_code not in json.dumps(w.row["error"]) and not w.history
