from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from executor.runtime.ed25519 import public_key, sign

SEED = bytes(range(32))
EXECUTOR_ID = "executor-local-001"


def digest(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode()).hexdigest()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def documents(source: str, *, assignment_id: str | None = None, lease_id: str | None = None) -> dict[str, Any]:
    assignment_id = assignment_id or str(uuid.uuid4())
    lease_id = lease_id or str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    source_digest = digest(source)
    catalog_digest = "sha256:" + "d" * 64
    capability = {"capability_id": "drawing.read", "tool_id": "count-by-layer", "tool_version": "1.0.0"}
    now = datetime.now(UTC)
    drawing_context = {"drawing_id": "rooftop-demo", "version_id": str(uuid.uuid4()),
                       "content_digest": "sha256:" + "e" * 64, "geometry_ref": "drawing-context:rooftop-ref-001"}
    assignment = {"contract": "leaf.instant-execution/v1", "assignment_id": assignment_id, "tenant_id": "tenant-demo",
                  "session_id": session_id, "executor_id": EXECUTOR_ID, "executor_endpoint": "http://127.0.0.1:8088",
                  "binding_epoch": 1, "lease_id": lease_id, "lease_token": "test-only-lease-token-000000000001",
                  "execution_class": "instant", "effective_catalog_digest": catalog_digest, "code_digest": source_digest,
                  "artifact_digest": source_digest, "drawing_context": drawing_context,
                  "issued_at": now.isoformat().replace("+00:00", "Z"),
                  "expires_at": (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")}
    load = {"contract": "leaf.instant-execution/v1", "load_id": str(uuid.uuid4()), "tenant_id": "tenant-demo",
            "session_id": session_id, "effective_catalog_digest": catalog_digest, "code_digest": source_digest,
            "artifact_digest": source_digest, "runtime": "python-3.12", "entrypoint": "tool:run"}
    catalog = {"contract": "leaf.instant-execution/v1", "catalog_commit": "0" * 40, "capability": capability,
               "execution_class": "instant", "runtime": "python-3.12",
               "limits": {"max_wall_ms": 200, "max_cpu_ms": 200, "max_memory_mb": 32, "max_output_bytes": 4096, "max_tool_calls": 0},
               "code_digest": source_digest, "artifact_digest": source_digest, "entrypoint": "tool:run",
               "params_schema_digest": "sha256:" + "c" * 64}
    invocation = {"contract": "leaf.instant-execution/v1", "invocation_id": str(uuid.uuid4()), "tenant_id": "tenant-demo",
                  "session_id": session_id, "assignment_id": assignment_id, "binding_epoch": 1, "lease_id": lease_id,
                  "effective_catalog_digest": catalog_digest, "code_digest": source_digest, "artifact_digest": source_digest,
                  "deadline_at": (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), "capability": capability,
                  "params": {"layer": "Panels"}, "drawing_context": drawing_context}
    return {"assignment": assignment, "code_load": load, "catalog": catalog, "source": source,
            "drawing_context": drawing_context, "invocation": invocation}


def lease(invocation: dict[str, Any], *, expires_in: int = 60, mutate: dict[str, Any] | None = None) -> str:
    claims = {"iss": "instant-control-plane", "aud": "instant-executor", "jti": invocation["lease_id"],
              "executor_id": EXECUTOR_ID, "tenant_id": invocation["tenant_id"], "session_id": invocation["session_id"],
              "assignment_id": invocation["assignment_id"], "binding_epoch": invocation["binding_epoch"],
              "effective_catalog_digest": invocation["effective_catalog_digest"], "code_digest": invocation["code_digest"],
              "artifact_digest": invocation["artifact_digest"], "capability": invocation["capability"],
              "lease_sequence": 1, "nbf": int(time.time()) - 1, "exp": int(time.time()) + expires_in}
    if mutate:
        claims.update(mutate)
    header = {"alg": "EdDSA", "kid": "test-key"}
    head, payload = b64(json.dumps(header, separators=(",", ":")).encode()), b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{head}.{payload}.{b64(sign(SEED, f'{head}.{payload}'.encode()))}"


def keys() -> dict[str, bytes]:
    return {"test-key": public_key(SEED)}
