"""Assignment and lifecycle service. It has no user-code execution path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import uuid
import weakref
from urllib.parse import urlparse

from .coordination import CoordinationUnavailable
from .jws import LeaseSigner
from .models import Lease, Session
from .reaper import ControlPlaneReaper, ReconciliationUnavailable
from .store import ControlStore, NoCapacity, StaleFence, StoreUnavailable


class ControlPlaneError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


class _SessionAssignmentLock:
    def __init__(self) -> None:
        self.lock = threading.Lock()


class ControlPlane:
    def __init__(self, store: ControlStore, runtime, signer: LeaseSigner | None = None, coordination=None, clock=None, lease_lifetime: timedelta = timedelta(seconds=60)):
        self.store, self.runtime, self.signer, self.coordination = store, runtime, signer or LeaseSigner(), coordination
        self.clock, self.lease_lifetime = clock or (lambda: datetime.now(timezone.utc)), lease_lifetime
        self._assignment_locks = weakref.WeakValueDictionary()
        self._assignment_locks_guard = threading.Lock()

    @staticmethod
    def _require(request: dict) -> None:
        required = {"tenant_id", "session_id", "effective_catalog_digest", "artifact", "drawing_context"}
        missing = required - request.keys()
        artifact = request.get("artifact", {})
        drawing_context = request.get("drawing_context", {})
        artifact_required = {"source", "code_digest", "artifact_digest", "runtime", "entrypoint", "limits", "tool_id", "tool_version", "capability_id", "params_schema_digest", "catalog_commit"}
        if (missing or not isinstance(artifact, dict) or artifact_required - artifact.keys()
                or artifact.get("runtime") != "python-3.12"
                or not isinstance(drawing_context, dict)
                or set(("reference", "data")) - drawing_context.keys()):
            raise ControlPlaneError("INVALID_ASSIGNMENT", "session request does not meet the trusted instant-artifact contract", 400)

    def _lease(self, session: Session, slot, claim, now: datetime, sequence: int) -> tuple[Lease, str]:
        lease = Lease(str(uuid.uuid4()), session.session_id, slot.executor_id, slot.slot_id, claim.claim_id, slot.host_epoch, slot.slot_epoch, claim.claim_epoch, session.binding_epoch, sequence, now, now + self.lease_lifetime)
        payload = {"iss": "instant-control-plane", "aud": "instant-executor", "jti": lease.lease_id, "lease_id": lease.lease_id,
                   "executor_id": slot.executor_id, "host_id": slot.executor_id, "slot_id": slot.slot_id, "host_epoch": slot.host_epoch,
                   "slot_epoch": slot.slot_epoch, "claim_id": claim.claim_id, "claim_epoch": claim.claim_epoch, "tenant_id": session.tenant_id,
                   "session_id": session.session_id, "assignment_id": session.assignment_id, "binding_epoch": session.binding_epoch,
                   "lease_sequence": sequence, "catalog_version": session.catalog_digest, "effective_catalog_digest": session.catalog_digest,
                   "code_digest": session.code_digest, "artifact_digest": session.artifact_digest, "capability": session.capability,
                   "nbf": int(now.timestamp()), "exp": int(lease.expires_at.timestamp())}
        return lease, self.signer.sign(payload)

    def assign(self, request: dict, binding_epoch: int = 1) -> dict:
        """Create or reject a binding without double-claiming one session.

        PostgreSQL remains the cross-worker fence. This process-local
        single-flight closes the gap between the durable read and session
        create for two simultaneous app requests in one control-plane worker.
        """
        session_id = str(request.get("session_id", ""))
        with self._assignment_locks_guard:
            holder = self._assignment_locks.get(session_id)
            if holder is None:
                holder = _SessionAssignmentLock()
                self._assignment_locks[session_id] = holder
        with holder.lock:
            return self._assign(request, binding_epoch)

    def _assign(self, request: dict, binding_epoch: int = 1) -> dict:
        self._require(request)
        if self.coordination is None:
            raise ControlPlaneError("REDIS_UNAVAILABLE", "Redis coordination is required for new assignments")
        now = self.clock()
        try:
            existing = self.store.get_session(request["session_id"])
        except StoreUnavailable as exc:
            raise ControlPlaneError("POSTGRES_UNAVAILABLE", "new assignment is fail closed while PostgreSQL is unavailable") from exc
        if existing and existing.state in {"BINDING", "ACTIVE", "DRAINING"}:
            if existing.expires_at is None or existing.expires_at > now:
                raise ControlPlaneError("SESSION_ALREADY_BOUND", "session already has a live instant binding", 409)
            self.release(existing.session_id, "lease_expired")
        lock = None
        candidate = None
        try:
            # Redis only narrows contention. This durable versioned CAS decides ownership.
            self.coordination.check_available()
            candidate = self.store.candidate()
            lock = self.coordination.acquire_claim_lock(candidate.executor_id, candidate.slot_id)
            slot, claim = self.store.claim_slot(candidate.executor_id, candidate.slot_id, candidate.version, now, self.lease_lifetime)
        except CoordinationUnavailable as exc:
            if lock is not None:
                self.coordination.release_claim_lock(candidate.executor_id, candidate.slot_id, lock)
            raise ControlPlaneError("REDIS_UNAVAILABLE", "new assignment is fail closed while Redis is unavailable") from exc
        except StoreUnavailable as exc:
            if lock is not None:
                self.coordination.release_claim_lock(candidate.executor_id, candidate.slot_id, lock)
            raise ControlPlaneError("POSTGRES_UNAVAILABLE", "new assignment is fail closed while PostgreSQL is unavailable") from exc
        except NoCapacity as exc:
            if lock is not None:
                self.coordination.release_claim_lock(candidate.executor_id, candidate.slot_id, lock)
            raise ControlPlaneError("NO_CAPACITY", str(exc), 429) from exc
        except StaleFence as exc:
            if lock is not None:
                self.coordination.release_claim_lock(candidate.executor_id, candidate.slot_id, lock)
            raise ControlPlaneError("NO_CAPACITY", "capacity claim lost a concurrent race", 429) from exc
        session_created = False
        try:
            artifact = request["artifact"]
            capability = {"capability_id": artifact["capability_id"], "tool_id": artifact["tool_id"], "tool_version": artifact["tool_version"]}
            session = Session(str(uuid.uuid4()), request["tenant_id"], request["session_id"], slot.executor_id, slot.slot_id, claim.claim_id,
                              binding_epoch, request["effective_catalog_digest"], artifact["code_digest"], artifact["artifact_digest"], slot.runtime_digest or "", capability)
            self.store.create_session(session)
            session_created = True
            lease, token = self._lease(session, slot, claim, now, 1)
            assignment = {"contract": "leaf.instant-execution/v1", "assignment_id": session.assignment_id, "tenant_id": session.tenant_id,
                "session_id": session.session_id, "executor_id": slot.executor_id, "executor_endpoint": slot.endpoint,
                "binding_epoch": session.binding_epoch, "lease_id": lease.lease_id, "lease_token": token,
                "execution_class": "instant", "effective_catalog_digest": session.catalog_digest, "code_digest": session.code_digest,
                "artifact_digest": session.artifact_digest, "drawing_context": request["drawing_context"]["reference"],
                "issued_at": now.isoformat().replace("+00:00", "Z"), "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z")}
            code_load = {"contract": "leaf.instant-execution/v1", "load_id": str(uuid.uuid4()), "tenant_id": session.tenant_id,
                "session_id": session.session_id, "effective_catalog_digest": session.catalog_digest, "code_digest": session.code_digest,
                "artifact_digest": session.artifact_digest, "runtime": artifact["runtime"], "entrypoint": artifact["entrypoint"]}
            catalog = {"contract": "leaf.instant-execution/v1", "catalog_commit": artifact["catalog_commit"], "capability": capability,
                "execution_class": "instant", "runtime": artifact["runtime"], "limits": artifact["limits"],
                "code_digest": session.code_digest, "artifact_digest": session.artifact_digest, "entrypoint": artifact["entrypoint"],
                "params_schema_digest": artifact["params_schema_digest"]}
            ready = self.runtime.assign(slot.endpoint, {"assignment": assignment, "code_load": code_load, "catalog": catalog,
                "source": artifact["source"], "drawing_context": request["drawing_context"]["data"]})
            if ready.get("state") != "ready":
                raise ControlPlaneError("RUNTIME_NOT_READY", "executor did not confirm code readiness")
            self.store.activate(session.session_id, lease)
            try:
                self.coordination.publish_hint("session.active", session.session_id, session.binding_epoch)
            except CoordinationUnavailable:
                pass  # durable activation succeeded; Redis notification is recoverable
            return assignment
        except ControlPlaneError:
            if session_created:
                self.release(request["session_id"], "runtime_not_ready")
            else:
                self.store.abort_claim(claim.claim_id)
                self.runtime.release(slot.endpoint, {"assignment_id": str(uuid.uuid4()), "reason": "runtime_not_ready"})
            raise
        except Exception as exc:
            if session_created:
                self.release(request["session_id"], "assignment_failed")
            else:
                self.store.abort_claim(claim.claim_id)
                self.runtime.release(slot.endpoint, {"assignment_id": str(uuid.uuid4()), "reason": "assignment_failed"})
            raise ControlPlaneError("ASSIGNMENT_FAILED", "executor assignment failed") from exc
        finally:
            try:
                self.coordination.release_claim_lock(slot.executor_id, slot.slot_id, lock)
            except CoordinationUnavailable:
                pass  # lock TTL and durable claim fencing prevent double ownership

    def renew(self, session_id: str, binding_epoch: int) -> dict:
        try:
            session = self.store.get_session(session_id)
            if not session or not session.expires_at or session.expires_at <= self.clock():
                raise ControlPlaneError("LEASE_EXPIRED", "session lease has expired", 409)
            slot = self.store.get_slot(session.executor_id, session.slot_id)
            claim = self.store.get_claim(session.claim_id)
            if claim is None or claim.state != "ACTIVE":
                raise ControlPlaneError("STALE_FENCE", "session capacity claim is not active", 409)
            lease, token = self._lease(session, slot, claim, self.clock(), session.lease_sequence + 1)
            self.store.renew(session_id, binding_epoch, lease)
            return {"lease_id": lease.lease_id, "lease_token": token, "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"), "lease_sequence": lease.sequence}
        except StoreUnavailable as exc:
            raise ControlPlaneError("POSTGRES_UNAVAILABLE", "renewal is fail closed while PostgreSQL is unavailable") from exc
        except StaleFence as exc:
            raise ControlPlaneError("STALE_FENCE", str(exc), 409) from exc

    def release(self, session_id: str, reason: str = "released") -> None:
        session = self.store.release(session_id, reason)
        if session:
            slot = self.store.get_slot(session.executor_id, session.slot_id)
            self.runtime.release(slot.endpoint, {"assignment_id": session.assignment_id, "reason": reason})
            complete = getattr(self.store, "complete_reclaim", None)
            if complete is not None:
                complete(session.session_id)

    def invalidate(self, session_id: str, reason: str, binding_epoch: int | None = None) -> None:
        try:
            session = self.store.invalidate(session_id, reason, binding_epoch)
            if session:
                slot = self.store.get_slot(session.executor_id, session.slot_id)
                self.runtime.release(slot.endpoint, {"assignment_id": session.assignment_id, "reason": reason})
                complete = getattr(self.store, "complete_reclaim", None)
                if complete is not None:
                    complete(session.session_id)
        except StaleFence as exc:
            raise ControlPlaneError("STALE_FENCE", str(exc), 409) from exc

    def drain(self, executor_id: str, deadline: datetime) -> list[str]:
        return [session.session_id for session in self.store.drain(executor_id, deadline)]

    def rebind(self, request: dict) -> dict:
        old = self.store.get_session(request["session_id"])
        next_epoch = (old.binding_epoch + 1) if old else 1
        if old:
            self.invalidate(old.session_id, "code_change", old.binding_epoch)
        return self.assign(request, next_epoch)

    def register_host(self, executor_id: str, endpoint: str, slot_ids: list[str]) -> list[dict]:
        parsed = urlparse(endpoint)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme not in ({"http", "https"} if loopback else {"https"}) or not parsed.hostname or parsed.username or parsed.password:
            raise ControlPlaneError("INVALID_EXECUTOR_ENDPOINT", "executor endpoint must be HTTPS, or loopback HTTP for local development", 400)
        return [slot.__dict__ for slot in self.store.register_host(executor_id, endpoint, slot_ids)]

    def readiness(self, executor_id: str, slot_id: str, ready: bool, code_digest: str, runtime_digest: str) -> dict:
        return self.store.set_readiness(executor_id, slot_id, ready, code_digest, runtime_digest).__dict__

    def heartbeat(self, executor_id: str) -> None:
        self.store.heartbeat(executor_id)

    def reconcile(self, *, idle_timeout: timedelta | None = None, reclaim=None) -> list[str]:
        """Reclaim expired or idle bindings from durable state for a scheduler.

        This deliberately has no HTTP route. Production composition supplies
        the store's atomic reclamation transaction to the scheduled worker.
        """
        try:
            return ControlPlaneReaper(self, idle_timeout=idle_timeout, reclaim=reclaim).sweep()
        except StoreUnavailable as exc:
            raise ControlPlaneError(
                "POSTGRES_UNAVAILABLE",
                "reconciliation is fail closed while PostgreSQL is unavailable",
            ) from exc
        except ReconciliationUnavailable as exc:
            raise ControlPlaneError("RECONCILIATION_UNAVAILABLE", str(exc)) from exc
