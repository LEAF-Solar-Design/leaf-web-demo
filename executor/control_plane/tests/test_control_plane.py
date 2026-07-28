from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import threading
import unittest
import uuid

from executor.contracts.validate_contracts import validate
from executor.control_plane.api import application
from executor.control_plane.coordination import CoordinationUnavailable
from executor.control_plane.jws import LeaseSigner, verify_jws
from executor.control_plane.service import ControlPlane, ControlPlaneError
from executor.control_plane.store import InMemoryStore, StaleFence


def digest(letter: str) -> str:
    return "sha256:" + letter * 64


class Clock:
    def __init__(self): self.now = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)
    def __call__(self): return self.now


class Coordination:
    def __init__(self): self.available = True; self.lock = threading.Lock(); self.events = []
    def check_available(self):
        if not self.available: raise CoordinationUnavailable("down")
    def acquire_claim_lock(self, *_):
        if not self.available or not self.lock.acquire(False): raise CoordinationUnavailable("down or busy")
        return "lock"
    def release_claim_lock(self, *_):
        if self.lock.locked(): self.lock.release()
    def publish_hint(self, *event): self.events.append(event)


class Runtime:
    def __init__(self, ready=True): self.ready = ready; self.events = []
    def assign(self, endpoint, command): self.events.append(("assign", endpoint, command)); return {"state": "ready" if self.ready else "not_ready"}
    def release(self, endpoint, command): self.events.append(("release", endpoint, command))


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.store, self.coord, self.runtime = Clock(), InMemoryStore(), Coordination(), Runtime()
        self.plane = ControlPlane(self.store, self.runtime, LeaseSigner(b"a" * 32, "test-key"), self.coord, self.clock)
        self.plane.register_host("executor-test-001", "https://executor.internal:8443", ["slot-1"])
        self.plane.readiness("executor-test-001", "slot-1", True, digest("c"), digest("r"))

    def request(self, session_id=None, code="a"):
        reference = {"drawing_id": "drawing-demo", "version_id": str(uuid.uuid4()), "content_digest": digest("f"), "geometry_ref": "drawing-context:rooftop-ref-001"}
        return {"tenant_id": "tenant-demo", "session_id": session_id or str(uuid.uuid4()), "effective_catalog_digest": digest("d"),
                "drawing_context": {"reference": reference, "data": {"layers": ["Panels"]}},
                "artifact": {"source": "trusted-registry://instant/tool", "code_digest": digest(code), "artifact_digest": digest("b"), "runtime": "python-3.12", "entrypoint": "leaf_tools.tool:run", "limits": {"max_wall_ms": 5000, "max_cpu_ms": 3000, "max_memory_mb": 64, "max_output_bytes": 65536, "max_tool_calls": 0}, "tool_id": "instant-list-layers", "tool_version": "1.0.0", "capability_id": "drawing.read", "params_schema_digest": digest("p"), "catalog_commit": "a" * 40}}

    @staticmethod
    def call_api(app, path, body, secret=None):
        captured = []
        environ = {"REQUEST_METHOD": "POST", "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if secret is not None:
            environ["HTTP_X_INSTANT_CONTROL_SECRET"] = secret
        response = app(environ, lambda status, _: captured.append(status))
        return captured[0], json.loads(b"".join(response))

    def test_assignment_validates_and_lease_verifies(self):
        assignment = self.plane.assign(self.request())
        with open("executor/contracts/schemas/session-assignment.v1.schema.json", encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertEqual(validate(assignment, schema), [])
        payload = verify_jws(assignment["lease_token"], self.plane.signer.jwks(), now=int(self.clock.now.timestamp()))
        self.assertEqual(payload["aud"], "instant-executor")
        self.assertEqual(payload["tenant_id"], assignment["tenant_id"])
        self.assertEqual(payload["lease_sequence"], 1)
        self.assertEqual(self.runtime.events[0][0], "assign")
        self.assertTrue(self.runtime.events[0][2]["code_load"])
        self.assertNotIn("d", self.plane.signer.jwks()["keys"][0])
        protected, body, signature = assignment["lease_token"].split(".")
        replacement = "B" if signature.startswith("A") else "A"
        tampered = f"{protected}.{body}.{replacement}{signature[1:]}"
        with self.assertRaisesRegex(ValueError, "signature"):
            verify_jws(tampered, self.plane.signer.jwks(), now=int(self.clock.now.timestamp()))

    def test_one_winner_concurrent_capacity_claim(self):
        slot = self.store.candidate()
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            try: return self.store.claim_slot(slot.executor_id, slot.slot_id, slot.version, self.clock(), timedelta(seconds=60))
            except StaleFence: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_stale_fence_and_expiry_are_rejected(self):
        assignment = self.plane.assign(self.request())
        with self.assertRaisesRegex(ControlPlaneError, "stale lease"):
            self.plane.renew(assignment["session_id"], 0)
        self.clock.now += timedelta(seconds=60)
        with self.assertRaisesRegex(ControlPlaneError, "expired"):
            self.plane.renew(assignment["session_id"], 1)

    def test_renew_issues_a_monotonic_new_lease(self):
        assignment = self.plane.assign(self.request())
        renewed = self.plane.renew(assignment["session_id"], 1)
        self.assertEqual(renewed["lease_sequence"], 2)
        self.assertNotEqual(renewed["lease_id"], assignment["lease_id"])

    def test_ninety_second_fake_clock_soak_renews_without_coordination(self):
        assignment = self.plane.assign(self.request())
        self.coord.available = False
        for _ in range(3):
            self.clock.now += timedelta(seconds=30)
            renewed = self.plane.renew(assignment["session_id"], 1)
            self.assertGreater(renewed["lease_sequence"], 1)
        session = self.store.get_session(assignment["session_id"])
        self.assertEqual(session.state, "ACTIVE")
        self.assertGreater(session.expires_at, self.clock.now)

    def test_expired_pool_is_reclaimed_before_pool_size_plus_one_assigns(self):
        self.plane.register_host("executor-test-002", "https://executor-2.internal:8443", ["slot-1"])
        self.plane.readiness("executor-test-002", "slot-1", True, digest("c"), digest("r"))
        first, second = self.plane.assign(self.request()), self.plane.assign(self.request())
        with self.assertRaisesRegex(ControlPlaneError, "no ready"):
            self.plane.assign(self.request())

        self.clock.now += timedelta(seconds=61)
        reclaimed = self.plane.reconcile()

        self.assertCountEqual(reclaimed, [first["session_id"], second["session_id"]])
        self.assertEqual([event[0] for event in self.runtime.events].count("release"), 2)
        replacement = self.plane.assign(self.request())
        self.assertIn(replacement["executor_id"], {"executor-test-001", "executor-test-002"})

    def test_idle_binding_is_reclaimed_and_executor_receives_release(self):
        assignment = self.plane.assign(self.request())
        session = self.store.get_session(assignment["session_id"])
        session.last_activity_at = self.clock.now - timedelta(seconds=31)

        reclaimed = self.plane.reconcile(idle_timeout=timedelta(seconds=30))

        self.assertEqual(reclaimed, [assignment["session_id"]])
        self.assertEqual(self.store.get_session(assignment["session_id"]).state, "INVALID")
        self.assertEqual(self.runtime.events[-1][0], "release")
        self.assertEqual(self.runtime.events[-1][2]["reason"], "idle_binding")

    def test_reconcile_fails_closed_when_durable_state_is_unavailable(self):
        self.store.available = False
        with self.assertRaisesRegex(ControlPlaneError, "PostgreSQL"):
            self.plane.reconcile()
        self.assertEqual(self.runtime.events, [])

    def test_repeated_live_create_does_not_claim_a_second_slot(self):
        request = self.request()
        first = self.plane.assign(request)
        with self.assertRaisesRegex(ControlPlaneError, "already has a live"):
            self.plane.assign(request)
        active_claims = [claim for claim in self.store.claims.values() if claim.state == "ACTIVE"]
        self.assertEqual([claim.claim_id for claim in active_claims], [self.store.get_session(first["session_id"]).claim_id])

    def test_concurrent_repeated_create_claims_one_slot(self):
        self.plane.register_host("executor-test-002", "https://executor-2.internal:8443", ["slot-1"])
        self.plane.readiness("executor-test-002", "slot-1", True, digest("c"), digest("r"))
        request = self.request()

        def create():
            try:
                return self.plane.assign(request)
            except ControlPlaneError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(result == "SESSION_ALREADY_BOUND" for result in results), 1)
        self.assertEqual(sum(claim.state == "ACTIVE" for claim in self.store.claims.values()), 1)

    def test_drain_rejects_new_assignment(self):
        self.plane.drain("executor-test-001", self.clock.now + timedelta(seconds=10))
        with self.assertRaisesRegex(ControlPlaneError, "no ready"):
            self.plane.assign(self.request())

    def test_rebind_invalidates_before_new_assignment(self):
        request = self.request()
        old = self.plane.assign(request)
        request["artifact"]["code_digest"] = digest("e")
        new = self.plane.rebind(request)
        kinds = [event[0] for event in self.runtime.events]
        self.assertEqual(kinds, ["assign", "release", "assign"])
        self.assertEqual(new["binding_epoch"], old["binding_epoch"] + 1)

    def test_concurrent_rebinds_receive_unique_binding_epochs(self):
        request = self.request()
        old = self.plane.assign(request)
        request["artifact"]["code_digest"] = digest("e")
        barrier = threading.Barrier(2)

        def rebind():
            barrier.wait()
            return self.plane.rebind(request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: rebind(), range(2)))

        self.assertEqual(sorted(result["binding_epoch"] for result in results), [old["binding_epoch"] + 1, old["binding_epoch"] + 2])
        self.assertEqual(self.store.get_session(request["session_id"]).binding_epoch, old["binding_epoch"] + 2)

    def test_redis_and_store_loss_fail_closed(self):
        self.coord.available = False
        with self.assertRaisesRegex(ControlPlaneError, "Redis"):
            self.plane.assign(self.request())
        self.coord.available = True
        self.store.available = False
        with self.assertRaisesRegex(ControlPlaneError, "PostgreSQL"):
            self.plane.assign(self.request())

    def test_runtime_readiness_failure_releases_claim(self):
        self.runtime.ready = False
        with self.assertRaisesRegex(ControlPlaneError, "did not confirm"):
            self.plane.assign(self.request())
        self.assertEqual([event[0] for event in self.runtime.events], ["assign", "release"])
        self.assertEqual(self.store.candidate().state, "READY")

    def test_response_has_no_service_credentials(self):
        assignment = self.plane.assign(self.request())
        forbidden = {"password", "secret", "redis", "postgres", "aws", "credential", "private_key"}
        self.assertFalse(forbidden & set(assignment))
        self.assertIn("lease_token", assignment)  # contract-required opaque lease, not a service credential

    def test_api_has_no_invocation_proxy(self):
        app = application(self.plane, shared_secret="test-control")
        captured = []
        body = b"{}"
        response = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/invoke", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "HTTP_X_INSTANT_CONTROL_SECRET": "test-control"}, lambda status, headers: captured.append(status))
        self.assertEqual(captured[0], "404 Not Found")
        self.assertEqual(json.loads(b"".join(response))["code"], "NOT_FOUND")

    def test_control_api_and_host_registration_fail_closed(self):
        app = application(self.plane, shared_secret="test-control")
        captured = []
        body = json.dumps(self.request()).encode()
        response = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/sessions", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}, lambda status, headers: captured.append(status))
        self.assertEqual("401 Unauthorized", captured[0])
        self.assertEqual("UNAUTHORIZED", json.loads(b"".join(response))["code"])
        with self.assertRaisesRegex(ControlPlaneError, "HTTPS"):
            self.plane.register_host("executor-evil-001", "http://169.254.169.254/latest", ["slot-1"])

    def test_production_host_registration_is_limited_to_the_pool_cidr_and_port(self):
        plane = ControlPlane(
            InMemoryStore(), Runtime(), LeaseSigner(b"a" * 32, "test-key"), Coordination(), self.clock,
            executor_cidrs=("10.20.0.0/16",), executor_port=8088,
        )
        registered = plane.register_host("executor-private-001", "https://10.20.14.9:8088", ["slot-1"])
        self.assertEqual("executor-private-001", registered[0]["executor_id"])
        with self.assertRaisesRegex(ControlPlaneError, "outside"):
            plane.register_host("executor-wrong-port", "https://10.20.14.9:8443", ["slot-1"])
        with self.assertRaisesRegex(ControlPlaneError, "outside"):
            plane.register_host("executor-wrong-cidr", "https://10.30.14.9:8088", ["slot-1"])
        with self.assertRaisesRegex(ControlPlaneError, "private task IP"):
            plane.register_host("executor-dns", "https://executor.internal:8088", ["slot-1"])

    def test_host_lifecycle_requires_its_own_secret(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            application(self.plane, app_control_secret="shared", host_lifecycle_secret="shared")
        app = application(self.plane, app_control_secret="app-control", host_lifecycle_secret="host-lifecycle")
        register = json.dumps({"executor_id": "executor-test-002", "endpoint": "https://executor-2.internal:8443", "slot_ids": ["slot-1"]}).encode()

        status, payload = self.call_api(app, "/v1/sessions", json.dumps(self.request()).encode(), "wrong-secret")
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload["code"], "UNAUTHORIZED")

        status, payload = self.call_api(app, "/v1/hosts/register", register, "app-control")
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload["code"], "UNAUTHORIZED")

        status, payload = self.call_api(app, "/v1/hosts/register", register, "wrong-secret")
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload["code"], "UNAUTHORIZED")

        status, _ = self.call_api(app, "/v1/hosts/register", register, "host-lifecycle")
        self.assertEqual(status, "201 Created")

    def test_api_rejects_malformed_non_object_and_unknown_fields(self):
        app = application(self.plane, app_control_secret="app-control", host_lifecycle_secret="host-lifecycle")
        bodies = [b"{", b"[]", b"{}", json.dumps({**self.request(), "unexpected": True}).encode()]
        for body in bodies:
            with self.subTest(body=body):
                status, payload = self.call_api(app, "/v1/sessions", body, "app-control")
                self.assertEqual(status, "400 Error")
                self.assertEqual(payload, {"code": "INVALID_REQUEST", "message": "invalid request body"})


if __name__ == "__main__":
    unittest.main()
