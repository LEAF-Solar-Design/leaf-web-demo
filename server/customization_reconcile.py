"""Reconcile a validated tenant release request with the effective release pin.

This module deliberately owns no storage, Git, HTTP, or cloud client.  Its
dependencies are narrow protocols so a coordination transaction and a Git
change-ref implementation can provide the required durability guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import re
from typing import Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

from platform_release_policy import (
    PlatformReleasePolicy,
    PlatformReleasePolicyError,
    validate_workspace_reference,
)


class ReconcileStatus(str, Enum):
    NOOP = "noop"
    UPDATED = "updated"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class ValidatedWorkspaceRelease:
    """The release fields accepted only after platform policy validation."""

    desired_release: str
    workspace_contract: str
    workspace_contract_digest: str


@dataclass(frozen=True)
class EffectiveRelease:
    """A coordination-store release pin and its CAS version."""

    release: str
    version: int


@dataclass(frozen=True)
class ReleaseAudit:
    """Data the repository must append with an effective-pin update."""

    operation_id: str
    tenant_id: str
    prior_release: str
    desired_release: str
    workspace_contract_digest: str


@dataclass(frozen=True)
class MigrationChangeSet:
    change_set_id: str
    ref: str
    created: bool


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    desired_release: str | None
    effective_release: str | None
    reason_code: str | None = None
    change_set_id: str | None = None
    change_ref: str | None = None


class CoordinationRepository(Protocol):
    """The only source of the runtime effective platform release."""

    def get_effective_release(self, *, tenant_id: str) -> EffectiveRelease:
        ...

    def compare_and_swap_effective_release(
        self,
        *,
        tenant_id: str,
        expected_version: int,
        desired_release: str,
        workspace_contract_digest: str,
        audit: ReleaseAudit,
    ) -> bool:
        """Update the pin and append ``audit`` in one durable transaction."""
        ...


class CompatibilityChecker(Protocol):
    """Platform-owned compatibility decision for two immutable releases."""

    def is_compatible(
        self,
        *,
        effective_release: str,
        desired_release: str,
        workspace_contract: str,
        workspace_contract_digest: str,
    ) -> bool:
        ...


class MigrationBranchAdapter(Protocol):
    """Reserve one deterministic migration change set and its isolated Git ref."""

    def reserve_migration(
        self,
        *,
        tenant_id: str,
        change_set_id: str,
        ref: str,
        expected_effective_version: int,
        effective_release: str,
        desired_release: str,
        workspace_contract_digest: str,
    ) -> MigrationChangeSet:
        ...


def validate_desired_release(
    policy: PlatformReleasePolicy, workspace_reference: Mapping[str, object]
) -> ValidatedWorkspaceRelease:
    """Convert a tenant reference into the only input accepted by ``reconcile``."""
    release = validate_workspace_reference(policy, workspace_reference)
    return ValidatedWorkspaceRelease(
        desired_release=release.release_id,
        workspace_contract=release.workspace_contract,
        workspace_contract_digest=release.workspace_contract_sha256,
    )


class PlatformReleaseReconciler:
    def __init__(
        self,
        repository: CoordinationRepository,
        compatibility: CompatibilityChecker,
        migration_branches: MigrationBranchAdapter,
    ) -> None:
        self._repository = repository
        self._compatibility = compatibility
        self._migration_branches = migration_branches

    def reconcile_workspace_reference(
        self,
        *,
        tenant_id: str,
        workspace_reference: Mapping[str, object],
        policy: PlatformReleasePolicy,
    ) -> ReconcileResult:
        """Validate a tenant reference before using its desired-release value."""
        try:
            desired = validate_desired_release(policy, workspace_reference)
        except (PlatformReleasePolicyError, TypeError, ValueError):
            return ReconcileResult(
                ReconcileStatus.FAILED, None, None, "invalid_workspace_reference"
            )
        return self.reconcile(tenant_id=tenant_id, desired=desired)

    def reconcile(
        self, *, tenant_id: str, desired: ValidatedWorkspaceRelease
    ) -> ReconcileResult:
        """Apply a validated desired release without trusting a tenant Git head.

        A failed read, compatibility decision, CAS, or migration reservation is
        fail-closed.  The deterministic change-set identifier makes a retry ask
        the adapter for the same ref, preventing branch storms.
        """
        if not _valid_input(tenant_id, desired):
            return ReconcileResult(ReconcileStatus.FAILED, None, None, "invalid_input")
        try:
            effective = self._repository.get_effective_release(tenant_id=tenant_id)
        except Exception:  # Repository errors must never infer an effective pin.
            return ReconcileResult(
                ReconcileStatus.FAILED, desired.desired_release, None, "effective_read_failed"
            )
        if not _valid_effective(effective):
            return ReconcileResult(
                ReconcileStatus.FAILED, desired.desired_release, None, "invalid_effective_release"
            )
        if effective.release == desired.desired_release:
            return ReconcileResult(
                ReconcileStatus.NOOP, desired.desired_release, effective.release
            )

        try:
            compatible = self._compatibility.is_compatible(
                effective_release=effective.release,
                desired_release=desired.desired_release,
                workspace_contract=desired.workspace_contract,
                workspace_contract_digest=desired.workspace_contract_digest,
            )
        except Exception:
            return ReconcileResult(
                ReconcileStatus.FAILED,
                desired.desired_release,
                effective.release,
                "compatibility_check_failed",
            )

        if not isinstance(compatible, bool):
            return ReconcileResult(
                ReconcileStatus.FAILED,
                desired.desired_release,
                effective.release,
                "invalid_compatibility_result",
            )

        if compatible:
            audit = ReleaseAudit(
                operation_id=_operation_id(tenant_id, effective.release, desired),
                tenant_id=tenant_id,
                prior_release=effective.release,
                desired_release=desired.desired_release,
                workspace_contract_digest=desired.workspace_contract_digest,
            )
            try:
                updated = self._repository.compare_and_swap_effective_release(
                    tenant_id=tenant_id,
                    expected_version=effective.version,
                    desired_release=desired.desired_release,
                    workspace_contract_digest=desired.workspace_contract_digest,
                    audit=audit,
                )
            except Exception:
                return ReconcileResult(
                    ReconcileStatus.FAILED, desired.desired_release, effective.release, "effective_update_failed"
                )
            if not updated:
                return ReconcileResult(
                    ReconcileStatus.FAILED, desired.desired_release, effective.release, "stale_effective_version"
                )
            return ReconcileResult(
                ReconcileStatus.UPDATED, desired.desired_release, desired.desired_release
            )

        change_set_id = _migration_change_set_id(tenant_id, effective.release, desired)
        ref = f"refs/leaf/changes/{change_set_id}"
        try:
            migration = self._migration_branches.reserve_migration(
                tenant_id=tenant_id,
                change_set_id=change_set_id,
                ref=ref,
                expected_effective_version=effective.version,
                effective_release=effective.release,
                desired_release=desired.desired_release,
                workspace_contract_digest=desired.workspace_contract_digest,
            )
        except Exception:
            return ReconcileResult(
                ReconcileStatus.FAILED, desired.desired_release, effective.release, "migration_reservation_failed"
            )
        if migration.change_set_id != change_set_id or migration.ref != ref:
            return ReconcileResult(
                ReconcileStatus.FAILED, desired.desired_release, effective.release, "invalid_migration_receipt"
            )
        return ReconcileResult(
            ReconcileStatus.PENDING,
            desired.desired_release,
            effective.release,
            "migration_required",
            change_set_id,
            ref,
        )


def _migration_change_set_id(
    tenant_id: str, effective_release: str, desired: ValidatedWorkspaceRelease
) -> str:
    # UUID5 is stable and valid for the contract's change-set ID format.
    return str(uuid5(
        NAMESPACE_URL,
        "leaf.customization.migration.v1|"
        f"{tenant_id}|{effective_release}|{desired.desired_release}|"
        f"{desired.workspace_contract}|{desired.workspace_contract_digest}",
    ))


def _operation_id(tenant_id: str, effective_release: str, desired: ValidatedWorkspaceRelease) -> str:
    return sha256(
        ("leaf.customization.reconcile.v1|" + tenant_id + "|" + effective_release + "|" +
         desired.desired_release + "|" + desired.workspace_contract_digest).encode("utf-8")
    ).hexdigest()


def _valid_input(tenant_id: str, desired: ValidatedWorkspaceRelease) -> bool:
    return (
        isinstance(tenant_id, str)
        and bool(tenant_id.strip())
        and len(tenant_id) <= 200
        and isinstance(desired, ValidatedWorkspaceRelease)
        and all(isinstance(value, str) and bool(value.strip()) for value in (
            desired.desired_release,
            desired.workspace_contract,
            desired.workspace_contract_digest,
        ))
        and len(desired.desired_release) <= 200
        and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,199}", desired.workspace_contract)
        is not None
        and re.fullmatch(r"[0-9a-f]{64}", desired.workspace_contract_digest)
        is not None
    )


def _valid_effective(effective: EffectiveRelease) -> bool:
    return (
        isinstance(effective, EffectiveRelease)
        and isinstance(effective.release, str)
        and bool(effective.release)
        and isinstance(effective.version, int)
        and not isinstance(effective.version, bool)
        and effective.version >= 0
    )
