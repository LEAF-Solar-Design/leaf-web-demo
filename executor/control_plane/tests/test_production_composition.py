from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from executor.control_plane import production
from executor.control_plane.reaper_main import run_reaper


class FakeSigner:
    def jwks(self):
        return {"keys": []}


class FakeRuntime:
    def assign(self, *_):
        return {"state": "ready"}

    def release(self, *_):
        pass


class FakeCoordination:
    def check_available(self):
        pass

    def acquire_claim_lock(self, *_):
        return "test"

    def release_claim_lock(self, *_):
        pass

    def publish_hint(self, *_):
        pass


class FakeStore:
    pass


def production_environment(seed_file: Path) -> dict[str, str]:
    return {
        "LEAF_INSTANT_CONTROL_DATABASE_URL": "postgresql://control-db.internal/control_plane?sslmode=verify-full",
        "LEAF_INSTANT_CONTROL_REDIS_URL": "rediss://cache.internal:6380/0",
        "LEAF_INSTANT_CONTROL_API_SECRET": "a" * 32,
        "LEAF_INSTANT_HOST_LIFECYCLE_SECRET": "h" * 32,
        "LEAF_INSTANT_RUNTIME_CONTROL_SECRET": "b" * 32,
        "LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME": "executor.instant.internal",
        "LEAF_INSTANT_EXECUTOR_CIDRS": "10.20.0.0/16",
        "LEAF_INSTANT_EXECUTOR_PORT": "8088",
        "LEAF_INSTANT_RUNTIME_CLIENT_CA_FILE": str(seed_file),
        "LEAF_INSTANT_RUNTIME_CLIENT_CERT_FILE": str(seed_file),
        "LEAF_INSTANT_RUNTIME_CLIENT_KEY_FILE": str(seed_file),
        "LEAF_INSTANT_LEASE_SIGNING_SEED_FILE": str(seed_file),
    }


