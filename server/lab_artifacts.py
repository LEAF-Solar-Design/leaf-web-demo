"""Tenant-bound graph ledger and immutable content-addressed canonical bytes.

All mutations serialize on the tenant, including first saves and quota checks.
SQLite is an explicit local-only authority; deployed processes must use Postgres.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import uuid

import platform_link
from lab_doc_validators import InvalidGraph, TooLarge, expected_head as validate_head, identifier, validate_graph

MAX_TENANT_DOCS = 100
MAX_TENANT_REVISIONS = 10000
MAX_TENANT_BYTES = 64 * 1024 * 1024
MAX_TENANT_PUBLISHES = 10000
LOG = logging.getLogger(__name__)


class Conflict(ValueError):
    pass


class KindFixed(Conflict):
    pass


class NotFound(LookupError):
    pass


class ExportForbidden(PermissionError):
    pass


class StoreUnavailable(RuntimeError):
    pass


class StoreMigrating(RuntimeError):
    pass


class GitConflict(Conflict):
    pass


class GitUnavailable(StoreUnavailable):
    pass


def _committed_write(tenant, operation, doc_id=None):
    import lab_artifacts_git as mirror
    import lab_git_authority as authority

    active = authority.authority_get(tenant)
    if active is not None and active['mode'] != 'git':
        raise GitUnavailable('lab_store_migrating')
    if active is not None and doc_id is not None:
        mirror.recover_document(tenant, doc_id)

    def guarded(sql):
        active = authority.authority_get_sql(sql, tenant)
        if active is None:
            mirror.check_frozen(sql, tenant)
        elif active['mode'] != 'git':
            raise GitUnavailable('lab_store_migrating')
        sql.git_authority = active
        try:
            result = operation(sql)
        except Conflict as error:
            if active and not isinstance(error, KindFixed):
                raise GitConflict('lab_artifact_conflict') from None
            raise
        if active and 'receipt_id' in result:
            state = mirror._state(sql, tenant)
            authority.enqueue_sql(sql, tenant, 'publish', state['local_sha'],
                {'target_sha': state['local_sha'], 'generation': str(active['generation'])})
        return result

    result = authority._run(tenant, guarded, write=True)
    if result.get('_git_staged'):
        return mirror.commit_pending(tenant, result, result['_generation'], author=result['_author'])
    try:
        mirror.after_write(tenant)
    except Exception:
        # No failure after commit may turn a successful ledger write into an
        # apparent failure that the caller would retry.
        pass
    return result


def _event(name, tenant, doc_id, revision, reason):
    # Never log exceptions, SQL, configuration, request bodies or host values.
    LOG.warning(name, extra={"tenant": tenant, "doc_id": doc_id,
                             "revision": revision, "reason": reason})


def store_mode():
    mode = os.environ.get("LEAF_LAB_STORE", "sqlite").strip().lower()
    if mode not in {"sqlite", "postgres"}:
        raise StoreUnavailable("lab_store_invalid")
    if mode == "sqlite":
        import runtime_posture

        if runtime_posture._deployed_runtime() or platform_link.postgres_required():
            raise StoreUnavailable("lab_postgres_required")
    return mode


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_documents (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, head INTEGER NOT NULL,
 published_revision INTEGER, kind TEXT NOT NULL DEFAULT 'graph' CHECK (kind IN ('graph','widget')),
 PRIMARY KEY (tenant_id, doc_id)
);
CREATE TABLE IF NOT EXISTS lab_blobs (
 tenant_id TEXT NOT NULL, sha256 TEXT NOT NULL, canonical_text TEXT NOT NULL,
 canonical_bytes BLOB NOT NULL, byte_count INTEGER NOT NULL,
 PRIMARY KEY (tenant_id, sha256)
);
CREATE TABLE IF NOT EXISTS lab_revisions (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision INTEGER NOT NULL,
 parent_revision INTEGER, author TEXT NOT NULL, created_at REAL NOT NULL,
 sha256 TEXT NOT NULL, PRIMARY KEY (tenant_id, doc_id, revision),
 FOREIGN KEY (tenant_id, sha256) REFERENCES lab_blobs (tenant_id, sha256),
 FOREIGN KEY (tenant_id, doc_id, parent_revision)
 REFERENCES lab_revisions (tenant_id, doc_id, revision)
);
CREATE TABLE IF NOT EXISTS lab_publish_receipts (
 receipt_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL,
 expected_head INTEGER NOT NULL, new_head INTEGER NOT NULL, sha256 TEXT NOT NULL,
 author TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY (tenant_id, doc_id, new_head)
 REFERENCES lab_revisions (tenant_id, doc_id, revision)
);
CREATE TRIGGER IF NOT EXISTS lab_blobs_immutable_update BEFORE UPDATE ON lab_blobs
 BEGIN SELECT RAISE(ABORT, 'immutable lab blob'); END;
CREATE TRIGGER IF NOT EXISTS lab_blobs_immutable_delete BEFORE DELETE ON lab_blobs
 BEGIN SELECT RAISE(ABORT, 'immutable lab blob'); END;
CREATE TRIGGER IF NOT EXISTS lab_revisions_immutable_update BEFORE UPDATE ON lab_revisions
 BEGIN SELECT RAISE(ABORT, 'immutable lab revision'); END;
CREATE TRIGGER IF NOT EXISTS lab_revisions_immutable_delete BEFORE DELETE ON lab_revisions
 BEGIN SELECT RAISE(ABORT, 'immutable lab revision'); END;
CREATE TRIGGER IF NOT EXISTS lab_publish_receipts_immutable_update BEFORE UPDATE ON lab_publish_receipts
 BEGIN SELECT RAISE(ABORT, 'immutable lab receipt'); END;
CREATE TRIGGER IF NOT EXISTS lab_publish_receipts_immutable_delete BEFORE DELETE ON lab_publish_receipts
 BEGIN SELECT RAISE(ABORT, 'immutable lab receipt'); END;
CREATE TABLE IF NOT EXISTS lab_layouts (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, sha256 TEXT NOT NULL,
 layout_text TEXT NOT NULL, byte_count INTEGER NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY (tenant_id, doc_id, sha256),
 FOREIGN KEY (tenant_id, sha256) REFERENCES lab_blobs (tenant_id, sha256)
);
"""


