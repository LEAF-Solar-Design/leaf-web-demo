"""Trusted, transport-free authority checks for customization staging and R6.

This module is deliberately independent of HTTP, prompt handling, and durable
storage.  Callers must pass bindings produced by their verified identity and
entitlement layers.  A plain request, prompt, or tenant-role string is never an
authority input to this service.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4


TENANT_ROLES = frozenset({"owner", "editor", "reviewer", "read_only"})
_STAGE_ROLES = frozenset({"owner", "editor"})
_APPROVAL_ROLES = frozenset({"owner", "reviewer"})


class AuthorityError(PermissionError):
    """A fail-closed authority denial with a stable, non-sensitive reason."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class TenantBinding:
    """A binding emitted by the existing, verified tenant identity layer."""

    tenant_id: str | None
    subject: str | None
    role: str | None
    verified: bool


@dataclass(frozen=True)
class BuilderEntitlement:
    """A verified builder capability.  It is not a tenant role."""

    tenant_id: str | None
    subject: str | None
    enabled: bool
    verified: bool


@dataclass(frozen=True)
class StaffAuthority:
    """Separate staff authority, never inferred from a tenant role."""

    subject: str | None
    operator: bool
    verified: bool


@dataclass(frozen=True)
class StageAuthorization:
    """The minimum trusted result needed by a staging coordinator."""

    tenant_id: str
    author_subject: str


@dataclass(frozen=True)
class StagedChange:
    """Immutable staged fields to which an R6 confirmation is bound."""

    tenant_id: str
    change_set_id: str
    staged_commit: str
    catalog_digest: str
    platform_release: str
    workspace_contract_digest: str
    author_subject: str
    high_impact: bool = False


@dataclass(frozen=True)
class PublishRequest:
    """The R6 request fields other than its confirmation and idempotency key."""

    change_set_id: str
    staged_commit: str
    catalog_digest: str
    platform_release: str
    workspace_contract_digest: str


@dataclass(frozen=True)
class PublishConfirmation:
    """Public confirmation receipt.  It intentionally carries no signing key."""

    confirmation_id: str
    tenant_id: str
    change_set_id: str
    staged_commit: str
    catalog_digest: str
    platform_release: str
    workspace_contract_digest: str
    author_subject: str
    approver_subject: str
    expires_at: datetime


class ConfirmationSigner(Protocol):
    """Server-injected signer for confirmation payloads."""

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


