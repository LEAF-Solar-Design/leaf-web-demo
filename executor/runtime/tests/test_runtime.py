from __future__ import annotations

import copy
import os
import signal
import time
import unittest
from unittest.mock import patch

from executor.runtime import child
from executor.registry import ArtifactReference, ImmutableArtifactRegistry, SignedArtifact
from executor.registry.artifacts import ImmutableArtifactRegistry as RegistryImplementation
from executor.runtime.ed25519 import sign
from executor.runtime.supervisor import WarmExecutorSupervisor
from executor.runtime.tests.helpers import CHILD_LOAD_WINDOW_SECONDS, EXECUTOR_ID, documents, keys, lease


COUNTER_SOURCE = "counter = [0]\ndef run(intake, params):\n    counter[0] += 1\n    return {'count': counter[0], 'drawing': intake['drawing_context']['drawing_id']}\n"


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)

    def tearDown(self) -> None:
        self.supervisor.close()

    def assigned(self, source: str = COUNTER_SOURCE):
        docs = documents(source)
        self.supervisor.assign({key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        return docs

    def invoke(self, docs):
        return self.supervisor.invoke(docs["invocation"], "Bearer " + lease(docs["invocation"]))

    def test_warm_pid_stays_stable_and_source_loads_once(self) -> None:
        docs = self.assigned()
        before = self.supervisor.process_ids()
        first = self.invoke(docs)
        second_body = copy.deepcopy(docs["invocation"])
        second_body["invocation_id"] = "11111111-1111-4111-8111-111111111111"
        second = self.supervisor.invoke(second_body, "Bearer " + lease(second_body))
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(1, first["result"]["count"])
        self.assertEqual(2, second["result"]["count"])
        self.assertEqual(before, self.supervisor.process_ids())

    def test_accounting_emits_ordered_payload_free_events_once_for_a_replay(self) -> None:
        class Emitter:
            def __init__(self) -> None:
                self.records = []
            def emit(self, record) -> None:
                self.records.append(record)

        emitter = Emitter()
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1,
            trusted_development_fixtures=True, accounting_emitter=emitter,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
        )
        docs = self.assigned()
        first = self.invoke(docs)
        replay = self.invoke(docs)
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(first, replay)
        self.assertEqual([item["state"] for item in emitter.records], ["accepted", "started", "terminal"])
        encoded = str(emitter.records).lower()
        self.assertNotIn("params", encoded)
        self.assertNotIn("drawing_context", encoded)
        self.assertEqual("succeeded", emitter.records[-1]["outcome"])
        self.assertEqual(0, emitter.records[-1]["memory_peak_bytes"])

    def test_process_high_water_memory_is_never_billed_as_per_invocation_usage(self) -> None:
        class Emitter:
            def __init__(self) -> None:
                self.records = []
            def emit(self, record) -> None:
                self.records.append(record)

        emitter = Emitter()
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1,
            trusted_development_fixtures=True, accounting_emitter=emitter,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
        )
        docs = self.assigned(
            "def run(intake, params):\n"
            " junk = [0] * params['size']\n"
            " return {'size': len(junk)}\n"
        )
        docs["invocation"]["params"] = {"size": 100_000}
        self.assertEqual("succeeded", self.invoke(docs)["status"])
        low = copy.deepcopy(docs["invocation"])
        low["invocation_id"] = "11111111-1111-4111-8111-111111111111"
        low["params"] = {"size": 1}
        self.assertEqual("succeeded", self.supervisor.invoke(low, "Bearer " + lease(low))["status"])
        terminals = [item for item in emitter.records if item["state"] == "terminal"]
        self.assertEqual([0, 0], [item["memory_peak_bytes"] for item in terminals])

    def test_normal_mode_resolves_signed_registry_bytes_and_rejects_inline_source(self) -> None:
        source = "def run(intake, params):\n return {'registry': True}\n"
        docs = documents(source)
        assignment = docs["assignment"]
        reference = ArtifactReference(
            assignment["tenant_id"], assignment["effective_catalog_digest"],
            assignment["artifact_digest"], assignment["code_digest"],
        )
        unsigned = SignedArtifact(reference, source.encode("utf-8"), "registry-key", b"")
        artifact = SignedArtifact(
            reference, source.encode("utf-8"), "registry-key",
            sign(bytes(range(32)), RegistryImplementation._signed_payload(unsigned)),
        )
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1,
            artifact_registry=ImmutableArtifactRegistry((artifact,), {"registry-key": keys()["test-key"]}),
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
        )
        normal_request = {key: docs[key] for key in ("assignment", "code_load", "catalog", "drawing_context")}
        self.supervisor.assign(normal_request)
        self.assertEqual("succeeded", self.invoke(docs)["status"])
        with self.assertRaisesRegex(ValueError, "inline source") as raised:
            self.supervisor.assign({**normal_request, "source": source})
        self.assertEqual("UNTRUSTED_SOURCE_REJECTED", raised.exception.code)

    def test_replay_and_conflict_do_not_execute_again(self) -> None:
        docs = self.assigned()
        first = self.invoke(docs)
        replay = self.invoke(docs)
        changed = copy.deepcopy(docs["invocation"])
        changed["params"] = {"layer": "Other"}
        conflict = self.supervisor.invoke(changed, "Bearer " + lease(changed))
        self.assertEqual(first, replay)
        self.assertEqual("INVOCATION_CONFLICT", conflict["error"]["code"])

    def test_stale_lease_and_wrong_digest_fail_before_code(self) -> None:
        docs = self.assigned()
        stale = self.supervisor.invoke(docs["invocation"], "Bearer " + lease(docs["invocation"], expires_in=-1))
        bad = self.supervisor.invoke(docs["invocation"], "Bearer " + lease(docs["invocation"])[:-1] + "x")
        wrong = copy.deepcopy(docs["invocation"])
        wrong["invocation_id"] = "22222222-2222-4222-8222-222222222222"
        wrong["code_digest"] = "sha256:" + "f" * 64
        denied = self.supervisor.invoke(wrong, "Bearer " + lease(wrong))
        self.assertEqual("SESSION_EXPIRED", stale["error"]["code"])
        self.assertEqual("SESSION_EXPIRED", bad["error"]["code"])
        self.assertEqual("SESSION_BINDING_MISMATCH", denied["error"]["code"])

    def test_secret_network_and_credential_paths_fail(self) -> None:
        docs = self.assigned()
        secret = copy.deepcopy(docs["invocation"])
        secret["params"] = {"api_key": "not-allowed"}
        response = self.supervisor.invoke(secret, "Bearer " + lease(secret))
        self.assertEqual("FORBIDDEN_PAYLOAD_FIELD", response["error"]["code"])
        for source in ("def run(intake, params):\n import socket\n return {}\n", "def run(intake, params):\n return {'x': open('credential') }\n"):
            other = self.assigned(source) if False else documents(source)
            # Use a fresh warm pool because this test has one fixed slot.
            self.supervisor.close()
            self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
            self.supervisor.assign({key: other[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
            failed = self.invoke(other)
            self.assertEqual("TOOL_FAILED", failed["error"]["code"])

    def test_timeout_replaces_slot_and_batch_is_rejected(self) -> None:
        docs = documents("def run(intake, params):\n while True:\n  pass\n")
        # This test PROVES the wall timeout, so it pins the tight wall the
        # generous shared fixture no longer carries; CPU stays high so the
        # POSIX CPU limit cannot preempt the wall expiry under test.
        docs["catalog"]["limits"]["max_wall_ms"] = 200
        docs["catalog"]["limits"]["max_cpu_ms"] = 30_000
        self.supervisor.assign({key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        before = self.supervisor.process_ids()
        timed_out = self.invoke(docs)
        self.assertEqual("DEADLINE_EXCEEDED", timed_out["error"]["code"])
        self.assertNotEqual(before, self.supervisor.process_ids())
        self.assertEqual(1, self.supervisor.health()["bound_slots"])
        batch = copy.deepcopy(docs["invocation"])
        batch["batch"] = True
        rejected = self.supervisor.invoke(batch, "Bearer " + lease(batch))
        self.assertEqual("EXECUTION_CLASS_DENIED", rejected["error"]["code"])

    def test_child_enforces_output_and_tool_call_limits(self) -> None:
        output_docs = documents("def run(intake, params):\n return {'data': 'x' * 128}\n")
        output_docs["catalog"]["limits"]["max_output_bytes"] = 32
        self.supervisor.assign({key: output_docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        output = self.invoke(output_docs)
        self.assertEqual("TOOL_FAILED", output["error"]["code"])
        self.assertIn("output exceeds", output["error"]["message"])

        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
        calls_docs = documents("def run(intake, params):\n intake['tool_call']()\n intake['tool_call']()\n return {}\n")
        calls_docs["catalog"]["limits"]["max_tool_calls"] = 1
        self.supervisor.assign({key: calls_docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        calls = self.invoke(calls_docs)
        self.assertEqual("TOOL_FAILED", calls["error"]["code"])
        self.assertIn("tool call count exceeds", calls["error"]["message"])

    def test_child_failure_replaces_only_the_failed_slot(self) -> None:
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=2, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
        failing = documents("def run(intake, params):\n raise MemoryError()\n")
        healthy = documents("def run(intake, params):\n return {'healthy': True}\n")
        self.supervisor.assign({key: failing[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        self.supervisor.assign({key: healthy[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        before = self.supervisor.process_ids()
        failed = self.invoke(failing)
        after = self.supervisor.process_ids()
        healthy_response = self.invoke(healthy)
        self.assertEqual("TOOL_FAILED", failed["error"]["code"])
        self.assertNotEqual(before["slot-1"], after["slot-1"])
        self.assertEqual(before["slot-2"], after["slot-2"])
        self.assertEqual("succeeded", healthy_response["status"])

    def test_idempotency_cache_has_a_ttl_and_entry_cap(self) -> None:
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1, idempotency_ttl_seconds=0.02, idempotency_max_entries=2,
            trusted_development_fixtures=True,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
        )
        docs = self.assigned()
        first = self.invoke(docs)
        time.sleep(0.03)
        after_ttl = self.invoke(docs)
        self.assertEqual(1, first["result"]["count"])
        self.assertEqual(2, after_ttl["result"]["count"])
        for invocation_id in ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"):
            current = copy.deepcopy(docs["invocation"])
            current["invocation_id"] = invocation_id
            self.supervisor.invoke(current, "Bearer " + lease(current))
        self.assertLessEqual(len(self.supervisor._idempotency), 2)
        stale = self.supervisor.invoke(docs["invocation"], "Bearer " + lease(docs["invocation"], expires_in=-1))
        self.assertEqual("SESSION_EXPIRED", stale["error"]["code"])

    def test_resource_limit_fallback_is_platform_safe(self) -> None:
        class FakeResource:
            RLIM_INFINITY = -1
            RLIMIT_AS = "as"
            RLIMIT_NPROC = "nproc"
            RLIMIT_FSIZE = "fsize"
            RLIMIT_CPU = "cpu"

            def __init__(self) -> None:
                self.applied: list[tuple[str, tuple[int, int]]] = []

            @staticmethod
            def getrlimit(_limit: str) -> tuple[int, int]:
                return (-1, -1)

            def setrlimit(self, limit: str, values: tuple[int, int]) -> None:
                self.applied.append((limit, values))

        original = child.resource
        fake = FakeResource()
        try:
            child.resource = fake
            child._apply_resource_limits({"max_memory_mb": 32, "max_output_bytes": 64, "max_cpu_ms": 1, "max_tool_calls": 0})
        finally:
            child.resource = original
        self.assertEqual({"as", "nproc", "fsize", "cpu"}, {name for name, _values in fake.applied})

    @unittest.skipUnless(os.name == "posix", "POSIX resource limits are unavailable on Windows")
    def test_linux_memory_limit_replaces_only_the_allocation_slot(self) -> None:
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=2, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
        allocation = documents("def run(intake, params):\n return {'data': 'x' * (64 * 1024 * 1024)}\n")
        allocation["catalog"]["limits"]["max_memory_mb"] = 32
        healthy = documents("def run(intake, params):\n return {'healthy': True}\n")
        self.supervisor.assign({key: allocation[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        self.supervisor.assign({key: healthy[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        before = self.supervisor.process_ids()
        failed = self.invoke(allocation)
        after = self.supervisor.process_ids()
        self.assertEqual("TOOL_FAILED", failed["error"]["code"])
        self.assertNotEqual(before["slot-1"], after["slot-1"])
        self.assertEqual(before["slot-2"], after["slot-2"])
        self.assertEqual("succeeded", self.invoke(healthy)["status"])

    @unittest.skipUnless(os.name == "posix" and hasattr(signal, "ITIMER_PROF"), "POSIX CPU timers are unavailable")
    def test_linux_cpu_limit_replaces_only_the_busy_slot(self) -> None:
        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=2, trusted_development_fixtures=True,
                                                child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
        busy = documents("def run(intake, params):\n while True:\n  pass\n")
        busy["catalog"]["limits"]["max_wall_ms"] = 30_000
        busy["catalog"]["limits"]["max_cpu_ms"] = 50
        healthy = documents("def run(intake, params):\n return {'healthy': True}\n")
        self.supervisor.assign({key: busy[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        self.supervisor.assign({key: healthy[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        before = self.supervisor.process_ids()
        failed = self.invoke(busy)
        after = self.supervisor.process_ids()
        self.assertEqual("TOOL_FAILED", failed["error"]["code"])
        self.assertNotEqual(before["slot-1"], after["slot-1"])
        self.assertEqual(before["slot-2"], after["slot-2"])
        self.assertEqual("succeeded", self.invoke(healthy)["status"])


class SlotRebindObservabilityTests(unittest.TestCase):
    """A rebind failing after the caller was answered must never be silent.

    invoke() replaces a slot on DEADLINE_EXCEEDED and on a child that died
    mid-invocation, then rebinds the assignment synchronously.  Both are
    ordinary production events.  If that rebind fails the session is gone for
    good, so the loss has to reach the container log plane.
    """

    def setUp(self) -> None:
        self.events: list[dict] = []
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
            runtime_event_sink=self.events.append,
        )

    def tearDown(self) -> None:
        self.supervisor.close()

    def assigned(self, source: str = COUNTER_SOURCE):
        docs = documents(source)
        self.supervisor.assign({key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        return docs

    def failures(self) -> list[dict]:
        return [item for item in self.events if item["event_type"] == "slot_rebind_failed"]

    def test_rebind_timeout_is_reported_instead_of_silently_dropping_the_binding(self) -> None:
        docs = self.assigned()
        assignment_id = docs["assignment"]["assignment_id"]
        slot = self.supervisor._find_assignment(assignment_id)
        self.assertEqual(1, self.supervisor.health()["bound_slots"])

        def refuse(_slot, _timeout):
            raise TimeoutError("child did not answer")

        with patch.object(self.supervisor, "_receive", refuse):
            self.supervisor._replace(slot, restore=True)

        # The binding really is lost; that unrecoverable state is the thing
        # being reported, not a transient the caller could retry through.
        self.assertEqual(0, self.supervisor.health()["bound_slots"])
        self.assertIsNone(self.supervisor._find_assignment(assignment_id))
        [record] = self.failures()
        self.assertEqual("TimeoutError", record["reason"])
        self.assertEqual(assignment_id, record["assignment_id"])
        self.assertEqual(docs["assignment"]["tenant_id"], record["tenant_id"])
        self.assertEqual("slot-1", record["slot_id"])
        self.assertIn("instant_executor_rebind_failures_total 1", self.supervisor.metrics())

    def test_rebind_rejected_by_the_child_is_reported_as_its_own_reason(self) -> None:
        docs = self.assigned()
        assignment_id = docs["assignment"]["assignment_id"]
        slot = self.supervisor._find_assignment(assignment_id)
        # The replacement child re-runs the captured source.  Make that load
        # fail the way a poisoned artifact would, with the real transport.
        slot.source = "raise ValueError('artifact no longer loads')"

        self.supervisor._replace(slot, restore=True)

        self.assertEqual(0, self.supervisor.health()["bound_slots"])
        [record] = self.failures()
        self.assertEqual("source_rejected", record["reason"])
        self.assertEqual(assignment_id, record["assignment_id"])
        self.assertIn("instant_executor_rebind_failures_total 1", self.supervisor.metrics())

    def test_a_successful_rebind_keeps_the_binding_and_reports_nothing(self) -> None:
        docs = self.assigned()
        slot = self.supervisor._find_assignment(docs["assignment"]["assignment_id"])
        self.supervisor._replace(slot, restore=True)
        self.assertEqual(1, self.supervisor.health()["bound_slots"])
        self.assertEqual([], self.failures())
        self.assertIn("instant_executor_rebind_failures_total 0", self.supervisor.metrics())

    def test_a_raising_sink_cannot_break_the_invocation_it_observes(self) -> None:
        """Telemetry must never cost the caller its response.

        _record_rebind_failure runs inside invoke()'s own failure handling, so
        a sink exception escaping through _replace() would lose the caller's
        DEADLINE_EXCEEDED answer and skip terminal accounting and idempotency
        recording.  The sink is injectable, so this is reachable by config.
        """
        def hostile(_record):
            raise RuntimeError("telemetry backend is down")

        self.supervisor.close()
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
            runtime_event_sink=hostile,
        )
        docs = self.assigned()

        # Patch AFTER assign, so the assign path uses the real transport. Now
        # every _receive fails: invoke times out, then its rebind fails too,
        # which is exactly the state that reaches the hostile sink.
        def refuse(_slot, _timeout):
            raise TimeoutError("child did not answer")

        with patch.object(self.supervisor, "_receive", refuse):
            response = self.supervisor.invoke(
                docs["invocation"], "Bearer " + lease(docs["invocation"]))

        self.assertEqual("failed", response["status"])
        self.assertEqual("DEADLINE_EXCEEDED", response["error"]["code"])
        self.assertEqual("unknown", response["error"]["execution_disposition"])
        self.assertEqual(0, self.supervisor.health()["bound_slots"])
        # The sink blew up, but the failure is still visible on /metrics.
        self.assertIn("instant_executor_rebind_failures_total 1", self.supervisor.metrics())

    def test_the_reported_record_carries_no_payload(self) -> None:
        """Same discipline the accounting emitter is held to: identifiers only,
        never tool input, source, or drawing geometry."""
        docs = self.assigned()
        slot = self.supervisor._find_assignment(docs["assignment"]["assignment_id"])
        poisoned = "raise ValueError('artifact no longer loads')"
        slot.source = poisoned
        self.supervisor._replace(slot, restore=True)

        [record] = self.failures()
        self.assertEqual(
            {"event_type", "executor_id", "slot_id", "assignment_id", "tenant_id",
             "session_id", "reason", "child_load_timeout_seconds", "occurred_at"},
            set(record),
        )
        # Assert the real secret-bearing values are absent, not merely that
        # certain words are: "source" legitimately appears inside the
        # source_rejected reason, so a word scan would be a false positive.
        encoded = str(record)
        for forbidden in (poisoned,
                          docs["assignment"]["lease_token"],
                          docs["drawing_context"]["geometry_ref"],
                          docs["drawing_context"]["content_digest"],
                          str(docs["invocation"]["params"])):
            self.assertNotIn(forbidden, encoded)
