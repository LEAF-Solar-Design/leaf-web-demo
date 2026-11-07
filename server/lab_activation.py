"""Published, read-only schedules sharing the run authority and tenant lock."""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import os
import uuid

import deps
import entitlements
import lab_artifacts
import lab_registry
import lab_run_store
import lab_tool_manifests
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
 failure_count INTEGER NOT NULL DEFAULT 0, last_started_at TEXT,
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
            columns = {row["name"] for row in sql.execute("PRAGMA table_info(lab_activations)").fetchall()}
            for name, definition in (("failure_count", "INTEGER NOT NULL DEFAULT 0"), ("last_started_at", "TEXT")):
                if name not in columns:
                    sql.execute("ALTER TABLE lab_activations ADD COLUMN " + name + " " + definition)
        return operation(sql)
    return lab_run_store._run(tenant, transaction, write=write)


def _row(sql, tenant, activation_id):
    row = sql.execute("SELECT * FROM lab_activations WHERE tenant_id=? AND activation_id=?",
                      (tenant, activation_id)).fetchone()
    if row is None:
        raise lab_artifacts.NotFound("lab_activation_not_found")
    return dict(row)


def _transition(sql, row, state, reason, actor, now, cancellations=None):
    tenant, aid = row["tenant_id"], row["activation_id"]
    if row.get("state") == "active" and state != "active" and row.get("in_flight_run_id"):
        cancellations.append(row["in_flight_run_id"])
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


def _cancel_committed(tenant, cancellations):
    # The public API takes its own tenant lock. Only call it after commit;
    # holds_run prevents evaluation if the process exits before cancellation.
    for run_id in cancellations:
        try:
            lab_run_store.request_cancel(tenant, run_id, "activation")
        except Exception as error:
            logging.getLogger(__name__).warning("lab_activation_cancel_failed class=%s", type(error).__name__)


def _admit_graph(graph, tenant):
    for block in graph["blocks"]:
        ref = block["registry"]
        tool = lab_tool_manifests.trusted_for(str(tenant), ref["id"], ref["version"])
        trusted = lab_registry.lookup(ref["id"], ref["version"])
        # trusted_for returns the static entry itself when no tenant tool projects.
        if tool is not None and tool is not trusted:
            if not lab_tool_nodes.enabled():
                raise Refused("lab_activation_tools_disabled", block=block["id"])
            if not lab_tool_nodes.tool_block_admissible(tool, ("compute", "read", "simulate")):
                raise Refused("lab_activation_effect_refused", block=block["id"])
            continue
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
    if not principal.startswith("tenant:"):
        if not deps.auth_live():
            return "auth_mode_changed"
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
        _admit_graph(artifact["graph"], row["tenant_id"])
    except Refused as error:
        return error.code
    return None


def _public(sql, row, transitions=None):
    result = {key: value for key, value in row.items() if key not in {"principal", "created_by"}}
    result["principal_bound"] = bool(row["principal"] and not row["principal"].startswith("tenant:"))
    for key in ("last_output", "last_summary"):
        if isinstance(result[key], str):
            result[key] = json.loads(result[key])
    if transitions is None:
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
        _admit_graph(artifact["graph"], tenant)
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
            if reason == "entitlement_revoked":
                raise Refused("lab_activation_entitlement_required", 403)
            raise Refused(reason, 409)
        row["state"] = None
        _transition(sql, row, "active", "created", actor, now)
        return _public(sql, _row(sql, tenant, aid))
    return _run(tenant, operation, write=True)


def list(tenant):
    def operation(sql):
        rows = [dict(row) for row in sql.execute(
            "SELECT * FROM lab_activations WHERE tenant_id=? AND (state IN ('active','paused','expired')"
            " OR activation_id IN (SELECT activation_id FROM lab_activations WHERE tenant_id=?"
            " AND state IN ('stopped','off') ORDER BY created_at DESC,activation_id LIMIT 20))"
            " ORDER BY created_at DESC,activation_id", (tenant, tenant)).fetchall()]
        if not rows:
            return []
        ids = [row["activation_id"] for row in rows]
        transitions = sql.execute(
            "SELECT activation_id,seq,from_state,to_state,reason,at FROM ("
            "SELECT activation_id,seq,from_state,to_state,reason,at,"
            "ROW_NUMBER() OVER (PARTITION BY activation_id ORDER BY seq DESC) AS rank"
            " FROM lab_activation_transitions WHERE tenant_id=? AND activation_id IN ("
            + ",".join("?" for _ in ids) + ")) AS latest WHERE rank<=20 ORDER BY seq DESC",
            (tenant, *ids)).fetchall()
        grouped = {aid: [] for aid in ids}
        for item in transitions:
            item = dict(item)
            grouped[item.pop("activation_id")].append(item)
        return [_public(sql, row, grouped[row["activation_id"]]) for row in rows]
    return _run(tenant, operation)


