"""SQLite repository for durable Leaf customization coordination.

Each operation opens a SQLite connection and uses a database transaction for
correctness.  In-process locks are deliberately not part of this design, so
multiple workers and processes contend through SQLite's transactional CAS.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from customization_audit import audit_payload, event_from_row
from customization_models import (
    AuditEvent,
    ChangeSet,
    ChangeSetConflictError,
    ChangeSetNotFoundError,
    ChangeState,
    EffectiveCatalog,
    IdempotencyReplayError,
    InvalidTransitionError,
    TERMINAL_STATES,
    require_bounded,
    require_nonempty,
    require_sha,
    require_uuid,
)


_STATE_SQL = ", ".join(repr(state.value) for state in ChangeState)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS customization_change_sets (
  change_set_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ({_STATE_SQL})),
  version INTEGER NOT NULL CHECK (version >= 0),
  base_commit TEXT NOT NULL,
  staged_commit TEXT,
  catalog_digest TEXT,
  desired_platform_release TEXT NOT NULL,
  workspace_contract_digest TEXT NOT NULL,
  author_subject TEXT NOT NULL,
  approver_subject TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS effective_catalogs (
  tenant_id TEXT PRIMARY KEY,
  change_set_id TEXT NOT NULL,
  catalog_commit TEXT NOT NULL,
  catalog_digest TEXT NOT NULL,
  effective_platform_release TEXT NOT NULL,
  workspace_contract_digest TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  FOREIGN KEY (change_set_id) REFERENCES customization_change_sets(change_set_id)
);

CREATE TABLE IF NOT EXISTS customization_audit_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  change_set_id TEXT NOT NULL,
  prior_state TEXT CHECK (prior_state IN ({_STATE_SQL}) OR prior_state IS NULL),
  next_state TEXT NOT NULL CHECK (next_state IN ({_STATE_SQL})),
  author_subject TEXT,
  approver_subject TEXT,
  base_commit TEXT,
  staged_commit TEXT,
  catalog_digest TEXT,
  platform_release TEXT,
  workspace_contract_digest TEXT,
  idempotency_key TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('ok', 'denied', 'failed')),
  reason_code TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  FOREIGN KEY (change_set_id) REFERENCES customization_change_sets(change_set_id),
  UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS customization_recovery_idx
  ON customization_change_sets (state, tenant_id);

CREATE TABLE IF NOT EXISTS customization_confirmations (
  confirmation_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  signature TEXT NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS customization_deployment_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  effective_catalog_digest TEXT NOT NULL,
  platform_release TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (idempotency_key)
);

CREATE TABLE IF NOT EXISTS customization_deployment_audit (
  audit_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('snapshot', 'verify', 'restore', 'restore_verify')),
  result TEXT NOT NULL CHECK (result IN ('ok', 'failed')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  FOREIGN KEY (snapshot_id) REFERENCES customization_deployment_snapshots(snapshot_id)
);
"""

_NEXT = {
    ChangeState.CREATED: {ChangeState.STAGING},
    ChangeState.STAGING: {ChangeState.STAGED},
    ChangeState.STAGED: {ChangeState.AWAITING_APPROVAL},
    ChangeState.AWAITING_APPROVAL: {ChangeState.APPROVED},
    ChangeState.APPROVED: {ChangeState.PUBLISHING},
    ChangeState.PUBLISHING: {ChangeState.PUBLISHED},
    ChangeState.PUBLISHED: {ChangeState.ROLLED_BACK},
}


class CustomizationRepository:
    """Storage-independent interface implemented by ``SQLiteCustomizationStore``."""

    def create_change_set(self, **kwargs: object) -> ChangeSet:
        raise NotImplementedError

    def transition(self, **kwargs: object) -> ChangeSet:
        raise NotImplementedError

    def publish(self, **kwargs: object) -> EffectiveCatalog:
        raise NotImplementedError


