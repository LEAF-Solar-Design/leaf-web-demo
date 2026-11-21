"""Inline decisions through the JWT author routes and real publication continuation."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import customization_service
import deps
from branch_tools.dev_catalog import read_catalog
from branch_tools.dev_publish import DevPublishError
from customization_authority import AuthorityError, StaffAuthority
from customization_models import ChangeState
from customization_service import CustomizationService, _binding
from routers import author
from test_customization_authority import binding, service as authority_service, staged
from test_customization_branch_target import NAME, _change, _request, lane
from test_customization_runtime import staged_change


REVIEWS = "/api/author/reviews"


@pytest.fixture
def review(lane, tmp_path, monkeypatch):
    staged_result = lane.service.stage(**_request(lane))
    lane.change = _change(lane, staged_result["receipt"])
    lane.key = dict(tenant_id="tenant-a", change_set_id=lane.change.change_set_id,
                    staged_commit=lane.change.staged_commit)
    lane.body = dict(change_set_id=lane.change.change_set_id, staged_commit=lane.change.staged_commit,
                     catalog_digest=lane.change.catalog_digest, decision="approve")
    monkeypatch.delattr(lane.service, "_authority")
    monkeypatch.setattr(customization_service, "_binding", _binding)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LEAF_CUSTOMIZATION_ALLOW_STATIC_BINDINGS", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_CONFIRMATION_KEY", "inline-review-test-key")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "tenant-a")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_STUDIO_PLAN_MODE", "all")
    monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", "internal")
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS", "1")
    lane.root = tmp_path / "dev"
    monkeypatch.setenv("LEAF_BRANCH_DEV_TOOLS_DIR", str(lane.root))
    monkeypatch.setattr(lane.service, "_publication_policy_state", lambda _: (True, True))
    monkeypatch.setattr(CustomizationService, "configured", lambda: lane.service)
    monkeypatch.setattr(author, "_review_calls", OrderedDict())
    app = FastAPI()
    app.include_router(author.router)
    client = TestClient(app)

    def viewer(subject, role, tenant_id="tenant-a"):
        principal = deps.TenantContext(tenant_id, subject=subject)
        app.dependency_overrides[deps.require_tenant] = lambda: principal
        monkeypatch.setenv("LEAF_CUSTOMIZATION_TENANT_BINDINGS", json.dumps({
            tenant_id: {"subject": subject, "role": role},
        }))

    lane.viewer = viewer
    viewer("auth0|reviewer", "reviewer")
    lane.post = lambda **updates: client.post(REVIEWS, json=dict(lane.body, **updates))
    lane.context = lambda: client.get(REVIEWS + "/context", params={"change_set_id": lane.change.change_set_id})
    return lane


def decisions(lane):
    with lane.store._connection() as conn:
        rows = conn.execute("SELECT * FROM customization_review_decisions").fetchall()
        audits = conn.execute(
            "SELECT payload_json FROM customization_audit_events WHERE reason_code = 'review_decided'"
        ).fetchall()
    return [dict(row) for row in rows], [json.loads(row["payload_json"]) for row in audits]


def refused(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["reason_code"] == code


@pytest.mark.parametrize(("subject", "role", "kind"), [
    ("auth0|reviewer", "reviewer", "independent"),
    ("auth0|author", "owner", "self"),
])
def test_approval_publishes_with_signed_attribution(review, subject, role, kind, monkeypatch):
    review.viewer(subject, role)
    observed = []
    publish = CustomizationService._publish

    def capture(service, **kwargs):
        observed.append((str(kwargs["tenant"]), kwargs["tenant"].subject, kwargs["require_actor_binding"]))
        return publish(service, **kwargs)

    monkeypatch.setattr(CustomizationService, "_publish", capture)
    assert review.context().json()["approval_kind_if_approved"] == kind
    result = review.post()
    assert result.status_code == 200, result.text
    (row,), (audit,) = decisions(review)
    assert result.json() == {
        "contract": "leaf.customization.v1", "change_set_id": review.change.change_set_id,
        "decision": "approve", "status": "published", "approval_kind": kind, "approver_role": role,
        "decided_at": row["decided_at"], "name": NAME, "version": "1.2.0",
    }
    assert observed == [("tenant-a", review.change.author_subject, False)]
    assert _change(review, review.body).state is ChangeState.PUBLISHED
    assert row == review.store.get_review_decision(**review.key)
    confirmation = review.store.get_confirmation(confirmation_id=row["confirmation_id"])
    assert confirmation["consumed"] and confirmation["payload"]["approval_kind"] == kind
    assert confirmation["payload"]["approver_subject"] == row["approver_subject"] == subject
    assert audit["event"] == "review_decided"
    for key in (*review.key, "approval_kind", "approver_role", "approver_subject", "decision"):
        assert audit[key] == row[key]
    assert audit["comment_len"] == 0 and "comment" not in audit
    metadata = {"status": "reviewed", "approval": kind, "approver_role": role}
    assert (review.root / NAME / "1.2.0" / "review.json").read_text() == json.dumps(metadata, sort_keys=True)
    entry, = read_catalog()
    assert (entry.metadata["review"], entry.metadata["approval"], entry.metadata["approver_role"]) == (
        "reviewed", kind, role,
    )
    refused(review.post(decision="request_changes"), 409, "review_already_decided")
    assert decisions(review) == ([row], [audit])


def test_self_refused_in_all_mode_and_a_different_owner_can_publish(review, monkeypatch):
    monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", "all")
    review.viewer("auth0|author", "owner")
    assert review.context().json() == {
        "viewer_may_approve": False, "approval_kind_if_approved": None, "review": None,
        "flags": {"plan_mode": True, "inline_review": True, "self_approval": False},
    }
    refused(review.post(), 403, "self_approval_not_allowed")
    assert decisions(review) == ([], [])
    review.viewer("auth0|other", "owner")
    assert review.post().json()["approval_kind"] == "independent"


def test_studio_approval_advances_effective_catalog(review, monkeypatch):
    change = staged_change(review.store)
    monkeypatch.setattr(review.service, "_verify_catalog", lambda *args: None)
    monkeypatch.setattr(review.service, "_harness_publish", lambda row: row.staged_commit)
    result = review.post(change_set_id=change.change_set_id, staged_commit=change.staged_commit,
                         catalog_digest=change.catalog_digest)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "published"
    assert set(result.json()) == {
        "contract", "change_set_id", "decision", "approval_kind", "status", "decided_at", "approver_role",
    }
    assert review.store.get_effective_catalog(tenant_id="tenant-a").change_set_id == change.change_set_id


@pytest.mark.parametrize(("case", "status", "code"), [
    ("editor", 403, "approver_role_denied"), ("read_only", 403, "approver_role_denied"),
    ("foreign", 409, "review_target_stale"), ("commit", 409, "review_target_stale"),
    ("digest", 409, "review_target_stale"), ("state", 409, "review_target_stale"),
    ("off", 404, "inline_review_disabled"), ("r6", 404, "customization_publish_disabled"),
])
def test_rejections_leave_no_decision_or_audit(review, monkeypatch, case, status, code):
    if case in {"editor", "read_only"}:
        review.viewer("auth0|other", case)
        refused(review.context(), 403, code)
    elif case == "foreign":
        monkeypatch.setenv("LEAF_STUDIO_INLINE_REVIEW", "all")
        review.viewer("auth0|owner", "owner", "tenant-b")
    elif case == "commit":
        review.body["staged_commit"] = "0" * 40
    elif case == "digest":
        review.body["catalog_digest"] = "stale"
    elif case == "state":
        review.store.transition(tenant_id="tenant-a", change_set_id=review.change.change_set_id,
                                expected_version=review.change.version, next_state=ChangeState.REJECTED,
                                idempotency_key="rejected")
    else:
        flag = "LEAF_STUDIO_INLINE_REVIEW" if case == "off" else "LEAF_CUSTOMIZATION_R6_MODE"
        monkeypatch.setenv(flag, "off")
        assert review.context().json()["viewer_may_approve"] is False
    refused(review.post(), status, code)
    assert decisions(review) == ([], [])
    assert not review.root.exists()


def test_editor_author_reads_but_cannot_decide(review):
    review.viewer("auth0|author", "editor")
    context = review.context()
    assert context.status_code == 200
    assert context.json()["viewer_may_approve"] is False
    assert context.json()["approval_kind_if_approved"] is None
    refused(review.post(), 403, "approver_role_denied")


def test_denial_records_clean_comment_and_minimal_context(review):
    response = review.post(decision="request_changes", comment="more\t\x00detail\nplease\x7f")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "denied"
    (row,), (event,) = decisions(review)
    assert row["comment"] == "moredetail\nplease" and row["confirmation_id"] is None
    assert event["decision"] == "request_changes" and event["comment_len"] == len(row["comment"])
    assert "comment" not in event and "moredetail" not in json.dumps(event)
    request = review.store.get_or_create_publication_request(tenant_id="tenant-a", change_set_id=row["change_set_id"])
    assert (request["status"], request["reason_code"]) == ("denied", "reviewer_requested_changes")
    assert review.context().json() == {
        "viewer_may_approve": False, "approval_kind_if_approved": None,
        "review": {key: result[key] for key in ("approval_kind", "decision", "decided_at", "approver_role")},
        "flags": {"inline_review": True, "plan_mode": True, "self_approval": True},
    }
    refused(review.post(), 409, "review_already_decided")
    with review.store._connection() as conn:
        for sql in ("DELETE FROM customization_review_decisions",
                    "UPDATE customization_review_decisions SET comment = NULL"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(sql)


def test_publish_failure_leaves_staged_and_later_review_can_succeed(review, monkeypatch):
    def fail(*args, **kwargs):
        raise DevPublishError("branch_dev_catalog_unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(customization_service, "publish_branch_package", fail)
        refused(review.post(), 503, "branch_dev_catalog_unavailable")
    assert decisions(review) == ([], [])
    assert _change(review, review.body) == review.change
    with review.store._connection() as conn:
        receipt, = conn.execute("SELECT consumed FROM customization_confirmations").fetchall()
        assert receipt["consumed"] == 0
    assert review.store.find_unconsumed_confirmation(
        tenant_id="tenant-a", change_set_id=review.change.change_set_id,
    ) is None
    assert review.context().json()["viewer_may_approve"]
    assert review.post().json()["status"] == "published"


def test_concurrent_opposite_decisions_share_one_winner(review):
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda decision: review.post(decision=decision), ["approve", "request_changes"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    losing, = [response for response in responses if response.status_code == 409]
    refused(losing, 409, "review_already_decided")
    (row,), (event,) = decisions(review)
    assert row["decision"] == event["decision"]
    assert review.root.exists() == (row["decision"] == "approve")


def test_failed_audit_rolls_back_denial_and_decision(review):
    with review.store._connection() as conn:
        conn.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON customization_audit_events
                        WHEN NEW.reason_code = 'review_decided'
                        BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    refused(review.post(decision="request_changes"), 503, "customization_review_failed")
    assert decisions(review) == ([], [])
    with review.store._connection() as conn:
        assert conn.execute("SELECT * FROM customization_publication_requests").fetchone() is None
        conn.execute("DROP TRIGGER fail_audit")
    assert review.post().json()["status"] == "published"


@pytest.mark.parametrize("invalid", [
    {"approver_role": "owner"}, {"decision": "reject"}, {"change_set_id": "bad"},
    {"staged_commit": "f" * 39}, {"staged_commit": "z" * 40},
    {"catalog_digest": "x" * 129}, {"comment": "x" * 501},
])
def test_review_body_is_closed_and_bounded(review, invalid):
    assert review.post(**invalid).status_code == 422
    assert decisions(review) == ([], [])


def test_rate_limit_is_per_subject(review):
    for _ in range(30):
        refused(review.post(catalog_digest="old"), 409, "review_target_stale")
    refused(review.post(), 429, "review_rate_limited")
    review.viewer("auth0|different", "owner")
    assert review.post().status_code == 200


@pytest.mark.parametrize(("kind", "approver", "staff"), [
    ("other", binding(), None), ("independent", binding(), None),
    ("self", binding(subject="someone-else"), None), ("self", None, StaffAuthority("author", True, True)),
    (None, binding(), None),
])
def test_self_exception_requires_exact_authority(kind, approver, staff):
    with pytest.raises(AuthorityError, match="approval_kind_invalid"):
        authority_service().issue_publish_confirmation(
            staged_change=staged(high_impact=True), author_binding=binding(), approver_binding=approver,
            staff_authority=staff, allow_self_approval=True, approval_kind=kind,
        )
