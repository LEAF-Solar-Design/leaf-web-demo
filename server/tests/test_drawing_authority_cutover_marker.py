"""Static contract for the production drawing-authority cutover marker."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "platform" / "migrations" / "0021_drawing_authority_cutover.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").split()).lower()


def test_marker_is_a_singleton_with_fixed_transition_states() -> None:
    sql = _sql()
    assert "create table if not exists drawing_authority_cutover" in sql
    assert "id smallint primary key default 1" in sql
    assert "constraint drawing_authority_cutover_singleton check (id = 1)" in sql
    for state in (
        "fence_closed",
        "migrating",
        "migrated",
        "promoted",
        "rolled_back",
    ):
        assert f"'{state}'" in sql
    assert "state not in ('migrated', 'promoted') or parity_digest is not null" in sql
    assert "state <> 'rolled_back' or last_error is not null" in sql


def test_marker_binds_exact_run_source_task_storage_and_deadline() -> None:
    sql = _sql()
    for field in (
        "schema_version",
        "source_commit",
        "run_id",
        "run_attempt",
        "task_definition_arn",
        "source_task_definition_arn",
        "efs_id",
        "fence_path",
        "source_counts",
        "parity_digest",
        "entered_at",
        "updated_at",
        "deadline",
        "last_error",
    ):
        assert re.search(rf"\b{field}\b", sql)
    assert "fence_path = '/data/state/drawing-mutations'" in sql
    assert "deadline > entered_at" in sql
    assert "last_error ~ '^[a-z0-9_]+$'" in sql
    assert "jsonb_object_length(source_counts) = 4" in sql
    for category in ("manifests", "versions", "attempts", "purge_receipts"):
        assert f"jsonb_typeof(source_counts->'{category}') = 'number'" in sql
        assert (
            f"source_counts->>'{category}' ~ '^(0|[1-9][0-9]*)$'" in sql
        )


def test_marker_contains_no_customer_identity_column() -> None:
    sql = _sql()
    for forbidden in ("tenant_id", "drawing_id", "object_key", "attempt"):
        assert not re.search(rf"\b{forbidden}\b", sql)
