"""Sanitized HTTP dispatch adapter for the separately deployed iOS provider."""
from __future__ import annotations

import os
import re
import stat
from uuid import UUID
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import requests


PROVIDER_PATH = "/v1/ios-ship/executions"
READINESS_PATH = "/v1/readiness"
SOURCE_CATALOG_PATH = "/v1/ios-ship/source-catalog"
_CATALOG_SCHEMA = "leaf.ios-ship-source-catalog.v1"
_MAX_CATALOG_SOURCES = 50
DEFAULT_TIMEOUT_SECONDS = 10.0
_RESPONSE_FIELDS = frozenset({"status", "stage", "provider_run_id"})
_RESPONSE_STATUSES = frozenset({"dispatched", "running", "failed"})
_READINESS_SCOPE_FIELDS = frozenset({
    "tenant_id", "project_id", "source_revision", "source_sha256", "bundle_id",
    "marketing_version", "build_number",
})
_READINESS_FIELDS = frozenset({
    "schema", "scope", "healthy", "reported_at", "grant_id", "grant_revision",
    "grant_expires_at", "setup_action", "dispatch_available",
})
_READINESS_SCHEMA = "leaf.ios-ship-provider-readiness.v1"
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|two.?factor|2fa|otp|p8|\.p8|private[_ -]?key|certificate|"
    r"provisioning|profile|credential|secret|keychain|authkey|token|session|cookie|"
    r"api[_ -]?key|signing[_ -]?(key|cert))", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|eyJ[a-zA-Z0-9_-]{10,}\.eyJ|"
    r"AAAA[A-Za-z0-9+/]{20,}={0,2}")


class ProviderConfigurationError(ValueError):
    """Provider configuration is missing or unsafe."""


class ProviderDispatchError(RuntimeError):
    """Provider admission failed without exposing provider response material."""


class ProviderCatalogError(RuntimeError):
    """Provider catalog is unavailable without exposing response material."""


class ProviderReadinessError(RuntimeError):
    """Provider readiness is unavailable without exposing response material."""

    def __init__(self, setup_action: str = "mount-apple-ship-dispatch"):
        super().__init__("provider readiness is unavailable")
        self.setup_action = setup_action