class SQLiteCustomizationStore(CustomizationRepository):
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def initialize(self) -> None:
        """Create the schema safely on every startup."""
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

    ensure_started = initialize

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def create_change_set(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        base_commit: str,
        desired_platform_release: str,
        workspace_contract_digest: str,
        author_subject: str,
        change_set_id: Optional[str] = None,
    ) -> ChangeSet:
        tenant_id = require_bounded(tenant_id, "tenant_id", 200)
        idempotency_key = require_bounded(idempotency_key, "idempotency_key", 200)
        base_commit = require_sha(base_commit, 40, "base_commit")
        workspace_contract_digest = require_sha(
            workspace_contract_digest, 64, "workspace_contract_digest"
        )
        desired_platform_release = require_bounded(
            desired_platform_release, "desired_platform_release", 200
        )
        author_subject = require_bounded(author_subject, "author_subject", 300)
        change_set_id = require_uuid(change_set_id or str(uuid4()))
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM customization_change_sets WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                row = self._change_set(existing)
                same_request = (
                    row.base_commit == base_commit
                    and row.desired_platform_release == desired_platform_release
                    and row.workspace_contract_digest == workspace_contract_digest
                    and row.author_subject == author_subject
                )
                if not same_request:
                    raise IdempotencyReplayError("idempotency key belongs to a different change-set request")
                return row
            conn.execute(
                "INSERT INTO customization_change_sets "
                "(change_set_id, tenant_id, idempotency_key, state, version, base_commit, "
                "desired_platform_release, workspace_contract_digest, author_subject) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (change_set_id, tenant_id, idempotency_key, ChangeState.CREATED.value,
                 base_commit, desired_platform_release, workspace_contract_digest, author_subject),
            )
            row = self._find_change_set(conn, tenant_id, change_set_id)
            self._append_audit(conn, row, None, ChangeState.CREATED, idempotency_key)
            return row

    def get_change_set(self, *, tenant_id: str, change_set_id: str) -> ChangeSet:
        self.initialize()
        with self._connection() as conn:
            return self._find_change_set(conn, tenant_id, change_set_id)

    def get_change_set_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ChangeSet:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM customization_change_sets "
                "WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise ChangeSetNotFoundError("change set was not found")
            return self._change_set(row)

    def transition(
        self,
        *,
        tenant_id: str,
        change_set_id: str,
        next_state: ChangeState | str,
        expected_version: int,
        idempotency_key: str,
        expected_state: ChangeState | str | None = None,
        approver_subject: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> ChangeSet:
        return self._transition(
            tenant_id=tenant_id,
            change_set_id=change_set_id,
            next_state=next_state,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            expected_state=expected_state,
            approver_subject=approver_subject,
            reason_code=reason_code,
        )

    def record_staged(
        self,
        *,
        tenant_id: str,
        change_set_id: str,
        expected_version: int,
        idempotency_key: str,
        staged_commit: str,
        catalog_digest: str,
        platform_release: str,
        workspace_contract_digest: str,
    ) -> ChangeSet:
        staged_commit = require_sha(staged_commit, 40, "staged_commit")
        catalog_digest = require_sha(catalog_digest, 64, "catalog_digest")
        platform_release = require_bounded(platform_release, "platform_release", 200)
        workspace_contract_digest = require_sha(
            workspace_contract_digest, 64, "workspace_contract_digest"
        )
        with self._transaction() as conn:
            row = self._find_change_set(conn, tenant_id, change_set_id)
            if row.workspace_contract_digest != workspace_contract_digest:
                raise ChangeSetConflictError("workspace contract digest does not match the reserved change set")
            if row.desired_platform_release != platform_release:
                raise ChangeSetConflictError("platform release does not match the reserved change set")
            return self._transition_row(
                conn, row, ChangeState.STAGED, expected_version, idempotency_key,
                staged_commit=staged_commit, catalog_digest=catalog_digest,
            )

    def publish(
        self,
        *,
        tenant_id: str,
        change_set_id: str,
        expected_version: int,
        idempotency_key: str,
        approver_subject: Optional[str] = None,
    ) -> EffectiveCatalog:
        """Publish a ``publishing`` row and its catalog pointer in one transaction."""
        with self._transaction() as conn:
            row = self._find_change_set(conn, tenant_id, change_set_id)
            published = self._transition_row(
                conn, row, ChangeState.PUBLISHED, expected_version, idempotency_key,
                approver_subject=approver_subject,
            )
            if not published.staged_commit or not published.catalog_digest:
                raise ChangeSetConflictError("a published change set requires a staged commit and catalog digest")
            conn.execute(
                "INSERT INTO effective_catalogs "
                "(tenant_id, change_set_id, catalog_commit, catalog_digest, "
                "effective_platform_release, workspace_contract_digest) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET "
                "change_set_id=excluded.change_set_id, catalog_commit=excluded.catalog_commit, "
                "catalog_digest=excluded.catalog_digest, "
                "effective_platform_release=excluded.effective_platform_release, "
                "workspace_contract_digest=excluded.workspace_contract_digest, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (tenant_id, published.change_set_id, published.staged_commit,
                 published.catalog_digest, published.desired_platform_release,
                 published.workspace_contract_digest),
            )
            return self._effective_catalog(conn, tenant_id)

    def get_effective_catalog(self, *, tenant_id: str) -> EffectiveCatalog:
        self.initialize()
        with self._connection() as conn:
            return self._effective_catalog(conn, tenant_id)

    # The confirmation rows are deliberately owned by this same SQLite file as
    # state and audit. ``consume`` is a durable compare-and-set, not an
    # in-process lock, so a retry or a second worker cannot reuse approval.
    def put_confirmation(self, *, confirmation_id: str, payload: dict, signature: str) -> None:
        with self._transaction() as conn:
            try:
                conn.execute("INSERT INTO customization_confirmations "
                             "(confirmation_id, payload_json, signature) VALUES (?, ?, ?)",
                             (confirmation_id, json.dumps(payload, sort_keys=True, separators=(",", ":")), signature))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyReplayError("confirmation already exists") from exc

    def get_confirmation(self, *, confirmation_id: str) -> dict | None:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute("SELECT payload_json, signature, consumed FROM customization_confirmations "
                               "WHERE confirmation_id = ?", (confirmation_id,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        return {"payload": payload, "signature": row["signature"], "consumed": bool(row["consumed"])}

    def consume_confirmation(self, *, confirmation_id: str, signature: str) -> bool:
        with self._transaction() as conn:
            result = conn.execute("UPDATE customization_confirmations SET consumed = 1 "
                                  "WHERE confirmation_id = ? AND signature = ? AND consumed = 0",
                                  (confirmation_id, signature))
            return result.rowcount == 1

    def find_unconsumed_confirmation(
        self, *, tenant_id: str, change_set_id: str
    ) -> dict | None:
        """Return an already-issued independent approval, never mint one."""
        self.initialize()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT confirmation_id, payload_json, signature, consumed "
                "FROM customization_confirmations WHERE consumed = 0 ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (payload.get("tenant_id"), payload.get("change_set_id")) == (
                tenant_id, change_set_id
            ):
                return {
                    "confirmation_id": row["confirmation_id"],
                    "payload": payload,
                    "signature": row["signature"],
                    "consumed": False,
                }
        return None

    def prepare_publish(
        self,
        *,
        tenant_id: str,
        change_set_id: str,
        confirmation_id: str,
        confirmation_signature: str,
        approver_subject: str,
        idempotency_key: str,
    ) -> ChangeSet:
        """Consume approval and advance to ``publishing`` in one transaction."""
        with self._transaction() as conn:
            row = self._find_change_set(conn, tenant_id, change_set_id)
            if row.state is ChangeState.PUBLISHING:
                confirmation = conn.execute(
                    "SELECT consumed, signature FROM customization_confirmations "
                    "WHERE confirmation_id = ?",
                    (confirmation_id,),
                ).fetchone()
                replay = conn.execute(
                    "SELECT 1 FROM customization_audit_events "
                    "WHERE tenant_id = ? AND change_set_id = ? AND idempotency_key = ? "
                    "AND next_state = ?",
                    (tenant_id, change_set_id, f"publishing:{idempotency_key}",
                     ChangeState.PUBLISHING.value),
                ).fetchone()
                if (confirmation is not None and confirmation["consumed"] == 1
                        and hmac.compare_digest(confirmation["signature"], confirmation_signature)
                        and row.approver_subject == approver_subject
                        and replay is not None):
                    return row
                raise ChangeSetConflictError("publishing state does not match this confirmation")
            if row.state is not ChangeState.STAGED:
                raise ChangeSetConflictError("change set is not ready for approval")
            other_publish = conn.execute(
                "SELECT change_set_id FROM customization_change_sets "
                "WHERE tenant_id = ? AND state = ? AND change_set_id != ? LIMIT 1",
                (tenant_id, ChangeState.PUBLISHING.value, change_set_id),
            ).fetchone()
            if other_publish is not None:
                raise ChangeSetConflictError(
                    "another publish for this tenant requires recovery first"
                )
            consumed = conn.execute(
                "UPDATE customization_confirmations SET consumed = 1 "
                "WHERE confirmation_id = ? AND signature = ? AND consumed = 0",
                (confirmation_id, confirmation_signature),
            )
            if consumed.rowcount != 1:
                raise IdempotencyReplayError("confirmation was already consumed")
            awaiting = self._transition_row(
                conn, row, ChangeState.AWAITING_APPROVAL, row.version,
                f"awaiting:{idempotency_key}", expected_state=ChangeState.STAGED,
            )
            approved = self._transition_row(
                conn, awaiting, ChangeState.APPROVED, awaiting.version,
                f"approved:{idempotency_key}",
                expected_state=ChangeState.AWAITING_APPROVAL,
                approver_subject=approver_subject,
            )
            return self._transition_row(
                conn, approved, ChangeState.PUBLISHING, approved.version,
                f"publishing:{idempotency_key}", expected_state=ChangeState.APPROVED,
            )

    @staticmethod
    def _catalog_snapshot(conn: sqlite3.Connection) -> tuple[list[dict], str]:
        rows = conn.execute(
            "SELECT tenant_id, change_set_id, catalog_commit, catalog_digest, "
            "effective_platform_release, workspace_contract_digest "
            "FROM effective_catalogs ORDER BY tenant_id"
        ).fetchall()
        payload = [dict(row) for row in rows]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return payload, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _deployment_audit(
        conn: sqlite3.Connection, *, snapshot_id: str, action: str,
        result: str, idempotency_key: str,
    ) -> str:
        old = conn.execute(
            "SELECT audit_id, snapshot_id, action, result "
            "FROM customization_deployment_audit WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if old is not None:
            if (old["snapshot_id"], old["action"], old["result"]) != (
                snapshot_id, action, result
            ):
                raise IdempotencyReplayError(
                    "deployment audit idempotency key belongs to another action"
                )
            return old["audit_id"]
        audit_id = str(uuid4())
        conn.execute(
            "INSERT INTO customization_deployment_audit "
            "(audit_id, snapshot_id, action, result, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (audit_id, snapshot_id, action, result, idempotency_key),
        )
        return audit_id

    def capture_deployment_snapshot(
        self, *, platform_release: str, idempotency_key: str
    ) -> dict:
        """Atomically capture every tenant's effective catalog pointer."""
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT * FROM customization_deployment_snapshots "
                "WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if old is not None:
                snapshot = dict(old)
            else:
                payload, digest = self._catalog_snapshot(conn)
                snapshot_id = str(uuid4())
                conn.execute(
                    "INSERT INTO customization_deployment_snapshots "
                    "(snapshot_id, payload_json, effective_catalog_digest, "
                    "platform_release, idempotency_key) VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, json.dumps(payload, sort_keys=True, separators=(",", ":")),
                     digest, platform_release, idempotency_key),
                )
                snapshot = dict(conn.execute(
                    "SELECT * FROM customization_deployment_snapshots "
                    "WHERE snapshot_id = ?", (snapshot_id,)
                ).fetchone())
            snapshot["audit_id"] = self._deployment_audit(
                conn, snapshot_id=snapshot["snapshot_id"], action="snapshot",
                result="ok", idempotency_key=f"audit:{idempotency_key}",
            )
            return snapshot

    def get_deployment_snapshot(self, *, snapshot_id: str) -> dict:
        self.initialize()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM customization_deployment_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise ChangeSetNotFoundError("deployment snapshot was not found")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def effective_catalog_snapshot(self) -> dict:
        self.initialize()
        with self._connection() as conn:
            payload, digest = self._catalog_snapshot(conn)
        return {"catalogs": payload, "digest": digest}

    def verify_deployment_snapshot(
        self, *, snapshot_id: str, action: str, idempotency_key: str
    ) -> dict:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM customization_deployment_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise ChangeSetNotFoundError("deployment snapshot was not found")
            _, digest = self._catalog_snapshot(conn)
            ok = hmac.compare_digest(digest, row["effective_catalog_digest"])
            audit_id = self._deployment_audit(
                conn, snapshot_id=snapshot_id, action=action,
                result="ok" if ok else "failed", idempotency_key=idempotency_key,
            )
            return {
                "snapshot_id": snapshot_id,
                "effective_catalog_digest": digest,
                "expected_effective_catalog_digest": row["effective_catalog_digest"],
                "verified": ok,
                "audit_id": audit_id,
            }

    def restore_deployment_snapshot(
        self, *, snapshot_id: str, idempotency_key: str
    ) -> dict:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM customization_deployment_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise ChangeSetNotFoundError("deployment snapshot was not found")
            payload = json.loads(row["payload_json"])
            conn.execute("DELETE FROM effective_catalogs")
            for item in payload:
                conn.execute(
                    "INSERT INTO effective_catalogs "
                    "(tenant_id, change_set_id, catalog_commit, catalog_digest, "
                    "effective_platform_release, workspace_contract_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item["tenant_id"], item["change_set_id"], item["catalog_commit"],
                     item["catalog_digest"], item["effective_platform_release"],
                     item["workspace_contract_digest"]),
                )
            _, digest = self._catalog_snapshot(conn)
            if not hmac.compare_digest(digest, row["effective_catalog_digest"]):
                raise ChangeSetConflictError("restored catalog digest does not match snapshot")
            audit_id = self._deployment_audit(
                conn, snapshot_id=snapshot_id, action="restore", result="ok",
                idempotency_key=idempotency_key,
            )
            return {
                "snapshot_id": snapshot_id,
                "effective_catalog_digest": digest,
                "platform_release": row["platform_release"],
                "audit_id": audit_id,
            }

    def restore_effective_catalog(self, *, tenant_id: str, target_change_set_id: str,
                                  prior_change_set_id: str, idempotency_key: str) -> EffectiveCatalog:
        """Restore a published pin and append one idempotent rollback audit."""
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM customization_audit_events WHERE tenant_id = ? AND idempotency_key = ?",
                                    (tenant_id, idempotency_key)).fetchone()
            if existing is not None:
                event = event_from_row(existing)
                if event.next_state is ChangeState.ROLLED_BACK:
                    return self._effective_catalog(conn, tenant_id)
                raise IdempotencyReplayError("idempotency key belongs to a different transition")
            target = self._find_change_set(conn, tenant_id, target_change_set_id)
            if target.state is not ChangeState.PUBLISHED or not target.staged_commit or not target.catalog_digest:
                raise ChangeSetConflictError("rollback target must be published")
            prior = self._find_change_set(conn, tenant_id, prior_change_set_id)
            conn.execute("INSERT INTO effective_catalogs "
                         "(tenant_id, change_set_id, catalog_commit, catalog_digest, effective_platform_release, workspace_contract_digest) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET "
                         "change_set_id=excluded.change_set_id, catalog_commit=excluded.catalog_commit, "
                         "catalog_digest=excluded.catalog_digest, effective_platform_release=excluded.effective_platform_release, "
                         "workspace_contract_digest=excluded.workspace_contract_digest, "
                         "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                         (tenant_id, target.change_set_id, target.staged_commit, target.catalog_digest,
                          target.desired_platform_release, target.workspace_contract_digest))
            self._append_audit(conn, prior, prior.state, ChangeState.ROLLED_BACK, idempotency_key,
                               reason_code="effective_catalog_restored")
            return self._effective_catalog(conn, tenant_id)

    def recovery_candidates(self, *, tenant_id: Optional[str] = None) -> list[ChangeSet]:
        """Return rows interrupted between reserve/stage or publish completion."""
        self.initialize()
        with self._connection() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT * FROM customization_change_sets WHERE state IN (?, ?) "
                    "ORDER BY created_at, change_set_id",
                    (ChangeState.STAGING.value, ChangeState.PUBLISHING.value),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM customization_change_sets WHERE tenant_id = ? AND state IN (?, ?) "
                    "ORDER BY created_at, change_set_id",
                    (tenant_id, ChangeState.STAGING.value, ChangeState.PUBLISHING.value),
                ).fetchall()
            return [self._change_set(row) for row in rows]

    def audit_events(self, *, tenant_id: str, change_set_id: Optional[str] = None) -> list[AuditEvent]:
        self.initialize()
        with self._connection() as conn:
            if change_set_id:
                rows = conn.execute(
                    "SELECT * FROM customization_audit_events WHERE tenant_id = ? AND change_set_id = ? "
                    "ORDER BY created_at, event_id", (tenant_id, change_set_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM customization_audit_events WHERE tenant_id = ? ORDER BY created_at, event_id",
                    (tenant_id,),
                ).fetchall()
            return [event_from_row(row) for row in rows]

    def _transition(self, **kwargs: object) -> ChangeSet:
        with self._transaction() as conn:
            row = self._find_change_set(conn, str(kwargs["tenant_id"]), str(kwargs["change_set_id"]))
            return self._transition_row(
                conn, row, ChangeState(kwargs["next_state"]), int(kwargs["expected_version"]),
                str(kwargs["idempotency_key"]),
                expected_state=(ChangeState(kwargs["expected_state"])
                                if kwargs.get("expected_state") is not None else None),
                approver_subject=kwargs.get("approver_subject") or None,
                reason_code=kwargs.get("reason_code") or None,
            )

    def _transition_row(
        self, conn: sqlite3.Connection, row: ChangeSet, next_state: ChangeState,
        expected_version: int, idempotency_key: str, *, expected_state: Optional[ChangeState] = None,
        approver_subject: Optional[str] = None,
        reason_code: Optional[str] = None, staged_commit: Optional[str] = None,
        catalog_digest: Optional[str] = None,
    ) -> ChangeSet:
        idempotency_key = require_bounded(idempotency_key, "idempotency_key", 200)
        if reason_code is not None:
            reason_code = require_bounded(reason_code, "reason_code", 200)
        existing_event = conn.execute(
            "SELECT * FROM customization_audit_events WHERE tenant_id = ? AND idempotency_key = ?",
            (row.tenant_id, idempotency_key),
        ).fetchone()
        if existing_event is not None:
            event = event_from_row(existing_event)
            if event.change_set_id == row.change_set_id and event.next_state == next_state:
                return self._find_change_set(conn, row.tenant_id, row.change_set_id)
            raise IdempotencyReplayError("idempotency key belongs to a different transition")
        if row.version != expected_version:
            raise ChangeSetConflictError("change set version does not match expected_version")
        if expected_state is not None and row.state != expected_state:
            raise ChangeSetConflictError("change set state does not match expected_state")
        permitted = _NEXT.get(row.state, set())
        may_end = next_state in TERMINAL_STATES and next_state != ChangeState.ROLLED_BACK
        if next_state not in permitted and not (row.state not in TERMINAL_STATES and may_end):
            raise InvalidTransitionError(f"cannot transition from {row.state.value} to {next_state.value}")
        if next_state == ChangeState.APPROVED and not approver_subject:
            raise ValueError("approver_subject is required for approval")
        if approver_subject is not None:
            approver_subject = require_bounded(approver_subject, "approver_subject", 300)
        if next_state == ChangeState.APPROVED and approver_subject == row.author_subject:
            raise ChangeSetConflictError("author and approver subjects must differ")
        updated = conn.execute(
            "UPDATE customization_change_sets SET state = ?, version = version + 1, "
            "staged_commit = COALESCE(?, staged_commit), catalog_digest = COALESCE(?, catalog_digest), "
            "approver_subject = COALESCE(?, approver_subject), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE change_set_id = ? AND tenant_id = ? AND version = ?",
            (next_state.value, staged_commit, catalog_digest, approver_subject,
             row.change_set_id, row.tenant_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ChangeSetConflictError("change set was changed by another writer")
        result = self._find_change_set(conn, row.tenant_id, row.change_set_id)
        self._append_audit(conn, result, row.state, next_state, idempotency_key, reason_code)
        return result

    def _append_audit(
        self, conn: sqlite3.Connection, row: ChangeSet, prior_state: Optional[ChangeState],
        next_state: ChangeState, idempotency_key: str, reason_code: Optional[str] = None,
    ) -> None:
        event_id = str(uuid4())
        audit_result = (
            "denied" if next_state is ChangeState.REJECTED
            else "failed" if next_state in {ChangeState.CONFLICTED, ChangeState.FAILED}
            else "ok"
        )
        event = AuditEvent(
            event_id=event_id, ts="", tenant_id=row.tenant_id, change_set_id=row.change_set_id,
            prior_state=prior_state, next_state=next_state, author_subject=row.author_subject,
            approver_subject=row.approver_subject, base_commit=row.base_commit,
            staged_commit=row.staged_commit, catalog_digest=row.catalog_digest,
            platform_release=row.desired_platform_release,
            workspace_contract_digest=row.workspace_contract_digest,
            idempotency_key=idempotency_key, result=audit_result, reason_code=reason_code,
        )
        conn.execute(
            "INSERT INTO customization_audit_events "
            "(event_id, tenant_id, change_set_id, prior_state, next_state, author_subject, "
            "approver_subject, base_commit, staged_commit, catalog_digest, platform_release, "
            "workspace_contract_digest, idempotency_key, result, reason_code, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, row.tenant_id, row.change_set_id,
             prior_state.value if prior_state else None, next_state.value,
             row.author_subject, row.approver_subject, row.base_commit, row.staged_commit,
             row.catalog_digest, row.desired_platform_release, row.workspace_contract_digest,
             idempotency_key, audit_result, reason_code, "{}"),
        )
        timestamp = conn.execute(
            "SELECT created_at FROM customization_audit_events WHERE event_id = ?", (event_id,)
        ).fetchone()["created_at"]
        payload = audit_payload(AuditEvent(
            event_id=event.event_id, ts=timestamp, tenant_id=event.tenant_id,
            change_set_id=event.change_set_id, prior_state=event.prior_state,
            next_state=event.next_state, author_subject=event.author_subject,
            approver_subject=event.approver_subject, base_commit=event.base_commit,
            staged_commit=event.staged_commit, catalog_digest=event.catalog_digest,
            platform_release=event.platform_release,
            workspace_contract_digest=event.workspace_contract_digest,
            idempotency_key=event.idempotency_key, result=event.result,
            reason_code=event.reason_code,
        ))
        conn.execute(
            "UPDATE customization_audit_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True), event_id),
        )

    def _find_change_set(self, conn: sqlite3.Connection, tenant_id: str, change_set_id: str) -> ChangeSet:
        row = conn.execute(
            "SELECT * FROM customization_change_sets WHERE tenant_id = ? AND change_set_id = ?",
            (tenant_id, change_set_id),
        ).fetchone()
        if row is None:
            raise ChangeSetNotFoundError("change set was not found")
        return self._change_set(row)

    @staticmethod
    def _change_set(row: sqlite3.Row) -> ChangeSet:
        return ChangeSet(
            change_set_id=row["change_set_id"], tenant_id=row["tenant_id"],
            idempotency_key=row["idempotency_key"], state=ChangeState(row["state"]),
            version=row["version"], base_commit=row["base_commit"],
            staged_commit=row["staged_commit"], catalog_digest=row["catalog_digest"],
            desired_platform_release=row["desired_platform_release"],
            workspace_contract_digest=row["workspace_contract_digest"],
            author_subject=row["author_subject"], approver_subject=row["approver_subject"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _effective_catalog(conn: sqlite3.Connection, tenant_id: str) -> EffectiveCatalog:
        row = conn.execute("SELECT * FROM effective_catalogs WHERE tenant_id = ?", (tenant_id,)).fetchone()
        if row is None:
            raise ChangeSetNotFoundError("effective catalog was not found")
        return EffectiveCatalog(
            tenant_id=row["tenant_id"], change_set_id=row["change_set_id"],
            catalog_commit=row["catalog_commit"], catalog_digest=row["catalog_digest"],
            effective_platform_release=row["effective_platform_release"],
            workspace_contract_digest=row["workspace_contract_digest"], updated_at=row["updated_at"],
        )
