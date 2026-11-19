"""Submit a host-selected action only while its exact operation and grant are current."""
from dataclasses import dataclass
from fractions import Fraction
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


def _exact_json_number(literal):
    # JSON Schema applies integer and multipleOf to the exact numeric value.
    value = Fraction(literal)
    return value.numerator if value.denominator == 1 else value


def form_digest(form):
    """Pin the catalog field definitions independently of the operation version."""
    return sha256(_identity(form["fields"]).encode("utf-8")).hexdigest()


_EMPTY_FORM = {"fields": [], "submit_label": "Submit"}


def _check_local_schema_refs(schema, registry):
    """Preflight the pinned draft's schema graph without retrieving other resources."""
    from jsonschema import Draft202012Validator
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT202012

    remaining = 4096
    active = set()
    dialect = Draft202012Validator.META_SCHEMA["$id"]

    def visit(node, resolver):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or len(active) >= 64:
            raise ValueError("Action schema resolution budget exceeded")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("Action schema reference must target a schema")
        if id(node) in active:
            raise ValueError("Cyclic action schema reference")
        if ("$dynamicRef" in node or "$recursiveRef" in node or
                node.get("$schema", dialect) != dialect):
            raise ValueError("Action schemas require static draft 2020-12 references")
        active.add(id(node))
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#"):
                raise ValueError("Action schema references must be local")
            try:
                target = resolver.lookup(ref)
            except Unresolvable as exc:
                raise ValueError("Unresolved action schema reference") from exc
            visit(target.contents, target.resolver)
        for child in DRAFT202012.subresources_of(node):
            visit(child, resolver.in_subresource(DRAFT202012.create_resource(child)))
        active.remove(id(node))

    visit(schema, registry.resolver_with_root(DRAFT202012.create_resource(schema)))


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
    argument_validator: object


class AuthorizedLocalActions:
    """Host-owned local operations. A grant is scoped to a subject, project and immutable operation."""
    def __init__(self):
        self._lock = RLock()
        self._handlers = {}
        self._role_grants = {}
        self._memberships = {}

    def register(self, tenant, project, reference, handler, *, form=None, argument_schema=None):
        slot = (tenant, project, reference["connection_id"], reference["operation_ref"]["id"])
        registered_form = json.loads(_identity(_EMPTY_FORM if form is None else form))
        validate_action_catalog({"actions": [{**reference, "connection_title": "Local",
                                 "title": "Local", "form": registered_form}], "next_cursor": None}, 1)
        validator = None
        if argument_schema is not None:
            # A structured local operation advertises the digest of its JSON
            # argument schema in its operation reference. The wire form holds
            # presentation hints for any scalar fields the host also displays.
            from jsonschema import Draft202012Validator
            from referencing import Registry

            encoded = _identity(argument_schema)
            if sha256(encoded.encode("utf-8")).hexdigest() != reference["operation_ref"]["digest"]:
                raise ValueError("Argument schema differs from the pinned operation digest")
            schema = json.loads(encoded, parse_float=_exact_json_number)
            if (not isinstance(schema, dict) or schema.get("type") != "object"
                    or schema.get("additionalProperties") is not False):
                raise ValueError("Structured action arguments require a closed object schema")
            Draft202012Validator.check_schema(schema)
            registry = Registry()
            _check_local_schema_refs(schema, registry)
            validator = Draft202012Validator(schema, registry=registry)
        with self._lock:
            self._handlers[slot] = _Registration(
                _identity(reference), form_digest(registered_form), registered_form["fields"],
                handler, form is not None or validator is not None, validator)

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
            if binding.argument_validator is None:
                _check_arguments(binding.fields, values)
            else:
                exact_values = json.loads(arguments, parse_float=_exact_json_number)
                if not binding.argument_validator.is_valid(exact_values):
                    raise ReadFailure("invalid_input")
                field_ids = {field["id"] for field in binding.fields}
                _check_arguments(binding.fields, {k: v for k, v in values.items() if k in field_ids})
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