class _SQL:
    def __init__(self, conn, postgres):
        self.conn = conn
        self.postgres = postgres

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s") if self.postgres else sql, params)


def _run(tenant, operation, *, write=False):
    if not isinstance(tenant, str) or not tenant or len(tenant) > 200:
        raise InvalidGraph("invalid_tenant")
    if store_mode() == "postgres":
        def transaction(conn):
            sql = _SQL(conn, True)
            if write:
                sql.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                            ("lab:" + tenant,))
            return operation(sql)

        return platform_link.platform_db().run_transaction(transaction)
    db_path = os.environ.get("LEAF_LAB_DB")
    if not db_path or not db_path.strip():
        raise StoreUnavailable("lab_db_unset")
    path = Path(db_path).resolve()
    # Resolve symlinks before checking the forbidden durable filesystem root.
    state_root = Path("/data/state").resolve()
    if path == state_root or state_root in path.parents:
        raise StoreUnavailable("lab_sqlite_path_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SQLITE_SCHEMA)
        from lab_artifacts_git import _SQLITE_SCHEMA as mirror_schema

        conn.executescript(mirror_schema)
        if "kind" not in {row["name"] for row in conn.execute("PRAGMA table_info(lab_documents)")}:
            try:
                conn.execute("ALTER TABLE lab_documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'graph'"
                             " CHECK (kind IN ('graph','widget'))")
            except sqlite3.OperationalError:
                # Another local connection may have completed the same expand.
                if "kind" not in {row["name"] for row in conn.execute("PRAGMA table_info(lab_documents)")}:
                    raise
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        result = operation(_SQL(conn, False))
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _head(sql, tenant, doc_id):
    row = sql.execute("SELECT head FROM lab_documents WHERE tenant_id=? AND doc_id=?",
                      (tenant, doc_id)).fetchone()
    return row["head"] if row else None


def _revision(sql, tenant, doc_id, revision):
    row = sql.execute(
        "SELECT r.*, d.kind, b.canonical_text, b.canonical_bytes, b.byte_count FROM lab_revisions r"
        " JOIN lab_documents d ON d.tenant_id=r.tenant_id AND d.doc_id=r.doc_id"
        " JOIN lab_blobs b ON b.tenant_id=r.tenant_id AND b.sha256=r.sha256"
        " WHERE r.tenant_id=? AND r.doc_id=? AND r.revision=?",
        (tenant, doc_id, revision)).fetchone()
    if row is None:
        raise NotFound("lab_revision_not_found")
    result = dict(row)
    data = bytes(result.pop("canonical_bytes"))
    if (data != result["canonical_text"].encode("utf-8") or len(data) != result["byte_count"]
            or hashlib.sha256(data).hexdigest() != result["sha256"]):
        raise StoreUnavailable("lab_digest_mismatch")
    result["graph"] = json.loads(result["canonical_text"])
    return result


def _check_head(sql, tenant, doc_id, expected):
    current = _head(sql, tenant, doc_id)
    if current != expected:
        raise Conflict("stale_expected_head")
    return current


def _append(sql, tenant, doc_id, data, author, current, parent, kind="graph"):
    if getattr(sql, 'git_authority', None) is not None:
        from lab_artifacts_git import stage_write

        return stage_write(sql, tenant, doc_id, data, author, current, parent, kind)
    count = sql.execute("SELECT COUNT(*) AS n FROM lab_documents WHERE tenant_id=?", (tenant,)).fetchone()["n"]
    revisions = sql.execute("SELECT COUNT(*) AS n FROM lab_revisions WHERE tenant_id=?", (tenant,)).fetchone()["n"]
    if (current is None and count >= MAX_TENANT_DOCS) or revisions >= MAX_TENANT_REVISIONS:
        raise TooLarge("tenant_revision_cap")
    digest = hashlib.sha256(data).hexdigest()
    existing = sql.execute("SELECT canonical_bytes FROM lab_blobs WHERE tenant_id=? AND sha256=?",
                           (tenant, digest)).fetchone()
    if existing is None:
        used = sql.execute("SELECT COALESCE(SUM(byte_count),0) AS n FROM lab_blobs WHERE tenant_id=?",
                           (tenant,)).fetchone()["n"]
        if used + len(data) > MAX_TENANT_BYTES:
            raise TooLarge("tenant_byte_cap")
        sql.execute("INSERT INTO lab_blobs (tenant_id,sha256,canonical_text,canonical_bytes,byte_count)"
                    " VALUES (?,?,?,?,?)", (tenant, digest, data.decode("utf-8"), data, len(data)))
    elif bytes(existing["canonical_bytes"]) != data:
        raise StoreUnavailable("lab_digest_mismatch")
    revision = (current or 0) + 1
    sql.execute("INSERT INTO lab_revisions (tenant_id,doc_id,revision,parent_revision,author,created_at,sha256)"
                " VALUES (?,?,?,?,?,?,?)", (tenant, doc_id, revision, parent, author, time.time(), digest))
    if current is None:
        sql.execute("INSERT INTO lab_documents (tenant_id,doc_id,head,kind) VALUES (?,?,?,?)", (tenant, doc_id, revision, kind))
    else:
        sql.execute("UPDATE lab_documents SET head=? WHERE tenant_id=? AND doc_id=?", (revision, tenant, doc_id))
    return _revision(sql, tenant, doc_id, revision)


def save(tenant, doc_id, graph, expected_head, author, kind="graph"):
    identifier(doc_id)
    validate_head(expected_head)
    if not isinstance(author, str) or not author or len(author) > 200:
        raise InvalidGraph("invalid_author")

    def validated_data(document_kind):
        if document_kind not in ("graph", "widget"):
            raise InvalidGraph("invalid_document_kind")
        if document_kind == "widget":
            from lab_widget_doc import validate_widget

            return validate_widget(graph)
        return validate_graph(graph)

    try:
        # Reject invalid first saves without creating a local database. An
        # existing store must be consulted first, even with a null expected head.
        db_path = os.environ.get("LEAF_LAB_DB")
        empty_local_store = (store_mode() == "sqlite" and db_path and not Path(db_path).exists())
        data = validated_data(kind) if empty_local_store else None

        def operation(sql):
            row = sql.execute("SELECT kind FROM lab_documents WHERE tenant_id=? AND doc_id=?",
                              (tenant, doc_id)).fetchone()
            if row is not None and row["kind"] != kind:
                raise KindFixed("lab_document_kind_fixed")
            current = _check_head(sql, tenant, doc_id, expected_head)
            document_kind = row["kind"] if row is not None else kind
            payload = data if data is not None else validated_data(document_kind)
            return _append(sql, tenant, doc_id, payload, author, current, current, document_kind)

        return _committed_write(tenant, operation, doc_id)
    except Conflict:
        _event("lab_save_conflict", tenant, doc_id, expected_head, "stale_expected_head")
        raise
    except Exception:
        _event("lab.save_failed", tenant, doc_id, expected_head, "save_refused")
        raise


def get(tenant, doc_id, revision=None):
    identifier(doc_id)
    if revision is not None:
        validate_head(revision)

    def operation(sql):
        selected = _head(sql, tenant, doc_id) if revision is None else revision
        if selected is None:
            raise NotFound("lab_document_not_found")
        result = _revision(sql, tenant, doc_id, selected)
        if revision is None:
            row = sql.execute("SELECT published_revision FROM lab_documents WHERE tenant_id=? AND doc_id=?",
                              (tenant, doc_id)).fetchone()
            result["published_revision"] = row["published_revision"]
        return result

    return _run(tenant, operation)


def list_revisions(tenant, doc_id, *, limit=100, before=None):
    identifier(doc_id)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise InvalidGraph("invalid_limit")
    if before is not None:
        validate_head(before)

    def operation(sql):
        if _head(sql, tenant, doc_id) is None:
            raise NotFound("lab_document_not_found")
        rows = sql.execute("SELECT r.*, d.kind FROM lab_revisions r JOIN lab_documents d"
                           " ON d.tenant_id=r.tenant_id AND d.doc_id=r.doc_id"
                           " WHERE r.tenant_id=? AND r.doc_id=?"
                           " AND r.revision<? ORDER BY r.revision DESC LIMIT ?",
                           (tenant, doc_id, before if before is not None else 9223372036854775807, limit)).fetchall()
        return [dict(row) for row in rows]

    return _run(tenant, operation)


def export_bundle(tenant, doc_id, revision, *, authorize):
    """Export a pinned composition and every stored evaluation, without sources.

    authorize rechecks the live principal's project/material access. It runs
    before private results are read and again before they leave this boundary.
    """
    from datetime import datetime
    from lab_canonical import canonical_bytes, graph_digest
    import lab_layouts
    import lab_run_store as runs

    artifact = get(tenant, doc_id, revision)
    if artifact["kind"] != "graph":
        raise InvalidGraph("invalid_lab_document")
    authorize(artifact["graph"], None)

    def portable(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: portable(child) for key, child in value.items()}
        if isinstance(value, list):
            return [portable(child) for child in value]
        return value

    def snapshot(sql):
        result, byte_count = [], 0
        for row in sql.execute(
                "SELECT * FROM lab_runs WHERE tenant_id=? AND doc_id=?"
                " AND graph_revision=? ORDER BY created_at,run_id",
                (tenant, doc_id, artifact["revision"])).fetchall():
            run = dict(row)
            if run["graph_sha256"] != artifact["sha256"]:
                raise StoreUnavailable("lab_export_lineage_mismatch")
            authorize(artifact["graph"], run)
            events = []
            for event in sql.execute(
                    "SELECT * FROM lab_run_events WHERE tenant_id=? AND run_id=? ORDER BY seq",
                    (tenant, run["run_id"])):
                event = dict(event)
                if isinstance(event["payload"], str):
                    event["payload"] = json.loads(event["payload"])
                byte_count += len(canonical_bytes(portable(event)))
                if byte_count > MAX_TENANT_BYTES:
                    raise TooLarge("lab_export_too_large")
                events.append(event)
            result.append((run, events))
        return result, byte_count

    evaluations, policies = [], []
    snapshots, export_bytes = runs._run(tenant, snapshot)
    for run, events in snapshots:
        complete = {}
        # Corpus value events carry a bounded summary. Resolve its durable job
        # to retain candidates, reports and policy identities, not just verdicts.
        for event in events:
            if event["kind"] != "value":
                continue
            block = next((block for block in artifact["graph"]["blocks"]
                          if block["id"] == event["block_id"]
                          and block["registry"]["id"] == "corpus-evaluator"), None)
            if block is None:
                continue
            envelope = event["payload"].get("result", {})
            summary = envelope.get("data") if isinstance(envelope, dict) else None
            if isinstance(summary, str):
                summary = json.loads(summary)
            if not isinstance(summary, dict) or not summary.get("job_id"):
                continue
            import jobs
            import lab_corpus_executor as corpus
            job = jobs.get_job(summary["job_id"])
            if (job is None or job["tenant_id"] != tenant
                    or str(job.get("org_id")) != tenant
                    or str(job.get("project_id")) != block["settings"]["project_id"]
                    or job.get("idempotency_key") != f'lab:{run["run_id"]}:{block["id"]}:corpus:v1'
                    or job.get("tool") != corpus.OPERATION or job.get("status") != "complete"):
                raise StoreUnavailable("lab_export_result_unavailable")
            value = job["result"]["result"]
            context = corpus.validate_context(value["context"])
            if (context["tenant_id"] != tenant
                    or context["project_id"] != block["settings"]["project_id"]
                    or any(context[key] != block["settings"][key] for key in ("corpus", "suite"))
                    or any(summary.get(key) != run[key] for key in
                           ("run_id", "doc_id", "graph_revision", "graph_sha256"))
                    or summary.get("block_id") != block["id"]
                    or value.get("job_id") != summary["job_id"]
                    or any(value.get(key) != summary.get(key) for key in ("worker_id", "attempt", "rows_sha256", "row_count"))
                    or value["result_sha256"] != summary["result_sha256"]
                    or value["result"].get("suite_sha256") != context["suite"]["sha256"]
                    or len(canonical_bytes(value)) > corpus.MAX_RESULT_BYTES + 8192):
                raise StoreUnavailable("lab_export_lineage_mismatch")
            # The producer hashes its sorted JSON result including one newline;
            # the bundle's manifest separately hashes canonical Lab JSON parts.
            raw_result = json.dumps(value["result"], sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            if hashlib.sha256(raw_result).hexdigest() != value["result_sha256"]:
                raise StoreUnavailable("lab_export_result_mismatch")
            if summary["job_id"] not in complete:
                export_bytes += len(canonical_bytes(value))
                if export_bytes > MAX_TENANT_BYTES:
                    raise TooLarge("lab_export_too_large")
            complete[summary["job_id"]] = value
            report = value["result"].get("confirm_report") or {}
            policies.append({"run_id": run["run_id"], "block_id": block["id"],
                             "suite": context["suite"],
                             "parent_policy_digest": report.get("parent_policy_digest"),
                             "child_policy_digest": report.get("child_policy_digest")})
        public_run = {key: value for key, value in run.items() if key not in
                      {"lease_owner", "lease_fence", "corpus_authority_json"}}
        evaluations.append({"run": public_run, "events": events, "complete_results": complete})
        authorize(artifact["graph"], run)

    parts = {"identity": {key: artifact[key] for key in
                          ("tenant_id", "doc_id", "revision", "parent_revision", "sha256", "kind", "author", "created_at")},
             "graph": artifact["graph"],
             "layout": lab_layouts.get_layout(tenant, doc_id, artifact["sha256"]),
             "policy_reference": {"graph_sha256": artifact["sha256"],
                                  "blocks": [{"id": block["id"], "registry": block["registry"],
                                              "settings_sha256": graph_digest(block["settings"])}
                                             for block in artifact["graph"]["blocks"]],
                                  "compiled": policies},
             "evaluations": evaluations}

    parts = portable(parts)
    bundle = {"schema": "leaf.lab-bundle.v1", "parts": parts,
              "manifest": {"algorithm": "sha256", "parts": {
                  key: graph_digest(value) for key, value in parts.items()}}}
    if len(canonical_bytes(bundle)) > MAX_TENANT_BYTES:
        raise TooLarge("lab_export_too_large")
    authorize(artifact["graph"], None)
    return bundle


def restore(tenant, doc_id, revision, expected_head, author):
    identifier(doc_id)
    validate_head(revision)
    validate_head(expected_head)
    if revision is None or expected_head is None:
        raise InvalidGraph("revision_required")
    if not isinstance(author, str) or not author or len(author) > 200:
        raise InvalidGraph("invalid_author")

    def operation(sql):
        current = _head(sql, tenant, doc_id)
        if current is None:
            raise NotFound("lab_document_not_found")
        _check_head(sql, tenant, doc_id, expected_head)
        source = _revision(sql, tenant, doc_id, revision)
        return _append(sql, tenant, doc_id, source["canonical_text"].encode("utf-8"), author, current, revision, source['kind'])

    try:
        return _committed_write(tenant, operation, doc_id)
    except Conflict:
        _event("lab_save_conflict", tenant, doc_id, expected_head, "stale_expected_head")
        raise


def publish(tenant, doc_id, expected_head, author):
    identifier(doc_id)
    validate_head(expected_head)
    if expected_head is None:
        raise InvalidGraph("revision_required")
    if not isinstance(author, str) or not author or len(author) > 200:
        raise InvalidGraph("invalid_author")

    def operation(sql):
        if _head(sql, tenant, doc_id) is None:
            raise NotFound("lab_document_not_found")
        _check_head(sql, tenant, doc_id, expected_head)
        source = _revision(sql, tenant, doc_id, expected_head)
        count = sql.execute("SELECT COUNT(*) AS n FROM lab_publish_receipts WHERE tenant_id=?", (tenant,)).fetchone()["n"]
        if count >= MAX_TENANT_PUBLISHES:
            raise TooLarge("tenant_publish_cap")
        receipt = {"receipt_id": str(uuid.uuid4()), "tenant_id": tenant, "doc_id": doc_id,
                   "expected_head": expected_head, "new_head": expected_head,
                   "sha256": source["sha256"], "author": author, "created_at": time.time()}
        sql.execute("INSERT INTO lab_publish_receipts"
                    " (receipt_id,tenant_id,doc_id,expected_head,new_head,sha256,author,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", tuple(receipt.values()))
        sql.execute("UPDATE lab_documents SET published_revision=? WHERE tenant_id=? AND doc_id=?",
                    (expected_head, tenant, doc_id))
        return receipt

    try:
        return _committed_write(tenant, operation, doc_id)
    except Conflict:
        _event("lab_publish_cas_fail", tenant, doc_id, expected_head, "stale_expected_head")
        raise
