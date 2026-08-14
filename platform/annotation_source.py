"""Canonical source-authority types for durable annotation writes.

This module lives in the collision-safe ``leaf_platform`` package. Server
adapters re-export these exact objects so top-level and package-style server
imports cannot create incompatible receipt classes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceVerificationRequest:
    """Exact Git facts a trusted source authority must prove."""

    tenant_id: str
    org_id: str
    project_id: str
    drawing_id: str
    repository_id: str
    relation: str
    commit_sha: str
    tree_sha: str
    base_commit: str
    base_tree: str
    reverses_commit: Optional[str] = None
    reverses_tree: Optional[str] = None


@dataclass(frozen=True)
class VerifiedSourceReceipt:
    """Authority-issued receipt. The store revalidates it before every write."""

    request: SourceVerificationRequest
    receipt_digest: str


@runtime_checkable
class SourceAuthority(Protocol):
    """No default exists. Production must inject a server-owned verifier."""

    def verify(self, request: SourceVerificationRequest) -> VerifiedSourceReceipt: ...

    def validate(
        self,
        receipt: VerifiedSourceReceipt,
        request: SourceVerificationRequest,
    ) -> bool: ...