class HmacConfirmationSigner:
    """HMAC-SHA256 signer whose key stays in server configuration."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("confirmation signing secret is required")
        self._secret = secret

    def sign(self, payload: bytes) -> str:
        digest = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii")

    def verify(self, payload: bytes, signature: str) -> bool:
        if not isinstance(signature, str):
            return False
        return hmac.compare_digest(self.sign(payload), signature)


@dataclass
class _ConfirmationRecord:
    payload: dict[str, Any]
    signature: str
    consumed: bool = False


class ConfirmationRepository(Protocol):
    """Durable implementations must make ``consume`` an atomic compare-and-set."""

    def put(self, confirmation_id: str, payload: dict[str, Any], signature: str) -> None: ...

    def get(self, confirmation_id: str) -> _ConfirmationRecord | None: ...

    def consume(self, confirmation_id: str, signature: str) -> bool: ...


class InMemoryConfirmationRepository:
    """Thread-safe test and demo store. Production wiring injects durable storage."""

    def __init__(self) -> None:
        self._records: dict[str, _ConfirmationRecord] = {}
        self._lock = threading.Lock()

    def put(self, confirmation_id: str, payload: dict[str, Any], signature: str) -> None:
        with self._lock:
            if confirmation_id in self._records:
                raise AuthorityError("confirmation_conflict")
            self._records[confirmation_id] = _ConfirmationRecord(deepcopy(payload), signature)

    def get(self, confirmation_id: str) -> _ConfirmationRecord | None:
        with self._lock:
            record = self._records.get(confirmation_id)
            return deepcopy(record) if record is not None else None

    def consume(self, confirmation_id: str, signature: str) -> bool:
        with self._lock:
            record = self._records.get(confirmation_id)
            if record is None or record.consumed or record.signature != signature:
                return False
            record.consumed = True
            return True


class CustomizationAuthority:
    """Authorize staging and create or consume exact, one-time R6 approvals."""

    def __init__(
        self,
        signer: ConfirmationSigner,
        *,
        confirmations: ConfirmationRepository | None = None,
        now: Any = None,
    ) -> None:
        if not callable(getattr(signer, "sign", None)) or not callable(getattr(signer, "verify", None)):
            raise TypeError("signer must provide sign and verify")
        self._signer = signer
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._confirmations = confirmations or InMemoryConfirmationRepository()

    def authorize_stage(
        self, *, binding: TenantBinding, builder_entitlement: BuilderEntitlement
    ) -> StageAuthorization:
        """Require a verified owner/editor binding and its builder capability."""
        tenant_id, subject, role = _verified_binding(binding)
        if role not in _STAGE_ROLES:
            raise AuthorityError("stage_role_denied")
        if not isinstance(builder_entitlement, BuilderEntitlement):
            raise AuthorityError("builder_entitlement_missing")
        if not builder_entitlement.verified or builder_entitlement.enabled is not True:
            raise AuthorityError("builder_entitlement_missing")
        if (builder_entitlement.tenant_id, builder_entitlement.subject) != (tenant_id, subject):
            raise AuthorityError("builder_entitlement_mismatch")
        return StageAuthorization(tenant_id=tenant_id, author_subject=subject)

    def issue_publish_confirmation(
        self,
        *,
        staged_change: StagedChange,
        author_binding: TenantBinding,
        approver_binding: TenantBinding | None = None,
        staff_authority: StaffAuthority | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> PublishConfirmation:
        """Create an always-required R6 confirmation bound to one staged change."""
        staged = _validated_staged_change(staged_change)
        author_tenant, author_subject, _ = _verified_binding(author_binding)
        if (author_tenant, author_subject) != (staged.tenant_id, staged.author_subject):
            raise AuthorityError("author_binding_mismatch")
        approver_subject = self._authorize_approver(
            tenant_id=staged.tenant_id,
            approver_binding=approver_binding,
            staff_authority=staff_authority,
        )
        if staged.high_impact and author_subject == approver_subject:
            raise AuthorityError("high_impact_self_approval")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be positive")

        issued_at = _utc(self._now())
        expires_at = issued_at + ttl
        confirmation_id = str(uuid4())
        payload = {
            "confirmation_id": confirmation_id,
            "tenant_id": staged.tenant_id,
            "change_set_id": staged.change_set_id,
            "staged_commit": staged.staged_commit,
            "catalog_digest": staged.catalog_digest,
            "platform_release": staged.platform_release,
            "workspace_contract_digest": staged.workspace_contract_digest,
            "author_subject": author_subject,
            "approver_subject": approver_subject,
            "expires_at": expires_at.isoformat(),
        }
        self._confirmations.put(
            confirmation_id,
            payload,
            self._signer.sign(_canonical_payload(payload)),
        )
        return _confirmation_from_payload(payload)

    def consume_publish_confirmation(
        self,
        *,
        tenant_id: str,
        request: PublishRequest,
        confirmation_id: str,
    ) -> PublishConfirmation:
        """Verify and consume the exact R6 approval once, before publication."""
        _required_text(tenant_id, "tenant_id", 200)
        _validated_request(request)
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise AuthorityError("confirmation_missing")
        record = self._confirmations.get(confirmation_id)
        if record is None:
            raise AuthorityError("confirmation_not_found")
        payload = record.payload
        if not self._signer.verify(_canonical_payload(payload), record.signature):
            raise AuthorityError("confirmation_tampered")
        if record.consumed:
            raise AuthorityError("confirmation_replayed")
        expires_at = _parse_expiry(payload.get("expires_at"))
        if _utc(self._now()) >= expires_at:
            raise AuthorityError("confirmation_expired")
        expected = {
            "tenant_id": tenant_id,
            "change_set_id": request.change_set_id,
            "staged_commit": request.staged_commit,
            "catalog_digest": request.catalog_digest,
            "platform_release": request.platform_release,
            "workspace_contract_digest": request.workspace_contract_digest,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise AuthorityError("confirmation_binding_mismatch")
        if not self._confirmations.consume(confirmation_id, record.signature):
            raise AuthorityError("confirmation_replayed")
        return _confirmation_from_payload(payload)

    def _authorize_approver(
        self,
        *,
        tenant_id: str,
        approver_binding: TenantBinding | None,
        staff_authority: StaffAuthority | None,
    ) -> str:
        if (approver_binding is None) == (staff_authority is None):
            raise AuthorityError("approver_authority_missing")
        if approver_binding is not None:
            binding_tenant, subject, role = _verified_binding(approver_binding)
            if binding_tenant != tenant_id:
                raise AuthorityError("approver_cross_tenant")
            if role not in _APPROVAL_ROLES:
                raise AuthorityError("approver_role_denied")
            return subject
        if not isinstance(staff_authority, StaffAuthority):
            raise AuthorityError("staff_authority_missing")
        if not staff_authority.verified or staff_authority.operator is not True:
            raise AuthorityError("staff_authority_missing")
        return _required_text(staff_authority.subject, "staff_subject", 300)


def _verified_binding(binding: TenantBinding) -> tuple[str, str, str]:
    if not isinstance(binding, TenantBinding) or binding.verified is not True:
        raise AuthorityError("tenant_binding_unverified")
    tenant_id = _required_text(binding.tenant_id, "tenant_id", 200)
    subject = _required_text(binding.subject, "subject", 300)
    role = binding.role
    if role not in TENANT_ROLES:
        raise AuthorityError("tenant_role_invalid")
    return tenant_id, subject, role


def _validated_staged_change(staged: StagedChange) -> StagedChange:
    if not isinstance(staged, StagedChange):
        raise AuthorityError("staged_change_invalid")
    _required_text(staged.tenant_id, "tenant_id", 200)
    _uuid(staged.change_set_id)
    _hex(staged.staged_commit, 40, "staged_commit")
    _hex(staged.catalog_digest, 64, "catalog_digest")
    _required_text(staged.platform_release, "platform_release", 200)
    _hex(staged.workspace_contract_digest, 64, "workspace_contract_digest")
    _required_text(staged.author_subject, "author_subject", 300)
    if not isinstance(staged.high_impact, bool):
        raise AuthorityError("staged_change_invalid")
    return staged


def _validated_request(request: PublishRequest) -> None:
    if not isinstance(request, PublishRequest):
        raise AuthorityError("publish_request_invalid")
    _uuid(request.change_set_id)
    _hex(request.staged_commit, 40, "staged_commit")
    _hex(request.catalog_digest, 64, "catalog_digest")
    _required_text(request.platform_release, "platform_release", 200)
    _hex(request.workspace_contract_digest, 64, "workspace_contract_digest")


def _required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AuthorityError(f"{name}_missing")
    return value


def _hex(value: object, length: int, name: str) -> str:
    if not isinstance(value, str) or len(value) != length or value.lower() != value:
        raise AuthorityError(f"{name}_invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise AuthorityError(f"{name}_invalid") from exc
    return value


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise AuthorityError("change_set_id_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise AuthorityError("change_set_id_invalid") from exc
    if str(parsed) != value.lower():
        raise AuthorityError("change_set_id_invalid")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str):
        raise AuthorityError("confirmation_tampered")
    try:
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise AuthorityError("confirmation_tampered") from exc


def _confirmation_from_payload(payload: dict[str, Any]) -> PublishConfirmation:
    return PublishConfirmation(
        confirmation_id=payload["confirmation_id"],
        tenant_id=payload["tenant_id"],
        change_set_id=payload["change_set_id"],
        staged_commit=payload["staged_commit"],
        catalog_digest=payload["catalog_digest"],
        platform_release=payload["platform_release"],
        workspace_contract_digest=payload["workspace_contract_digest"],
        author_subject=payload["author_subject"],
        approver_subject=payload["approver_subject"],
        expires_at=_parse_expiry(payload["expires_at"]),
    )
