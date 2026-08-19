"""Exact version receipts on every CAD artifact (card C1-4).

A receipt is the one honest proof of what bytes an artifact version actually
contains: ``(tenant_id, project_id, artifact_id, version) -> sha256 digest``.
Every write (upload or derive) mints an exact receipt for the version it
just created; every read must re-verify the bytes it got back against that
same receipt before trusting them — a caller that skips verify_read() has
not actually confirmed anything.

Receipts are immutable and append-only: once a (tenant, project, artifact,
version) slot is written, only a byte-identical rewrite of it is accepted
(an idempotent replay of the same upload/derive). Any attempt to bind that
same slot to different bytes raises ReceiptConflict instead of silently
overwriting history — the receipt ledger is the thing later audits and
digest re-checks trust, so it can never quietly change under them.

Every lookup is scoped by (tenant_id, project_id): the ledger keys on the
full tuple, so a tenant or project that never wrote a receipt can never read
one back, even if it guesses another tenant's artifact_id and version.

Standalone module: this is the receipt ledger itself, not yet wired into any
upload/derive/read route. Callers construct their own CadVersionStore.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

UPLOAD = "upload"
DERIVE = "derive"
_KINDS = frozenset({UPLOAD, DERIVE})

_ScopeKey = Tuple[str, str, str]


class CadVersionError(Exception):
    """Base error for the CAD version receipt ledger."""


class UnknownArtifact(CadVersionError):
    """No receipt exists for this (tenant, project, artifact) at all."""


class VersionNotFound(CadVersionError):
    """The (tenant, project, artifact) exists, but not this version."""


class ReceiptConflict(CadVersionError):
    """A write tried to bind an existing receipt slot to different bytes."""


class DigestMismatch(CadVersionError):
    """Bytes read back do not match the digest the receipt named at write time."""

    def __init__(self, artifact_id: str, version: int, expected: str, actual: str):
        super().__init__(
            f"artifact {artifact_id!r} v{version} digest mismatch: "
            f"expected {expected}, got {actual}"
        )
        self.artifact_id = artifact_id
        self.version = version
        self.expected_digest = expected
        self.actual_digest = actual


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_scope_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CadVersionError(f"{field} must be a non-empty string")
    return value


def _require_bytes(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise CadVersionError("artifact data must be bytes")
    return bytes(data)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ArtifactReceipt:
    """One immutable proof: this exact artifact_id/version names this digest."""

    tenant_id: str
    project_id: str
    artifact_id: str
    version: int
    digest: str
    kind: str
    byte_length: int
    created_at: str
    parent_version: Optional[int] = None


class CadVersionStore:
    """Append-only, tenant+project-scoped ledger of artifact version receipts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._receipts: Dict[_ScopeKey, Dict[int, ArtifactReceipt]] = {}

    # ------------------------------------------------------------------ #
    # writes
    # ------------------------------------------------------------------ #
    def record_upload(
        self, tenant_id: str, project_id: str, artifact_id: str, data: bytes,
    ) -> ArtifactReceipt:
        """Mint the version-1 receipt for a brand-new artifact."""
        return self._record(
            tenant_id, project_id, artifact_id, data,
            kind=UPLOAD, version=1, parent_version=None,
        )

    def record_derive(
        self, tenant_id: str, project_id: str, artifact_id: str,
        parent_version: int, data: bytes,
    ) -> ArtifactReceipt:
        """Mint the next version, chained to the exact parent it derived from.

        The parent version must already have a receipt in THIS scope — a
        derive can never invent a lineage the ledger never recorded.
        """
        if (
            isinstance(parent_version, bool)
            or not isinstance(parent_version, int)
            or parent_version < 1
        ):
            raise CadVersionError("parent_version must be a positive int")
        # Confirms the parent receipt exists (and scope) before hashing —
        # a derive against a missing parent must fail before it commits
        # anything, not race a concurrent write for the slot below.
        self.get_receipt(tenant_id, project_id, artifact_id, parent_version)
        return self._record(
            tenant_id, project_id, artifact_id, data,
            kind=DERIVE, version=parent_version + 1, parent_version=parent_version,
        )

    def _record(
        self, tenant_id: str, project_id: str, artifact_id: str, data: bytes,
        *, kind: str, version: int, parent_version: Optional[int],
    ) -> ArtifactReceipt:
        assert kind in _KINDS
        _require_scope_id(tenant_id, "tenant_id")
        _require_scope_id(project_id, "project_id")
        _require_scope_id(artifact_id, "artifact_id")
        payload = _require_bytes(data)
        digest = _sha256_hex(payload)
        scope: _ScopeKey = (tenant_id, project_id, artifact_id)
        receipt = ArtifactReceipt(
            tenant_id=tenant_id, project_id=project_id, artifact_id=artifact_id,
            version=version, digest=digest, kind=kind, byte_length=len(payload),
            created_at=_now_iso(), parent_version=parent_version,
        )
        with self._lock:
            versions = self._receipts.setdefault(scope, {})
            existing = versions.get(version)
            if existing is not None:
                if not hmac.compare_digest(existing.digest, digest):
                    raise ReceiptConflict(
                        f"receipt for {artifact_id!r} v{version} is immutable: "
                        f"existing digest {existing.digest} != new digest {digest}"
                    )
                # Byte-identical replay: the ORIGINAL receipt stays authoritative
                # (created_at included) — a resend never rewrites history.
                return existing
            versions[version] = receipt
            return receipt

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def get_receipt(
        self, tenant_id: str, project_id: str, artifact_id: str, version: int,
    ) -> ArtifactReceipt:
        scope: _ScopeKey = (tenant_id, project_id, artifact_id)
        with self._lock:
            versions = self._receipts.get(scope)
            if not versions:
                raise UnknownArtifact(
                    f"no artifact {artifact_id!r} in tenant={tenant_id!r} "
                    f"project={project_id!r}"
                )
            receipt = versions.get(version)
            if receipt is None:
                raise VersionNotFound(
                    f"no receipt for {artifact_id!r} v{version} in "
                    f"tenant={tenant_id!r} project={project_id!r}"
                )
            return receipt

    def latest_receipt(
        self, tenant_id: str, project_id: str, artifact_id: str,
    ) -> ArtifactReceipt:
        scope: _ScopeKey = (tenant_id, project_id, artifact_id)
        with self._lock:
            versions = self._receipts.get(scope)
            if not versions:
                raise UnknownArtifact(
                    f"no artifact {artifact_id!r} in tenant={tenant_id!r} "
                    f"project={project_id!r}"
                )
            return versions[max(versions)]

    def verify_read(
        self, tenant_id: str, project_id: str, artifact_id: str,
        version: int, data: bytes,
    ) -> ArtifactReceipt:
        """Re-verify bytes just read against the receipt's stored digest.

        Raises DigestMismatch on ANY divergence. A caller must treat the
        read as unverified until this returns without raising.
        """
        receipt = self.get_receipt(tenant_id, project_id, artifact_id, version)
        actual = _sha256_hex(_require_bytes(data))
        if not hmac.compare_digest(actual, receipt.digest):
            raise DigestMismatch(artifact_id, version, receipt.digest, actual)
        return receipt

    def list_versions(
        self, tenant_id: str, project_id: str, artifact_id: str,
    ) -> List[ArtifactReceipt]:
        scope: _ScopeKey = (tenant_id, project_id, artifact_id)
        with self._lock:
            versions = self._receipts.get(scope, {})
            return [versions[v] for v in sorted(versions)]
