from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from executor.runtime.registration import (
    HostRegistrar,
    HostRegistrationConfig,
    RegistrationError,
    UrllibHttpsTransport,
    resolve_executor_endpoint,
    resolve_executor_id,
)


def digest(letter: str) -> str:
    return "sha256:" + letter * 64


class RecordingTransport:
    def __init__(self, failures: list[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures = list(failures or [])

    def post(self, route: str, body: dict[str, object]) -> None:
        self.calls.append((route, body))
        if self.failures and self.failures[0] == route:
            self.failures.pop(0)
            raise RegistrationError("temporary failure")


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ca_file = Path(self.temporary.name) / "ca.pem"
        self.ca_file.write_text("test CA", encoding="utf-8")

    def config(self, **overrides) -> HostRegistrationConfig:
        values = {
            "control_plane_url": "https://control.internal",
            "executor_id": "executor-test-001",
            "executor_endpoint": "https://executor.internal:8443",
            "slot_ids": ("slot-1", "slot-2"),
            "code_digest": digest("a"),
            "runtime_digest": digest("b"),
            "host_lifecycle_secret": "s" * 32,
            "ca_file": str(self.ca_file),
            "heartbeat_interval_seconds": 30,
            "retry_initial_seconds": 0.001,
            "retry_max_seconds": 0.002,
        }
        values.update(overrides)
        return HostRegistrationConfig(**values)

    def test_start_orders_registration_readiness_and_heartbeat_before_available(self) -> None:
        transport = RecordingTransport()
        registrar = HostRegistrar(self.config(), transport=transport)
        registrar.start()
        self.addCleanup(registrar.close)

        self.assertTrue(registrar.available)
        self.assertEqual(
            ["/v1/hosts/register", "/v1/hosts/readiness", "/v1/hosts/readiness", "/v1/hosts/heartbeat"],
            [route for route, _ in transport.calls],
        )
        self.assertEqual(["slot-1", "slot-2"], [body["slot_id"] for route, body in transport.calls if route.endswith("readiness")])

    def test_failed_registration_never_starts_or_marks_available(self) -> None:
        transport = RecordingTransport(["/v1/hosts/register"])
        registrar = HostRegistrar(self.config(), transport=transport)

        with self.assertRaisesRegex(RegistrationError, "temporary"):
            registrar.start()

        self.assertFalse(registrar.available)
        self.assertEqual(["/v1/hosts/register"], [route for route, _ in transport.calls])

    def test_heartbeat_failure_reregisters_with_bounded_backoff(self) -> None:
        transport = RecordingTransport()
        waits: list[float] = []
        registrar = HostRegistrar(self.config(), transport=transport, wait=lambda delay: waits.append(delay) or False)
        registrar._register_and_heartbeat()
        transport.failures = ["/v1/hosts/heartbeat", "/v1/hosts/register"]

        registrar.heartbeat_once()

        self.assertEqual([0.001, 0.002], waits)
        routes = [route for route, _ in transport.calls]
        self.assertEqual("/v1/hosts/heartbeat", routes[4])
        self.assertEqual(["/v1/hosts/register", "/v1/hosts/readiness", "/v1/hosts/readiness", "/v1/hosts/heartbeat"], routes[-4:])
        self.assertTrue(registrar.available)

    def test_non_registration_transport_failure_still_retries(self) -> None:
        class BrokenTransport:
            def post(self, _route, _body):
                raise OSError("network down")

        registrar = HostRegistrar(self.config(), transport=BrokenTransport())

        with self.assertRaisesRegex(RegistrationError, "lifecycle"):
            registrar.start()

        self.assertFalse(registrar.available)

    def test_shutdown_stops_the_heartbeat_thread_and_makes_host_unavailable(self) -> None:
        transport = RecordingTransport()
        registrar = HostRegistrar(self.config(heartbeat_interval_seconds=0.01), transport=transport)
        registrar.start()
        registrar.close()

        self.assertFalse(registrar.available)
        self.assertTrue(registrar._stop.is_set())
        self.assertFalse(registrar._thread.is_alive())

    def test_invalid_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.config(control_plane_url="http://control.internal")
        with self.assertRaisesRegex(ValueError, "32"):
            self.config(host_lifecycle_secret="short")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.config(slot_ids=("slot-1", "slot-1"))
        with self.assertRaisesRegex(ValueError, "client certificate"):
            self.config(client_cert_file=str(self.ca_file))

    def test_https_transport_uses_ca_optional_client_certificate_and_secret_header(self) -> None:
        cert_file = Path(self.temporary.name) / "client.pem"
        key_file = Path(self.temporary.name) / "client.key"
        cert_file.write_text("certificate", encoding="utf-8")
        key_file.write_text("key", encoding="utf-8")
        config = self.config(client_cert_file=str(cert_file), client_key_file=str(key_file))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b""
        with mock.patch("executor.runtime.registration.ssl.create_default_context") as context_factory, \
                mock.patch("executor.runtime.registration.urlopen", return_value=response) as open_url:
            context = context_factory.return_value
            transport = UrllibHttpsTransport(config)
            transport.post("/v1/hosts/heartbeat", {"executor_id": config.executor_id})

        context_factory.assert_called_once_with(__import__("ssl").Purpose.SERVER_AUTH, cafile=str(self.ca_file))
        context.load_cert_chain.assert_called_once_with(str(cert_file), str(key_file))
        request = open_url.call_args.args[0]
        self.assertEqual("https://control.internal/v1/hosts/heartbeat", request.full_url)
        self.assertEqual("s" * 32, request.get_header("X-instant-control-secret"))

    def test_executor_endpoint_comes_from_private_awsvpc_metadata(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "Networks": [{
                "NetworkMode": "awsvpc",
                "IPv4Addresses": ["10.20.14.9"],
            }],
        }).encode("utf-8")
        opener = mock.Mock(return_value=response)

        endpoint = resolve_executor_endpoint({
            "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/container-id",
            "LEAF_INSTANT_EXECUTOR_PORT": "8443",
        }, opener=opener)

        self.assertEqual("https://10.20.14.9:8443", endpoint)
        opener.assert_called_once_with("http://169.254.170.2/v4/container-id", timeout=2.0)

    def test_executor_id_comes_from_task_metadata(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "TaskARN": "arn:aws:ecs:us-east-1:807034087062:task/cluster/0123456789abcdef0123456789abcdef",
        }).encode()
        opener = mock.Mock(return_value=response)

        value = resolve_executor_id({
            "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/container-id",
        }, opener=opener)

        self.assertEqual("ecs-0123456789abcdef0123456789abcdef", value)
        opener.assert_called_once_with("http://169.254.170.2/v4/container-id/task", timeout=2.0)

    def test_executor_endpoint_discovery_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "trusted ECS metadata"):
            resolve_executor_endpoint({
                "ECS_CONTAINER_METADATA_URI_V4": "http://example.com/metadata",
            })
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "Networks": [{
                "NetworkMode": "awsvpc",
                "IPv4Addresses": ["10.20.14.9", "10.20.14.10"],
            }],
        }).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolve_executor_endpoint({
                "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/container-id",
            }, opener=mock.Mock(return_value=response))


if __name__ == "__main__":
    unittest.main()
