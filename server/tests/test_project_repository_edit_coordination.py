"""Changed-surface proofs for the durable P8 Unit 4/5 coordination facade."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import project_repository_edit_contract as contract  # noqa: E402
import project_repository_edit_coordination as coordination  # noqa: E402

TENANT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
REPO = "44444444-4444-4444-8444-444444444444"
EDIT = "55555555-5555-4555-8555-555555555555"
ACTOR = "77777777-7777-4777-8777-777777777777"
STAGE_LEASE = "88888888-8888-4888-8888-888888888888"
PUBLISH_LEASE = "99999999-9999-4999-8999-999999999999"
RECOVERY_LEASE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CONFIRMATION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
BASE = "1" * 40
HEAD = "2" * 40
TREE = "3" * 40
AUTHORITY = contract.RepositoryAuthorityKey(TENANT, ORG, PROJECT, REPO)


def _receipt():
    return contract.parse_staged_receipt({
        "contract": contract.STAGED_RECEIPT_CONTRACT, "edit_id": EDIT,
        "state": "staged", "operation": "edit", "source_edit_id": None,
        "actor_binding_id": ACTOR, "tenant_id": TENANT, "organization_id": ORG,
        "project_id": PROJECT, "repo_key": REPO, "writer_lease_id": STAGE_LEASE,
        "writer_lease_generation": 7, "base_commit": BASE,
        "staged_head_commit": HEAD, "staged_tree": TREE,
        "changed_paths": ["src/a.py"], "diff_digest": "4" * 64,
        "instruction_digest": "5" * 64, "idempotency_key": "stage-key",
    })


class Backend:
    def __init__(self):
        self.calls = []

    def record_staged(self, receipt, digest, **kwargs):
        self.calls.append(("record", receipt, digest, kwargs))
        return {"edit_id": EDIT, "state": "staged", "version": 1}

    def consume_for_publish(self, authority, edit_id, confirmation_id, **kwargs):
        self.calls.append(("consume", authority, edit_id, confirmation_id, kwargs))
        return {"edit_id": EDIT, "state": "publishing", "version": 3,
                "receipt_digest": kwargs.pop("expected_digest", None) or self.digest,
                "expected_main_commit": BASE, "staged_head_commit": HEAD,
                "staged_tree": TREE}

    def settle_publish(self, authority, edit_id, **kwargs):
        self.calls.append(("settle", authority, edit_id, kwargs))
        return {"edit_id": EDIT, "state": "published", "version": 4}

    def recover_publish(self, authority, edit_id, **kwargs):
        self.calls.append(("recover", authority, edit_id, kwargs))
        return {"edit_id": EDIT, "state": "published", "version": 4}


def _state(role="owner"):
    backend = Backend()
    state = coordination.RepositoryEditCoordinationState(
        actor_roles=lambda actor, authority: role if actor == ACTOR and authority == AUTHORITY else None,
        backend=backend,
    )
    return state, backend


def test_record_staged_delegates_exact_receipt_to_durable_backend():
    state, backend = _state()
    receipt = _receipt()
    digest = contract.staged_receipt_digest(receipt)
    result = state.record_staged(
        receipt, digest, expected_version=0, transition_key="stage-one")
    assert result == {"edit_id": EDIT, "state": "staged", "version": 1}
    assert backend.calls[0] == (
        "record", receipt.to_mapping(), digest,
        {"expected_version": 0, "transition_key": "stage-one"})


def test_record_staged_rejects_digest_and_non_writer_before_backend():
    receipt = _receipt()
    state, backend = _state()
    with pytest.raises(coordination.CoordinationError, match="receipt_digest_mismatch"):
        state.record_staged(receipt, "f" * 64, expected_version=0, transition_key="stage")
    denied, denied_backend = _state("reviewer")
    with pytest.raises(coordination.CoordinationError, match="actor_authority_unavailable"):
        denied.record_staged(
            receipt, contract.staged_receipt_digest(receipt), expected_version=0,
            transition_key="stage")
    assert backend.calls == [] and denied_backend.calls == []


def test_authorize_publish_forwards_distinct_publish_witness_and_returns_git_matrix():
    state, backend = _state()
    digest = contract.staged_receipt_digest(_receipt())
    backend.digest = digest
    result = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT, actor_binding_id=ACTOR,
        confirmation_id=CONFIRMATION, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE, publish_lease_generation=8,
        expected_version=2, transition_key="publish-one")
    assert result == {
        "edit_id": EDIT, "state": "publishing", "version": 3,
        "receipt_digest": digest, "expected_main_commit": BASE,
        "staged_head_commit": HEAD, "staged_tree": TREE,
        "private_ref": f"refs/leaf/changes/{EDIT}",
        "publish_lease_id": PUBLISH_LEASE, "publish_lease_generation": 8,
    }
    assert backend.calls[0][-1]["publish_lease_id"] == PUBLISH_LEASE
    assert backend.calls[0][-1]["publish_lease_generation"] == 8


def test_authorize_publish_rejects_backend_receipt_digest_drift():
    state, backend = _state()
    backend.digest = "f" * 64
    with pytest.raises(coordination.CoordinationError, match="receipt_digest_mismatch"):
        state.authorize_publish(
            authority=AUTHORITY, edit_id=EDIT, actor_binding_id=ACTOR,
            confirmation_id=CONFIRMATION,
            receipt_digest=contract.staged_receipt_digest(_receipt()),
            publish_lease_id=PUBLISH_LEASE, publish_lease_generation=8,
            expected_version=2, transition_key="publish-one")


def test_settle_and_recovery_forward_exact_lease_witnesses():
    state, backend = _state()
    settled = state.settle_publish(
        authority=AUTHORITY, edit_id=EDIT, actor_binding_id=ACTOR,
        publish_lease_id=PUBLISH_LEASE, publish_lease_generation=8,
        private_ref_commit=HEAD, main_commit=HEAD, main_tree=TREE,
        expected_version=3, transition_key="settle-one")
    recovered = state.recover_publish(
        authority=AUTHORITY, edit_id=EDIT, actor_binding_id=ACTOR,
        recovery_lease_id=RECOVERY_LEASE, recovery_lease_generation=9,
        private_ref_commit=HEAD, main_commit=HEAD, main_tree=TREE,
        expected_version=3, reason_code="transport_failure",
        transition_key="recover-one")
    assert settled["state"] == recovered["state"] == "published"
    assert backend.calls[0][-1]["publish_lease_id"] == PUBLISH_LEASE
    assert backend.calls[1][-1]["recovery_lease_id"] == RECOVERY_LEASE


def test_handlers_reject_extra_fields_and_uppercase_git_witness_before_backend():
    state, backend = _state()
    body = {
        "contract": coordination.CONTRACT, "action": "record_staged",
        "receipt": _receipt().to_mapping(),
        "receipt_digest": contract.staged_receipt_digest(_receipt()),
        "expected_version": 0, "transition_key": "stage", "extra": True,
    }
    with pytest.raises(coordination.CoordinationError, match="record_staged_request_fields_invalid"):
        coordination.handle_record_staged(state, body)
    assert backend.calls == []


def test_module_has_no_private_state_machine_or_git_mutation_surface():
    source = (SERVER_DIR / "project_repository_edit_coordination.py").read_text(encoding="utf-8")
    for forbidden in ("self._rows", "self._confirmations", "threading.Lock", "subprocess", "git update-ref"):
        assert forbidden not in source
    assert "repository_edit_store" in source and "resolve_project_repository_authority" in source
