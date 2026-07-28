"""In-memory supervisor for a fixed pool of prestarted restricted children."""
from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from typing import Any

from .child import child_main
from .contracts import ContractError, validate_contract
from .ed25519 import verify

SECRET_WORDS = re.compile(r"(?:secret|password|credential|authorization|api[_-]?key|access[_-]?key)", re.I)
MAX_INPUT_BYTES = 1_048_576
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 300.0
DEFAULT_IDEMPOTENCY_MAX_ENTRIES = 10_000


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
                 idempotency_max_entries: int = DEFAULT_IDEMPOTENCY_MAX_ENTRIES) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be positive")
        if idempotency_ttl_seconds <= 0:
            raise ValueError("idempotency_ttl_seconds must be positive")
        if idempotency_max_entries < 1:
            raise ValueError("idempotency_max_entries must be positive")
        self.executor_id = executor_id
        self.public_keys = dict(public_keys)
        self._ctx = multiprocessing.get_context("spawn")
        self._slots: list[Slot] = []
        self._idempotency: OrderedDict[tuple[str, str, str], tuple[float, str, dict[str, Any]]] = OrderedDict()
        self._idempotency_ttl_seconds = idempotency_ttl_seconds
        self._idempotency_max_entries = idempotency_max_entries
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
        ready = sum(slot.process.is_alive() and slot.assignment is None for slot in self._slots)
        bound = sum(slot.process.is_alive() and slot.assignment is not None for slot in self._slots)
        return {"contract": "leaf.instant-execution/v1", "executor_id": self.executor_id,
                "state": "ready" if ready or bound else "not_ready", "ready_slots": ready,
                "bound_slots": bound, "total_slots": len(self._slots), "observed_at": _now()}

    def metrics(self) -> str:
        state = self.health()
        return "\n".join((
            "# TYPE instant_executor_slots gauge",
            f"instant_executor_slots{{state=\"ready\"}} {state['ready_slots']}",
            f"instant_executor_slots{{state=\"bound\"}} {state['bound_slots']}",
            f"instant_executor_slots{{state=\"total\"}} {state['total_slots']}",
            f"instant_executor_idempotency_entries {len(self._idempotency)}",
            "",
        ))

    def assign(self, request: dict[str, Any]) -> dict[str, Any]:
        """Validate an immutable source artifact and load it into one ready child."""
        try:
            assignment = request["assignment"]
            load = request["code_load"]
            catalog = request["catalog"]
            source = request["source"]
            drawing_context = request["drawing_context"]
            validate_contract("session-assignment.v1.schema.json", assignment)
            validate_contract("code-load.v1.schema.json", load)
            validate_contract("catalog-entry.v1.schema.json", catalog)
        except (KeyError, ContractError) as exc:
            raise ExecutorError("INVALID_CONTRACT", str(exc)) from exc
        if not isinstance(source, str) or not isinstance(drawing_context, dict):
            raise ExecutorError("INVALID_CONTRACT", "source and assignment drawing_context are required")
        source_digest = "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
        for document in (assignment, load, catalog):
            if document["code_digest"] != source_digest or document["artifact_digest"] != source_digest:
                raise ExecutorError("ARTIFACT_DIGEST_MISMATCH", "source does not match immutable code and artifact digests")
        if assignment["executor_id"] != self.executor_id:
            raise ExecutorError("SESSION_BINDING_MISMATCH", "assignment names another executor")
        pairs = ("tenant_id", "session_id", "effective_catalog_digest", "code_digest", "artifact_digest")
        if any(assignment[key] != load[key] for key in pairs):
            raise ExecutorError("SESSION_BINDING_MISMATCH", "assignment and code load differ")
        if assignment["code_digest"] != catalog["code_digest"] or assignment["artifact_digest"] != catalog["artifact_digest"]:
            raise ExecutorError("CODE_DIGEST_MISMATCH", "catalog does not match assignment")
        if _secret_shaped(drawing_context):
            raise ExecutorError("FORBIDDEN_PAYLOAD_FIELD", "drawing context contains a credential-shaped field")
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
                reply = self._receive(slot, 2.0)
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
        if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > MAX_INPUT_BYTES:
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
        with self._state_lock:
            self._purge_idempotency()
            self._idempotency[key] = (time.monotonic() + self._idempotency_ttl_seconds, request_hash, response)
            self._idempotency.move_to_end(key)
            while len(self._idempotency) > self._idempotency_max_entries:
                self._idempotency.popitem(last=False)
        return response

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
                reply = self._receive(slot, 2.0)
            except (BrokenPipeError, EOFError, OSError, TimeoutError):
                return
            if reply.get("ok"):
                slot.assignment, slot.catalog, slot.load = assignment, catalog, load
                slot.source, slot.drawing_context = source, drawing_context

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
