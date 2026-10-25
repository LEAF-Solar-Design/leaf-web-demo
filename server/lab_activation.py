"""Published, read-only schedules sharing the run authority and tenant lock."""
from __future__ import annotations

from datetime import timedelta
import json
import os
import uuid

import deps
import entitlements
import lab_artifacts
import lab_registry
import lab_run_store
import lab_tool_nodes
from dependency_health import _source_revision
from lab_canonical import graph_digest
from lab_doc_validators import TooLarge, identifier

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_activations (
 tenant_id TEXT NOT NULL, activation_id TEXT NOT NULL, doc_id TEXT NOT NULL,
 revision INTEGER NOT NULL, graph_sha256 TEXT NOT NULL, registry_digest TEXT NOT NULL,
 ops_revision TEXT NOT NULL, input_digest TEXT NOT NULL, principal TEXT NOT NULL,
 interval_s INTEGER NOT NULL CHECK (interval_s BETWEEN 60 AND 86400),
 state TEXT NOT NULL CHECK (state IN ('active','paused','expired','stopped','off')),
 reason TEXT, lease_expires_at TEXT NOT NULL, next_due_at TEXT NOT NULL,
 in_flight_run_id TEXT, coalesced_ticks INTEGER NOT NULL DEFAULT 0,
 last_output TEXT, last_summary TEXT, created_by TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id, activation_id)
);
CREATE INDEX IF NOT EXISTS lab_activations_due_idx ON lab_activations (state,next_due_at);
CREATE TABLE IF NOT EXISTS lab_activation_transitions (
 tenant_id TEXT NOT NULL, activation_id TEXT NOT NULL, seq INTEGER NOT NULL,
 from_state TEXT, to_state TEXT NOT NULL, reason TEXT, actor TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY (tenant_id, activation_id, seq),
 FOREIGN KEY (tenant_id, activation_id) REFERENCES lab_activations (tenant_id, activation_id)
);
"""


class Refused(ValueError):
    def __init__(self, code, status=422, block=None):
        super().__init__(code)
        self.code, self.status, self.block = code, status, block


def _now():
    return lab_run_store._now()


def _ops_revision():
    from lab_worker import OPS_REVISION
    return OPS_REVISION


def _run(tenant, operation, *, write=False):
    def transaction(sql):
        if not sql.postgres:
            for statement in _SQLITE_SCHEMA.split(";"):
                if statement.strip():
                    sql.execute(statement)
        return operation(sql)
    return lab_run_store._run(tenant, transaction, write=write)


def _row(sql, tenant, activation_id):
    row = sql.execute("SELECT * FROM lab_activations WHERE tenant_id=? AND activation_id=?",
                      (tenant, activation_id)).fetchone()
    if row is None:
        raise lab_artifacts.NotFound("lab_activation_not_found")
    return dict(row)


def _transition(sql, row, state, reason, actor, now):
    tenant, aid = row["tenant_id"], row["activation_id"]
    seq = sql.execute("SELECT MAX(seq) AS seq FROM lab_activation_transitions"
                      " WHERE tenant_id=? AND activation_id=?", (tenant, aid)).fetchone()["seq"]
    seq = 1 if seq is None else seq + 1
    sql.execute("INSERT INTO lab_activation_transitions"
                " (tenant_id,activation_id,seq,from_state,to_state,reason,actor,at) VALUES (?,?,?,?,?,?,?,?)",
                (tenant, aid, seq, row.get("state"), state, reason, actor, now.isoformat()))
    sql.execute("DELETE FROM lab_activation_transitions WHERE tenant_id=? AND activation_id=? AND seq<=?",
                (tenant, aid, seq - 200))
    sql.execute("UPDATE lab_activations SET state=?,reason=?,updated_at=? WHERE tenant_id=? AND activation_id=?",
                (state, reason, now.isoformat(), tenant, aid))
    row.update(state=state, reason=reason)


def _admit_graph(graph):
    for block in graph["blocks"]:
        ref = block["registry"]
        trusted = lab_registry.lookup(ref["id"], ref["version"])
        if (trusted is None or trusted.residency == "host"
                or trusted.effect not in ("pure", "read", "simulate")
                or lab_registry.total_effect(trusted.capability, trusted.solar_adapter,
                                             trusted.execution_class, trusted.aps_live)
                not in ("pure", "read", "simulate")):
            raise Refused("lab_activation_effect_refused", block=block["id"])
        if trusted.effect in ("read", "simulate") and not lab_tool_nodes.enabled():
            raise Refused("lab_activation_tools_disabled", block=block["id"])


def readmit(row):
    principal = row["principal"]
    if deps.auth_live() and principal and not principal.startswith("tenant:"):
        try:
            tenant, tier = deps.resolve_active_platform_tenant_authority(principal)
        except Exception:
            return "binding_unavailable"
        if tenant != row["tenant_id"]:
            return "binding_changed"
        try:
            if not entitlements.entitlements_for(tier).get("deploy"):
                return "entitlement_revoked"
        except entitlements.EntitlementsError:
            return "policy_unavailable"
    try:
        head = lab_artifacts.get(row["tenant_id"], row["doc_id"])
        if head["published_revision"] != row["revision"]:
            return "publication_changed"
        artifact = lab_artifacts.get(row["tenant_id"], row["doc_id"], row["revision"])
        if artifact["sha256"] != row["graph_sha256"]:
            return "publication_changed"
    except lab_artifacts.NotFound:
        return "publication_changed"
    if lab_run_store.registry_digest() != row["registry_digest"]:
        return "registry_changed"
    if _ops_revision() != row["ops_revision"]:
        return "ops_changed"
    if graph_digest({}) != row["input_digest"]:
        return "input_changed"
    try:
        _admit_graph(artifact["graph"])
    except Refused as error:
        return error.code
    return None


def _public(sql, row):
    result = {key: value for key, value in row.items() if key not in {"principal", "created_by"}}
    result["principal_bound"] = bool(row["principal"] and not row["principal"].startswith("tenant:"))
    for key in ("last_output", "last_summary"):
        if isinstance(result[key], str):
            result[key] = json.loads(result[key])
    transitions = sql.execute("SELECT seq,from_state,to_state,reason,at FROM lab_activation_transitions"
                              " WHERE tenant_id=? AND activation_id=? ORDER BY seq DESC LIMIT 20",
                              (row["tenant_id"], row["activation_id"])).fetchall()
    result["transitions"] = [dict(item) for item in transitions]
    return result


def create(tenant, doc_id, interval_s, principal, actor):
    identifier(doc_id)
    if type(interval_s) is not int or not 60 <= interval_s <= 86400:
        raise Refused("invalid_lab_activation_interval")
    if not isinstance(principal, str) or not principal or len(principal) > 512:
        raise Refused("invalid_lab_activation_principal")

    def operation(sql):
        head = lab_artifacts.get(tenant, doc_id)
        revision = head["published_revision"]
        if revision is None:
            raise Refused("lab_activation_unpublished", 409)
        artifact = lab_artifacts.get(tenant, doc_id, revision)
        _admit_graph(artifact["graph"])
        count = sql.execute("SELECT COUNT(*) AS count FROM lab_activations WHERE tenant_id=?"
                            " AND state IN ('active','paused','expired')", (tenant,)).fetchone()["count"]
        if count >= 10:
            raise Refused("lab_activation_limit", 409)
        now, aid = _now(), uuid.uuid4().hex
        sql.execute("INSERT INTO lab_activations (tenant_id,activation_id,doc_id,revision,graph_sha256,"
                    "registry_digest,ops_revision,input_digest,principal,interval_s,state,lease_expires_at,"
                    "next_due_at,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant, aid, doc_id, revision, artifact["sha256"], lab_run_store.registry_digest(),
                     _ops_revision(), graph_digest({}), principal, interval_s, "active",
                     (now + timedelta(hours=24)).isoformat(), now.isoformat(), actor,
                     now.isoformat(), now.isoformat()))
        row = _row(sql, tenant, aid)
        reason = readmit(row)
        if reason:
            raise Refused(reason, 409)
        row["state"] = None
        _transition(sql, row, "active", "created", actor, now)
        return _public(sql, _row(sql, tenant, aid))
    return _run(tenant, operation, write=True)


def list(tenant):
    return _run(tenant, lambda sql: [_public(sql, dict(row)) for row in sql.execute(
        "SELECT * FROM lab_activations WHERE tenant_id=? ORDER BY created_at DESC,activation_id",
        (tenant,)).fetchall()])


def _control(tenant, activation_id, action, actor):
    identifier(activation_id)
    def operation(sql):
        row, now = _row(sql, tenant, activation_id), _now()
        allowed = {"pause": {"active"}, "resume": {"active", "paused"},
                   "renew": {"active", "paused", "expired"},
                   "off": {"active", "paused", "expired", "stopped"}}
        if row["state"] not in allowed[action]:
            raise Refused("lab_activation_state_conflict", 409)
        if action == "resume" and lab_run_store._date(row["lease_expires_at"]) <= now:
            _transition(sql, row, "expired", "lease_expired", actor, now)
            return _public(sql, _row(sql, tenant, activation_id))
        if action in {"resume", "renew"}:
            reason = readmit(row)
            if reason:
                _transition(sql, row, "stopped", reason, actor, now)
                return _public(sql, _row(sql, tenant, activation_id))
            if action == "renew":
                sql.execute("UPDATE lab_activations SET lease_expires_at=? WHERE tenant_id=? AND activation_id=?",
                            ((now + timedelta(hours=24)).isoformat(), tenant, activation_id))
            sql.execute("UPDATE lab_activations SET next_due_at=? WHERE tenant_id=? AND activation_id=?",
                        (now.isoformat(), tenant, activation_id))
        state = {"pause": "paused", "resume": "active", "renew": "active", "off": "off"}[action]
        _transition(sql, row, state, action, actor, now)
        return _public(sql, _row(sql, tenant, activation_id))
    result = _run(tenant, operation, write=True)
    if action == "resume" and result["state"] == "expired":
        raise Refused("lab_activation_state_conflict", 409)
    return result


def pause(tenant, activation_id, actor):
    return _control(tenant, activation_id, "pause", actor)


def resume(tenant, activation_id, actor):
    return _control(tenant, activation_id, "resume", actor)


def renew(tenant, activation_id, actor):
    return _control(tenant, activation_id, "renew", actor)


def off(tenant, activation_id, actor):
    return _control(tenant, activation_id, "off", actor)


def _harvest(sql, row, run):
    graph = lab_artifacts.get(row["tenant_id"], row["doc_id"], row["revision"])["graph"]
    values = {}
    events = sql.execute("SELECT block_id,payload FROM lab_run_events WHERE tenant_id=? AND run_id=?"
                         " AND kind='value' ORDER BY seq", (row["tenant_id"], run["run_id"])).fetchall()
    for event in events:
        payload = event["payload"]
        values[event["block_id"]] = json.loads(payload) if isinstance(payload, str) else payload
    output = {name: values.get(ref["node"], {}).get(ref["port"])
              for name, ref in graph.get("outputs", {}).items()}
    encoded = json.dumps(output, allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        encoded = json.dumps({"truncated": True})
    finished = run["finished_at"]
    summary = json.dumps({"run_id": run["run_id"], "state": run["state"],
                          "finished_at": finished.isoformat() if hasattr(finished, "isoformat") else finished,
                          "duration_s": max(0, (lab_run_store._date(finished) -
                                              lab_run_store._date(run["created_at"])).total_seconds())
                          if finished else None, "coalesced_ticks": row["coalesced_ticks"]})
    placeholder = "?::jsonb" if sql.postgres else "?"
    sql.execute("UPDATE lab_activations SET last_output=" + placeholder + ",last_summary=" + placeholder +
                ",in_flight_run_id=NULL,coalesced_ticks=0 WHERE tenant_id=? AND activation_id=?",
                (encoded, summary, row["tenant_id"], row["activation_id"]))


def _enqueue(sql, row, now):
    # Use the same transaction as the activation update: nesting store.create
    # would deadlock its tenant lock and could orphan a queued run after a crash.
    count = sql.execute("SELECT COUNT(*) AS count FROM lab_runs WHERE tenant_id=? AND state='queued'",
                        (row["tenant_id"],)).fetchone()["count"]
    if count >= lab_run_store.MAX_QUEUED_PER_TENANT:
        raise TooLarge("lab_run_queue_full")
    run_id = uuid.uuid4().hex
    sql.execute("INSERT INTO lab_runs (tenant_id,doc_id,run_id,graph_revision,graph_sha256,registry_digest,"
                "ops_revision,input_digest,adapter_revision,build_revision,state,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["tenant_id"], row["doc_id"], run_id, row["revision"], row["graph_sha256"],
                 row["registry_digest"], row["ops_revision"], row["input_digest"], "lab-activation.v1",
                 _source_revision(os.environ) or "unknown", "queued", now.isoformat(), now.isoformat()))
    return run_id


def tick(now, *, limit=20):
    if lab_run_store.paused():
        return
    now = lab_run_store._date(now)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid_lab_activation_limit")
    due = _run("lab-activation-scheduler", lambda sql: [dict(row) for row in sql.execute(
        "SELECT tenant_id,activation_id FROM lab_activations WHERE state='active' AND next_due_at<=?"
        " ORDER BY next_due_at,tenant_id,activation_id LIMIT ?", (now.isoformat(), limit)).fetchall()])
    for candidate in due:
        tenant, aid = candidate["tenant_id"], candidate["activation_id"]
        def operation(sql):
            row = _row(sql, tenant, aid)
            if row["state"] != "active" or lab_run_store._date(row["next_due_at"]) > now:
                return
            if lab_run_store._date(row["lease_expires_at"]) <= now:
                _transition(sql, row, "expired", "lease_expired", "scheduler", now)
                return
            if row["in_flight_run_id"]:
                run = lab_run_store._row(sql, tenant, row["in_flight_run_id"])
                if run is None:
                    _transition(sql, row, "stopped", "run_unavailable", "scheduler", now)
                    return
                if run["state"] not in {"finished", "failed", "cancelled"}:
                    reason = readmit(row)
                    if reason:
                        _transition(sql, row, "stopped", reason, "scheduler", now)
                        return
                    sql.execute("UPDATE lab_activations SET coalesced_ticks=coalesced_ticks+1,next_due_at=?,"
                                "updated_at=? WHERE tenant_id=? AND activation_id=?",
                                ((lab_run_store._date(row["next_due_at"]) + timedelta(seconds=row["interval_s"])).isoformat(),
                                 now.isoformat(), tenant, aid))
                    return
                _harvest(sql, row, run)
            reason = readmit(row)
            if reason:
                _transition(sql, row, "stopped", reason, "scheduler", now)
                return
            try:
                run_id = _enqueue(sql, row, now)
            except TooLarge:
                sql.execute("UPDATE lab_activations SET coalesced_ticks=coalesced_ticks+1,next_due_at=?,"
                            "updated_at=? WHERE tenant_id=? AND activation_id=?",
                            ((now + timedelta(seconds=row["interval_s"])).isoformat(), now.isoformat(), tenant, aid))
                return
            sql.execute("UPDATE lab_activations SET in_flight_run_id=?,next_due_at=?,updated_at=?"
                        " WHERE tenant_id=? AND activation_id=?",
                        (run_id, (now + timedelta(seconds=row["interval_s"])).isoformat(), now.isoformat(), tenant, aid))
        _run(tenant, operation, write=True)
