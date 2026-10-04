"""Immutable G33 source admission, without geometry conversion or head changes."""
import hashlib

import solar_artifacts
import solar_pvcase_conversion
import write_loop
from envelopes import ErrorCode
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context

SOURCE_TOOL = "solar-pvcase-g33-source"
SOURCE_KIND = "pvcase-g33"
SOURCE_MEDIA_TYPE = "application/json"
SOURCE_FILENAME = "pvcase-g33-source.json"
RESULT_SCHEMA = "leaf.pvcase-g33-source.v1"
MAX_IMPORT_G33_BYTES = solar_artifacts.MAX_ARTIFACT_BYTES
MAX_PROJECT_ID_CHARS = 100

REFUSALS = {
    "PVS_DRAWING_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "PVS_PROJECT_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "PVS_MEDIA_TYPE_REFUSED": (415, ErrorCode.BAD_PARAMS, False),
    "PVG_INVALID_JSON": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_INPUT_BYTES_EXCEEDED": (413, ErrorCode.BAD_PARAMS, False),
    "PVG_ENVELOPE_FIELDS": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_ENVELOPE_SCHEMA": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_INVALID_INTAKE": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_LIST_LIMIT": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_DEPTH_LIMIT": (400, ErrorCode.BAD_PARAMS, False),
    "PVG_NODE_LIMIT": (400, ErrorCode.BAD_PARAMS, False),
    "PVS_DRAWING_NOT_FOUND": (404, ErrorCode.BAD_PARAMS, False),
    "PVS_GRAPH_REQUIRED": (409, ErrorCode.BAD_PARAMS, False),
    "PVS_PROJECT_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "PVS_SOURCE_ID_INVALID": (400, ErrorCode.BAD_PARAMS, False),
    "PVS_SOURCE_NOT_FOUND": (404, ErrorCode.BAD_PARAMS, False),
    "PVS_SOURCE_KIND_MISMATCH": (409, ErrorCode.BAD_PARAMS, False),
    "PVS_WRITES_DRAINED": (503, ErrorCode.INTERNAL, True),
    "PVS_STORE_UNAVAILABLE": (503, ErrorCode.INTERNAL, True),
    "PVS_SOURCE_CONFLICT": (500, ErrorCode.INTERNAL, False),
    "PVS_SOURCE_CORRUPT": (500, ErrorCode.INTERNAL, False),
    "PVS_SOURCE_INVALID": (500, ErrorCode.INTERNAL, False),
}
SOURCE_CODES = frozenset(REFUSALS)
SHARED_CODES = frozenset({"UNAUTHENTICATED", "FORBIDDEN", "ENTITLEMENT_REQUIRED", "INTERNAL", "BAD_PARAMS"})
CODES = SOURCE_CODES | SHARED_CODES
_VALIDATION_CODES = frozenset(code for code in SOURCE_CODES if code.startswith("PVG_"))


class PvcaseSourceError(ValueError):
    def __init__(self, code):
        self.code = code if code in SOURCE_CODES else "PVS_SOURCE_INVALID"
        super().__init__(self.code)


def _artifact_refusal(code):
    return {
        "ARTIFACT_WRITES_DRAINED": "PVS_WRITES_DRAINED",
        "ARTIFACT_STORE_UNAVAILABLE": "PVS_STORE_UNAVAILABLE",
        "ARTIFACT_CONFLICT": "PVS_SOURCE_CONFLICT",
        "ARTIFACT_NOT_FOUND": "PVS_SOURCE_NOT_FOUND",
        "ARTIFACT_ID_INVALID": "PVS_SOURCE_ID_INVALID",
        "ARTIFACT_CORRUPT": "PVS_SOURCE_CORRUPT",
        "ARTIFACT_VERIFY_MISMATCH": "PVS_SOURCE_CORRUPT",
    }.get(code, "PVS_SOURCE_INVALID")


def _project(project_id):
    if not (isinstance(project_id, str) and 1 <= len(project_id) <= MAX_PROJECT_ID_CHARS):
        raise PvcaseSourceError("PVS_PROJECT_ID_INVALID")


