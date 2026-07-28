"""Trusted platform artifact resolution for the instant execution tier.

The filesystem adapter is deliberately limited to checked-in platform tools.
It accepts catalog metadata, never tenant paths or caller-supplied source. An
object-store adapter can implement :class:`InstantArtifactRegistry` later
without changing session routing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping, Protocol


_DIGEST = "sha256:"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_ARTIFACT_BYTES = 1 * 1024 * 1024


class ArtifactResolutionError(ValueError):
    """A catalog entry cannot resolve to a trusted immutable artifact."""


@dataclass(frozen=True)
class InstantArtifact:
    """Validated immutable artifact and its execution binding."""

    tool_id: str
    tool_version: str
    execution_class: str
    runtime: str
    capability_id: str
    catalog_commit: str
    entrypoint: str
    params_schema_digest: str
    limits: Mapping[str, Any]
    code_digest: str
    artifact_digest: str
    source: bytes

    def limits_for_transport(self) -> dict[str, Any]:
        """Return a JSON-safe copy for the control-plane staging request."""
        return _thaw(self.limits)


class InstantArtifactRegistry(Protocol):
    """Resolve a trusted catalog entry without accepting caller source bytes."""

    def resolve(
        self, catalog_entry: Mapping[str, Any], *, catalog_commit: str,
    ) -> InstantArtifact:
        """Return the artifact bound to this immutable catalog commit."""


def _sha256(value: bytes) -> str:
    return _DIGEST + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactResolutionError("instant parameter schema is not canonical JSON") from exc
    return _sha256(encoded)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactResolutionError(f"instant artifact requires {field}")
    return value


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArtifactResolutionError(f"instant artifact requires a sha256 {field}")
    return value


def _relative_entry(value: Any) -> PurePosixPath:
    entry = _require_string(value, "entry")
    normalized = entry.replace("\\", "/")
    path = PurePosixPath(normalized)
    segments = normalized.split("/")
    if (
        path.is_absolute()
        or PureWindowsPath(entry).is_absolute()
        or PureWindowsPath(entry).drive
        or ".." in segments
        or "." in segments
    ):
        raise ArtifactResolutionError("instant artifact entry must be a relative platform path")
    if not path.parts:
        raise ArtifactResolutionError("instant artifact requires entry")
    return path


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _immutable_limits(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ArtifactResolutionError("instant artifact requires catalog limits")
    try:
        normalized = json.loads(json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ))
    except (TypeError, ValueError) as exc:
        raise ArtifactResolutionError("instant artifact limits are not canonical JSON") from exc
    if not isinstance(normalized, dict):  # Defensive against future decoder changes.
        raise ArtifactResolutionError("instant artifact limits must be an object")
    return _freeze(normalized)


class FilesystemTrustedPlatformArtifactRegistry:
    """Resolve only UTF-8 checked-in platform files under one trusted root."""

    def __init__(self, root: Path, *, max_artifact_bytes: int = _MAX_ARTIFACT_BYTES) -> None:
        if not isinstance(max_artifact_bytes, int) or max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._root = Path(root)
        self._max_artifact_bytes = max_artifact_bytes

    def resolve(
        self, catalog_entry: Mapping[str, Any], *, catalog_commit: str,
    ) -> InstantArtifact:
        if not isinstance(catalog_entry, Mapping):
            raise ArtifactResolutionError("instant artifact catalog entry must be an object")
        if not isinstance(catalog_commit, str) or not _COMMIT.fullmatch(catalog_commit):
            raise ArtifactResolutionError("instant artifact requires an immutable catalog commit")
        declared_commit = catalog_entry.get("catalog_commit")
        if declared_commit is not None and declared_commit != catalog_commit:
            raise ArtifactResolutionError("instant artifact catalog commit does not match the active pin")
        if catalog_entry.get("execution_class") != "instant":
            raise ArtifactResolutionError("instant artifact execution_class must be instant")
        if catalog_entry.get("runtime") != "python-3.12":
            raise ArtifactResolutionError("instant artifact runtime must be python-3.12")
        if catalog_entry.get("capabilities") != ["drawing.read"]:
            raise ArtifactResolutionError("instant artifact capability must be drawing.read")

        tool_id = _require_string(catalog_entry.get("name"), "tool ID")
        tool_version = _require_string(catalog_entry.get("version", "1.0.0"), "tool version")
        entrypoint = _require_string(catalog_entry.get("entrypoint", "tool:run"), "entrypoint")
        params_schema = catalog_entry.get("params", {"type": "object", "properties": {}})
        if not isinstance(params_schema, dict):
            raise ArtifactResolutionError("instant artifact parameter schema must be an object")
        limits = _immutable_limits(catalog_entry.get("limits"))
        path = self._resolve_entry(_relative_entry(catalog_entry.get("entry")))
        source = self._read_normalized(path)
        digest = _sha256(source)
        code_digest = _require_digest(catalog_entry.get("code_digest"), "code_digest")
        artifact_digest = _require_digest(catalog_entry.get("artifact_digest"), "artifact_digest")
        if code_digest != digest or artifact_digest != digest:
            raise ArtifactResolutionError("instant artifact digest does not match trusted source")

        return InstantArtifact(
            tool_id=tool_id,
            tool_version=tool_version,
            execution_class="instant",
            runtime="python-3.12",
            capability_id="drawing.read",
            catalog_commit=catalog_commit,
            entrypoint=entrypoint,
            params_schema_digest=_canonical_digest(params_schema),
            limits=limits,
            code_digest=code_digest,
            artifact_digest=artifact_digest,
            source=source,
        )

    def _resolve_entry(self, entry: PurePosixPath) -> Path:
        try:
            root = self._root.resolve(strict=True)
        except OSError as exc:
            raise ArtifactResolutionError("trusted instant artifact root is unavailable") from exc
        if not root.is_dir():
            raise ArtifactResolutionError("trusted instant artifact root is not a directory")
        try:
            candidate = (root.joinpath(*entry.parts)).resolve(strict=True)
        except OSError as exc:
            raise ArtifactResolutionError("instant artifact is unavailable") from exc
        if not _inside(candidate, root) or not candidate.is_file():
            raise ArtifactResolutionError("instant artifact is outside the trusted platform root")
        return candidate

    def _read_normalized(self, path: Path) -> bytes:
        try:
            if path.stat().st_size > self._max_artifact_bytes:
                raise ArtifactResolutionError("instant artifact exceeds the size limit")
            raw = path.read_bytes()
        except OSError as exc:
            raise ArtifactResolutionError("instant artifact is unavailable") from exc
        if len(raw) > self._max_artifact_bytes:
            raise ArtifactResolutionError("instant artifact exceeds the size limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactResolutionError("instant artifact must be valid UTF-8") from exc
        if "\x00" in text:
            raise ArtifactResolutionError("instant artifact must not contain NUL bytes")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        if len(normalized) > self._max_artifact_bytes:
            raise ArtifactResolutionError("instant artifact exceeds the size limit")
        return normalized