class ProductionCompositionTests(unittest.TestCase):
    def test_database_url_uses_installed_ca_bundle_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed_file = Path(temporary) / "seed"
            seed_file.write_bytes(b"x" * 32)
            seed_file.chmod(0o600)

            settings = production.load_production_settings(production_environment(seed_file))

        self.assertEqual(
            settings.database_url,
            "postgresql://control-db.internal/control_plane"
            "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt",
        )

    def test_database_url_preserves_explicit_root_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed_file = Path(temporary) / "seed"
            seed_file.write_bytes(b"x" * 32)
            seed_file.chmod(0o600)
            environment = production_environment(seed_file)
            environment["LEAF_INSTANT_CONTROL_DATABASE_URL"] = (
                "postgresql://control-db.internal/control_plane"
                "?sslmode=verify-full&sslrootcert=/run/leaf/secrets/rds-ca.pem"
            )

            settings = production.load_production_settings(environment)

        self.assertEqual(settings.database_url, environment["LEAF_INSTANT_CONTROL_DATABASE_URL"])

    def test_missing_and_unsafe_configuration_fail_closed(self):
        with self.assertRaisesRegex(production.ProductionConfigurationError, "DATABASE_URL"):
            production.load_production_settings({})

        with tempfile.TemporaryDirectory() as temporary:
            seed_file = Path(temporary) / "seed"
            seed_file.write_bytes(b"x" * 32)
            seed_file.chmod(0o600)
            environment = production_environment(seed_file)
            environment["LEAF_INSTANT_CONTROL_REDIS_URL"] = "redis://cache.internal/0"
            with self.assertRaisesRegex(production.ProductionConfigurationError, "TLS"):
                production.load_production_settings(environment)
            environment = production_environment(seed_file)
            environment["LEAF_INSTANT_CONTROL_API_SECRET"] = "short"
            with self.assertRaisesRegex(production.ProductionConfigurationError, "32"):
                production.load_production_settings(environment)
            environment = production_environment(seed_file)
            environment["LEAF_INSTANT_HOST_LIFECYCLE_SECRET"] = environment["LEAF_INSTANT_CONTROL_API_SECRET"]
            with self.assertRaisesRegex(production.ProductionConfigurationError, "must differ"):
                production.load_production_settings(environment)
            environment = production_environment(seed_file)
            environment["LEAF_INSTANT_EXECUTOR_CIDRS"] = "0.0.0.0/0"
            with self.assertRaisesRegex(production.ProductionConfigurationError, "private IPv4"):
                production.load_production_settings(environment)
            environment = production_environment(seed_file)
            del environment["LEAF_INSTANT_RUNTIME_CLIENT_CA_FILE"]
            with self.assertRaisesRegex(production.ProductionConfigurationError, "CLIENT_CA_FILE"):
                production.load_production_settings(environment)

    def test_seed_must_be_private_and_exactly_32_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed_file = Path(temporary) / "seed"
            seed_file.write_bytes(b"x" * 31)
            seed_file.chmod(0o600)
            with self.assertRaisesRegex(production.ProductionConfigurationError, "exactly 32"):
                production.load_signing_seed(seed_file)

    def test_composition_uses_production_adapters(self):
        with tempfile.TemporaryDirectory() as temporary:
            seed_file = Path(temporary) / "seed"
            seed_file.write_bytes(b"s" * 32)
            seed_file.chmod(0o600)
            connection_factory = object()
            redis_client = object()
            store = object()
            coordination = object()
            runtime = object()
            signer = object()
            with mock.patch.object(production, "_postgres_connection_factory", return_value=connection_factory), \
                    mock.patch.object(production, "_redis_client", return_value=redis_client), \
                    mock.patch.object(production, "PostgresStore", return_value=store) as postgres_store, \
                    mock.patch.object(production, "RedisCoordination", return_value=coordination) as redis_coordination, \
                    mock.patch.object(production, "HttpRuntimeClient", return_value=runtime) as runtime_client, \
                    mock.patch.object(production, "LeaseSigner", return_value=signer) as lease_signer:
                plane = production.create_control_plane(environ=production_environment(seed_file))

            self.assertIs(plane.store, store)
            self.assertIs(plane.coordination, coordination)
            self.assertIs(plane.runtime, runtime)
            self.assertIs(plane.signer, signer)
            postgres_store.assert_called_once_with(connection_factory)
            redis_coordination.assert_called_once_with(redis_client)
            runtime_client.assert_called_once_with(
                "b" * 32,
                ca_file=str(seed_file),
                client_cert_file=str(seed_file),
                client_key_file=str(seed_file),
                tls_server_name="executor.instant.internal",
            )
            lease_signer.assert_called_once_with(b"s" * 32, "control-plane-v1")

    def test_local_test_injection_is_not_an_environment_switch(self):
        dependencies = production.LocalTestDependencies(
            store=FakeStore(), coordination=FakeCoordination(), runtime=FakeRuntime(),
            signer=FakeSigner(), control_api_secret="t" * 32,
            host_lifecycle_secret="h" * 32,
        )
        app = production.create_wsgi_application(environ={}, local_test_dependencies=dependencies)
        captured = []
        response = app(
            {"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/sessions", "CONTENT_LENGTH": "2",
             "wsgi.input": io.BytesIO(b"{}")},
            lambda status, _: captured.append(status),
        )
        self.assertEqual(captured, ["401 Unauthorized"])
        self.assertEqual(b'{"code": "UNAUTHORIZED"}', b"".join(response))


class ReaperEntrypointTests(unittest.TestCase):
    def test_reaper_retries_and_uses_bounded_interval(self):
        class Plane:
            def __init__(self):
                self.calls = []

            def reconcile(self, *, idle_timeout):
                self.calls.append(idle_timeout)
                if len(self.calls) == 1:
                    raise RuntimeError("temporary failure")

        plane = Plane()
        sleeps = []
        self.assertEqual(
            run_reaper(plane, interval_seconds=30, idle_timeout_seconds=60,
                       sleep=sleeps.append, max_cycles=2),
            0,
        )
        self.assertEqual(sleeps, [30])
        self.assertEqual(len(plane.calls), 2)
        self.assertEqual(plane.calls[0].total_seconds(), 60)

    def test_reaper_rejects_an_unbounded_interval(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 300"):
            run_reaper(object(), interval_seconds=301, max_cycles=1)


if __name__ == "__main__":
    unittest.main()
