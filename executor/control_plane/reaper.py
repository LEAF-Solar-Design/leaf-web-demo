"""Durable-state reconciliation for expired instant bindings.

Redis is deliberately absent from this module. A production store must expose
an atomic ``reclaim_instant_sessions(now, idle_before)`` transaction that marks
the returned sessions terminal, releases their claims, and writes an outbox
record before this worker asks an executor to release its assignment.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable

from .store import StoreUnavailable


class ReconciliationUnavailable(RuntimeError):
    """The durable store lacks the required atomic reclamation transaction."""


class ControlPlaneReaper:
    """Run one deterministic reclaim pass for a scheduled control-plane worker."""

    def __init__(self, control_plane, *, idle_timeout: timedelta | None = None,
                 reclaim: Callable[[datetime, datetime | None], Iterable] | None = None):
        self.control_plane = control_plane
        self.idle_timeout = idle_timeout
        self._reclaim = reclaim

    def sweep(self) -> list[str]:
        now = self.control_plane.clock()
        idle_before = now - self.idle_timeout if self.idle_timeout else None
        sessions = self._reclaimed_sessions(now, idle_before)
        released = []
        for session in sessions:
            slot = self.control_plane.store.get_slot(session.executor_id, session.slot_id)
            if slot is None:
                continue
            reason = self._reason(session, now, idle_before)
            self.control_plane.runtime.release(
                slot.endpoint,
                {"assignment_id": session.assignment_id, "reason": reason},
            )
            complete = getattr(self.control_plane.store, "complete_reclaim", None)
            if complete is not None:
                complete(session.session_id)
            released.append(session.session_id)
        return released

    def _reclaimed_sessions(self, now: datetime, idle_before: datetime | None) -> list:
        reclaim = self._reclaim or getattr(self.control_plane.store, "reclaim_instant_sessions", None)
        if reclaim is not None:
            try:
                return list(reclaim(now, idle_before))
            except StoreUnavailable:
                raise
            except Exception as exc:
                raise ReconciliationUnavailable("durable instant-session reclamation failed") from exc

        # The in-memory store is itself the authoritative state in hermetic
        # tests. Keep this compatibility path out of production adapters, where
        # a scan followed by a separate release would be racy.
        store = self.control_plane.store
        if not hasattr(store, "sessions"):
            raise ReconciliationUnavailable(
                "durable store must implement reclaim_instant_sessions"
            )
        released = []
        with store._lock:
            store._require()
            for session in list(store.sessions.values()):
                if session.state not in {"BINDING", "ACTIVE", "DRAINING"}:
                    continue
                if not self._eligible(session, now, idle_before):
                    continue
                released_session = store.release(session.session_id, self._reason(session, now, idle_before))
                if released_session is not None:
                    released.append(released_session)
        return released

    @staticmethod
    def _eligible(session, now: datetime, idle_before: datetime | None) -> bool:
        if session.expires_at is not None and session.expires_at <= now:
            return True
        activity = getattr(session, "last_activity_at", None)
        return bool(idle_before is not None and activity is not None and activity <= idle_before)

    @staticmethod
    def _reason(session, now: datetime, idle_before: datetime | None) -> str:
        if session.expires_at is not None and session.expires_at <= now:
            return "lease_expired"
        return "idle_binding"
