"""P8 Unit 4 and Unit 5 project repository edit coordination.

Server-side counterpart to the harness's closed, digest-only coordination
client and the Unit 5 lease-handoff coordinator. This module owns no route,
feature flag, model tool, SdkRepoEditor, customer call path, database
migration, repository root input, Git credential, or live repository
mutation. It is a dormant, in-process, versioned edit-row state machine: a
later caller may back it with the Unit 2/3 PostgreSQL store without changing
this module's wire contract or invariants.

Tenant, organization, project, repository, and actor-role resolution stay
server-owned but are supplied by the caller through an injected callable so
this module never itself reaches into the platform's authority store.
Foreign, missing, revoked, and mismatched authority collapse to the same
unavailable result, never a distinguishing error.

No prompt, diff, source, file content, repository path, credential, or
secret ever appears in a request, response, or error raised here.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from project_repository_edit_contract import (
    ContractValidationError,
    OneUseConfirmation,
    RepositoryAuthorityKey,
    StagedRepositoryEditReceipt,
    parse_staged_receipt,
    receipt_digest_matches,
    require_confirmation_binding,
)

CONTRACT = "leaf.project-repository-edit-coordination.v1"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_REF_RE = re.compile(
    r"^refs/leaf/changes/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_REASON_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# Only these roles may stage, publish, recover, or roll back.
_WRITER_ROLES = frozenset({"writer", "owner"})

_AUTHORITY_FIELDS = ("tenant_id", "organization_id", "project_id", "repo_key")

_RECORD_STAGED_REQUEST_FIELDS = frozenset({
    "contract", "action", "receipt", "receipt_digest", "expected_version", "transition_key",
})
_AUTHORIZE_PUBLISH_REQUEST_FIELDS = frozenset({
    "contract", "action", *_AUTHORITY_FIELDS, "edit_id", "actor_binding_id", "confirmation_id",
    "receipt_digest", "publish_lease_id", "publish_lease_generation", "expected_version",
    "transition_key",
})
_SETTLE_PUBLISH_REQUEST_FIELDS = frozenset({
    "contract", "action", *_AUTHORITY_FIELDS, "edit_id", "actor_binding_id", "publish_lease_id",
    "publish_lease_generation", "private_ref_commit", "main_commit", "main_tree",
    "expected_version", "transition_key",
})
_RECOVER_PUBLISH_REQUEST_FIELDS = frozenset({
    "contract", "action", *_AUTHORITY_FIELDS, "edit_id", "actor_binding_id", "recovery_lease_id",
    "recovery_lease_generation", "private_ref_commit", "main_commit", "main_tree",
    "expected_version", "transition_key", "reason_code",
})


class CoordinationError(RuntimeError):
    """Fixed-code coordination failure. Never carries request content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str):
    raise CoordinationError(code)


