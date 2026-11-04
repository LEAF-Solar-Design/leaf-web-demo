"""Exact canonical physical bindings and fenced, deferred publication.

No object is written before the lease and parent are checked under the drawing
row lock. Clock expiry before INSERT or a later transaction abort can leave at
most one state artifact, one source artifact and one intake per failed attempt.
These content-addressed objects are unreachable; Phase A has no orphan sweep.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

import solar_artifacts
import solar_civil_operations as civil
import solar_ground_terrain_adapter as adapter
import solar_landxml_import as landxml
import solar_local_graph as local
import solar_physical_head as ph
import solar_physical_state as ps
import solar_project_artifacts as artifacts
import solar_project_context as project
import solar_project_graph as graph
from solar_design_graph import GraphValidationError

REF_KEY = "solar_project_physical"
REF_SCHEMA = "leaf.solar-project-physical-ref.v1"
MANIFEST = "sha256:" + sha256(b"leaf.solar-project-physical.v1").hexdigest()
_REF_KEYS = frozenset(("schema", "index", "parent", "artifact_id", "content_sha256"))


def check_ref(value):
    if (type(value) is not dict or set(value) != _REF_KEYS
            or value["schema"] != REF_SCHEMA or type(value["index"]) is not int
            or not 0 <= value["index"] < ph.MAX_LOG_ENTRIES
            or (value["parent"] is not None and not ph._hex(value["parent"]))
            or not ph._hex(value["artifact_id"]) or not ph._hex(value["content_sha256"])):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT")
    return deepcopy(value)


def _view(binding, project_id, ref, state):
    return {"schema": ph.HEAD_SCHEMA, "drawing_id": str(binding.drawing_id),
            "project_id": project_id, "index": ref["index"], "parent": ref["parent"],
            "state": state}


def load_bound(verified: project.VerifiedProjectContext):
    if REF_KEY not in verified.intake:
        return None, None
    ref = check_ref(verified.intake[REF_KEY])
    binding = verified.binding
    scope = {"organization_id": str(binding.organization_id),
             "project_id": str(binding.project_id), "drawing_id": str(binding.drawing_id)}
    try:
        backend = artifacts._backend(binding.organization_id)
        raw = backend.get(artifacts._key(scope, ref["artifact_id"]))
    except KeyError:
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    except Exception:
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    try:
        meta = artifacts._metadata(raw, scope, ref["artifact_id"])
        if meta["tool"] != ps.TOOL or meta["content_sha256"] != ref["content_sha256"]:
            raise ValueError()
    except (GraphValidationError, ValueError):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    try:
        content = backend.get(artifacts._key(scope, ref["content_sha256"], blob=True))
    except KeyError:
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None
    except Exception:
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    try:
        if (type(content) is not bytes or len(content) != meta["byte_length"]
                or sha256(content).hexdigest() != ref["content_sha256"]):
            raise ValueError()
        document = ps.decode_document(content)
        if document["parent"] != ref["parent"]:
            raise ValueError()
        context = graph.resolve_project_graph_context(verified)
        if context.graph is None:
            raise ValueError()
        state = {key: meta[key] for key in artifacts._REF_KEYS}
        state.update(schema=artifacts.REF_SCHEMA, download=(
            f"/api/projects/{binding.project_id}/drawings/{binding.drawing_id}/"
            f"solar-artifacts/{ref['artifact_id']}"))
        return _view(binding, context.graph_project_id, ref, state), document
    except (ValueError, ps.PhysicalStateError, GraphValidationError):
        raise ph.PhysicalHeadError("PHYSICAL_HEAD_CORRUPT") from None


class CanonicalSource:
    def __init__(self, admission: project.AdmissionContext):
        self.admission = admission
        self.pending = None
        self.sinks = []
        self._parent = None

    def project_id(self):
        context = graph.resolve_project_graph_context(self.admission.context)
        if context.graph is None:
            raise GraphValidationError("GRAPH_NOT_EMBEDDED")
        return context.graph_project_id

    def parent(self):
        if self._parent is None:
            self._parent = load_bound(self.admission.context)
        return self._parent

    def source_sink(self, tool, request_sha256):
        sink = artifacts.ProjectArtifactSink(None, artifacts.project_artifact_binding(
            self.admission.context, tool, MANIFEST, request_sha256))
        self.sinks.append(sink)
        return sink

    def publish(self, document):
        if self.pending is not None:
            raise ValueError("one publication per source")
        head, _ = self.parent()
        parent = None if head is None else head["state"]["artifact_id"]
        if document["parent"] != parent:
            raise ph.PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")
        index = 0 if head is None else head["index"] + 1
        if index >= ph.MAX_LOG_ENTRIES:
            raise ph.PhysicalHeadError("PHYSICAL_HEAD_LOG_FULL")
        backend = artifacts._backend(self.admission.context.binding.organization_id)
        if not ph._atomic_backend(backend):
            raise ph.PhysicalHeadError("PHYSICAL_HEAD_STORE_UNSAFE")
        data = ps.encode_document(document)
        sink = self.source_sink(ps.TOOL, sha256(data).hexdigest())
        state = sink.finish(sink.prepare(solar_artifacts.ArtifactOutput(
            {"capability": document["capability"]}, ps.MEDIA_TYPE, ps.FILENAME, data)))
        ref = check_ref({"schema": REF_SCHEMA, "index": index, "parent": parent,
                         "artifact_id": state["artifact_id"], "content_sha256": state["content_sha256"]})
        self.pending = ref
        return {"schema": ph.PUBLISH_SCHEMA, "created": True, "drawing_version": None,
                "head": _view(self.admission.context.binding, self.project_id(), ref, state)}

    def unchanged_drawing_version(self):
        return None


def commit(admission, source):
    if source.admission is not admission:
        raise ValueError("source admission mismatch")
    if source.pending is None:
        return None
    binding = admission.context.binding
    org, proj, drawing, parent = (binding.organization_id, binding.project_id,
                                 binding.drawing_id, binding.input_version_id)
    raw = local.canonical_bytes(dict(admission.context.intake, solar_project_physical=source.pending))
    digest = sha256(raw).hexdigest()
    request = {"organization_id": str(org), "project_id": str(proj), "drawing_id": str(drawing),
               "input_version_id": str(parent), "checkout_fence": str(admission.checkout.fence),
               "holder_binding_id": str(admission.checkout.holder_binding_id), "intake_sha256": digest}
    request_id = uuid5(NAMESPACE_URL, "leaf.solar-project-physical-commit.v1:" +
                       sha256(local.canonical_bytes(request)).hexdigest())

    def operation(conn):
        store = project.graph_store()

        def write():
            store.verify_checkout(org, proj, drawing,
                actor_binding_id=admission.checkout.holder_binding_id,
                expected_fence=admission.checkout.fence, conn=conn)
            if not store.resolve_version_binding(org, proj, parent,
                                                  drawing_id=drawing, conn=conn).is_head:
                project.refuse("STALE_VERSION")
            # State first, then the optional LandXML source, all after authorization.
            for sink in sorted(source.sinks, key=lambda item: item.binding["tool"] != ps.TOOL):
                sink.commit()
            key = store.publication_intake_key(org, proj, drawing, digest)
            project.write_loop.upload_backend_for_tenant(str(org)).put_if_absent_or_verify(key, raw)
            project._load_intake(org, key, digest)

        return store.publish_version(org, proj, drawing, expected_parent_version_id=parent,
            actor_binding_id=admission.checkout.holder_binding_id,
            expected_fence=admission.checkout.fence, request_id=request_id,
            intake_sha256=digest, conn=conn, before_new=write).version_id

    return project._mutation(operation)


def _finish(admission, source, result):
    output = commit(admission, source) or admission.context.binding.input_version_id
    result = dict(result)
    result.pop("drawing_version", None)
    return {"result": result, "output_version_id": str(output),
            "created": output != admission.context.binding.input_version_id}


def run_operation(admission, body):
    source = CanonicalSource(admission)
    binding = admission.context.binding
    args = (None, str(binding.organization_id), str(binding.drawing_id))
    if body["operation"] in civil.OPERATIONS:
        result = civil.operate(*args, body, source=source)
    else:
        call = {"mesh": adapter.render_mesh, "slope": adapter.check_tracker_slope,
                "slope-clear": adapter.clear_tracker_slope}[body["operation"]]
        kwargs = {"expected_head": body["expected_head"], "source": source}
        if "limits" in body:
            kwargs["limits"] = body["limits"]
        result = call(*args, **kwargs)
    return _finish(admission, source, result)


def run_landxml_import(admission, data, *, drawing_units, crs, target_cells):
    source = CanonicalSource(admission)
    binding = admission.context.binding
    result = landxml.import_landxml_terrain(None, str(binding.organization_id), str(binding.drawing_id),
        data, drawing_units=drawing_units, crs=crs, target_cells=target_cells, source=source)
    return _finish(admission, source, result)
