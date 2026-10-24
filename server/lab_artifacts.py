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


class NotFound(LookupError):
    pass


class StoreUnavailable(RuntimeError):
    pass


def _event(name, tenant, doc_id, revision, reason):
    # Never log exceptions, SQL, configuration, request bodies or host values.
    LOG.warning(name, extra={"tenant": tenant, "doc_id": doc_id,
                             "revision": revision, "reason": reason})


def store_mode():
    mode = os.environ.get("LEAF_LAB_STORE", "sqlite").strip().lower()
    if mode not in {"sqlite", "postgres"}:
        raise StoreUnavailable("lab_store_invalid")
    if mode == "sqlite":
        from broker import _deployed_runtime

        if _deployed_runtime() or platform_link.postgres_required():
            raise StoreUnavailable("lab_postgres_required")
    return mode


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_documents (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, head INTEGER NOT NULL,
 published_revision INTEGER, PRIMARY KEY (tenant_id, doc_id)
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
        "SELECT r.*, b.canonical_text, b.canonical_bytes, b.byte_count FROM lab_revisions r"
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


def _append(sql, tenant, doc_id, data, author, current, parent):
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
        sql.execute("INSERT INTO lab_documents (tenant_id,doc_id,head) VALUES (?,?,?)", (tenant, doc_id, revision))
    else:
        sql.execute("UPDATE lab_documents SET head=? WHERE tenant_id=? AND doc_id=?", (revision, tenant, doc_id))
    return _revision(sql, tenant, doc_id, revision)


def save(tenant, doc_id, graph, expected_head, author):
    identifier(doc_id)
    validate_head(expected_head)
    if not isinstance(author, str) or not author or len(author) > 200:
        raise InvalidGraph("invalid_author")
    try:
        data = validate_graph(graph)

        def operation(sql):
            current = _check_head(sql, tenant, doc_id, expected_head)
            return _append(sql, tenant, doc_id, data, author, current, current)

        return _run(tenant, operation, write=True)
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
        rows = sql.execute("SELECT * FROM lab_revisions WHERE tenant_id=? AND doc_id=?"
                           " AND revision<? ORDER BY revision DESC LIMIT ?",
                           (tenant, doc_id, before if before is not None else 9223372036854775807, limit)).fetchall()
        return [dict(row) for row in rows]

    return _run(tenant, operation)


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
        return _append(sql, tenant, doc_id, source["canonical_text"].encode("utf-8"), author, current, revision)

    try:
        return _run(tenant, operation, write=True)
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
        return _run(tenant, operation, write=True)
    except Conflict:
        _event("lab_publish_cas_fail", tenant, doc_id, expected_head, "stale_expected_head")
        raise
