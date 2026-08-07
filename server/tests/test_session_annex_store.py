"""Store-mode dispatch for the two per-session annex tables.

WHAT THESE TESTS COVER, AND WHAT THEY CANNOT. No PostgreSQL server runs here, so
the PostgreSQL halves are exercised against a recording fake that stands in for
``platform.db``. That proves DISPATCH -- which backend each mode reads, which it
writes, where a shadow compare fires and where it deliberately does not -- and
it proves the SQL each path emits carries its tenant predicate and its conflict
target. It does NOT prove the queries execute against a real schema; the
migration and its constraints are covered by the catalog contract in
platform/db.py and by whatever applies 0029 in CI.

That boundary is why the legacy-mode tests matter as much as the rest: the
repository default is ``legacy``, so those are the assertions that speak for
every deployment running today.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="session-annex-test-"))
os.environ.setdefault("SESSIONS_DB", str(_TMP_DIR / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import checkpoints  # noqa: E402
import platform_link  # noqa: E402
import session_annex  # noqa: E402
import session_policy  # noqa: E402

REPO_ROOT = SERVER_DIR.parent


# --------------------------------------------------------------------------
# A recording stand-in for platform.db.
# --------------------------------------------------------------------------
class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=()):
        return _Result(self._db.respond(sql, params))


class _FakeCursor(_FakeConn):
    def __init__(self, db):
        super().__init__(db)
        self._last = _Result([])

    def execute(self, sql, params=()):
        self._last = _Result(self._db.respond(sql, params))
        return self._last

    def fetchone(self):
        return self._last.fetchone()

    def fetchall(self):
        return self._last.fetchall()


class _CtxWrapper:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc):
        return False


class FakeDb:
    """Records every statement and answers from a scripted queue."""

    def __init__(self):
        self.statements = []
        self.responses = []
        self.transactions = 0

    def queue(self, rows):
        self.responses.append(rows)

    def respond(self, sql, params):
        self.statements.append((" ".join(sql.split()), params))
        if self.responses:
            return self.responses.pop(0)
        return []

    def cursor(self):
        return _CtxWrapper(_FakeCursor(self))

    def transaction(self):
        self.transactions += 1
        return _CtxWrapper(_FakeConn(self))

    def sql(self):
        return [statement for statement, _ in self.statements]

    def touched(self):
        return bool(self.statements)


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(session_annex, "platform_db", lambda: db)
    return db


@pytest.fixture(autouse=True)
def legacy_by_default(monkeypatch):
    # The LITERAL name, never session_annex.SELECTOR. Clearing the constant
    # would follow it if it were ever repointed at LEAF_SESSIONS_STORE, and this
    # fixture would then silently clear the sessions selector for every test in
    # the file — including the one whose whole job is to catch that repointing.
    monkeypatch.delenv("LEAF_SESSION_ANNEX_STORE", raising=False)


_counter = [0]


def _ids():
    _counter[0] += 1
    return f"annex-session-{_counter[0]}", "tenant-a"


def _make_checkpoint(session_id, tenant_id, label=None):
    return checkpoints.create_checkpoint(
        session_id=session_id, tenant_id=tenant_id, drawing_id="drawing-1",
        drawing_version="7", transcript_seq=0, label=label)


# --------------------------------------------------------------------------
# The selector itself.
# --------------------------------------------------------------------------
def test_default_mode_is_legacy_and_a_typo_fails_closed(monkeypatch):
    assert session_annex.store_mode() == "legacy"

    monkeypatch.setenv(session_annex.SELECTOR, "  Postgres  ")
    assert session_annex.store_mode() == "postgres", "value is stripped and lowered"

    monkeypatch.setenv(session_annex.SELECTOR, "postgress")
    with pytest.raises(RuntimeError, match="invalid LEAF_SESSION_ANNEX_STORE"):
        session_annex.store_mode()


def test_mode_is_read_per_call_not_frozen_at_import(monkeypatch):
    """A selector cached at import cannot be changed without a reload, which is
    how a deploy-time flip silently keeps serving the old authority."""
    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    assert session_annex.store_mode() == "postgres"
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    assert session_annex.store_mode() == "legacy"


def test_selector_is_not_the_sessions_selector(monkeypatch):
    """Setting LEAF_SESSIONS_STORE alone must not move the annex authority.

    This is the decision recorded in docs/POSTGRES-CUTOVER.md made executable:
    the two cut over independently, and the coupling is enforced separately by
    platform_link.validate_session_annex_authority.

    Every name below is a LITERAL. Written against session_annex.SELECTOR this
    test passed even with the constant repointed at LEAF_SESSIONS_STORE, because
    the delenv then removed the very variable the setenv had just placed — the
    test defeated itself and reported green. Caught by mutation.
    """
    assert session_annex.SELECTOR == "LEAF_SESSION_ANNEX_STORE"
    monkeypatch.delenv("LEAF_SESSION_ANNEX_STORE", raising=False)
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    assert session_annex.store_mode() == "legacy"


# --------------------------------------------------------------------------
# legacy: the repository default, and what every deployment runs today.
# --------------------------------------------------------------------------
def test_legacy_mode_never_touches_postgres(fake_db):
    session_id, tenant_id = _ids()

    created = _make_checkpoint(session_id, tenant_id, "before edit")
    assert created is not None
    assert checkpoints.list_checkpoints(session_id, tenant_id) == [created]
    assert checkpoints.get_checkpoint(
        session_id, tenant_id, created["checkpoint_id"]) == created

    session_policy.set_policy(session_id, tenant_id, "auto_approve_reads")
    assert session_policy.get_policy(session_id, tenant_id) == "auto_approve_reads"

    assert not fake_db.touched(), (
        "legacy mode reached PostgreSQL; the default must not require a database")


def test_legacy_tenant_scoping_survives_the_refactor(fake_db):
    """The storage-boundary guarantees the pre-dispatch code already had."""
    session_id, tenant_id = _ids()
    _make_checkpoint(session_id, tenant_id)

    assert checkpoints.list_checkpoints(session_id, "tenant-intruder") == []
    session_policy.set_policy(session_id, tenant_id, "plan_first")
    assert session_policy.get_policy(session_id, "tenant-intruder") == (
        session_policy.DEFAULT_POLICY)
    assert not fake_db.touched()


# --------------------------------------------------------------------------
# postgres: authority, and no legacy read behind it.
# --------------------------------------------------------------------------
def test_postgres_mode_reads_and_writes_only_postgres(monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    session_id, tenant_id = _ids()
    row = {
        "checkpoint_id": "cp-1", "session_id": session_id, "tenant_id": tenant_id,
        "drawing_id": "drawing-1", "drawing_version": "7", "transcript_seq": 0,
        "label": None, "created_at": 1.0,
    }
    fake_db.queue([])              # SELECT ... FOR UPDATE
    fake_db.queue([{"n": 0}])      # COUNT
    fake_db.queue([])              # INSERT
    fake_db.queue([row])           # SELECT the inserted row

    created = _make_checkpoint(session_id, tenant_id)

    assert created == row
    assert fake_db.transactions == 1, "the cap count and the insert must share one"
    joined = " ".join(fake_db.sql())
    assert "app_session_checkpoints" in joined
    # An ADVISORY lock, not SELECT ... FOR UPDATE. A row lock has no target on a
    # session with zero rows, so an empty session would serialize nothing and
    # concurrent creates could overshoot the cap (review round 1).
    assert "pg_advisory_xact_lock" in joined, (
        "the cap needs a lock on the session KEY, which holds when the session "
        "has no rows yet")
    assert "FOR UPDATE" not in joined, (
        "a row lock cannot serialize the empty-session case; see round 1")
    # Taken BEFORE the count, or the count can still read a stale snapshot.
    assert fake_db.sql()[0].startswith("SELECT pg_advisory_xact_lock")
    # The legacy file must not have gained a row.
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    assert checkpoints.list_checkpoints(session_id, tenant_id) == []


def test_postgres_checkpoint_reads_are_tenant_scoped_at_the_storage_boundary(
        monkeypatch, fake_db):
    """The same defence the SQLite queries carry, and for the same reason: the
    router's ownership guard is the first wall, not the only one. A PostgreSQL
    read that dropped the tenant predicate would resolve foreign metadata for
    any caller reaching the store directly."""
    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    session_id, tenant_id = _ids()

    fake_db.queue([])
    checkpoints.list_checkpoints(session_id, tenant_id)
    fake_db.queue([])
    checkpoints.get_checkpoint(session_id, tenant_id, "cp-1")

    for statement, params in fake_db.statements:
        assert "session_id = %s AND tenant_id = %s" in statement, statement
        assert tenant_id in params
    # Ordering is pinned too: two checkpoints sharing a created_at must not swap
    # places between calls.
    assert "ORDER BY created_at ASC, checkpoint_id ASC" in fake_db.sql()[0]


def test_postgres_policy_upsert_keeps_the_tenant_guard(monkeypatch, fake_db):
    """Dropping the trailing WHERE would make the PostgreSQL authority strictly
    more permissive than the SQLite one it replaces: another tenant's policy for
    the same session_id would be overwritten instead of left alone."""
    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    session_id, tenant_id = _ids()

    session_policy.set_policy(session_id, tenant_id, "plan_first")

    statement = fake_db.sql()[0]
    assert "ON CONFLICT (session_id) DO UPDATE" in statement
    assert (
        "WHERE app_session_policies.tenant_id = excluded.tenant_id" in statement
    ), "the tenant guard is missing from the PostgreSQL upsert"


def test_postgres_policy_read_is_tenant_scoped_and_fails_closed(monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    session_id, tenant_id = _ids()

    fake_db.queue([{"policy": "auto_approve_reads"}])
    assert session_policy.get_policy(session_id, tenant_id) == "auto_approve_reads"
    assert "tenant_id = %s" in fake_db.sql()[0]

    # A row holding something unknown degrades to the safe default rather than
    # propagating a value no caller knows how to honour.
    fake_db.queue([{"policy": "yolo"}])
    assert session_policy.get_policy(session_id, tenant_id) == (
        session_policy.DEFAULT_POLICY)

    fake_db.queue([])
    assert session_policy.get_policy(session_id, tenant_id) == (
        session_policy.DEFAULT_POLICY)


# --------------------------------------------------------------------------
# dual_write: legacy is authority, PostgreSQL is a mirror.
# --------------------------------------------------------------------------
def _queue_mirror_create(fake_db, session_id, tenant_id, checkpoint_id, created_at):
    fake_db.queue([])          # ensure_started to_regclass -> filled below
    fake_db.queue([])          # FOR UPDATE
    fake_db.queue([{"n": 0}])  # COUNT
    fake_db.queue([])          # INSERT
    fake_db.queue([{
        "checkpoint_id": checkpoint_id, "session_id": session_id,
        "tenant_id": tenant_id, "drawing_id": "drawing-1", "drawing_version": "7",
        "transcript_seq": 0, "label": None, "created_at": created_at,
    }])


def test_dual_write_returns_legacy_and_mirrors_the_same_identity(
        monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "dual_write")
    session_id, tenant_id = _ids()
    # ensure_started's to_regclass probe must report both tables present, then
    # the pre-check counts the mirror and finds it well under the cap.
    fake_db.queue([{"checkpoints": "app_session_checkpoints",
                    "policies": "app_session_policies"}])
    fake_db.queue([{"n": 0}])
    captured = {}

    real = checkpoints._pg_create_checkpoint

    def spy(*args, **kwargs):
        captured.update(kwargs)
        fake_db.queue([])
        fake_db.queue([{"n": 0}])
        fake_db.queue([])
        fake_db.queue([{
            "checkpoint_id": kwargs["checkpoint_id"], "session_id": session_id,
            "tenant_id": tenant_id, "drawing_id": "drawing-1",
            "drawing_version": "7", "transcript_seq": 0, "label": None,
            "created_at": kwargs["created_at"],
        }])
        return real(*args, **kwargs)

    monkeypatch.setattr(checkpoints, "_pg_create_checkpoint", spy)
    created = _make_checkpoint(session_id, tenant_id)

    assert created is not None
    # The mirror is handed the legacy row's identity and timestamp rather than
    # minting its own, or the two stores diverge on the first write.
    assert captured["checkpoint_id"] == created["checkpoint_id"]
    assert captured["created_at"] == created["created_at"]
    # Legacy remains the authority for the returned value.
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    assert checkpoints.list_checkpoints(session_id, tenant_id) == [created]


def test_dual_write_refuses_when_the_mirror_schema_is_absent(monkeypatch, fake_db):
    """The pre-flight narrows the source-only window. It does not close it --
    the two stores share no transaction -- but a KNOWN-missing mirror must not
    be written past."""
    monkeypatch.setenv(session_annex.SELECTOR, "dual_write")
    session_id, tenant_id = _ids()
    fake_db.queue([{"checkpoints": None, "policies": None}])

    with pytest.raises(RuntimeError, match="0029_session_annex.sql"):
        _make_checkpoint(session_id, tenant_id)

    # And the legacy authority was not mutated first.
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    assert checkpoints.list_checkpoints(session_id, tenant_id) == []


def test_the_legacy_policy_timestamp_is_sampled_under_the_lock(monkeypatch, fake_db):
    """Sampling before the lock lets two setters commit in the opposite order
    from their timestamps, so updated_at moves backwards -- a real change from
    the pre-dispatch behaviour (review round 1). The writer must sample inside
    its own lock and hand the value back for the mirror."""
    session_id, tenant_id = _ids()
    observed = []
    real_time = time.time

    # Captured BEFORE the monkeypatch, or the wrapper resolves the module
    # attribute it just replaced and recurses into itself.
    real_lock = session_policy._lock

    class _Recorder:
        def __enter__(self):
            observed.append("lock")
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    def spy_time():
        observed.append("time")
        return real_time()

    monkeypatch.setattr(session_policy, "_lock", _Recorder())
    monkeypatch.setattr(session_policy.time, "time", spy_time)
    stamped = session_policy._legacy_set_policy(session_id, tenant_id, "plan_first")

    assert observed[:2] == ["lock", "time"], (
        f"timestamp was sampled outside the lock: {observed}")
    assert isinstance(stamped, float), "the writer must return the value it stored"


def test_dual_write_policy_uses_one_timestamp_for_both_stores(monkeypatch, fake_db):
    """Two time.time() calls would make every dual_write_shadow read compare
    rows that differ only on updated_at, which reads as corruption."""
    monkeypatch.setenv(session_annex.SELECTOR, "dual_write")
    session_id, tenant_id = _ids()
    fake_db.queue([{"checkpoints": "app_session_checkpoints",
                    "policies": "app_session_policies"}])

    session_policy.set_policy(session_id, tenant_id, "plan_first")

    upserts = [
        params for statement, params in fake_db.statements
        if "INSERT INTO app_session_policies" in statement
    ]
    assert len(upserts) == 1
    mirrored_at = upserts[0][3]
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    stored = session_policy._legacy_get_policy(session_id, tenant_id)
    assert stored == "plan_first"
    row = session_policy._db().execute(
        "SELECT updated_at FROM session_policies WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row[0] == mirrored_at, "the two stores were stamped separately"


# --------------------------------------------------------------------------
# shadow: compares reads, and deliberately does NOT compare writes.
# --------------------------------------------------------------------------
def test_shadow_read_mismatch_fails_closed(monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "shadow")
    session_id, tenant_id = _ids()
    monkeypatch.setattr(
        checkpoints, "_pg_list_checkpoints", lambda *a: [{"checkpoint_id": "ghost"}])

    with pytest.raises(RuntimeError, match="checkpoints shadow mismatch"):
        checkpoints.list_checkpoints(session_id, tenant_id)


def test_shadow_agreeing_read_returns_the_legacy_value(monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    session_id, tenant_id = _ids()
    created = _make_checkpoint(session_id, tenant_id)

    monkeypatch.setenv(session_annex.SELECTOR, "shadow")
    monkeypatch.setattr(checkpoints, "_pg_list_checkpoints", lambda *a: [created])
    assert checkpoints.list_checkpoints(session_id, tenant_id) == [created]

    monkeypatch.setattr(checkpoints, "_pg_get_checkpoint", lambda *a: created)
    assert checkpoints.get_checkpoint(
        session_id, tenant_id, created["checkpoint_id"]) == created


def test_shadow_write_does_not_compare_against_a_target_it_never_updated(
        monkeypatch, fake_db):
    """`shadow` never writes PostgreSQL, so comparing straight after a write
    would compare against a target the mode deliberately left alone -- every
    create would raise. session_store.append_event follows the same rule."""
    monkeypatch.setenv(session_annex.SELECTOR, "shadow")
    session_id, tenant_id = _ids()

    created = _make_checkpoint(session_id, tenant_id)
    session_policy.set_policy(session_id, tenant_id, "plan_first")

    assert created is not None
    assert not fake_db.touched(), (
        "a shadow-mode write reached PostgreSQL or compared against it")


def test_shadow_policy_mismatch_fails_closed(monkeypatch, fake_db):
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    session_id, tenant_id = _ids()
    session_policy.set_policy(session_id, tenant_id, "plan_first")

    monkeypatch.setenv(session_annex.SELECTOR, "dual_write_shadow")
    monkeypatch.setattr(
        session_policy, "_pg_get_policy", lambda *a: "auto_approve_reads")
    with pytest.raises(RuntimeError, match="session policy shadow mismatch"):
        session_policy.get_policy(session_id, tenant_id)

    monkeypatch.setattr(session_policy, "_pg_get_policy", lambda *a: "plan_first")
    assert session_policy.get_policy(session_id, tenant_id) == "plan_first"


# --------------------------------------------------------------------------
# The cap, which is the one invariant a second store can break.
# --------------------------------------------------------------------------
def test_cap_is_enforced_in_both_backends(monkeypatch, fake_db):
    session_id, tenant_id = _ids()
    for _ in range(checkpoints.CHECKPOINT_CAP):
        assert _make_checkpoint(session_id, tenant_id) is not None
    assert _make_checkpoint(session_id, tenant_id) is None

    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    other_id, _ = _ids()
    fake_db.queue([])
    fake_db.queue([{"n": checkpoints.CHECKPOINT_CAP}])
    assert _make_checkpoint(other_id, tenant_id) is None
    assert not any("INSERT" in statement for statement in fake_db.sql())


def test_a_full_mirror_against_an_unfull_legacy_is_loud(monkeypatch, fake_db):
    """Silently returning the legacy row would leave a checkpoint the mirror
    will never hold, and no later parity read would explain why.

    It must ALSO refuse without writing. Round 2 caught the raise landing after
    the legacy insert had committed, so a client retrying against the realistic
    staging divergence (empty task-local SQLite, full PostgreSQL) wrote one
    hidden checkpoint per 500 until the two counts met.
    """
    monkeypatch.setenv(session_annex.SELECTOR, "dual_write")
    session_id, tenant_id = _ids()
    # ensure_started(), then the pre-check's PostgreSQL count: already full.
    fake_db.queue([{"checkpoints": "app_session_checkpoints",
                    "policies": "app_session_policies"}])
    fake_db.queue([{"n": checkpoints.CHECKPOINT_CAP}])
    monkeypatch.setattr(checkpoints, "_pg_create_checkpoint", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="mirror is at its cap"):
        _make_checkpoint(session_id, tenant_id)

    # The legacy authority was NOT mutated, so a retry refuses the same way
    # instead of committing another row.
    assert checkpoints._legacy_checkpoint_count(session_id) == 0
    assert not any("INSERT" in statement for statement in fake_db.sql())


def test_both_stores_at_the_cap_report_the_cap_rather_than_a_divergence(
        monkeypatch, fake_db):
    """Agreement is not divergence: a full session is a 409, not a 500."""
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    session_id, tenant_id = _ids()
    for _ in range(checkpoints.CHECKPOINT_CAP):
        assert _make_checkpoint(session_id, tenant_id) is not None

    monkeypatch.setenv(session_annex.SELECTOR, "dual_write")
    fake_db.queue([{"checkpoints": "app_session_checkpoints",
                    "policies": "app_session_policies"}])
    fake_db.queue([{"n": checkpoints.CHECKPOINT_CAP}])
    assert _make_checkpoint(session_id, tenant_id) is None


# --------------------------------------------------------------------------
# The selector dependency, enforced rather than merely recorded.
# --------------------------------------------------------------------------
def test_postgres_sessions_without_postgres_annex_refuses_to_start(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    for annex in ("legacy", "dual_write", "dual_write_shadow", "shadow"):
        monkeypatch.setenv(session_annex.SELECTOR, annex)
        with pytest.raises(RuntimeError, match="requires LEAF_SESSION_ANNEX_STORE"):
            platform_link.validate_session_annex_authority()

    monkeypatch.setenv(session_annex.SELECTOR, "postgres")
    platform_link.validate_session_annex_authority()


def test_the_dependency_only_binds_a_postgres_sessions_authority(monkeypatch):
    """Every pre-flip sessions mode is left alone, including the one staging
    runs today, so this change cannot break a deployment that has not flipped."""
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    for sessions in ("legacy", "dual_write", "dual_write_shadow", "shadow"):
        monkeypatch.setenv("LEAF_SESSIONS_STORE", sessions)
        platform_link.validate_session_annex_authority()


def test_the_dependency_is_reached_from_the_startup_gate(monkeypatch):
    """A validator nothing calls is a comment. `sessions=postgres` alone makes
    postgres_startup_required() true, so the gate cannot early-return past it."""
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    monkeypatch.delenv("LEAF_PLATFORM_POSTGRES_REQUIRED", raising=False)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "development")

    assert platform_link.postgres_startup_required() is True
    with pytest.raises(RuntimeError, match="requires LEAF_SESSION_ANNEX_STORE"):
        platform_link.validate_postgres_startup()


def test_annex_selector_is_validated_at_startup(monkeypatch):
    monkeypatch.setenv(session_annex.SELECTOR, "postgress")
    with pytest.raises(RuntimeError, match="LEAF_SESSION_ANNEX_STORE must be one of"):
        platform_link.postgres_authorities_selected()


def test_a_postgres_touching_annex_mode_requires_the_database(monkeypatch):
    for annex in ("dual_write", "dual_write_shadow", "shadow", "postgres"):
        monkeypatch.setenv(session_annex.SELECTOR, annex)
        assert platform_link.postgres_authorities_selected() is True, annex
    monkeypatch.setenv(session_annex.SELECTOR, "legacy")
    monkeypatch.delenv("LEAF_SESSIONS_STORE", raising=False)
    assert platform_link.postgres_authorities_selected() is False


# --------------------------------------------------------------------------
# Migration and config surfaces.
# --------------------------------------------------------------------------
def test_migration_declares_both_tables_and_no_session_foreign_key():
    sql = (REPO_ROOT / "platform" / "migrations" / "0029_session_annex.sql").read_text(
        encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app_session_checkpoints" in sql
    assert "CREATE TABLE IF NOT EXISTS app_session_policies" in sql
    # An FK would make `annex=postgres, sessions=legacy` fail at INSERT time
    # with a 500 rather than at review time. See the migration's header.
    assert "REFERENCES app_sessions" not in sql


def test_migration_policy_check_matches_the_python_policy_set():
    """Two copies of the same rule, so pin them together. A policy added to
    POLICIES without the CHECK becomes an unwritable value in PostgreSQL; a
    value dropped from POLICIES but left in the CHECK becomes storable garbage
    that get_policy then silently degrades."""
    sql = (REPO_ROOT / "platform" / "migrations" / "0029_session_annex.sql").read_text(
        encoding="utf-8")
    checked = {
        value for value in session_policy.POLICIES if f"'{value}'" in sql
    }
    assert checked == set(session_policy.POLICIES), (
        "0029's policy CHECK and session_policy.POLICIES disagree")


def test_the_image_ships_a_legacy_default_and_the_manifest_stays_out_of_it():
    """The image bakes the selector; `required-config.app.json` deliberately
    does NOT require it yet, and that asymmetry is the finding this pins.

    PR #499 required `LEAF_SESSIONS_STORE` in the manifest because its absence
    means task-local SQLite and real loss. The same move here would WEDGE the
    pipeline: the staging deploy's manifest check compares the manifest against
    a configuration baseline cloned from the previously live task definition,
    which cannot contain a brand-new variable, and the configuration-delta lane
    cannot introduce it either because its allowlist is positive and
    value-exact (`LEAF_JOBS_STORE=postgres` and the five `LEAF_SESSIONS_STORE`
    values only).

    The safety the manifest entry would buy is close to zero here, which is why
    the sequencing wins: an absent selector resolves to `legacy`, which is
    byte-identical to current behaviour, and the combination that actually harms
    a user is refused at startup by validate_session_annex_authority whether or
    not the manifest names the variable.

    Re-add it only after the infrastructure repository carries the variable, in
    that order. See docs/POSTGRES-CUTOVER.md.
    """
    import json

    required = json.loads(
        (REPO_ROOT / "deploy" / "required-config.app.json").read_text(
            encoding="utf-8"))
    assert "LEAF_SESSION_ANNEX_STORE" not in required["required"]["environment"], (
        "requiring the selector before the task definitions carry it blocks the "
        "next app deploy; see the docstring")

    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.app").read_text(encoding="utf-8")
    assert "LEAF_SESSION_ANNEX_STORE=legacy" in dockerfile
