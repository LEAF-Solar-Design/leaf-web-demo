"""Fail-closed executor host registration and heartbeat support.

This module owns only executor-to-control-plane lifecycle traffic.  It does
not participate in the direct invocation path.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import threading
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_RETRY_SECONDS = 300.0


def _required_secret(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    filename = environ.get(f"{name}_FILE", "")
    if value and filename:
        raise ValueError(f"configure one {name} source")
    if filename:
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError(f"{name}_FILE must be an absolute path")
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"{name}_FILE is unavailable") from exc
    if not value or value != value.strip():
        raise ValueError(f"{name} is required")
    return value


class RegistrationError(RuntimeError):
    """Host lifecycle traffic could not establish a trusted control-plane state."""


class ControlPlaneTransport(Protocol):
    """Small seam for deterministic tests and the stdlib HTTPS implementation."""

    def post(self, route: str, body: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class HostRegistrationConfig:
    """Inputs needed to make one executor host available to the control plane."""

    control_plane_url: str
    executor_id: str
    executor_endpoint: str
    slot_ids: Sequence[str]
    code_digest: str
    runtime_digest: str
    host_lifecycle_secret: str
    ca_file: str
    client_cert_file: str | None = None
    client_key_file: str | None = None
    heartbeat_interval_seconds: float = 10.0
    retry_initial_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    request_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        _https_url(self.control_plane_url, "control-plane URL")
        _https_url(self.executor_endpoint, "executor endpoint")
        _identifier(self.executor_id, "executor ID")
        slots = _slot_ids(self.slot_ids)
        object.__setattr__(self, "slot_ids", slots)
        _digest(self.code_digest, "code digest")
        _digest(self.runtime_digest, "runtime digest")
        _secret(self.host_lifecycle_secret)
        _file(self.ca_file, "CA file")
        if bool(self.client_cert_file) != bool(self.client_key_file):
            raise ValueError("client certificate and client key must be configured together")
        if self.client_cert_file:
            _file(self.client_cert_file, "client certificate")
            _file(self.client_key_file or "", "client key")
        _seconds(self.heartbeat_interval_seconds, "heartbeat interval", minimum=0.01)
        _seconds(self.retry_initial_seconds, "retry initial delay", minimum=0.001)
        _seconds(self.retry_max_seconds, "retry maximum delay", minimum=0.001)
        _seconds(self.request_timeout_seconds, "request timeout", minimum=0.001)
        if self.retry_initial_seconds > self.retry_max_seconds:
            raise ValueError("retry initial delay must not exceed retry maximum delay")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *,
                         executor_id: str | None = None,
                         slot_ids: Sequence[str] | None = None) -> "HostRegistrationConfig":
        """Load the registration inputs once, before the executor accepts traffic."""
        source = os.environ if environ is None else environ
        return cls(
            control_plane_url=_required(source, "LEAF_INSTANT_CONTROL_PLANE_URL"),
            executor_id=executor_id or _required(source, "LEAF_INSTANT_EXECUTOR_ID"),
            executor_endpoint=resolve_executor_endpoint(source),
            slot_ids=slot_ids or tuple(item.strip() for item in _required(source, "LEAF_INSTANT_EXECUTOR_SLOT_IDS").split(",")),
            code_digest=_required(source, "LEAF_INSTANT_EXECUTOR_CODE_DIGEST"),
            runtime_digest=_required(source, "LEAF_INSTANT_EXECUTOR_RUNTIME_DIGEST"),
            host_lifecycle_secret=_required_secret(source, "LEAF_INSTANT_HOST_LIFECYCLE_SECRET"),
            ca_file=_required(source, "LEAF_INSTANT_CONTROL_CA_FILE"),
            client_cert_file=source.get("LEAF_INSTANT_CONTROL_CLIENT_CERT_FILE") or None,
            client_key_file=source.get("LEAF_INSTANT_CONTROL_CLIENT_KEY_FILE") or None,
            heartbeat_interval_seconds=_environment_seconds(source, "LEAF_INSTANT_HOST_HEARTBEAT_SECONDS", 10.0),
            retry_initial_seconds=_environment_seconds(source, "LEAF_INSTANT_HOST_RETRY_INITIAL_SECONDS", 1.0),
            retry_max_seconds=_environment_seconds(source, "LEAF_INSTANT_HOST_RETRY_MAX_SECONDS", 30.0),
        )


def resolve_executor_endpoint(
    environ: Mapping[str, str],
    *,
    opener: Callable[..., object] = urlopen,
) -> str:
    """Resolve an explicit local endpoint or one private Fargate task address."""
    explicit = environ.get("LEAF_INSTANT_EXECUTOR_ENDPOINT", "").strip()
    if explicit:
        _https_url(explicit, "executor endpoint")
        return explicit
    document = _ecs_metadata_document(environ, opener=opener)
    networks = document.get("Networks")
    addresses: list[str] = []
    if isinstance(networks, list):
        for network in networks:
            if not isinstance(network, dict) or network.get("NetworkMode") != "awsvpc":
                continue
            values = network.get("IPv4Addresses")
            if isinstance(values, list):
                addresses.extend(value for value in values if isinstance(value, str))
    valid = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if (
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
        ):
            valid.append(str(address))
    if len(set(valid)) != 1:
        raise ValueError("ECS task metadata must contain exactly one private awsvpc IPv4 address")
    try:
        port = int(environ.get("LEAF_INSTANT_EXECUTOR_PORT", "8088"))
    except ValueError as exc:
        raise ValueError("LEAF_INSTANT_EXECUTOR_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("LEAF_INSTANT_EXECUTOR_PORT is out of range")
    return f"https://{valid[0]}:{port}"


def resolve_executor_id(
    environ: Mapping[str, str],
    *,
    opener: Callable[..., object] = urlopen,
) -> str:
    """Derive one stable host ID from the current ECS task ARN."""
    document = _ecs_metadata_document(environ, opener=opener, task=True)
    task_arn = document.get("TaskARN")
    if not isinstance(task_arn, str):
        raise ValueError("ECS task metadata has no task ARN")
    task_id = task_arn.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise ValueError("ECS task metadata contains an invalid task ARN")
    return f"executor-ecs-{task_id}"


def _ecs_metadata_document(
    environ: Mapping[str, str],
    *,
    opener: Callable[..., object],
    task: bool = False,
) -> dict[str, object]:
    metadata_uri = environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip()
    if not metadata_uri:
        raise ValueError(
            "LEAF_INSTANT_EXECUTOR_ENDPOINT or ECS_CONTAINER_METADATA_URI_V4 is required"
        )
    parsed = urlsplit(metadata_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "169.254.170.2"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ECS_CONTAINER_METADATA_URI_V4 is not the trusted ECS metadata endpoint")
    request_uri = metadata_uri.rstrip("/") + ("/task" if task else "")
    try:
        with opener(request_uri, timeout=2.0) as response:  # nosec B310: exact link-local host validated above.
            document = json.loads(response.read())
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("ECS task metadata could not be read") from exc
    if not isinstance(document, dict):
        raise ValueError("ECS task metadata must be a JSON object")
    return document


class UrllibHttpsTransport:
    """HTTPS transport that validates the configured CA and may use mTLS."""

    def __init__(self, config: HostRegistrationConfig) -> None:
        self._base_url = config.control_plane_url.rstrip("/")
        self._secret = config.host_lifecycle_secret
        self._timeout = config.request_timeout_seconds
        self._context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=config.ca_file)
        if config.client_cert_file:
            self._context.load_cert_chain(config.client_cert_file, config.client_key_file)

    def post(self, route: str, body: dict[str, object]) -> None:
        request = Request(
            self._base_url + route,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Instant-Control-Secret": self._secret,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout, context=self._context) as response:  # nosec B310: config validates HTTPS.
                response.read()
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise RegistrationError("control-plane lifecycle request failed") from exc


class HostRegistrar:
    """Registers slots before availability and maintains host heartbeats."""

    def __init__(self, config: HostRegistrationConfig, *, transport: ControlPlaneTransport | None = None,
                 wait: Callable[[float], bool] | None = None,
                 thread_factory: Callable[..., threading.Thread] = threading.Thread) -> None:
        self.config = config
        self._transport = transport or UrllibHttpsTransport(config)
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._thread_factory = thread_factory
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._available = False
        self._closed = False

    @property
    def available(self) -> bool:
        with self._lock:
            return self._available

    def start(self) -> None:
        """Synchronously establish lifecycle state, then start periodic heartbeats."""
        with self._lock:
            if self._closed:
                raise RegistrationError("host registrar is closed")
            if self._thread is not None:
                return
        self._register_and_heartbeat()
        with self._lock:
            if self._closed:
                raise RegistrationError("host registrar is closed")
            self._available = True
            self._thread = self._thread_factory(target=self._run, name="executor-host-heartbeat", daemon=True)
            self._thread.start()

    def close(self, timeout: float | None = 5.0) -> None:
        """Stop periodic activity and make this host unavailable in-process."""
        with self._lock:
            self._closed = True
            self._available = False
            thread = self._thread
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def heartbeat_once(self) -> None:
        """Send a heartbeat, re-registering with capped retry after any failure."""
        try:
            self._heartbeat()
        except RegistrationError:
            with self._lock:
                self._available = False
            self._recover()
        else:
            with self._lock:
                self._available = True

    def _run(self) -> None:
        while not self._wait(self.config.heartbeat_interval_seconds):
            self.heartbeat_once()

    def _recover(self) -> None:
        delay = self.config.retry_initial_seconds
        while not self._stop.is_set():
            if self._wait(delay):
                return
            try:
                self._register_and_heartbeat()
            except RegistrationError:
                delay = min(delay * 2, self.config.retry_max_seconds, _MAX_RETRY_SECONDS)
                continue
            with self._lock:
                if not self._closed:
                    self._available = True
            return

    def _register_and_heartbeat(self) -> None:
        self._post("/v1/hosts/register", {
            "executor_id": self.config.executor_id,
            "endpoint": self.config.executor_endpoint,
            "slot_ids": list(self.config.slot_ids),
        })
        for slot_id in self.config.slot_ids:
            self._post("/v1/hosts/readiness", {
                "executor_id": self.config.executor_id,
                "slot_id": slot_id,
                "ready": True,
                "code_digest": self.config.code_digest,
                "runtime_digest": self.config.runtime_digest,
            })
        self._heartbeat()

    def _heartbeat(self) -> None:
        self._post("/v1/hosts/heartbeat", {"executor_id": self.config.executor_id})

    def _post(self, route: str, body: dict[str, object]) -> None:
        try:
            self._transport.post(route, body)
        except RegistrationError:
            raise
        except Exception as exc:
            raise RegistrationError("control-plane lifecycle request failed") from exc


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} is required")
    return value


def _https_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if (not isinstance(value, str) or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an HTTPS URL")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _slot_ids(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not 1 <= len(value) <= 1024:
        raise ValueError("slot list must contain between 1 and 1024 slot IDs")
    slots = tuple(value)
    for slot_id in slots:
        _identifier(slot_id, "slot ID")
    if len(set(slots)) != len(slots):
        raise ValueError("slot list must not contain duplicate slot IDs")
    return slots


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a sha256 digest")


def _secret(value: str) -> None:
    if (not isinstance(value, str) or len(value) < 32
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError("control API secret must contain at least 32 non-whitespace characters")


def _file(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or not Path(value).is_file():
        raise ValueError(f"{name} must name a readable file")


def _seconds(value: float, name: str, *, minimum: float) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= float(value) <= _MAX_RETRY_SECONDS:
        raise ValueError(f"{name} must be between {minimum} and {_MAX_RETRY_SECONDS} seconds")


def _environment_seconds(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of seconds") from exc
