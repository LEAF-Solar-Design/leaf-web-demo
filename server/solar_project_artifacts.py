"""Immutable Solar output files bound to exact canonical project versions."""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import UUID

import solar_artifacts as standalone
import solar_project_admission as policy
import solar_project_context as project
import solar_project_graph as graph_adapter
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_solve_results import require_current_export

MAX_ARTIFACT_BYTES = standalone.MAX_ARTIFACT_BYTES
MAX_META_BYTES = standalone.MAX_META_BYTES
BINDING_SCHEMA = "leaf.solar-project-artifact-binding.v1"
ARTIFACT_SCHEMA = "leaf.solar-project-artifact.v1"
REF_SCHEMA = "leaf.solar-project-artifact-ref.v1"
_IDS = ("organization_id", "project_id", "drawing_id", "source_version_id")
_HASHES = ("source_intake_sha256", "graph_sha256", "request_sha256")
_BINDING_KEYS = frozenset(("schema", *_IDS, *_HASHES, "tool", "tool_manifest_sha256"))
_META_KEYS = _BINDING_KEYS | {"artifact_id", "media_type", "filename", "byte_length", "content_sha256"}
_REF_KEYS = ("organization_id", "project_id", "drawing_id", "source_version_id",
             "source_intake_sha256", "graph_sha256", "artifact_id", "media_type",
             "filename", "byte_length", "content_sha256")
_ELECTRICAL_EXPORTS = frozenset(("solar-electrical-schedules", "solar-cable-export"))


def _fail(code):
    raise GraphValidationError(code)


def _binding_valid(binding):
    try:
        return (type(binding) is dict and set(binding) == _BINDING_KEYS
                and binding["schema"] == BINDING_SCHEMA
                and all(type(binding[key]) is str and str(UUID(binding[key])) == binding[key]
                        for key in _IDS)
                and all(standalone._hex(binding[key]) for key in _HASHES)
                and type(binding["tool_manifest_sha256"]) is str
                and re.fullmatch(r"sha256:[0-9a-f]{64}", binding["tool_manifest_sha256"]) is not None
                and type(binding["tool"]) is str
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", binding["tool"]) is not None)
    except (ValueError, TypeError, AttributeError, KeyError):
        return False


def project_artifact_binding(context, tool_name, tool_manifest_sha256, request_sha256):
    if isinstance(context, project.VerifiedProjectContext):
        context = graph_adapter.resolve_project_graph_context(context)
    if not isinstance(context, graph_adapter.ProjectGraphContext):
        _fail("ARTIFACT_INVALID")
    binding = {"schema": BINDING_SCHEMA,
        "organization_id": str(context.organization_id), "project_id": str(context.project_id),
        "drawing_id": str(context.drawing_id), "source_version_id": str(context.parent_version_id),
        "source_intake_sha256": context.intake_sha256, "graph_sha256": context.graph_sha256,
        "tool": tool_name, "tool_manifest_sha256": tool_manifest_sha256,
        "request_sha256": request_sha256}
    if not _binding_valid(binding):
        _fail("ARTIFACT_INVALID")
    return binding


def _key(scope, digest, *, blob=False):
    if not standalone._hex(digest):
        _fail("ARTIFACT_ID_INVALID")
    prefix = (f"tenants/{scope['organization_id']}/projects/{scope['project_id']}/"
              f"drawings/{scope['drawing_id']}/solar-artifacts/")
    return prefix + ("blobs/" + digest + ".bin" if blob else digest + ".json")


def _backend(org):
    try:
        return write_loop.upload_backend_for_tenant(str(org))
    except Exception:
        _fail("ARTIFACT_STORE_UNAVAILABLE")


@dataclass(frozen=True)
class _Prepared:
    ref: dict
    meta: dict
    meta_bytes: bytes
    content: bytes


class ProjectArtifactSink:
    def __init__(self, backend, binding):
        if not _binding_valid(binding):
            _fail("ARTIFACT_INVALID")
        self.backend = backend
        self.binding = deepcopy(binding)
        self.prepared = None

    def prepare(self, value):
        standalone._validate(value)
        artifact_id = sha256(canonical_bytes(self.binding)).hexdigest()
        meta = dict(self.binding, schema=ARTIFACT_SCHEMA, artifact_id=artifact_id,
                    media_type=value.media_type, filename=value.filename,
                    byte_length=len(value.content), content_sha256=sha256(value.content).hexdigest())
        raw = canonical_bytes(meta)
        if len(raw) > MAX_META_BYTES:
            _fail("ARTIFACT_INVALID")
        ref = {key: meta[key] for key in _REF_KEYS}
        ref.update(schema=REF_SCHEMA, download=(
            f"/api/projects/{meta['project_id']}/drawings/{meta['drawing_id']}/"
            f"solar-artifacts/{artifact_id}"))
        return _Prepared(ref, meta, raw, value.content)

    def finish(self, prepared):
        """Keep the candidate in memory until final authorization and size checks."""
        self.prepared = prepared
        return prepared.ref

    def commit(self):
        prepared = self.prepared
        if prepared is None:
            return None
        if write_loop.drawing_mutations_refusal() is not None:
            _fail("ARTIFACT_WRITES_DRAINED")
        backend = self.backend if self.backend is not None else _backend(self.binding["organization_id"])
        try:
            backend.put_if_absent_or_verify(
                _key(self.binding, prepared.meta["content_sha256"], blob=True), prepared.content)
            backend.put_if_absent_or_verify(
                _key(self.binding, prepared.meta["artifact_id"]), prepared.meta_bytes)
        except standalone.store.ImmutableConflict:
            _fail("ARTIFACT_CONFLICT")
        except Exception:
            _fail("ARTIFACT_STORE_UNAVAILABLE")
        return prepared.ref


def _metadata(raw, scope, artifact_id):
    try:
        if type(raw) is not bytes or len(raw) > MAX_META_BYTES:
            raise ValueError()
        meta = json.loads(raw.decode("utf-8"), object_pairs_hook=project._object_pairs,
                          parse_constant=project._nonfinite, parse_float=project._json_float)
        if type(meta) is not dict or set(meta) != _META_KEYS or canonical_bytes(meta) != raw:
            raise ValueError()
        binding = {key: meta[key] for key in _BINDING_KEYS}
        binding["schema"] = BINDING_SCHEMA
        if (meta["schema"] != ARTIFACT_SCHEMA or not _binding_valid(binding)
                or any(meta[key] != value for key, value in scope.items())
                or sha256(canonical_bytes(binding)).hexdigest() != artifact_id
                or meta["artifact_id"] != artifact_id or not standalone._hex(meta["content_sha256"])
                or type(meta["byte_length"]) is not int
                or not 1 <= meta["byte_length"] <= MAX_ARTIFACT_BYTES):
            raise ValueError()
        standalone._media_filename(meta["media_type"], meta["filename"])
        return meta
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, GraphValidationError):
        _fail("ARTIFACT_CORRUPT")


