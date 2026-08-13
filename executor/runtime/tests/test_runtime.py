from __future__ import annotations

import copy
import json
import os
import signal
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from executor.runtime import child
from executor.runtime.environment import ENVIRONMENT_VARIABLE, UNSET_ENVIRONMENT_LABEL
from executor.registry import ArtifactReference, ImmutableArtifactRegistry, SignedArtifact
from executor.registry.artifacts import ImmutableArtifactRegistry as RegistryImplementation
from executor.runtime.ed25519 import sign
from executor.runtime.supervisor import (
    CAPACITY_ALARM_PERIOD_SECONDS,
    DEFAULT_CAPACITY_SAMPLE_SECONDS,
    MAX_CAPACITY_SAMPLE_SECONDS,
    CapacitySampler,
    WarmExecutorSupervisor,
    emit_runtime_event,
)
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


class HealthSnapshotConsistencyTests(unittest.TestCase):
    """`health()` runs unlocked from /health, /metrics AND the sampler timer.

    It used to count ready and bound in two separate passes. A concurrent
    assign between them reported ready=1, bound=1, total=1 -- more slots than
    exist -- and the sampler publishes that straight to CloudWatch. Reproduced
    under a forced switch between the passes before this was changed.
    """

    class FlipSlot:
        """A slot whose binding changes on every read, i.e. maximally racy."""

        def __init__(self) -> None:
            self._reads = 0
            self.process = SimpleNamespace(is_alive=lambda: True)

        @property
        def assignment(self):
            self._reads += 1
            return {"assignment_id": "a"} if self._reads % 2 else None

    def test_a_snapshot_can_never_report_more_slots_than_exist(self) -> None:
        supervisor = WarmExecutorSupervisor.__new__(WarmExecutorSupervisor)
        supervisor.executor_id = EXECUTOR_ID
        supervisor._slots = [self.FlipSlot() for _ in range(4)]
        for _ in range(20):
            state = supervisor.health()
            self.assertEqual(
                state["total_slots"],
                state["ready_slots"] + state["bound_slots"],
                f"impossible snapshot {state!r}: every slot is alive, so ready "
                "plus bound must equal total",
            )


