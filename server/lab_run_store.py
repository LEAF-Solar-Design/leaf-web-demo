"""Tenant-bound Lab runs, fenced leases and transactionally numbered events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

import platform_link
import lab_registry
from lab_artifacts import NotFound, StoreUnavailable, _SQL
from lab_doc_validators import InvalidGraph, TooLarge

MAX_EVENTS = 10000
_PIN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_runs (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, run_id TEXT NOT NULL,
 graph_revision INTEGER NOT NULL, graph_sha256 TEXT NOT NULL,
 registry_digest TEXT NOT NULL, ops_revision TEXT NOT NULL, input_digest TEXT NOT NULL,
 adapter_revision TEXT NOT NULL, build_revision TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('queued','running','finished','failed','cancelled')),
 lease_owner TEXT, lease_fence INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT,
 next_seq INTEGER NOT NULL DEFAULT 0, event_count INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
 PRIMARY KEY (tenant_id, run_id)
);
CREATE INDEX IF NOT EXISTS lab_runs_doc_created_idx ON lab_runs (tenant_id,doc_id,created_at DESC);
CREATE TABLE IF NOT EXISTS lab_run_events (
 tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
 block_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id,run_id,seq),
 FOREIGN KEY (tenant_id,run_id) REFERENCES lab_runs (tenant_id,run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS lab_run_events_created_idx ON lab_run_events (created_at);
"""


def store_mode():
    mode = os.environ.get("LEAF_LAB_RUN_STORE", "sqlite").strip().lower()
    if mode == "postgres_required":
        return "postgres"
    if mode not in {"sqlite", "postgres"}:
        raise StoreUnavailable("lab_run_store_invalid")
    if mode == "sqlite":
        from broker import _deployed_runtime

        if _deployed_runtime() or platform_link.postgres_required():
            raise StoreUnavailable("lab_postgres_required")
    return mode


def _pin(value):
    if not isinstance(value, str) or _PIN.fullmatch(value) is None:
        raise InvalidGraph("invalid_lab_run_pin")
    return value


def registry_digest():
    with lab_registry._REGISTRY_LOCK:
        pairs = sorted(lab_registry.REGISTRY)
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("invalid_lab_run_time")
    return result.astimezone(timezone.utc)


def _run(tenant, operation, *, write=False):
    if not isinstance(tenant, str) or not tenant or len(tenant) > 200:
        raise InvalidGraph("invalid_tenant")
    if store_mode() == "postgres":
        def transaction(conn):
            sql = _SQL(conn, True)
            if write:
                sql.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("lab:" + tenant,))
            return operation(sql)

        return platform_link.platform_db().run_transaction(transaction)
    db_path = os.environ.get("LEAF_LAB_DB")
    if not db_path or not db_path.strip():
        raise StoreUnavailable("lab_db_unset")
    path = Path(db_path).resolve()
    state_root = Path("/data/state").resolve()
    if path == state_root or state_root in path.parents:
        raise StoreUnavailable("lab_sqlite_path_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SQLITE_SCHEMA)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        result = operation(_SQL(conn, False))
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row(sql, tenant, run_id):
    return sql.execute("SELECT * FROM lab_runs WHERE tenant_id=? AND run_id=?", (tenant, run_id)).fetchone()


def create(tenant, doc_id, run_id, graph_revision, graph_sha256, ops_revision,
           input_digest, adapter_revision, build_revision, *, registry_digest=None):
    for value in (doc_id, run_id, graph_sha256, ops_revision, input_digest, adapter_revision, build_revision):
        _pin(value)
    if type(graph_revision) is not int or graph_revision < 1:
        raise InvalidGraph("invalid_lab_graph_revision")
    digest = globals()["registry_digest"]()
    if registry_digest is not None and _pin(registry_digest) != digest:
        raise InvalidGraph("lab_registry_digest_mismatch")

    def operation(sql):
        now = _now().isoformat()
        sql.execute("INSERT INTO lab_runs (tenant_id,doc_id,run_id,graph_revision,graph_sha256,"
                    "registry_digest,ops_revision,input_digest,adapter_revision,build_revision,state,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tenant, doc_id, run_id, graph_revision, graph_sha256, digest, ops_revision,
                     input_digest, adapter_revision, build_revision, "queued", now, now))
        return dict(_row(sql, tenant, run_id))

    return _run(tenant, operation, write=True)


