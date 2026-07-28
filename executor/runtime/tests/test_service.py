from __future__ import annotations

import http.client
import json
import threading
import unittest

from executor.runtime.service import make_server
from executor.runtime.supervisor import WarmExecutorSupervisor
from executor.runtime.tests.helpers import EXECUTOR_ID, documents, keys, lease


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1)
        self.server = make_server(("127.0.0.1", 0), self.supervisor, "test-runtime-control")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.supervisor.close()

    def request(self, path: str, body: dict, headers: dict | None = None):
        all_headers = {"Content-Type": "application/json"}
        all_headers.update(headers or {})
        self.connection.request("POST", path, json.dumps(body), all_headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def test_keep_alive_control_and_direct_invoke(self) -> None:
        docs = documents("def run(intake, params):\n return {'ok': params['layer']}\n")
        assignment = {key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")}
        status, denied = self.request("/v1/control/assign", assignment)
        self.assertEqual(401, status)
        self.assertIn("authentication", denied["error"])
        status, ready = self.request("/v1/control/assign", assignment, {
            "X-Instant-Runtime-Control-Secret": "test-runtime-control",
        })
        self.assertEqual(200, status)
        self.assertEqual("ready", ready["state"])
        status, result = self.request("/v1/invoke", docs["invocation"], {"Authorization": "Bearer " + lease(docs["invocation"])})
        self.assertEqual(200, status)
        self.assertEqual("Panels", result["result"]["ok"])
        self.connection.request("GET", "/health")
        self.assertEqual(200, self.connection.getresponse().status)