def _closed(value: object, fields: frozenset, label: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        _fail(f"{label}_fields_invalid")
    return value  # type: ignore[return-value]


def _string(value: object, code: str) -> str:
    if not isinstance(value, str):
        _fail(code)
    return value


def _uuid(value: object, code: str) -> str:
    raw = _string(value, code)
    if not _UUID_RE.fullmatch(raw):
        _fail(code)
    return raw


def _sha(value: object, code: str) -> str:
    raw = _string(value, code)
    if not _SHA_RE.fullmatch(raw):
        _fail(code)
    return raw


def _digest(value: object, code: str) -> str:
    raw = _string(value, code)
    if not _DIGEST_RE.fullmatch(raw):
        _fail(code)
    return raw


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(code)
    return value


def _non_negative_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(code)
    return value


def _transition_key(value: object) -> str:
    raw = _string(value, "invalid_transition_key")
    if not 1 <= len(raw) <= 200:
        _fail("invalid_transition_key")
    return raw


def _authority(record: Mapping[str, object]) -> RepositoryAuthorityKey:
    return RepositoryAuthorityKey(
        tenant_id=_uuid(record["tenant_id"], "invalid_tenant_id"),
        organization_id=_uuid(record["organization_id"], "invalid_organization_id"),
        project_id=_uuid(record["project_id"], "invalid_project_id"),
        repo_key=_uuid(record["repo_key"], "invalid_repo_key"),
    )


@dataclass(frozen=True)
class PublishLeaseWitness:
    publish_lease_id: str
    publish_lease_generation: int


@dataclass
class _EditRow:
    edit_id: str
    authority: RepositoryAuthorityKey
    receipt: StagedRepositoryEditReceipt
    receipt_digest: str
    state: str  # staged | publishing | published
    version: int
    publish_witness: Optional[PublishLeaseWitness] = None
    recovery_witness: Optional[PublishLeaseWitness] = None
    private_ref_commit: Optional[str] = None
    main_commit: Optional[str] = None
    main_tree: Optional[str] = None


# actor_binding_id, authority -> role, or None when unresolvable (foreign,
# missing, revoked, or mismatched authority all resolve to None).
ActorRoleResolver = Callable[[str, RepositoryAuthorityKey], Optional[str]]


class RepositoryEditCoordinationState:
    """Dormant, in-process, versioned edit-row state machine for one process.

    Every mutation is guarded by the row's optimistic `expected_version`, so
    only one writer's compare-and-swap can land per version. Publish and
    recovery never touch Git; they hold and return the exact witnesses the
    harness's own single `git update-ref refs/heads/main` compare-and-swap
    must recheck and report.
    """

    def __init__(self, *, actor_roles: ActorRoleResolver):
        self._rows: dict[str, _EditRow] = {}
        self._confirmations: dict[str, OneUseConfirmation] = {}
        self._actor_roles = actor_roles
        self._lock = threading.Lock()

    def put_confirmation(self, confirmation: OneUseConfirmation) -> None:
        """Test/seam entry point standing in for the Unit 3 confirmation store."""
        with self._lock:
            self._confirmations[confirmation.confirmation_id] = confirmation

    def _require_writer(self, actor_binding_id: str, authority: RepositoryAuthorityKey) -> None:
        role = self._actor_roles(actor_binding_id, authority)
        if role not in _WRITER_ROLES:
            _fail("actor_authority_unavailable")

    def record_staged(self, receipt: StagedRepositoryEditReceipt, receipt_digest: str, *,
                       expected_version: int) -> dict:
        with self._lock:
            self._require_writer(receipt.actor_binding_id, receipt.authority_key)
            if not receipt_digest_matches(receipt, receipt_digest):
                _fail("receipt_digest_mismatch")
            if receipt.edit_id in self._rows:
                _fail("edit_already_exists")
            if expected_version != 0:
                _fail("version_conflict")
            row = _EditRow(
                edit_id=receipt.edit_id, authority=receipt.authority_key, receipt=receipt,
                receipt_digest=receipt_digest, state="staged", version=1,
            )
            self._rows[receipt.edit_id] = row
            return {"edit_id": row.edit_id, "state": row.state, "version": row.version}

    def _row_for(self, authority: RepositoryAuthorityKey, edit_id: str) -> _EditRow:
        row = self._rows.get(edit_id)
        if row is None or row.authority != authority:
            _fail("edit_unavailable")
        return row

    def authorize_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                           actor_binding_id: str, confirmation_id: str, receipt_digest: str,
                           publish_lease_id: str, publish_lease_generation: int,
                           expected_version: int) -> dict:
        with self._lock:
            self._require_writer(actor_binding_id, authority)
            row = self._row_for(authority, edit_id)
            if row.receipt_digest != receipt_digest:
                _fail("receipt_digest_mismatch")
            if (row.state == "publishing" and row.publish_witness is not None and
                    row.publish_witness.publish_lease_id == publish_lease_id and
                    row.publish_witness.publish_lease_generation == publish_lease_generation and
                    row.version == expected_version + 1):
                # Exact already-authorized retry: read-only, no re-consumption.
                return self._publish_matrix(row)
            if row.state != "staged":
                _fail("edit_not_staged")
            if row.version != expected_version:
                _fail("version_conflict")
            confirmation = self._confirmations.get(confirmation_id)
            if confirmation is None:
                _fail("confirmation_unavailable")
            try:
                require_confirmation_binding(confirmation, row.receipt)
            except ContractValidationError:
                _fail("confirmation_binding_mismatch")
            if publish_lease_generation <= row.receipt.writer_witness.writer_lease_generation:
                _fail("publish_generation_not_strictly_greater")
            # Atomically consume the one-use confirmation and record the new
            # publish lease witness as a separate field on the same row. The
            # staged receipt above is never replaced or rewritten.
            del self._confirmations[confirmation_id]
            row.publish_witness = PublishLeaseWitness(publish_lease_id, publish_lease_generation)
            row.state = "publishing"
            row.version += 1
            return self._publish_matrix(row)

    def _publish_matrix(self, row: _EditRow) -> dict:
        assert row.publish_witness is not None
        return {
            "edit_id": row.edit_id,
            "state": row.state,
            "version": row.version,
            "receipt_digest": row.receipt_digest,
            "expected_main_commit": row.receipt.base_commit,
            "staged_head_commit": row.receipt.staged_head_commit,
            "staged_tree": row.receipt.staged_tree,
            "private_ref": f"refs/leaf/changes/{row.edit_id}",
            "publish_lease_id": row.publish_witness.publish_lease_id,
            "publish_lease_generation": row.publish_witness.publish_lease_generation,
        }

    def settle_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                        actor_binding_id: str, publish_lease_id: str,
                        publish_lease_generation: int, private_ref_commit: str,
                        main_commit: str, main_tree: str, expected_version: int) -> dict:
        with self._lock:
            self._require_writer(actor_binding_id, authority)
            row = self._row_for(authority, edit_id)
            if row.state == "published":
                if row.version != expected_version:
                    _fail("version_conflict")
                return {"edit_id": row.edit_id, "state": row.state, "version": row.version}
            if row.state != "publishing" or row.publish_witness is None:
                _fail("edit_not_publishing")
            if row.version != expected_version:
                _fail("version_conflict")
            if (row.publish_witness.publish_lease_id != publish_lease_id or
                    row.publish_witness.publish_lease_generation != publish_lease_generation):
                _fail("publish_lease_mismatch")
            if (private_ref_commit != row.receipt.staged_head_commit or
                    main_commit != row.receipt.staged_head_commit or
                    main_tree != row.receipt.staged_tree):
                _fail("settlement_observation_mismatch")
            row.state = "published"
            row.private_ref_commit = private_ref_commit
            row.main_commit = main_commit
            row.main_tree = main_tree
            row.version += 1
            return {"edit_id": row.edit_id, "state": row.state, "version": row.version}

    def recover_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                         actor_binding_id: str, recovery_lease_id: str,
                         recovery_lease_generation: int, private_ref_commit: str,
                         main_commit: str, main_tree: str, expected_version: int,
                         reason_code: str) -> dict:
        with self._lock:
            self._require_writer(actor_binding_id, authority)
            row = self._row_for(authority, edit_id)
            if row.state == "published":
                # Observation-only: verify the frozen Git matrix and resume.
                # No second compare-and-swap, no confirmation, no new authority.
                if (private_ref_commit != row.receipt.staged_head_commit or
                        main_commit != row.receipt.staged_head_commit or
                        main_tree != row.receipt.staged_tree):
                    _fail("recovery_observation_mismatch")
                return {"edit_id": row.edit_id, "state": row.state, "version": row.version}
            if row.state != "publishing" or row.publish_witness is None:
                _fail("edit_not_publishing")
            if row.version != expected_version:
                _fail("version_conflict")
            if recovery_lease_generation < row.publish_witness.publish_lease_generation:
                _fail("recovery_generation_stale")
            if (private_ref_commit != row.receipt.staged_head_commit or
                    main_commit != row.receipt.staged_head_commit or
                    main_tree != row.receipt.staged_tree):
                _fail("recovery_witness_incomplete")
            row.state = "published"
            row.private_ref_commit = private_ref_commit
            row.main_commit = main_commit
            row.main_tree = main_tree
            row.recovery_witness = PublishLeaseWitness(recovery_lease_id, recovery_lease_generation)
            row.version += 1
            return {"edit_id": row.edit_id, "state": row.state, "version": row.version}