def holds_run(tenant, run_id):
    return _run(tenant, lambda sql: sql.execute(
        "SELECT 1 FROM lab_activations WHERE tenant_id=? AND in_flight_run_id=? AND state='active' LIMIT 1",
        (tenant, run_id)).fetchone() is not None)


def _control(tenant, activation_id, action, actor):
    identifier(activation_id)
    cancellations = []
    def operation(sql):
        cancellations.clear()
        row, now = _row(sql, tenant, activation_id), _now()
        allowed = {"pause": {"active"}, "resume": {"active", "paused"},
                   "renew": {"active", "paused", "expired"},
                   "off": {"active", "paused", "expired", "stopped"}}
        if row["state"] not in allowed[action]:
            raise Refused("lab_activation_state_conflict", 409)
        if action == "resume" and lab_run_store._date(row["lease_expires_at"]) <= now:
            _transition(sql, row, "expired", "lease_expired", actor, now, cancellations)
            return _public(sql, _row(sql, tenant, activation_id))
        if action in {"resume", "renew"}:
            reason = readmit(row)
            if reason:
                _transition(sql, row, "stopped", reason, actor, now, cancellations)
                return _public(sql, _row(sql, tenant, activation_id))
            if action == "renew":
                sql.execute("UPDATE lab_activations SET lease_expires_at=? WHERE tenant_id=? AND activation_id=?",
                            ((now + timedelta(hours=24)).isoformat(), tenant, activation_id))
            due = now
            if row["last_started_at"]:
                due = max(now, lab_run_store._date(row["last_started_at"]) + timedelta(seconds=row["interval_s"]))
            sql.execute("UPDATE lab_activations SET next_due_at=? WHERE tenant_id=? AND activation_id=?",
                        (due.isoformat(), tenant, activation_id))
        state = {"pause": "paused", "resume": "active", "renew": "active", "off": "off"}[action]
        _transition(sql, row, state, action, actor, now, cancellations)
        return _public(sql, _row(sql, tenant, activation_id))
    result = _run(tenant, operation, write=True)
    _cancel_committed(tenant, cancellations)
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
    unavailable = False
    try:
        graph = lab_artifacts.get(row["tenant_id"], row["doc_id"], row["revision"])["graph"]
    except lab_artifacts.NotFound:
        graph, unavailable = {}, True
    outputs = graph.get("outputs", {})
    blocks = sorted({ref["node"] for ref in outputs.values()})
    values, decoded, truncated = {}, {}, False
    events = []
    if blocks:
        size = "octet_length(CAST(payload AS TEXT))" if sql.postgres else "length(CAST(payload AS BLOB))"
        # Cast JSONB to text so even PostgreSQL's driver cannot decode it first.
        events = sql.execute(
            "SELECT block_id,CASE WHEN " + size + "<=65536 THEN CAST(payload AS TEXT) ELSE NULL END AS payload"
            " FROM lab_run_events WHERE tenant_id=? AND run_id=? AND seq IN ("
            "SELECT MAX(seq) FROM lab_run_events WHERE tenant_id=? AND run_id=? AND kind='value'"
            " AND block_id IN (" + ",".join("?" for _ in blocks) + ") GROUP BY block_id)",
            (row["tenant_id"], run["run_id"], row["tenant_id"], run["run_id"], *blocks)).fetchall()
    for event in events:
        values[event["block_id"]] = event["payload"]
    output, used = {}, 2
    for name, ref in outputs.items():
        node = ref["node"]
        if node in values and values[node] is None:
            truncated = True
            break
        if node not in decoded:
            decoded[node] = json.loads(values[node]) if node in values else {}
        value = decoded[node].get(ref["port"])
        item_size = len(json.dumps({name: value}, allow_nan=False).encode("utf-8")) - 2
        if used + item_size + (2 if output else 0) > 65536:
            truncated = True
            break
        output[name] = value
        used += item_size + (2 if len(output) > 1 else 0)
    if truncated:
        output = {"truncated": True}
    encoded = json.dumps(output, allow_nan=False)
    finished = run["finished_at"]
    summary = json.dumps({"run_id": run["run_id"], "state": run["state"],
                          "finished_at": finished.isoformat() if hasattr(finished, "isoformat") else finished,
                          "duration_s": max(0, (lab_run_store._date(finished) -
                                              lab_run_store._date(run["created_at"])).total_seconds())
                          if finished else None, "coalesced_ticks": row["coalesced_ticks"],
                          "harvest": "unavailable" if unavailable else "available"})
    placeholder = "?::jsonb" if sql.postgres else "?"
    sql.execute("UPDATE lab_activations SET last_output=" + placeholder + ",last_summary=" + placeholder +
                ",in_flight_run_id=NULL,coalesced_ticks=0 WHERE tenant_id=? AND activation_id=?",
                (encoded, summary, row["tenant_id"], row["activation_id"]))
    row["in_flight_run_id"] = None
    return unavailable


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
    cutoff = (now - timedelta(days=7)).isoformat()
    terminal = _run("lab-activation-scheduler", lambda sql: [dict(row) for row in sql.execute(
        "SELECT tenant_id,activation_id FROM lab_activations WHERE state IN ('stopped','off')"
        " AND updated_at<? ORDER BY updated_at,tenant_id,activation_id LIMIT 100", (cutoff,)).fetchall()])
    for candidate in terminal:
        def purge(sql):
            row = _row(sql, candidate["tenant_id"], candidate["activation_id"])
            if row["state"] in {"stopped", "off"} and lab_run_store._date(row["updated_at"]) < lab_run_store._date(cutoff):
                args = (row["tenant_id"], row["activation_id"])
                sql.execute("DELETE FROM lab_activation_transitions WHERE tenant_id=? AND activation_id=?", args)
                sql.execute("DELETE FROM lab_activations WHERE tenant_id=? AND activation_id=?", args)
        try:
            _run(candidate["tenant_id"], purge, write=True)
        except Exception as error:
            logging.getLogger(__name__).warning("lab_activation_purge_failed class=%s", type(error).__name__)
    due = _run("lab-activation-scheduler", lambda sql: [dict(row) for row in sql.execute(
        "SELECT tenant_id,activation_id FROM lab_activations WHERE state='active' AND next_due_at<=?"
        " ORDER BY next_due_at,tenant_id,activation_id LIMIT ?", (now.isoformat(), limit)).fetchall()])
    for candidate in due:
        tenant, aid = candidate["tenant_id"], candidate["activation_id"]
        cancellations = []
        def operation(sql):
            row = _row(sql, tenant, aid)
            if row["state"] != "active" or lab_run_store._date(row["next_due_at"]) > now:
                return
            if lab_run_store._date(row["lease_expires_at"]) <= now:
                _transition(sql, row, "expired", "lease_expired", "scheduler", now, cancellations)
                return
            if row["in_flight_run_id"]:
                run = lab_run_store._row(sql, tenant, row["in_flight_run_id"])
                if run is None:
                    _transition(sql, row, "stopped", "run_unavailable", "scheduler", now, cancellations)
                    return
                if run["state"] not in {"finished", "failed", "cancelled"}:
                    reason = readmit(row)
                    if reason:
                        _transition(sql, row, "stopped", reason, "scheduler", now, cancellations)
                        return
                    sql.execute("UPDATE lab_activations SET coalesced_ticks=coalesced_ticks+1,next_due_at=?,"
                                "updated_at=? WHERE tenant_id=? AND activation_id=?",
                                ((lab_run_store._date(row["next_due_at"]) + timedelta(seconds=row["interval_s"])).isoformat(),
                                 now.isoformat(), tenant, aid))
                    return
                if _harvest(sql, row, run):
                    _transition(sql, row, "stopped", "publication_changed", "scheduler", now, cancellations)
                    return
            reason = readmit(row)
            if reason:
                _transition(sql, row, "stopped", reason, "scheduler", now, cancellations)
                return
            try:
                run_id = _enqueue(sql, row, now)
            except TooLarge:
                sql.execute("UPDATE lab_activations SET coalesced_ticks=coalesced_ticks+1,next_due_at=?,"
                            "updated_at=? WHERE tenant_id=? AND activation_id=?",
                            ((now + timedelta(seconds=row["interval_s"])).isoformat(), now.isoformat(), tenant, aid))
                return
            sql.execute("UPDATE lab_activations SET in_flight_run_id=?,next_due_at=?,updated_at=?,last_started_at=?"
                        " WHERE tenant_id=? AND activation_id=?",
                        (run_id, (now + timedelta(seconds=row["interval_s"])).isoformat(), now.isoformat(),
                         now.isoformat(), tenant, aid))
        def successful(sql):
            cancellations.clear()
            operation(sql)
            sql.execute("UPDATE lab_activations SET failure_count=0 WHERE tenant_id=? AND activation_id=?", (tenant, aid))
        try:
            _run(tenant, successful, write=True)
        except Exception as error:
            cancellations.clear()
            logging.getLogger(__name__).warning("lab_activation_tick_failed class=%s", type(error).__name__)
            def failed(sql):
                cancellations.clear()
                row = _row(sql, tenant, aid)
                if row["state"] != "active" or lab_run_store._date(row["next_due_at"]) > now:
                    return
                failures = row["failure_count"] + 1
                sql.execute("UPDATE lab_activations SET failure_count=?,next_due_at=?,updated_at=?"
                            " WHERE tenant_id=? AND activation_id=?",
                            (failures, (now + timedelta(seconds=row["interval_s"])).isoformat(),
                             now.isoformat(), tenant, aid))
                if failures >= 3:
                    _transition(sql, row, "stopped", "readmit_unavailable", "scheduler", now, cancellations)
            try:
                _run(tenant, failed, write=True)
            except Exception as failure_error:
                cancellations.clear()
                logging.getLogger(__name__).warning("lab_activation_failure_record_failed class=%s", type(failure_error).__name__)
        _cancel_committed(tenant, cancellations)
