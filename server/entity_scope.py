"""Server-owned entity identity for SSD1 prompt and approval snapshots."""
from copy import deepcopy
import hashlib
import re

import write_loop
import store

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@~+-]{0,127}")
_COLLECTIONS = ("polylines", "inserts", "faces3d", "circles", "arcs",
                "texts", "dimensions", "mleaders")


class ScopeError(ValueError):
    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


def _identifier(value):
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def validate_request(value):
    if (type(value) is not dict or set(value) != {"drawing_id", "handle"}
            or not all(_identifier(value[key]) for key in value)):
        raise ScopeError("entity_scope must contain exactly valid drawing_id and handle strings")
    return deepcopy(value)


def stored_binding(payload):
    if not isinstance(payload, dict) or "entity_scope" not in payload:
        return None
    value = payload["entity_scope"]
    if (type(value) is not dict
            or set(value) != {"drawing_id", "base_version", "base_source_sha256", "allowed_handles"}
            or not _identifier(value["drawing_id"])
            or type(value["base_version"]) is not int or value["base_version"] < 1
            or type(value["base_source_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["base_source_sha256"]) is None
            or type(value["allowed_handles"]) is not list
            or len(value["allowed_handles"]) != 1
            or not _identifier(value["allowed_handles"][0])):
        raise ScopeError("invalid stored entity_scope; request a new approval", 409)
    return deepcopy(value)


def approval_payload(payload, binding):
    if payload is not None and not isinstance(payload, dict):
        if binding is not None:
            raise ScopeError("scoped approval payload must be an object")
        return deepcopy(payload)
    result = deepcopy(payload) if payload is not None else {}
    result.pop("entity_scope", None)
    if binding is not None:
        result["entity_scope"] = deepcopy(binding)
    return result if result or payload is not None else None


def freeze(tenant, session, requested):
    if requested["drawing_id"] != session.get("drawing_id"):
        raise ScopeError("entity_scope drawing does not match session drawing", 409)
    drawing_id = requested["drawing_id"]
    try:
        backend = write_loop.backend_for_tenant(str(tenant), aps_live=False, da=None)
        version, key = store.resolve_version(backend, str(tenant), drawing_id, "head")
        if type(version) is not int or version < 1:
            raise ValueError("invalid source version")
        _, intake = write_loop.read_intake(backend, str(tenant), drawing_id, version)
        if not isinstance(intake, dict):
            raise ValueError("invalid intake")
        matches = 0
        for name in _COLLECTIONS:
            collection = intake.get(name, [])
            if not isinstance(collection, list) or any(not isinstance(e, dict) for e in collection):
                raise ValueError("invalid intake collection")
            matches += sum(e.get("handle") == requested["handle"] for e in collection)
        source_hash = hashlib.sha256(backend.get(key)).hexdigest()
    except write_loop.ProofStateUnreadable:
        raise
    except Exception as exc:
        raise ScopeError(f"drawing/version unavailable: {exc}", 404) from exc
    if matches == 0:
        raise ScopeError("entity_scope handle is not present in drawing", 400)
    if matches != 1:
        raise ScopeError("entity_scope handle is ambiguous in drawing", 409)
    return {"drawing_id": drawing_id, "base_version": version,
            "base_source_sha256": source_hash, "allowed_handles": [requested["handle"]]}
