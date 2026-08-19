"""Acceptance tests for card C2-5: undo (single-step, receipted).

Acceptance oracle:
    Undo reverts the last write exactly, is itself receipted, and is
    idempotent (double-undo is a no-op with a truthful response).

Offline, in-process (direct function calls against the real ``templates``
module), mirroring ``tests/test_template_clone.py``'s sys.path setup and
fixture convention.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import templates  # noqa: E402
from customization_authority import AuthorityError, TenantBinding  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_project_clone_store(monkeypatch):
    """Every test starts and ends with an empty clone/write/undo store, flag on."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOG", {})
    monkeypatch.setattr(templates, "_CLONE_UNDO_LOG", {})
    yield


def _binding(tenant_id="tenant-a", subject="author", role="owner", verified=True):
    return TenantBinding(tenant_id=tenant_id, subject=subject, role=role, verified=verified)


OWNER = _binding(role="owner")
EDITOR = _binding(role="editor")


def _new_clone(tenant_id="tenant-a", project_id="project-1", binding=OWNER):
    return templates.clone_template_for_project(
        "rooftop-standard-string", tenant_id=tenant_id, project_id=project_id,
        version="1.0.0", binding=binding,
    )


# --------------------------------------------------------------------------- #
# ORACLE: undo reverts the last write EXACTLY
# --------------------------------------------------------------------------- #
def test_undo_reverts_the_last_write_exactly():
    clone = _new_clone()
    pre_write_content = dict(clone.content)  # captured independently, before the write

    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 999}, binding=OWNER,
    )
    assert templates.get_project_clone(clone.clone_id).content["setback_ft"] == 999

    result = templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)

    assert result.undone is True
    assert result.already_undone is False
    reverted = templates.get_project_clone(clone.clone_id)
    assert reverted.content == pre_write_content


def test_undo_restores_the_stored_prior_snapshot_not_a_recomputed_delta(monkeypatch):
    """Even if deep-copying the wrong object would silently drift, undo must
    restore the SNAPSHOT captured at write time, not something recomputed
    from the clone's current (post-write) state."""
    clone = _new_clone()
    original_content = dict(clone.content)
    write = templates.write_project_clone_content(
        clone.clone_id, {"array_type": "totally_different", "setback_ft": -5}, binding=OWNER,
    )

    result = templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)

    assert templates.get_project_clone(clone.clone_id).content == original_content
    assert result.receipt["reverted_write_id"] == write.write_id


# --------------------------------------------------------------------------- #
# ORACLE: undo is itself receipted
# --------------------------------------------------------------------------- #
def test_undo_receipt_names_the_reverted_write():
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 42}, binding=OWNER,
    )
    result = templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)

    assert result.receipt["action"] == "undo"
    assert result.receipt["reverted_write_id"] == write.write_id
    assert result.receipt["reverted_write_receipt"] == write.receipt
    assert result.receipt["clone_id"] == clone.clone_id
    assert result.receipt["tenant_id"] == clone.tenant_id
    assert result.receipt["project_id"] == clone.project_id
    assert result.receipt["undo_id"]
    assert result.receipt["undo_id"] != write.write_id


# --------------------------------------------------------------------------- #
# ORACLE: idempotent -- double-undo is a no-op with a truthful response
# --------------------------------------------------------------------------- #
def test_double_undo_is_a_truthful_no_op_same_receipt_unchanged_content():
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 7}, binding=OWNER,
    )

    first = templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)
    content_after_first = templates.get_project_clone(clone.clone_id).content

    second = templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)
    content_after_second = templates.get_project_clone(clone.clone_id).content

    assert first.undone is True and first.already_undone is False
    assert second.undone is True and second.already_undone is True
    assert second.receipt == first.receipt  # same receipt, no fresh id minted
    assert content_after_second == content_after_first  # no redo, no re-revert
    assert len(templates._CLONE_UNDO_LOG) == 1


def test_double_undo_does_not_grow_the_undo_ledger_across_three_calls():
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 3}, binding=OWNER,
    )
    receipts = [
        templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER).receipt
        for _ in range(3)
    ]
    assert receipts[0] == receipts[1] == receipts[2]
    assert len(templates._CLONE_UNDO_LOG) == 1


def test_concurrent_double_undo_never_double_reverts():
    """A real thread race on the same write_id: both callers must serialize
    through the lock, exactly one performs the revert, and both observe the
    SAME undo receipt -- no double revert, no two distinct receipts."""
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 55}, binding=OWNER,
    )

    results = []
    barrier = threading.Barrier(2)

    def _call():
        barrier.wait()
        results.append(templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER))

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert results[0].receipt == results[1].receipt
    assert len(templates._CLONE_UNDO_LOG) == 1
    assert sum(1 for r in results if r.already_undone is False) == 1
    assert sum(1 for r in results if r.already_undone is True) == 1


