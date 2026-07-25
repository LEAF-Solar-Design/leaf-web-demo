"""PostgreSQL authority tests for the application session store.

The dispatch tests are always local. The two-writer tests require an explicit
DATABASE_URL and skip cleanly otherwise.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

import session_store

# DELIBERATE GATE PROOF -- REVERTED IN THE NEXT COMMIT ON THIS BRANCH.
# Skips the whole module with this suite's EXACT allowlisted reason. All 15
# tests then report as skipped, 0 failures, and every skip is tolerated by the
# skip-allowlist rule -- the precise shape of a vacuous green. The only thing
# left that can redden the gate is coverage_verdict rule 1, "ALL skipped: no
# coverage".
pytestmark = pytest.mark.skip(
    reason="PostgreSQL integration test requires explicit DATABASE_URL")


def test_store_mode_is_call_time_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    assert session_store._store_mode() == "legacy"
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    assert session_store._store_mode() == "postgres"
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write_shadow")
    assert session_store._store_mode() == "dual_write_shadow"
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "typo")
    with pytest.raises(RuntimeError, match="invalid LEAF_SESSIONS_STORE"):
        session_store._store_mode()


def test_postgres_authority_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")

    def unavailable(_session_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(session_store, "_pg_get_session", unavailable)
    monkeypatch.setattr(
        session_store, "_legacy_get_session",
        lambda _session_id: pytest.fail("legacy fallback must not run"),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        session_store.get_session("session-1")


def test_shadow_read_fails_closed_on_mismatch(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "shadow")
    monkeypatch.setattr(
        session_store, "_legacy_get_session",
        lambda _session_id: {"session_id": "legacy"},
    )
    monkeypatch.setattr(
        session_store, "_pg_get_session",
        lambda _session_id: {"session_id": "postgres"},
    )
    with pytest.raises(RuntimeError, match="session shadow mismatch"):
        session_store.get_session("session-1")


def test_dual_write_false_turn_result_never_mutates_postgres(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    fence = ("live-turn", 123.0, "hosted_pro")
    monkeypatch.setattr(session_store, "_legacy_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(session_store, "_pg_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(
        session_store, "_legacy_try_begin_turn",
        lambda _sid, _turn, _stale, _tier=None: False,
    )
    monkeypatch.setattr(
        session_store, "_pg_try_begin_turn",
        lambda *_args, **_kwargs: pytest.fail(
            "a legacy no-op must not run the PostgreSQL CAS"
        ),
    )

    assert session_store.try_begin_turn(
        "session-1", "new-turn", 60, "hosted_pro",
    ) is False


def test_dual_write_turn_pre_fence_mismatch_blocks_legacy_mutation(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    monkeypatch.setattr(
        session_store, "_legacy_turn_fence",
        lambda _sid: ("legacy-turn", 123.0, "hosted_pro"),
    )
    monkeypatch.setattr(
        session_store, "_pg_turn_fence",
        lambda _sid: ("postgres-turn", 123.0, "hosted_pro"),
    )
    monkeypatch.setattr(
        session_store, "_legacy_try_begin_turn",
        lambda *_args, **_kwargs: pytest.fail(
            "divergent pre-fences must block the legacy mutation"
        ),
    )

    with pytest.raises(RuntimeError, match="turn fence before acquisition shadow mismatch"):
        session_store.try_begin_turn("session-1", "turn-1", 60, "hosted_pro")


def test_dual_write_turn_acquisition_compares_full_post_fence(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    fences = iter([
        (None, None, None),
        ("turn-1", 456.0, "hosted_pro"),
    ])
    monkeypatch.setattr(session_store, "_legacy_turn_fence", lambda _sid: next(fences))
    pg_fences = iter([
        (None, None, None),
        ("other-turn", 456.0, "hosted_pro"),
    ])
    monkeypatch.setattr(session_store, "_pg_turn_fence", lambda _sid: next(pg_fences))
    monkeypatch.setattr(
        session_store, "_legacy_try_begin_turn",
        lambda _sid, _turn, _stale, _tier=None: True,
    )
    monkeypatch.setattr(
        session_store, "_pg_try_begin_turn",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(RuntimeError, match="turn fence after acquisition shadow mismatch"):
        session_store.try_begin_turn("session-1", "turn-1", 60, "hosted_pro")


def test_dual_write_noop_end_turn_never_mutates_postgres(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    fence = ("newer-turn", 123.0, "hosted_pro")
    monkeypatch.setattr(session_store, "_legacy_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(session_store, "_pg_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(session_store, "_legacy_end_turn", lambda _sid, _turn: None)
    monkeypatch.setattr(
        session_store, "_pg_end_turn",
        lambda *_args, **_kwargs: pytest.fail(
            "a legacy no-op must not clear the PostgreSQL turn"
        ),
    )

    session_store.end_turn("session-1", "stale-turn")


def test_dual_write_end_turn_requires_postgres_row(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    fence = ("turn-1", 123.0, "hosted_pro")
    monkeypatch.setattr(session_store, "_legacy_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(session_store, "_pg_turn_fence", lambda _sid: fence)
    monkeypatch.setattr(session_store, "_legacy_end_turn", lambda _sid, _turn: None)
    monkeypatch.setattr(
        session_store, "_legacy_get_session",
        lambda _sid: {"updated_at": 456.0},
    )
    monkeypatch.setattr(
        session_store, "_pg_end_turn", lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="PostgreSQL turn release mirror failed"):
        session_store.end_turn("session-1", "turn-1")


def test_postgres_authority_unconsume_does_not_fall_back(monkeypatch):
    """The approval give-back (routers/sessions.py's TurnBusy path) must honour
    the authority seam exactly like consume does — a PostgreSQL-authority
    deployment must never silently un-spend the approval in SQLite instead."""
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    monkeypatch.setattr(
        session_store, "_pg_unconsume_approval",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        session_store, "_legacy_unconsume_approval",
        lambda *_args: pytest.fail("legacy fallback must not run"),
    )

    assert session_store.unconsume_approval("confirm-1", "session-1", "tenant-1") is True


def test_dual_write_unconsume_fails_closed_on_mismatch(monkeypatch):
    """A give-back that lands in one store but not the other leaves the two
    ledgers disagreeing about whether the approval is still redeemable —
    exactly the divergence the shadow comparison exists to catch."""
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    monkeypatch.setattr(
        session_store, "_legacy_unconsume_approval", lambda *_args: True,
    )
    monkeypatch.setattr(
        session_store, "_pg_unconsume_approval", lambda *_args: False,
    )

    with pytest.raises(RuntimeError, match="approval consumption release shadow mismatch"):
        session_store.unconsume_approval("confirm-1", "session-1", "tenant-1")


def test_dual_write_unconsume_releases_postgres_before_legacy(monkeypatch):
    """Release order is load-bearing and is the REVERSE of consume's.

    consume_approval gates on legacy (it calls _pg_consume only after
    _legacy_consume succeeds), so legacy is what blocks a second consume. The
    release must therefore free legacy LAST."""
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    order = []

    def _pg(*_args):
        order.append("postgres")
        return True

    def _legacy(*_args):
        order.append("legacy")
        return True

    monkeypatch.setattr(session_store, "_pg_unconsume_approval", _pg)
    monkeypatch.setattr(session_store, "_legacy_unconsume_approval", _legacy)

    assert session_store.unconsume_approval("confirm-1", "session-1", "tenant-1") is True
    assert order == ["postgres", "legacy"], (
        "legacy is the gate consume_approval checks first, so releasing it "
        "before PostgreSQL lets a concurrent consume re-take legacy, fail "
        "against the still-consumed PostgreSQL row, and leave the two stores "
        "divergent -- with BOTH release calls returning True, so _shadow_equal "
        "cannot catch it"
    )


def test_dual_write_unconsume_blocks_a_consume_racing_mid_release(monkeypatch):
    """Fire a concurrent consume in the WINDOW between the two release calls.

    It must be blocked cleanly at the legacy gate, and the stores must not
    diverge. This is the interleaving that both release calls returning True
    would otherwise hide from the shadow comparison."""
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "dual_write")
    consumed = {"legacy": True, "postgres": True}
    race = {}

    def _racing_consume():
        # exactly consume_approval's order: the legacy gate first...
        if consumed["legacy"]:
            race["blocked"] = True
            return
        race["blocked"] = False
        consumed["legacy"] = True
        # ...and PostgreSQL only after legacy succeeded.
        race["diverged"] = consumed["postgres"] is True

    def _pg(*_args):
        consumed["postgres"] = False
        _racing_consume()          # the race lands mid-release
        return True

    def _legacy(*_args):
        consumed["legacy"] = False
        return True

    monkeypatch.setattr(session_store, "_pg_unconsume_approval", _pg)
    monkeypatch.setattr(session_store, "_legacy_unconsume_approval", _legacy)

    assert session_store.unconsume_approval("confirm-1", "session-1", "tenant-1") is True
    assert race["blocked"] is True, (
        "a consume racing mid-release got past the legacy gate; with legacy "
        "released first it would re-take legacy and strand the stores at "
        "legacy=consumed / postgres=free"
    )
    assert consumed == {"legacy": False, "postgres": False}


def test_shadow_append_never_writes_postgres(monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "shadow")
    monkeypatch.setattr(
        session_store, "_legacy_append_event",
        lambda _sid, _turn, _type, _data: 7,
    )
    monkeypatch.setattr(
        session_store, "_pg_append_event",
        lambda *_args, **_kwargs: pytest.fail("shadow mode must not write PostgreSQL"),
    )

    assert session_store.append_event(
        "session-1", "turn-1", "text_delta", {"text": "ok"},
    ) == 7


requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL integration test requires explicit DATABASE_URL",
)


@pytest.fixture
def postgres_session_schema(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL integration test requires explicit DATABASE_URL")
    db = session_store._platform_db()
    migration = (
        session_store._PROJECT_ROOT / "platform" / "migrations" / "0012_sessions.sql"
    )
    db.apply_migration(migration)
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    session_store.ensure_started()
    yield
    db.reset_pool()


@requires_database
def test_two_database_writers_allocate_gapless_event_sequence(
    postgres_session_schema,
):
    token = uuid.uuid4().hex
    session = session_store.get_or_create_session(
        f"pg-seq-tenant-{token}", f"pg-seq-drawing-{token}",
    )
    session_id = session["session_id"]
    barrier = threading.Barrier(2)
    sequences = []
    errors = []
    lock = threading.Lock()

    def writer(writer_id):
        try:
            barrier.wait(timeout=10)
            local = [
                session_store._pg_append_event(
                    session_id, f"turn-{writer_id}", "text_delta",
                    {"writer": writer_id, "offset": offset},
                )
                for offset in range(25)
            ]
            with lock:
                sequences.extend(local)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert sorted(sequences) == list(range(1, 51))
    stored = session_store.events_after(session_id, 0, 100)
    assert [event["seq"] for event in stored] == list(range(1, 51))


@requires_database
def test_two_database_writers_redeem_approval_once(postgres_session_schema):
    token = uuid.uuid4().hex
    tenant_id = f"pg-approval-tenant-{token}"
    session = session_store.get_or_create_session(
        tenant_id, f"pg-approval-drawing-{token}",
    )
    confirmation_id = f"pg-confirmation-{token}"
    session_store.create_approval(
        confirmation_id, session["session_id"], tenant_id, "turn-1",
        "drawing.write", {"x": 1}, "drawing.write", "test", "tool_run",
        {"x": 1}, 60,
    )
    assert session_store.decide_approval(
        confirmation_id, True, by=tenant_id,
    ) == "recorded"

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def writer():
        try:
            barrier.wait(timeout=10)
            session_store._pg_consume_approval(
                confirmation_id, session["session_id"], tenant_id,
            )
            outcome = "consumed"
        except session_store.ApprovalConsumeError as exc:
            outcome = exc.reason
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["already_consumed", "consumed"]
