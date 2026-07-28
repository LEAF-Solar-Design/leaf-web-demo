from __future__ import annotations

import copy
import unittest

from executor.runtime.supervisor import WarmExecutorSupervisor
from executor.runtime.tests.helpers import EXECUTOR_ID, documents, keys, lease


COUNTER_SOURCE = "counter = [0]\ndef run(intake, params):\n    counter[0] += 1\n    return {'count': counter[0], 'drawing': intake['drawing_context']['drawing_id']}\n"


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1)

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
            self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1)
            self.supervisor.assign({key: other[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")})
            failed = self.invoke(other)
            self.assertEqual("TOOL_FAILED", failed["error"]["code"])

    def test_timeout_replaces_slot_and_batch_is_rejected(self) -> None:
        docs = self.assigned("def run(intake, params):\n while True:\n  pass\n")
        before = self.supervisor.process_ids()
        timed_out = self.invoke(docs)
        self.assertEqual("DEADLINE_EXCEEDED", timed_out["error"]["code"])
        self.assertNotEqual(before, self.supervisor.process_ids())
        self.assertEqual(1, self.supervisor.health()["bound_slots"])
        batch = copy.deepcopy(docs["invocation"])
        batch["batch"] = True
        rejected = self.supervisor.invoke(batch, "Bearer " + lease(batch))
        self.assertEqual("EXECUTION_CLASS_DENIED", rejected["error"]["code"])
