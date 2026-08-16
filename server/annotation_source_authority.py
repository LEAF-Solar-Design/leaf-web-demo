"""Server-owned Git source authority for durable annotation mutations.

The HTTP caller may name immutable object ids, but it cannot select a
repository, ref, root, or Git implementation. The repository comes from the
durable project mapping. The private harness verifies the Git relationship
under the same project-repository lease used by repository writers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Any

import requests

import broker_client
import platform_link

platform_link._ensure_platform_package()
from leaf_platform.annotation_source import (  # noqa: E402
    SourceVerificationRequest,
    VerifiedSourceReceipt,
)


class SourceAuthorityUnavailable(RuntimeError):
    """The repository or its immutable Git relationship cannot be proved."""


_CONTRACT = "leaf.project-repository-source-witness.v1"
_PATH = "/internal/project-repository-source/verify"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _canonical_json(value: dict[str, str]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _digest(value: dict[str, str]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _closed_json(raw: bytes) -> dict[str, Any]:
    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate response field")
            value[key] = item
        return value

    decoded = raw.decode("utf-8", errors="strict")
    value = json.loads(decoded, object_pairs_hook=closed_object)
    if not isinstance(value, dict):
        raise ValueError("response must be an object")
    return value


class AnnotationSourceAuthority:
    """Recompute mapped authority and ask the private harness for a Git witness."""

    def _mapped_repository(self, request: SourceVerificationRequest) -> str:
        try:
            mapping = platform_link.platform_store().resolve_project_repository_authority(
                request.tenant_id, request.org_id, request.project_id,
            )
        except Exception as exc:
            raise SourceAuthorityUnavailable("annotation source unavailable") from exc
        repository = str((mapping or {}).get("repo_key") or "")
        if not repository or repository != request.repository_id:
            raise SourceAuthorityUnavailable("annotation source unavailable")
        return repository

    def _wire_request(self, request: SourceVerificationRequest) -> dict[str, str]:
        if (
            request.tenant_id != request.org_id
            or not all(_canonical_uuid(value) for value in (
                request.tenant_id, request.org_id, request.project_id,
                request.drawing_id, request.repository_id,
            ))
            or not all(_SHA.fullmatch(value or "") for value in (
                request.commit_sha, request.tree_sha,
                request.base_commit, request.base_tree,
            ))
        ):
            raise SourceAuthorityUnavailable("annotation source unavailable")

        repository = self._mapped_repository(request)
        authority = {
            "tenant_id": request.tenant_id,
            "organization_id": request.org_id,
            "project_id": request.project_id,
            "repo_key": repository,
        }
        if request.relation == "preview":
            if request.reverses_commit is not None or request.reverses_tree is not None:
                raise SourceAuthorityUnavailable("annotation source unavailable")
            return {
                **authority,
                "relation": "preview",
                "base_commit": request.base_commit,
                "base_tree": request.base_tree,
                "candidate_commit": request.commit_sha,
                "candidate_tree": request.tree_sha,
            }
        if request.relation == "inverse":
            if (
                request.reverses_commit is None
                or request.reverses_tree is None
                or _SHA.fullmatch(request.reverses_commit) is None
                or _SHA.fullmatch(request.reverses_tree) is None
            ):
                raise SourceAuthorityUnavailable("annotation source unavailable")
            return {
                **authority,
                "relation": "inverse",
                "original_commit": request.reverses_commit,
                "original_tree": request.reverses_tree,
                "target_commit": request.base_commit,
                "target_tree": request.base_tree,
                "inverse_commit": request.commit_sha,
                "inverse_tree": request.tree_sha,
            }
        raise SourceAuthorityUnavailable("annotation source unavailable")

    def _verify(self, request: SourceVerificationRequest) -> str:
        if not isinstance(request, SourceVerificationRequest):
            raise SourceAuthorityUnavailable("annotation source unavailable")
        body = self._wire_request(request)
        expected_digest = _digest(body)
        base_url = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/")
        headers = broker_client.harness_headers()
        if not base_url or "X-Harness-Secret" not in headers:
            raise SourceAuthorityUnavailable("annotation source unavailable")
        headers = {**headers, "X-Tenant-Id": request.tenant_id}
        try:
            response = requests.post(
                f"{base_url}{_PATH}", json=body, headers=headers, timeout=10,
            )
            if response.status_code != 200:
                raise SourceAuthorityUnavailable("annotation source unavailable")
            payload = _closed_json(response.content)
        except SourceAuthorityUnavailable:
            raise
        except (requests.RequestException, UnicodeError, ValueError, TypeError) as exc:
            raise SourceAuthorityUnavailable("annotation source unavailable") from exc
        if (
            set(payload) != {"contract", "request_digest"}
            or payload.get("contract") != _CONTRACT
            or not isinstance(payload.get("request_digest"), str)
            or _DIGEST.fullmatch(payload["request_digest"]) is None
            or not hmac.compare_digest(payload["request_digest"], expected_digest)
        ):
            raise SourceAuthorityUnavailable("annotation source unavailable")
        return expected_digest

    def verify(self, request: SourceVerificationRequest) -> VerifiedSourceReceipt:
        return VerifiedSourceReceipt(request=request, receipt_digest=self._verify(request))

    def validate(
        self,
        receipt: VerifiedSourceReceipt,
        request: SourceVerificationRequest,
    ) -> bool:
        if (
            not isinstance(receipt, VerifiedSourceReceipt)
            or receipt.request != request
            or not isinstance(receipt.receipt_digest, str)
            or _DIGEST.fullmatch(receipt.receipt_digest) is None
        ):
            return False
        try:
            verified_digest = self._verify(request)
        except SourceAuthorityUnavailable:
            return False
        return hmac.compare_digest(receipt.receipt_digest, verified_digest)


SOURCE_AUTHORITY = AnnotationSourceAuthority()


def source_request(**values: Any) -> SourceVerificationRequest:
    """Build the canonical request type without exposing provider selection."""
    return SourceVerificationRequest(**values)