class CapacityGaugeTests(unittest.TestCase):
    """The free-slot count has to leave this process to be alarmable.

    `health()` and `/metrics` are pull-only on :8088 and the executor task
    definition declares no task role, so nothing in AWS can read them.  A
    metric filter over the container log plane is the only channel, which
    makes the exact shape of this line a contract with the terraform root.
    """

    def setUp(self) -> None:
        self.events: list[dict] = []
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=2, trusted_development_fixtures=True,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
            runtime_event_sink=self.events.append,
        )

    def tearDown(self) -> None:
        self.supervisor.close()

    def samples(self) -> list[dict]:
        return [item for item in self.events if item["event_type"] == "capacity_sample"]

    def test_sample_reports_the_supervisors_own_live_slot_numbers(self) -> None:
        # A pinned payload would pass even if the record were a constant, so
        # bind BOTH states: the numbers must move when a slot is really bound.
        self.supervisor.sample_capacity()
        docs = documents(COUNTER_SOURCE)
        self.supervisor.assign({key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        self.supervisor.sample_capacity()

        idle, busy = self.samples()
        self.assertEqual((2, 0, 2), (idle["ready_slots"], idle["bound_slots"], idle["total_slots"]))
        self.assertEqual((1, 1, 2), (busy["ready_slots"], busy["bound_slots"], busy["total_slots"]))
        # ...and that they are health()'s numbers, not an independent count.
        health = self.supervisor.health()
        self.assertEqual(
            (health["ready_slots"], health["bound_slots"], health["total_slots"], health["state"]),
            (busy["ready_slots"], busy["bound_slots"], busy["total_slots"], busy["state"]),
        )

    def test_sample_carries_exactly_the_fields_the_metric_filter_selects(self) -> None:
        self.supervisor.sample_capacity()
        [record] = self.samples()
        # `env` is deliberately absent HERE. It is stamped by
        # `emit_runtime_event`, the single writer of the envelope, rather than
        # at this construction site, so that a record type added later cannot
        # omit it. This sink is an injected test double standing in for that
        # writer, so it sees the record before the stamp; the LINE is the
        # contract with the terraform root and is pinned in
        # test_sample_reaches_fd_two_in_the_runtime_envelope below.
        self.assertEqual(
            {"event_type", "executor_id", "state", "ready_slots",
             "bound_slots", "total_slots", "observed_at"},
            set(record),
        )
        # The filter reads $.record.ready_slots as a metric value, so it has to
        # be a JSON number.  A stringified count publishes nothing at all.
        for field in ("ready_slots", "bound_slots", "total_slots"):
            self.assertIsInstance(record[field], int)

    def _emitted_line(self, record: dict, environment: str | None = "staging") -> dict:
        """Push one record through the REAL emitter on fd 2 and parse the line."""
        patched = {} if environment is None else {ENVIRONMENT_VARIABLE: environment}
        read_fd, write_fd = os.pipe()
        saved = os.dup(2)
        try:
            os.dup2(write_fd, 2)
            with mock.patch.dict(os.environ, patched, clear=True):
                emit_runtime_event(record)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(write_fd)
        with os.fdopen(read_fd, "rb") as stream:
            raw = stream.read()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertLessEqual(len(raw), 4096)
        return json.loads(raw)

    def test_sample_reaches_fd_two_in_the_runtime_envelope(self) -> None:
        # The in-memory sink proves the record; only the real emitter proves
        # the LINE, and the line is what CloudWatch Logs actually matches.
        self.supervisor.sample_capacity()
        [record] = self.samples()
        line = self._emitted_line(record)
        self.assertEqual("leaf.instant.runtime", line["event"])
        self.assertEqual("capacity_sample", line["record"]["event_type"])
        self.assertEqual(record["ready_slots"], line["record"]["ready_slots"])

    def test_the_emitted_line_carries_the_env_the_filters_dimension_on(self) -> None:
        """`dimensions = { env = "$.record.env" }` on the CapacityAvailableSlots
        and RebindFailures filters selects THIS field. A line without it
        publishes no datapoint at all, so both alarms would read nothing while
        staying green -- the exact defect this namespace exists to remove.
        """
        self.supervisor.sample_capacity()
        [record] = self.samples()
        self.assertEqual("staging", self._emitted_line(record)["record"]["env"])

    def test_the_emitter_stamps_env_on_a_record_that_never_set_it(self) -> None:
        """The stamp is at the WRITER, so a record type nobody has written yet
        carries the dimension without its author doing anything.
        """
        line = self._emitted_line({"event_type": "some_future_record"})
        self.assertEqual("staging", line["record"]["env"])

    def test_a_caller_cannot_mislabel_which_deployment_emitted_the_line(self) -> None:
        line = self._emitted_line({"event_type": "capacity_sample", "env": "production"})
        self.assertEqual("staging", line["record"]["env"])

    def test_an_unset_variable_still_emits_the_field(self) -> None:
        """Never an omitted key: absence drops the datapoint, the sentinel does
        not. The loud half is the entrypoint refusing to boot (test_service).
        """
        self.supervisor.sample_capacity()
        [record] = self.samples()
        line = self._emitted_line(record, environment=None)
        self.assertEqual(UNSET_ENVIRONMENT_LABEL, line["record"]["env"])

    def test_sample_carries_no_tenant_or_lease_material(self) -> None:
        docs = documents(COUNTER_SOURCE)
        self.supervisor.assign({key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
        self.supervisor.sample_capacity()
        [record] = self.samples()
        encoded = str(record)
        for forbidden in (docs["assignment"]["lease_token"],
                          docs["assignment"]["tenant_id"],
                          docs["assignment"]["session_id"],
                          docs["drawing_context"]["geometry_ref"]):
            self.assertNotIn(forbidden, encoded)

    def test_a_sink_that_raises_cannot_kill_the_gauge(self) -> None:
        # The sampler is a bare thread: an escaping exception would end it and
        # the metric would go quiet, which is the failure this change removes.
        def explode(_record: dict) -> None:
            raise RuntimeError("telemetry backend is down")

        self.supervisor._runtime_event_sink = explode
        self.supervisor.sample_capacity()  # must not raise
        self.supervisor._runtime_event_sink = self.events.append
        self.supervisor.sample_capacity()
        self.assertEqual(1, len(self.samples()))


class CapacitySamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict] = []
        self.supervisor = WarmExecutorSupervisor(
            EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
            child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
            runtime_event_sink=self.events.append,
        )

    def tearDown(self) -> None:
        self.supervisor.close()

    def test_sampler_publishes_repeatedly_and_stops_on_request(self) -> None:
        sampler = CapacitySampler(self.supervisor, interval_seconds=0.02)
        sampler.start()
        deadline = time.monotonic() + 5
        while len(self.events) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        sampler.stop()
        published = len(self.events)

        self.assertGreaterEqual(published, 3)
        time.sleep(0.1)
        # A stopped sampler must go silent, or `stop()` is decorative and the
        # thread outlives the supervisor it reads.
        self.assertEqual(published, len(self.events))

    def test_sampler_publishes_immediately_rather_than_after_one_interval(self) -> None:
        # At the ceiling interval the second sample is half an alarm period
        # away, so without a boot sample the metric would be absent for the
        # first period after every deploy.
        sampler = CapacitySampler(self.supervisor, interval_seconds=MAX_CAPACITY_SAMPLE_SECONDS)
        sampler.start()
        deadline = time.monotonic() + 5
        while not self.events and time.monotonic() < deadline:
            time.sleep(0.01)
        sampler.stop()
        self.assertEqual(1, len(self.events))

    def test_interval_comes_from_the_environment_and_rejects_nonsense(self) -> None:
        self.assertEqual(30.0, CapacitySampler.from_environment(self.supervisor, {})._interval_seconds)
        configured = CapacitySampler.from_environment(
            self.supervisor, {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": "5"})
        self.assertEqual(5.0, configured._interval_seconds)
        with self.assertRaisesRegex(ValueError, "must be a number"):
            CapacitySampler.from_environment(
                self.supervisor, {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": "often"})
        with self.assertRaisesRegex(ValueError, "must be positive"):
            CapacitySampler.from_environment(
                self.supervisor, {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": "0"})

    def test_non_finite_and_oversized_intervals_are_rejected_at_construction(self) -> None:
        """`float()` accepts "nan" and "inf", and both slip past a bare <= 0.

        Confirmed by execution, not inferred: Event.wait(nan) returns instantly,
        so the sampler busy-loops and floods the log group; Thread.join(nan)
        then raises ValueError and Thread.join(inf) raises OverflowError, and
        either escapes stop() into serve_registered's cleanup. Rejecting the
        value at construction is the only place that costs nothing.
        """
        for value in ("nan", "-nan", "inf", "-inf", "Infinity"):
            with self.assertRaises(ValueError, msg=f"{value} was accepted"):
                CapacitySampler.from_environment(
                    self.supervisor, {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": value})
    def test_the_interval_ceiling_is_half_the_companion_alarms_period(self) -> None:
        """The ceiling must be HALF the alarm's period, not the period itself.

        A ceiling of, say, an hour would accept an interval that emits once and
        then leaves 59 consecutive one-minute periods with no datapoint at all,
        so the alarm can never accumulate consecutive breaching periods. The
        gauge would exist and never be alarmable, which is exactly the defect
        this feature removes -- so a too-loose ceiling silently reintroduces it.

        A ceiling equal to the period is ALSO too loose, which an earlier
        revision of this test missed by pinning the literal 60 instead of the
        property. Samples land on the wall clock; alarm periods land on fixed
        60-second boundaries. At exactly one period of spacing, any jitter puts
        two samples in one bucket and none in the next, and missing data counts
        as non-breaching -- so the empty period interrupts the consecutive-breach
        run. 60 is therefore in the rejected list below, not the accepted one.
        """
        self.assertEqual(CAPACITY_ALARM_PERIOD_SECONDS / 2, MAX_CAPACITY_SAMPLE_SECONDS)
        self.assertLessEqual(DEFAULT_CAPACITY_SAMPLE_SECONDS, MAX_CAPACITY_SAMPLE_SECONDS)

        at_ceiling = CapacitySampler.from_environment(
            self.supervisor,
            {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": repr(MAX_CAPACITY_SAMPLE_SECONDS)})
        self.assertEqual(MAX_CAPACITY_SAMPLE_SECONDS, at_ceiling._interval_seconds)
        for over in ("30.5", "31", "60", "3600"):
            with self.assertRaisesRegex(ValueError, "at most", msg=f"{over} was accepted"):
                CapacitySampler.from_environment(
                    self.supervisor, {"LEAF_INSTANT_CAPACITY_SAMPLE_SECONDS": over})

    def _spacing_at_ceiling(self, sample_costs: list[float], samples: int = 5) -> list[float]:
        """Drive `_run()` on a stubbed clock and return the spacing it produces.

        The clock is faked rather than slept through, so this measures the real
        loop at the real ceiling in milliseconds. `time.monotonic` is patched
        process-wide for the duration, which is safe only because `_run()` is
        called directly here -- no sampler thread is started, and the stubbed
        wait never blocks, so nothing else in the process is mid-measurement.
        """
        clock = [1000.0]
        stamps: list[float] = []
        sampler = CapacitySampler(self.supervisor, interval_seconds=MAX_CAPACITY_SAMPLE_SECONDS)

        def sample() -> None:
            stamps.append(clock[0])
            clock[0] += sample_costs[min(len(stamps) - 1, len(sample_costs) - 1)]
            if len(stamps) >= samples:
                sampler._stop.set()

        def wait(timeout: float | None = None) -> bool:
            self.assertIsNotNone(timeout, "the loop waited with no bound")
            self.assertGreater(timeout, 0, "the loop waited on a non-positive timeout")
            clock[0] += timeout
            return sampler._stop.is_set()

        self.supervisor.sample_capacity = sample
        sampler._stop.wait = wait
        with patch("time.monotonic", lambda: clock[0]):
            sampler._run()

        self.assertEqual(samples, len(stamps), "the loop did not run to completion")
        return [b - a for a, b in zip(stamps, stamps[1:])]

    def test_sample_spacing_is_the_interval_not_the_interval_plus_the_sample(self) -> None:
        """The property the ceiling exists for, measured instead of asserted.

        The previous revision pinned the ceiling's literal value and never
        checked spacing, which let this through: `_run()` sampled and THEN
        waited the whole interval, so real spacing was `interval + however long
        the sample took` and drifted later every cycle. The sample writes to
        fd 2 and that write can block, so the extra term is not negligible.
        Charging the sample four seconds here makes the old behaviour visible --
        it would space samples 34s apart at a 30s interval.

        The bar is the one the alarm actually needs: every 60-second period must
        contain a datapoint, so spacing must stay under a period with room to
        spare for jitter.
        """
        spacings = self._spacing_at_ceiling([4.0])

        self.assertEqual(
            [MAX_CAPACITY_SAMPLE_SECONDS] * len(spacings), spacings,
            "the sample's own duration leaked into the spacing")
        self.assertLess(
            max(spacings), CAPACITY_ALARM_PERIOD_SECONDS,
            "a 60-second alarm period can contain no datapoint")

    def test_recovering_from_one_slow_sample_does_not_fire_a_catch_up_burst(self) -> None:
        """The overrun branch must re-baseline, not repay the missed intervals.

        This needs a slow sample FOLLOWED BY fast ones; a uniformly slow sample
        cannot show the defect, because samples can never arrive faster than
        their own cost. One 100-second sample puts the deadline more than three
        intervals in the past. Without the re-baseline the loop then computes a
        negative remaining three times in a row and samples with no wait at all,
        emitting three lines at the same instant -- flooding fd 2 at exactly the
        moment fd 2 is the thing that was slow.
        """
        slow = MAX_CAPACITY_SAMPLE_SECONDS * 10 / 3
        spacings = self._spacing_at_ceiling([slow, 0.0])

        self.assertEqual(slow, spacings[0], "the slow sample itself sets the first gap")
        for spacing in spacings[1:]:
            self.assertGreaterEqual(
                spacing, MAX_CAPACITY_SAMPLE_SECONDS,
                f"recovery emitted at {spacing}s spacing: {spacings}")

    def test_stop_does_not_wait_for_the_configured_interval(self) -> None:
        """Shutdown must not be hostage to a telemetry write.

        The sink writes to fd 2. A backed-up fd 2 blocks that write, so joining
        for the CONFIGURED interval would stall every later teardown step for
        as long as the operator set it to. The join is a fixed short bound.
        """
        sampler = CapacitySampler(self.supervisor, interval_seconds=MAX_CAPACITY_SAMPLE_SECONDS)
        blocked = threading.Event()

        def wedge(_record: dict) -> None:
            blocked.set()
            time.sleep(30)

        self.supervisor._runtime_event_sink = wedge
        sampler.start()
        self.assertTrue(blocked.wait(5), "the sampler never reached the sink")
        started = time.monotonic()
        sampler.stop()
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            MAX_CAPACITY_SAMPLE_SECONDS / 2,
            f"stop() waited {elapsed:.1f}s on a wedged sink; it must not scale "
            "with the configured interval",
        )

    # ---- a failing sample must not take the gauge down with it ----------
    #
    # `sample_capacity()` guards its sink but not the `health()` call above it,
    # and `_run()` is a bare daemon thread. An escaping exception therefore ends
    # the thread for the life of the process, and NO alarm sees it: `capacity`
    # stays green (the task is alive), `registration` stays green (heartbeats
    # continue), and `capacity_slots` treats the resulting missing data as
    # notBreaching -- a deliberate choice, because when the whole executor dies
    # the other two already fire and a third would triple-page one outage.

    def _sampler_over_a_failing_health(self, sink) -> tuple["CapacitySampler", list]:
        attempts: list[float] = []

        def sample() -> None:
            attempts.append(time.monotonic())
            raise RuntimeError("health() is broken")

        self.supervisor.sample_capacity = sample
        return CapacitySampler(self.supervisor, interval_seconds=0.02,
                               runtime_event_sink=sink), attempts

    def _run_until_three_attempts(self, sampler, attempts: list) -> bool:
        sampler.start()
        deadline = time.monotonic() + 5
        while len(attempts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        alive = sampler._thread.is_alive()
        sampler.stop()
        return alive

    def test_a_failing_sample_does_not_end_the_sampler_thread(self) -> None:
        """The whole point: a raising sample must cost one sample, not all of them."""
        sampler, attempts = self._sampler_over_a_failing_health(lambda _record: None)
        alive = self._run_until_three_attempts(sampler, attempts)

        self.assertGreaterEqual(
            len(attempts), 3,
            "the sampler stopped attempting after a failing sample: the gauge is "
            "now silent for the life of the process and no alarm can see it")
        self.assertTrue(alive, "the sampler thread died on a failing sample")

    def test_a_failure_report_that_raises_cannot_kill_the_sampler(self) -> None:
        """The report runs BECAUSE something already raised.

        A sink that raises in turn would kill the thread from inside the handler
        that exists to keep it alive -- restoring the exact failure by way of the
        fix for it.
        """
        def explode(_record: dict) -> None:
            raise OSError("fd 2 is gone")

        sampler, attempts = self._sampler_over_a_failing_health(explode)
        alive = self._run_until_three_attempts(sampler, attempts)

        self.assertGreaterEqual(
            len(attempts), 3, "a raising failure-sink ended the sampler")
        self.assertTrue(alive, "the sampler thread died reporting its own failure")

    def _drive(self, outcomes: list[bool]) -> list[dict]:
        """Run `_run()` over a scripted sequence of sample outcomes on a fake clock.

        True = the sample returns, False = it raises. No thread and no sleeping,
        so the reporting schedule pinned below is measured off the real loop
        body rather than approximated by a timing race. Same stubbing discipline
        as `_spacing_at_ceiling`: `_run()` is called directly, no sampler thread
        exists, and the stubbed wait never blocks.
        """
        emitted: list[dict] = []
        clock = [1000.0]
        taken = [0]
        sampler = CapacitySampler(self.supervisor,
                                  interval_seconds=MAX_CAPACITY_SAMPLE_SECONDS,
                                  runtime_event_sink=emitted.append)

        def sample() -> None:
            index = taken[0]
            taken[0] += 1
            if taken[0] >= len(outcomes):
                sampler._stop.set()
            if not outcomes[index]:
                raise RuntimeError("health() is broken")

        def wait(timeout: float | None = None) -> bool:
            clock[0] += timeout
            return sampler._stop.is_set()

        self.supervisor.sample_capacity = sample
        sampler._stop.wait = wait
        with patch("time.monotonic", lambda: clock[0]):
            sampler._run()

        self.assertEqual(len(outcomes), taken[0], "the loop did not run the whole script")
        return emitted

    def test_a_failing_sample_is_reported_rather_than_silently_swallowed(self) -> None:
        """A swallowed failure publishes exactly as much as a dead thread: nothing.

        So the failure carries its own line, which is what a metric filter in
        the terraform root would key on to alarm on a running-but-blind sampler.
        """
        [record] = self._drive([False])

        self.assertEqual("capacity_sample_failed", record["event_type"])
        self.assertEqual(EXECUTOR_ID, record["executor_id"])
        self.assertEqual("RuntimeError", record["error_type"])
        self.assertEqual(1, record["consecutive_failures"])
        # The message is deliberately absent: `sample_capacity` promises its
        # record carries no tenant, session, assignment or source value, and an
        # uncontrolled exception string would put that promise back in play.
        self.assertEqual(
            {"event_type", "executor_id", "error_type",
             "consecutive_failures", "occurred_at"},
            set(record))
        self.assertNotIn("health() is broken", str(record))

    def test_a_permanently_broken_sample_reports_on_a_bounded_schedule(self) -> None:
        """Bound the OUTPUT, not the trigger.

        A permanently broken `health()` fails every interval forever. Reporting
        each one would emit two lines a minute at the default interval, and far
        more at a short one, into the same log group the gauge itself uses.
        Powers of two keep the first failure immediate -- the one an operator
        needs -- and cost log2(n) lines for a run of n instead of n.
        """
        records = self._drive([False] * 20)

        self.assertEqual([1, 2, 4, 8, 16],
                         [record["consecutive_failures"] for record in records])
        self.assertEqual({"capacity_sample_failed"},
                         {record["event_type"] for record in records})

    def test_recovery_closes_the_run_exactly_once_and_states_its_length(self) -> None:
        """Without this the newest failure line is unbounded in time.

        A healthy sampler is silent about its own health, so nothing else
        distinguishes a run that ended an hour ago from one still going.
        """
        records = self._drive([False, False, False, True, True])

        self.assertEqual(
            ["capacity_sample_failed", "capacity_sample_failed",
             "capacity_sample_recovered"],
            [record["event_type"] for record in records])
        # Three failures, reported at 1 and 2; the recovery states all three.
        self.assertEqual(3, records[-1]["recovered_after_failures"])

    def test_recovery_publishes_a_zero_not_the_run_length(self) -> None:
        """`consecutive_failures` must mean the same thing on both event types.

        A metric filter selecting `$.record.consecutive_failures` from both is
        the natural way to alarm on a blind sampler, and it needs the count to
        climb while the sampler is failing and to hit 0 the moment it recovers,
        or the alarm can never clear. An earlier revision published the RUN
        LENGTH under that name, so the one event meaning "the gauge is back"
        carried the largest breaching value of the whole incident.
        """
        *_, recovery = self._drive([False, False, False, True])

        self.assertEqual("capacity_sample_recovered", recovery["event_type"])
        self.assertEqual(0, recovery["consecutive_failures"])
        self.assertEqual(3, recovery["recovered_after_failures"])
        self.assertEqual(
            {"event_type", "executor_id", "consecutive_failures",
             "recovered_after_failures", "occurred_at"},
            set(recovery))

    def test_the_failure_run_restarts_after_a_recovery(self) -> None:
        # A counter that recovery did not reset would make the second run's
        # first failure read as the second failure of one long run, and would
        # skip reporting it entirely once the counter passed a power of two.
        records = self._drive([False, True, False])

        self.assertEqual(
            ["capacity_sample_failed", "capacity_sample_recovered",
             "capacity_sample_failed"],
            [record["event_type"] for record in records])
        self.assertEqual(1, records[0]["consecutive_failures"])
        self.assertEqual(1, records[1]["recovered_after_failures"])
        # The second run's first failure is a 1, not a 2.
        self.assertEqual(1, records[2]["consecutive_failures"])

    def test_a_healthy_sampler_says_nothing_about_itself(self) -> None:
        # Recovery must fire only after a failure. A line every interval would
        # double the gauge's own volume and say nothing.
        self.assertEqual([], self._drive([True, True, True]))