def handle_record_staged(state: RepositoryEditCoordinationState, body: object) -> dict:
    record = _closed(body, _RECORD_STAGED_REQUEST_FIELDS, "record_staged_request")
    if record["contract"] != CONTRACT:
        _fail("invalid_contract")
    if record["action"] != "record_staged":
        _fail("invalid_action")
    try:
        receipt = parse_staged_receipt(record["receipt"])  # type: ignore[arg-type]
    except ContractValidationError:
        _fail("invalid_receipt")
        raise  # unreachable, keeps type-checkers happy
    receipt_digest = _digest(record["receipt_digest"], "invalid_receipt_digest")
    expected_version = _non_negative_int(record["expected_version"], "invalid_expected_version")
    _transition_key(record["transition_key"])
    result = state.record_staged(receipt, receipt_digest, expected_version=expected_version)
    return {"contract": CONTRACT, "action": "record_staged", **result}


def handle_authorize_publish(state: RepositoryEditCoordinationState, body: object) -> dict:
    record = _closed(body, _AUTHORIZE_PUBLISH_REQUEST_FIELDS, "authorize_publish_request")
    if record["contract"] != CONTRACT:
        _fail("invalid_contract")
    if record["action"] != "authorize_publish":
        _fail("invalid_action")
    authority = _authority(record)
    edit_id = _uuid(record["edit_id"], "invalid_edit_id")
    actor_binding_id = _uuid(record["actor_binding_id"], "invalid_actor")
    confirmation_id = _uuid(record["confirmation_id"], "invalid_confirmation")
    receipt_digest = _digest(record["receipt_digest"], "invalid_receipt_digest")
    publish_lease_id = _uuid(record["publish_lease_id"], "invalid_publish_lease")
    publish_lease_generation = _positive_int(
        record["publish_lease_generation"], "invalid_publish_lease_generation")
    expected_version = _positive_int(record["expected_version"], "invalid_expected_version")
    _transition_key(record["transition_key"])
    result = state.authorize_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        confirmation_id=confirmation_id, receipt_digest=receipt_digest,
        publish_lease_id=publish_lease_id, publish_lease_generation=publish_lease_generation,
        expected_version=expected_version,
    )
    return {"contract": CONTRACT, "action": "authorize_publish", **result}