def _reject_secret_shaped(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise ProviderDispatchError(f"secret-shaped dispatch field at {path}.{key}")
            _reject_secret_shaped(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_shaped(child, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise ProviderDispatchError(f"secret-shaped dispatch value at {path}")


def _is_private_token_file(path: Path) -> bool:
    """Return true only for a regular POSIX mode-0600 token file.

    The production server is Linux. Windows does not expose a POSIX mode that
    can prove this contract, so it fails closed unless a test replaces this
    narrow predicate.
    """
    try:
        info = path.stat()
    except OSError:
        return False
    return os.name != "nt" and stat.S_ISREG(info.st_mode) \
        and stat.S_IMODE(info.st_mode) == (stat.S_IRUSR | stat.S_IWUSR)


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    token_file: Path
    ca_file: Path
    provider_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_environment(cls) -> Optional["ProviderConfig"]:
        base_url = os.environ.get("LEAF_IOS_SHIP_PROVIDER_URL", "").strip()
        token_name = os.environ.get("LEAF_IOS_SHIP_PROVIDER_TOKEN_FILE", "").strip()
        ca_name = os.environ.get("LEAF_IOS_SHIP_PROVIDER_CA_FILE", "").strip()
        provider_id = os.environ.get("LEAF_IOS_SHIP_PROVIDER_ID", "").strip()
        if not base_url or not token_name or not ca_name or not provider_id:
            return None
        if not base_url.startswith("https://"):
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", provider_id):
            return None
        ca_file = Path(ca_name)
        if not ca_file.is_file():
            return None
        config = cls(base_url=base_url.rstrip("/"), token_file=Path(token_name),
                     ca_file=ca_file, provider_id=provider_id)
        try:
            config.read_token()
        except ProviderConfigurationError:
            return None
        return config

    def read_token(self) -> str:
        if not _is_private_token_file(self.token_file):
            raise ProviderConfigurationError("provider token file is unavailable or insecure")
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise ProviderConfigurationError("provider token file is unavailable or insecure") \
                from None
        if not token or "\n" in token or "\r" in token or len(token) > 4096:
            raise ProviderConfigurationError("provider token file is unavailable or insecure")
        return token


class HttpProviderDispatch:
    """POST one admitted browser-safe intent to the provider admission API."""

    def __init__(self, config: ProviderConfig, *,
                 post: Callable[..., Any] = requests.post,
                 get: Callable[..., Any] = requests.get):
        self.config = config
        self._post = post
        self._get = get

    def readiness(self, scope: Mapping[str, str]) -> Dict[str, Any]:
        if not isinstance(scope, Mapping) or set(scope) != _READINESS_SCOPE_FIELDS:
            raise ProviderReadinessError()
        if any(not isinstance(scope[name], str) or not scope[name] for name in _READINESS_SCOPE_FIELDS):
            raise ProviderReadinessError()
        _reject_secret_shaped(scope)
        token = self.config.read_token()
        try:
            response = self._get(
                self.config.base_url + READINESS_PATH,
                headers={"Authorization": f"Bearer {token}"}, params=dict(scope),
                timeout=self.config.timeout_seconds, verify=str(self.config.ca_file),
            )
            response.raise_for_status()
            payload = response.json()
            return _validate_readiness(payload, scope)
        except ProviderReadinessError:
            raise
        except Exception:
            raise ProviderReadinessError() from None

    def source_catalog(self, project_id: str) -> Dict[str, Any]:
        try:
            if not isinstance(project_id, str):
                raise ProviderCatalogError()
            UUID(project_id)
            token = self.config.read_token()
            response = self._get(
                self.config.base_url + SOURCE_CATALOG_PATH,
                headers={"Authorization": f"Bearer {token}"},
                params={"project_id": project_id},
                timeout=self.config.timeout_seconds, verify=str(self.config.ca_file),
            )
            response.raise_for_status()
            return _validate_source_catalog(response.json(), project_id)
        except ProviderCatalogError:
            raise
        except Exception:
            raise ProviderCatalogError() from None

    def dispatch(self, intent: Dict[str, Any]) -> Dict[str, str]:
        _reject_secret_shaped(intent)
        execution_id = intent.get("execution_id") if isinstance(intent, dict) else None
        if not isinstance(execution_id, str) or not execution_id:
            raise ProviderDispatchError("dispatch intent needs an execution_id")
        token = self.config.read_token()
        try:
            response = self._post(
                self.config.base_url + PROVIDER_PATH,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": execution_id,
                },
                json=intent,
                timeout=self.config.timeout_seconds,
                verify=str(self.config.ca_file),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            raise ProviderDispatchError("provider admission outcome is unavailable") from None
        return _validate_response(payload)


def _validate_source_catalog(payload: Any, project_id: str) -> Dict[str, Any]:
    try:
        _reject_secret_shaped(payload)
        fields = {"schema", "project_id", "catalog_key", "status", "sources", "unpinned", "refused"}
        patterns = {
            "source_revision": r"[0-9a-f]{40}",
            "source_sha256": r"[0-9a-f]{64}",
            "producer_receipt_digest": r"[0-9a-f]{64}",
            "bundle_identifier": r"[A-Za-z0-9.-]{3,255}",
            "marketing_version": r"[0-9]+(?:\.[0-9]+){1,2}",
            "build_number": r"[0-9]{1,20}",
        }

        def matches(value, pattern):
            return isinstance(value, str) and re.fullmatch(pattern, value) is not None

        if not isinstance(payload, dict) or set(payload) != fields:
            raise ProviderCatalogError()
        if (payload["schema"] != _CATALOG_SCHEMA or payload["project_id"] != project_id
                or payload["status"] not in {"ok", "metadata_unavailable", "unconfigured"}
                or not isinstance(payload["catalog_key"], str)
                or not 1 <= len(payload["catalog_key"]) <= 128):
            raise ProviderCatalogError()
        for name in ("sources", "unpinned", "refused"):
            if not isinstance(payload[name], list) or len(payload[name]) > _MAX_CATALOG_SOURCES:
                raise ProviderCatalogError()
        for entry in payload["sources"]:
            if not isinstance(entry, dict) or set(entry) != set(patterns) | {"repository"}:
                raise ProviderCatalogError()
            if any(not matches(entry[key], pattern) for key, pattern in patterns.items()):
                raise ProviderCatalogError()
            repository = entry["repository"]
            if (not isinstance(repository, str) or not 1 <= len(repository) <= 512
                    or not repository.startswith("https://")):
                raise ProviderCatalogError()
        if any(not matches(value, patterns["source_revision"]) for value in payload["unpinned"]):
            raise ProviderCatalogError()
        for entry in payload["refused"]:
            if (not isinstance(entry, dict) or set(entry) != {"source_revision", "reason"}
                    or not matches(entry["source_revision"], patterns["source_revision"])
                    or not matches(entry["reason"], r"[a-z_]{1,64}")):
                raise ProviderCatalogError()
        # Reject all PEM markers, including material outside the shared private-key pattern.
        if "-----BEGIN" in str(payload):
            raise ProviderCatalogError()
        return payload
    except ProviderCatalogError:
        raise
    except Exception:
        raise ProviderCatalogError() from None


def _validate_response(payload: Any) -> Dict[str, str]:
    _reject_secret_shaped(payload)
    if not isinstance(payload, dict) or not set(payload) <= _RESPONSE_FIELDS:
        raise ProviderDispatchError("provider admission response has unsupported fields")
    status, provider_run_id = payload.get("status"), payload.get("provider_run_id")
    stage = payload.get("stage")
    if status not in _RESPONSE_STATUSES or not isinstance(provider_run_id, str) \
            or not provider_run_id:
        raise ProviderDispatchError("provider admission response is invalid")
    if stage is not None and (not isinstance(stage, str) or not stage):
        raise ProviderDispatchError("provider admission response is invalid")
    if status == "failed" and not stage:
        raise ProviderDispatchError("failed provider admission needs an exact stage")
    return {key: payload[key] for key in ("status", "stage", "provider_run_id")
            if key in payload}


def _validate_readiness(payload: Any, scope: Mapping[str, str]) -> Dict[str, Any]:
    _reject_secret_shaped(payload)
    if not isinstance(payload, dict) or set(payload) != _READINESS_FIELDS:
        raise ProviderReadinessError()
    if payload.get("schema") != _READINESS_SCHEMA or payload.get("scope") != dict(scope):
        raise ProviderReadinessError()
    required = ("reported_at", "grant_id", "grant_revision", "grant_expires_at")
    if (not isinstance(payload.get("healthy"), bool)
            or not isinstance(payload.get("dispatch_available"), bool)
            or any(not isinstance(payload.get(name), str) or not payload[name] for name in required)
            or payload.get("setup_action") is not None and (
                not isinstance(payload["setup_action"], str) or not payload["setup_action"])
            or (payload["healthy"] and payload["setup_action"] is not None)):
        raise ProviderReadinessError()
    return {name: payload[name] for name in _READINESS_FIELDS - {"scope"}}
