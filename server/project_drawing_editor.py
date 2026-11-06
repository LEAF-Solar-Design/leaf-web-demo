"""Project-owned browser DXF publication and exact immutable source readback."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from uuid import UUID

import checkout_capability
import dxf_intake
import guest_uploads
import solar_project_context as project

MAX_DXF_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DXF_FIELDS = frozenset(("dwg", "layers", "polylines", "texts", "circles", "arcs", "inserts",
    "dimensions", "dimstyles", "dimensions_unsupported", "blocks", "blocksCapped", "mlstyles",
    "mleaders", "mleaders_unsupported", "mleaders_unsupported_handles", "properties",
    "propertiesDropped", "groups", "memberEvidenceCovered", "parseErrors", "polylineWidthCovered"))
_GEOMETRY = ("polylines", "texts", "circles", "arcs", "inserts", "dimensions", "blocks", "mleaders", "groups")


class EditorError(ValueError):
    def __init__(self, code, status=400, retryable=False):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable


def _fail(code, status=400, retryable=False):
    raise EditorError(code, status, retryable)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def request_uuid(value):
    try:
        result = UUID(value)
        if str(result) != value:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        _fail("PROJECT_EDITOR_REQUEST_INVALID")


def _parse(raw, drawing_id):
    if not isinstance(raw, bytes) or not raw:
        _fail("PROJECT_EDITOR_DXF_INVALID", 422)
    if len(raw) > MAX_DXF_BYTES:
        _fail("PROJECT_EDITOR_TOO_LARGE", 413)
    ext, error = guest_uploads.validate_upload("edited.dxf", raw)
    if error is not None or ext != ".dxf":
        _fail("PROJECT_EDITOR_DXF_INVALID", 422)
    try:
        intake = dxf_intake.parse_dxf_bytes(raw, source_name=str(drawing_id))
        intake["polylineWidthCovered"] = True
        payload = json.dumps(intake, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (dxf_intake.DxfParseError, ValueError, TypeError, RecursionError):
        _fail("PROJECT_EDITOR_DXF_INVALID", 422)
    return payload


def _edited_intake(parent, parsed):
    """Replace parsed CAD fields, retain project material, invalidate old design geometry."""
    before_intake = parent.intake
    parsed = json.loads(parsed)
    result = {key: deepcopy(value) for key, value in before_intake.items() if key not in _DXF_FIELDS}
    result.update(parsed)
    has_graph = "solar_design_graph" in before_intake
    if has_graph != ("solar_design_graph_sha256" in before_intake):
        _fail("PROJECT_EDITOR_GRAPH_INVALID", 409)
    if has_graph:
        from solar_design_graph import GraphValidationError, entities, validate_graph
        from solar_project import invalidate_project_dependents
        from solar_solve_results import digest, finish_mutation, version_companion
        try:
            before = validate_graph(before_intake["solar_design_graph"])
            if before_intake["solar_design_graph_sha256"] != digest(before):
                raise GraphValidationError("STALE_GRAPH_COMPANION")
            if any(before_intake.get(key) != parsed.get(key) for key in _GEOMETRY):
                after = deepcopy(before)
                invalidate_project_dependents(after)
                # A raw CAD edit has no graph-id mutation plan. Preserve its
                # last design facts, but none of the old source geometry may
                # be presented as current until its groups are rebuilt.
                for entity in entities(after):
                    if entity["kind"] not in ("project", "settings"):
                        entity["validity"] = {"state": "stale", "reasons": ["source_geometry_changed"]}
                after = finish_mutation(before, after, "browser-dxf-edit")
                result = version_companion(result, before, after)
        except (GraphValidationError, ValueError, KeyError, TypeError, RecursionError):
            _fail("PROJECT_EDITOR_GRAPH_INVALID", 409)
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _backend(org_id):
    try:
        return project.write_loop.upload_backend_for_tenant(str(org_id))
    except Exception:
        _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)


def _verified_bytes(backend, key, digest):
    try:
        raw = backend.get(key)
    except Exception:
        _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
    if not isinstance(raw, bytes) or len(raw) > MAX_DXF_BYTES or _sha(raw) != digest:
        _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
    return raw


def _source(context, seen=None):
    """Imported DXF bytes are digest-bound separately from its intake blob."""
    binding, version = context.binding, context.binding.version
    seen = set() if seen is None else seen
    if version.version_id in seen or len(seen) >= 128:
        _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
    seen.add(version.version_id)
    proof = version.provenance or {}
    source = proof.get("source")
    if proof.get("schema") == "leaf.project-drawing-publication.v1" and source is None:
        # Graph-only versions intentionally retain their parent's source
        # object. Follow that exact same-artifact chain, never another file.
        try:
            parent = project.graph_store().resolve_version_binding(
                binding.organization_id, binding.project_id, UUID(proof["parent_version_id"]),
                drawing_id=binding.drawing_id)
        except project.ProjectContextError:
            raise
        except Exception:
            _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
        if parent.version.oss_object != version.oss_object:
            _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
        return _source(project._read_intake(parent), seen)
    if not isinstance(source, dict):
        _fail("PROJECT_EDITOR_SOURCE_REQUIRED", 409)
    if proof.get("schema") in ("leaf.project-drawing-publication.v1",
                               project.graph_store().INITIAL_PUBLICATION_SCHEMA):
        digest = source.get("sha256")
        if (not _digest(digest) or source.get("ref") != version.oss_object
                or version.oss_object != project.graph_store().publication_source_key(
                    binding.organization_id, binding.project_id, binding.drawing_id, digest)):
            _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
        return version.oss_object, digest, None
    if proof.get("schema") != "leaf.drawing-import.v1":
        _fail("PROJECT_EDITOR_SOURCE_REQUIRED", 409)
    digest = source.get("upload_content_sha256")
    if (not _digest(digest) or source.get("kind") != "account_upload"
            or source.get("tenant_id") != str(binding.organization_id)
            or not project.platform_link.platform_store().is_account_upload_source_id(source.get("drawing_id"))):
        _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
    key = project.graph_store().publication_source_key(
        binding.organization_id, binding.project_id, binding.drawing_id, digest)
    return key, digest, source["drawing_id"]


def upload_source_key(org_id, drawing_id, digest):
    """Original account-upload source, bound by canonical import provenance."""
    if (not isinstance(org_id, UUID) or not _digest(digest)
            or not project.platform_link.platform_store().is_account_upload_source_id(drawing_id)):
        _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
    return guest_uploads.original_dxf_key(str(org_id), drawing_id, digest)


def read_dxf(tenant, project_id, drawing_id, version_id):
    context = project.resolve_context(tenant, project_id, version_id, drawing_id=drawing_id)
    key, digest, staged_drawing = _source(context)
    backend = _backend(context.binding.organization_id)
    try:
        exists = backend.exists(key)
    except Exception:
        _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
    if not exists:
        if staged_drawing is None:
            _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
        # The upload producer preserves original DXF bytes independently of
        # its legacy intake-JSON version object. Old uploads may still have
        # only their staged file; backfill it only with an exact digest match.
        try:
            original_key = upload_source_key(context.binding.organization_id, staged_drawing, digest)
            if backend.exists(original_key):
                raw = _verified_bytes(backend, original_key, digest)
            else:
                path = guest_uploads.staged_path(str(context.binding.organization_id), staged_drawing, ".dxf")
                with path.open("rb") as stream:
                    raw = stream.read(MAX_DXF_BYTES + 1)
        except EditorError:
            raise
        except Exception:
            _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
        if len(raw) > MAX_DXF_BYTES or _sha(raw) != digest:
            _fail("PROJECT_EDITOR_SOURCE_INVALID", 409)
        _parse(raw, drawing_id)
        try:
            backend.put_if_absent_or_verify(key, raw)
        except Exception:
            _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
    raw = _verified_bytes(backend, key, digest)
    _parse(raw, drawing_id)
    return raw, digest


def _replay_fence(conn, org_id, project_id, drawing_id, actor, parent_id,
                  request_id, source_digest, intake_digest, tenant, capability):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM drawing_versions WHERE org_id=%s AND project_id=%s "
                    "AND idempotency_key=%s FOR SHARE",
                    (org_id, project_id, "sip-r2:" + str(request_id)))
        row = cur.fetchone()
    if row is None:
        return None
    proof = row.get("provenance") or {}
    source = proof.get("source") or {}
    if (row.get("deleted_at") is not None or row.get("drawing_id") != drawing_id
            or proof.get("schema") != "leaf.project-drawing-publication.v1"
            or proof.get("actor_binding_id") != str(actor)
            or proof.get("parent_version_id") != str(parent_id)
            or proof.get("request_id") != str(request_id)
            or source.get("sha256") != source_digest
            or (proof.get("intake") or {}).get("sha256") != intake_digest):
        raise project.ProjectContextError("SIP_R2_IDEMPOTENCY_CONFLICT")
    fence = proof.get("checkout_fence")
    if not isinstance(fence, str) or re.fullmatch(r"[1-9][0-9]{0,18}", fence) is None:
        project.refuse("CHECKOUT_DENIED")
    try:
        # This credential check returns an already committed result only.
        # A new write must still prove the current active lease below.
        checkout_capability.verify(capability, tenant, project.checkout_scope(project_id, drawing_id),
                                   {"holder": "Project editor", "fence": int(fence)})
    except checkout_capability.CapabilityUnavailable:
        project.refuse("CHECKOUT_UNAVAILABLE")
    except checkout_capability.CapabilityRejected:
        project.refuse("CHECKOUT_DENIED")
    return int(fence)


def save_dxf(tenant, project_id, drawing_id, parent_id, raw, *, request_id, capability,
             _restored_intake=None):
    org_id, actor = project._access(tenant, project_id, write=True)
    if not all(isinstance(value, UUID) for value in (drawing_id, parent_id, request_id)):
        project.refuse("INVALID_BINDING")
    parent = project.resolve_context(tenant, project_id, parent_id, drawing_id=drawing_id, write=True)
    intake = _edited_intake(parent, _parse(raw, drawing_id))
    if _restored_intake is not None:
        # Only restore_dxf supplies this server-read verified predecessor
        # snapshot. Restore includes graph/settings, not just DXF geometry.
        intake = _restored_intake
    source_digest, intake_digest = _sha(raw), _sha(intake)
    graph = project.graph_store()
    source_key = graph.publication_source_key(org_id, project_id, drawing_id, source_digest)
    intake_key = graph.publication_intake_key(org_id, project_id, drawing_id, intake_digest)
    backend = _backend(org_id)

    def operation(conn):
        fence = _replay_fence(conn, org_id, project_id, drawing_id, actor, parent_id,
                              request_id, source_digest, intake_digest, tenant, capability)
        if fence is None:
            state = graph.get_checkout(org_id, project_id, drawing_id, conn=conn, for_update=True)
            # A concurrent winner may have committed while this transaction
            # waited for the artifact's checkout row. Replay before demanding
            # an active lease or current parent for a *new* publication.
            fence = _replay_fence(conn, org_id, project_id, drawing_id, actor, parent_id,
                                  request_id, source_digest, intake_digest, tenant, capability)
            if fence is None:
                if state.checkout is None:
                    project.refuse("CHECKOUT_REQUIRED")
                if state.checkout.expires_at <= state.observed_at:
                    project.refuse("CHECKOUT_EXPIRED")
                expected = project._expected_capability(tenant,
                    project.checkout_scope(project_id, drawing_id), state, capability)
                lease = graph.verify_checkout(org_id, project_id, drawing_id,
                    actor_binding_id=actor, expected_fence=expected, conn=conn)
                fence = lease.fence

        def durable():
            try:
                backend.put_if_absent_or_verify(source_key, raw)
                backend.put_if_absent_or_verify(intake_key, intake)
            except Exception:
                _fail("PROJECT_EDITOR_SOURCE_UNAVAILABLE", 503, True)
            _verified_bytes(backend, source_key, source_digest)
            project._load_intake(org_id, intake_key, intake_digest)

        version = graph.publish_version(org_id, project_id, drawing_id,
            expected_parent_version_id=parent_id, actor_binding_id=actor,
            expected_fence=fence, request_id=request_id, intake_sha256=intake_digest,
            source_object_ref=source_key, source_sha256=source_digest,
            conn=conn, before_new=durable)
        return {"drawing_version": version.to_dict(), "organization_id": str(org_id),
                "project_id": str(project_id), "drawing_id": str(drawing_id),
                "parent_version_id": str(parent_id), "request_id": str(request_id),
                "source_sha256": source_digest, "intake_sha256": intake_digest}

    # Preserve typed editor failures instead of the generic mutation wrapper's
    # conversion to STORE_UNAVAILABLE, while using the same drain fence.
    try:
        with project.write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                project.refuse("WRITES_DRAINED")
            return project.platform_link.platform_db().run_transaction(operation)
    except (EditorError, project.ProjectContextError):
        raise
    except Exception:
        project.refuse("STORE_UNAVAILABLE")


def restore_dxf(tenant, project_id, drawing_id, parent_id, restore_id, *, request_id, capability):
    project._access(tenant, project_id, write=True)
    if not isinstance(restore_id, UUID):
        project.refuse("INVALID_BINDING")
    target = project.resolve_context(tenant, project_id, restore_id, drawing_id=drawing_id, write=True)
    parent = project.resolve_context(tenant, project_id, parent_id, drawing_id=drawing_id, write=True)
    if target.binding.version.seq >= parent.binding.version.seq:
        _fail("PROJECT_EDITOR_RESTORE_INVALID", 409)
    raw, _digest_value = read_dxf(tenant, project_id, drawing_id, restore_id)
    return save_dxf(tenant, project_id, drawing_id, parent_id, raw,
                    request_id=request_id, capability=capability,
                    _restored_intake=target.intake_bytes)
