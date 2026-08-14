"""Pure authority and receipt primitives for dormant project repository edits.

This module validates values only. It owns no repository selection, storage,
Git operation, route, entitlement decision, or confirmation consumption.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import rfc8785  # type: ignore[import-not-found]


STAGED_RECEIPT_CONTRACT = "leaf.project-repository-edit.v2"
CONFIRMATION_CONTRACT = "leaf.project-repository-edit-confirmation.v2"

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_STAGED_FIELDS = frozenset({
    "contract",
    "edit_id",
    "state",
    "operation",
    "source_edit_id",
    "actor_binding_id",
    "tenant_id",
    "organization_id",
    "project_id",
    "repo_key",
    "writer_lease_id",
    "writer_lease_generation",
    "base_commit",
    "staged_head_commit",
    "staged_tree",
    "changed_paths",
    "diff_digest",
    "instruction_digest",
    "idempotency_key",
})
_CONFIRMATION_FIELDS = frozenset({
    "contract",
    "confirmation_id",
    "receipt_digest",
    "approver_binding_id",
    "tenant_id",
    "organization_id",
    "project_id",
    "repo_key",
    "edit_id",
    "writer_lease_id",
    "writer_lease_generation",
    "staged_tree",
    "issued_at",
    "expires_at",
})


class ContractValidationError(ValueError):
    """A closed project repository edit value failed validation."""


@dataclass(frozen=True)
class RepositoryAuthorityKey:
    tenant_id: str
    organization_id: str
    project_id: str
    repo_key: str


@dataclass(frozen=True)
class WriterLeaseWitness:
    writer_lease_id: str
    writer_lease_generation: int


@dataclass(frozen=True)
class StagedRepositoryEditReceipt:
    edit_id: str
    operation: str
    source_edit_id: str | None
    actor_binding_id: str
    authority_key: RepositoryAuthorityKey
    writer_witness: WriterLeaseWitness
    base_commit: str
    staged_head_commit: str
    staged_tree: str
    changed_paths: tuple[str, ...]
    diff_digest: str
    instruction_digest: str
    idempotency_key: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": STAGED_RECEIPT_CONTRACT,
            "edit_id": self.edit_id,
            "state": "staged",
            "operation": self.operation,
            "source_edit_id": self.source_edit_id,
            "actor_binding_id": self.actor_binding_id,
            "tenant_id": self.authority_key.tenant_id,
            "organization_id": self.authority_key.organization_id,
            "project_id": self.authority_key.project_id,
            "repo_key": self.authority_key.repo_key,
            "writer_lease_id": self.writer_witness.writer_lease_id,
            "writer_lease_generation": self.writer_witness.writer_lease_generation,
            "base_commit": self.base_commit,
            "staged_head_commit": self.staged_head_commit,
            "staged_tree": self.staged_tree,
            "changed_paths": list(self.changed_paths),
            "diff_digest": self.diff_digest,
            "instruction_digest": self.instruction_digest,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class OneUseConfirmation:
    confirmation_id: str
    receipt_digest: str
    approver_binding_id: str
    authority_key: RepositoryAuthorityKey
    edit_id: str
    writer_witness: WriterLeaseWitness
    staged_tree: str
    issued_at: str
    expires_at: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": CONFIRMATION_CONTRACT,
            "confirmation_id": self.confirmation_id,
            "receipt_digest": self.receipt_digest,
            "approver_binding_id": self.approver_binding_id,
            "tenant_id": self.authority_key.tenant_id,
            "organization_id": self.authority_key.organization_id,
            "project_id": self.authority_key.project_id,
            "repo_key": self.authority_key.repo_key,
            "edit_id": self.edit_id,
            "writer_lease_id": self.writer_witness.writer_lease_id,
            "writer_lease_generation": self.writer_witness.writer_lease_generation,
            "staged_tree": self.staged_tree,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


def _duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: str | bytes) -> dict[str, object]:
    if not isinstance(raw, (str, bytes)):
        raise ContractValidationError("receipt JSON must be text or UTF-8 bytes")
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractValidationError("receipt JSON must be valid UTF-8") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_duplicate_keys)
    except ContractValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ContractValidationError("receipt JSON is invalid") from exc
    if type(value) is not dict:
        raise ContractValidationError("receipt JSON must contain one object")
    return value


def _closed(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    if type(value) is not dict or set(value) != fields:
        raise ContractValidationError(f"closed {label} fields do not match")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{field} must be a string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContractValidationError(f"{field} must be valid Unicode") from exc
    return value


def _uuid(value: object, field: str) -> str:
    raw = _string(value, field)
    try:
        parsed = uuid.UUID(raw)
    except ValueError as exc:
        raise ContractValidationError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != raw:
        raise ContractValidationError(f"{field} must be a canonical UUID")
    return raw


def _positive_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractValidationError("writer_lease_generation must be a positive integer")
    return value


def _git_sha(value: object, field: str) -> str:
    raw = _string(value, field)
    if not _GIT_SHA_RE.fullmatch(raw):
        raise ContractValidationError(f"{field} must be a lowercase Git SHA")
    return raw


def _digest(value: object, field: str) -> str:
    raw = _string(value, field)
    if not _DIGEST_RE.fullmatch(raw):
        raise ContractValidationError(f"{field} must be a lowercase SHA-256 digest")
    return raw


def _authority(value: Mapping[str, object]) -> RepositoryAuthorityKey:
    return RepositoryAuthorityKey(
        tenant_id=_uuid(value["tenant_id"], "tenant_id"),
        organization_id=_uuid(value["organization_id"], "organization_id"),
        project_id=_uuid(value["project_id"], "project_id"),
        repo_key=_uuid(value["repo_key"], "repo_key"),
    )


def _writer_witness(value: Mapping[str, object]) -> WriterLeaseWitness:
    return WriterLeaseWitness(
        writer_lease_id=_uuid(value["writer_lease_id"], "writer_lease_id"),
        writer_lease_generation=_positive_generation(value["writer_lease_generation"]),
    )


def _changed_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1_000:
        raise ContractValidationError("changed_paths must contain 1 to 1000 paths")
    paths: list[str] = []
    aliases: set[str] = set()
    for item in value:
        path = _string(item, "changed_paths item")
        try:
            encoded = path.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ContractValidationError("changed_paths must be valid UTF-8") from exc
        segments = path.split("/")
        if (
            not path
            or len(path) > 1_024
            or path.startswith("/")
            or _DRIVE_PATH_RE.match(path)
            or "\\" in path
            or "\x00" in path
            or path.endswith("/")
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(segment.casefold() == ".git" for segment in segments)
            or unicodedata.normalize("NFC", path) != path
        ):
            raise ContractValidationError("changed_paths contains an unsafe path")
        alias = unicodedata.normalize("NFC", path).casefold()
        if alias in aliases:
            raise ContractValidationError("changed_paths contains a case-fold alias")
        aliases.add(alias)
        paths.append(path)
        if len(encoded) == 0:
            raise ContractValidationError("changed_paths contains an empty path")
    if paths != sorted(paths, key=lambda item: item.encode("utf-8")):
        raise ContractValidationError("changed_paths must be sorted by UTF-8 bytes")
    return tuple(paths)


def _idempotency_key(value: object) -> str:
    raw = _string(value, "idempotency_key")
    if not 1 <= len(raw) <= 200:
        raise ContractValidationError("idempotency_key length is invalid")
    return raw


def parse_staged_receipt(value: Mapping[str, object]) -> StagedRepositoryEditReceipt:
    """Validate one closed staged receipt and return immutable values."""
    _closed(value, _STAGED_FIELDS, "staged receipt")
    if value["contract"] != STAGED_RECEIPT_CONTRACT:
        raise ContractValidationError("staged receipt contract is invalid")
    if value["state"] != "staged":
        raise ContractValidationError("staged receipt state is invalid")
    operation = _string(value["operation"], "operation")
    if operation not in {"edit", "rollback"}:
        raise ContractValidationError("operation must be edit or rollback")
    source_edit_id = value["source_edit_id"]
    if operation == "edit":
        if source_edit_id is not None:
            raise ContractValidationError("edit source_edit_id must be null")
        parsed_source_edit_id = None
    else:
        if source_edit_id is None:
            raise ContractValidationError("rollback source_edit_id must be a UUID")
        parsed_source_edit_id = _uuid(source_edit_id, "source_edit_id")
    return StagedRepositoryEditReceipt(
        edit_id=_uuid(value["edit_id"], "edit_id"),
        operation=operation,
        source_edit_id=parsed_source_edit_id,
        actor_binding_id=_uuid(value["actor_binding_id"], "actor_binding_id"),
        authority_key=_authority(value),
        writer_witness=_writer_witness(value),
        base_commit=_git_sha(value["base_commit"], "base_commit"),
        staged_head_commit=_git_sha(value["staged_head_commit"], "staged_head_commit"),
        staged_tree=_git_sha(value["staged_tree"], "staged_tree"),
        changed_paths=_changed_paths(value["changed_paths"]),
        diff_digest=_digest(value["diff_digest"], "diff_digest"),
        instruction_digest=_digest(value["instruction_digest"], "instruction_digest"),
        idempotency_key=_idempotency_key(value["idempotency_key"]),
    )


def parse_staged_receipt_json(raw: str | bytes) -> StagedRepositoryEditReceipt:
    return parse_staged_receipt(_strict_json(raw))


def staged_receipt_digest(receipt: StagedRepositoryEditReceipt) -> str:
    """Return lowercase SHA-256 over the RFC 8785 serialized receipt."""
    if not isinstance(receipt, StagedRepositoryEditReceipt):
        raise ContractValidationError("receipt must be a validated staged receipt")
    return hashlib.sha256(rfc8785.dumps(receipt.to_mapping())).hexdigest()


def receipt_digest_matches(receipt: StagedRepositoryEditReceipt, presented: object) -> bool:
    """Compare one syntactically valid receipt digest in constant time."""
    if not isinstance(presented, str) or not _DIGEST_RE.fullmatch(presented):
        return False
    expected = staged_receipt_digest(receipt)
    return hmac.compare_digest(expected.encode("ascii"), presented.encode("ascii"))


def _timestamp(value: object, field: str) -> tuple[str, datetime]:
    raw = _string(value, field)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ContractValidationError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{field} must include a timezone")
    return raw, parsed


def parse_confirmation(value: Mapping[str, object]) -> OneUseConfirmation:
    """Validate a closed confirmation value. Durable storage consumes it once."""
    _closed(value, _CONFIRMATION_FIELDS, "confirmation")
    if value["contract"] != CONFIRMATION_CONTRACT:
        raise ContractValidationError("confirmation contract is invalid")
    issued_at, issued = _timestamp(value["issued_at"], "issued_at")
    expires_at, expires = _timestamp(value["expires_at"], "expires_at")
    if expires <= issued:
        raise ContractValidationError("expires_at must be after issued_at")
    return OneUseConfirmation(
        confirmation_id=_uuid(value["confirmation_id"], "confirmation_id"),
        receipt_digest=_digest(value["receipt_digest"], "receipt_digest"),
        approver_binding_id=_uuid(value["approver_binding_id"], "approver_binding_id"),
        authority_key=_authority(value),
        edit_id=_uuid(value["edit_id"], "edit_id"),
        writer_witness=_writer_witness(value),
        staged_tree=_git_sha(value["staged_tree"], "staged_tree"),
        issued_at=issued_at,
        expires_at=expires_at,
    )


def parse_confirmation_json(raw: str | bytes) -> OneUseConfirmation:
    return parse_confirmation(_strict_json(raw))


def require_confirmation_binding(
    confirmation: OneUseConfirmation,
    receipt: StagedRepositoryEditReceipt,
) -> None:
    """Require one confirmation to bind the exact frozen receipt witnesses."""
    if not receipt_digest_matches(receipt, confirmation.receipt_digest):
        raise ContractValidationError("confirmation receipt digest does not match")
    if (
        confirmation.authority_key != receipt.authority_key
        or confirmation.edit_id != receipt.edit_id
        or confirmation.writer_witness != receipt.writer_witness
        or confirmation.staged_tree != receipt.staged_tree
    ):
        raise ContractValidationError("confirmation does not bind the staged receipt")