# --------------------------------------------------------------------------- #
# Stale receipt: "last write" is bound to write_id, never re-resolved at
# request time against whatever the current head happens to be.
# --------------------------------------------------------------------------- #
def test_stale_receipt_undo_is_refused_and_reverts_nothing():
    clone = _new_clone()
    write_a = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 1}, binding=OWNER,
    )
    write_b = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 2}, binding=OWNER,
    )
    content_before_undo_attempt = templates.get_project_clone(clone.clone_id).content

    with pytest.raises(templates.StaleUndoReceiptError):
        templates.undo_last_write(clone.clone_id, write_a.write_id, binding=OWNER)

    # Nobody asked to undo write B -- it must still be in effect, untouched.
    assert templates.get_project_clone(clone.clone_id).content == content_before_undo_attempt
    assert write_a.write_id not in templates._CLONE_UNDO_LOG


def test_undoing_the_true_head_after_a_stale_attempt_still_works():
    clone = _new_clone()
    write_a = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 1}, binding=OWNER,
    )
    write_b = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 2}, binding=OWNER,
    )
    with pytest.raises(templates.StaleUndoReceiptError):
        templates.undo_last_write(clone.clone_id, write_a.write_id, binding=OWNER)

    result = templates.undo_last_write(clone.clone_id, write_b.write_id, binding=OWNER)
    assert result.already_undone is False
    assert templates.get_project_clone(clone.clone_id).content["setback_ft"] == 1


# --------------------------------------------------------------------------- #
# Zero-write / unknown target: truthful refusal, never a crash.
# --------------------------------------------------------------------------- #
def test_undo_with_no_writes_at_all_raises_truthfully_not_a_crash():
    clone = _new_clone()  # never written to, only cloned
    with pytest.raises(templates.ProjectCloneWriteNotFoundError):
        templates.undo_last_write(clone.clone_id, "never-existed", binding=OWNER)
    # The birth clone must still list cleanly -- no phantom/broken state.
    assert templates.get_project_clone(clone.clone_id).content == clone.content
    assert [c.clone_id for c in templates.list_project_clones("tenant-a", "project-1")] == [
        clone.clone_id
    ]


def test_undo_of_unknown_write_id_on_a_written_clone_raises():
    clone = _new_clone()
    templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 9}, binding=OWNER,
    )
    with pytest.raises(templates.ProjectCloneWriteNotFoundError):
        templates.undo_last_write(clone.clone_id, "not-a-real-write-id", binding=OWNER)


def test_undo_of_unknown_clone_id_raises():
    with pytest.raises(templates.ProjectTemplateCloneNotFoundError):
        templates.undo_last_write("does-not-exist", "some-write-id", binding=OWNER)


# --------------------------------------------------------------------------- #
# Flag fence: solar_template_beta gates undo too, before any mutation.
# --------------------------------------------------------------------------- #
def test_undo_refuses_when_flag_is_off_and_mutates_nothing(monkeypatch):
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 13}, binding=OWNER,
    )
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)

    with pytest.raises(templates.TemplateBetaDisabledError):
        templates.undo_last_write(clone.clone_id, write.write_id, binding=OWNER)

    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    assert templates.get_project_clone(clone.clone_id).content["setback_ft"] == 13
    assert write.write_id not in templates._CLONE_UNDO_LOG


# --------------------------------------------------------------------------- #
# Authz: undo is write-grade (owner/editor of the clone's own tenant), the
# SAME gate the write itself uses -- never the read-grade "any authenticated
# caller" dependency.
# --------------------------------------------------------------------------- #
def test_undo_denies_read_only_roles_and_mutates_nothing():
    clone = _new_clone()
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 21}, binding=OWNER,
    )
    for role in ("reviewer", "read_only", "builder", "unknown"):
        with pytest.raises(AuthorityError) as excinfo:
            templates.undo_last_write(
                clone.clone_id, write.write_id, binding=_binding(role=role),
            )
        assert excinfo.value.reason_code == "template_clone_denied"
    assert templates.get_project_clone(clone.clone_id).content["setback_ft"] == 21
    assert write.write_id not in templates._CLONE_UNDO_LOG


def test_undo_denies_cross_tenant_binding_same_shape():
    clone = _new_clone(tenant_id="tenant-a")
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 31}, binding=OWNER,
    )
    with pytest.raises(AuthorityError) as excinfo:
        templates.undo_last_write(
            clone.clone_id, write.write_id,
            binding=_binding(tenant_id="tenant-b", role="owner"),
        )
    assert excinfo.value.reason_code == "template_clone_denied"
    assert templates.get_project_clone(clone.clone_id).content["setback_ft"] == 31


def test_undo_allows_editor_and_owner_of_the_clones_own_tenant():
    for role_binding in (OWNER, EDITOR):
        clone = _new_clone()
        write = templates.write_project_clone_content(
            clone.clone_id, {**clone.content, "setback_ft": 11}, binding=role_binding,
        )
        result = templates.undo_last_write(clone.clone_id, write.write_id, binding=role_binding)
        assert result.already_undone is False
