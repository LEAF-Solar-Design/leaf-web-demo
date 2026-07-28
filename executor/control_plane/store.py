"""Durable authority interface and memory/PostgreSQL implementations."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import threading
import uuid
from typing import Protocol

from .models import Claim, Lease, Session, Slot


class StoreUnavailable(RuntimeError):
    pass


class NoCapacity(RuntimeError):
    pass


class StaleFence(RuntimeError):
    pass


class ControlStore(Protocol):
    def register_host(self, executor_id: str, endpoint: str, slot_ids: list[str]) -> list[Slot]: ...
    def set_readiness(self, executor_id: str, slot_id: str, ready: bool, code_digest: str, runtime_digest: str) -> Slot: ...
    def heartbeat(self, executor_id: str) -> None: ...
    def candidate(self) -> Slot: ...
    def claim_slot(self, executor_id: str, slot_id: str, expected_version: int, now: datetime, lifetime: timedelta) -> tuple[Slot, Claim]: ...
    def get_claim(self, claim_id: str) -> Claim | None: ...
    def abort_claim(self, claim_id: str) -> None: ...
    def create_session(self, session: Session) -> None: ...
    def activate(self, session_id: str, lease: Lease) -> None: ...
    def get_session(self, session_id: str) -> Session | None: ...
    def get_slot(self, executor_id: str, slot_id: str) -> Slot | None: ...
    def renew(self, session_id: str, expected_epoch: int, lease: Lease) -> None: ...
    def release(self, session_id: str, reason: str) -> Session | None: ...
    def invalidate(self, session_id: str, reason: str, expected_epoch: int | None = None) -> Session | None: ...
    def drain(self, executor_id: str, deadline: datetime) -> list[Session]: ...


class InMemoryStore:
    """Deterministic store. The lock makes capacity claims single-winner."""
    def __init__(self) -> None:
        self.available = True
        self._lock = threading.RLock()
        self.slots: dict[tuple[str, str], Slot] = {}
        self.sessions: dict[str, Session] = {}
        self.claims: dict[str, Claim] = {}
        self._host_epochs: dict[str, int] = {}

    def _require(self) -> None:
        if not self.available:
            raise StoreUnavailable("PostgreSQL durable authority is unavailable")

    def register_host(self, executor_id: str, endpoint: str, slot_ids: list[str]) -> list[Slot]:
        with self._lock:
            self._require()
            epoch = self._host_epochs.get(executor_id, 0) + 1
            self._host_epochs[executor_id] = epoch
            result = []
            for slot_id in slot_ids:
                slot = Slot(executor_id, slot_id, endpoint, host_epoch=epoch)
                self.slots[(executor_id, slot_id)] = slot
                result.append(slot)
            return result

    def set_readiness(self, executor_id: str, slot_id: str, ready: bool, code_digest: str, runtime_digest: str) -> Slot:
        with self._lock:
            self._require()
            slot = self.slots[(executor_id, slot_id)]
            slot.state = "READY" if ready else "STARTING"
            slot.code_digest, slot.runtime_digest = code_digest, runtime_digest
            slot.version += 1
            return slot

    def heartbeat(self, executor_id: str) -> None:
        with self._lock:
            self._require()
            if executor_id not in self._host_epochs:
                raise KeyError(executor_id)

    def candidate(self) -> Slot:
        with self._lock:
            self._require()
            for key in sorted(self.slots):
                slot = self.slots[key]
                if slot.state == "READY":
                    return Slot(**slot.__dict__)
            raise NoCapacity("no ready executor slot")

    def claim_slot(self, executor_id: str, slot_id: str, expected_version: int, now: datetime, lifetime: timedelta) -> tuple[Slot, Claim]:
        with self._lock:
            self._require()
            slot = self.slots[(executor_id, slot_id)]
            if slot.state != "READY" or slot.version != expected_version:
                raise StaleFence("capacity claim lost its compare-and-set race")
            slot.state = "CLAIMED"
            slot.version += 1
            claim = Claim(str(uuid.uuid4()), slot.executor_id, slot.slot_id, slot.version, now + lifetime)
            self.claims[claim.claim_id] = claim
            return slot, claim

    def get_claim(self, claim_id: str) -> Claim | None:
        with self._lock:
            self._require()
            return self.claims.get(claim_id)

    def abort_claim(self, claim_id: str) -> None:
        with self._lock:
            self._require()
            claim = self.claims.get(claim_id)
            if not claim or claim.state != "ACTIVE":
                return
            claim.state = "RELEASED"
            slot = self.slots[(claim.executor_id, claim.slot_id)]
            if slot.state == "CLAIMED":
                slot.state, slot.version = "READY", slot.version + 1

    def create_session(self, session: Session) -> None:
        with self._lock:
            self._require()
            if session.session_id in self.sessions and self.sessions[session.session_id].state in {"BINDING", "ACTIVE", "DRAINING"}:
                raise StaleFence("session already has a live binding")
            self.sessions[session.session_id] = session

    def activate(self, session_id: str, lease: Lease) -> None:
        with self._lock:
            self._require()
            session = self.sessions[session_id]
            if session.state != "BINDING" or lease.binding_epoch != session.binding_epoch:
                raise StaleFence("stale binding activation")
            session.lease_id, session.lease_sequence = lease.lease_id, lease.sequence
            session.expires_at, session.state = lease.expires_at, "ACTIVE"
            session.last_activity_at = lease.not_before

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            self._require()
            return self.sessions.get(session_id)

    def get_slot(self, executor_id: str, slot_id: str) -> Slot | None:
        with self._lock:
            self._require()
            return self.slots.get((executor_id, slot_id))

    def renew(self, session_id: str, expected_epoch: int, lease: Lease) -> None:
        with self._lock:
            self._require()
            session = self.sessions[session_id]
            if session.state != "ACTIVE" or session.binding_epoch != expected_epoch or lease.sequence <= session.lease_sequence:
                raise StaleFence("stale lease renewal")
            session.lease_id, session.lease_sequence, session.expires_at = lease.lease_id, lease.sequence, lease.expires_at
            session.last_activity_at = lease.not_before

    def release(self, session_id: str, reason: str) -> Session | None:
        with self._lock:
            self._require()
            session = self.sessions.get(session_id)
            if not session:
                return None
            session.state = "INVALID"
            claim = self.claims.get(session.claim_id)
            if claim:
                claim.state = "RELEASED"
            slot = self.slots[(session.executor_id, session.slot_id)]
            if slot.state == "CLAIMED":
                slot.state, slot.version = "READY", slot.version + 1
            return session

    def invalidate(self, session_id: str, reason: str, expected_epoch: int | None = None) -> Session | None:
        with self._lock:
            self._require()
            session = self.sessions.get(session_id)
            if not session:
                return None
            if expected_epoch is not None and expected_epoch != session.binding_epoch:
                raise StaleFence("stale binding fence")
            return self.release(session_id, reason)

    def drain(self, executor_id: str, deadline: datetime) -> list[Session]:
        with self._lock:
            self._require()
            affected = []
            for slot in self.slots.values():
                if slot.executor_id == executor_id:
                    slot.state = "DRAINING"
            for session in self.sessions.values():
                if session.executor_id == executor_id and session.state == "ACTIVE":
                    session.state = "DRAINING"
                    affected.append(session)
            return affected


class PostgresStore:
    """PostgreSQL durable-authority adapter using DB-API connections.

    The caller supplies a connection factory for psycopg or psycopg2. Every
    mutation runs in one database transaction. Capacity ownership is decided
    by a single compare-and-set statement in PostgreSQL.
    """
    def __init__(self, connection_factory):
        self._connect = connection_factory

    @staticmethod
    def _unavailable(exc: Exception) -> bool:
        """Return true only for errors that establish a lost DB connection."""
        current: BaseException | None = exc
        while current is not None:
            sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
            if isinstance(sqlstate, str) and sqlstate.startswith("08"):
                return True
            if type(current).__name__ in {"OperationalError", "InterfaceError"}:
                return True
            current = current.__cause__ or current.__context__
        return False

    def _transaction(self, operation):
        conn = None
        try:
            conn = self._connect()
            with conn:
                with conn.cursor() as cur:
                    return operation(cur)
        except Exception as exc:
            if self._unavailable(exc):
                raise StoreUnavailable("PostgreSQL durable authority is unavailable") from exc
            raise
        finally:
            if conn is not None:
                close = getattr(conn, "close", None)
                if close is not None:
                    close()

    @staticmethod
    def _slot(row) -> Slot:
        return Slot(str(row[0]), str(row[1]), row[2], int(row[3]), int(row[4]), row[5], row[6], row[7], int(row[8]))

    @staticmethod
    def _claim(row) -> Claim:
        return Claim(str(row[0]), str(row[1]), str(row[2]), int(row[3]), row[4], row[5])

    @staticmethod
    def _session(row) -> Session:
        capability = row[10]
        if isinstance(capability, str):
            capability = json.loads(capability)
        return Session(str(row[0]), row[1], str(row[2]), str(row[3]), str(row[4]), str(row[5]), int(row[6]), row[7], row[8], row[9], row[11], capability, row[12], str(row[13]) if row[13] is not None else None, int(row[14]), row[15], row[16])

    @staticmethod
    def _session_sql() -> str:
        return """assignment_id, tenant_id, session_id, host_id, slot_id, claim_id,
                  binding_epoch, catalog_digest, code_digest, artifact_digest,
                  capability, runtime_digest, state, lease_id, lease_sequence,
                  expires_at, last_activity_at"""

    def _read_session(self, cur, session_id: str, *, lock: bool = False) -> Session | None:
        cur.execute(
            f"SELECT {self._session_sql()} FROM instant_sessions WHERE session_id=%s" + (" FOR UPDATE" if lock else ""),
            (session_id,),
        )
        row = cur.fetchone()
        return self._session(row) if row else None

    def register_host(self, executor_id, endpoint, slot_ids):
        def operation(cur):
            cur.execute(
                """INSERT INTO executor_hosts
                       (host_id, state, host_epoch, public_key_fingerprint, capacity_total,
                        capacity_ready, endpoint, version)
                   VALUES (%s, 'ACTIVE', 1, '', %s, 0, %s, 1)
                   ON CONFLICT (host_id) DO UPDATE SET
                     state='ACTIVE', host_epoch=executor_hosts.host_epoch + 1,
                     capacity_total=EXCLUDED.capacity_total, capacity_ready=0,
                     endpoint=EXCLUDED.endpoint, drain_deadline_at=NULL,
                     revoked_at=NULL, version=executor_hosts.version + 1
                   RETURNING host_epoch""",
                (executor_id, len(slot_ids), endpoint),
            )
            host_epoch = cur.fetchone()[0]
            # A new host epoch fences all old work before a slot may be ready again.
            cur.execute(
                """UPDATE instant_sessions SET state='INVALID', invalidated_at=now(),
                       reason='host_reregistered', version=version+1
                   WHERE host_id=%s AND state IN ('BINDING', 'ACTIVE', 'DRAINING')""",
                (executor_id,),
            )
            cur.execute(
                """UPDATE capacity_claims SET state='RELEASED', released_at=now(), version=version+1
                   WHERE host_id=%s AND state='ACTIVE'""",
                (executor_id,),
            )
            cur.execute(
                """UPDATE executor_slots SET state='REVOKED', slot_epoch=slot_epoch+1,
                       code_digest=NULL, runtime_digest=NULL, current_claim_id=NULL,
                       last_ready_at=NULL, version=version+1
                   WHERE host_id=%s""",
                (executor_id,),
            )
            slots = []
            for slot_id in slot_ids:
                cur.execute(
                    """INSERT INTO executor_slots
                           (host_id, slot_id, state, slot_epoch, code_digest, runtime_digest,
                            current_claim_id, version)
                       VALUES (%s, %s, 'STARTING', 1, NULL, NULL, NULL, 1)
                       ON CONFLICT (host_id, slot_id) DO UPDATE SET
                         state='STARTING'
                       RETURNING host_id, slot_id, %s, %s, slot_epoch, state,
                                 code_digest, runtime_digest, version""",
                    (executor_id, slot_id, endpoint, host_epoch),
                )
                row = cur.fetchone()
                slots.append(Slot(str(row[0]), str(row[1]), row[2], int(row[3]), int(row[4]), row[5], row[6], row[7], int(row[8])))
            return slots
        return self._transaction(operation)

    def set_readiness(self, executor_id, slot_id, ready, code_digest, runtime_digest):
        def operation(cur):
            cur.execute(
                """UPDATE executor_slots s SET state=%s, code_digest=%s, runtime_digest=%s,
                       last_ready_at=CASE WHEN %s THEN now() ELSE NULL END, version=s.version+1
                   FROM executor_hosts h
                   WHERE s.host_id=%s AND s.slot_id=%s AND h.host_id=s.host_id
                     AND h.state='ACTIVE'
                   RETURNING s.host_id, s.slot_id, h.endpoint, h.host_epoch, s.slot_epoch,
                             s.state, s.code_digest, s.runtime_digest, s.version""",
                ("READY" if ready else "STARTING", code_digest, runtime_digest, ready, executor_id, slot_id),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError((executor_id, slot_id))
            cur.execute(
                """UPDATE executor_hosts SET capacity_ready=(
                       SELECT count(*) FROM executor_slots WHERE host_id=%s AND state='READY'),
                       version=version+1 WHERE host_id=%s""",
                (executor_id, executor_id),
            )
            return self._slot(row)
        return self._transaction(operation)

    def heartbeat(self, executor_id):
        def operation(cur):
            cur.execute("UPDATE executor_hosts SET last_heartbeat_at=now(), version=version+1 WHERE host_id=%s", (executor_id,))
            if cur.rowcount != 1:
                raise KeyError(executor_id)
        self._transaction(operation)

    def candidate(self):
        def operation(cur):
            cur.execute(
                """SELECT s.host_id, s.slot_id, h.endpoint, h.host_epoch, s.slot_epoch,
                          s.state, s.code_digest, s.runtime_digest, s.version
                   FROM executor_slots s JOIN executor_hosts h ON h.host_id=s.host_id
                   WHERE s.state='READY' AND h.state='ACTIVE'
                   ORDER BY s.host_id, s.slot_id LIMIT 1"""
            )
            row = cur.fetchone()
            if row is None:
                raise NoCapacity("no ready executor slot")
            return self._slot(row)
        return self._transaction(operation)

    def claim_slot(self, executor_id, slot_id, expected_version, now, lifetime):
        claim_id = str(uuid.uuid4())
        expires_at = now + lifetime
        def operation(cur):
            cur.execute(
                """WITH claimed AS (
                     UPDATE executor_slots s SET state='CLAIMED', current_claim_id=%s, version=s.version+1
                     WHERE s.host_id=%s AND s.slot_id=%s AND s.state='READY' AND s.version=%s
                       AND EXISTS (SELECT 1 FROM executor_hosts h WHERE h.host_id=s.host_id AND h.state='ACTIVE')
                     RETURNING s.host_id, s.slot_id, s.slot_epoch, s.code_digest, s.runtime_digest, s.version
                   ), created AS (
                     INSERT INTO capacity_claims
                       (claim_id, host_id, slot_id, owner_id, claim_epoch, state, expires_at, version)
                     SELECT %s, host_id, slot_id, %s, version, 'ACTIVE', %s, 1 FROM claimed
                     RETURNING claim_id, host_id, slot_id, claim_epoch, expires_at, state
                   )
                   SELECT c.host_id, c.slot_id, h.endpoint, h.host_epoch, c.slot_epoch, 'CLAIMED',
                          c.code_digest, c.runtime_digest, c.version,
                          x.claim_id, x.host_id, x.slot_id, x.claim_epoch, x.expires_at, x.state
                   FROM claimed c JOIN executor_hosts h ON h.host_id=c.host_id CROSS JOIN created x""",
                (claim_id, executor_id, slot_id, expected_version, claim_id, executor_id, expires_at),
            )
            row = cur.fetchone()
            if row is None:
                raise StaleFence("capacity claim lost its compare-and-set race")
            return self._slot(row[:9]), self._claim(row[9:])
        return self._transaction(operation)

    def get_claim(self, claim_id):
        def operation(cur):
            cur.execute("SELECT claim_id, host_id, slot_id, claim_epoch, expires_at, state FROM capacity_claims WHERE claim_id=%s", (claim_id,))
            row = cur.fetchone()
            return self._claim(row) if row else None
        return self._transaction(operation)

    def abort_claim(self, claim_id):
        def operation(cur):
            cur.execute(
                """UPDATE capacity_claims SET state='RELEASED', released_at=now(), version=version+1
                   WHERE claim_id=%s AND state='ACTIVE' RETURNING host_id, slot_id""",
                (claim_id,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE executor_slots SET state='READY', current_claim_id=NULL, version=version+1
                       WHERE host_id=%s AND slot_id=%s AND current_claim_id=%s AND state='CLAIMED'""",
                    (row[0], row[1], claim_id),
                )
        self._transaction(operation)

    def create_session(self, session):
        def operation(cur):
            cur.execute(
                """WITH valid_claim AS (
                     SELECT c.claim_id FROM capacity_claims c JOIN executor_slots s
                       ON s.host_id=c.host_id AND s.slot_id=c.slot_id
                     WHERE c.claim_id=%s AND c.host_id=%s AND c.slot_id=%s
                       AND c.state='ACTIVE' AND c.expires_at > now()
                       AND s.state='CLAIMED' AND s.current_claim_id=c.claim_id
                   )
                   INSERT INTO instant_sessions
                     (assignment_id, tenant_id, session_id, host_id, slot_id, claim_id, binding_epoch,
                      catalog_digest, code_digest, artifact_digest, runtime_digest, capability, state,
                      lease_sequence, version)
                   SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'BINDING', 0, 1
                   FROM valid_claim
                   ON CONFLICT (session_id) DO UPDATE SET
                     assignment_id=EXCLUDED.assignment_id, tenant_id=EXCLUDED.tenant_id,
                     host_id=EXCLUDED.host_id, slot_id=EXCLUDED.slot_id, claim_id=EXCLUDED.claim_id,
                     binding_epoch=EXCLUDED.binding_epoch, catalog_digest=EXCLUDED.catalog_digest,
                     code_digest=EXCLUDED.code_digest, artifact_digest=EXCLUDED.artifact_digest,
                     runtime_digest=EXCLUDED.runtime_digest, capability=EXCLUDED.capability,
                     state='BINDING', lease_id=NULL, lease_sequence=0, expires_at=NULL,
                     invalidated_at=NULL, reason=NULL, version=instant_sessions.version+1
                   WHERE instant_sessions.state NOT IN ('BINDING', 'ACTIVE', 'DRAINING')
                   RETURNING session_id""",
                (session.claim_id, session.executor_id, session.slot_id, session.assignment_id, session.tenant_id,
                 session.session_id, session.executor_id, session.slot_id, session.claim_id, session.binding_epoch,
                 session.catalog_digest, session.code_digest, session.artifact_digest, session.runtime_digest,
                 json.dumps(session.capability, sort_keys=True, separators=(",", ":"))),
            )
            if cur.fetchone() is None:
                raise StaleFence("session already has a live binding or its capacity claim is stale")
        self._transaction(operation)

    def activate(self, session_id, lease):
        def operation(cur):
            cur.execute(
                """WITH activated AS (
                     UPDATE instant_sessions s SET lease_id=%s, lease_sequence=%s, expires_at=%s,
                       last_activity_at=%s, state='ACTIVE', version=s.version+1
                     WHERE s.session_id=%s AND s.state='BINDING' AND s.binding_epoch=%s
                       AND EXISTS (
                         SELECT 1 FROM capacity_claims c
                         JOIN executor_slots slot ON slot.host_id=c.host_id AND slot.slot_id=c.slot_id
                         JOIN executor_hosts host ON host.host_id=c.host_id
                         WHERE c.claim_id=s.claim_id AND c.state='ACTIVE' AND c.expires_at >= %s
                           AND slot.state='CLAIMED' AND host.state='ACTIVE'
                       )
                     RETURNING s.session_id
                   )
                   INSERT INTO executor_leases
                     (lease_id, host_id, slot_id, host_epoch, slot_epoch, claim_id, session_id,
                      binding_epoch, lease_sequence, not_before, expires_at, state, version)
                   SELECT %s, %s, %s, %s, %s, %s, session_id, %s, %s, %s, %s, 'ACTIVE', 1
                   FROM activated RETURNING session_id""",
                (lease.lease_id, lease.sequence, lease.expires_at, lease.not_before, session_id, lease.binding_epoch,
                 lease.not_before, lease.lease_id, lease.executor_id, lease.slot_id, lease.host_epoch,
                 lease.slot_epoch, lease.claim_id, lease.binding_epoch, lease.sequence,
                 lease.not_before, lease.expires_at),
            )
            if cur.fetchone() is None:
                raise StaleFence("stale binding activation")
        self._transaction(operation)

    def get_session(self, session_id):
        return self._transaction(lambda cur: self._read_session(cur, session_id))

    def get_slot(self, executor_id, slot_id):
        def operation(cur):
            cur.execute(
                """SELECT s.host_id, s.slot_id, h.endpoint, h.host_epoch, s.slot_epoch,
                          s.state, s.code_digest, s.runtime_digest, s.version
                   FROM executor_slots s JOIN executor_hosts h ON h.host_id=s.host_id
                   WHERE s.host_id=%s AND s.slot_id=%s""",
                (executor_id, slot_id),
            )
            row = cur.fetchone()
            return self._slot(row) if row else None
        return self._transaction(operation)

    def renew(self, session_id, expected_epoch, lease):
        def operation(cur):
            cur.execute(
                """UPDATE executor_leases SET state='SUPERSEDED', revoked_at=%s, version=version+1
                   WHERE session_id=%s AND state='ACTIVE'""",
                (lease.not_before, session_id),
            )
            cur.execute(
                """WITH renewed AS (
                     UPDATE instant_sessions s SET lease_id=%s, lease_sequence=%s, expires_at=%s,
                       last_activity_at=%s, version=s.version+1
                     WHERE s.session_id=%s AND s.state='ACTIVE' AND s.binding_epoch=%s
                       AND s.lease_sequence < %s
                       AND EXISTS (SELECT 1 FROM capacity_claims c WHERE c.claim_id=s.claim_id
                                   AND c.state='ACTIVE' AND c.expires_at >= %s)
                     RETURNING s.session_id
                   )
                   INSERT INTO executor_leases
                     (lease_id, host_id, slot_id, host_epoch, slot_epoch, claim_id, session_id,
                      binding_epoch, lease_sequence, not_before, expires_at, state, version)
                   SELECT %s, %s, %s, %s, %s, %s, session_id, %s, %s, %s, %s, 'ACTIVE', 1
                   FROM renewed RETURNING session_id""",
                (lease.lease_id, lease.sequence, lease.expires_at, lease.not_before, session_id, expected_epoch,
                 lease.sequence, lease.not_before, lease.lease_id, lease.executor_id, lease.slot_id,
                 lease.host_epoch, lease.slot_epoch, lease.claim_id, lease.binding_epoch,
                 lease.sequence, lease.not_before, lease.expires_at),
            )
            if cur.fetchone() is None:
                raise StaleFence("stale lease renewal")
        self._transaction(operation)

    def _finish_session(self, cur, session_id: str, reason: str, expected_epoch: int | None = None) -> Session | None:
        session = self._read_session(cur, session_id, lock=True)
        if session is None:
            return None
        if expected_epoch is not None and session.binding_epoch != expected_epoch:
            raise StaleFence("stale binding fence")
        cur.execute(
            """UPDATE instant_sessions SET state='INVALID', invalidated_at=now(), reason=%s,
                   version=version+1 WHERE session_id=%s""",
            (reason, session_id),
        )
        cur.execute(
            """UPDATE executor_leases SET state='REVOKED', revoked_at=now(), version=version+1
               WHERE session_id=%s AND state='ACTIVE'""",
            (session_id,),
        )
        cur.execute(
            """UPDATE capacity_claims SET state='RELEASED', released_at=now(), version=version+1
               WHERE claim_id=%s AND state='ACTIVE'""",
            (session.claim_id,),
        )
        cur.execute(
            """UPDATE executor_slots SET state='RELEASING', version=version+1
               WHERE host_id=%s AND slot_id=%s AND current_claim_id=%s AND state='CLAIMED'""",
            (session.executor_id, session.slot_id, session.claim_id),
        )
        session.state = "INVALID"
        return session

    def release(self, session_id, reason):
        return self._transaction(lambda cur: self._finish_session(cur, session_id, reason))

    def invalidate(self, session_id, reason, expected_epoch=None):
        return self._transaction(lambda cur: self._finish_session(cur, session_id, reason, expected_epoch))

    def reclaim_instant_sessions(self, now, idle_before=None):
        """Atomically fence eligible sessions and return all pending releases."""
        def operation(cur):
            args = [now]
            idle_sql = ""
            if idle_before is not None:
                idle_sql = " OR (last_activity_at IS NOT NULL AND last_activity_at <= %s)"
                args.append(idle_before)
            cur.execute(
                f"""SELECT session_id FROM instant_sessions
                    WHERE state IN ('BINDING', 'ACTIVE', 'DRAINING')
                      AND ((expires_at IS NOT NULL AND expires_at <= %s){idle_sql})
                    FOR UPDATE SKIP LOCKED""",
                tuple(args),
            )
            for row in cur.fetchall():
                session_id = str(row[0])
                session = self._finish_session(cur, session_id, "lease_expired")
                if session is not None:
                    cur.execute(
                        """INSERT INTO control_outbox
                           (event_id, kind, entity_id, entity_version)
                           VALUES (%s, 'executor.release', %s, 1)""",
                        (str(uuid.uuid4()), session_id),
                    )
            cur.execute(
                f"""SELECT {self._session_sql()} FROM instant_sessions s
                    JOIN control_outbox o ON o.entity_id=s.session_id::text
                    WHERE o.kind='executor.release' AND o.published_at IS NULL
                    ORDER BY o.created_at, o.event_id"""
            )
            return [self._session(row) for row in cur.fetchall()]
        return self._transaction(operation)

    def complete_reclaim(self, session_id: str) -> None:
        """Make a fenced slot reusable only after executor release succeeds."""
        def operation(cur):
            session = self._read_session(cur, session_id, lock=True)
            if session is None:
                return
            cur.execute(
                """UPDATE executor_slots SET state='READY', current_claim_id=NULL,
                       version=version+1
                   WHERE host_id=%s AND slot_id=%s AND current_claim_id=%s
                     AND state='RELEASING'""",
                (session.executor_id, session.slot_id, session.claim_id),
            )
            cur.execute(
                """UPDATE control_outbox SET published_at=now()
                   WHERE kind='executor.release' AND entity_id=%s
                     AND published_at IS NULL""",
                (session_id,),
            )
        self._transaction(operation)

    def drain(self, executor_id, deadline):
        def operation(cur):
            cur.execute(
                """UPDATE executor_hosts SET state='DRAINING', drain_deadline_at=%s, version=version+1
                   WHERE host_id=%s RETURNING host_id""",
                (deadline, executor_id),
            )
            if cur.fetchone() is None:
                raise KeyError(executor_id)
            cur.execute("UPDATE executor_slots SET state='DRAINING', version=version+1 WHERE host_id=%s", (executor_id,))
            cur.execute(
                f"""UPDATE instant_sessions SET state='DRAINING', version=version+1
                    WHERE host_id=%s AND state='ACTIVE'
                    RETURNING {self._session_sql()}""",
                (executor_id,),
            )
            return [self._session(row) for row in cur.fetchall()]
        return self._transaction(operation)
