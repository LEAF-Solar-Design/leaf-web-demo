"""T1 overlay SQL binding — static proofs that need no database.

The live behaviour (CAS under concurrency, transactional atomicity) needs a
real Postgres and belongs in the Postgres-marked suite. What CAN be proven
statically is that the SQL says what the design requires — and that is worth
pinning, because every one of these is a property an adversarial review named
and a plausible future edit could quietly remove.

Run:  cd platform && python -m pytest tests/test_overlay_store_static.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parent.parent
MIGRATION = PLATFORM_DIR / "migrations" / "0028_overlay_tokens.sql"
STORE = PLATFORM_DIR / "overlay_store.py"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _store() -> str:
    return STORE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Migration shape
# --------------------------------------------------------------------------- #
def test_migration_exists_and_is_next_in_sequence():
    """0027 is the highest migration on main; a duplicate number would make
    apply_migration's sorted order ambiguous."""
    names = sorted(p.name for p in (PLATFORM_DIR / "migrations").glob("00*.sql"))
    assert MIGRATION.name in names
    assert names[-1] == "0028_overlay_tokens.sql"


def test_migration_is_idempotent_like_every_other_one():
    """apply_migration replays the whole directory in order; a non-idempotent
    statement breaks a re-run and, worse, a partial recovery."""
    sql = _sql()
    creates = re.findall(r"CREATE (TABLE|INDEX|UNIQUE INDEX)", sql)
    assert creates, "expected CREATE statements"
    assert sql.count("IF NOT EXISTS") >= len(creates)


def test_migration_is_additive_only():
    """Expand-contract: 0023+ are gated on being additive."""
    sql = _sql().upper()
    for destructive in ("DROP TABLE", "DROP COLUMN", "ALTER COLUMN",
                        "RENAME TO", "TRUNCATE", "DELETE FROM"):
        assert destructive not in sql, destructive


def test_state_values_are_constrained_at_the_database():
    """The state machine is enforced in two places on purpose: a bug in the
    Python layer must not be able to persist a state nobody handles."""
    sql = _sql()
    assert "CHECK (state IN ('pending','approved','denied','expired','reverted'))" in sql


def test_one_pending_preview_per_session_is_enforced_by_an_index():
    """Two pending overlays would make 'what is the user looking at'
    ambiguous, and leave the revoke path guessing which to pull."""
    sql = _sql()
    assert "overlay_proposals_one_pending_per_session" in sql
    assert "WHERE state = 'pending'" in sql
    assert "UNIQUE INDEX" in sql


def test_scoping_is_per_tenant_with_no_platform_wide_row_shape():
    """Operator decision: no platform-wide fast path exists AT ALL, so a
    mistaken overlay is bounded by construction rather than by a policy check.
    Both tables key on tenant_id with a FK to orgs."""
    sql = _sql()
    assert "tenant_id   UUID        PRIMARY KEY REFERENCES orgs(org_id)" in sql
    assert "tenant_id         UUID        NOT NULL REFERENCES orgs(org_id)" in sql
    assert "global" not in sql.lower()


def test_audit_table_stores_a_count_not_content():
    """Tenant copy must not reach logs or exports through the audit trail."""
    sql = _sql()
    assert "token_count  INTEGER" in sql
    audit_block = sql.split("CREATE TABLE IF NOT EXISTS overlay_audit")[1]
    assert "tokens" not in audit_block.split(");")[0]


def test_proposals_are_versioned_by_revision_not_updated_in_place():
    sql = _sql()
    assert "PRIMARY KEY (proposal_id, revision)" in sql


# --------------------------------------------------------------------------- #
# Store shape — the properties that are easy to lose in a SQL layer
# --------------------------------------------------------------------------- #
def test_approve_uses_compare_and_swap_on_the_document_version():
    """A stale operator card must match zero rows, not overwrite."""
    src = _store()
    assert "WHERE tenant_id = %(t)s AND version = %(ver)s" in src
    assert "version_conflict" in src


def test_decided_proposals_are_never_updated_in_place():
    """Transitions INSERT a new revision so the previous one stays readable.
    An UPDATE against overlay_proposals would break the audit trail."""
    src = _store()
    assert "UPDATE overlay_proposals" not in src
    assert "_insert_revision" in src


def test_reads_filter_the_lease_in_sql():
    """Expiry is a function of TIME, not of the sweeper having run — the
    preview read must exclude a lapsed proposal by itself."""
    src = _store()
    assert "lease_expires_at > NOW()" in src


def test_every_decision_path_takes_the_lock_through_the_one_helper():
    """Counting "FOR UPDATE" occurrences was the original check here, and it was
    false comfort twice over: a docstring mentioning the phrase inflates the
    count, and the count says nothing about whether the lock actually
    serializes. A live-Postgres run proved the old single-statement lock did
    NOT serialize (see test_overlay_store_postgres.py), so what matters now is
    that all three write paths go through `_lock_latest` and none re-invents it.
    """
    src = _store()
    assert "def _lock_latest(" in src
    body = src.split("def _lock_latest(", 1)[1]
    for path in ("def approve(", "def deny(", "def revert("):
        segment = body.split(path, 1)[1].split("\ndef ", 1)[0]
        assert "_lock_latest(cur, proposal_id)" in segment, path
        assert "ORDER BY revision DESC LIMIT 1 FOR UPDATE" not in segment, (
            f"{path} re-introduced the single-statement lock that does not "
            f"serialize under READ COMMITTED")


def test_sweeper_does_not_block_the_decision_path():
    """SKIP LOCKED: a sweep must never stall an operator's tap."""
    assert "FOR UPDATE SKIP LOCKED" in _store()


#: Words that mean a string is SQL rather than prose. An f-string carrying one
#: of these is building a query; an f-string without them is an error message.
_SQL_WORDS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WHERE ", "VALUES ",
              "FROM ", "SET ")


def test_no_sql_is_built_by_interpolation():
    """Values reach the database through %(name)s binds only.

    The check targets f-strings that are SQL — an f-string in an ERROR MESSAGE
    is fine and must not be flagged, or the test trains people to weaken it.
    The one legitimate SQL f-string is the constant column list, which contains
    no interpolated value at all.
    """
    offenders = []
    for line in _store().splitlines():
        stripped = line.strip()
        if not stripped.startswith('f"'):
            continue
        if not any(word in stripped.upper() for word in _SQL_WORDS):
            continue  # prose, not a query
        if "_PROPOSAL_COLS" in stripped:
            continue  # the constant column list — no value is interpolated
        offenders.append(stripped)
    assert offenders == [], offenders


def test_the_only_sql_fstring_interpolates_a_constant():
    """_PROPOSAL_COLS is a module constant, not caller input — pin that, so
    the exemption above cannot be widened by accident."""
    src = _store()
    assert re.search(r'^_PROPOSAL_COLS = \(', src, re.M)
    assert "%s" not in src.split("_PROPOSAL_COLS = (")[1].split(")")[0]


def test_revert_removes_its_own_keys_rather_than_restoring_a_snapshot():
    """A snapshot restore would silently roll back tokens a LATER approval
    changed."""
    src = _store()
    assert "tokens - %(keys)s::text[]" in src
