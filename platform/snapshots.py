"""Deterministic snapshot import, persistence, and reviewable diff primitives."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping

from psycopg.types.json import Jsonb

from .db import connection
from .models import new_uuid

SNAPSHOT_KINDS = ("catalog", "standards", "ahj")
REVIEW_STATES = ("candidate", "accepted", "advisory")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class SnapshotDraft:
    kind: str
    content: Any
    source_sha256: str
    provenance: Mapping[str, Any]
    review_state: str = "candidate"

    @property
    def content_sha256(self) -> str:
        return sha256(canonical_bytes(self.content))


def draft(kind: str, content: Any, source_bytes: bytes, provenance: Mapping[str, Any],
          *, review_state: str = "candidate") -> SnapshotDraft:
    if kind not in SNAPSHOT_KINDS:
        raise ValueError(f"snapshot kind must be one of {SNAPSHOT_KINDS}")
    if review_state not in REVIEW_STATES:
        raise ValueError(f"review state must be one of {REVIEW_STATES}")
    if not isinstance(provenance, Mapping) or not provenance:
        raise ValueError("snapshot provenance must be a non-empty object")
    canonical_bytes(content)
    return SnapshotDraft(kind, content, sha256(source_bytes), dict(provenance), review_state)


def import_snapshot(item: SnapshotDraft) -> Dict[str, Any]:
    """Idempotently persist a content-addressed immutable snapshot."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_snapshots (snapshot_id, snapshot_kind, content_sha256, "
                "content, source_sha256, provenance, review_state) VALUES "
                "(%(id)s, %(kind)s, %(digest)s, %(content)s, %(source)s, %(provenance)s, %(state)s) "
                "ON CONFLICT (snapshot_kind, content_sha256) DO NOTHING RETURNING *",
                {"id": new_uuid(), "kind": item.kind, "digest": item.content_sha256,
                 "content": Jsonb(item.content), "source": item.source_sha256,
                 "provenance": Jsonb(dict(item.provenance)), "state": item.review_state},
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT * FROM platform_snapshots WHERE snapshot_kind = %(kind)s "
                            "AND content_sha256 = %(digest)s",
                            {"kind": item.kind, "digest": item.content_sha256})
                row = cur.fetchone()
            return dict(row)


def select_channel(kind: str, channel: str, snapshot_id: uuid.UUID, selected_by: str) -> None:
    if kind not in SNAPSHOT_KINDS or not channel.strip() or not selected_by.strip():
        raise ValueError("valid kind, channel, and selector are required")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO snapshot_channels (snapshot_kind, channel, snapshot_id, selected_by) "
                "VALUES (%(kind)s, %(channel)s, %(id)s, %(actor)s) ON CONFLICT "
                "(snapshot_kind, channel) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, "
                "selected_by = EXCLUDED.selected_by, selected_at = NOW()",
                {"kind": kind, "channel": channel, "id": snapshot_id, "actor": selected_by},
            )


def channel_pins(channel: str = "local-candidate") -> Dict[str, Dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.snapshot_kind, s.snapshot_id, s.content_sha256, s.source_sha256, "
                "s.review_state FROM snapshot_channels c JOIN platform_snapshots s "
                "ON s.snapshot_id = c.snapshot_id AND s.snapshot_kind = c.snapshot_kind "
                "WHERE c.channel = %(channel)s ORDER BY s.snapshot_kind", {"channel": channel})
            rows = cur.fetchall()
    pins = {row["snapshot_kind"]: {key: str(value) for key, value in dict(row).items()
                                      if key != "snapshot_kind"} for row in rows}
    missing = set(SNAPSHOT_KINDS) - set(pins)
    if missing:
        raise ValueError(f"snapshot channel {channel!r} is missing {sorted(missing)}")
    return pins


def get_snapshot(snapshot_id: uuid.UUID) -> Dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM platform_snapshots WHERE snapshot_id = %(id)s",
                        {"id": snapshot_id})
            row = cur.fetchone()
    return dict(row) if row else None


def diff(left: Any, right: Any) -> List[Dict[str, Any]]:
    """Return a deterministic JSON-pointer-like structural diff."""
    changes: List[Dict[str, Any]] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if type(a) is not type(b):
            changes.append({"path": path or "/", "before": a, "after": b})
        elif isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                child = f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"
                if key not in a:
                    changes.append({"path": child, "before": None, "after": b[key]})
                elif key not in b:
                    changes.append({"path": child, "before": a[key], "after": None})
                else:
                    walk(a[key], b[key], child)
        elif isinstance(a, list):
            for index in range(max(len(a), len(b))):
                child = f"{path}/{index}"
                if index >= len(a):
                    changes.append({"path": child, "before": None, "after": b[index]})
                elif index >= len(b):
                    changes.append({"path": child, "before": a[index], "after": None})
                else:
                    walk(a[index], b[index], child)
        elif a != b:
            changes.append({"path": path or "/", "before": a, "after": b})

    walk(left, right, "")
    return changes
