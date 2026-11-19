"""Submit a host-selected action only while its exact operation and grant are current."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from threading import RLock

from integration_tool_service import ReadFailure, validate_action_catalog


def matches(description):
    return bool(re.match(r"(?:please\s+)?(?:run|execute|submit|invoke)\s+"
                         r"(?:the |this |my )?(?:selected|discovered|chosen|pinned) action\b",
                         description.strip(), re.I)) and not re.search(
                             r"\b(?:never|not|don't|without)\b", description, re.I)


def build_body(description):
    return _BODY


_BODY = """
def run(intake, params):
    selection = intake.get("selected_project_action")
    if params or type(selection) is not dict:
        raise ValueError("Choose an action from the project catalog.")
    return {key: selection[key] for key in ("connection_id", "connector_ref", "operation_ref")}, None
"""


def _pins(entry):
    return {key: entry[key] for key in ("connection_id", "connector_ref", "operation_ref")}


def _identity(ref):
    return json.dumps(ref, sort_keys=True, separators=(",", ":"))


def form_digest(form):
    """Pin the catalog field definitions independently of the operation version."""
    return sha256(_identity(form["fields"]).encode("utf-8")).hexdigest()


_EMPTY_FORM = {"fields": [], "submit_label": "Submit"}


def _valid_value(field, value):
    if value is None:
        return field["nullable"]
    kind = field["kind"]
    if kind == "text":
        return (type(value) is str and field.get("min_length", 0) <= len(value)
                and ("max_length" not in field or len(value) <= field["max_length"]))
    if kind == "number":
        return (type(value) in (int, float)
                and (type(value) is int or math.isfinite(value))
                and (not field["integer"] or type(value) is int or value.is_integer())
                and ("minimum" not in field or value >= field["minimum"])
                and ("maximum" not in field or value <= field["maximum"]))
    if kind == "boolean":
        return type(value) is bool
    if kind == "choice":
        return any(value == item["value"] for item in field["choices"])
    return False


def _check_arguments(fields, values):
    by_id = {f["id"]: f for f in fields}
    if (type(values) is not dict or values.keys() - by_id.keys()
            or any(f["required"] and f["id"] not in values for f in fields)
            or any(not _valid_value(by_id[k], v) for k, v in values.items())):
        raise ReadFailure("invalid_input")


@dataclass(frozen=True)
class Selection:
    authority: object
    reference: str
    form_digest: str
    arguments: str


@dataclass(frozen=True)
class _Registration:
    reference: str
    form_digest: str
    fields: list
    handler: object
    expects_arguments: bool


class AuthorizedLocalActions:
    """Host-owned local operations. A grant is scoped to a subject, project and immutable operation."""
    def __init__(self):
        self._lock = RLock()
        self._handlers = {}
        self._role_grants = {}
        self._memberships = {}

    def register(self, tenant, project, reference, handler, *, form=None):
        slot = (tenant, project, reference["connection_id"], reference["operation_ref"]["id"])
        registered_form = json.loads(_identity(_EMPTY_FORM if form is None else form))
        validate_action_catalog({"actions": [{**reference, "connection_title": "Local",
                                 "title": "Local", "form": registered_form}], "next_cursor": None}, 1)
        with self._lock:
            self._handlers[slot] = _Registration(
                _identity(reference), form_digest(registered_form), registered_form["fields"],
                handler, form is not None)

    def set_role_grants(self, tenant, project, role, references):
        with self._lock:
            self._role_grants[tenant, project, role] = frozenset(_identity(r) for r in references)

    def assign_roles(self, tenant, project, subject, roles):
        with self._lock:
            self._memberships[tenant, project, subject] = frozenset(roles)

    def submit(self, authority, reference, digest=form_digest(_EMPTY_FORM), arguments="{}"):
        ref = json.loads(reference)
        scope = authority.tenant_id, authority.project_id
        slot = (*scope, ref["connection_id"], ref["operation_ref"]["id"])
        with self._lock:
            binding = self._handlers.get(slot)
            roles = self._memberships.get((*scope, authority.subject), ())
            allowed = any(reference in self._role_grants.get((*scope, role), ()) for role in roles)
            if (binding is None or binding.reference != reference
                    or binding.form_digest != digest or not allowed):
                raise ReadFailure("denied")
            values = json.loads(arguments)
            _check_arguments(binding.fields, values)
            return binding.handler(values) if binding.expects_arguments else binding.handler()


def select_action(service, description, tenant, session, turn, *, selected, arguments=None):
    """Interpret an explicit submit request for the host-selected catalog entry."""
    import tools_fallback
    import tool_loader

    authority = service._resolve_operation("action_catalog", tenant, session, turn).authority
    package, source, _ = tools_fallback.author_tool(description)
    if package["engine_op"] != "guarded_action_selection" or not isinstance(selected, dict):
        raise ReadFailure("invalid_input")
    status, payload = tool_loader._run_source_in_sandbox(
        source, "selected_action.py", {"selected_project_action": selected}, {})
    if status != "ok":
        raise ReadFailure("invalid_input")
    ref, _ = payload
    values = {} if arguments is None else arguments
    if type(values) is not dict or any(type(key) is not str for key in values):
        raise ReadFailure("invalid_input")
    try:
        encoded = json.dumps(values, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ReadFailure("invalid_input") from exc
    return Selection(authority, _identity(ref), selected.get("form_digest", form_digest(_EMPTY_FORM)), encoded)


def run_selected_action(service, selection, tenant, session, turn, *, host):
    """Recheck current turn scope and catalog before the local host's atomic submit."""
    authority = service._resolve_operation("action_catalog", tenant, session, turn).authority
    if selection.authority != authority:
        raise ReadFailure("denied")
    found = False
    for page in service._catalog_pages("action_catalog", tenant, session, turn, 50, authority):
        for entry in page["actions"]:
            if _identity(_pins(entry)) == selection.reference:
                if found:
                    raise ReadFailure("invalid_response")
                if form_digest(entry["form"]) != selection.form_digest:
                    raise ReadFailure("denied")
                found = True
    if not found or service._resolve_operation("action_catalog", tenant, session, turn).authority != authority:
        raise ReadFailure("denied")
    return host.submit(authority, selection.reference, selection.form_digest, selection.arguments)