def _source(tenant, project_id, drawing_id, meta):
    verified = project.resolve_context(tenant, project_id, UUID(meta["source_version_id"]),
                                       drawing_id=drawing_id, write=False)
    if verified.intake_sha256 != meta["source_intake_sha256"]:
        _fail("SIP_R6_ARTIFACT_SOURCE_MISMATCH")
    try:
        context = graph_adapter.resolve_project_graph_context(verified)
    except GraphValidationError:
        _fail("SIP_R6_ARTIFACT_SOURCE_MISMATCH")
    if ((str(context.organization_id), str(context.project_id), str(context.drawing_id),
         str(context.parent_version_id)) != tuple(meta[key] for key in _IDS)
            or context.graph_sha256 != meta["graph_sha256"] or context.graph is None):
        _fail("SIP_R6_ARTIFACT_SOURCE_MISMATCH")
    return verified, context


def read_project_artifact(tenant, project_id, drawing_id, artifact_id, *, current=False):
    if not policy.project_runs_enabled():
        _fail("project_execution_disabled")
    if (not isinstance(project_id, UUID) or not isinstance(drawing_id, UUID)
            or not standalone._hex(artifact_id) or type(current) is not bool):
        _fail("ARTIFACT_ID_INVALID")
    org, _actor = project._access(tenant, project_id, write=False)
    scope = {"organization_id": str(org), "project_id": str(project_id), "drawing_id": str(drawing_id)}
    backend = _backend(org)
    try:
        raw = backend.get(_key(scope, artifact_id))
    except KeyError:
        _fail("ARTIFACT_NOT_FOUND")
    except Exception:
        _fail("ARTIFACT_STORE_UNAVAILABLE")
    meta = _metadata(raw, scope, artifact_id)
    _source(tenant, project_id, drawing_id, meta)
    try:
        content = backend.get(_key(scope, meta["content_sha256"], blob=True))
    except KeyError:
        _fail("ARTIFACT_CORRUPT")
    except Exception:
        _fail("ARTIFACT_STORE_UNAVAILABLE")
    if (type(content) is not bytes or len(content) != meta["byte_length"]
            or sha256(content).hexdigest() != meta["content_sha256"]):
        _fail("ARTIFACT_CORRUPT")
    verified, context = _source(tenant, project_id, drawing_id, meta)
    if current:
        if not verified.binding.is_head or str(verified.binding.head_version_id) != meta["source_version_id"]:
            _fail("ARTIFACT_STALE")
        if meta["tool"] in _ELECTRICAL_EXPORTS:
            require_current_export(context.graph)
    return meta, content
