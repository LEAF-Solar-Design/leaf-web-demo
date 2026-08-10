"""P5a durable request journal contract and integration tests."""
from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import request_journal  # noqa: E402
import turn_runner  # noqa: E402
from routers import sessions  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.result = _Result([])

    def execute(self, sql, params=()):
        self.result = self.db.execute(sql, params)
        return self.result

    def fetchone(self):
        return self.result.fetchone()

    def fetchall(self):
        return self.result.fetchall()


class FakeDb:
    """Small stateful PostgreSQL double for the journal's exact statements."""

    def __init__(self):
        self.rows = {}
        self.sql = []

    @contextmanager
    def transaction(self):
        yield self

    @contextmanager
    def cursor(self):
        yield _Cursor(self)

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.sql.append((normalized, params))
        if "to_regclass('app_session_requests')" in normalized:
            return _Result([{
                "requests": "app_session_requests",
                "executing_index": "idx_app_session_requests_one_executing",
                "queued_index": "idx_app_session_requests_one_queued",
            }])
        if normalized.startswith("INSERT INTO app_session_requests"):
            request_id = params[0]
            if request_id in self.rows:
                return _Result([])
            self.rows[request_id] = {
                "request_id": request_id, "tenant_id": params[1],
                "drawing_id": params[2], "session_id": params[3],
                "principal_key": params[4], "payload_digest": params[5],
                "recoverable_json": None, "state": "admitted",
                "turn_id": None, "lease_owner": None,
                "lease_expires_at": None, "response_status": None,
                "response_json": None, "created_at": params[6],
                "updated_at": params[7], "terminal_at": None,
            }
            return _Result([{"request_id": request_id}])
        if normalized.startswith("SELECT * FROM app_session_requests WHERE request_id"):
            row = self.rows.get(params[0])
            return _Result([dict(row)] if row else [])
        if normalized.startswith(
            "UPDATE app_session_requests SET state='abandoned', response_status=503"
        ) and "WHERE state='executing' AND lease_expires_at <" in normalized:
            response, terminal, updated, now, *scope = params
            request_id = None
            tenant_id = None
            drawing_id = None
            if "request_id=%s" in normalized:
                request_id = scope.pop(0)
            if "tenant_id=%s" in normalized:
                tenant_id = scope.pop(0)
            if "drawing_id=%s" in normalized:
                drawing_id = scope.pop(0)
            changed = []
            for row in self.rows.values():
                if row["state"] != "executing" or row["lease_expires_at"] >= now:
                    continue
                if request_id is not None and row["request_id"] != request_id:
                    continue
                if tenant_id is not None and row["tenant_id"] != tenant_id:
                    continue
                if drawing_id is not None and row["drawing_id"] != drawing_id:
                    continue
                row.update(state="abandoned", response_status=503,
                           response_json=response, terminal_at=terminal,
                           updated_at=updated, lease_owner=None,
                           lease_expires_at=None)
                changed.append({"request_id": row["request_id"],
                                "session_id": row["session_id"],
                                "turn_id": row["turn_id"]})
            return _Result(changed)
        if "SET state='executing'" in normalized and normalized.startswith("UPDATE"):
            turn_id, owner, lease, updated, request_id = params
            row = self.rows.get(request_id)
            blocked = row is None or row["state"] != "admitted" or any(
                other["session_id"] == row["session_id"]
                and other["state"] == "executing"
                and other["request_id"] != request_id
                for other in self.rows.values()
            )
            if blocked:
                return _Result([])
            row.update(state="executing", turn_id=turn_id, lease_owner=owner,
                       lease_expires_at=lease, updated_at=updated)
            return _Result([{"request_id": request_id}])
        if normalized.startswith("UPDATE app_session_requests AS candidate SET state='queued'"):
            recoverable, updated, request_id = params
            row = self.rows.get(request_id)
            if row is None or row["state"] != "admitted" or any(
                other["session_id"] == row["session_id"]
                and other["state"] == "queued"
                for other in self.rows.values()
            ):
                return _Result([])
            row.update(state="queued", recoverable_json=recoverable,
                       turn_id=None, lease_owner=None,
                       lease_expires_at=None, updated_at=updated)
            return _Result([{"request_id": request_id}])
        if normalized.startswith("UPDATE app_session_requests SET state=%s") \
                and "response_status" not in normalized:
            state, recoverable, updated, request_id, turn_id = params
            row = self.rows.get(request_id)
            if row is None or row["state"] != "executing" or row["turn_id"] != turn_id:
                return _Result([])
            if state == "queued" and any(
                other["session_id"] == row["session_id"]
                and other["state"] == "queued"
                and other["request_id"] != request_id
                for other in self.rows.values()
            ):
                raise request_journal.UniqueViolation("queued slot already occupied")
            row.update(state=state, recoverable_json=recoverable,
                       turn_id=None, lease_owner=None,
                       lease_expires_at=None, updated_at=updated)
            return _Result([{"request_id": request_id}])
        if normalized.startswith("WITH next_request AS"):
            session_id, _same, turn_id, owner, lease, updated = params
            candidates = [row for row in self.rows.values() if row["state"] == "queued"]
            if session_id is not None:
                candidates = [row for row in candidates if row["session_id"] == session_id]
            candidates = [
                row for row in candidates
                if not any(
                    active["session_id"] == row["session_id"]
                    and active["state"] == "executing"
                    for active in self.rows.values()
                )
            ]
            candidates.sort(key=lambda row: (row["created_at"], row["request_id"]))
            if not candidates:
                return _Result([])
            row = candidates[0]
            recoverable = row["recoverable_json"]
            row.update(state="executing", turn_id=turn_id, lease_owner=owner,
                       lease_expires_at=lease, updated_at=updated,
                       recoverable_json=None)
            result = dict(row)
            result["claimed_recoverable_json"] = recoverable
            return _Result([result])
        if normalized.startswith("SELECT * FROM app_session_requests WHERE session_id"):
            row = next((row for row in self.rows.values()
                        if row["session_id"] == params[0] and row["state"] == "queued"), None)
            return _Result([dict(row)] if row else [])
        if normalized.startswith(
            "SELECT MIN(active.lease_expires_at) AS lease_expires_at"
        ):
            expiries = [
                active["lease_expires_at"]
                for queued in self.rows.values()
                if queued["state"] == "queued"
                for active in self.rows.values()
                if active["session_id"] == queued["session_id"]
                and active["state"] == "executing"
            ]
            return _Result([{
                "lease_expires_at": min(expiries) if expiries else None,
            }])
        if normalized.startswith("UPDATE app_session_requests SET state=%s"):
            state, status, response, terminal, updated, request_id, turn_id = params
            row = self.rows.get(request_id)
            if row is None or row["state"] != "executing" or row["turn_id"] != turn_id:
                return _Result([])
            row.update(state=state, response_status=status, response_json=response,
                       terminal_at=terminal, updated_at=updated,
                       recoverable_json=None, lease_owner=None,
                       lease_expires_at=None)
            return _Result([{"request_id": request_id}])
        if normalized.startswith("SELECT state, COUNT(*) AS count"):
            tenant_id, drawing_id = params
            counts = {}
            for row in self.rows.values():
                if row["tenant_id"] == tenant_id and row["drawing_id"] == drawing_id \
                        and row["state"] in {"queued", "executing"}:
                    counts[row["state"]] = counts.get(row["state"], 0) + 1
            return _Result([{"state": state, "count": count}
                            for state, count in counts.items()])
        if normalized.startswith("UPDATE app_session_requests SET state='abandoned'"):
            _response, terminal, updated, now, cutoff = params
            changed = []
            for row in self.rows.values():
                stale = row["state"] == "executing" and row["lease_expires_at"] < now
                stale = stale or row["state"] == "admitted" and row["created_at"] < cutoff
                if stale:
                    row.update(state="abandoned", response_status=503,
                               response_json=_response, terminal_at=terminal,
                               updated_at=updated, lease_owner=None,
                               lease_expires_at=None)
                    changed.append({"request_id": row["request_id"],
                                    "session_id": row["session_id"],
                                    "turn_id": row["turn_id"]})
            return _Result(changed)
        if normalized.startswith("UPDATE app_sessions SET active_turn_id=NULL"):
            return _Result([])
        raise AssertionError(f"unhandled SQL: {normalized}")


