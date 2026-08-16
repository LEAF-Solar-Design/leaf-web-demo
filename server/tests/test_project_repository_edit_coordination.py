"""Changed-surface acceptance for the P8 Unit 4/5 server coordination module."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import project_repository_edit_contract as contract  # noqa: E402
import project_repository_edit_coordination as coordination  # noqa: E402


TENANT_ID = "11111111-1111-4111-8111-111111111111"
ORG_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
REPO_KEY = "44444444-4444-4444-8444-444444444444"
FOREIGN_REPO_KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
EDIT_ID = "55555555-5555-4555-8555-555555555555"
ACTOR_ID = "77777777-7777-4777-8777-777777777777"
REVIEWER_ACTOR_ID = "77777777-7777-4777-8777-777777777778"
APPROVER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
STAGE_LEASE_ID = "88888888-8888-4888-8888-888888888888"
PUBLISH_LEASE_ID = "99999999-9999-4999-8999-999999999999"
RECOVERY_LEASE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CONFIRMATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

BASE_COMMIT = "1" * 40
STAGED_HEAD_COMMIT = "2" * 40
STAGED_TREE = "3" * 40
DIFF_DIGEST = "4" * 64
INSTRUCTION_DIGEST = "5" * 64

STAGE_LEASE_GENERATION = 7

AUTHORITY = contract.RepositoryAuthorityKey(TENANT_ID, ORG_ID, PROJECT_ID, REPO_KEY)


def _staged(**overrides):
    value = {
        "contract": contract.STAGED_RECEIPT_CONTRACT,
        "edit_id": EDIT_ID,
        "state": "staged",
        "operation": "edit",
        "source_edit_id": None,
        "actor_binding_id": ACTOR_ID,
        "tenant_id": TENANT_ID,
        "organization_id": ORG_ID,
        "project_id": PROJECT_ID,
        "repo_key": REPO_KEY,
        "writer_lease_id": STAGE_LEASE_ID,
        "writer_lease_generation": STAGE_LEASE_GENERATION,
        "base_commit": BASE_COMMIT,
        "staged_head_commit": STAGED_HEAD_COMMIT,
        "staged_tree": STAGED_TREE,
        "changed_paths": ["src/a.py"],
        "diff_digest": DIFF_DIGEST,
        "instruction_digest": INSTRUCTION_DIGEST,
        "idempotency_key": "stage-key",
    }
    value.update(overrides)
    return value


def _confirmation(receipt, **overrides):
    value = {
        "contract": contract.CONFIRMATION_CONTRACT,
        "confirmation_id": CONFIRMATION_ID,
        "receipt_digest": contract.staged_receipt_digest(receipt),
        "approver_binding_id": APPROVER_ID,
        "tenant_id": TENANT_ID,
        "organization_id": ORG_ID,
        "project_id": PROJECT_ID,
        "repo_key": REPO_KEY,
        "edit_id": EDIT_ID,
        "writer_lease_id": STAGE_LEASE_ID,
        "writer_lease_generation": STAGE_LEASE_GENERATION,
        "staged_tree": STAGED_TREE,
        "issued_at": "2026-08-14T07:00:00Z",
        "expires_at": "2026-08-14T07:05:00Z",
    }
    value.update(overrides)
    return value


def _resolver(roles):
    def resolve(actor_binding_id, authority):
        if authority != AUTHORITY:
            return None
        return roles.get(actor_binding_id)
    return resolve


def _state(roles=None):
    roles = roles if roles is not None else {ACTOR_ID: "writer"}
    return coordination.RepositoryEditCoordinationState(actor_roles=_resolver(roles))


def _staged_row(state, **overrides):
    receipt = contract.parse_staged_receipt(_staged(**overrides))
    digest = contract.staged_receipt_digest(receipt)
    state.record_staged(receipt, digest, expected_version=0)
    return receipt, digest


def _confirm(state, receipt, **overrides):
    confirmation = contract.parse_confirmation(_confirmation(receipt, **overrides))
    state.put_confirmation(confirmation)
    return confirmation


def test_record_staged_persists_the_receipt_and_binds_its_digest():
    state = _state()
    receipt, digest = _staged_row(state)

    row = state._rows[EDIT_ID]
    assert row.state == "staged"
    assert row.version == 1
    assert row.receipt is receipt
    assert row.receipt_digest == digest


def test_record_staged_rejects_receipt_digest_mismatch_before_persisting():
    state = _state()
    receipt = contract.parse_staged_receipt(_staged())
    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.record_staged(receipt, "f" * 64, expected_version=0)
    assert excinfo.value.code == "receipt_digest_mismatch"
    assert EDIT_ID not in state._rows


def test_record_staged_rejects_non_writer_roles():
    state = _state(roles={ACTOR_ID: "reviewer"})
    receipt = contract.parse_staged_receipt(_staged())
    digest = contract.staged_receipt_digest(receipt)
    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.record_staged(receipt, digest, expected_version=0)
    assert excinfo.value.code == "actor_authority_unavailable"
    assert EDIT_ID not in state._rows


def test_authorize_publish_atomically_consumes_confirmation_and_records_new_lease():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)

    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )

    assert matrix["state"] == "publishing"
    assert matrix["version"] == 2
    assert matrix["expected_main_commit"] == BASE_COMMIT
    assert matrix["staged_head_commit"] == STAGED_HEAD_COMMIT
    assert matrix["staged_tree"] == STAGED_TREE
    assert matrix["private_ref"] == f"refs/leaf/changes/{EDIT_ID}"
    assert matrix["publish_lease_id"] == PUBLISH_LEASE_ID
    assert matrix["publish_lease_generation"] == STAGE_LEASE_GENERATION + 1

    # The one-use confirmation is gone; the staged witness is untouched.
    assert CONFIRMATION_ID not in state._confirmations
    row = state._rows[EDIT_ID]
    assert row.receipt.writer_witness.writer_lease_id == STAGE_LEASE_ID
    assert row.receipt.writer_witness.writer_lease_generation == STAGE_LEASE_GENERATION


def test_authorize_publish_exact_retry_is_read_only_and_does_not_reconsume():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    request = dict(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )

    first = state.authorize_publish(**request)
    retry = state.authorize_publish(**request)

    assert retry == first
    assert state._rows[EDIT_ID].version == 2


def test_authorize_publish_rejects_non_increasing_publish_generation():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.authorize_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
            publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION,
            expected_version=1,
        )
    assert excinfo.value.code == "publish_generation_not_strictly_greater"
    assert state._rows[EDIT_ID].state == "staged"
    assert CONFIRMATION_ID in state._confirmations


def test_authorize_publish_rejects_a_confirmation_bound_to_a_drifted_stage_lease():
    state = _state()
    receipt, digest = _staged_row(state)
    # The confirmation was minted for a DIFFERENT stage lease generation than
    # the one the persisted receipt actually carries.
    _confirm(state, receipt, writer_lease_generation=STAGE_LEASE_GENERATION + 5)

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.authorize_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
            publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
            expected_version=1,
        )
    assert excinfo.value.code == "confirmation_binding_mismatch"
    assert CONFIRMATION_ID in state._confirmations


def test_authorize_publish_rejects_a_foreign_authority_tuple():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    foreign = contract.RepositoryAuthorityKey(TENANT_ID, ORG_ID, PROJECT_ID, FOREIGN_REPO_KEY)

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.authorize_publish(
            authority=foreign, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
            publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
            expected_version=1,
        )
    assert excinfo.value.code == "actor_authority_unavailable"


def test_authorize_publish_rejects_receipt_digest_mismatch():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.authorize_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            confirmation_id=CONFIRMATION_ID, receipt_digest="f" * 64,
            publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
            expected_version=1,
        )
    assert excinfo.value.code == "receipt_digest_mismatch"


def test_authorize_publish_rejects_duplicate_publication():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )
    state.settle_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=matrix["version"],
    )

    second_confirmation_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    _confirm(state, receipt, confirmation_id=second_confirmation_id)
    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.authorize_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            confirmation_id=second_confirmation_id, receipt_digest=digest,
            publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 2,
            expected_version=matrix["version"],
        )
    assert excinfo.value.code == "edit_not_staged"


def test_settle_publish_rejects_a_git_observation_that_drifted_from_the_receipt():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.settle_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            publish_lease_id=PUBLISH_LEASE_ID,
            publish_lease_generation=STAGE_LEASE_GENERATION + 1,
            private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
            main_tree="6" * 40, expected_version=matrix["version"],
        )
    assert excinfo.value.code == "settlement_observation_mismatch"
    assert state._rows[EDIT_ID].state == "publishing"


def test_settle_publish_exact_already_published_retry_is_read_only():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )
    settle_kwargs = dict(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=matrix["version"],
    )

    first = state.settle_publish(**settle_kwargs)
    retry_kwargs = {**settle_kwargs, "expected_version": first["version"]}
    retry = state.settle_publish(**retry_kwargs)

    assert first["state"] == "published"
    assert retry == first


def test_recovery_resumes_a_settlement_transport_failure_without_a_second_cas():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )

    # Settlement's transport failed; the harness already observed and froze
    # the Git matrix. Recovery resumes from it with a strictly newer lease,
    # no second compare-and-swap and no new confirmation.
    result = state.recover_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        recovery_lease_id=RECOVERY_LEASE_ID,
        recovery_lease_generation=STAGE_LEASE_GENERATION + 2,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=matrix["version"],
        reason_code="settlement_transport_failure",
    )

    assert result["state"] == "published"
    row = state._rows[EDIT_ID]
    assert row.recovery_witness == coordination.PublishLeaseWitness(
        RECOVERY_LEASE_ID, STAGE_LEASE_GENERATION + 2)


def test_recovery_rejects_a_stale_recovery_generation():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.recover_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            recovery_lease_id=RECOVERY_LEASE_ID,
            recovery_lease_generation=STAGE_LEASE_GENERATION,
            private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
            main_tree=STAGED_TREE, expected_version=matrix["version"],
            reason_code="settlement_transport_failure",
        )
    assert excinfo.value.code == "recovery_generation_stale"
    assert state._rows[EDIT_ID].state == "publishing"


def test_recovery_after_publication_is_observation_only_and_rejects_git_drift():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )
    state.settle_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=matrix["version"],
    )
    published_version = state._rows[EDIT_ID].version

    resumed = state.recover_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        recovery_lease_id=RECOVERY_LEASE_ID,
        recovery_lease_generation=STAGE_LEASE_GENERATION + 3,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=published_version,
        reason_code="observed_after_publish",
    )
    assert resumed["state"] == "published"
    assert state._rows[EDIT_ID].version == published_version

    with pytest.raises(coordination.CoordinationError) as excinfo:
        state.recover_publish(
            authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
            recovery_lease_id=RECOVERY_LEASE_ID,
            recovery_lease_generation=STAGE_LEASE_GENERATION + 4,
            private_ref_commit=STAGED_HEAD_COMMIT, main_commit="6" * 40,
            main_tree=STAGED_TREE, expected_version=published_version,
            reason_code="observed_after_publish",
        )
    assert excinfo.value.code == "recovery_observation_mismatch"
    assert state._rows[EDIT_ID].version == published_version


def test_forward_rollback_is_a_fresh_staged_edit_with_its_own_receipt():
    state = _state()
    receipt, digest = _staged_row(state)
    _confirm(state, receipt)
    matrix = state.authorize_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        confirmation_id=CONFIRMATION_ID, receipt_digest=digest,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        expected_version=1,
    )
    state.settle_publish(
        authority=AUTHORITY, edit_id=EDIT_ID, actor_binding_id=ACTOR_ID,
        publish_lease_id=PUBLISH_LEASE_ID, publish_lease_generation=STAGE_LEASE_GENERATION + 1,
        private_ref_commit=STAGED_HEAD_COMMIT, main_commit=STAGED_HEAD_COMMIT,
        main_tree=STAGED_TREE, expected_version=matrix["version"],
    )

    rollback_edit_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    rollback_receipt, rollback_digest = _staged_row(
        state,
        edit_id=rollback_edit_id, operation="rollback", source_edit_id=EDIT_ID,
        writer_lease_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        writer_lease_generation=1,
        base_commit=STAGED_HEAD_COMMIT, staged_head_commit="7" * 40, staged_tree="8" * 40,
    )

    rollback_row = state._rows[rollback_edit_id]
    assert rollback_row.state == "staged"
    assert rollback_row.version == 1
    # The original published edit's row is untouched by the rollback's stage.
    assert state._rows[EDIT_ID].state == "published"
    assert state._rows[EDIT_ID].main_commit == STAGED_HEAD_COMMIT


def test_handlers_reject_extra_wire_fields_before_any_state_change():
    state = _state()
    receipt = contract.parse_staged_receipt(_staged())
    digest = contract.staged_receipt_digest(receipt)
    body = {
        "contract": coordination.CONTRACT,
        "action": "record_staged",
        "receipt": _staged(),
        "receipt_digest": digest,
        "expected_version": 0,
        "transition_key": "stage-key",
        "repository_path": "/tmp/repo",
    }

    with pytest.raises(coordination.CoordinationError) as excinfo:
        coordination.handle_record_staged(state, body)
    assert excinfo.value.code == "record_staged_request_fields_invalid"
    assert EDIT_ID not in state._rows


def test_handlers_reject_malformed_uppercase_hex_values():
    state = _state()
    # Uppercasing a hex digest with letters (not digits) is a real change --
    # a fixture built from an all-digit value would silently no-op here.
    body = {
        "contract": coordination.CONTRACT,
        "action": "record_staged",
        "receipt": _staged(),
        "receipt_digest": "ABCDEF" + "1" * 58,
        "expected_version": 0,
        "transition_key": "stage-key",
    }
    assert body["receipt_digest"] != body["receipt_digest"].lower()

    with pytest.raises(coordination.CoordinationError) as excinfo:
        coordination.handle_record_staged(state, body)
    assert excinfo.value.code == "invalid_receipt_digest"
    assert EDIT_ID not in state._rows


def test_handler_success_round_trips_the_closed_wire_envelope():
    state = _state()
    receipt = contract.parse_staged_receipt(_staged())
    digest = contract.staged_receipt_digest(receipt)
    body = {
        "contract": coordination.CONTRACT,
        "action": "record_staged",
        "receipt": _staged(),
        "receipt_digest": digest,
        "expected_version": 0,
        "transition_key": "stage-key",
    }

    response = coordination.handle_record_staged(state, body)

    assert response == {
        "contract": coordination.CONTRACT,
        "action": "record_staged",
        "edit_id": EDIT_ID,
        "state": "staged",
        "version": 1,
    }


def test_errors_never_carry_receipt_or_path_content():
    state = _state()
    body = {
        "contract": coordination.CONTRACT,
        "action": "record_staged",
        "receipt": {**_staged(), "sneaky_repository_path": "D:/tenants/secret-repo"},
        "receipt_digest": "f" * 64,
        "expected_version": 0,
        "transition_key": "stage-key",
    }

    with pytest.raises(coordination.CoordinationError) as excinfo:
        coordination.handle_record_staged(state, body)
    assert str(excinfo.value) == "invalid_receipt"
    assert "secret-repo" not in str(excinfo.value)
    assert "D:/tenants" not in str(excinfo.value)