def get(tenant, run_id):
    _pin(run_id)

    def operation(sql):
        row = _row(sql, tenant, run_id)
        if row is None:
            raise NotFound("lab_run_not_found")
        return dict(row)

    return _run(tenant, operation)


def _ttl(ttl_s):
    if type(ttl_s) not in (int, float) or not math.isfinite(ttl_s) or ttl_s <= 0:
        raise ValueError("invalid_lab_lease_ttl")
    return timedelta(seconds=ttl_s)


def claim(tenant, run_id, owner, ttl_s):
    _pin(run_id)
    _pin(owner)
    duration = _ttl(ttl_s)

    def operation(sql):
        now = _now()
        row = _row(sql, tenant, run_id)
        if (row is None or row["state"] not in {"queued", "running"}
                or (row["lease_expires_at"] and _date(row["lease_expires_at"]) > now)):
            return False
        fence = row["lease_fence"] + 1
        expires = (now + duration).isoformat()
        sql.execute("UPDATE lab_runs SET state='running',lease_owner=?,lease_fence=?,"
                    "lease_expires_at=?,updated_at=? WHERE tenant_id=? AND run_id=?",
                    (owner, fence, expires, now.isoformat(), tenant, run_id))
        return {"owner": owner, "fence": fence, "expires_at": expires}

    return _run(tenant, operation, write=True)


def _holder(sql, tenant, run_id, owner, fence, now):
    row = _row(sql, tenant, run_id)
    if (type(fence) is not int or row is None or row["state"] != "running"
            or row["lease_owner"] != owner or row["lease_fence"] != fence
            or not row["lease_expires_at"] or _date(row["lease_expires_at"]) <= now):
        return None
    return row


def heartbeat(tenant, run_id, owner, fence, ttl_s):
    duration = _ttl(ttl_s)

    def operation(sql):
        now = _now()
        row = _holder(sql, tenant, run_id, owner, fence, now)
        if row is None:
            return False
        expires = max(_date(row["lease_expires_at"]), now + duration)
        sql.execute("UPDATE lab_runs SET lease_expires_at=?,updated_at=? WHERE tenant_id=? AND run_id=?",
                    (expires.isoformat(), now.isoformat(), tenant, run_id))
        return True

    return _run(tenant, operation, write=True)


def finish(tenant, run_id, owner, fence, state="finished"):
    if state not in {"finished", "failed", "cancelled"}:
        raise ValueError("invalid_lab_run_state")

    def operation(sql):
        now = _now()
        if _holder(sql, tenant, run_id, owner, fence, now) is None:
            return False
        sql.execute("UPDATE lab_runs SET state=?,finished_at=?,updated_at=?,lease_expires_at=NULL"
                    " WHERE tenant_id=? AND run_id=?",
                    (state, now.isoformat(), now.isoformat(), tenant, run_id))
        return True

    return _run(tenant, operation, write=True)


def _insert_event(sql, tenant, run_id, seq, block, kind, payload, now):
    sql.execute("INSERT INTO lab_run_events (tenant_id,run_id,seq,block_id,kind,payload,created_at)"
                " VALUES (?,?,?,?,?," + ("?::jsonb" if sql.postgres else "?") + ",?)",
                (tenant, run_id, seq, block, kind, payload, now))


