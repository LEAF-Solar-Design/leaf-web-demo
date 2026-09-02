import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from glug_executor import GlugExecutorError
from glug_jobs import GlugJobService, GlugJobStore
from glug_live_adapters import SQLiteApprovalStore

NOW = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
COMMIT = "a" * 40
RECEIPT = {
    "contract": "glug.mushy-stage-receipt.v1", "repository": "biting-fogies/glug",
    "commit": COMMIT, "base_commit": "b" * 40, "claim_id": "claim-1",
}


def _store(tmp_path, clock=lambda: NOW):
    return GlugJobStore(tmp_path / "jobs.sqlite", clock=clock)


def _complete_stage(store, actor="board-admin", key="stage-1"):
    job, _ = store.create(
        actor_id=actor, job_type="stage_change", job_input={"instruction": "Change copy"},
        idempotency_key=key, max_attempts=2,
    )
    attempt, claim, _, _ = store.claim(job["id"])
    store.settle_success(job["id"], attempt=attempt, claim=claim,
                         result={"receipt": RECEIPT})
    return job["id"]


def test_job_idempotency_actor_isolation_and_hashed_claim(tmp_path):
    store = _store(tmp_path)
    first, created = store.create(
        actor_id="board-admin", job_type="code_question",
        job_input={"instruction": "Where is the home tab?"}, idempotency_key="ask-1",
        max_attempts=2,
    )
    replay, replay_created = store.create(
        actor_id="board-admin", job_type="code_question",
        job_input={"instruction": "Where is the home tab?"}, idempotency_key="ask-1",
        max_attempts=2,
    )
    assert created is True and replay_created is False and replay["id"] == first["id"]
    assert store.get(first["id"], actor_id="board-admin") is not None
    assert store.get(first["id"], actor_id="another-admin") is None
    attempt, claim, _, _ = store.claim(first["id"])
    with sqlite3.connect(tmp_path / "jobs.sqlite") as connection:
        stored = connection.execute(
            "SELECT claim_token_hash FROM glug_mushy_jobs WHERE id=?", (first["id"],)
        ).fetchone()[0]
    assert stored != claim and len(stored) == 64 and attempt == 1


def test_wrong_or_stale_job_claim_cannot_settle(tmp_path):
    store = _store(tmp_path)
    job, _ = store.create(
        actor_id="board-admin", job_type="code_question", job_input={"instruction": "Ask"},
        idempotency_key="ask-1", max_attempts=2,
    )
    attempt, claim, _, _ = store.claim(job["id"])
    with pytest.raises(GlugExecutorError, match="stale"):
        store.settle_success(job["id"], attempt=attempt, claim="wrong", result={"text": "no"})
    store.settle_success(job["id"], attempt=attempt, claim=claim, result={"text": "yes"})
    with pytest.raises(GlugExecutorError, match="stale"):
        store.settle_success(job["id"], attempt=attempt, claim=claim, result={"text": "again"})


def test_expired_queued_job_fails_without_running(tmp_path):
    clock = {"now": NOW}
    store = _store(tmp_path, clock=lambda: clock["now"])
    job, _ = store.create(
        actor_id="board-admin", job_type="stage_change", job_input={"instruction": "Change"},
        idempotency_key="stage-expired", max_attempts=2,
    )
    clock["now"] = NOW + dt.timedelta(hours=25)
    with pytest.raises(GlugExecutorError, match="expired"):
        store.claim(job["id"])
    terminal = store.get(job["id"], actor_id="board-admin")
    assert terminal["status"] == "failed"
    assert terminal["attempt"] == 0
    assert terminal["error"] == {"code": "job_expired"}


def test_restart_requeues_only_safe_nonterminal_jobs(tmp_path):
    store = _store(tmp_path)
    safe, _ = store.create(
        actor_id="board-admin", job_type="stage_change", job_input={"instruction": "Change"},
        idempotency_key="stage-1", max_attempts=2,
    )
    store.claim(safe["id"])
    unsafe, _ = store.create(
        actor_id="board-admin", job_type="create_pull_request",
        job_input={"origin_job_id": "job-stage", "approval_id": "approval-1"},
        idempotency_key="publish-1", max_attempts=1,
    )
    store.claim(unsafe["id"])
    recovered = _store(tmp_path)
    assert recovered.get(safe["id"], actor_id="board-admin")["status"] == "queued"
    publication = recovered.get(unsafe["id"], actor_id="board-admin")
    assert publication["status"] == "failed"
    assert publication["error"]["code"] == "restart_recovery_required"


def test_approval_is_exact_expiring_and_single_use(tmp_path):
    approvals = SQLiteApprovalStore(tmp_path / "jobs.sqlite", clock=lambda: NOW,
                                    id_factory=lambda: "approval-1")
    approval = approvals.issue(
        actor_digest="1" * 64, origin_job_id="job-stage", repository="biting-fogies/glug",
        commit=COMMIT, power="create_pull_request", idempotency_key="approve-1",
        expires_at="2026-09-01T12:10:00Z",
    )
    assert approval["commit"] == COMMIT and approval["origin_job_id"] == "job-stage"
    assert approvals.verify(
        approval_id="approval-1", actor_id="1" * 64, power="create_pull_request",
        repository_slug="biting-fogies/glug", commit="c" * 40,
    ) is None
    assert approvals.verify(
        approval_id="approval-1", actor_id="1" * 64, power="create_pull_request",
        repository_slug="biting-fogies/glug", commit=COMMIT,
    )["approved"] is True
    assert approvals.verify(
        approval_id="approval-1", actor_id="1" * 64, power="create_pull_request",
        repository_slug="biting-fogies/glug", commit=COMMIT,
    ) is None


def test_approval_issue_derives_exact_stage_job_subject(tmp_path):
    store = _store(tmp_path)
    origin = _complete_stage(store)
    approvals = SQLiteApprovalStore(tmp_path / "jobs.sqlite", clock=lambda: NOW,
                                    id_factory=lambda: "approval-1")
    service = GlugJobService(store=store, executor=object(), approvals=approvals, background=False)
    approval = service.issue_approval(
        actor_id="board-admin", origin_job_id=origin,
        publication_power="create_review_branch", idempotency_key="approve-1",
    )
    assert approval["repository"] == RECEIPT["repository"]
    assert approval["commit"] == RECEIPT["commit"]
    with pytest.raises(GlugExecutorError, match="Completed stage"):
        service.issue_approval(
            actor_id="another-admin", origin_job_id=origin,
            publication_power="create_review_branch", idempotency_key="approve-2",
        )
