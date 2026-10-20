"""Exact-key terminal cleanup and deterministic insertion-race coverage."""
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

from test_integration_turn_grant_capture import (
    custody, dispatch, grants, session_store, turn_runner, Authority, Binding,
    ORG, PROJECT, USER, TOKEN, ENVELOPE,
)

_real_spawn_relay = turn_runner._spawn_relay


def test_terminal_before_insert_revokes_again_after_insert(custody, monkeypatch):
    h = custody
    entered, released = threading.Barrier(2), threading.Barrier(2)
    results = []
    def put(binding, envelope):
        entered.wait(timeout=5)
        released.wait(timeout=5)
        h.puts.append(binding)
        return "created"
    monkeypatch.setattr(h.store, "put_once", put)
    worker = threading.Thread(target=lambda: results.append(h.capture()))
    worker.start()
    try:
        entered.wait(timeout=5)
        grants.cleanup_turn("s", "t")
        assert len(h.revokes) == 1 and not h.puts
        assert grants._references[("s", "t")].terminal
    finally:
        released.wait(timeout=5)
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert results == [grants.CaptureOutcome("unavailable")]
    assert len(h.puts) == 1 and len(h.revokes) == 2
    assert all(a == h.puts[0].authority for a, _ in h.revokes)


def test_termination_during_seal_never_inserts(custody, monkeypatch):
    h = custody
    def seal(token, binding):
        grants.cleanup_turn("s", "t")
        return ENVELOPE
    monkeypatch.setattr(grants, "_adapters", lambda: (Authority, Binding, h.store, seal))
    assert h.capture().code == "unavailable"
    assert not h.puts and h.revokes


def test_old_terminal_cleanup_cannot_touch_superseding_turn(custody):
    h = custody
    assert h.capture().code == "captured"
    h.session["active_turn_id"] = "new"
    assert grants._capture(TOKEN, h.snapshot, h.tenant, "s", "new", 300).code == "captured"
    grants.cleanup_turn("s", "t")
    grants.cleanup_turn("s", "t")
    grants.cleanup_turn("s", "unknown")
    assert [a.turn_id for a, _ in h.revokes] == ["t"]
    assert not grants._references[("s", "new")].terminal
    grants.cleanup_turn("s", "new")
    assert [a.turn_id for a, _ in h.revokes] == ["t", "new"]


@pytest.mark.parametrize("change", ["cancel", "project", "binding", "membership", "deadline", "start"])
def test_post_insert_recheck_revokes_original_authority(custody, change):
    h = custody
    def mutate():
        if change == "cancel": h.session["active_turn_id"] = None
        if change == "project": h.session["project_id"] = USER
        if change == "binding": h.identity.binding_id = UUID(USER)
        if change == "membership": h.access = False
        if change == "deadline": h.now = 1121
        if change == "start": h.session["turn_started_at"] = 991
    h.after_put = mutate
    assert h.capture().code == "unavailable"
    assert len(h.puts) == len(h.revokes) == 1
    assert h.revokes[0][0] == h.puts[0].authority
    assert h.revokes[0][0].project_id == PROJECT


def test_registry_capacity_fixed_expiry_and_conflicting_authority(custody, monkeypatch):
    h = custody
    monkeypatch.setattr(grants, "_CAPACITY", 2)
    assert h.capture().code == "captured"
    b = h.puts[0]
    assert grants._reserve(replace(b, authority=replace(b.authority, project_id=USER)), h.now) is None
    h.session["active_turn_id"] = "second"
    assert grants._capture(TOKEN, h.snapshot, h.tenant, "s", "second", 300).code == "captured"
    h.session["active_turn_id"] = "third"
    assert grants._capture(TOKEN, h.snapshot, h.tenant, "s", "third", 300).code == "unavailable"
    assert len(grants._references) == len(h.seals) == 2
    h.now = 1121
    h.session["turn_started_at"] = 1120
    assert grants._capture(TOKEN, h.snapshot, h.tenant, "s", "third", 300).code == "captured"
    assert set(grants._references) == {("s", "third")}
    assert TOKEN not in repr(grants._references) and ENVELOPE.decode() not in repr(grants._references)


