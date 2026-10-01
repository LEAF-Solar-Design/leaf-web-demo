"""Reconcile one explicitly scoped, drained legacy upload marker into PostgreSQL.

Scope is the upload MARKER only (upload.state.json -> drawing_upload_attempts)
for one tenant/drawing. Backfill is a DRY RUN unless --apply is passed: a dry
run opens a read-only transaction and writes nothing. Purge receipts, active
extraction or purge leases, conflicting rows and non-terminal markers fail
closed with zero writes. This command never changes selectors, never touches
blob bytes and never imports purge receipts.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

RECEIPT_SCHEMA = "leaf.upload-authority-reconciliation.v1"
SUPPORTED_SCOPES = ("markers",)
# Accepted on the command line only so that it is REFUSED by name rather than
# read as a typo: purge receipt import is outside this slice.
REFUSED_SCOPES = ("purge_receipts",)
TERMINAL_STATUSES = frozenset({"ready", "failed"})
TENANT_KINDS = frozenset({"guest", "account"})
MAX_MARKER_BYTES = 1024 * 1024  # bounded read: a marker is a few hundred bytes


def _sanitize_id(raw):
    """The shared canonical tenant/opaque-id rule (da/store.sanitize_id), inline."""
    m = re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", raw) if isinstance(raw, str) else None
    if m is None:
        raise ValueError(f"invalid id {raw!r}")
    return str(m.group(0))


def marker_key(tenant_id, drawing_id):
    """Same key as write_loop.upload_marker_key, without importing the server."""
    return (f"tenants/{_sanitize_id(tenant_id)}/drawings/"
            f"{_sanitize_id(drawing_id)}/upload.state.json")


def advisory_lock_id(tenant_id, drawing_id):
    """Shared with reconcile_drawing_authority so both reconcilers serialize per drawing."""
    digest = hashlib.sha256(f"{tenant_id}/{drawing_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate marker field")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"non-finite marker value {name}")


def _path(root, key):
    root = Path(root).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes explicit directory")
    return path


def _timestamp(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    elif type(value) is str:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid retention timestamp") from exc
    else:
        raise ValueError("invalid retention timestamp")
    if stamp.tzinfo is None:
        raise ValueError("retention timestamp requires timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def read_marker_bytes(source_dir, tenant_id, drawing_id):
    path = _path(source_dir, marker_key(tenant_id, drawing_id))
    if not path.is_file():
        raise ValueError("no legacy upload marker for this drawing")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MARKER_BYTES + 1)
    if len(raw) > MAX_MARKER_BYTES:
        raise ValueError("upload marker exceeds size bound")
    return raw


def validate_marker(raw):
    """Return the canonical row a drained terminal marker maps to; fail closed otherwise."""
    try:
        marker = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=_reject_constant)
    except UnicodeDecodeError as exc:
        raise ValueError("upload marker is not UTF-8") from exc
    if type(marker) is not dict:
        raise ValueError("upload marker must be an object")
    if marker.get("authority") == "postgres":
        raise ValueError("marker already delegates to PostgreSQL authority")
    if type(marker.get("schema")) is not int or marker["schema"] != 1:
        raise ValueError("unsupported marker schema")
    status = marker.get("status")
    if status == "extracting":
        raise ValueError("active extraction lease must be drained")
    if status in {"purging", "purged"}:
        raise ValueError("purge state is outside marker scope")
    if status not in TERMINAL_STATUSES:
        raise ValueError("unsupported marker status")
    attempt = marker.get("attempt")
    if type(attempt) is not str or re.fullmatch(r"[0-9a-f]{16}", attempt) is None:
        raise ValueError("unsupported marker attempt")
    if marker.get("tenant_kind") not in TENANT_KINDS:
        raise ValueError("unsupported marker tenant_kind")
    if status == "ready":
        version = marker.get("extracted_version")
        if type(version) is not int or not 1 <= version <= 99999999:
            raise ValueError("ready marker requires an extracted version")
        if type(marker.get("intake_ref")) is not str or not marker["intake_ref"]:
            raise ValueError("ready marker requires an intake_ref")
    return {"attempt": attempt, "status": status,
            "retention_expires_at": _timestamp(marker.get("retention_expires_at")),
            "marker": marker}


def _canonical_target(row):
    """Normalize a stored row, refusing any live lease or purge state."""
    if row is None:
        return None
    if row["status"] in {"extracting", "purging"} or any(
        row.get(key) is not None for key in (
            "extraction_owner", "extraction_expires_at", "purge_owner", "purge_expires_at",
        )
    ):
        raise ValueError("upload leases must be drained")
    if row["status"] == "purged":
        raise ValueError("purged attempt is outside marker scope")
    marker = row["marker"]
    if isinstance(marker, (str, bytes)):
        marker = json.loads(marker, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    return {"attempt": row["attempt"], "status": row["status"],
            "retention_expires_at": _timestamp(row["retention_expires_at"]),
            "marker": marker}


class _PostgresSession:
    """One serializable transaction against the real tables (0016 migration)."""

    def __init__(self, conn):
        self._conn = conn

    def lock(self, tenant_id, drawing_id):
        self._conn.execute("SELECT pg_advisory_xact_lock(%s)",
                           (advisory_lock_id(tenant_id, drawing_id),))

    def upload_row(self, tenant_id, drawing_id):
        return self._conn.execute(
            "SELECT attempt, marker, status, retention_expires_at, extraction_owner, "
            "extraction_expires_at, purge_owner, purge_expires_at "
            "FROM drawing_upload_attempts WHERE tenant_id = %s AND drawing_id = %s",
            (tenant_id, drawing_id),
        ).fetchone()

    def purge_receipt_count(self, tenant_id, drawing_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM drawing_purge_receipts "
            "WHERE tenant_id = %s AND drawing_id = %s",
            (tenant_id, drawing_id),
        ).fetchone()
        return int(row["n"])

    def drawing_version_ready(self, tenant_id, drawing_id, version):
        return self._conn.execute(
            "SELECT 1 FROM drawing_store_versions WHERE tenant_id = %s AND drawing_id = %s "
            "AND version = %s AND state = 'ready'",
            (tenant_id, drawing_id, version),
        ).fetchone() is not None

    def insert_upload_row(self, tenant_id, drawing_id, row):
        self._conn.execute(
            "INSERT INTO drawing_upload_attempts "
            "(tenant_id, drawing_id, attempt, marker, status, retention_expires_at) "
            "VALUES (%s, %s, %s, CAST(%s AS jsonb), %s, %s)",
            (tenant_id, drawing_id, row["attempt"],
             json.dumps(row["marker"], sort_keys=True, allow_nan=False),
             row["status"], row["retention_expires_at"]),
        )


class PostgresUploadAuthority:
    """Adapter over the platform db handle; the only store that touches PostgreSQL."""

    def __init__(self, db):
        self._db = db

    def assert_ready(self):
        self._db.assert_schema_current()

    @contextlib.contextmanager
    def transaction(self, *, read_only):
        with self._db.transaction(isolation="serializable", read_only=read_only) as conn:
            yield _PostgresSession(conn)


def _default_store():
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("explicit DATABASE_URL required")
    da_dir = str(Path(__file__).resolve().parents[1] / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store
    return PostgresUploadAuthority(store._db())


def reconcile(*, mode, source_dir, tenant_id, drawing_id, scope="markers", apply=False,
              store=None):
    """Compare or import one marker. Every refusal raises before any write commits."""
    if scope in REFUSED_SCOPES:
        raise ValueError("unsupported purge-receipt scope: only markers are reconciled")
    if scope not in SUPPORTED_SCOPES:
        raise ValueError("unsupported scope")
    if mode not in {"backfill", "parity"}:
        raise ValueError("unsupported mode")
    if apply and mode != "backfill":
        raise ValueError("--apply is only valid with --mode backfill")
    tenant_id = _sanitize_id(tenant_id)
    drawing_id = _sanitize_id(drawing_id)
    raw = read_marker_bytes(source_dir, tenant_id, drawing_id)
    source = validate_marker(raw)
    authority = store if store is not None else _default_store()
    authority.assert_ready()
    writes = mode == "backfill" and apply
    inserted = 0
    with authority.transaction(read_only=not writes) as session:
        # Reconciler runs for one identity coordinate on this lock; application
        # writers do not take it, so operators drain them separately.
        session.lock(tenant_id, drawing_id)
        if session.purge_receipt_count(tenant_id, drawing_id):
            raise ValueError("unsupported purge-receipt scope: drawing has purge receipts")
        target = _canonical_target(session.upload_row(tenant_id, drawing_id))
        if source["status"] == "ready" and not session.drawing_version_ready(
            tenant_id, drawing_id, source["marker"]["extracted_version"]
        ):
            raise ValueError("drawing authority must be reconciled before its ready marker")
        if target is not None and target != source:
            raise ValueError("upload marker conflict")
        would_insert = 1 if target is None else 0
        if writes and target is None:
            session.insert_upload_row(tenant_id, drawing_id, source)
            inserted = 1
            target = _canonical_target(session.upload_row(tenant_id, drawing_id))
            if target != source:
                raise ValueError("post-import conflict")
        if read_marker_bytes(source_dir, tenant_id, drawing_id) != raw:
            raise ValueError("source marker changed during reconciliation")
    return {"schema": RECEIPT_SCHEMA, "mode": mode, "scope": scope,
            "dry_run": not writes, "tenant_id": tenant_id, "drawing_id": drawing_id,
            "parity": target == source, "would_insert": would_insert,
            "inserted": inserted, "status": source["status"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("backfill", "parity"), required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--drawing-id", required=True)
    parser.add_argument("--scope", choices=SUPPORTED_SCOPES + REFUSED_SCOPES, default="markers")
    parser.add_argument("--apply", action="store_true",
                        help="write during backfill; without it backfill is a dry run")
    args = parser.parse_args(argv)
    try:
        receipt = reconcile(**vars(args))
    except Exception as exc:
        print(f"upload authority reconciliation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    if receipt["dry_run"] and receipt["mode"] == "backfill":
        return 0
    return 0 if receipt["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
