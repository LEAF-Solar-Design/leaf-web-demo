"""Focused authorization tests for the transport-free customization authority."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
from uuid import uuid4

import pytest


SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from customization_authority import (  # noqa: E402
    AuthorityError,
    BuilderEntitlement,
    CustomizationAuthority,
    HmacConfirmationSigner,
    InMemoryConfirmationRepository,
    PublishRequest,
    StaffAuthority,
    StagedChange,
    TenantBinding,
)


NOW = datetime(2026, 7, 23, 18, 0, tzinfo=timezone.utc)
SHA40 = "a" * 40
SHA64 = "b" * 64


def service(now=NOW):
    return CustomizationAuthority(HmacConfirmationSigner(b"test-only-hmac-key"), now=lambda: now)


def binding(subject="author", role="owner", tenant="tenant-a", verified=True):
    return TenantBinding(tenant_id=tenant, subject=subject, role=role, verified=verified)


def staged(author="author", high_impact=False):
    return StagedChange(
        tenant_id="tenant-a",
        change_set_id=str(uuid4()),
        staged_commit=SHA40,
        catalog_digest=SHA64,
        platform_release="platform@sha256:immutable",
        workspace_contract_digest=SHA64,
        author_subject=author,
        high_impact=high_impact,
    )


def request(change):
    return PublishRequest(
        change_set_id=change.change_set_id,
        staged_commit=change.staged_commit,
        catalog_digest=change.catalog_digest,
        platform_release=change.platform_release,
        workspace_contract_digest=change.workspace_contract_digest,
    )


def test_stage_requires_verified_owner_or_editor_and_matching_builder_entitlement():
    authority = service()
    grant = BuilderEntitlement("tenant-a", "author", enabled=True, verified=True)
    assert authority.authorize_stage(binding=binding(), builder_entitlement=grant).author_subject == "author"
    assert authority.authorize_stage(binding=binding(role="editor"), builder_entitlement=grant).tenant_id == "tenant-a"

    for role in ("reviewer", "read_only", "builder", "unknown", None):
        with pytest.raises(AuthorityError):
            authority.authorize_stage(binding=binding(role=role), builder_entitlement=grant)
    with pytest.raises(AuthorityError):
        authority.authorize_stage(
            binding=binding(verified=False), builder_entitlement=grant
        )
    with pytest.raises(AuthorityError):
        authority.authorize_stage(
            binding=TenantBinding("tenant-a", None, "owner", True),
            builder_entitlement=grant,
        )
    with pytest.raises(AuthorityError):
        authority.authorize_stage(  # type: ignore[arg-type]
            binding={"tenant_id": "tenant-a", "role": "owner"},
            builder_entitlement=grant,
        )
    with pytest.raises(AuthorityError):
        authority.authorize_stage(
            binding=binding(),
            builder_entitlement=BuilderEntitlement("tenant-a", "author", True, False),
        )
    with pytest.raises(AuthorityError):
        authority.authorize_stage(
            binding=binding(),
            builder_entitlement=BuilderEntitlement("tenant-a", "other", True, True),
        )


def test_confirmation_binds_exact_publish_fields_and_is_single_use():
    authority = service()
    change = staged()
    confirmation = authority.issue_publish_confirmation(
        staged_change=change,
        author_binding=binding(),
        approver_binding=binding(subject="reviewer", role="reviewer"),
    )
    approved = authority.consume_publish_confirmation(
        tenant_id="tenant-a", request=request(change), confirmation_id=confirmation.confirmation_id
    )
    assert approved.author_subject == "author"
    assert approved.approver_subject == "reviewer"
    with pytest.raises(AuthorityError, match="confirmation_replayed"):
        authority.consume_publish_confirmation(
            tenant_id="tenant-a", request=request(change), confirmation_id=confirmation.confirmation_id
        )


def test_confirmation_consume_is_atomic_under_a_race():
    authority = service()
    change = staged()
    confirmation = authority.issue_publish_confirmation(
        staged_change=change,
        author_binding=binding(),
        approver_binding=binding(subject="reviewer", role="reviewer"),
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def consume():
        barrier.wait()
        try:
            authority.consume_publish_confirmation(
                tenant_id="tenant-a",
                request=request(change),
                confirmation_id=confirmation.confirmation_id,
            )
            outcomes.append("ok")
        except AuthorityError as exc:
            outcomes.append(exc.reason_code)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["confirmation_replayed", "ok"]


def test_confirmation_rejects_cross_tenant_and_tampered_or_expired_records_without_consuming():
    authority = service()
    change = staged()
    confirmation = authority.issue_publish_confirmation(
        staged_change=change, author_binding=binding(),
        approver_binding=binding(subject="reviewer", role="reviewer"),
    )
    with pytest.raises(AuthorityError, match="confirmation_binding_mismatch"):
        authority.consume_publish_confirmation(
            tenant_id="tenant-b", request=request(change), confirmation_id=confirmation.confirmation_id
        )
    assert authority.consume_publish_confirmation(
        tenant_id="tenant-a", request=request(change), confirmation_id=confirmation.confirmation_id
    ).confirmation_id == confirmation.confirmation_id

    expiring = CustomizationAuthority(
        HmacConfirmationSigner(b"test-only-hmac-key"), now=lambda: NOW
    )
    expired_change = staged()
    expired = expiring.issue_publish_confirmation(
        staged_change=expired_change, author_binding=binding(),
        approver_binding=binding(subject="reviewer", role="reviewer"), ttl=timedelta(seconds=1),
    )
    expiring._now = lambda: NOW + timedelta(seconds=1)  # test a server clock advance
    with pytest.raises(AuthorityError, match="confirmation_expired"):
        expiring.consume_publish_confirmation(
            tenant_id="tenant-a", request=request(expired_change), confirmation_id=expired.confirmation_id
        )

    confirmation_store = InMemoryConfirmationRepository()
    tampered = CustomizationAuthority(
        HmacConfirmationSigner(b"test-only-hmac-key"),
        confirmations=confirmation_store,
        now=lambda: NOW,
    )
    tampered_change = staged()
    tampered_confirmation = tampered.issue_publish_confirmation(
        staged_change=tampered_change, author_binding=binding(),
        approver_binding=binding(subject="reviewer", role="reviewer"),
    )
    confirmation_store._records[
        tampered_confirmation.confirmation_id
    ].payload["catalog_digest"] = "c" * 64
    with pytest.raises(AuthorityError, match="confirmation_tampered"):
        tampered.consume_publish_confirmation(
            tenant_id="tenant-a", request=request(tampered_change),
            confirmation_id=tampered_confirmation.confirmation_id,
        )


def test_approval_requires_tenant_reviewer_or_owner_or_explicit_staff_operator():
    authority = service()
    change = staged()
    for role in ("editor", "read_only", "builder", None):
        with pytest.raises(AuthorityError):
            authority.issue_publish_confirmation(
                staged_change=change, author_binding=binding(),
                approver_binding=binding(subject="approver", role=role),
            )
    with pytest.raises(AuthorityError, match="approver_cross_tenant"):
        authority.issue_publish_confirmation(
            staged_change=change, author_binding=binding(),
            approver_binding=binding(subject="approver", role="owner", tenant="tenant-b"),
        )
    staff_confirmation = authority.issue_publish_confirmation(
        staged_change=change, author_binding=binding(),
        staff_authority=StaffAuthority(subject="staff-operator", operator=True, verified=True),
    )
    assert staff_confirmation.approver_subject == "staff-operator"
    with pytest.raises(AuthorityError, match="staff_authority_missing"):
        authority.issue_publish_confirmation(
            staged_change=change, author_binding=binding(),
            staff_authority=StaffAuthority(subject="staff-operator", operator=False, verified=True),
        )


def test_high_impact_publish_requires_distinct_author_and_approver_and_receipt_has_no_prompt_or_secret():
    authority = service()
    change = staged(high_impact=True)
    with pytest.raises(AuthorityError, match="high_impact_self_approval"):
        authority.issue_publish_confirmation(
            staged_change=change, author_binding=binding(), approver_binding=binding(role="owner"),
        )
    confirmation = authority.issue_publish_confirmation(
        staged_change=change, author_binding=binding(),
        approver_binding=binding(subject="other-owner", role="owner"),
    )
    assert "prompt" not in vars(confirmation)
    assert "secret" not in vars(confirmation)
