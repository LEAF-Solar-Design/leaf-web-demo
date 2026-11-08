"""Mutable display positions, scoped to a tenant's document content digest."""
from __future__ import annotations

import json
import math
import re
import time

import lab_artifacts as ledger
from lab_canonical import canonical_bytes
from lab_doc_validators import ID

LAYOUT_SCHEMA = "leaf.lab-layout.v1"
MAX_LAYOUT_BYTES = 32_768
MAX_POSITIONS = 200
COORD_LIMIT = 1_000_000


class LayoutRefused(ValueError):
    def __init__(self, code="lab_layout_invalid"):
        super().__init__(code)
        self.code = code


def validate_layout(layout: dict, block_ids: set[str]) -> bytes:
    if (not isinstance(layout, dict) or set(layout) != {"schema", "positions"}
            or layout["schema"] != LAYOUT_SCHEMA or not isinstance(layout["positions"], dict)):
        raise LayoutRefused()
    positions = layout["positions"]
    if len(positions) > MAX_POSITIONS:
        raise LayoutRefused("lab_layout_too_large")
    for block_id, point in positions.items():
        if not isinstance(block_id, str):
            raise LayoutRefused()
        if block_id not in block_ids:
            raise LayoutRefused("lab_layout_unknown_block")
        if len(block_id) > MAX_LAYOUT_BYTES:
            raise LayoutRefused("lab_layout_too_large")
        if not isinstance(point, dict) or set(point) != {"x", "y"}:
            raise LayoutRefused()
        for value in point.values():
            if (type(value) not in (int, float) or not -COORD_LIMIT <= value <= COORD_LIMIT
                    or not math.isfinite(value)):
                raise LayoutRefused()
    try:
        data = canonical_bytes(layout)
    except (ValueError, UnicodeError):
        raise LayoutRefused() from None
    if len(data) > MAX_LAYOUT_BYTES:
        raise LayoutRefused("lab_layout_too_large")
    return data


def _scope(tenant, doc_id, sha256):
    if (not isinstance(tenant, str) or not 1 <= len(tenant) <= 200
            or not isinstance(doc_id, str) or len(doc_id) > 128 or not ID.fullmatch(doc_id)
            or not isinstance(sha256, str) or len(sha256) != 64 or not re.fullmatch(r"[0-9a-f]{64}", sha256)):
        raise LayoutRefused()


def get_layout(tenant, doc_id, sha256) -> dict | None:
    _scope(tenant, doc_id, sha256)

    def operation(sql):
        row = sql.execute(
            "SELECT l.layout_text FROM lab_layouts l WHERE l.tenant_id=? AND l.doc_id=? AND l.sha256=?"
            " AND EXISTS (SELECT 1 FROM lab_revisions r"
            " WHERE r.tenant_id=l.tenant_id AND r.doc_id=l.doc_id AND r.sha256=l.sha256)",
            (tenant, doc_id, sha256)).fetchone()
        return json.loads(row["layout_text"]) if row else None

    return ledger._run(tenant, operation)


def put_layout(tenant, doc_id, sha256, layout) -> None:
    _scope(tenant, doc_id, sha256)

    def operation(sql):
        row = sql.execute("SELECT revision FROM lab_revisions WHERE tenant_id=? AND doc_id=? AND sha256=? LIMIT 1",
                          (tenant, doc_id, sha256)).fetchone()
        if row is None:
            raise LayoutRefused("lab_layout_unknown_revision")
        revision = ledger._revision(sql, tenant, doc_id, row["revision"])
        data = validate_layout(layout, {block["id"] for block in revision["graph"].get("blocks", [])})
        sql.execute(
            "INSERT INTO lab_layouts (tenant_id,doc_id,sha256,layout_text,byte_count,updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (tenant_id,doc_id,sha256) DO UPDATE SET"
            " layout_text=excluded.layout_text,byte_count=excluded.byte_count,updated_at=excluded.updated_at",
            (tenant, doc_id, sha256, data.decode("utf-8"), len(data), time.time()))

    ledger._run(tenant, operation, write=True)
