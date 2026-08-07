from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock
from unittest.mock import Mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from executor.control_plane.runtime import HttpRuntimeClient
from executor.runtime.service import (
    RuntimeTlsConfig,
    configure_host_registrar,
    main,
    make_server,
    scrub_child_environment,
    serve_registered,
)
from executor.runtime.supervisor import WarmExecutorSupervisor
from executor.runtime.tests.helpers import CHILD_LOAD_WINDOW_SECONDS, EXECUTOR_ID, documents, keys, lease


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supervisor = WarmExecutorSupervisor(EXECUTOR_ID, keys(), pool_size=1, trusted_development_fixtures=True,
                                                 child_load_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS)
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

    def test_non_loopback_listener_requires_mutual_tls(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutual TLS"):
            make_server(("0.0.0.0", 0), self.supervisor, "test-runtime-control")

    def test_mutual_tls_rejects_untrusted_client_without_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            certificates = _certificates(Path(temporary))
            tls_server = make_server(
                ("127.0.0.1", 0), self.supervisor, "test-runtime-control",
                tls_config=RuntimeTlsConfig(certificates["server_cert"], certificates["server_key"], certificates["ca_cert"]),
            )
            tls_thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
            tls_thread.start()
            try:
                untrusted_context = ssl.create_default_context(cafile=certificates["ca_cert"])
                untrusted_context.load_cert_chain(certificates["untrusted_client_cert"], certificates["untrusted_client_key"])
                connection = http.client.HTTPSConnection("localhost", tls_server.server_port, context=untrusted_context)
                # Windows reports a rejected client certificate as a reset;
                # OpenSSL-based platforms surface the TLS alert directly.
                with self.assertRaises((ssl.SSLError, ConnectionResetError)):
                    connection.request("POST", "/v1/control/assign", json.dumps({}), {"Content-Type": "application/json"})
                    connection.getresponse()
                connection.close()
                self.assertEqual(0, self.supervisor.health()["bound_slots"])

                client = HttpRuntimeClient(
                    "test-runtime-control", ca_file=certificates["ca_cert"],
                    client_cert_file=certificates["trusted_client_cert"], client_key_file=certificates["trusted_client_key"],
                    tls_server_name="localhost",
                    request_timeout_seconds=CHILD_LOAD_WINDOW_SECONDS,
                )
                docs = documents("def run(intake, params):\n return {'ok': True}\n")
                assignment = {key: docs[key] for key in ("assignment", "code_load", "catalog", "source", "drawing_context")}
                ready = client.assign(f"https://127.0.0.1:{tls_server.server_port}", assignment)
                self.assertEqual("ready", ready["state"])
                self.assertEqual(1, self.supervisor.health()["bound_slots"])
            finally:
                tls_server.shutdown()
                tls_server.server_close()
                tls_thread.join(timeout=2)

    def test_https_client_requires_ca_and_certificate(self) -> None:
        with self.assertRaisesRegex(ValueError, "CA file, client certificate, and client key"):
            HttpRuntimeClient("test-runtime-control").assign("https://localhost:8443", {})

    def test_registration_must_match_the_prestarted_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ca_file = Path(temporary) / "ca.pem"
            ca_file.write_text("test-ca", encoding="utf-8")
            environment = {
                "LEAF_INSTANT_CONTROL_PLANE_URL": "https://control.internal:8080",
                "LEAF_INSTANT_EXECUTOR_ID": EXECUTOR_ID,
                "LEAF_INSTANT_EXECUTOR_ENDPOINT": "https://10.20.1.24:8088",
                "LEAF_INSTANT_EXECUTOR_SLOT_IDS": "slot-1",
                "LEAF_INSTANT_EXECUTOR_CODE_DIGEST": "sha256:" + "0" * 64,
                "LEAF_INSTANT_EXECUTOR_RUNTIME_DIGEST": "sha256:" + "1" * 64,
                "LEAF_INSTANT_HOST_LIFECYCLE_SECRET": "r" * 32,
                "LEAF_INSTANT_CONTROL_CA_FILE": str(ca_file),
            }
            captured = []
            registrar = configure_host_registrar(
                self.supervisor,
                environ=environment,
                registrar_factory=lambda config: captured.append(config) or Mock(),
            )
            self.assertIsNotNone(registrar)
            self.assertEqual(("slot-1",), captured[0].slot_ids)

            environment["LEAF_INSTANT_EXECUTOR_SLOT_IDS"] = "slot-2"
            with self.assertRaisesRegex(ValueError, "slot IDs"):
                configure_host_registrar(self.supervisor, environ=environment)
            environment["LEAF_INSTANT_EXECUTOR_SLOT_IDS"] = "slot-1"
            environment["LEAF_INSTANT_EXECUTOR_ID"] = "another-host"
            with self.assertRaisesRegex(ValueError, "executor ID"):
                configure_host_registrar(self.supervisor, environ=environment)

    def test_registration_precedes_serving_and_closes_first(self) -> None:
        events: list[str] = []
        server = Mock()
        supervisor = Mock()
        registrar = Mock()
        registrar.start.side_effect = lambda: events.append("register")
        server.serve_forever.side_effect = lambda: events.append("serve")
        registrar.close.side_effect = lambda: events.append("registrar-close")
        server.server_close.side_effect = lambda: events.append("server-close")
        supervisor.close.side_effect = lambda: events.append("supervisor-close")

        serve_registered(server, supervisor, registrar)

        self.assertEqual(
            ["register", "serve", "registrar-close", "server-close", "supervisor-close"],
            events,
        )

    def test_capacity_sampler_brackets_the_whole_advertised_window(self) -> None:
        # The gauge must already be publishing before this host is advertised
        # as usable, and must stop before the pool it reads is torn down.
        events: list[str] = []
        server, supervisor, registrar, sampler = Mock(), Mock(), Mock(), Mock()
        sampler.start.side_effect = lambda: events.append("sample-start")
        registrar.start.side_effect = lambda: events.append("register")
        server.serve_forever.side_effect = lambda: events.append("serve")
        sampler.stop.side_effect = lambda: events.append("sample-stop")
        registrar.close.side_effect = lambda: events.append("registrar-close")
        server.server_close.side_effect = lambda: events.append("server-close")
        supervisor.close.side_effect = lambda: events.append("supervisor-close")

        serve_registered(server, supervisor, registrar, sampler)

        self.assertEqual(
            ["sample-start", "register", "serve",
             "sample-stop", "registrar-close", "server-close", "supervisor-close"],
            events,
        )

    def test_a_registration_that_never_serves_still_stops_the_sampler(self) -> None:
        server, supervisor, registrar, sampler = Mock(), Mock(), Mock(), Mock()
        registrar.start.side_effect = RuntimeError("control plane refused the host")

        with self.assertRaisesRegex(RuntimeError, "control plane refused"):
            serve_registered(server, supervisor, registrar, sampler)

        sampler.stop.assert_called_once()
        server.serve_forever.assert_not_called()

    def test_a_sampler_that_fails_to_stop_cannot_skip_teardown(self) -> None:
        """Flat cleanup let one raising stop() strand the whole shutdown.

        The sampler's own validation and its join can both raise, and with a
        flat finally block that exception left the registrar registered, the
        listening socket open, and every child process alive.
        """
        events: list[str] = []
        server, supervisor, registrar, sampler = Mock(), Mock(), Mock(), Mock()
        sampler.stop.side_effect = ValueError("Invalid value NaN (not a number)")
        registrar.close.side_effect = lambda: events.append("registrar-close")
        server.server_close.side_effect = lambda: events.append("server-close")
        supervisor.close.side_effect = lambda: events.append("supervisor-close")

        with self.assertRaisesRegex(ValueError, "NaN"):
            serve_registered(server, supervisor, registrar, sampler)

        self.assertEqual(
            ["registrar-close", "server-close", "supervisor-close"],
            events,
            "a failing sampler stop skipped teardown; children would be orphaned",
        )

    def test_parent_secrets_are_removed_before_child_spawn(self) -> None:
        environment = {
            "LEAF_INSTANT_RUNTIME_CONTROL_SECRET": "runtime",
            "LEAF_INSTANT_HOST_LIFECYCLE_SECRET": "host",
            "LEAF_INSTANT_CONTROL_API_SECRET": "legacy",
            "LEAF_INSTANT_CONTROL_JWKS_FILE": "safe-path",
        }
        scrub_child_environment(environment)
        self.assertEqual({"LEAF_INSTANT_CONTROL_JWKS_FILE": "safe-path"}, environment)


class DeployedEnvironmentLabelTests(unittest.TestCase):
    """A deployed executor must refuse to boot without LEAF_INSTANT_ENV.

    Every Leaf/InstantExecution metric this process feeds is dimensioned on
    that label, and a record without it publishes NO datapoint -- while the
    alarms above stay green, because absence is what they were configured to
    tolerate. So the failure has to be moved somewhere an operator sees it,
    and the only such place is a container that will not start.

    Both directions are pinned. Asserting only that an unset variable exits
    would pass against a `main()` that exits for some unrelated reason, so the
    control asserts that SETTING the variable moves the failure on to the NEXT
    precondition. Together they show this gate fires for this reason.
    """

    #: Enough configuration to reach the gate: a non-loopback host makes the
    #: control-secret check (which sits immediately before it) pass.
    BASE = {
        "LEAF_INSTANT_RUNTIME_CONTROL_SECRET": "r" * 40,
    }

    def _main(self, environment: dict[str, str]) -> str:
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(sys, "argv", ["service", "--host", "0.0.0.0"]):
                with self.assertRaises(SystemExit) as caught:
                    main()
        return str(caught.exception)

    def test_a_non_loopback_listener_refuses_to_start_without_the_label(self) -> None:
        message = self._main(dict(self.BASE))
        self.assertIn("LEAF_INSTANT_ENV", message)

    def test_the_gate_passes_once_the_label_is_set(self) -> None:
        """Control. With the label present the boot proceeds PAST this gate and
        stops at the TLS precondition instead, which is the next one in
        `main()`. If this ever reports the LEAF_INSTANT_ENV message again, the
        gate is refusing a correctly configured deployment.
        """
        message = self._main({**self.BASE, "LEAF_INSTANT_ENV": "staging"})
        self.assertNotIn("LEAF_INSTANT_ENV", message)
        self.assertIn("client CA", message)

    def test_a_malformed_label_is_refused_rather_than_published(self) -> None:
        """A label with a space would shift every field of the control plane's
        space-delimited access line. The emitters downgrade it to the sentinel
        so telemetry cannot fail an invocation; the boot must not be so
        forgiving, or the deployment publishes under a label nothing watches.
        """
        message = self._main({**self.BASE, "LEAF_INSTANT_ENV": "two words"})
        self.assertIn("LEAF_INSTANT_ENV", message)

    def test_a_loopback_development_run_is_not_gated(self) -> None:
        """The gate is scoped to non-loopback listeners, exactly like the
        control secret, TLS and trust-bundle preconditions beside it. A local
        run emits the sentinel instead of refusing to start.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(sys, "argv", ["service", "--host", "127.0.0.1"]):
                with self.assertRaises(SystemExit) as caught:
                    main()
        # It still exits, but on the trust-bundle precondition rather than on
        # the environment label.
        self.assertNotIn("LEAF_INSTANT_ENV", str(caught.exception))
        self.assertIn("trust bundle", str(caught.exception))


def _certificates(directory: Path) -> dict[str, str]:
    ca_key, ca_cert = _certificate("test-ca", is_ca=True)
    server_key, server_cert = _certificate("localhost", issuer_key=ca_key, issuer_cert=ca_cert, server=True)
    trusted_client_key, trusted_client_cert = _certificate("trusted-client", issuer_key=ca_key, issuer_cert=ca_cert)
    other_ca_key, other_ca_cert = _certificate("other-ca", is_ca=True)
    untrusted_client_key, untrusted_client_cert = _certificate("untrusted-client", issuer_key=other_ca_key, issuer_cert=other_ca_cert)
    return {
        "ca_cert": _write_certificate(directory / "ca.pem", ca_cert),
        "server_cert": _write_certificate(directory / "server.pem", server_cert),
        "server_key": _write_key(directory / "server.key", server_key),
        "trusted_client_cert": _write_certificate(directory / "trusted-client.pem", trusted_client_cert),
        "trusted_client_key": _write_key(directory / "trusted-client.key", trusted_client_key),
        "untrusted_client_cert": _write_certificate(directory / "untrusted-client.pem", untrusted_client_cert),
        "untrusted_client_key": _write_key(directory / "untrusted-client.key", untrusted_client_key),
    }


def _certificate(subject: str, *, is_ca: bool = False, issuer_key=None, issuer_cert=None,
                 server: bool = False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    builder = x509.CertificateBuilder().subject_name(name).issuer_name(
        issuer_cert.subject if issuer_cert is not None else name,
    ).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(
        datetime.now(UTC) - timedelta(minutes=1),
    ).not_valid_after(datetime.now(UTC) + timedelta(days=1)).add_extension(
        x509.BasicConstraints(ca=is_ca, path_length=None), critical=True,
    ).add_extension(
        x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False,
    ).add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
            issuer_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            if issuer_cert is not None else x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
        ), critical=False,
    ).add_extension(
        x509.KeyUsage(
            digital_signature=not is_ca, content_commitment=False, key_encipherment=not is_ca,
            data_encipherment=False, key_agreement=False, key_cert_sign=is_ca, crl_sign=is_ca,
            encipher_only=False, decipher_only=False,
        ), critical=True,
    )
    if server:
        builder = builder.add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
        ]), critical=False).add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
    elif not is_ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False,
        )
    return key, builder.sign(private_key=issuer_key or key, algorithm=hashes.SHA256())


def _write_certificate(path: Path, certificate: x509.Certificate) -> str:
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(path)


def _write_key(path: Path, key) -> str:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return str(path)
