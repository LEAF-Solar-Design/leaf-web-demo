"""Static contract checks for the P7 annotation authority."""
from __future__ import annotations

import ast
from pathlib import Path

from leaf_platform.db import (MIGRATION_GLOB, _AUTHORITY_REQUIRED_CONSTRAINTS,
                              required_catalog_for_selected_authorities)

PLATFORM = Path(__file__).resolve().parent.parent
MIGRATION = PLATFORM / "migrations" / "0036_annotation_batches.sql"
STORE = PLATFORM / "annotation_store.py"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _store() -> str:
    return STORE.read_text(encoding="utf-8")


def test_migration_number_is_unique_and_additive():
    names = sorted(path.name for path in (PLATFORM / "migrations").glob(MIGRATION_GLOB))
    assert MIGRATION.name in names
    numbers = [name.split("_", 1)[0] for name in names]
    assert len(numbers) == len(set(numbers))
    upper = _sql().upper()
    for destructive in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert destructive not in upper


def test_target_is_exact_tenant_project_drawing_cas_head():
    sql = _sql()
    assert "PRIMARY KEY (tenant_id, org_id, project_id, drawing_id)" in sql
    assert "CHECK (tenant_id = org_id)" in sql
    assert "REFERENCES drawing_artifacts(drawing_id, project_id, org_id)" in sql
    assert "commit_sha  TEXT" in sql and "tree_sha    TEXT" in sql
    store = _store()
    assert "AND version = %(version)s AND commit_sha = %(base_commit)s" in store
    assert "AND tree_sha = %(base_tree)s" in store


def test_batch_binds_owned_session_and_exact_git_witnesses():
    sql = _sql()
    assert "session_id     TEXT        NOT NULL REFERENCES app_sessions(session_id)" in sql
    for column in ("base_commit", "base_tree", "preview_commit", "preview_tree"):
        assert column in sql
    store = _store()
    assert "AND tenant_id = %(tenant_text)s AND drawing_id = %(drawing_text)s" in store
    assert "AND status = 'active'" in store


def test_batches_are_append_only_revisions_with_fresh_retry_and_undo_links():
    sql = _sql()
    assert "PRIMARY KEY (batch_id, revision)" in sql
    assert "retry_of_batch_id UUID" in sql
    assert "reverses_batch_id UUID" in sql
    assert "annotation_batches_one_pending_per_session_target" in sql
    store = _store()
    assert "UPDATE annotation_batches SET superseded_at = NOW()" in store
    assert "revision = 0 FOR UPDATE" in store
    assert "undo_head_intervened" in store
    assert "AND lease_expires_at <= NOW() FOR UPDATE" in store
    assert 'state="expired"' in store


def test_decision_locks_batch_anchor_and_target_in_one_transaction():
    tree = ast.parse(_store())
    accept = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == "accept")
    text = ast.get_source_segment(_store(), accept) or ""
    assert "with db.connection() as conn, conn.cursor() as cur" in text
    assert "_lock_latest" in text
    assert "FOR UPDATE" in text
    assert "_append_revision" in text
    assert "_audit" in text


def test_audit_is_content_free_but_keeps_exact_witnesses():
    block = _sql().split("CREATE TABLE IF NOT EXISTS annotation_audit", 1)[1].split(");", 1)[0]
    assert "payload_digest" in block and "payload_count" in block
    assert "before_commit" in block and "after_commit" in block
    assert "payload_json" not in block and "payload_content" not in block
    assert "annotations " not in block.lower() and "annotation_json" not in block.lower()


def test_strict_schema_readiness_owns_annotation_constraints():
    contracts = _AUTHORITY_REQUIRED_CONSTRAINTS["annotations"]
    assert "annotation_targets_drawing_fk" in contracts
    assert "annotation_batches_target_fk" in contracts
    assert "annotation_batches_kind_link_check" in contracts
    kind_fragments = set(
        contracts["annotation_batches_kind_link_check"]["definition_fragments"]
    )
    assert {
        "reverses_batch_id IS NULL",
        "reverses_commit IS NULL",
        "reverses_tree IS NULL",
        "reverses_batch_id IS NOT NULL",
        "reverses_commit IS NOT NULL",
        "reverses_tree IS NOT NULL",
    } <= kind_fragments


def test_annotation_catalog_is_unconditional_without_a_selector():
    catalog = required_catalog_for_selected_authorities({})
    assert "annotation_batches_target_fk" in catalog["constraints"]
    assert "annotation_batches_request_key_uq" in catalog["indexes"]