def test_cleanup_failure_is_nonthrowing_and_marks_terminal_first(custody, monkeypatch, capsys, caplog):
    assert custody.capture().code == "captured"
    def revoke(*a):
        assert grants._references[("s", "t")].terminal
        # The lock must be free while I/O runs.
        assert grants._lock.acquire(blocking=False)
        grants._lock.release()
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(custody.store, "revoke_turn", revoke)
    grants.cleanup_turn("s", "t")
    grants.cleanup_turn("s", "t")
    assert custody.capture().code == "unavailable"
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("mode", ["legacy", "postgres", "shadow", "dual_write", "dual_write_shadow"])
@pytest.mark.parametrize("fail", [False, True])
def test_final_public_end_turn_preserves_each_store_mode_and_errors(custody, monkeypatch, mode, fail):
    h = custody
    assert h.capture().code == "captured"
    marker = RuntimeError("original release error")
    calls = []
    monkeypatch.setattr(session_store, "_store_mode", lambda: mode)
    monkeypatch.setattr(session_store, "_legacy_turn_fence", lambda s: ("t", 990))
    monkeypatch.setattr(session_store, "_pg_turn_fence", lambda s: ("t", 990))
    monkeypatch.setattr(session_store, "_shadow_equal", lambda *a: None)
    monkeypatch.setattr(session_store, "_legacy_get_session", lambda s: {"updated_at": 1000})
    def release(kind):
        def run(*a, **kw):
            calls.append(kind)
            if fail: raise marker
            return True
        return run
    monkeypatch.setattr(session_store, "_legacy_end_turn", release("legacy"))
    monkeypatch.setattr(session_store, "_pg_end_turn", release("postgres"))
    if fail:
        with pytest.raises(RuntimeError) as exc:
            session_store.end_turn("s", "t")
        assert exc.value is marker
    else:
        assert session_store.end_turn("s", "t") is None
    assert calls[0] == ("postgres" if mode == "postgres" else "legacy")
    assert len(h.revokes) == 1 and h.revokes[0][0] == h.puts[0].authority


def test_end_turn_preserves_original_exception_if_cleanup_itself_fails(custody, monkeypatch):
    marker = ValueError("store failure")
    def release(*a): raise marker
    def cleanup(*a): raise RuntimeError(TOKEN)
    monkeypatch.setattr(session_store, "_end_turn_with_store_mode", release)
    monkeypatch.setattr(grants, "cleanup_turn", cleanup)
    with pytest.raises(ValueError) as exc:
        session_store.end_turn("s", "t")
    assert exc.value is marker


def test_cleanup_without_local_reference_does_no_adapter_io(custody, monkeypatch):
    monkeypatch.setattr(grants, "_adapters", lambda: pytest.fail("privileged lookup"))
    grants.cleanup_turn("s", "missing")


def test_synchronous_rejection_revokes_accepted_capture(dispatch, monkeypatch):
    h = dispatch
    response = SimpleNamespace(status_code=401, json=lambda: {}, close=lambda: None)
    monkeypatch.setattr(turn_runner.requests, "post", lambda *a, **kw: response)
    with pytest.raises(turn_runner.TurnRejected):
        h.start(integration_capture=h.callback)
    assert len(h.puts) == len(h.revokes) == 1


def test_orphan_cancellation_uses_central_cleanup(dispatch, monkeypatch):
    h = dispatch
    assert h.capture().code == "captured"
    monkeypatch.setattr(turn_runner, "_cancellers", {})
    assert turn_runner.request_cancel(h.tenant, "s", "t") == "cancelled"
    assert len(h.revokes) == 1 and h.revokes[0][0].turn_id == "t"


@pytest.mark.parametrize("event", ["turn_complete", "error"])
def test_relay_success_and_error_use_exact_cleanup(dispatch, monkeypatch, event):
    h = dispatch
    assert h.capture().code == "captured"
    targets = {}
    def thread(*, target, name, **kw):
        targets[name] = target
        return SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(turn_runner, "threading", SimpleNamespace(
        Lock=threading.Lock, Event=threading.Event, Thread=thread))
    monkeypatch.setattr(turn_runner.agent_ledger, "append", lambda *a: None)
    monkeypatch.setattr(turn_runner.telemetry_sink, "emit", lambda *a, **kw: None)
    line = json.dumps({"type": event, "data": {"stop_reason": "complete"}}).encode()
    response = SimpleNamespace(iter_lines=lambda **kw: iter([line]), close=lambda: None)
    _real_spawn_relay(ORG, "s", "t", response, 300)
    targets["turn-drain-t"]()
    assert len(h.revokes) == 1 and h.revokes[0][0].turn_id == "t"
