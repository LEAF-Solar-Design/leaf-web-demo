"""Data model for the frozen ``leaf.customization.v1`` coordination store."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from uuid import UUID


class ChangeState(str, Enum):
    CREATED = "created"
    STAGING = "staging"
    STAGED = "staged"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


WORKFLOW_STATES = frozenset(ChangeState)
TERMINAL_STATES = frozenset({
    ChangeState.PUBLISHED,
    ChangeState.REJECTED,
    ChangeState.CONFLICTED,
    ChangeState.FAILED,
    ChangeState.SUPERSEDED,
    ChangeState.ROLLED_BACK,
})


class CustomizationStoreError(Exception):
    """Base error for the storage-independent repository interface."""


class ChangeSetNotFoundError(CustomizationStoreError):
    pass


class ChangeSetConflictError(CustomizationStoreError):
    pass


class InvalidTransitionError(CustomizationStoreError):
    pass


class IdempotencyReplayError(CustomizationStoreError):
    """An idempotency key was replayed with a different requested operation."""


@dataclass(frozen=True)
class ChangeSet:
    change_set_id: str
    tenant_id: str
    idempotency_key: str
    state: ChangeState
    version: int
    base_commit: str
    staged_commit: Optional[str]
    catalog_digest: Optional[str]
    desired_platform_release: str
    workspace_contract_digest: str
    author_subject: str
    approver_subject: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EffectiveCatalog:
    tenant_id: str
    change_set_id: str
    catalog_commit: str
    catalog_digest: str
    effective_platform_release: str
    workspace_contract_digest: str
    updated_at: str


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    ts: str
    tenant_id: str
    change_set_id: str
    prior_state: Optional[ChangeState]
    next_state: ChangeState
    author_subject: Optional[str]
    approver_subject: Optional[str]
    base_commit: Optional[str]
    staged_commit: Optional[str]
    catalog_digest: Optional[str]
    platform_release: Optional[str]
    workspace_contract_digest: Optional[str]
    idempotency_key: str
    result: str
    reason_code: Optional[str]


def require_uuid(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("change_set_id must be a UUID") from exc


def require_sha(value: str, length: int, name: str) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError(f"{name} must be a {length}-character lowercase hexadecimal digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a {length}-character lowercase hexadecimal digest") from exc
    if value.lower() != value:
        raise ValueError(f"{name} must be a {length}-character lowercase hexadecimal digest")
    return value


def require_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def require_bounded(value: str, name: str, max_length: int) -> str:
    value = require_nonempty(value, name)
    if len(value) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return value
