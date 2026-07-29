"""Fail-closed verification and caching for immutable executor artifacts."""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
import threading
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


@dataclass(frozen=True)
class ArtifactReference:
    """The complete identity required to resolve an executable artifact."""

    tenant_id: str
    catalog_version: str
    artifact_digest: str
    code_digest: str


@dataclass(frozen=True)
class SignedArtifact:
    """Artifact bytes and their detached Ed25519 signature."""

    reference: ArtifactReference
    bytes: bytes
    signing_key_id: str
    signature: bytes


class ArtifactRegistryError(ValueError):
    """A registry artifact cannot safely be loaded."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ImmutableArtifactRegistry:
    """Resolve only registered, signed, unrevoked artifact bytes.

    This in-memory implementation models a trusted registry boundary. Callers
    supply every immutable identity field, and cache entries are keyed by that
    full tuple so bytes never cross tenants or catalog versions.
    """

    def __init__(self, artifacts: tuple[SignedArtifact, ...], signing_keys: Mapping[str, bytes], *,
                 revoked_artifact_digests: frozenset[str] = frozenset(), cache_max_entries: int = 256) -> None:
        if cache_max_entries < 1:
            raise ValueError("cache_max_entries must be positive")
        self._artifacts = {artifact.reference: artifact for artifact in artifacts}
        if len(self._artifacts) != len(artifacts):
            raise ValueError("artifact references must be unique")
        self._signing_keys = dict(signing_keys)
        self._static_references = frozenset(self._artifacts)
        self._revoked_artifact_digests = frozenset(revoked_artifact_digests)
        self._cache: OrderedDict[ArtifactReference, bytes] = OrderedDict()
        self._cache_max_entries = cache_max_entries
        self._lock = threading.RLock()
        self.verification_count = 0

    def resolve(self, reference: ArtifactReference) -> bytes:
        """Return verified bytes, or fail before any runtime load occurs."""
        with self._lock:
            return self._resolve_locked(reference)

    def _resolve_locked(self, reference: ArtifactReference) -> bytes:
        if reference.artifact_digest in self._revoked_artifact_digests:
            raise ArtifactRegistryError("ARTIFACT_REVOKED", "artifact is revoked")
        cached = self._cache.get(reference)
        if cached is not None:
            self._cache.move_to_end(reference)
            return cached
        artifact = self._artifacts.get(reference)
        if artifact is None:
            raise ArtifactRegistryError("ARTIFACT_NOT_FOUND", "artifact identity is not registered")
        key = self._signing_keys.get(artifact.signing_key_id)
        if key is None or not _verify(key, self._signed_payload(artifact), artifact.signature):
            raise ArtifactRegistryError("ARTIFACT_SIGNATURE_INVALID", "artifact signature is invalid")
        self.verification_count += 1
        bytes_digest = _digest(artifact.bytes)
        if bytes_digest != reference.code_digest:
            raise ArtifactRegistryError("CODE_DIGEST_MISMATCH", "artifact bytes do not match code digest")
        if bytes_digest != reference.artifact_digest:
            raise ArtifactRegistryError("ARTIFACT_DIGEST_MISMATCH", "artifact bytes do not match artifact digest")
        self._cache[reference] = artifact.bytes
        self._cache.move_to_end(reference)
        while len(self._cache) > self._cache_max_entries:
            evicted, _ = self._cache.popitem(last=False)
            if evicted not in self._static_references:
                self._artifacts.pop(evicted, None)
        return artifact.bytes

    def resolve_signed(self, artifact: SignedArtifact) -> bytes:
        """Verify and cache one signed assignment-time artifact envelope.

        The control plane transports this envelope only while binding a
        session. Invocation requests never carry code bytes or consult the
        registry. A previously registered identity cannot be replaced with
        different signed material.
        """
        with self._lock:
            existing = self._artifacts.get(artifact.reference)
            if existing is not None and existing != artifact:
                raise ArtifactRegistryError(
                    "ARTIFACT_IDENTITY_CONFLICT",
                    "artifact identity is already bound to different signed material",
                )
            self._artifacts[artifact.reference] = artifact
            try:
                return self._resolve_locked(artifact.reference)
            except Exception:
                if existing is None:
                    self._artifacts.pop(artifact.reference, None)
                raise

    @staticmethod
    def _signed_payload(artifact: SignedArtifact) -> bytes:
        reference = artifact.reference
        return b"\n".join((
            b"leaf.instant-execution/artifact/v1",
            reference.tenant_id.encode("utf-8"),
            reference.catalog_version.encode("utf-8"),
            reference.artifact_digest.encode("ascii"),
            reference.code_digest.encode("ascii"),
            artifact.bytes,
        ))


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