def handle_settle_publish(state: RepositoryEditCoordinationState, body: object) -> dict:
    record = _closed(body, _SETTLE_PUBLISH_REQUEST_FIELDS, "settle_publish_request")
    if record["contract"] != CONTRACT:
        _fail("invalid_contract")
    if record["action"] != "settle_publish":
        _fail("invalid_action")
    authority = _authority(record)
    edit_id = _uuid(record["edit_id"], "invalid_edit_id")
    actor_binding_id = _uuid(record["actor_binding_id"], "invalid_actor")
    publish_lease_id = _uuid(record["publish_lease_id"], "invalid_publish_lease")
    publish_lease_generation = _positive_int(
        record["publish_lease_generation"], "invalid_publish_lease_generation")
    private_ref_commit = _sha(record["private_ref_commit"], "invalid_private_ref_commit")
    main_commit = _sha(record["main_commit"], "invalid_main_commit")
    main_tree = _sha(record["main_tree"], "invalid_main_tree")
    expected_version = _positive_int(record["expected_version"], "invalid_expected_version")
    _transition_key(record["transition_key"])
    result = state.settle_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        publish_lease_id=publish_lease_id, publish_lease_generation=publish_lease_generation,
        private_ref_commit=private_ref_commit, main_commit=main_commit, main_tree=main_tree,
        expected_version=expected_version,
    )
    return {"contract": CONTRACT, "action": "settle_publish", **result}


def handle_recover_publish(state: RepositoryEditCoordinationState, body: object) -> dict:
    record = _closed(body, _RECOVER_PUBLISH_REQUEST_FIELDS, "recover_publish_request")
    if record["contract"] != CONTRACT:
        _fail("invalid_contract")
    if record["action"] != "recover_publish":
        _fail("invalid_action")
    authority = _authority(record)
    edit_id = _uuid(record["edit_id"], "invalid_edit_id")
    actor_binding_id = _uuid(record["actor_binding_id"], "invalid_actor")
    recovery_lease_id = _uuid(record["recovery_lease_id"], "invalid_recovery_lease")
    recovery_lease_generation = _positive_int(
        record["recovery_lease_generation"], "invalid_recovery_lease_generation")
    private_ref_commit = _sha(record["private_ref_commit"], "invalid_private_ref_commit")
    main_commit = _sha(record["main_commit"], "invalid_main_commit")
    main_tree = _sha(record["main_tree"], "invalid_main_tree")
    expected_version = _positive_int(record["expected_version"], "invalid_expected_version")
    _transition_key(record["transition_key"])
    reason_code = _string(record["reason_code"], "invalid_reason_code")
    if not _REASON_CODE_RE.fullmatch(reason_code):
        _fail("invalid_reason_code")
    result = state.recover_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        recovery_lease_id=recovery_lease_id, recovery_lease_generation=recovery_lease_generation,
        private_ref_commit=private_ref_commit, main_commit=main_commit, main_tree=main_tree,
        expected_version=expected_version, reason_code=reason_code,
    )
    return {"contract": CONTRACT, "action": "recover_publish", **result}
