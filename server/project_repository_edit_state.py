"""Dormant server binding for durable project repository-edit state.

The adapter validates Unit1 values, resolves Unit2B authority server-side, and
passes exact immutable witnesses to the Unit3 PostgreSQL store. It owns no
route, Git operation, repository path, filesystem access, or model call.
"""
from __future__ import annotations

from typing import Mapping

from leaf_platform import repository_edit_store
from leaf_platform import store as platform_store

from project_repository_edit_contract import (
    ContractValidationError,
    OneUseConfirmation,
    RepositoryAuthorityKey,
    StagedRepositoryEditReceipt,
    parse_confirmation,
    parse_confirmation_json,
    parse_staged_receipt,
    parse_staged_receipt_json,
    receipt_digest_matches,
    require_confirmation_binding,
)


class RepositoryEditStateError(RuntimeError):
    """The request could not bind to server-owned repository authority."""


def _receipt(value: Mapping[str, object] | str | bytes) -> StagedRepositoryEditReceipt:
    try:
        return (parse_staged_receipt(value) if isinstance(value, Mapping)
                else parse_staged_receipt_json(value))
    except ContractValidationError as exc:
        raise RepositoryEditStateError("invalid_staged_receipt") from exc


def _confirmation(value: Mapping[str, object] | str | bytes) -> OneUseConfirmation:
    try:
        return (parse_confirmation(value) if isinstance(value, Mapping)
                else parse_confirmation_json(value))
    except ContractValidationError as exc:
        raise RepositoryEditStateError("invalid_confirmation") from exc


def _server_authority(authority: RepositoryAuthorityKey) -> dict[str, str]:
    resolved = platform_store.resolve_project_repository_authority(
        authority.tenant_id, authority.organization_id, authority.project_id)
    if resolved is None or resolved != {
        "tenant_id": authority.tenant_id,
        "organization_id": authority.organization_id,
        "project_id": authority.project_id,
        "repo_key": authority.repo_key,
    }:
        raise RepositoryEditStateError("project_repository_authority_mismatch")
    return resolved


def record_staged(receipt_value: Mapping[str, object] | str | bytes,
                  receipt_digest: str, *, expected_version: int,
                  transition_key: str) -> dict:
    receipt = _receipt(receipt_value)
    _server_authority(receipt.authority_key)
    if not receipt_digest_matches(receipt, receipt_digest):
        raise RepositoryEditStateError("receipt_digest_mismatch")
    return repository_edit_store.record_staged(
        receipt, receipt_digest, expected_version=expected_version,
        transition_key=transition_key)


def await_confirmation(authority: RepositoryAuthorityKey, edit_id: str, *,
                       expected_version: int, transition_key: str) -> dict:
    _server_authority(authority)
    return repository_edit_store.await_confirmation(
        edit_id, expected_version=expected_version, transition_key=transition_key)


def put_confirmation(receipt_value: Mapping[str, object] | str | bytes,
                     confirmation_value: Mapping[str, object] | str | bytes, *,
                     expected_edit_version: int, transition_key: str) -> dict:
    receipt = _receipt(receipt_value)
    confirmation = _confirmation(confirmation_value)
    _server_authority(receipt.authority_key)
    try:
        require_confirmation_binding(confirmation, receipt)
    except ContractValidationError as exc:
        raise RepositoryEditStateError("confirmation_binding_mismatch") from exc
    return repository_edit_store.put_confirmation(
        confirmation, expected_edit_version=expected_edit_version,
        transition_key=transition_key)


def consume_for_publish(authority: RepositoryAuthorityKey, edit_id: str,
                        confirmation_id: str, *, expected_version: int,
                        transition_key: str) -> dict:
    _server_authority(authority)
    return repository_edit_store.consume_for_publish(
        edit_id, confirmation_id, expected_version=expected_version,
        transition_key=transition_key)


def settle_publish(authority: RepositoryAuthorityKey, edit_id: str, *,
                   private_ref_commit: str, main_commit: str, main_tree: str,
                   expected_version: int, transition_key: str) -> dict:
    _server_authority(authority)
    return repository_edit_store.settle_publish(
        edit_id, private_ref_commit=private_ref_commit, main_commit=main_commit,
        main_tree=main_tree, expected_version=expected_version,
        transition_key=transition_key)


def recover_publish(authority: RepositoryAuthorityKey, edit_id: str, *,
                    private_ref_commit: str, main_commit: str, main_tree: str,
                    expected_version: int, transition_key: str,
                    reason_code: str) -> dict:
    _server_authority(authority)
    return repository_edit_store.recover_publish(
        edit_id, private_ref_commit=private_ref_commit, main_commit=main_commit,
        main_tree=main_tree, expected_version=expected_version,
        transition_key=transition_key, reason_code=reason_code)
