"""Server-owned, immutable selected-version custody for project drawing turns."""
from copy import deepcopy
import re
import time
from uuid import UUID


SCHEMA = "leaf.project-execution-context.v1"
KEY = "project_execution_context"
UUID_FIELDS = ("organization_id", "project_id", "drawing_id", "input_version_id")


class ContextError(ValueError):
    def __init__(self, code, status_code=409):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _uuid(value):
    if type(value) is not str:
        raise ValueError()
    return str(UUID(value))


def validate_request(value):
    try:
        if type(value) is not dict or set(value) != {"project_id", "input_version_id"}:
            raise ValueError()
        return {key: _uuid(value[key]) for key in ("project_id", "input_version_id")}
    except (ValueError, AttributeError, TypeError):
        raise ContextError("project_context_invalid", 422) from None


def validate_binding(value):
    try:
        if (type(value) is not dict or set(value) != {"schema", *UUID_FIELDS, "intake_sha256"}
                or value["schema"] != SCHEMA
                or any(_uuid(value[key]) != value[key] for key in UUID_FIELDS)
                or type(value["intake_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value["intake_sha256"]) is None):
            raise ValueError()
        return deepcopy(value)
    except (ValueError, AttributeError, TypeError, KeyError):
        raise ContextError("project_execution_context_invalid") from None


def stored_binding(payload):
    if not isinstance(payload, dict) or KEY not in payload:
        return None
    return validate_binding(payload[KEY])


def freeze(tenant, session, requested):
    if requested is None:
        return None
    requested = validate_request(requested)
    import platform_link
    import solar_project_context

    if (not isinstance(session, dict) or session.get("tenant_id") != str(tenant)
            or str(session.get("project_id")) != requested["project_id"]
            or not session.get("org_id")):
        raise ContextError("project_context_session_mismatch")
    try:
        if platform_link.require_project_session_access(session, tenant, write=False) is None:
            raise ContextError("project_context_forbidden", 403)
        context = solar_project_context.resolve_context(
            tenant, UUID(requested["project_id"]), UUID(requested["input_version_id"]), write=False)
        binding = context.binding
        value = validate_binding({
            "schema": SCHEMA,
            "organization_id": str(binding.organization_id),
            "project_id": str(binding.project_id),
            "drawing_id": str(binding.drawing_id),
            "input_version_id": str(binding.input_version_id),
            "intake_sha256": context.intake_sha256,
        })
        if (value["organization_id"] != str(session["org_id"])
                or value["project_id"] != requested["project_id"]
                or value["input_version_id"] != requested["input_version_id"]):
            raise ContextError("project_context_session_mismatch")
        return value
    except ContextError:
        raise
    except platform_link.ProjectSessionForbidden:
        raise ContextError("project_context_forbidden", 403) from None
    except Exception:
        raise ContextError("project_context_unavailable") from None


def revalidate(tenant, session, binding):
    value = validate_binding(binding)
    current = freeze(tenant, session, {key: value[key] for key in ("project_id", "input_version_id")})
    if current != value:
        raise ContextError("project_execution_context_changed")
    return value


def resolve_turn_binding(session_id, turn_id, tenant, *, required=True):
    import session_store
    import turn_runner

    try:
        session = session_store.get_session(session_id)
        started = session.get("turn_started_at") if isinstance(session, dict) else None
        if (not session or session.get("tenant_id") != str(tenant)
                or session.get("active_turn_id") != turn_id
                or type(started) not in (float, int)
                or time.time() - started >= turn_runner.turn_max_s()):
            raise ContextError("project_turn_authority_required", 403)
        data = session_store.turn_started_data(session_id, turn_id, str(tenant))
        if data is None:
            raise ContextError("project_turn_authority_required", 403)
        value = stored_binding(data)
        if value is None:
            if not required:
                return None
            if session.get("project_id") is not None or session.get("org_id") is not None:
                raise ContextError("project_execution_context_required")
            return None
        return revalidate(tenant, session, value)
    except ContextError:
        raise
    except Exception:
        raise ContextError("project_turn_authority_required", 403) from None


def approval_payload(payload, binding):
    if payload is not None and not isinstance(payload, dict):
        if binding is not None:
            raise ContextError("project_approval_payload_invalid")
        return deepcopy(payload)
    result = deepcopy(payload) if payload is not None else {}
    result.pop(KEY, None)
    if binding is not None:
        result[KEY] = validate_binding(binding)
    return result if result or payload is not None else None


def drawing_state(session_id, turn_id, tenant):
    """Revalidate stored authority and project only bounded immutable source facts."""
    value = resolve_turn_binding(session_id, turn_id, tenant, required=False)
    if value is None:
        return {"binding": None}
    import solar_project_context
    current = solar_project_context.resolve_context(
        tenant, UUID(value["project_id"]), UUID(value["input_version_id"]),
        drawing_id=UUID(value["drawing_id"]), write=False)
    binding = current.binding
    if current.intake_sha256 != value["intake_sha256"]:
        raise ContextError("project_execution_context_changed")
    revision = binding.version.seq
    if type(revision) is not int or revision < 1:
        raise ContextError("project_execution_context_invalid")
    import solar_project_graph
    graph = solar_project_graph._companion(current.intake)
    graph_revision = None if graph is None else graph["rev"]
    layers = {}
    for kind in ("polylines", "inserts", "faces3d"):
        for entity in current.intake.get(kind) or []:
            name = str(entity.get("layer", "0"))[:200]
            layers[name] = layers.get(name, 0) + 1
    ranked = sorted(layers.items(), key=lambda row: (-row[1], row[0]))
    return {"binding": value, "revision": revision, "graph_revision": graph_revision,
            "head_version_id": str(binding.head_version_id), "is_head": binding.is_head,
            "summary": {"entity_total": sum(layers.values()),
                        "layers": [{"name": name, "count": count} for name, count in ranked[:32]]}}
