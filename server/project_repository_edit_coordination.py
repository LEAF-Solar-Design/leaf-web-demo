"""P8 Unit 4 and Unit 5 project repository edit coordination.

Server-side counterpart to the harness's closed, digest-only coordination
client and the Unit 5 lease-handoff coordinator. This module owns no route,
feature flag, model tool, SdkRepoEditor, customer call path, database
migration, repository root input, Git credential, or live repository
mutation. It is a dormant, closed wire facade over the Unit 2/3 PostgreSQL
authority and transaction store.

Tenant, organization, project, repository, and actor-role resolution stay
server-owned. The durable backend rechecks the immutable authority tuple, and
the caller supplies the active role resolver.
Foreign, missing, revoked, and mismatched authority collapse to the same
unavailable result, never a distinguishing error.

No prompt, diff, source, file content, repository path, credential, or
secret ever appears in a request, response, or error raised here.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable, Mapping, Optional

from leaf_platform import repository_edit_store
from leaf_platform import store as platform_store

from project_repository_edit_contract import (
    ContractValidationError,
    RepositoryAuthorityKey,
    StagedRepositoryEditReceipt,
    parse_staged_receipt,
    receipt_digest_matches,
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
_WRITER_ROLES = frozenset({"owner", "editor"})

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


# actor_binding_id, authority -> role, or None when unresolvable (foreign,
# missing, revoked, or mismatched authority all resolve to None).
ActorRoleResolver = Callable[[str, RepositoryAuthorityKey], Optional[str]]


def _active_role(actor_binding_id: str, authority: RepositoryAuthorityKey) -> Optional[str]:
    return platform_store.active_identity_role(
        uuid.UUID(authority.organization_id), uuid.UUID(actor_binding_id))


class _DurableBackend:
    @staticmethod
    def _authority(authority: RepositoryAuthorityKey) -> None:
        resolved = platform_store.resolve_project_repository_authority(
            authority.tenant_id, authority.organization_id, authority.project_id)
        if resolved != {
            "tenant_id": authority.tenant_id, "organization_id": authority.organization_id,
            "project_id": authority.project_id, "repo_key": authority.repo_key,
        }:
            _fail("actor_authority_unavailable")

    def record_staged(self, receipt, digest, **kwargs):
        parsed = parse_staged_receipt(receipt)
        self._authority(parsed.authority_key)
        return repository_edit_store.record_staged(parsed, digest, **kwargs)

    def consume_for_publish(self, authority, edit_id, confirmation_id, **kwargs):
        self._authority(authority)
        return repository_edit_store.consume_for_publish(
            edit_id, confirmation_id, **kwargs)

    def settle_publish(self, authority, edit_id, **kwargs):
        self._authority(authority)
        return repository_edit_store.settle_publish(edit_id, **kwargs)

    def recover_publish(self, authority, edit_id, **kwargs):
        self._authority(authority)
        return repository_edit_store.recover_publish(edit_id, **kwargs)


class RepositoryEditCoordinationState:
    """Closed wire facade over the durable Unit 3 PostgreSQL transaction."""

    def __init__(self, *, actor_roles: ActorRoleResolver = _active_role, backend=None):
        self._actor_roles = actor_roles
        self._backend = backend or _DurableBackend()

    def _require_writer(self, actor_binding_id: str, authority: RepositoryAuthorityKey) -> None:
        role = self._actor_roles(actor_binding_id, authority)
        if role not in _WRITER_ROLES:
            _fail("actor_authority_unavailable")

    def _call(self, method: str, *args, **kwargs):
        try:
            return getattr(self._backend, method)(*args, **kwargs)
        except repository_edit_store.RepositoryEditStoreError as exc:
            _fail(exc.code)

    def record_staged(self, receipt: StagedRepositoryEditReceipt, receipt_digest: str, *,
                       expected_version: int, transition_key: str) -> dict:
        self._require_writer(receipt.actor_binding_id, receipt.authority_key)
        if not receipt_digest_matches(receipt, receipt_digest):
            _fail("receipt_digest_mismatch")
        return self._call("record_staged",
            receipt.to_mapping(), receipt_digest, expected_version=expected_version,
            transition_key=transition_key)

    def authorize_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                           actor_binding_id: str, confirmation_id: str, receipt_digest: str,
                           publish_lease_id: str, publish_lease_generation: int,
                           expected_version: int, transition_key: str) -> dict:
        self._require_writer(actor_binding_id, authority)
        row = self._call("consume_for_publish",
            authority, edit_id, confirmation_id, expected_version=expected_version,
            transition_key=transition_key, publish_lease_id=publish_lease_id,
            publish_lease_generation=publish_lease_generation,
            receipt_digest=receipt_digest)
        if row["receipt_digest"] != receipt_digest:
            _fail("receipt_digest_mismatch")
        return {
            "edit_id": row["edit_id"], "state": row["state"], "version": row["version"],
            "receipt_digest": row["receipt_digest"],
            "expected_main_commit": row["expected_main_commit"],
            "staged_head_commit": row["staged_head_commit"], "staged_tree": row["staged_tree"],
            "private_ref": f"refs/leaf/changes/{row['edit_id']}",
            "publish_lease_id": publish_lease_id,
            "publish_lease_generation": publish_lease_generation,
        }

    def settle_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                        actor_binding_id: str, publish_lease_id: str,
                        publish_lease_generation: int, private_ref_commit: str,
                        main_commit: str, main_tree: str, expected_version: int,
                        transition_key: str) -> dict:
        self._require_writer(actor_binding_id, authority)
        return self._call("settle_publish",
            authority, edit_id, private_ref_commit=private_ref_commit,
            main_commit=main_commit, main_tree=main_tree, expected_version=expected_version,
            transition_key=transition_key,
            publish_lease_id=publish_lease_id,
            publish_lease_generation=publish_lease_generation)

    def recover_publish(self, *, authority: RepositoryAuthorityKey, edit_id: str,
                         actor_binding_id: str, recovery_lease_id: str,
                         recovery_lease_generation: int, private_ref_commit: str,
                         main_commit: str, main_tree: str, expected_version: int,
                         reason_code: str, transition_key: str) -> dict:
        self._require_writer(actor_binding_id, authority)
        return self._call("recover_publish",
            authority, edit_id, private_ref_commit=private_ref_commit,
            main_commit=main_commit, main_tree=main_tree, expected_version=expected_version,
            transition_key=transition_key,
            reason_code=reason_code, recovery_lease_id=recovery_lease_id,
            recovery_lease_generation=recovery_lease_generation)


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
    transition_key = _transition_key(record["transition_key"])
    result = state.record_staged(
        receipt, receipt_digest, expected_version=expected_version,
        transition_key=transition_key)
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
    transition_key = _transition_key(record["transition_key"])
    result = state.authorize_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        confirmation_id=confirmation_id, receipt_digest=receipt_digest,
        publish_lease_id=publish_lease_id, publish_lease_generation=publish_lease_generation,
        expected_version=expected_version, transition_key=transition_key,
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
    transition_key = _transition_key(record["transition_key"])
    result = state.settle_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        publish_lease_id=publish_lease_id, publish_lease_generation=publish_lease_generation,
        private_ref_commit=private_ref_commit, main_commit=main_commit, main_tree=main_tree,
        expected_version=expected_version, transition_key=transition_key,
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
    transition_key = _transition_key(record["transition_key"])
    reason_code = _string(record["reason_code"], "invalid_reason_code")
    if not _REASON_CODE_RE.fullmatch(reason_code):
        _fail("invalid_reason_code")
    result = state.recover_publish(
        authority=authority, edit_id=edit_id, actor_binding_id=actor_binding_id,
        recovery_lease_id=recovery_lease_id, recovery_lease_generation=recovery_lease_generation,
        private_ref_commit=private_ref_commit, main_commit=main_commit, main_tree=main_tree,
        expected_version=expected_version, reason_code=reason_code,
        transition_key=transition_key,
    )
    return {"contract": CONTRACT, "action": "recover_publish", **result}
