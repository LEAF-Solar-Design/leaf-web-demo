"""PostgreSQL state-machine proofs for dormant P8 repository edits."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

from leaf_platform import db, repository_edit_store, store

SERVER = Path(__file__).resolve().parents[2] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from project_repository_edit_contract import (  # noqa: E402
    CONFIRMATION_CONTRACT,
    STAGED_RECEIPT_CONTRACT,
    parse_confirmation,
    parse_staged_receipt,
    staged_receipt_digest,
)


def _ids():
    return {name: str(uuid.uuid4()) for name in (
        "edit", "source", "actor", "approver", "lease", "confirmation", "repo")}


def _receipt(org, project, ids, **changes):
    raw = {
        "contract": STAGED_RECEIPT_CONTRACT, "edit_id": ids["edit"], "state": "staged",
        "operation": "edit", "source_edit_id": None, "actor_binding_id": ids["actor"],
        "tenant_id": str(org.org_id), "organization_id": str(org.org_id),
        "project_id": str(project.project_id), "repo_key": ids["repo"],
        "writer_lease_id": ids["lease"], "writer_lease_generation": 7,
        "base_commit": "1" * 40, "staged_head_commit": "2" * 40,
        "staged_tree": "3" * 40, "changed_paths": ["src/a.py"],
        "diff_digest": "4" * 64, "instruction_digest": "5" * 64,
        "idempotency_key": "stage-one",
    }
    raw.update(changes)
    return parse_staged_receipt(raw)


def _confirmation(receipt, ids):
    now = datetime.now(timezone.utc)
    return parse_confirmation({
        "contract": CONFIRMATION_CONTRACT, "confirmation_id": ids["confirmation"],
        "receipt_digest": staged_receipt_digest(receipt),
        "approver_binding_id": ids["approver"],
        "tenant_id": receipt.authority_key.tenant_id,
        "organization_id": receipt.authority_key.organization_id,
        "project_id": receipt.authority_key.project_id,
        "repo_key": receipt.authority_key.repo_key, "edit_id": receipt.edit_id,
        "writer_lease_id": receipt.writer_witness.writer_lease_id,
        "writer_lease_generation": receipt.writer_witness.writer_lease_generation,
        "staged_tree": receipt.staged_tree,
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
    })


def test_migration_0043_owns_closed_state_confirmation_and_append_only_audit():
    sql = (Path(__file__).resolve().parent.parent / "migrations" /
           "0043_project_repository_edit_state.sql").read_text(encoding="utf-8")
    for table in ("project_repository_edits", "project_repository_edit_confirmations",
                  "project_repository_edit_audit_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "expected_main_commit = base_commit" in sql
    assert "consumed_at IS NULL" in sql and "consumed_at IS NOT NULL" in sql
    assert "idempotency_key TEXT NOT NULL" in sql
    assert "BEFORE UPDATE OR DELETE ON project_repository_edit_audit_events" in sql


def test_validated_receipt_values_preserve_exact_authority_lease_and_git_witnesses():
    ids = _ids()
    class Obj:
        pass
    org, project = Obj(), Obj()
    org.org_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    project.project_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    ids.update({"actor": "33333333-3333-4333-8333-333333333333",
                "lease": "44444444-4444-4444-8444-444444444444",
                "edit": "55555555-5555-4555-8555-555555555555",
                "repo": "66666666-6666-4666-8666-666666666666"})
    receipt = _receipt(org, project, ids)
    values = repository_edit_store._receipt_values(  # noqa: SLF001
        receipt, staged_receipt_digest(receipt))
    assert str(values["repo"]) == ids["repo"]
    assert str(values["lease"]) == ids["lease"]
    assert values["generation"] == 7
    assert values["base"] == "1" * 40
    assert values["head"] == "2" * 40 and values["tree"] == "3" * 40


def test_store_has_no_git_filesystem_route_or_provider_authority():
    source = (Path(__file__).resolve().parent.parent /
              "repository_edit_store.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "git ", "fastapi", "APIRouter", "boto3", "os.environ"):
        assert forbidden not in source


@pytest.fixture
def authority(make_org):
    ids = _ids()
    org = make_org("P8 edit state")
    project = store.create_project(org.org_id, "Project")
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO identity_bindings (binding_id,external_authority,external_subject,"
            "platform_tenant_id,platform_user_id,role) VALUES "
            "(%s,'test',%s,%s,%s,'owner'),(%s,'test',%s,%s,%s,'owner')",
            (ids["actor"], f"actor-{ids['actor']}", org.org_id, uuid.uuid4(),
             ids["approver"], f"approver-{ids['approver']}", org.org_id, uuid.uuid4()))
    store.register_project_repository_authority(
        str(org.org_id), str(org.org_id), str(project.project_id), ids["repo"])
    return org, project, ids


def test_confirmation_consumption_and_publish_settlement_are_exact(authority):
    org, project, ids = authority
    receipt = _receipt(org, project, ids)
    digest = staged_receipt_digest(receipt)
    staged = repository_edit_store.record_staged(
        receipt, digest, expected_version=0, transition_key="stage-one")
    assert staged["state"] == "staged" and staged["version"] == 1
    waiting = repository_edit_store.await_confirmation(
        ids["edit"], expected_version=1, transition_key="wait-one")
    confirmation = _confirmation(receipt, ids)
    repository_edit_store.put_confirmation(
        confirmation, expected_edit_version=2, transition_key="confirm-one")
    publishing = repository_edit_store.consume_for_publish(
        ids["edit"], ids["confirmation"], expected_version=2,
        transition_key="publish-one")
    assert publishing["state"] == "publishing" and publishing["version"] == 3
    published = repository_edit_store.settle_publish(
        ids["edit"], private_ref_commit="2" * 40, main_commit="2" * 40,
        main_tree="3" * 40, expected_version=3, transition_key="settle-one")
    assert published["state"] == "published"
    assert waiting["state"] == "awaiting_confirmation"
    with pytest.raises(repository_edit_store.RepositoryEditStoreError):
        repository_edit_store.consume_for_publish(
            ids["edit"], ids["confirmation"], expected_version=3,
            transition_key="second-consume")


def test_git_witness_drift_conflicts_without_inventing_success(authority):
    org, project, ids = authority
    receipt = _receipt(org, project, ids)
    repository_edit_store.record_staged(
        receipt, staged_receipt_digest(receipt), expected_version=0,
        transition_key="stage-one")
    repository_edit_store.await_confirmation(
        ids["edit"], expected_version=1, transition_key="wait-one")
    repository_edit_store.put_confirmation(
        _confirmation(receipt, ids), expected_edit_version=2,
        transition_key="confirm-one")
    repository_edit_store.consume_for_publish(
        ids["edit"], ids["confirmation"], expected_version=2,
        transition_key="publish-one")
    result = repository_edit_store.recover_publish(
        ids["edit"], private_ref_commit="2" * 40, main_commit="9" * 40,
        main_tree="3" * 40, expected_version=3, transition_key="recover-one",
        reason_code="response_lost")
    assert result["state"] == "conflicted"


def test_main_still_at_base_remains_recoverable_with_consumed_confirmation(authority):
    org, project, ids = authority
    receipt = _receipt(org, project, ids)
    repository_edit_store.record_staged(
        receipt, staged_receipt_digest(receipt), expected_version=0,
        transition_key="stage-one")
    repository_edit_store.await_confirmation(
        ids["edit"], expected_version=1, transition_key="wait-one")
    repository_edit_store.put_confirmation(
        _confirmation(receipt, ids), expected_edit_version=2,
        transition_key="confirm-one")
    repository_edit_store.consume_for_publish(
        ids["edit"], ids["confirmation"], expected_version=2,
        transition_key="publish-one")
    interrupted = repository_edit_store.settle_publish(
        ids["edit"], private_ref_commit="2" * 40, main_commit="1" * 40,
        main_tree="1" * 40, expected_version=3, transition_key="observe-base")
    assert interrupted["state"] == "publishing" and interrupted["version"] == 4
    recovered = repository_edit_store.recover_publish(
        ids["edit"], private_ref_commit="2" * 40, main_commit="2" * 40,
        main_tree="3" * 40, expected_version=4, transition_key="recover-one",
        reason_code="response_lost")
    assert recovered["state"] == "published"


def test_concurrent_expected_version_has_one_winner(authority):
    org, project, ids = authority
    receipt = _receipt(org, project, ids)
    repository_edit_store.record_staged(
        receipt, staged_receipt_digest(receipt), expected_version=0,
        transition_key="stage-one")
    barrier = threading.Barrier(2)
    outcomes = []

    def advance(key):
        barrier.wait(timeout=10)
        try:
            outcomes.append(repository_edit_store.await_confirmation(
                ids["edit"], expected_version=1, transition_key=key)["state"])
        except repository_edit_store.RepositoryEditStoreError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=advance, args=(f"wait-{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert outcomes.count("awaiting_confirmation") == 1
    assert outcomes.count("stale_transition") == 1
