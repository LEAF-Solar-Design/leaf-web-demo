"""T1 overlay store — the properties that only a REAL Postgres can prove.

test_overlay_store_static.py proves the SQL *says* the right thing. It cannot
prove the database *does* the right thing, and the gap between those two is
where every concurrency bug lives. A `WHERE version = %(ver)s` that reads
correctly still has to actually match zero rows under a real race; a partial
unique index has to actually reject the second insert; `FOR UPDATE` has to
actually serialize two operators tapping Approve at the same instant.

So these tests run against a live server and use REAL concurrency — separate
connections in separate threads with genuinely overlapping transactions — not
a mock and not sequential calls pretending to be a race.

Run:
    docker run -d --name pg-overlay -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
    OVERLAY_PG_URL=postgresql://postgres:pw@127.0.0.1:55432/postgres \\
        python -m pytest tests/test_overlay_store_postgres.py -q

Skipped, never silently passed, when OVERLAY_PG_URL is unset: a suite that
reports green without a database would be worse than no suite, because it
would retire the exact doubt it exists to settle.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("OVERLAY_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="OVERLAY_PG_URL is not set")

psycopg = pytest.importorskip("psycopg")
# Explicit: `psycopg.rows` is a submodule, and relying on the package __init__
# to have imported it makes the suite depend on an implementation detail.
from psycopg import rows as pg_rows  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0028_overlay_tokens.sql"

# The migration FKs to orgs(org_id). Standing up the whole platform schema for
# three columns would couple this suite to migrations it is not testing.
ORGS_DDL = """
CREATE TABLE IF NOT EXISTS orgs (
    org_id     UUID PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT 'test',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@pytest.fixture(scope="module")
def schema():
    """A throwaway schema per run, dropped afterwards.

    Per-run rather than shared: a leftover row from a previous run could make a
    uniqueness test pass for the wrong reason.
    """
    name = f"overlay_t1_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
        conn.execute(f"SET search_path TO {name}")
        conn.execute(ORGS_DDL)
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
    yield name
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture()
def conn(schema):
    with psycopg.connect(PG_URL, row_factory=pg_rows.dict_row) as c:
        c.execute(f"SET search_path TO {schema}")
        yield c
        c.rollback()


def _connect(schema, autocommit=False):
    c = psycopg.connect(PG_URL, autocommit=autocommit, row_factory=pg_rows.dict_row)
    c.execute(f"SET search_path TO {schema}")
    return c


@pytest.fixture()
def tenant(conn):
    tid = uuid.uuid4()
    conn.execute("INSERT INTO orgs (org_id) VALUES (%s)", (tid,))
    conn.commit()
    return tid


def _new_proposal(conn, tenant, *, session_id=None, tokens=None, lease="1 hour",
                  state="pending"):
    pid = uuid.uuid4()
    conn.execute(
        "INSERT INTO overlay_proposals (proposal_id, revision, tenant_id, "
        "session_id, tokens, state, lease_expires_at) VALUES "
        "(%(pid)s, 1, %(t)s, %(s)s, %(tok)s::jsonb, %(st)s, NOW() + %(l)s::interval)",
        {"pid": pid, "t": tenant, "s": session_id or str(uuid.uuid4()),
         "tok": tokens or '{"color.canvas.bg": "#ffffff"}', "st": state, "l": lease})
    conn.commit()
    return pid


def _seed_doc(conn, tenant, version=0, tokens="{}"):
    conn.execute(
        "INSERT INTO overlay_documents (tenant_id, version, tokens) "
        "VALUES (%s, %s, %s::jsonb) ON CONFLICT (tenant_id) DO UPDATE "
        "SET version = EXCLUDED.version, tokens = EXCLUDED.tokens",
        (tenant, version, tokens))
    conn.commit()


# --------------------------------------------------------------------------- #
# The CAS, for real
# --------------------------------------------------------------------------- #
def _cas(conn, tenant, expected_version, tokens='{"color.canvas.bg": "#000000"}',
         actor="op"):
    """The exact UPDATE from overlay_store.approve()."""
    cur = conn.execute(
        "UPDATE overlay_documents "
        "SET tokens = tokens || %(tok)s::jsonb, version = version + 1, "
        "    updated_at = NOW(), updated_by = %(by)s "
        "WHERE tenant_id = %(t)s AND version = %(ver)s "
        "RETURNING version, tokens",
        {"tok": tokens, "by": actor, "t": tenant, "ver": expected_version})
    return cur.fetchone()


def _lock_latest(conn, proposal_id):
    """overlay_store._lock_latest, as two statements against a live server.

    Kept in the test as literal SQL rather than importing the store, because
    the store needs the platform package's `db` module and a configured
    DATABASE_URL. What is being verified here is the LOCKING BEHAVIOUR of these
    two statements, so having them written out is the point: if someone
    collapses them back into a single `ORDER BY ... LIMIT 1 FOR UPDATE`, this
    test fails, which is exactly what it is for.
    """
    conn.execute(
        "SELECT proposal_id FROM overlay_proposals WHERE proposal_id = %(pid)s "
        "AND revision = (SELECT MIN(revision) FROM overlay_proposals "
        "                WHERE proposal_id = %(pid)s) FOR UPDATE",
        {"pid": proposal_id})
    return conn.execute(
        "SELECT state, revision FROM overlay_proposals WHERE proposal_id = %(pid)s "
        "ORDER BY revision DESC LIMIT 1", {"pid": proposal_id}).fetchone()


def test_the_single_statement_lock_does_NOT_serialize(schema, tenant):
    """The bug this suite found, pinned so it cannot come back.

    `ORDER BY revision DESC LIMIT 1 FOR UPDATE` reads like it serializes two
    deciders. It does not: under READ COMMITTED the ORDER BY/LIMIT is evaluated
    against the statement's snapshot BEFORE the lock wait, so the blocked
    decider wakes and returns the revision it had already chosen — still
    'pending' — and walks past the state guard.

    Asserting the WRONG behaviour on purpose. If a future Postgres or a
    changed isolation level makes the one-liner safe, this test fails and the
    two-statement dance in overlay_store._lock_latest can be simplified. A
    failure here is good news; silence would just be folklore.
    """
    with _connect(schema, autocommit=True) as c:
        pid = _new_proposal(c, tenant)

    one_liner = ("SELECT state FROM overlay_proposals WHERE proposal_id = %s "
                 "ORDER BY revision DESC LIMIT 1 FOR UPDATE")
    first = _connect(schema)
    first.execute(one_liner, (pid,))

    started, seen = threading.Event(), {}

    def second():
        c = _connect(schema)
        try:
            started.set()
            seen["state"] = c.execute(one_liner, (pid,)).fetchone()["state"]
            c.commit()
        finally:
            c.close()

    t = threading.Thread(target=second)
    t.start()
    started.wait(timeout=5)
    time.sleep(0.4)  # let the second decider reach its lock wait

    first.execute(
        "INSERT INTO overlay_proposals (proposal_id, revision, tenant_id, "
        "session_id, tokens, state, lease_expires_at) SELECT proposal_id, "
        "revision + 1, tenant_id, session_id, tokens, 'approved', "
        "lease_expires_at FROM overlay_proposals WHERE proposal_id = %s "
        "ORDER BY revision DESC LIMIT 1", (pid,))
    first.commit()
    first.close()
    t.join(timeout=20)

    assert seen.get("state") == "pending", (
        "the one-statement lock now serializes — _lock_latest can be simplified")


def test_a_stale_card_matches_zero_rows_and_changes_nothing(conn, tenant):
    """The whole point of the CAS. An operator whose card was rendered at
    version 0, while someone else already moved the document to 1, must not
    overwrite: they are deciding about something they never saw."""
    _seed_doc(conn, tenant, version=0)

    assert _cas(conn, tenant, 0)["version"] == 1          # the fresh card wins
    assert _cas(conn, tenant, 0) is None                  # the stale card loses
    conn.commit()

    row = conn.execute("SELECT version, tokens FROM overlay_documents "
                       "WHERE tenant_id = %s", (tenant,)).fetchone()
    assert row["version"] == 1, "the losing CAS must not have bumped the version"


def test_two_simultaneous_approvals_produce_exactly_one_winner(schema, tenant):
    """REAL concurrency: two connections, two threads, both starting from
    version 0 and racing. Postgres serializes the UPDATEs; the loser must see
    zero rows rather than a lost update."""
    with _connect(schema, autocommit=True) as c:
        c.execute("INSERT INTO overlay_documents (tenant_id, version, tokens) "
                  "VALUES (%s, 0, '{}'::jsonb) ON CONFLICT (tenant_id) DO NOTHING",
                  (tenant,))

    results, barrier = {}, threading.Barrier(2)

    def worker(name, colour):
        c = _connect(schema)
        try:
            barrier.wait(timeout=10)   # maximise the overlap
            row = _cas(c, tenant, 0, tokens='{"color.canvas.bg": "%s"}' % colour,
                       actor=name)
            c.commit()
            results[name] = row
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(n, c))
               for n, c in (("op-a", "#111111"), ("op-b", "#222222"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    winners = [n for n, r in results.items() if r is not None]
    assert len(results) == 2, f"a thread died: {results}"
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"

    with _connect(schema, autocommit=True) as c:
        row = c.execute("SELECT version, tokens, updated_by FROM overlay_documents "
                        "WHERE tenant_id = %s", (tenant,)).fetchone()
    assert row["version"] == 1, "two winners would have produced version 2"
    assert row["updated_by"] == winners[0]


def test_the_row_lock_serializes_two_operators_on_one_proposal(schema, tenant):
    """`SELECT ... FOR UPDATE` in approve(). Without it both operators read
    'pending' and both proceed; with it the second blocks until the first
    commits and then sees the decided state."""
    with _connect(schema, autocommit=True) as c:
        pid = _new_proposal(c, tenant)

    first = _connect(schema)
    _lock_latest(first, pid)

    blocked = threading.Event()
    observed = {}

    def second():
        c = _connect(schema)
        try:
            blocked.set()
            observed["state"] = _lock_latest(c, pid)["state"]
            c.commit()
        finally:
            c.close()

    t = threading.Thread(target=second)
    t.start()
    blocked.wait(timeout=5)
    time.sleep(0.4)  # let the second decider reach its lock wait

    # The second connection is now waiting on the lock. Decide, then release.
    first.execute(
        "INSERT INTO overlay_proposals (proposal_id, revision, tenant_id, "
        "session_id, tokens, state, lease_expires_at, decided_at, decided_by, "
        "decision_key) SELECT proposal_id, revision + 1, tenant_id, session_id, "
        "tokens, 'approved', lease_expires_at, NOW(), 'op-a', 'k1' "
        "FROM overlay_proposals WHERE proposal_id = %s ORDER BY revision DESC "
        "LIMIT 1", (pid,))
    first.commit()
    first.close()

    t.join(timeout=20)
    assert observed.get("state") == "approved", (
        "the second operator read a stale 'pending' — the lock did not hold")


# --------------------------------------------------------------------------- #
# The partial unique index
# --------------------------------------------------------------------------- #
def test_a_session_cannot_hold_two_pending_proposals(conn, tenant):
    """Two pending overlays make 'what is the user looking at' ambiguous and
    leave the revoke path guessing which to pull."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session)
    with pytest.raises(pg_errors.UniqueViolation):
        _new_proposal(conn, tenant, session_id=session)
    conn.rollback()


def test_the_index_is_PARTIAL_so_decided_proposals_do_not_block_the_next_one(
        conn, tenant):
    """The index covers `WHERE state = 'pending'` only. A non-partial index
    would mean a session could never propose again after its first decision —
    which would look like the feature simply breaking after one use."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session, state="approved")
    _new_proposal(conn, tenant, session_id=session, state="denied")
    _new_proposal(conn, tenant, session_id=session, state="pending")  # allowed
    conn.commit()

    n = conn.execute("SELECT COUNT(*) AS n FROM overlay_proposals "
                     "WHERE session_id = %s", (session,)).fetchone()["n"]
    assert n == 3


# --------------------------------------------------------------------------- #
# Expiry is a function of TIME, not of the sweeper having run
# --------------------------------------------------------------------------- #
def test_a_lapsed_proposal_reads_as_gone_before_any_sweep(conn, tenant):
    """If the read did not filter the lease, a user would keep seeing a preview
    whose lease lapsed until a background job happened to notice."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session, lease="-1 minute")
    row = conn.execute(
        "SELECT proposal_id FROM overlay_proposals WHERE session_id = %s "
        "AND state = 'pending' AND lease_expires_at > NOW()", (session,)).fetchone()
    assert row is None


def test_the_sweeper_skips_locked_rows_instead_of_queueing_behind_them(
        schema, tenant):
    """SKIP LOCKED. A sweep must never stall an operator's tap: if the sweeper
    blocked on a row an operator is deciding, the tap waits on the sweep."""
    with _connect(schema, autocommit=True) as c:
        locked = _new_proposal(c, tenant, lease="-1 minute")
        free = _new_proposal(c, tenant, lease="-1 minute")

    holder = _connect(schema)
    holder.execute("SELECT proposal_id FROM overlay_proposals "
                   "WHERE proposal_id = %s FOR UPDATE", (locked,))
    try:
        sweeper = _connect(schema)
        rows = sweeper.execute(
            "SELECT proposal_id FROM overlay_proposals WHERE state = 'pending' "
            "AND lease_expires_at <= NOW() FOR UPDATE SKIP LOCKED").fetchall()
        got = {r["proposal_id"] for r in rows}
        sweeper.close()
    finally:
        holder.rollback()
        holder.close()

    assert free in got, "the sweeper failed to pick up an unlocked expired row"
    assert locked not in got, "the sweeper did not skip the locked row"


# --------------------------------------------------------------------------- #
# Revert semantics against real jsonb
# --------------------------------------------------------------------------- #
def test_revert_removes_only_its_own_keys(conn, tenant):
    """`tokens - %(keys)s::text[]`. A snapshot restore would silently roll back
    a token that a LATER approval changed — the reverting operator would undo
    someone else's decision without being told."""
    _seed_doc(conn, tenant, version=3,
              tokens='{"color.canvas.bg": "#000000", "copy.home.title": "Hi"}')
    conn.execute(
        "UPDATE overlay_documents SET tokens = tokens - %(keys)s::text[], "
        "version = version + 1 WHERE tenant_id = %(t)s AND version = %(ver)s",
        {"keys": ["color.canvas.bg"], "t": tenant, "ver": 3})
    conn.commit()

    row = conn.execute("SELECT version, tokens FROM overlay_documents "
                       "WHERE tenant_id = %s", (tenant,)).fetchone()
    assert row["tokens"] == {"copy.home.title": "Hi"}
    assert row["version"] == 4


def test_reverting_a_key_nobody_set_is_harmless(conn, tenant):
    """Postgres's `-` on a missing key is a no-op. Pinned because an
    implementation that errored here would make a double-revoke fail loudly
    when it should be idempotent."""
    _seed_doc(conn, tenant, version=1, tokens='{"copy.home.title": "Hi"}')
    conn.execute("UPDATE overlay_documents SET tokens = tokens - %s::text[] "
                 "WHERE tenant_id = %s", (["color.canvas.bg"], tenant))
    conn.commit()
    row = conn.execute("SELECT tokens FROM overlay_documents WHERE tenant_id = %s",
                       (tenant,)).fetchone()
    assert row["tokens"] == {"copy.home.title": "Hi"}


# --------------------------------------------------------------------------- #
# Atomicity and scoping
# --------------------------------------------------------------------------- #
def test_a_failed_transaction_leaves_no_partial_decision(conn, tenant):
    """The document bump, the proposal revision and the audit row are one
    transaction. A crash between them would leave a proposal advertising an
    applied_version the document never reached."""
    _seed_doc(conn, tenant, version=0)
    pid = _new_proposal(conn, tenant)

    try:
        _cas(conn, tenant, 0)
        conn.execute(
            "INSERT INTO overlay_proposals (proposal_id, revision, tenant_id, "
            "session_id, tokens, state, lease_expires_at) "
            "VALUES (%s, 2, %s, %s, '{}'::jsonb, 'not_a_state', NOW())",
            (pid, tenant, str(uuid.uuid4())))  # violates the CHECK
        conn.commit()
    except pg_errors.CheckViolation:
        conn.rollback()
    else:
        pytest.fail("the state CHECK did not fire")

    row = conn.execute("SELECT version FROM overlay_documents WHERE tenant_id = %s",
                       (tenant,)).fetchone()
    assert row["version"] == 0, "the CAS survived a rolled-back transaction"


def test_one_tenants_overlay_is_invisible_to_another(conn, tenant):
    """Per-tenant scoping is the blast-radius bound: there is no platform-wide
    row shape at all, so a mistaken overlay is contained by construction."""
    other = uuid.uuid4()
    conn.execute("INSERT INTO orgs (org_id) VALUES (%s)", (other,))
    _seed_doc(conn, tenant, version=1, tokens='{"color.canvas.bg": "#000000"}')
    conn.commit()

    row = conn.execute("SELECT tokens FROM overlay_documents WHERE tenant_id = %s",
                       (other,)).fetchone()
    assert row is None


def test_audit_rows_record_a_count_and_never_the_content(conn, tenant):
    """Tenant copy must not reach logs or exports through the audit trail."""
    pid = _new_proposal(conn, tenant)
    conn.execute(
        "INSERT INTO overlay_audit (proposal_id, tenant_id, from_state, "
        "to_state, actor, decision_key, token_count) "
        "VALUES (%s, %s, 'pending', 'approved', 'op', 'k1', 2)", (pid, tenant))
    conn.commit()

    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'overlay_audit'").fetchall()}
    assert "token_count" in cols
    assert "tokens" not in cols


# --------------------------------------------------------------------------- #
# The supersession blocker — found by review, reproduced here first
# --------------------------------------------------------------------------- #
def _append_revision(conn, pid, *, state, stamp=True):
    """overlay_store._insert_revision, as the two statements it runs."""
    if stamp:
        conn.execute(
            "UPDATE overlay_proposals SET superseded_at = NOW() "
            "WHERE proposal_id = %s AND superseded_at IS NULL "
            "  AND revision = (SELECT MAX(revision) FROM overlay_proposals "
            "                  WHERE proposal_id = %s)", (pid, pid))
    conn.execute(
        "INSERT INTO overlay_proposals (proposal_id, revision, tenant_id, "
        "session_id, tokens, state, lease_expires_at) "
        "SELECT proposal_id, revision + 1, tenant_id, session_id, tokens, %s, "
        "lease_expires_at FROM overlay_proposals WHERE proposal_id = %s "
        "ORDER BY revision DESC LIMIT 1", (state, pid))
    conn.commit()


def test_deciding_frees_the_session_for_a_new_proposal(conn, tenant):
    """THE blocker. Appending a decided revision without stamping the old one
    left it reading 'pending', so the partial unique index rejected every later
    proposal for that session — permanently, for the life of the tenant."""
    session = str(uuid.uuid4())
    pid = _new_proposal(conn, tenant, session_id=session)
    _append_revision(conn, pid, state="denied")

    _new_proposal(conn, tenant, session_id=session)  # must not raise
    conn.commit()


def test_the_deadlock_returns_if_the_stamp_is_skipped(conn, tenant):
    """Pin the mechanism, not just the symptom: without the stamp the very same
    sequence fails. If this ever stops failing, the index predicate changed and
    the stamp may be removable."""
    session = str(uuid.uuid4())
    pid = _new_proposal(conn, tenant, session_id=session)
    _append_revision(conn, pid, state="denied", stamp=False)

    with pytest.raises(pg_errors.UniqueViolation):
        _new_proposal(conn, tenant, session_id=session)
    conn.rollback()


def test_a_decided_proposal_stops_being_previewed(conn, tenant):
    """The other half of the same bug: the preview read kept serving a REJECTED
    overlay until its lease lapsed, so the user watched a theme the operator
    had already denied."""
    session = str(uuid.uuid4())
    pid = _new_proposal(conn, tenant, session_id=session)
    _append_revision(conn, pid, state="denied")

    row = conn.execute(
        "SELECT proposal_id FROM overlay_proposals WHERE tenant_id = %s "
        "AND session_id = %s AND state = 'pending' AND superseded_at IS NULL "
        "AND lease_expires_at > NOW()", (tenant, session)).fetchone()
    assert row is None


def test_supersession_never_rewrites_decision_content(conn, tenant):
    """The stamp is the ONLY mutation. If it ever rewrote state or tokens the
    audit trail would stop being true."""
    session = str(uuid.uuid4())
    pid = _new_proposal(conn, tenant, session_id=session,
                        tokens='{"color.canvas.bg": "#abcdef"}')
    _append_revision(conn, pid, state="approved")

    old = conn.execute(
        "SELECT state, tokens, superseded_at FROM overlay_proposals "
        "WHERE proposal_id = %s AND revision = 1", (pid,)).fetchone()
    assert old["state"] == "pending", "the original decision content was rewritten"
    assert old["tokens"] == {"color.canvas.bg": "#abcdef"}
    assert old["superseded_at"] is not None


# --------------------------------------------------------------------------- #
# Review majors — the revert clobber, against real jsonb
# --------------------------------------------------------------------------- #
def _revert_scoped(conn, tenant, mine_json, expected_version, actor="op"):
    """overlay_store.revert()'s conditional removal, verbatim."""
    return conn.execute(
        "UPDATE overlay_documents d "
        "SET tokens = d.tokens - ("
        "      SELECT COALESCE(array_agg(t.k), ARRAY[]::text[]) "
        "      FROM jsonb_each_text(%(mine)s::jsonb) AS t(k, v) "
        "      WHERE d.tokens ->> t.k IS NOT DISTINCT FROM t.v), "
        "    version = version + 1, updated_at = NOW(), updated_by = %(by)s "
        "WHERE d.tenant_id = %(t)s AND d.version = %(ver)s "
        "RETURNING version, tokens",
        {"mine": mine_json, "by": actor, "t": tenant,
         "ver": expected_version}).fetchone()


def test_reverting_does_not_undo_a_later_approvals_value(conn, tenant):
    """THE major. Approve A (bg=#111111), approve B (bg=#222222), revert A.
    The blind `tokens - keys` deleted the key outright, so the operator
    reverting A silently undid B's decision."""
    _seed_doc(conn, tenant, version=2, tokens='{"color.canvas.bg": "#222222"}')
    row = _revert_scoped(conn, tenant, '{"color.canvas.bg": "#111111"}', 2)
    conn.commit()
    assert row["tokens"] == {"color.canvas.bg": "#222222"}, (
        "revert deleted a value a LATER approval set")


def test_reverting_still_removes_a_key_it_does_own(conn, tenant):
    """The scoping must not make revert a no-op — it still has to work."""
    _seed_doc(conn, tenant, version=1,
              tokens='{"color.canvas.bg": "#111111", "copy.home.title": "Hi"}')
    row = _revert_scoped(conn, tenant, '{"color.canvas.bg": "#111111"}', 1)
    conn.commit()
    assert row["tokens"] == {"copy.home.title": "Hi"}


def test_reverting_a_partially_superseded_set_removes_only_the_untouched(conn, tenant):
    """A proposal that set two tokens where a later approval changed one:
    remove the one still ours, leave the one that moved on."""
    _seed_doc(conn, tenant, version=3,
              tokens='{"color.canvas.bg": "#999999", "copy.home.title": "Mine"}')
    row = _revert_scoped(
        conn, tenant,
        '{"color.canvas.bg": "#111111", "copy.home.title": "Mine"}', 3)
    conn.commit()
    assert row["tokens"] == {"color.canvas.bg": "#999999"}


# --------------------------------------------------------------------------- #
# Round 2 findings — expiry lockout and the deny race
# --------------------------------------------------------------------------- #
def _retire_lapsed(conn, tenant, session):
    """create_proposal's pre-insert retire, verbatim."""
    conn.execute(
        "UPDATE overlay_proposals SET superseded_at = NOW() "
        "WHERE tenant_id = %(t)s AND session_id = %(s)s "
        "  AND state = 'pending' AND superseded_at IS NULL "
        "  AND lease_expires_at <= NOW()",
        {"t": tenant, "s": session})


def test_an_expired_proposal_does_not_lock_the_session_out(conn, tenant):
    """Round 2 major. A partial index predicate cannot call NOW(), so a lapsed
    pending row kept occupying the session's one slot: the preview correctly
    showed nothing while every new proposal failed, permanently."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session, lease="-1 minute")

    _retire_lapsed(conn, tenant, session)
    _new_proposal(conn, tenant, session_id=session)   # must not raise
    conn.commit()


def test_the_lockout_returns_without_the_retire(conn, tenant):
    """Pin the mechanism. If this stops failing, the index predicate changed
    and the retire may be removable."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session, lease="-1 minute")
    with pytest.raises(pg_errors.UniqueViolation):
        _new_proposal(conn, tenant, session_id=session)
    conn.rollback()


def test_the_retire_does_not_touch_a_LIVE_pending_proposal(conn, tenant):
    """The retire must free only LAPSED slots. Clearing a live one would let a
    session hold two previews, which is the ambiguity the index exists for."""
    session = str(uuid.uuid4())
    _new_proposal(conn, tenant, session_id=session, lease="1 hour")
    _retire_lapsed(conn, tenant, session)
    with pytest.raises(pg_errors.UniqueViolation):
        _new_proposal(conn, tenant, session_id=session)
    conn.rollback()


def test_deny_locks_the_document_it_compares_against(schema, tenant):
    """Round 2 major. deny() read the version without locking, so an approval
    could move the document between the comparison and the commit and the
    stale denial the check exists to refuse would still land."""
    with _connect(schema, autocommit=True) as c:
        c.execute("INSERT INTO overlay_documents (tenant_id, version, tokens) "
                  "VALUES (%s, 1, '{}'::jsonb) ON CONFLICT (tenant_id) DO NOTHING",
                  (tenant,))

    denier = _connect(schema)
    denier.execute("SELECT version FROM overlay_documents WHERE tenant_id = %s "
                   "FOR UPDATE", (tenant,))

    blocked, seen = threading.Event(), {}

    def approver():
        c = _connect(schema)
        try:
            blocked.set()
            c.execute("UPDATE overlay_documents SET version = version + 1 "
                      "WHERE tenant_id = %s AND version = 1", (tenant,))
            seen["rows"] = c.cursor().rowcount if False else 1
            c.commit()
        except Exception as exc:            # noqa: BLE001
            seen["error"] = type(exc).__name__
        finally:
            c.close()

    t = threading.Thread(target=approver)
    t.start()
    blocked.wait(timeout=5)
    time.sleep(0.4)

    # While the denier holds the lock the approver CANNOT have committed.
    still = denier.execute("SELECT version FROM overlay_documents "
                           "WHERE tenant_id = %s", (tenant,)).fetchone()
    assert still["version"] == 1, "the approver moved the document mid-denial"
    denier.rollback()
    denier.close()
    t.join(timeout=20)