@pytest.fixture()
def journal(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(request_journal, "platform_db", lambda: db)
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    return db


def _admit(db, *, request_id=None, tenant="tenant-a", drawing="drawing-a",
           session="session-a", text="first"):
    request_id = request_id or str(uuid.uuid4())
    row, inserted = request_journal.admit_request(
        request_id=request_id, tenant_id=tenant, drawing_id=drawing,
        session_id=session, principal_key="auth0|alice",
        digest=request_journal.payload_digest({"text": text}),
    )
    return request_id, row, inserted


def _recoverable(text="first", classifier_hint=None):
    return {
        "text": text, "classifier_hint": classifier_hint, "model": None,
        "tier": "hosted_pro", "subject": "auth0|alice",
        "entitlement_tier": "hosted_pro", "entitlement_roles": [],
        "entitlement_elevated": False,
    }


def test_migration_sequence_is_collision_free_and_manifest_ordered():
    migrations = sorted((REPO_ROOT / "platform" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    names = [path.name for path in migrations]
    assert "0035_session_request_journal.sql" in names
    assert len({name[:4] for name in names}) == len(names), "migration prefixes must be unique"
    assert names.index("0035_session_request_journal.sql") == len(names) - 1
    sql = (REPO_ROOT / "platform" / "migrations" /
           "0035_session_request_journal.sql").read_text(encoding="utf-8")
    assert "idx_app_session_requests_one_executing" in sql
    assert "idx_app_session_requests_one_queued" in sql
    assert "REFERENCES app_sessions(session_id) ON DELETE CASCADE" in sql


def test_admission_replays_exact_binding_and_rejects_cross_tenant(journal):
    request_id, first, inserted = _admit(journal)
    assert inserted is True and first["state"] == "admitted"
    assert first["recoverable_json"] is None
    assert request_journal.begin_request(request_id, "turn-1", lease_seconds=30)
    assert journal.rows[request_id]["recoverable_json"] is None
    request_journal.release_execution(request_id, "turn-1", requeue=False)
    _, replay, inserted = _admit(journal, request_id=request_id)
    assert inserted is False and replay["request_id"] == request_id
    with pytest.raises(request_journal.RequestConflict):
        _admit(journal, request_id=request_id, tenant="tenant-b")
    with pytest.raises(request_journal.RequestConflict):
        _admit(journal, request_id=request_id, drawing="drawing-b")


def test_one_active_turn_fence_is_per_session(journal):
    first, *_ = _admit(journal, session="session-a", text="one")
    second, *_ = _admit(journal, session="session-a", text="two")
    other, *_ = _admit(journal, session="session-b", drawing="drawing-b", text="three")
    assert request_journal.begin_request(first, "turn-1", lease_seconds=30) is True
    assert request_journal.begin_request(second, "turn-2", lease_seconds=30) is False
    assert request_journal.begin_request(other, "turn-3", lease_seconds=30) is True


def test_release_execution_queue_slot_race_preserves_admitted_retry(journal):
    older, *_ = _admit(journal, session="session-a", text="older")
    newer, *_ = _admit(journal, session="session-a", text="newer")
    recovered = {"text": "older", "classifier_hint": {"layout": "solar"}}

    assert request_journal.begin_request(older, "turn-older", lease_seconds=30)
    assert request_journal.queue_request(newer, {"text": "newer"})

    assert request_journal.release_execution(
        older, "turn-older", requeue=True, recoverable=recovered,
    )
    older_row = journal.rows[older]
    assert older_row["state"] == "admitted"
    assert older_row["recoverable_json"] is None
    assert older_row["response_status"] is None
    assert older_row["terminal_at"] is None
    assert journal.rows[newer]["state"] == "queued"

    # The immutable request survives for an exact retry and is never replayed
    # from the displaced recoverable payload.
    _, replay, inserted = _admit(
        journal, request_id=older, session="session-a", text="older",
    )
    assert inserted is False
    assert replay["state"] == "admitted"
    assert request_journal.begin_request(older, "turn-retry", lease_seconds=30)


def test_queue_payload_is_written_only_by_transition_and_cleared_on_claim(journal):
    request_id, *_ = _admit(journal)
    assert journal.rows[request_id]["recoverable_json"] is None
    recoverable = _recoverable(classifier_hint={"layout": {"mode": "solar"}})
    assert request_journal.queue_request(request_id, recoverable) is True
    assert journal.rows[request_id]["recoverable_json"]["text"] == "first"
    claimed = request_journal.claim_next_queued(lease_seconds=30)
    assert claimed["request_id"] == request_id
    assert claimed["recoverable_json"]["text"] == "first"
    assert claimed["recoverable_json"]["classifier_hint"] == {
        "layout": {"mode": "solar"},
    }
    assert journal.rows[request_id]["recoverable_json"] is None
    response = {"request_id": request_id, "turn_id": claimed["turn_id"],
                "status": "completed", "stop_reason": "end_turn"}
    assert request_journal.finish_request(
        request_id, claimed["turn_id"], state="completed",
        response_status=200, response=response,
    ) is True
    assert request_journal.get_request(request_id)["response_json"] == response
    assert request_journal.finish_request(
        request_id, claimed["turn_id"], state="completed",
        response_status=200, response={"status": "changed"},
    ) is False


def test_replacement_settles_execution_that_expires_after_startup(journal, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(request_journal.time, "time", lambda: clock[0])
    request_id, *_ = _admit(journal)
    assert request_journal.begin_request(request_id, "turn-1", lease_seconds=600)

    # The replacement starts while the crashed task's lease still looks live.
    assert request_journal.settle_abandoned() == 0
    assert request_journal.active_counts("tenant-a", "drawing-a")["active"] == 1

    # After expiry, an exact retry settles atomically instead of replaying work.
    clock[0] = 701.0
    _, row, inserted = _admit(journal, request_id=request_id)
    assert inserted is False
    assert row["state"] == "abandoned"
    assert request_journal.active_counts("tenant-a", "drawing-a") == {
        "queued": 0, "executing": 0, "active": 0,
    }
    assert request_journal.claim_next_queued(lease_seconds=30) is None


def test_startup_recovery_rearms_until_live_lease_expires(journal, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(request_journal.time, "time", lambda: clock[0])
    executing, *_ = _admit(journal, text="already running")
    queued, *_ = _admit(journal, text="recover me")
    assert request_journal.begin_request(executing, "turn-1", lease_seconds=600)
    assert request_journal.queue_request(queued, _recoverable("recover me"))

    timers = []

    class FakeTimer:
        def __init__(self, delay, target):
            self.delay = delay
            self.target = target
            self.daemon = False
            timers.append(self)

        def is_alive(self):
            return True

        def start(self):
            return None

    monkeypatch.setattr(turn_runner.threading, "Timer", FakeTimer)
    monkeypatch.setattr(turn_runner, "_durable_recovery_timer", None)
    monkeypatch.setattr(
        turn_runner.entitlements, "entitlements_for",
        lambda *_a, **_kw: {"converse": True},
    )
    starts = []
    monkeypatch.setattr(
        turn_runner, "start_turn",
        lambda *args, **kwargs: starts.append((args, kwargs)) or kwargs["turn_id"],
    )

    # A replacement process must not abandon a lease that is still live, but
    # it must arm one later recovery pass for the queued successor.
    turn_runner.recover_queued_turns()
    assert starts == []
    assert len(timers) == 1
    assert timers[0].delay == pytest.approx(600.0)

    clock[0] = 701.0
    timers[0].target()
    assert journal.rows[executing]["state"] == "abandoned"
    assert len(starts) == 1
    assert starts[0][1]["request_id"] == queued
    assert starts[0][1]["journal_claimed"] is True
    assert starts[0][1]["text"] == "recover me"
    assert journal.rows[queued]["recoverable_json"] is None


def test_active_counts_are_tenant_and_drawing_scoped(journal):
    queued, *_ = _admit(journal, session="session-a")
    executing, *_ = _admit(journal, session="session-b", text="two")
    foreign, *_ = _admit(journal, tenant="tenant-b", drawing="drawing-b",
                         session="session-c", text="three")
    assert request_journal.queue_request(queued, _recoverable())
    assert request_journal.begin_request(executing, "turn-2", lease_seconds=30)
    assert request_journal.begin_request(foreign, "turn-3", lease_seconds=30)
    assert request_journal.active_counts("tenant-a", "drawing-a") == {
        "queued": 1, "executing": 1, "active": 2,
    }


def test_durable_kicker_dispatches_claimed_payload_once(monkeypatch):
    request_id = str(uuid.uuid4())
    claims = [{
        "request_id": request_id, "turn_id": "turn-1", "tenant_id": "tenant-a",
        "session_id": "session-a", "recoverable_json": {
            "text": "recover me", "classifier_hint": None, "model": None,
            "tier": "hosted_pro", "subject": "auth0|alice",
            "entitlement_tier": "hosted_pro", "entitlement_roles": [],
            "entitlement_elevated": False,
        },
    }, None]
    monkeypatch.setattr(request_journal, "enabled", lambda: True)
    monkeypatch.setattr(request_journal, "claim_next_queued", lambda **_kw: claims.pop(0))
    monkeypatch.setattr(
        turn_runner.entitlements, "entitlements_for",
        lambda *_a, **_kw: {"converse": True},
    )
    calls = []
    monkeypatch.setattr(turn_runner, "start_turn", lambda *a, **kw: calls.append((a, kw)))
    turn_runner._kick_durable_queued()
    assert len(calls) == 1
    assert calls[0][1]["request_id"] == request_id
    assert calls[0][1]["journal_claimed"] is True
    assert calls[0][1]["text"] == "recover me"


def test_terminal_retry_returns_stored_result_without_dispatch(monkeypatch):
    request_id = str(uuid.uuid4())
    row = {
        "request_id": request_id, "tenant_id": "tenant-a",
        "drawing_id": "drawing-a", "session_id": "session-a",
        "state": "completed", "turn_id": "turn-1", "response_status": 200,
        "response_json": {"request_id": request_id, "turn_id": "turn-1",
                          "status": "completed", "stop_reason": "end_turn"},
    }
    class Tenant(str):
        subject = "auth0|alice"
        tier = "hosted_pro"
    tenant = Tenant("tenant-a")
    monkeypatch.setattr(request_journal, "enabled", lambda: True)
    monkeypatch.setattr(request_journal, "canonical_request_id", lambda _v: request_id)
    monkeypatch.setattr(request_journal, "payload_digest", lambda _v: "a" * 64)
    monkeypatch.setattr(request_journal, "admit_request", lambda **_kw: (row, False))
    live_counts = iter((
        {"queued": 0, "executing": 0, "active": 0},
        {"queued": 1, "executing": 0, "active": 1},
    ))
    monkeypatch.setattr(request_journal, "active_counts", lambda *_a: next(live_counts))
    monkeypatch.setattr(sessions, "_require_owned_session",
                        lambda *_a: {"drawing_id": "drawing-a"})
    monkeypatch.setattr(sessions.entitlements, "resolve_tier", lambda _t: "hosted_pro")
    monkeypatch.setattr(sessions.entitlements, "resolve_roles", lambda _t: ((), False))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for",
                        lambda *_a, **_kw: {"converse": True})
    monkeypatch.setattr(turn_runner, "start_turn",
                        lambda *_a, **_kw: pytest.fail("terminal replay dispatched"))
    req = sessions.MessageRequest(text="first", request_id=request_id)
    first_response = sessions.post_message("session-a", req, object(), tenant)
    second_response = sessions.post_message("session-a", req, object(), tenant)
    assert first_response.status_code == second_response.status_code == 200
    first_body = json.loads(first_response.body)
    second_body = json.loads(second_response.body)
    first_counts = first_body.pop("active_requests")
    second_counts = second_body.pop("active_requests")
    assert first_body == second_body
    assert first_body["request_id"] == request_id
    assert first_body["status"] == "completed"
    assert first_counts == {"queued": 0, "executing": 0, "active": 0}
    assert second_counts == {"queued": 1, "executing": 0, "active": 1}
    assert row["response_json"] == {
        "request_id": request_id, "turn_id": "turn-1",
        "status": "completed", "stop_reason": "end_turn",
    }


@pytest.mark.parametrize(
    ("text", "classifier_hint", "secret_fragment"),
    (
        ("Bearer abcdefghijklmnop", None, "abcdefghijklmnop"),
        ("ordinary request", {"nested": {"authorization": "hidden-value"}},
         "hidden-value"),
        ("ordinary request", {"nested": [{"api_key": "sk-hidden-value"}]},
         "sk-hidden-value"),
        ("ordinary request", {"nested": {"password": "hunter2-secret"}},
         "hunter2-secret"),
        ("ordinary request", {"nested": [{"private-key": "private-key-value"}]},
         "private-key-value"),
        ("ordinary request", {"nested": {"SECRET": "opaque-secret-value"}},
         "opaque-secret-value"),
        ("ordinary request", {"nested": [{"Cookie": "session=cookie-value"}]},
         "cookie-value"),
        ("ordinary request", {"note": (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
        )}, "signature123"),
    ),
)
def test_queue_secret_shapes_are_rejected_before_persistence(
    monkeypatch, text, classifier_hint, secret_fragment,
):
    request_id = str(uuid.uuid4())

    class Tenant(str):
        subject = "auth0|alice"
        tier = "hosted_pro"

    tenant = Tenant("tenant-a")
    persisted = []
    monkeypatch.setattr(request_journal, "enabled", lambda: True)
    monkeypatch.setattr(
        request_journal, "admit_request",
        lambda **kwargs: persisted.append(kwargs) or pytest.fail("secret persisted"),
    )
    monkeypatch.setattr(sessions, "_require_owned_session",
                        lambda *_a: {"drawing_id": "drawing-a"})
    monkeypatch.setattr(sessions.entitlements, "resolve_tier", lambda _t: "hosted_pro")
    monkeypatch.setattr(sessions.entitlements, "resolve_roles", lambda _t: ((), False))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for",
                        lambda *_a, **_kw: {"converse": True})
    monkeypatch.setattr(turn_runner, "start_turn",
                        lambda *_a, **_kw: pytest.fail("secret dispatched"))

    response = sessions.post_message(
        "session-a",
        sessions.MessageRequest(
            text=text, classifier_hint=classifier_hint, queue=True,
            request_id=request_id,
        ),
        object(), tenant,
    )

    assert response.status_code == 400
    assert persisted == []
    assert secret_fragment.encode() not in response.body
    assert b"queue payload contains credential material" in response.body


def test_ordinary_nested_classifier_data_remains_recoverable():
    payload = _recoverable(
        classifier_hint={
            "layout": {"mode": "solar", "confidence": 0.91},
            "routing": ["roof", "array"],
        },
    )

    assert request_journal.validate_recoverable_payload(payload) == payload


def test_legacy_mode_ignores_request_id_and_keeps_dispatch_response(monkeypatch):
    request_id = {"ignored": ["legacy", "shape"]}

    class Tenant(str):
        subject = "auth0|alice"
        tier = "hosted_pro"

    tenant = Tenant("tenant-a")
    calls = []
    monkeypatch.setattr(request_journal, "enabled", lambda: False)
    monkeypatch.setattr(sessions, "_require_owned_session",
                        lambda *_a: {"drawing_id": "drawing-a"})
    monkeypatch.setattr(sessions.entitlements, "resolve_tier", lambda _t: "hosted_pro")
    monkeypatch.setattr(sessions.entitlements, "resolve_roles", lambda _t: ((), False))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for",
                        lambda *_a, **_kw: {"converse": True})
    monkeypatch.setattr(
        turn_runner, "start_turn",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "turn-1",
    )

    with_id = sessions.post_message(
        "session-a", sessions.MessageRequest(text="hello", request_id=request_id),
        object(), tenant,
    )
    without_id = sessions.post_message(
        "session-a", sessions.MessageRequest(text="hello"), object(), tenant,
    )

    assert with_id.status_code == without_id.status_code == 202
    assert with_id.body == without_id.body
    assert len(calls) == 2
    assert calls[0][1]["request_id"] is None
    assert calls[1][1]["request_id"] is None