def _coalesce(sql, tenant, run_id, seq, count, now):
    oldest = sql.execute("SELECT seq,block_id FROM lab_run_events WHERE tenant_id=? AND run_id=?"
                         " ORDER BY seq LIMIT ?", (tenant, run_id, count // 2)).fetchall()
    latest = {row["block_id"]: row["seq"] for row in oldest if row["block_id"] is not None}
    removed = [row["seq"] for row in oldest
               if row["block_id"] is not None and row["seq"] != latest[row["block_id"]]]
    # Preserve non-block control events and unique blocks; refuse overflow if
    # there is nothing losslessly coalescible, rolling back the entire append.
    if not removed:
        raise TooLarge("lab_run_event_cap")
    cutoff = oldest[-1]["seq"]
    sql.execute("DELETE FROM lab_run_events WHERE tenant_id=? AND run_id=? AND seq<=?"
                " AND block_id IS NOT NULL AND seq NOT IN (SELECT MAX(seq) FROM lab_run_events"
                " WHERE tenant_id=? AND run_id=? AND seq<=? AND block_id IS NOT NULL GROUP BY block_id)",
                (tenant, run_id, cutoff, tenant, run_id, cutoff))
    _insert_event(sql, tenant, run_id, seq, None, "coalesced", json.dumps({"count": len(removed)}), now)
    return seq + 1, count - len(removed) + 1


def append_events(tenant, run_id, owner, fence, events):
    prepared = []
    for event in events:
        kind = _pin(event["kind"])
        block = event.get("block_id")
        if block is not None:
            _pin(block)
        prepared.append((block, kind, json.dumps(event.get("payload", {}), allow_nan=False)))

    def operation(sql):
        now = _now()
        row = _holder(sql, tenant, run_id, owner, fence, now)
        if row is None:
            return False
        seq, count = row["next_seq"], row["event_count"]
        for block, kind, payload in prepared:
            _insert_event(sql, tenant, run_id, seq, block, kind, payload, now.isoformat())
            seq, count = seq + 1, count + 1
            if count >= MAX_EVENTS:
                seq, count = _coalesce(sql, tenant, run_id, seq, count, now.isoformat())
                if count > MAX_EVENTS:
                    raise TooLarge("lab_run_event_cap")
        sql.execute("UPDATE lab_runs SET next_seq=?,event_count=?,updated_at=? WHERE tenant_id=? AND run_id=?",
                    (seq, count, now.isoformat(), tenant, run_id))
        return True

    return _run(tenant, operation, write=True)


def list_events(tenant, run_id):
    def operation(sql):
        if _row(sql, tenant, run_id) is None:
            raise NotFound("lab_run_not_found")
        result = []
        for row in sql.execute("SELECT * FROM lab_run_events WHERE tenant_id=? AND run_id=? ORDER BY seq",
                               (tenant, run_id)).fetchall():
            event = dict(row)
            if not sql.postgres:
                event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    return _run(tenant, operation)


def sweep(now):
    """Retain fifty newest runs per tenant/document and seven days of events."""
    cutoff = (_date(now) - timedelta(days=7)).isoformat()
    # Discover tenants read-only; each deletion then shares the writers' lock.
    tenants = _run("lab-sweep", lambda sql: [row["tenant_id"] for row in
                   sql.execute("SELECT DISTINCT tenant_id FROM lab_runs").fetchall()])
    for tenant in tenants:
        def operation(sql):
            rows = sql.execute("SELECT run_id,doc_id FROM lab_runs WHERE tenant_id=?"
                               " ORDER BY created_at DESC,run_id DESC", (tenant,)).fetchall()
            counts = {}
            for row in rows:
                doc = row["doc_id"]
                counts[doc] = counts.get(doc, 0) + 1
                if counts[doc] > 50:
                    sql.execute("DELETE FROM lab_runs WHERE tenant_id=? AND run_id=?", (tenant, row["run_id"]))
            sql.execute("DELETE FROM lab_run_events WHERE tenant_id=? AND created_at<?", (tenant, cutoff))
            sql.execute("UPDATE lab_runs SET event_count=(SELECT COUNT(*) FROM lab_run_events e"
                        " WHERE e.tenant_id=lab_runs.tenant_id AND e.run_id=lab_runs.run_id) WHERE tenant_id=?", (tenant,))

        _run(tenant, operation, write=True)
