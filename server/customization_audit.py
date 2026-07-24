"""Audit projection helpers for ``leaf.customization.audit.v1``.

The coordination store accepts no free-form audit payload.  This module only
projects the frozen receipt fields, which prevents prompts and secret values
from entering the durable audit record.
"""
from __future__ import annotations

from typing import Any, Mapping

from customization_models import AuditEvent, ChangeState


AUDIT_CONTRACT = "leaf.customization.audit.v1"


def audit_payload(event: AuditEvent) -> dict[str, Any]:
    """Return the exact public audit receipt shape, without free-text data."""
    return {
        "contract": AUDIT_CONTRACT,
        "event_id": event.event_id,
        "ts": event.ts,
        "tenant_id": event.tenant_id,
        "change_set_id": event.change_set_id,
        "prior_state": event.prior_state.value if event.prior_state else None,
        "next_state": event.next_state.value,
        "author_subject": event.author_subject,
        "approver_subject": event.approver_subject,
        "base_commit": event.base_commit,
        "staged_commit": event.staged_commit,
        "catalog_digest": event.catalog_digest,
        "platform_release": event.platform_release,
        "workspace_contract_digest": event.workspace_contract_digest,
        "idempotency_key": event.idempotency_key,
        "result": event.result,
        "reason_code": event.reason_code,
    }


def event_from_row(row: Mapping[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        ts=row["created_at"],
        tenant_id=row["tenant_id"],
        change_set_id=row["change_set_id"],
        prior_state=ChangeState(row["prior_state"]) if row["prior_state"] else None,
        next_state=ChangeState(row["next_state"]),
        author_subject=row["author_subject"],
        approver_subject=row["approver_subject"],
        base_commit=row["base_commit"],
        staged_commit=row["staged_commit"],
        catalog_digest=row["catalog_digest"],
        platform_release=row["platform_release"],
        workspace_contract_digest=row["workspace_contract_digest"],
        idempotency_key=row["idempotency_key"],
        result=row["result"],
        reason_code=row["reason_code"],
    )
