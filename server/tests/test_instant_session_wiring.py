from __future__ import annotations

import hashlib
import base64
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

import instant_execution
from instant_artifact_registry import FilesystemTrustedPlatformArtifactRegistry
import turn_runner
from routers import sessions as sessions_router


class _Response:
    status_code = 200

    def __init__(self, body=None):
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        return None

    def close(self):
        return None


def _assignment(tenant_id="tenant-demo", session_id="22222222-2222-4222-8222-222222222222"):
    return {
        "contract": "leaf.instant-execution/v1",
        "assignment_id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": tenant_id,
        "session_id": session_id,
        "executor_id": "executor-local-001",
        "executor_endpoint": "http://127.0.0.1:8160",
        "binding_epoch": 1,
        "lease_id": "77777777-7777-4777-8777-777777777777",
        "lease_token": "test-only-ed25519-lease-token-0000000000000001",
        "execution_class": "instant",
        "effective_catalog_digest": "sha256:" + ("d" * 64),
        "code_digest": "sha256:" + ("a" * 64),
        "artifact_digest": "sha256:" + ("b" * 64),
        "drawing_context": {
            "drawing_id": "rooftop-demo",
            "version_id": "55555555-5555-4555-8555-555555555555",
            "content_digest": "sha256:" + ("e" * 64),
            "geometry_ref": "drawing-context:rooftop-ref-001",
        },
        "issued_at": "2099-07-28T16:00:00Z",
        "expires_at": "2099-07-28T16:05:00Z",
    }


