"""Production WSGI composition for the instant execution control plane.

Use ``gunicorn 'executor.control_plane.production:create_wsgi_application()'``.
This module intentionally has no development fallbacks.  Tests may pass
``local_test_dependencies`` to bypass environment loading with hermetic fakes.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import stat
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .api import application
from .coordination import RedisCoordination
from .jws import LeaseSigner
from .runtime import HttpRuntimeClient
from .service import ControlPlane
from .store import PostgresStore


_DEFAULT_ROOT_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt"


class ProductionConfigurationError(RuntimeError):
    """Raised when production composition cannot be made safe."""


@dataclass(frozen=True)
class ProductionSettings:
    database_url: str
    redis_url: str
    control_api_secret: str
    host_lifecycle_secret: str
    runtime_control_secret: str
    runtime_tls_server_name: str
    executor_cidrs: tuple[str, ...]
    executor_port: int
    runtime_ca_file: Path
    runtime_client_cert_file: Path
    runtime_client_key_file: Path
    signing_seed_file: Path
    signing_key_id: str
    reaper_interval_seconds: int
    reaper_idle_timeout_seconds: int | None
    allowed_artifact_digests: frozenset[str] = frozenset()
    accounting_ingest_secret: str = ""
    accounting_stale_timeout_seconds: int = 120


@dataclass(frozen=True)
class LocalTestDependencies:
    """Hermetic dependencies for unit tests only, never selected by env vars."""

    store: object
    coordination: object
    runtime: object
    signer: object
    control_api_secret: str
    host_lifecycle_secret: str


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    filename = environ.get(f"{name}_FILE", "")
    if value and filename:
        raise ProductionConfigurationError(f"configure one {name} source")
    if filename:
        path = Path(filename)
        if not path.is_absolute():
            raise ProductionConfigurationError(f"{name}_FILE must be an absolute mounted path")
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProductionConfigurationError(f"{name}_FILE is unavailable") from exc
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProductionConfigurationError(f"{name} is required")
    return value


def _secret(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if len(value) < 32 or any(character.isspace() or ord(character) < 32 for character in value):
        raise ProductionConfigurationError(f"{name} must contain at least 32 non-whitespace characters")
    return value


def _postgres_url(environ: Mapping[str, str]) -> str:
    value = _required(environ, "LEAF_INSTANT_CONTROL_DATABASE_URL")
    parsed = urlparse(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
            or parsed.fragment or any(part.isspace() for part in value)
            or query.get("sslmode") != ["verify-full"]):
        raise ProductionConfigurationError(
            "LEAF_INSTANT_CONTROL_DATABASE_URL must be a PostgreSQL URL with sslmode=verify-full"
        )
    if "sslrootcert" not in query:
        separator = "&" if parsed.query else "?"
        return f"{value}{separator}sslrootcert={_DEFAULT_ROOT_CERT_FILE}"
    return value


def _redis_url(environ: Mapping[str, str]) -> str:
    value = _required(environ, "LEAF_INSTANT_CONTROL_REDIS_URL")
    parsed = urlparse(value)
    if (parsed.scheme != "rediss" or not parsed.hostname or parsed.fragment
            or any(part.isspace() for part in value)):
        raise ProductionConfigurationError("LEAF_INSTANT_CONTROL_REDIS_URL must be a TLS rediss:// URL")
    return value


def _positive_seconds(environ: Mapping[str, str], name: str, *, default: int | None,
                      maximum: int) -> int | None:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProductionConfigurationError(f"{name} must be an integer number of seconds") from exc
    if value < 1 or value > maximum:
        raise ProductionConfigurationError(f"{name} must be between 1 and {maximum} seconds")
    return value


def _absolute_file(environ: Mapping[str, str], name: str) -> Path:
    filename = Path(_required(environ, name))
    if not filename.is_absolute():
        raise ProductionConfigurationError(f"{name} must be an absolute mounted path")
    try:
        if not filename.is_file():
            raise ProductionConfigurationError(f"{name} must name a regular file")
    except OSError as exc:
        raise ProductionConfigurationError(f"{name} is unavailable") from exc
    return filename


def _executor_networks(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = _required(environ, "LEAF_INSTANT_EXECUTOR_CIDRS")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise ProductionConfigurationError("LEAF_INSTANT_EXECUTOR_CIDRS must not be empty")
    try:
        networks = tuple(ipaddress.ip_network(value, strict=True) for value in values)
    except ValueError as exc:
        raise ProductionConfigurationError("LEAF_INSTANT_EXECUTOR_CIDRS contains an invalid network") from exc
    if any(network.version != 4 or not network.is_private for network in networks):
        raise ProductionConfigurationError("LEAF_INSTANT_EXECUTOR_CIDRS must contain private IPv4 networks")
    return tuple(str(network) for network in networks)


def _artifact_allowlist(environ: Mapping[str, str]) -> frozenset[str]:
    raw = _required(environ, "LEAF_INSTANT_ALLOWED_ARTIFACT_DIGESTS")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in values):
        raise ProductionConfigurationError(
            "LEAF_INSTANT_ALLOWED_ARTIFACT_DIGESTS must contain sha256 digests"
        )
    if len(values) != len(set(values)):
        raise ProductionConfigurationError("artifact allowlist contains duplicate digests")
    return frozenset(values)


def load_production_settings(environ: Mapping[str, str] | None = None) -> ProductionSettings:
    """Read all production inputs once, before requests or scheduler work begin."""
    source = os.environ if environ is None else environ
    database_url = _postgres_url(source)
    redis_url = _redis_url(source)
    control_api_secret = _secret(source, "LEAF_INSTANT_CONTROL_API_SECRET")
    accounting_ingest_secret = _secret(source, "LEAF_INSTANT_ACCOUNTING_INGEST_SECRET")
    host_lifecycle_secret = _secret(source, "LEAF_INSTANT_HOST_LIFECYCLE_SECRET")
    if len({host_lifecycle_secret, control_api_secret, accounting_ingest_secret}) != 3:
        raise ProductionConfigurationError("app, host lifecycle, and accounting secrets must differ")
    runtime_control_secret = _secret(source, "LEAF_INSTANT_RUNTIME_CONTROL_SECRET")
    runtime_tls_server_name = _required(source, "LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", runtime_tls_server_name):
        raise ProductionConfigurationError("LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME is invalid")
    executor_cidrs = _executor_networks(source)
    executor_port = int(_positive_seconds(source, "LEAF_INSTANT_EXECUTOR_PORT", default=8088, maximum=65535) or 0)
    runtime_ca_file = _absolute_file(source, "LEAF_INSTANT_RUNTIME_CLIENT_CA_FILE")
    runtime_client_cert_file = _absolute_file(source, "LEAF_INSTANT_RUNTIME_CLIENT_CERT_FILE")
    runtime_client_key_file = _absolute_file(source, "LEAF_INSTANT_RUNTIME_CLIENT_KEY_FILE")
    seed_file = _absolute_file(source, "LEAF_INSTANT_LEASE_SIGNING_SEED_FILE")
    kid = source.get("LEAF_INSTANT_LEASE_SIGNING_KEY_ID", "control-plane-v1")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", kid):
        raise ProductionConfigurationError("LEAF_INSTANT_LEASE_SIGNING_KEY_ID contains unsafe characters")
    return ProductionSettings(
        database_url=database_url,
        redis_url=redis_url,
        control_api_secret=control_api_secret,
        host_lifecycle_secret=host_lifecycle_secret,
        runtime_control_secret=runtime_control_secret,
        runtime_tls_server_name=runtime_tls_server_name,
        executor_cidrs=executor_cidrs,
        executor_port=executor_port,
        runtime_ca_file=runtime_ca_file,
        runtime_client_cert_file=runtime_client_cert_file,
        runtime_client_key_file=runtime_client_key_file,
        signing_seed_file=seed_file,
        signing_key_id=kid,
        reaper_interval_seconds=_positive_seconds(
            source, "LEAF_INSTANT_REAPER_INTERVAL_SECONDS", default=30, maximum=300,
        ),
        reaper_idle_timeout_seconds=_positive_seconds(
            source, "LEAF_INSTANT_REAPER_IDLE_TIMEOUT_SECONDS", default=None, maximum=86_400,
        ),
        allowed_artifact_digests=_artifact_allowlist(source),
        accounting_ingest_secret=accounting_ingest_secret,
        accounting_stale_timeout_seconds=_positive_seconds(
            source, "LEAF_INSTANT_ACCOUNTING_STALE_TIMEOUT_SECONDS", default=120, maximum=3600,
        ),
    )


def load_signing_seed(filename: Path) -> bytes:
    """Load exactly one private Ed25519 seed from the mounted secret file."""
    try:
        metadata = filename.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ProductionConfigurationError("lease signing seed path must be a regular file")
        if os.name != "nt" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProductionConfigurationError("lease signing seed file must not be group or world accessible")
        seed = filename.read_bytes()
    except OSError as exc:
        raise ProductionConfigurationError("lease signing seed file is unavailable") from exc
    if len(seed) != 32:
        raise ProductionConfigurationError("lease signing seed file must contain exactly 32 bytes")
    return seed


def _postgres_connection_factory(database_url: str) -> Callable[[], object]:
    try:
        import psycopg
    except ImportError as exc:
        raise ProductionConfigurationError("psycopg is required for production control-plane storage") from exc
    return lambda: psycopg.connect(database_url)


def _redis_client(redis_url: str):
    try:
        import redis
    except ImportError as exc:
        raise ProductionConfigurationError("redis is required for production control-plane coordination") from exc
    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        ssl_cert_reqs="required",
    )


def _compose(settings: ProductionSettings) -> ControlPlane:
    store = PostgresStore(_postgres_connection_factory(settings.database_url))
    coordination = RedisCoordination(_redis_client(settings.redis_url))
    runtime = HttpRuntimeClient(
        settings.runtime_control_secret,
        ca_file=str(settings.runtime_ca_file),
        client_cert_file=str(settings.runtime_client_cert_file),
        client_key_file=str(settings.runtime_client_key_file),
        tls_server_name=settings.runtime_tls_server_name,
    )
    signer = LeaseSigner(load_signing_seed(settings.signing_seed_file), settings.signing_key_id)
    return ControlPlane(
        store,
        runtime,
        signer,
        coordination,
        executor_cidrs=settings.executor_cidrs,
        executor_port=settings.executor_port,
        allowed_artifact_digests=settings.allowed_artifact_digests,
    )


def create_control_plane(*, environ: Mapping[str, str] | None = None,
                         local_test_dependencies: LocalTestDependencies | None = None) -> ControlPlane:
    """Build the fail-closed production service, or explicit hermetic test fakes."""
    if local_test_dependencies is not None:
        return ControlPlane(
            local_test_dependencies.store,
            local_test_dependencies.runtime,
            local_test_dependencies.signer,
            local_test_dependencies.coordination,
        )
    return _compose(load_production_settings(environ))


def create_wsgi_application(*, environ: Mapping[str, str] | None = None,
                            local_test_dependencies: LocalTestDependencies | None = None):
    """WSGI factory. Invalid configuration prevents the worker from starting."""
    if local_test_dependencies is not None:
        return application(
            create_control_plane(local_test_dependencies=local_test_dependencies),
            app_control_secret=local_test_dependencies.control_api_secret,
            host_lifecycle_secret=local_test_dependencies.host_lifecycle_secret,
        )
    settings = load_production_settings(environ)
    return application(
        _compose(settings),
        app_control_secret=settings.control_api_secret,
        host_lifecycle_secret=settings.host_lifecycle_secret,
        accounting_secret=settings.accounting_ingest_secret,
    )
