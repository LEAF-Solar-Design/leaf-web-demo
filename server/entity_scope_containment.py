"""Fail-closed containment for the drawing writers' frozen entity binding."""
import hashlib
import hmac
import json
import re

from envelopes import ErrorCode, err_envelope


REASON_PREFIX = "ENTITY_SCOPE_"
REASON_PARENT = "ENTITY_SCOPE_PARENT_MISMATCH"
REASON_OUT_OF_SCOPE = "ENTITY_SCOPE_OUT_OF_SCOPE"
REASON_DRAWING_WIDE = "ENTITY_SCOPE_DRAWING_WIDE"
REASON_UNCHECKABLE = "ENTITY_SCOPE_UNCHECKABLE"
HANDLE_COLLECTIONS = ("polylines", "inserts", "circles", "arcs", "texts", "dimensions", "mleaders")
KNOWN_PLAN_KEYS = frozenset({
    "added", "removed", "transforms", "added_groups", "removed_groups",
    "block_defs", "set_layer", "set_points", "set_circle", "set_arc",
    "set_color", "set_linetype", "set_lineweight", "removed_kinds",
})
_PARENT_MESSAGE = "This change was prepared for a different version of the drawing. Nothing was published."
_LABELS = {
    "groups": "groups", "blocks": "block definitions",
    "blockdefs": "block definitions", "block_defs": "block definitions",
    "layers": "layers", "mlstyles": "multileader styles",
    "dimstyles": "dimension styles",
}


class ContainmentRefusal(Exception):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


def uncheckable() -> ContainmentRefusal:
    return ContainmentRefusal(
        REASON_UNCHECKABLE,
        "This drawing cannot be checked completely. Nothing was published.")


def _out_of_scope(handles):
    names = [h if isinstance(h, str) else "another entity" for h in handles]
    name = min(names)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@~+-]{0,127}", name) is None:
        name = "another entity"
    return ContainmentRefusal(
        REASON_OUT_OF_SCOPE, f"This change also affects {name}. Nothing was published.")


def _drawing_wide(keys):
    key = min(keys)
    label = _LABELS.get(key)
    if label is None:
        label = key if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) else "other data"
    return ContainmentRefusal(
        REASON_DRAWING_WIDE,
        f"This change also affects the drawing's {label}. Nothing was published.")


def check_parent(binding, *, drawing_id, version, head_version, stored_source) -> None:
    if (drawing_id != binding["drawing_id"]
            or type(version) is not int or type(head_version) is not int
            or version != binding["base_version"]
            or head_version != binding["base_version"]
            or not isinstance(stored_source, bytes)
            or not hmac.compare_digest(hashlib.sha256(stored_source).hexdigest(),
                                       binding["base_source_sha256"])):
        raise ContainmentRefusal(REASON_PARENT, _PARENT_MESSAGE)


def check_plan(binding, canonical) -> None:
    if not isinstance(canonical, dict) or set(canonical) - KNOWN_PLAN_KEYS:
        raise uncheckable()
    for key in ("added_groups", "removed_groups", "block_defs"):
        if canonical.get(key):
            raise _drawing_wide(["groups" if key != "block_defs" else key])
    offenders = []
    for key, items in canonical.items():
        # This validator-written mapping describes removals, not new targets.
        if key == "removed_kinds":
            continue
        if not isinstance(items, list):
            raise uncheckable()
        for item in items:
            if key == "removed":
                if not isinstance(item, str):
                    raise uncheckable()
                handle = item
            else:
                if not isinstance(item, dict) or "handle" not in item:
                    raise uncheckable()
                handle = item["handle"]
            if key == "added" or handle != binding["allowed_handles"][0]:
                offenders.append(handle)
    if offenders:
        raise _out_of_scope(offenders)


def require_payload_intake(stored_source, intake) -> None:
    try:
        payload = json.loads(stored_source.decode("utf-8"))
    except (AttributeError, UnicodeError, ValueError, TypeError):
        raise uncheckable() from None
    if not isinstance(payload, dict) or payload != intake:
        raise uncheckable()


def _handle_index(intake):
    result = {}
    for collection in HANDLE_COLLECTIONS:
        items = intake.get(collection, [])
        if not isinstance(items, list):
            raise uncheckable()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("handle"), str):
                raise uncheckable()
            handle = item["handle"]
            if handle in result:
                raise uncheckable()
            result[handle] = item
    return result


def check_output_exact(binding, parent_intake, output_intake) -> None:
    if not isinstance(parent_intake, dict) or not isinstance(output_intake, dict):
        raise uncheckable()
    parent = _handle_index(parent_intake)
    output = _handle_index(output_intake)
    added = output.keys() - parent.keys()
    if added:
        raise _out_of_scope(added)
    allowed = binding["allowed_handles"][0]
    changed = [h for h in parent if h != allowed
               and (h not in output or parent[h] != output[h])]
    if changed:
        raise _out_of_scope(changed)
    before = parent_intake.get("properties", {})
    after = output_intake.get("properties", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise uncheckable()
    missing = object()
    changed = [h for h in before.keys() | after.keys() if h != allowed
               and before.get(h, missing) != after.get(h, missing)]
    if changed:
        raise _out_of_scope(changed)
    changed = []
    for key in parent_intake.keys() | output_intake.keys():
        if key in HANDLE_COLLECTIONS or key == "properties":
            continue
        if not isinstance(key, str):
            raise uncheckable()
        default = [] if key == "layers" else missing
        if parent_intake.get(key, default) != output_intake.get(key, default):
            changed.append(key)
    if changed:
        raise _drawing_wide(changed)


def refusal_envelope(exc, *, tool, version) -> tuple:
    env = err_envelope(ErrorCode.BAD_PARAMS, exc.message, retryable=False,
                       tool=tool, version=version)
    env["error"]["reason_code"] = exc.reason_code
    return env, 409


def is_scope_refusal(error) -> bool:
    return (isinstance(error, dict) and isinstance(error.get("reason_code"), str)
            and error["reason_code"].startswith(REASON_PREFIX))