def inspect_pvcase_source(data: bytes) -> dict:
    if type(data) is not bytes:
        raise PvcaseSourceError("PVG_INVALID_JSON")
    if len(data) > MAX_IMPORT_G33_BYTES:
        raise PvcaseSourceError("PVG_INPUT_BYTES_EXCEEDED")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise PvcaseSourceError("PVG_INVALID_JSON") from None
    try:
        envelope = solar_pvcase_conversion.validate_envelope(data)
    except GraphValidationError as exc:
        raise PvcaseSourceError(exc.code if exc.code in _VALIDATION_CODES else "PVS_SOURCE_INVALID") from None
    except Exception:
        raise PvcaseSourceError("PVS_SOURCE_INVALID") from None
    stack = [envelope]
    try:
        while stack:
            value = stack.pop()
            if isinstance(value, str):
                value.encode("utf-8", errors="strict")
            elif isinstance(value, dict):
                stack.extend(value.keys())
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
    except UnicodeError:
        raise PvcaseSourceError("PVG_INVALID_JSON") from None
    return envelope


def import_pvcase_source(backend, tenant_id, drawing_id, data, *, project_id=None) -> dict:
    if write_loop.drawing_mutations_refusal() is not None:
        raise PvcaseSourceError("PVS_WRITES_DRAINED")
    if project_id is not None:
        _project(project_id)
    inspect_pvcase_source(data)
    try:
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=project_id)
    except GraphValidationError as exc:
        raise PvcaseSourceError({"PROJECT_MISMATCH": "PVS_PROJECT_MISMATCH",
                                 "GRAPH_CONTEXT_UNAVAILABLE": "PVS_DRAWING_NOT_FOUND"}.get(
                                     exc.code, "PVS_GRAPH_REQUIRED")) from None
    except (OSError, RuntimeError):
        raise PvcaseSourceError("PVS_STORE_UNAVAILABLE") from None
    except Exception:
        raise PvcaseSourceError("PVS_SOURCE_INVALID") from None
    sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context,
                                       SOURCE_TOOL, hashlib.sha256(data).hexdigest(), False)
    try:
        prepared = sink.prepare(solar_artifacts.ArtifactOutput(
            {}, SOURCE_MEDIA_TYPE, SOURCE_FILENAME, data))
        ref = sink.finish(prepared)
    except GraphValidationError as exc:
        raise PvcaseSourceError(_artifact_refusal(exc.code)) from None
    except (OSError, RuntimeError):
        raise PvcaseSourceError("PVS_STORE_UNAVAILABLE") from None
    except Exception:
        raise PvcaseSourceError("PVS_SOURCE_INVALID") from None
    return {"schema": RESULT_SCHEMA, "kind": SOURCE_KIND, "drawing_id": drawing_id,
            "project_id": context["project_id"], "source_version": context["resolved_version"],
            "graph_rev": context["graph"]["rev"], "graph_sha256": context["graph_sha256"], "source": ref}


def load_pvcase_source(backend, tenant_id, drawing_id, artifact_id, *, project_id) -> tuple[dict, bytes, dict]:
    _project(project_id)
    try:
        meta, content = solar_artifacts.read_artifact(
            backend, tenant_id, drawing_id, artifact_id, require_head=False)
    except GraphValidationError as exc:
        raise PvcaseSourceError(_artifact_refusal(exc.code)) from None
    except (OSError, RuntimeError):
        raise PvcaseSourceError("PVS_STORE_UNAVAILABLE") from None
    except Exception:
        raise PvcaseSourceError("PVS_SOURCE_INVALID") from None
    if meta["tenant_id"] != tenant_id or meta["drawing_id"] != drawing_id:
        raise PvcaseSourceError("PVS_SOURCE_NOT_FOUND")
    if (meta["tool"] != SOURCE_TOOL or meta["media_type"] != SOURCE_MEDIA_TYPE
            or meta["filename"] != SOURCE_FILENAME):
        raise PvcaseSourceError("PVS_SOURCE_KIND_MISMATCH")
    if meta["project_id"] != project_id:
        raise PvcaseSourceError("PVS_PROJECT_MISMATCH")
    if hashlib.sha256(content).hexdigest() != meta["content_sha256"] or meta["content_sha256"] != meta["request_sha256"]:
        raise PvcaseSourceError("PVS_SOURCE_CORRUPT")
    try:
        envelope = inspect_pvcase_source(content)
    except PvcaseSourceError:
        raise PvcaseSourceError("PVS_SOURCE_CORRUPT") from None
    return meta, content, envelope