def test_prepare_assigns_and_loads_before_reporting_ready(monkeypatch, tmp_path):
    instant_execution._reset_for_tests()
    source = b"def run(intake, params):\n    return {'ok': True}\n"
    path = tmp_path / "tool.py"
    path.write_bytes(source)
    code_digest = "sha256:" + hashlib.sha256(source).hexdigest()
    tool = {
        "name": "instant-read",
        "version": "1.0.0",
        "capabilities": ["drawing.read"],
        "execution_class": "instant",
        "runtime": "python-3.12",
        "entry": "tool.py",
        "limits": {"max_wall_ms": 100, "max_cpu_ms": 50, "max_memory_mb": 64,
                   "max_output_bytes": 65536, "max_tool_calls": 0},
        "params": {"type": "object", "properties": {}},
        "code_digest": code_digest,
        "artifact_digest": code_digest,
    }
    posted = []
    assignment = _assignment()
    assignment["effective_catalog_digest"] = "sha256:" + instant_execution.deps.base_catalog_pin(
        instant_execution.deps.all_tools("tenant-demo")
    )["effective_catalog_digest"]
    assignment["code_digest"] = code_digest
    assignment["artifact_digest"] = code_digest

    monkeypatch.setenv("LEAF_INSTANT_CONTROL_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(instant_execution, "_eligible_tool", lambda: tool)
    monkeypatch.setattr(
        instant_execution, "ARTIFACT_REGISTRY",
        FilesystemTrustedPlatformArtifactRegistry(path.parent),
    )
    monkeypatch.setattr(
        instant_execution, "_sanitized_drawing_context",
        lambda _drawing: ({"layers": ["Panels"]}, assignment["drawing_context"]),
    )
    monkeypatch.setattr(
        instant_execution.requests, "post",
        lambda url, **kwargs: posted.append((url, kwargs)) or _Response(assignment),
    )

    status = instant_execution.prepare_session(
        "tenant-demo", assignment["session_id"], "rooftop-demo",
    )

    assert status == {"ready": True, "reason": None}
    assert posted[0][0] == "http://127.0.0.1:8080/v1/sessions"
    assert posted[0][1]["json"]["artifact"]["source"].startswith("def run")
    assert instant_execution.assignment_for_session(
        "tenant-demo", assignment["session_id"],
    )["lease_token"] == assignment["lease_token"]
    # Idempotent repeat uses the ready binding. It does not reload source.
    assert instant_execution.prepare_session(
        "tenant-demo", assignment["session_id"], "rooftop-demo",
    )["ready"] is True
    assert len(posted) == 1


def test_tenant_authored_path_never_enters_the_instant_loader(monkeypatch):
    tenant_tool = {
        "name": "tenant-python",
        "entry": "../../tenant-repo/tool.py",
        "execution_class": "instant",
        "runtime": "python-3.12",
        "capabilities": ["drawing.read"],
    }
    calls = []
    monkeypatch.setattr(instant_execution.deps, "all_tools", lambda tenant: calls.append(tenant) or [tenant_tool])
    monkeypatch.setattr(instant_execution.deps, "load_seed_catalog_tools", lambda: [])

    assert instant_execution._eligible_tool() is None
    assert calls == []


def test_create_session_returns_readiness_but_never_assignment_secret(monkeypatch):
    monkeypatch.setattr(
        sessions_router.session_store, "get_or_create_session",
        lambda *_args: {
            "session_id": "session-1", "status": "active", "created_at": 1.0,
            "model": None,
        },
    )
    monkeypatch.setattr(
        sessions_router.instant_execution, "prepare_session",
        lambda *_args: {"ready": True, "reason": None},
    )

    body = sessions_router.create_session(
        sessions_router.CreateSessionRequest(drawing_id="drawing-1"),
        tenant="tenant-demo",
    )

    encoded = str(body)
    assert body["instant_ready"] is True
    assert "lease" not in encoded
    assert "executor_endpoint" not in encoded


def test_control_transport_requires_mtls_and_rejects_nonloopback_http(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        instant_execution._control_transport("http://control.internal:8080")
    with pytest.raises(ValueError, match="requires CA"):
        instant_execution._control_transport("https://control.internal:8080")

    ca_file = tmp_path / "ca.pem"
    cert_file = tmp_path / "client.pem"
    key_file = tmp_path / "client.key"
    for filename in (ca_file, cert_file, key_file):
        filename.write_text("test", encoding="utf-8")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_CA_FILE", str(ca_file))
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_CLIENT_CERT_FILE", str(cert_file))
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_CLIENT_KEY_FILE", str(key_file))

    assert instant_execution._control_transport("https://control.internal:8080") == {
        "verify": str(ca_file),
        "cert": (str(cert_file), str(key_file)),
    }


def test_assignment_validation_rejects_digest_substitution_and_expiry():
    assignment = _assignment()
    expected = {
        "effective_catalog_digest": assignment["effective_catalog_digest"],
        "artifact": {
            "code_digest": assignment["code_digest"],
            "artifact_digest": assignment["artifact_digest"],
        },
        "drawing_context": {"reference": assignment["drawing_context"]},
    }
    assert instant_execution._assignment_valid(
        assignment, assignment["tenant_id"], assignment["session_id"], expected,
    )

    substituted = dict(assignment)
    substituted["artifact_digest"] = "sha256:" + ("f" * 64)
    assert not instant_execution._assignment_valid(
        substituted, assignment["tenant_id"], assignment["session_id"], expected,
    )

    expired = dict(assignment)
    expired["expires_at"] = "2000-01-01T00:00:00Z"
    assert not instant_execution._assignment_valid(
        expired, assignment["tenant_id"], assignment["session_id"], expected,
    )

    # The control plane selects a healthy host from the shared pool. The app
    # validates the returned binding but does not pin a specific executor.
    other_host = dict(assignment)
    other_host["executor_id"] = "executor-pool-002"
    assert instant_execution._assignment_valid(
        other_host, assignment["tenant_id"], assignment["session_id"], expected,
    )


def test_assignment_renews_before_half_life(monkeypatch):
    instant_execution._reset_for_tests()
    assignment = _assignment()
    now = datetime.now(UTC)
    assignment["issued_at"] = (now - timedelta(seconds=40)).isoformat().replace("+00:00", "Z")
    assignment["expires_at"] = (now + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    key = (assignment["tenant_id"], assignment["session_id"])
    with instant_execution._lock:
        instant_execution._assignments[key] = dict(assignment)
    renewed_expiry = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(
        instant_execution.requests, "post",
        lambda url, **kwargs: _Response({
            "lease_id": "88888888-8888-4888-8888-888888888888",
            "lease_token": "renewed-test-only-token-00000000000000000001",
            "expires_at": renewed_expiry,
        }),
    )

    current = instant_execution.assignment_for_session(*key)

    assert current["lease_id"] == "88888888-8888-4888-8888-888888888888"
    assert instant_execution._expires_at(current) > time.time() + 200


def test_ninety_second_fake_clock_soak_renews_one_session_without_redis(monkeypatch):
    instant_execution._reset_for_tests()
    assignment = _assignment()
    base = datetime(2099, 7, 28, 16, 0, tzinfo=UTC).timestamp()
    clock = [base]
    assignment["issued_at"] = datetime.fromtimestamp(base, UTC).isoformat().replace("+00:00", "Z")
    assignment["expires_at"] = datetime.fromtimestamp(base + 60, UTC).isoformat().replace("+00:00", "Z")
    key = (assignment["tenant_id"], assignment["session_id"])
    instant_execution._remember_assignment(key, assignment)
    monkeypatch.setattr(instant_execution, "_now", lambda: clock[0])
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_SECRET", "test-control-secret")
    renewals = []

    def _renew(_url, **_kwargs):
        renewals.append(clock[0])
        return _Response({
            "lease_id": f"lease-{len(renewals)}",
            "lease_token": f"token-{len(renewals)}",
            "expires_at": datetime.fromtimestamp(clock[0] + 60, UTC).isoformat().replace("+00:00", "Z"),
        })

    monkeypatch.setattr(instant_execution.requests, "post", _renew)
    for _ in range(6):
        clock[0] += 15
        assert instant_execution.assignment_for_session(*key) is not None

    assert clock[0] - base == 90
    assert len(renewals) == 3
    assert instant_execution.assignment_for_session(*key)["lease_id"] == "lease-3"


def test_failed_renewal_never_returns_an_expired_assignment(monkeypatch):
    instant_execution._reset_for_tests()
    assignment = _assignment()
    base = datetime(2099, 7, 28, 16, 0, tzinfo=UTC).timestamp()
    clock = [base]
    assignment["issued_at"] = datetime.fromtimestamp(base - 50, UTC).isoformat().replace("+00:00", "Z")
    assignment["expires_at"] = datetime.fromtimestamp(base + 1, UTC).isoformat().replace("+00:00", "Z")
    key = (assignment["tenant_id"], assignment["session_id"])
    instant_execution._remember_assignment(key, assignment)
    monkeypatch.setattr(instant_execution, "_now", lambda: clock[0])
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("LEAF_INSTANT_CONTROL_SECRET", "test-control-secret")

    def _fail(_url, **_kwargs):
        clock[0] += 2
        raise instant_execution.requests.RequestException("renewal failed")

    monkeypatch.setattr(instant_execution.requests, "post", _fail)
    assert instant_execution.assignment_for_session(*key) is None
    assert key not in instant_execution._assignments


def test_prepare_is_single_flight_for_one_session(monkeypatch):
    instant_execution._reset_for_tests()
    started, allow_return = threading.Event(), threading.Event()
    calls = []

    def _prepare(*_args):
        calls.append(1)
        started.set()
        assert allow_return.wait(2)
        instant_execution._remember_assignment(("tenant", "session"), _assignment(tenant_id="tenant", session_id="session"))
        return {"ready": True, "reason": None}

    monkeypatch.setattr(instant_execution, "_prepare_session_uncached", _prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(instant_execution.prepare_session, "tenant", "session", "drawing")
        assert started.wait(2)
        second = pool.submit(instant_execution.prepare_session, "tenant", "session", "drawing")
        allow_return.set()
        assert first.result() == {"ready": True, "reason": None}
        assert second.result() == {"ready": True, "reason": None}
    assert calls == [1]


def test_assignment_cache_evicts_oldest_entry_at_its_bound(monkeypatch):
    instant_execution._reset_for_tests()
    monkeypatch.setenv("LEAF_INSTANT_CACHE_MAX", "2")
    for suffix in ("one", "two", "three"):
        instant_execution._remember_assignment(("tenant", suffix), _assignment(session_id=suffix))
    assert list(instant_execution._assignments) == [("tenant", "two"), ("tenant", "three")]


def test_turn_back_edge_receives_assignment_without_transcript_persistence(monkeypatch):
    assignment = _assignment(session_id="session-1")
    captured = {"body": None, "headers": None}
    appended = []

    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", "http://harness.internal")
    monkeypatch.setattr(
        turn_runner, "_require_session",
        lambda *_args: {"drawing_id": "drawing-1", "model": None},
    )
    monkeypatch.setattr(turn_runner.session_store, "try_begin_turn", lambda *_a, **_k: True)
    monkeypatch.setattr(
        turn_runner.session_store, "append_event",
        lambda _sid, _tid, kind, data: appended.append((kind, data)) or 1,
    )
    monkeypatch.setattr(turn_runner, "_prior_messages", lambda *_a, **_k: [])
    monkeypatch.setattr(turn_runner, "_spawn_relay", lambda *_a, **_k: None)
    monkeypatch.setattr(
        turn_runner.instant_execution, "assignment_for_session",
        lambda *_args: assignment,
    )

    def _post(_url, **kwargs):
        captured["body"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return _Response()

    monkeypatch.setattr(turn_runner.requests, "post", _post)

    turn_runner.start_turn("tenant-demo", "session-1", text="count panels")

    encoded = captured["headers"]["x-leaf-instant-assignment"]
    assert json.loads(base64.urlsafe_b64decode(encoded)) == assignment
    assert "instant_assignment" not in captured["body"]
    assert appended[0] == ("turn_started", {"text": "count panels"})
    assert "instant_assignment" not in appended[0][1]
