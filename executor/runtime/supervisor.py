"""In-memory supervisor for a fixed pool of prestarted restricted children."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import multiprocessing
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from typing import Any, Callable

from executor.registry import ArtifactReference, ArtifactRegistryError, ImmutableArtifactRegistry, SignedArtifact

from .child import child_main
from .accounting import AccountingEmissionError
from .contracts import ContractError, validate_contract
from .ed25519 import verify

SECRET_WORDS = re.compile(r"(?:secret|password|credential|authorization|api[_-]?key|access[_-]?key)", re.I)
MAX_INPUT_BYTES = 1_048_576
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 300.0
DEFAULT_IDEMPOTENCY_MAX_ENTRIES = 10_000
DEFAULT_CHILD_LOAD_TIMEOUT_SECONDS = 2.0
DEFAULT_CAPACITY_SAMPLE_SECONDS = 30.0
# Bounded by HALF the companion alarm's period, not by a round number and not by
# the period itself. The staging `capacity_slots` alarm evaluates 60-second
# periods, so a period with no datapoint at all breaks the consecutive-breach
# count and the alarm can never fire: the gauge would exist and never be
# alarmable, which is the defect this whole feature removes.
#
# One sample per period is NOT enough to prevent that, for two reasons, and an
# earlier revision of this ceiling was equal to the period and was wrong on both:
#   1. Samples land on the wall clock, alarm periods land on fixed 60-second
#      boundaries. Exactly-one-period spacing puts two samples in one bucket and
#      none in the next the moment anything jitters.
#   2. `CapacitySampler._run()` samples and THEN waits, so real spacing is the
#      wait plus the sample's own duration -- necessarily more than the interval.
#      The deadline in `_run()` absorbs that term while the sample is quicker
#      than one interval, but the ceiling must not depend on it being so.
# At half a period both go away: every 60-second period holds two samples, and
# emptying one would take a sample that itself ran longer than 30 seconds.
CAPACITY_ALARM_PERIOD_SECONDS = 60.0
MAX_CAPACITY_SAMPLE_SECONDS = CAPACITY_ALARM_PERIOD_SECONDS / 2
STOP_JOIN_SECONDS = 5.0


_RUNTIME_EVENT_LOCK = threading.Lock()


def emit_runtime_event(record: dict[str, Any]) -> None:
    """Write one bounded JSON line to the container log plane, best effort.

    Slot telemetry must never break the path it observes, so every failure
    here is swallowed.  This is deliberately NOT the accounting emitter: an
    accounting write that fails has to fail its invocation, while a lost
    diagnostic line must not.

    Bounded and locked for the same reason the accounting emitter is: several
    slots can abandon a rebind at once, and two interleaved writes would
    produce lines no log consumer can parse.
    """
    try:
        encoded = json.dumps(
            {"event": "leaf.instant.runtime", "record": record},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") + b"\n"
        if len(encoded) <= 4096:
            with _RUNTIME_EVENT_LOCK:
                os.write(2, encoded)
    except (OSError, TypeError, ValueError):
        pass


class ExecutorError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False, disposition: str = "not_started") -> None:
        super().__init__(message)
        self.code, self.retryable, self.disposition = code, retryable, disposition


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _b64decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _secret_shaped(value: Any) -> bool:
    if isinstance(value, dict):
        return any(SECRET_WORDS.search(str(key)) or _secret_shaped(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret_shaped(item) for item in value)
    return isinstance(value, str) and value.lower().startswith(("bearer ", "basic "))


@dataclass
class Slot:
    slot_id: str
    process: multiprocessing.Process
    conn: Connection
    assignment: dict[str, Any] | None = None
    catalog: dict[str, Any] | None = None
    load: dict[str, Any] | None = None
    source: str | None = None
    drawing_context: dict[str, Any] | None = None
    loading: bool = False
    highest_lease_sequence: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class WarmExecutorSupervisor:
    """Starts processes once and routes already-authorized calls to a bound slot."""

    def __init__(self, executor_id: str, public_keys: dict[str, bytes], pool_size: int = 2,
                 *, idempotency_ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
                 idempotency_max_entries: int = DEFAULT_IDEMPOTENCY_MAX_ENTRIES,
                 artifact_registry: ImmutableArtifactRegistry | None = None,
                 trusted_development_fixtures: bool = False,
                 accounting_emitter: Any | None = None,
                 child_load_timeout_seconds: float = DEFAULT_CHILD_LOAD_TIMEOUT_SECONDS,
                 runtime_event_sink: Callable[[dict[str, Any]], None] = emit_runtime_event) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be positive")
        if idempotency_ttl_seconds <= 0:
            raise ValueError("idempotency_ttl_seconds must be positive")
        if idempotency_max_entries < 1:
            raise ValueError("idempotency_max_entries must be positive")
        if child_load_timeout_seconds <= 0:
            raise ValueError("child_load_timeout_seconds must be positive")
        self.executor_id = executor_id
        self.public_keys = dict(public_keys)
        self._ctx = multiprocessing.get_context("spawn")
        self._slots: list[Slot] = []
        self._idempotency: OrderedDict[tuple[str, str, str], tuple[float, str, dict[str, Any]]] = OrderedDict()
        self._idempotency_ttl_seconds = idempotency_ttl_seconds
        self._idempotency_max_entries = idempotency_max_entries
        self._artifact_registry = artifact_registry
        self._trusted_development_fixtures = trusted_development_fixtures
        self._accounting_emitter = accounting_emitter
        # Bounds how long a (possibly still-booting) child may take to
        # acknowledge a source load, on both the assign path and the
        # post-replacement rebind; a contended host needs more than the
        # production default, so tests pass a generous value.
        self._child_load_timeout_seconds = child_load_timeout_seconds
        self._runtime_event_sink = runtime_event_sink
        self._rebind_failures = 0
        # A leaf lock: nothing is acquired WHILE it is held.  The rebind path
        # reaches it holding slot.lock, and _state_lock is taken BEFORE
        # slot.lock elsewhere (release), so counting under _state_lock here
        # would invert that order.
        self._telemetry_lock = threading.Lock()
        self._state_lock = threading.RLock()
        for index in range(pool_size):
            self._slots.append(self._start_slot(f"slot-{index + 1}"))

    def _start_slot(self, slot_id: str) -> Slot:
        parent, child = self._ctx.Pipe()
        process = self._ctx.Process(target=child_main, args=(child,), daemon=True)
        process.start()
        child.close()
        return Slot(slot_id, process, parent)

    def close(self) -> None:
        for slot in self._slots:
            try:
                slot.conn.send({"action": "stop"})
            except (BrokenPipeError, EOFError, OSError):
                pass
            slot.process.join(timeout=1)
            if slot.process.is_alive():
                slot.process.terminate()
                slot.process.join(timeout=1)
            slot.conn.close()

    def process_ids(self) -> dict[str, int | None]:
        return {slot.slot_id: slot.process.pid for slot in self._slots}

    def health(self) -> dict[str, Any]:
        # ONE pass, reading each slot's liveness and binding exactly once.
        # Two passes could be torn by a concurrent assign or release between
        # them and report ready=1, bound=1, total=1 -- an impossible snapshot,
        # reproduced under a forced switch between the passes.  This runs
        # unlocked from three callers now (/health, /metrics, and the capacity
        # sampler's timer), so it has to be self-consistent without a lock:
        # reading each slot once guarantees ready + bound <= total and that no
        # slot is double-counted or dropped.
        observed = [(slot.process.is_alive(), slot.assignment is not None) for slot in self._slots]
        ready = sum(alive and not bound for alive, bound in observed)
        bound = sum(alive and bound for alive, bound in observed)
        return {"contract": "leaf.instant-execution/v1", "executor_id": self.executor_id,
                "state": "ready" if ready or bound else "not_ready", "ready_slots": ready,
                "bound_slots": bound, "total_slots": len(self._slots), "observed_at": _now()}

    def sample_capacity(self) -> None:
        """Publish the slot gauge `health()` already computes to the log plane.

        WHY THIS EXISTS.  `health()` and `/metrics` are the only places the
        free-slot count is ever stated, and both are pull-only on :8088.  The
        executor task definition deliberately declares no `task_role_arn`
        ("Executor application code cannot obtain AWS credentials"), and
        nothing scrapes that port, so those numbers reached no monitoring
        surface at all.  The consequence was a `CapacityAvailableSlots` alarm
        naming a metric nothing could ever publish.

        Writing the gauge as a runtime event solves that without touching the
        credential boundary: the line goes to fd 2, the awslogs driver already
        configured on the task carries it to CloudWatch Logs, and a metric
        filter in the terraform root turns it into a real metric.  A log line
        is the ONLY channel this process has that reaches AWS at all.

        The record repeats every field of `health()` that is a number or a
        state, and nothing else: no tenant, session, assignment, or source
        value is in scope here, so this line cannot leak one.

        A raise from `health()` is NOT caught here.  It belongs to
        `CapacitySampler._run()`, this method's only production caller, which
        both survives it and counts it -- swallowing it here would leave the
        sampler unable to tell a failing sample from a working one.
        """
        state = self.health()
        try:
            self._runtime_event_sink({
                "event_type": "capacity_sample",
                "executor_id": self.executor_id,
                "state": state["state"],
                "ready_slots": state["ready_slots"],
                "bound_slots": state["bound_slots"],
                "total_slots": state["total_slots"],
                "observed_at": state["observed_at"],
            })
        except Exception:  # noqa: BLE001
            # Same contract as _record_rebind_failure: the default sink cannot
            # raise, but the sink is injectable, and a sampler thread that dies
            # on a caller's telemetry bug would silently stop publishing the
            # very gauge an alarm is watching -- which fails toward "quiet", the
            # exact failure mode this whole change exists to remove.
            pass

    def metrics(self) -> str:
        state = self.health()
        return "\n".join((
            "# TYPE instant_executor_slots gauge",
            f"instant_executor_slots{{state=\"ready\"}} {state['ready_slots']}",
            f"instant_executor_slots{{state=\"bound\"}} {state['bound_slots']}",
            f"instant_executor_slots{{state=\"total\"}} {state['total_slots']}",
            f"instant_executor_idempotency_entries {len(self._idempotency)}",
            "# TYPE instant_executor_rebind_failures_total counter",
            f"instant_executor_rebind_failures_total {self._rebind_failures}",
            "",
        ))

    def assign(self, request: dict[str, Any]) -> dict[str, Any]:
        """Resolve one immutable artifact and load it into one ready child."""
        try:
            assignment = request["assignment"]
            load = request["code_load"]
            catalog = request["catalog"]
            drawing_context = request["drawing_context"]
            validate_contract("session-assignment.v1.schema.json", assignment)
            validate_contract("code-load.v1.schema.json", load)
            validate_contract("catalog-entry.v1.schema.json", catalog)
        except (KeyError, ContractError) as exc:
            raise ExecutorError("INVALID_CONTRACT", str(exc)) from exc
        if not isinstance(drawing_context, dict):
            raise ExecutorError("INVALID_CONTRACT", "assignment drawing_context is required")
        if assignment["executor_id"] != self.executor_id:
            raise ExecutorError("SESSION_BINDING_MISMATCH", "assignment names another executor")
        pairs = ("tenant_id", "session_id", "effective_catalog_digest", "code_digest", "artifact_digest")
        if any(assignment[key] != load[key] for key in pairs):
            raise ExecutorError("SESSION_BINDING_MISMATCH", "assignment and code load differ")
        if assignment["code_digest"] != catalog["code_digest"] or assignment["artifact_digest"] != catalog["artifact_digest"]:
            raise ExecutorError("CODE_DIGEST_MISMATCH", "catalog does not match assignment")
        if _secret_shaped(drawing_context):
            raise ExecutorError("FORBIDDEN_PAYLOAD_FIELD", "drawing context contains a credential-shaped field")
        source = self._resolve_source(request, assignment)
        with self._state_lock:
            existing = self._find_assignment(assignment["assignment_id"])
            if existing:
                if existing.loading:
                    raise ExecutorError("EXECUTOR_NOT_READY", "assignment is still loading", True)
                return self._readiness(existing)
            slot = next((item for item in self._slots if item.assignment is None and item.process.is_alive()), None)
            if slot is None:
                raise ExecutorError("EXECUTOR_NOT_READY", "no ready warm slot", True)
            slot.assignment, slot.load, slot.catalog = assignment, load, catalog
            slot.source, slot.drawing_context, slot.loading = source, drawing_context, True
        try:
            with slot.lock:
                slot.conn.send({"action": "load", "assignment_id": assignment["assignment_id"],
                                "source": source, "drawing_context": drawing_context,
                                "limits": catalog["limits"]})
                reply = self._receive(slot, self._child_load_timeout_seconds)
        except Exception:
            with self._state_lock:
                self._replace(slot, restore=False)
            raise
        if not reply.get("ok"):
            with self._state_lock:
                slot.assignment = slot.catalog = slot.load = None
                slot.source = slot.drawing_context = None
                slot.loading = False
            raise ExecutorError("ARTIFACT_UNAVAILABLE", reply.get("error", "child rejected source"))
        with self._state_lock:
            slot.loading = False
            return self._readiness(slot)

    def _resolve_source(self, request: dict[str, Any], assignment: dict[str, Any]) -> str:
        inline_source = request.get("source")
        if self._trusted_development_fixtures:
            if not isinstance(inline_source, str):
                raise ExecutorError("INVALID_CONTRACT", "trusted development fixtures require source")
            source_digest = "sha256:" + hashlib.sha256(inline_source.encode("utf-8")).hexdigest()
            if (assignment["code_digest"] != source_digest
                    or assignment["artifact_digest"] != source_digest):
                raise ExecutorError("ARTIFACT_DIGEST_MISMATCH", "fixture source does not match immutable digests")
            return inline_source
        if inline_source is not None:
            raise ExecutorError("UNTRUSTED_SOURCE_REJECTED", "inline source is only allowed in trusted development fixtures")
        if self._artifact_registry is None:
            raise ExecutorError("ARTIFACT_UNAVAILABLE", "immutable artifact registry is not configured", True)
        reference = ArtifactReference(
            tenant_id=assignment["tenant_id"],
            catalog_version=assignment["effective_catalog_digest"],
            artifact_digest=assignment["artifact_digest"],
            code_digest=assignment["code_digest"],
        )
        try:
            envelope = request.get("artifact")
            if envelope is None:
                return self._artifact_registry.resolve(reference).decode("utf-8")
            if not isinstance(envelope, dict):
                raise ExecutorError("INVALID_CONTRACT", "signed artifact envelope must be an object")
            if set(envelope) != {"source_b64", "signing_key_id", "signature_b64"}:
                raise ExecutorError("INVALID_CONTRACT", "signed artifact envelope has unknown or missing fields")
            key_id = envelope["signing_key_id"]
            if not isinstance(key_id, str) or not key_id:
                raise ExecutorError("INVALID_CONTRACT", "artifact signing key ID is invalid")
            signed = SignedArtifact(
                reference=reference,
                bytes=_b64decode(envelope["source_b64"]),
                signing_key_id=key_id,
                signature=_b64decode(envelope["signature_b64"]),
            )
            return self._artifact_registry.resolve_signed(signed).decode("utf-8")
        except ArtifactRegistryError as exc:
            raise ExecutorError(exc.code, str(exc)) from exc
        except ExecutorError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutorError("INVALID_CONTRACT", "signed artifact envelope is malformed") from exc
        except UnicodeDecodeError as exc:
            raise ExecutorError("ARTIFACT_UNAVAILABLE", "artifact source is not valid UTF-8") from exc

    def release(self, assignment_id: str) -> dict[str, Any]:
        with self._state_lock:
            slot = self._find_assignment(assignment_id)
            if slot is None:
                return {"released": False, "assignment_id": assignment_id}
            with slot.lock:
                tenant_id = slot.assignment["tenant_id"]
                session_id = slot.assignment["session_id"]
                for key in [item for item in self._idempotency if item[:2] == (tenant_id, session_id)]:
                    self._idempotency.pop(key, None)
                # Address-space and CPU limits can only be lowered safely.  A
                # released slot must therefore get a fresh child before it can
                # accept an assignment with different limits.
                self._replace(slot, restore=False)
            return {"released": True, "assignment_id": assignment_id}

    def invoke(self, body: dict[str, Any], authorization: str | None) -> dict[str, Any]:
        # Batch markers are rejected explicitly, rather than accepted as an
        # unknown extension to the strict v1 envelope.
        if body.get("execution_class") == "batch" or body.get("batch"):
            return self._error(body, "EXECUTION_CLASS_DENIED", "batch work is not accepted by this endpoint")
        try:
            validate_contract("invocation.v1.schema.json", body)
        except ContractError as exc:
            return self._error(body, "INVALID_CONTRACT", str(exc))
        input_bytes = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        if input_bytes > MAX_INPUT_BYTES:
            return self._error(body, "INVALID_CONTRACT", "invocation input exceeds one megabyte")
        if _secret_shaped({"params": body["params"], "drawing_context": body["drawing_context"]}):
            return self._error(body, "FORBIDDEN_PAYLOAD_FIELD", "credential-shaped input is forbidden")
        request_hash = _canonical(body)
        key = (body["tenant_id"], body["session_id"], body["invocation_id"])
        try:
            slot, _claims = self._verify_lease(body, authorization)
        except ExecutorError as exc:
            return self._error(body, exc.code, str(exc), exc.retryable, exc.disposition)
        with self._state_lock:
            self._purge_idempotency()
            prior = self._idempotency.get(key)
            if prior:
                _expires_at, prior_hash, prior_response = prior
                if prior_hash != request_hash:
                    return self._error(body, "INVOCATION_CONFLICT", "invocation ID has another request hash")
                self._idempotency.move_to_end(key)
                return prior_response
        deadline = datetime.fromisoformat(body["deadline_at"].replace("Z", "+00:00")).timestamp()
        timeout = min(max(0.001, deadline - time.time()), slot.catalog["limits"]["max_wall_ms"] / 1000)
        wall_started = time.perf_counter_ns()
        if not self._emit_accounting(body, "accepted") or not self._emit_accounting(body, "started"):
            return self._error(body, "ACCOUNTING_UNAVAILABLE", "accounting event could not reach the delivery plane", True)
        reply: dict[str, Any] = {}
        with slot.lock:
            try:
                slot.conn.send({"action": "invoke", "assignment_id": body["assignment_id"], "params": body["params"]})
                reply = self._receive(slot, timeout)
            except TimeoutError:
                self._replace(slot)
                response = self._error(body, "DEADLINE_EXCEEDED", "tool exceeded its wall time", disposition="unknown")
            except (BrokenPipeError, EOFError, OSError):
                self._replace(slot)
                response = self._error(body, "TOOL_FAILED", "child exited during invocation", disposition="unknown")
            else:
                if not reply.get("ok"):
                    response = self._error(body, "TOOL_FAILED", reply.get("error", "tool failed"), disposition="completed")
                elif reply["bytes"] > slot.catalog["limits"]["max_output_bytes"]:
                    response = self._error(body, "TOOL_FAILED", "tool output exceeds configured limit", disposition="completed")
                else:
                    response = {"contract": "leaf.instant-execution/v1", "invocation_id": body["invocation_id"],
                                "tenant_id": body["tenant_id"], "session_id": body["session_id"], "status": "succeeded",
                                "code_digest": body["code_digest"], "completed_at": _now(), **reply["payload"]}
        wall_ms = max(0, (time.perf_counter_ns() - wall_started) // 1_000_000)
        terminal = {
            "outcome": "succeeded" if response["status"] == "succeeded" else "failed",
            "cpu_ms": max(0, int(reply.get("cpu_ms", 0))),
            "wall_ms": wall_ms,
            "memory_peak_bytes": max(0, int(reply.get("memory_peak_bytes", 0))),
            "input_bytes": input_bytes,
            "output_bytes": len(json.dumps(response, separators=(",", ":")).encode("utf-8")),
        }
        final_response = response
        if not self._emit_accounting(body, "terminal", **terminal):
            final_response = self._error(
                body, "ACCOUNTING_UNAVAILABLE",
                "terminal accounting event could not reach the delivery plane",
                True, "completed" if response["status"] == "succeeded" else "unknown",
            )
        with self._state_lock:
            self._purge_idempotency()
            self._idempotency[key] = (
                time.monotonic() + self._idempotency_ttl_seconds,
                request_hash,
                final_response,
            )
            self._idempotency.move_to_end(key)
            while len(self._idempotency) > self._idempotency_max_entries:
                self._idempotency.popitem(last=False)
        return final_response

    def _emit_accounting(self, body: dict[str, Any], state: str, **usage: Any) -> bool:
        if self._accounting_emitter is None:
            return True
        record = {
            "invocation_id": body["invocation_id"],
            "tenant_id": body["tenant_id"],
            "session_id": body["session_id"],
            "lease_id": body["lease_id"],
            "code_digest": body["code_digest"],
            "state": state,
            "occurred_at": _now(),
            **usage,
        }
        try:
            self._accounting_emitter.emit(record)
            return True
        except AccountingEmissionError:
            return False

    def _verify_lease(self, body: dict[str, Any], authorization: str | None) -> tuple[Slot, dict[str, Any]]:
        if not authorization or not authorization.startswith("Bearer "):
            raise ExecutorError("SESSION_EXPIRED", "missing bearer lease")
        token = authorization[7:]
        try:
            encoded_header, encoded_payload, encoded_signature = token.split(".")
            header = json.loads(_b64decode(encoded_header))
            claims = json.loads(_b64decode(encoded_payload))
            key = self.public_keys.get(header.get("kid"))
            signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
            if header.get("alg") != "EdDSA" or key is None or not verify(key, signed, _b64decode(encoded_signature)):
                raise ValueError("bad signature")
        except (ValueError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
            raise ExecutorError("SESSION_EXPIRED", "invalid lease signature") from exc
        if claims.get("aud") != "instant-executor" or claims.get("iss") != "instant-control-plane":
            raise ExecutorError("SESSION_EXPIRED", "lease issuer or audience mismatch")
        now = time.time()
        if not isinstance(claims.get("exp"), (int, float)) or now >= claims["exp"]:
            raise ExecutorError("SESSION_EXPIRED", "lease has expired")
        if isinstance(claims.get("nbf"), (int, float)) and now + 5 < claims["nbf"]:
            raise ExecutorError("SESSION_EXPIRED", "lease is not active")
        slot = self._find_assignment(body["assignment_id"])
        if slot is None or slot.assignment is None:
            raise ExecutorError("EXECUTOR_NOT_READY", "assignment is not bound", True)
        if slot.loading:
            raise ExecutorError("EXECUTOR_NOT_READY", "assignment is still loading", True)
        expected = {"jti": body["lease_id"], "executor_id": self.executor_id, "tenant_id": body["tenant_id"],
                    "session_id": body["session_id"], "assignment_id": body["assignment_id"],
                    "binding_epoch": body["binding_epoch"], "effective_catalog_digest": body["effective_catalog_digest"],
                    "code_digest": body["code_digest"], "artifact_digest": body["artifact_digest"],
                    "capability": body["capability"]}
        if any(claims.get(name) != value for name, value in expected.items()):
            raise ExecutorError("SESSION_BINDING_MISMATCH", "lease does not match invocation binding")
        assignment = slot.assignment
        if any(assignment[name] != body[name] for name in ("tenant_id", "session_id", "binding_epoch", "effective_catalog_digest", "code_digest", "artifact_digest")):
            raise ExecutorError("SESSION_BINDING_MISMATCH", "invocation does not match local assignment")
        if slot.catalog["capability"] != body["capability"]:
            raise ExecutorError("CAPABILITY_DENIED", "tool identity is not bound to this source")
        sequence = claims.get("lease_sequence")
        if not isinstance(sequence, int) or sequence < slot.highest_lease_sequence:
            raise ExecutorError("SESSION_EXPIRED", "lease fencing sequence is stale")
        slot.highest_lease_sequence = sequence
        return slot, claims

    def _receive(self, slot: Slot, timeout: float) -> dict[str, Any]:
        if not slot.conn.poll(timeout):
            raise TimeoutError("child did not answer")
        return slot.conn.recv()

    def _replace(self, slot: Slot, *, restore: bool = True) -> None:
        assignment, catalog, load = slot.assignment, slot.catalog, slot.load
        source, drawing_context = slot.source, slot.drawing_context
        old = slot.process
        old.terminate()
        old.join(timeout=1)
        slot.conn.close()
        replacement = self._start_slot(slot.slot_id)
        slot.process, slot.conn = replacement.process, replacement.conn
        slot.assignment = slot.catalog = slot.load = None
        slot.source = slot.drawing_context = None
        slot.highest_lease_sequence = 0
        if restore and assignment and catalog and load and source is not None and drawing_context is not None:
            try:
                slot.conn.send({"action": "load", "assignment_id": assignment["assignment_id"],
                                "source": source, "drawing_context": drawing_context, "limits": catalog["limits"]})
                reply = self._receive(slot, self._child_load_timeout_seconds)
            except (BrokenPipeError, EOFError, OSError, TimeoutError) as exc:
                self._record_rebind_failure(slot, assignment, type(exc).__name__)
                return
            if reply.get("ok"):
                slot.assignment, slot.catalog, slot.load = assignment, catalog, load
                slot.source, slot.drawing_context = source, drawing_context
            else:
                self._record_rebind_failure(slot, assignment, "source_rejected")

    def _record_rebind_failure(self, slot: Slot, assignment: dict[str, Any], reason: str) -> None:
        """Surface an abandoned post-replacement rebind.

        Both failure paths above leave the slot unbound after the caller has
        already been answered.  The session is then unreachable: every later
        invocation answers EXECUTOR_NOT_READY with retryable=true, so a client
        retries forever against a binding that will never come back.  Without
        this line nothing on any surface says that happened -- bound_slots just
        quietly drops, and the executor publishes no CloudWatch metric that any
        alarm could see.
        """
        with self._telemetry_lock:
            self._rebind_failures += 1
        try:
            self._runtime_event_sink({
                "event_type": "slot_rebind_failed",
                "executor_id": self.executor_id,
                "slot_id": slot.slot_id,
                "assignment_id": assignment["assignment_id"],
                "tenant_id": assignment["tenant_id"],
                "session_id": assignment["session_id"],
                "reason": reason,
                "child_load_timeout_seconds": self._child_load_timeout_seconds,
                "occurred_at": _now(),
            })
        except Exception:  # noqa: BLE001 - see below
            # The default sink cannot raise, but the sink is injectable, so an
            # unguarded call would let a caller's telemetry break the very path
            # it observes: this runs inside invoke()'s failure handling, and an
            # escaping exception would cost the caller its DEADLINE_EXCEEDED or
            # TOOL_FAILED response and skip terminal accounting and idempotency
            # recording.  The counter above is already incremented, so a
            # swallowed sink still leaves the failure visible on /metrics.
            pass

    def _purge_idempotency(self) -> None:
        now = time.monotonic()
        for key, (expires_at, _request_hash, _response) in list(self._idempotency.items()):
            if expires_at <= now:
                self._idempotency.pop(key)

    def _find_assignment(self, assignment_id: str) -> Slot | None:
        return next((slot for slot in self._slots if slot.assignment and slot.assignment["assignment_id"] == assignment_id), None)

    def _readiness(self, slot: Slot) -> dict[str, Any]:
        return {"contract": "leaf.instant-execution/v1", "executor_id": self.executor_id,
                "assignment_id": slot.assignment["assignment_id"], "slot_id": slot.slot_id,
                "state": "ready", "accepting_new_invocations": True, "observed_at": _now()}

    @staticmethod
    def _error(body: dict[str, Any], code: str, message: str, retryable: bool = False,
               disposition: str = "not_started") -> dict[str, Any]:
        return {"contract": "leaf.instant-execution/v1", "invocation_id": body.get("invocation_id", "unknown"),
                "tenant_id": body.get("tenant_id", "unknown"), "session_id": body.get("session_id", "unknown"),
                "status": "failed", "code_digest": body.get("code_digest", "sha256:" + "0" * 64),
                "completed_at": _now(), "error": {"code": code, "message": message[:512],
                "retryable": retryable, "execution_disposition": disposition}}


class CapacitySampler:
    """Drives `sample_capacity()` on a daemon thread for the life of the server.

    A gauge only alarms if it keeps arriving, so this publishes on a fixed
    interval rather than on slot transitions: an executor whose slots are all
    bound produces no transitions at all, and that is precisely the state the
    capacity alarm has to see.  It samples on start too, so a metric exists
    within a second of boot instead of one interval later.

    The thread is a daemon and waits on an Event, so `stop()` returns promptly
    and an un-stopped sampler can never hold the process open.
    """

    def __init__(self, supervisor: WarmExecutorSupervisor,
                 interval_seconds: float = DEFAULT_CAPACITY_SAMPLE_SECONDS,
                 *,
                 runtime_event_sink: Callable[[dict[str, Any]], None] = emit_runtime_event) -> None:
        # Non-finite values pass a bare `<= 0` test and then break in ways that
        # are much worse than a rejected config.  `nan` makes Event.wait return
        # instantly, so the sampler busy-loops and floods the log group, and
        # Thread.join(nan) then raises ValueError out of stop(); `inf` makes
        # Thread.join(inf) raise OverflowError.  Either exception escapes
        # serve_registered's cleanup.  All four behaviours were reproduced.
        if not math.isfinite(interval_seconds):
            raise ValueError("interval_seconds must be a finite number")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if interval_seconds > MAX_CAPACITY_SAMPLE_SECONDS:
            raise ValueError(
                f"interval_seconds must be at most {MAX_CAPACITY_SAMPLE_SECONDS:g}, "
                f"half the companion alarm's {CAPACITY_ALARM_PERIOD_SECONDS:g}s period; "
                "a longer interval can leave a period with no datapoint and the "
                "alarm can never fire")
        self._supervisor = supervisor
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # The sampler carries its OWN sink rather than borrowing the
        # supervisor's.  The thing it has to report on is a supervisor whose
        # `health()` just raised, and reaching into that same object for the
        # channel used to say so would make the report depend on the component
        # it is reporting about.
        self._runtime_event_sink = runtime_event_sink
        self._consecutive_failures = 0

    @classmethod
    def from_environment(cls, supervisor: WarmExecutorSupervisor,
                         environ: dict[str, str] | None = None) -> "CapacitySampler":
        source = os.environ if environ is None else environ
        raw = source.get("LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS", "").strip()
        if not raw:
            return cls(supervisor)
        try:
            interval = float(raw)
        except ValueError as exc:
            raise ValueError("LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS must be a number") from exc
        return cls(supervisor, interval)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("capacity sampler already started")
        self._thread = threading.Thread(target=self._run, name="capacity-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # A FIXED short join, not `interval + 1`.  The sink writes to fd 2, and
        # a backed-up fd 2 makes that write block, so joining for the configured
        # interval would stall every later step of shutdown for as long as the
        # operator set the interval to.  Shutdown must not be hostage to a
        # telemetry write.  Past the timeout the thread is a daemon, and its
        # only remaining act is one more sample -- which reads liveness and
        # bindings, never a closed pipe -- so letting it go is safe.
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=STOP_JOIN_SECONDS)

    def _record_sample_failure(self, exc: BaseException) -> None:
        """Say that a sample failed, on a schedule that cannot flood the log group.

        A swallowed failure is barely better than a dead thread: both publish
        nothing, and `capacity_slots` reads both as healthy quiet.  So the
        failure gets its own line -- and the line is what a metric filter in the
        terraform root would key on to alarm on a running-but-blind sampler.
        Wiring that filter and its alarm is a terraform-side change and is NOT
        in this commit; what is here is the event it would need.

        BOUNDED BY OUTPUT, NOT BY TRIGGER.  A permanently broken `health()`
        fails every interval forever, so reporting each one would emit two lines
        a minute at the default interval and far more at a short one, into the
        same log group the gauge itself uses.  Reporting only at counts that are
        a power of two (1, 2, 4, 8, ...) keeps the first failure immediate -- the
        one an operator needs -- while a run of length n costs log2(n) lines
        instead of n.  The count rides in every line, so a consumer reading only
        the newest one still learns how long the run is.

        The exception TYPE is reported and its message is NOT.  `sample_capacity`
        documents that its record carries no tenant, session, assignment, or
        source value, and a test pins that; an uncontrolled exception string
        would put that promise back in play (a KeyError on a session-keyed dict
        prints the session id).  The type alone separates the causes worth
        separating and cannot carry a payload.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures & (self._consecutive_failures - 1):
            return  # not a power of two; already reported at the last one
        self._emit({
            "event_type": "capacity_sample_failed",
            "executor_id": self._supervisor.executor_id,
            "error_type": type(exc).__name__,
            "consecutive_failures": self._consecutive_failures,
            "occurred_at": _now(),
        })

    def _record_sample_success(self) -> None:
        """Close a run of failures, so the log says when the gauge came back.

        Without this the newest `capacity_sample_failed` line is unbounded in
        time: a consumer cannot tell a run that ended two hours ago from one
        still going, because a healthy sampler is silent about its own health.
        This fires at most once per run of failures, and never on a sampler that
        has not failed.

        `consecutive_failures` is ZERO here, and the run's length rides in
        `recovered_after_failures` instead.  The two fields are not
        interchangeable and an earlier revision published the run length under
        the first name, which reads as a still-failing sampler on the one event
        that means the opposite.  A metric filter selecting
        `$.record.consecutive_failures` from both event types now gets exactly
        what an alarm needs -- the count climbing while the sampler is blind and
        a 0 datapoint the moment it recovers, which is what lets that alarm
        clear.  Reading the run's length is then a second, separate filter.
        """
        if not self._consecutive_failures:
            return
        recovered_after, self._consecutive_failures = self._consecutive_failures, 0
        self._emit({
            "event_type": "capacity_sample_recovered",
            "executor_id": self._supervisor.executor_id,
            "consecutive_failures": 0,
            "recovered_after_failures": recovered_after,
            "occurred_at": _now(),
        })

    def _emit(self, record: dict[str, Any]) -> None:
        # Same contract as the sinks in the supervisor, and load-bearing twice
        # over here: this runs on the path that exists BECAUSE something already
        # raised, so a sink that raises in turn would kill the thread from
        # inside the handler that exists to keep it alive.  It cannot recurse --
        # nothing in here calls back into the sample path -- and the failure
        # counter is advanced before the emit, so a sink that raises every time
        # still decays to log2 attempts rather than one per interval.
        try:
            self._runtime_event_sink(record)
        except Exception:  # noqa: BLE001 - see above
            pass

    def _run(self) -> None:
        # Wait to a DEADLINE, not for a fixed duration. Sampling then waiting the
        # full interval spaces samples by `interval + however long the sample
        # took`, which drifts later every cycle -- and the sample writes to fd 2,
        # which can block. Subtracting the sample's own cost keeps spacing at the
        # interval instead of above it, so the ceiling's guarantee holds against
        # the interval rather than against the interval plus unbounded work.
        deadline = time.monotonic()
        while not self._stop.is_set():
            # The ONLY thing standing between a raising sample and a gauge that
            # is silent for the life of the process.  `sample_capacity()` guards
            # its sink but not the `health()` call above it, and this loop is a
            # bare daemon thread: an escaping exception ends the thread, nothing
            # restarts it, and no surface says so.  The `capacity_slots` alarm
            # then reads the resulting absence as notBreaching -- deliberately,
            # because when the whole EXECUTOR dies two other alarms already fire
            # -- while `capacity` and `registration` both stay green because the
            # task is alive and still heartbeating.  A sampler-only death is
            # therefore invisible on every surface, which is why it is caught
            # here rather than left to the alarm.
            #
            # The guard wraps the call and NOTHING else: the deadline arithmetic
            # below is what keeps the sample's own cost out of the spacing, and
            # it must run on the failure path too, or a fast-failing sample would
            # spin the loop.  `Exception`, not `BaseException`: a KeyboardInterrupt
            # or SystemExit aimed at this thread should still end it.
            try:
                self._supervisor.sample_capacity()
            except Exception as exc:  # noqa: BLE001 - see above
                self._record_sample_failure(exc)
            else:
                self._record_sample_success()
            deadline += self._interval_seconds
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The sample outran a whole interval. Resync rather than firing a
                # catch-up burst into the log group; the next sample is already
                # overdue, so take it now and re-baseline from here.
                deadline = time.monotonic()
                continue
            if self._stop.wait(remaining):
                return
