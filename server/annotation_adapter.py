"""Pure P7 adapter between annotation previews and the durable store.

The adapter validates public identifiers and session ownership without reading
Git or PostgreSQL. Callers must resolve the session from the durable session
store and pass the resulting row here before invoking annotation_store.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional


class AnnotationAdapterError(ValueError):
    def __init__(self, code: str, status_code: int = 400, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail


_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AnnotationBatchRequest:
    tenant_id: str
    org_id: str
    project_id: str
    drawing_id: str
    session_id: str
    kind: str
    base_version: int
    base_commit: str
    base_tree: str
    preview_commit: str
    preview_tree: str
    payload_digest: str
    payload_count: int
    retry_of_batch_id: Optional[str] = None
    reverses_batch_id: Optional[str] = None


def _canonical_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AnnotationAdapterError("annotation_not_found", 404) from exc


def _git(value: Any, field: str) -> str:
    text = str(value or "")
    if not _SHA.fullmatch(text):
        raise AnnotationAdapterError("invalid_git_witness", 400, field)
    return text


def _batch_link(value: Optional[str]) -> Optional[str]:
    return _canonical_uuid(value) if value else None


def build_request(*, session: Optional[Mapping[str, Any]], tenant_id: str,
                  org_id: str, project_id: str, drawing_id: str, kind: str,
                  base_version: int, base_commit: str, base_tree: str,
                  preview_commit: str, preview_tree: str, payload_digest: str,
                  payload_count: int, retry_of_batch_id: Optional[str] = None,
                  reverses_batch_id: Optional[str] = None) -> AnnotationBatchRequest:
    """Return a normalized request only for the caller's owned active session.

    Unknown and foreign targets deliberately share ``annotation_not_found``.
    The durable store repeats these checks in its transaction.
    """
    tenant = _canonical_uuid(tenant_id)
    org = _canonical_uuid(org_id)
    project = _canonical_uuid(project_id)
    drawing = _canonical_uuid(drawing_id)
    if tenant != org:
        raise AnnotationAdapterError("annotation_not_found", 404)
    if not session or str(session.get("status") or "") != "active":
        raise AnnotationAdapterError("annotation_not_found", 404)
    session_id = str(session.get("session_id") or "")
    if (not session_id or str(session.get("tenant_id") or "") != tenant
            or str(session.get("drawing_id") or "") != drawing):
        raise AnnotationAdapterError("annotation_not_found", 404)
    if kind not in {"apply", "undo"}:
        raise AnnotationAdapterError("invalid_batch_kind", 400)
    retry = _batch_link(retry_of_batch_id)
    reverses = _batch_link(reverses_batch_id)
    if kind == "undo" and reverses is None:
        raise AnnotationAdapterError("undo_source_required", 400)
    if kind == "apply" and reverses is not None:
        raise AnnotationAdapterError("invalid_batch_link", 400)
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
        raise AnnotationAdapterError("invalid_base_version", 400)
    if not isinstance(payload_count, int) or isinstance(payload_count, bool) or payload_count < 1:
        raise AnnotationAdapterError("invalid_payload_count", 400)
    digest = str(payload_digest or "")
    if not _DIGEST.fullmatch(digest):
        raise AnnotationAdapterError("invalid_payload_digest", 400)
    return AnnotationBatchRequest(
        tenant_id=tenant, org_id=org, project_id=project, drawing_id=drawing,
        session_id=session_id, kind=kind, base_version=base_version,
        base_commit=_git(base_commit, "base_commit"),
        base_tree=_git(base_tree, "base_tree"),
        preview_commit=_git(preview_commit, "preview_commit"),
        preview_tree=_git(preview_tree, "preview_tree"),
        payload_digest=digest, payload_count=payload_count,
        retry_of_batch_id=retry, reverses_batch_id=reverses,
    )


def store_args(request: AnnotationBatchRequest) -> dict[str, Any]:
    """Project the validated request into annotation_store.create_batch args."""
    if not isinstance(request, AnnotationBatchRequest):
        raise TypeError("request must be an AnnotationBatchRequest")
    return {
        "tenant_id": request.tenant_id,
        "org_id": request.org_id,
        "project_id": request.project_id,
        "drawing_id": request.drawing_id,
        "session_id": request.session_id,
        "kind": request.kind,
        "base_version": request.base_version,
        "base_commit": request.base_commit,
        "base_tree": request.base_tree,
        "preview_commit": request.preview_commit,
        "preview_tree": request.preview_tree,
        "payload_digest": request.payload_digest,
        "payload_count": request.payload_count,
        "retry_of_batch_id": request.retry_of_batch_id,
        "reverses_batch_id": request.reverses_batch_id,
    }
