from __future__ import annotations

from dataclasses import dataclass, field
import pytest

from customization_reconcile import (
    EffectiveRelease,
    MigrationChangeSet,
    PlatformReleaseReconciler,
    ReconcileStatus,
    ReleaseAudit,
    ValidatedWorkspaceRelease,
)
from platform_release_policy import load_policy


RELEASE = "leaf-platform-2026.07.23"
CONTRACT = "leaf.workspace.v1"
CONTRACT_DIGEST = "fc5fdcb63704127f1c70a430632699e878f79bcea4d7fecdc60782fc210e6865"
DESIRED = ValidatedWorkspaceRelease(RELEASE, CONTRACT, CONTRACT_DIGEST)


@dataclass
class FakeRepository:
    effective: EffectiveRelease
    cas_result: bool = True
    cas_error: Exception | None = None
    cas_calls: list[dict[str, object]] = field(default_factory=list)

    def get_effective_release(self, *, tenant_id: str) -> EffectiveRelease:
        return self.effective

    def compare_and_swap_effective_release(self, **kwargs: object) -> bool:
        self.cas_calls.append(kwargs)
        if self.cas_error:
            raise self.cas_error
        if self.cas_result:
            self.effective = EffectiveRelease(str(kwargs["desired_release"]), self.effective.version + 1)
        return self.cas_result


@dataclass
class FakeCompatibility:
    result: bool
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def is_compatible(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


@dataclass
class FakeBranches:
    error: Exception | None = None
    calls: list[dict[str, object]] = field(default_factory=list)
    reservations: dict[str, MigrationChangeSet] = field(default_factory=dict)

    def reserve_migration(self, **kwargs: object) -> MigrationChangeSet:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        change_set_id = str(kwargs["change_set_id"])
        existing = self.reservations.get(change_set_id)
        if existing:
            return existing
        receipt = MigrationChangeSet(change_set_id, str(kwargs["ref"]), True)
        self.reservations[change_set_id] = receipt
        return receipt


def reconciler(repo: FakeRepository, compatible: bool, branches: FakeBranches | None = None):
    return PlatformReleaseReconciler(repo, FakeCompatibility(compatible), branches or FakeBranches())


def test_identical_release_is_idempotent_noop_without_cas_or_branch():
    repo = FakeRepository(EffectiveRelease(RELEASE, 3))
    branches = FakeBranches()
    result = reconciler(repo, True, branches).reconcile(tenant_id="tenant-a", desired=DESIRED)

    assert result.status is ReconcileStatus.NOOP
    assert repo.cas_calls == []
    assert branches.calls == []


def test_compatible_upgrade_uses_audited_cas_and_never_creates_branch():
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    branches = FakeBranches()
    result = reconciler(repo, True, branches).reconcile(tenant_id="tenant-a", desired=DESIRED)

    assert result.status is ReconcileStatus.UPDATED
    assert repo.effective.release == RELEASE
    assert repo.cas_calls[0]["expected_version"] == 7
    assert isinstance(repo.cas_calls[0]["audit"], ReleaseAudit)
    assert branches.calls == []


def test_incompatible_upgrade_reserves_one_deterministic_ref_without_changing_pin():
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    branches = FakeBranches()
    service = reconciler(repo, False, branches)

    first = service.reconcile(tenant_id="tenant-a", desired=DESIRED)
    second = service.reconcile(tenant_id="tenant-a", desired=DESIRED)

    assert first.status is ReconcileStatus.PENDING
    assert second.status is ReconcileStatus.PENDING
    assert first.change_set_id == second.change_set_id
    assert first.change_ref == f"refs/leaf/changes/{first.change_set_id}"
    assert repo.effective == EffectiveRelease("leaf-platform-2026.07.01", 7)
    assert repo.cas_calls == []
    assert len(branches.reservations) == 1


@pytest.mark.parametrize("cas_result, expected_reason", [(False, "stale_effective_version")])
def test_stale_effective_version_fails_closed_without_branch(cas_result: bool, expected_reason: str):
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7), cas_result=cas_result)
    branches = FakeBranches()
    result = reconciler(repo, True, branches).reconcile(tenant_id="tenant-a", desired=DESIRED)

    assert result.status is ReconcileStatus.FAILED
    assert result.reason_code == expected_reason
    assert branches.calls == []


def test_adapter_failure_fails_closed_and_retry_uses_same_requested_change_set():
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    branches = FakeBranches(error=RuntimeError("git unavailable"))
    service = reconciler(repo, False, branches)

    result = service.reconcile(tenant_id="tenant-a", desired=DESIRED)
    retry = service.reconcile(tenant_id="tenant-a", desired=DESIRED)

    assert result.status is ReconcileStatus.FAILED
    assert retry.status is ReconcileStatus.FAILED
    assert result.reason_code == "migration_reservation_failed"
    assert len(branches.calls) == 2
    assert branches.calls[0]["change_set_id"] == branches.calls[1]["change_set_id"]


def test_non_boolean_compatibility_result_fails_closed():
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    compatibility = FakeCompatibility(True)
    compatibility.result = "yes"  # type: ignore[assignment]
    branches = FakeBranches()
    result = PlatformReleaseReconciler(repo, compatibility, branches).reconcile(
        tenant_id="tenant-a", desired=DESIRED,
    )

    assert result.status is ReconcileStatus.FAILED
    assert result.reason_code == "invalid_compatibility_result"
    assert repo.cas_calls == []
    assert branches.calls == []


def test_unknown_release_and_contract_mismatch_fail_closed_before_repository_access():
    policy = load_policy()
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    service = reconciler(repo, True)

    unknown = service.reconcile_workspace_reference(
        tenant_id="tenant-a",
        policy=policy,
        workspace_reference={
            "contract": "leaf.workspace.v1",
            "workspace_contract": CONTRACT,
            "desired_platform_release": "unknown-release",
        },
    )
    mismatch = service.reconcile_workspace_reference(
        tenant_id="tenant-a",
        policy=policy,
        workspace_reference={
            "contract": "leaf.workspace.v1",
            "workspace_contract": "wrong-contract",
            "desired_platform_release": RELEASE,
        },
    )

    assert unknown.status is ReconcileStatus.FAILED
    assert mismatch.status is ReconcileStatus.FAILED
    assert unknown.reason_code == mismatch.reason_code == "invalid_workspace_reference"


def test_workspace_reference_validation_is_the_source_of_the_desired_release():
    repo = FakeRepository(EffectiveRelease("leaf-platform-2026.07.01", 7))
    branches = FakeBranches()
    result = reconciler(repo, True, branches).reconcile_workspace_reference(
        tenant_id="tenant-a",
        policy=load_policy(),
        workspace_reference={
            "contract": "leaf.workspace.v1",
            "workspace_contract": CONTRACT,
            "desired_platform_release": RELEASE,
        },
    )

    assert result.status is ReconcileStatus.UPDATED
    assert repo.effective.release == RELEASE
    assert branches.calls == []
