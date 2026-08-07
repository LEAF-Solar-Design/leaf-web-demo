"""T1 overlay SQL binding — static proofs that need no database.

The live behaviour (CAS under concurrency, transactional atomicity) needs a
real Postgres and belongs in the Postgres-marked suite. What CAN be proven
statically is that the SQL says what the design requires — and that is worth
pinning, because every one of these is a property an adversarial review named
and a plausible future edit could quietly remove.

Run:  cd platform && python -m pytest tests/test_overlay_store_static.py -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from leaf_platform.db import MIGRATION_GLOB

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
def test_migration_exists_and_its_number_is_unique():
    """A duplicate number would make apply_migration's sorted order ambiguous.

    This deliberately does NOT pin 0028 as the highest migration. It used to,
    and that pin was already wrong on its own terms: the docstring claimed 0027
    was the highest while the assertion named 0028, so it had been hand-bumped
    at least once. A tail pin fails for EVERY future migration by any lane --
    0029_session_annex.sql broke it -- while proving nothing about the
    ambiguity it exists to catch. Uniqueness is that property; being last is
    not.

    The glob is the loader's own ``db.MIGRATION_GLOB``, not a hand-written
    pattern. It used to be ``00*.sql``, which matches 0000-0099 and nothing
    after; the loader has always matched four digits. So from 0100 on, a
    duplicate pair would be loaded and applied while this test saw neither
    file and reported no duplicate. Sharing the loader's pattern is what keeps
    the test looking at the set it claims to be guarding.
    """
    names = sorted(p.name for p in (PLATFORM_DIR / "migrations").glob(MIGRATION_GLOB))
    assert MIGRATION.name in names
    numbers = [name.split("_", 1)[0] for name in names]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"duplicate migration numbers: {duplicates}"


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


def test_decision_content_is_never_updated_in_place():
    """Transitions INSERT a new revision so the previous one stays readable.

    This check used to be `"UPDATE overlay_proposals" not in src`, which was
    too blunt: a review found the append-only design deadlocked a session
    permanently, because the superseded revision kept reading `pending` and the
    partial unique index blocked every later proposal. The fix stamps
    `superseded_at` on the old row, so one UPDATE now exists ON PURPOSE.

    The invariant that actually matters is narrower and is what this asserts:
    the only column any UPDATE may touch is `superseded_at`. Rewriting state,
    actor, decision_key or tokens would break the audit trail.
    """
    src = _store()
    assert "_insert_revision" in src
    updates = re.findall(r"UPDATE overlay_proposals SET ([a-z_]+)", src)
    assert updates, "expected the supersession stamp"
    assert set(updates) == {"superseded_at"}, (
        f"an UPDATE rewrites decision content: {sorted(set(updates))}")


def test_supersession_is_stamped_wherever_a_revision_is_appended():
    """The stamp and the append must not drift apart: an append without a stamp
    re-creates the session deadlock, silently."""
    src = _store()
    body = src.split("def _insert_revision(", 1)[1].split("\ndef ", 1)[0]
    assert "superseded_at = NOW()" in body
    assert body.index("UPDATE overlay_proposals") < body.index("INSERT INTO overlay_proposals")


def test_reads_that_must_ignore_superseded_revisions_do():
    """A preview read that forgets this serves an overlay the operator already
    decided on; a sweeper that forgets it re-expires settled history."""
    src = _store()
    for fn in ("def pending_for_session(", "def sweep_expired("):
        segment = src.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "superseded_at IS NULL" in segment, fn


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
        assert "_lock_latest(cur, proposal_id, tenant_id)" in segment, path
        assert "ORDER BY revision DESC LIMIT 1 FOR UPDATE" not in segment, (
            f"{path} re-introduced the single-statement lock that does not "
            f"serialize under READ COMMITTED")


def test_the_locked_lookup_is_tenant_scoped_in_BOTH_statements():
    """Tenant scoping in the locked lookup is a security boundary, not a
    filter: without it, knowing another tenant's proposal id was enough to
    approve, deny or revert it, and the mutation landed on THEIR document
    because the code read the tenant off the proposal row (sol-critic PR #439
    round 2). Both statements need the predicate — the anchor-row FOR UPDATE
    and the latest-revision re-read — or the lock and the read disagree about
    which rows exist.
    """
    src = _store()
    body = src.split("def _lock_latest(", 1)[1].split("\ndef ", 1)[0]
    statements = [chunk for chunk in body.split("cur.execute(")[1:]]
    assert len(statements) == 2, "the two-statement lock discipline changed"
    for statement in statements:
        assert "tenant_id = %(tid)s" in statement, statement[:120]
    # The anchor subquery must be scoped too, or MIN(revision) is taken over
    # another tenant's rows and the FOR UPDATE matches nothing.
    assert body.count("tenant_id = %(tid)s") >= 3


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


def test_revert_removes_only_keys_still_holding_its_own_value():
    """A snapshot restore would silently roll back tokens a LATER approval
    changed — but so did the first fix for it.

    `tokens - keys` removed every key the proposal named, regardless of who
    last wrote them. Approve A (bg=#111), approve B (bg=#222), revert A, and
    B's value was deleted outright: the operator reverting A silently undid
    B's decision. The removal is now conditional on the stored value still
    being the one this proposal applied.
    """
    src = _store()
    assert "tokens - %(keys)s::text[]" not in src, "the unconditional delete is back"
    assert "IS NOT DISTINCT FROM" in src
    assert "jsonb_each_text" in src


def test_deny_requires_the_same_cas_witness_approve_does():
    """A denial recorded against a version the operator was no longer looking
    at. The safe-looking button must not be the unguarded one."""
    src = _store()
    segment = src.split("def deny(", 1)[1].split("\ndef ", 1)[0]
    assert "expected_version" in src.split("def deny(", 1)[1].split(")", 1)[0]
    assert "version_conflict" in segment


def test_no_transaction_reaches_for_a_second_connection():
    """`document()` opens its own connection. Called from inside a
    `with db.connection()` block it can exhaust the pool: holders wait on the
    proposal anchor while the lock holder asks for one more connection than the
    pool has. Reads inside a transaction must reuse the caller's cursor.
    """
    src = _store()
    for fn in ("def approve(", "def deny(", "def revert("):
        segment = src.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "document(current[" not in segment, (
            f"{fn} calls the connection-opening document() inside a transaction")


def test_row_conversion_stringifies_uuids_like_the_fakes_do():
    """The gap that let a 500 reach staging (PR #441).

    psycopg returns uuid columns as uuid.UUID objects; every consumer
    JSON-serializes what the store returns (the decide route puts proposal_id
    into an SSE envelope AND its own response body). A UUID raises "Object of
    type UUID is not JSON serializable" — approve 500'd on real Postgres while
    every unit test passed, because the fake stores return strings.

    Asserted against the real function with a row shaped like psycopg's, so
    this fails if the coercion is removed — not against a copy of the rule.
    """
    import importlib.util
    import uuid as _uuid
    from pathlib import Path as _Path

    spec = importlib.util.spec_from_file_location(
        "leaf_platform_store_probe", _Path(__file__).resolve().parent.parent / "overlay_store.py")
    # The module imports `from . import db`, so load it as part of its package.
    import sys
    pkg_dir = _Path(__file__).resolve().parent.parent
    if "leaf_platform_probe" not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            "leaf_platform_probe", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)])
        pkg = importlib.util.module_from_spec(pkg_spec)
        sys.modules["leaf_platform_probe"] = pkg
        pkg_spec.loader.exec_module(pkg)
    store = importlib.import_module("leaf_platform_probe.overlay_store")

    pid, tid = _uuid.uuid4(), _uuid.uuid4()
    row = {"proposal_id": pid, "tenant_id": tid, "session_id": "s-1",
           "state": "approved", "revision": 2, "tokens": {"color.accent": "#123456"}}
    out = store._row_to_dict(row)

    assert out["proposal_id"] == str(pid) and isinstance(out["proposal_id"], str)
    assert out["tenant_id"] == str(tid) and isinstance(out["tenant_id"], str)
    assert out["state"] == "approved"          # non-uuid values pass through
    assert out["revision"] == 2
    assert out["tokens"] == {"color.accent": "#123456"}
    json.dumps(out)                            # the operation that used to raise
