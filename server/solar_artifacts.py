"""Revision-bound files for Solar read results.

Artifacts bind tenant, drawing, project, source version, graph, tool and request.
Immutable content and metadata make duplicate requests idempotent. Files are
limited to 16 MiB and a closed CSV, JSON, XLSX, KML, XML and PDF media list. Everything
lives under the drawing prefix and shares drawing retention. Validation and
readback fail closed; metadata is written only after its content is durable.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import re

import write_loop
import store
from leaf_cloud_client import canonical_bytes
from solar_sizing_client import digest
from solar_design_graph import GraphValidationError

MAX_ARTIFACT_BYTES = 16_777_216
MAX_META_BYTES = 4096
MEDIA_TYPES = {
    "text/csv": "csv",
    "application/json": "json",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.google-earth.kml+xml": "kml",
    "application/xml": "xml",
    "application/pdf": "pdf",
}
BINDING_SCHEMA = "leaf.solar-artifact-binding.v1"
ARTIFACT_SCHEMA = "leaf.solar-artifact.v1"
REF_SCHEMA = "leaf.solar-artifact-ref.v1"
_META_KEYS = {"schema", "tenant_id", "project_id", "drawing_id", "source_version",
              "graph_sha256", "tool", "request_sha256", "artifact_id", "media_type",
              "filename", "byte_length", "content_sha256"}


@dataclass(frozen=True)
class ArtifactOutput:
    summary: dict
    media_type: str
    filename: str
    content: bytes


@dataclass(frozen=True)
class _Prepared:
    meta: dict
    meta_bytes: bytes
    content: bytes
    ref: dict


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _constant(value):
    raise ValueError("nonfinite JSON constant")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def artifact_references(output):
    references = []
    if type(output) is dict:
        if output.get("schema") == REF_SCHEMA:
            references.append(output)
        for key, value in output.items():
            if key == "artifact" or (type(value) is dict and value.get("schema") == REF_SCHEMA):
                references.append(value)
    return references


def _media_filename(media_type, filename):
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
        raise GraphValidationError("ARTIFACT_MEDIA_TYPE_REFUSED")
    if (not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(csv|json|xlsx|kml|xml|pdf)", filename)
            or ".." in filename or filename.rsplit(".", 1)[1] != MEDIA_TYPES[media_type]):
        raise GraphValidationError("ARTIFACT_FILENAME_INVALID")


def _validate(value):
    if type(value.summary) is not dict or type(value.content) is not bytes or not value.content:
        raise GraphValidationError("ARTIFACT_INVALID")
    if len(value.content) > MAX_ARTIFACT_BYTES:
        raise GraphValidationError("ARTIFACT_TOO_LARGE")
    _media_filename(value.media_type, value.filename)
    extension = MEDIA_TYPES[value.media_type]
    try:
        if extension == "xlsx":
            if value.content[:4] != b"PK\x03\x04":
                raise ValueError()
        elif extension == "pdf":
            if not value.content[:1024].lstrip().startswith(b"%PDF-"):
                raise ValueError()
        else:
            text = value.content.decode("utf-8", errors="strict")
            if extension == "json":
                json.loads(text, parse_constant=_constant, parse_float=_finite_float)
            elif extension in ("kml", "xml"):
                if not text.removeprefix("\ufeff").lstrip().startswith("<"):
                    raise ValueError()
    except (ValueError, RecursionError):
        raise GraphValidationError("ARTIFACT_CONTENT_MISMATCH") from None


def artifact_binding(tenant_id, drawing_id, context, tool, request_sha256):
    try:
        store.sanitize_id(tenant_id)
        store.sanitize_id(drawing_id)
        project = context["project_id"]
        version = context["resolved_version"]
        graph_sha = context["graph_sha256"]
        if (not isinstance(project, str) or not 1 <= len(project) <= 100
                or type(version) is not int or not 1 <= version <= 99999999
                or not _hex(graph_sha) or not _hex(request_sha256)
                or not isinstance(tool, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", tool)):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise GraphValidationError("ARTIFACT_BINDING_INVALID") from None
    return {"schema": BINDING_SCHEMA, "tenant_id": tenant_id, "project_id": project,
            "drawing_id": drawing_id, "source_version": version, "graph_sha256": graph_sha,
            "tool": tool, "request_sha256": request_sha256}


def _key(tenant_id, drawing_id, sha, *, blob=False):
    if not _hex(sha):
        raise GraphValidationError("ARTIFACT_ID_INVALID")
    prefix = store.drawing_prefix(tenant_id, drawing_id) + "/artifacts/"
    return prefix + ("blobs/" + sha + ".bin" if blob else sha + ".json")


class ArtifactSink:
    def __init__(self, backend, tenant_id, drawing_id, context, tool, request_sha256, verify):
        self.backend = backend
        self.tenant_id = tenant_id
        self.drawing_id = drawing_id
        self.context = context
        self.tool = tool
        self.request_sha256 = request_sha256
        self.verify = verify

    def prepare(self, value):
        _validate(value)
        binding = artifact_binding(self.tenant_id, self.drawing_id, self.context,
                                   self.tool, self.request_sha256)
        artifact_id = digest(binding)
        meta = dict(binding, schema=ARTIFACT_SCHEMA, artifact_id=artifact_id,
                    media_type=value.media_type, filename=value.filename,
                    byte_length=len(value.content), content_sha256=hashlib.sha256(value.content).hexdigest())
        ref = {key: meta[key] for key in ("artifact_id", "media_type", "filename", "byte_length",
                                        "content_sha256", "source_version")}
        ref.update(schema=REF_SCHEMA,
                   download=f"/api/drawings/{self.drawing_id}/artifacts/{artifact_id}")
        return _Prepared(meta, canonical_bytes(meta), value.content, ref)

    def finish(self, prepared):
        if not self.verify:
            return store_artifact(self.backend, prepared)
        meta, content = read_artifact(self.backend, self.tenant_id, self.drawing_id,
                                      prepared.meta["artifact_id"])
        if meta != prepared.meta or content != prepared.content:
            raise GraphValidationError("ARTIFACT_VERIFY_MISMATCH")
        return prepared.ref


    def verify_reference(self, reference):
        if type(reference) is not dict:
            raise GraphValidationError("ARTIFACT_VERIFY_MISMATCH")
        meta, _ = read_artifact(self.backend, self.tenant_id, self.drawing_id,
                                reference.get("artifact_id"))
        binding = artifact_binding(self.tenant_id, self.drawing_id, self.context,
                                   self.tool, self.request_sha256)
        if any(meta[key] != value for key, value in binding.items() if key != "schema"):
            raise GraphValidationError("ARTIFACT_VERIFY_MISMATCH")
        expected = {key: meta[key] for key in ("artifact_id", "media_type", "filename", "byte_length",
                                             "content_sha256", "source_version")}
        expected.update(schema=REF_SCHEMA,
                        download=f"/api/drawings/{self.drawing_id}/artifacts/{meta['artifact_id']}")
        if reference != expected:
            raise GraphValidationError("ARTIFACT_VERIFY_MISMATCH")


def store_artifact(backend, prepared):
    if write_loop.drawing_mutations_refusal() is not None:
        raise GraphValidationError("ARTIFACT_WRITES_DRAINED")
    meta = prepared.meta
    try:
        backend.put_if_absent_or_verify(
            _key(meta["tenant_id"], meta["drawing_id"], meta["content_sha256"], blob=True), prepared.content)
        backend.put_if_absent_or_verify(
            _key(meta["tenant_id"], meta["drawing_id"], meta["artifact_id"]), prepared.meta_bytes)
    except store.ImmutableConflict:
        raise GraphValidationError("ARTIFACT_CONFLICT") from None
    except (OSError, RuntimeError, ValueError):
        raise GraphValidationError("ARTIFACT_STORE_UNAVAILABLE") from None
    return prepared.ref


def read_artifact(backend, tenant_id, drawing_id, artifact_id, *, require_head=False):
    try:
        store.sanitize_id(tenant_id)
        store.sanitize_id(drawing_id)
        if not _hex(artifact_id):
            raise ValueError()
    except (ValueError, TypeError):
        raise GraphValidationError("ARTIFACT_ID_INVALID") from None
    try:
        raw = backend.get(_key(tenant_id, drawing_id, artifact_id))
    except KeyError:
        raise GraphValidationError("ARTIFACT_NOT_FOUND") from None
    except (OSError, RuntimeError):
        raise GraphValidationError("ARTIFACT_STORE_UNAVAILABLE") from None
    try:
        if len(raw) > MAX_META_BYTES:
            raise ValueError()
        meta = json.loads(raw.decode("utf-8", errors="strict"), parse_constant=_constant,
                          parse_float=_finite_float)
        if type(meta) is not dict or set(meta) != _META_KEYS or canonical_bytes(meta) != raw:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise GraphValidationError("ARTIFACT_CORRUPT") from None
    if meta["tenant_id"] != tenant_id or meta["drawing_id"] != drawing_id:
        raise GraphValidationError("ARTIFACT_NOT_FOUND")
    try:
        binding = artifact_binding(tenant_id, drawing_id,
                                   {"project_id": meta["project_id"], "resolved_version": meta["source_version"],
                                    "graph_sha256": meta["graph_sha256"]}, meta["tool"], meta["request_sha256"])
        _media_filename(meta["media_type"], meta["filename"])
        if (meta["schema"] != ARTIFACT_SCHEMA or digest(binding) != artifact_id
                or meta["artifact_id"] != artifact_id or type(meta["byte_length"]) is not int
                or not 1 <= meta["byte_length"] <= MAX_ARTIFACT_BYTES or not _hex(meta["content_sha256"])):
            raise ValueError()
    except (ValueError, TypeError):
        raise GraphValidationError("ARTIFACT_CORRUPT") from None
    if require_head:
        try:
            head = store.load_manifest(backend, tenant_id, drawing_id)["head"]
        except (OSError, RuntimeError):
            raise GraphValidationError("ARTIFACT_STORE_UNAVAILABLE") from None
        except (KeyError, ValueError, TypeError):
            raise GraphValidationError("ARTIFACT_CORRUPT") from None
        if head != meta["source_version"]:
            raise GraphValidationError("ARTIFACT_STALE")
    try:
        content = backend.get(_key(tenant_id, drawing_id, meta["content_sha256"], blob=True))
    except KeyError:
        raise GraphValidationError("ARTIFACT_CORRUPT") from None
    except (OSError, RuntimeError):
        raise GraphValidationError("ARTIFACT_STORE_UNAVAILABLE") from None
    if (type(content) is not bytes or len(content) != meta["byte_length"]
            or hashlib.sha256(content).hexdigest() != meta["content_sha256"]):
        raise GraphValidationError("ARTIFACT_CORRUPT")
    return meta, content
