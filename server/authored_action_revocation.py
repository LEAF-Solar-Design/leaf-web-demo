"""Submit a host-selected action only while its exact operation and grant are current."""
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from functools import cache
from hashlib import sha256
import json
import math
import re
from threading import RLock
from urllib.parse import quote, unquote, urljoin

from integration_tool_service import ReadFailure, validate_action_catalog


SCHEMA_EVALUATION_LIMIT = 16384
REGEX_ARGUMENT_LIMIT = 16384
_remaining_evaluations = ContextVar("action_schema_work")

# A fixed-width prefix can contain literals, escapes and simple character
# classes. Only a start-anchored pattern may repeat its final single atom.
_PATTERN_ATOM = r"(?:[^\\^$*+?{}\[\]|()]|\\[\\.^$*+?{}\[\]|()dDsSwWnrt]|\[\^?[A-Za-z0-9_ -]+\])"
_FIXED_PATTERN = re.compile(rf"\^?{_PATTERN_ATOM}*\$?")
_REPEATED_PATTERN = re.compile(rf"\^{_PATTERN_ATOM}+(?:[+*?]|\{{[0-9]{{1,2}}(?:,[0-9]{{0,2}})?\}})\$?")


@cache
def _pinned_validator_type(base_validator):
    from attrs import evolve
    from jsonschema import validators

    validator_type = validators.extend(base_validator)
    # Local graph admission already verifies the dialect at every resource.
    # Retain this validator and its resolver when entering an explicit $schema.
    validator_type.evolve = evolve

    def counted(method):
        def visit(self, *args, **kwargs):
            remaining = _remaining_evaluations.get()
            if remaining == 0:
                raise ReadFailure("schema_work_limit")
            _remaining_evaluations.set(remaining - 1)
            return method(self, *args, **kwargs)
        return visit

    # jsonschema uses descend for subschemas and is_valid for independent
    # checks (including the remaining branches after the first oneOf match).
    validator_type.descend = counted(validator_type.descend)
    validator_type.is_valid = counted(validator_type.is_valid)
    return validator_type


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
    if len(literal) > 768 or not math.isfinite(float(literal)):
        raise ValueError("Unsupported JSON number")
    decimal = Decimal(literal)
    if abs(decimal.as_tuple().exponent) > 768:
        raise ValueError("Unsupported JSON decimal scale")
    value = Fraction(decimal)
    return value.numerator if value.denominator == 1 else value


def form_digest(form):
    """Pin the catalog field definitions independently of the operation version."""
    return sha256(_identity(form["fields"]).encode("utf-8")).hexdigest()


_EMPTY_FORM = {"fields": [], "submit_label": "Submit"}
_ASSERTED_FORMATS = ("date", "ipv4", "ipv6", "uuid")


@dataclass(frozen=True)
class _LocalFragmentResolver:
    """Apply identical URI and pointer rules during preflight and validation."""

    resolver: object

    def in_subresource(self, resource):
        return _LocalFragmentResolver(self.resolver.in_subresource(resource))

    def lookup(self, reference):
        from attrs import evolve

        if not isinstance(reference, str) or not reference.startswith("#"):
            raise ValueError("Action schema references must be local")
        if re.search(r"%(?![0-9A-Fa-f]{2})", reference):
            raise ValueError("Malformed action schema fragment escape")
        fragment = unquote(reference[1:], errors="strict")
        if fragment.startswith("/"):
            if re.search(r"~(?![01])", fragment):
                raise ValueError("Malformed action schema pointer escape")
            # referencing decodes pointer bytes, but takes named anchors as-is.
            fragment = quote(fragment, safe="/~")
        result = self.resolver.lookup("#" + fragment)
        return evolve(result, resolver=_LocalFragmentResolver(result.resolver))


def _check_local_schema_refs(schema, registry, format_policy, base_validator, specification):
    """Preflight the pinned draft's schema graph without retrieving other resources."""
    from jsonschema.exceptions import SchemaError
    from referencing.exceptions import Unresolvable
    from referencing.jsonschema import DRAFT7

    remaining = 4096
    has_patterns = False
    active = set()
    dialect = base_validator.META_SCHEMA["$id"]
    vocabularies = base_validator.META_SCHEMA.get("$vocabulary", {})

    def children(node):
        # Draft-07 allows property-name lists and schemas together in dependencies.
        if specification is DRAFT7 and isinstance(node, dict) and "dependencies" in node:
            other = node.copy()
            dependencies = other.pop("dependencies")
            yield from specification.subresources_of(other)
            yield from (value for value in dependencies.values() if not isinstance(value, list))
        else:
            yield from specification.subresources_of(node)

    # Index lexical declarations before lookup: registry crawling can silently
    # choose one of two declarations. Following references in this pass would
    # count legitimate repeated uses of the same declaration as collisions.
    resource_uris = set()
    anchors = set()
    declarations_left = remaining

    def index(resource, base, depth):
        nonlocal declarations_left
        declarations_left -= 1
        if declarations_left < 0 or depth >= 64:
            raise ValueError("Action schema resolution budget exceeded")
        identifier = resource.id()
        if depth == 0 or identifier is not None:
            base = urljoin(base, identifier or "")
            if base in resource_uris:
                raise ValueError("Ambiguous local schema resource ID")
            resource_uris.add(base)
        for anchor in resource.anchors():
            key = (base, anchor.name)
            if key in anchors:
                raise ValueError("Ambiguous local schema anchor")
            anchors.add(key)
        for child in children(resource.contents):
            index(specification.create_resource(child), base, depth + 1)

    def visit(node, resolver):
        nonlocal remaining, has_patterns
        remaining -= 1
        if remaining < 0 or len(active) >= 64:
            raise ValueError("Action schema resolution budget exceeded")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise ValueError("Action schema reference must target a schema")
        if id(node) in active:
            raise ValueError("Cyclic action schema reference")
        if node.get("$schema", dialect) != dialect:
            raise ValueError("Unsupported action schema dialect")
        if any(required and uri not in vocabularies
               for uri, required in node.get("$vocabulary", {}).items()):
            raise ValueError("Unsupported required action vocabulary")
        if node.get("x-leaf-format-policy", format_policy) != format_policy:
            raise ValueError("Conflicting action format policy")
        if format_policy == "assertion" and "format" in node and node["format"] not in _ASSERTED_FORMATS:
            raise ValueError("Unsupported asserted action format")
        if "$dynamicRef" in node or "$recursiveRef" in node:
            raise ValueError("Action schemas require static draft 2020-12 references")
        patterns = list(node.get("patternProperties", {}))
        if "pattern" in node:
            patterns.append(node["pattern"])
        for pattern in patterns:
            if (len(pattern) > 256 or
                    not (_FIXED_PATTERN.fullmatch(pattern) or _REPEATED_PATTERN.fullmatch(pattern))):
                raise SchemaError("Unsupported action pattern")
            has_patterns = True
        active.add(id(node))
        if "$ref" in node:
            ref = node["$ref"]
            try:
                target = resolver.lookup(ref)
            except Unresolvable as exc:
                raise ValueError("Unresolved action schema reference") from exc
            visit(target.contents, target.resolver)
        for child in children(node):
            visit(child, resolver.in_subresource(specification.create_resource(child)))
        active.remove(id(node))

    root = specification.create_resource(schema)
    index(root, "", 0)
    resolver = _LocalFragmentResolver(registry.resolver_with_root(root))
    visit(schema, resolver)
    return has_patterns, resolver


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
    has_patterns: bool


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
        has_patterns = False
        if argument_schema is not None:
            # A structured local operation advertises the digest of its JSON
            # argument schema in its operation reference. The wire form holds
            # presentation hints for any scalar fields the host also displays.
            from jsonschema import Draft7Validator, Draft202012Validator, FormatChecker
            from referencing import Registry

            from referencing.jsonschema import DRAFT7, DRAFT202012

            encoded = _identity(argument_schema)
            if sha256(encoded.encode("utf-8")).hexdigest() != reference["operation_ref"]["digest"]:
                raise ValueError("Argument schema differs from the pinned operation digest")
            schema = json.loads(encoded, parse_float=_exact_json_number, parse_constant=_exact_json_number)
            base_validator, specification = Draft202012Validator, DRAFT202012
            if isinstance(schema, dict) and schema.get("$schema") == Draft7Validator.META_SCHEMA["$id"]:
                base_validator, specification = Draft7Validator, DRAFT7
                # Draft-07 ignores $ref siblings and has no unevaluatedProperties.
                if "$ref" in schema or schema.get("additionalProperties") is not False:
                    raise ValueError("Structured draft-07 action arguments require a closed object schema")
            if not isinstance(schema, bool) and (
                not isinstance(schema, dict) or schema.get("type") != "object"
                or (schema.get("additionalProperties") is not False
                    and schema.get("unevaluatedProperties") is not False)
            ):
                raise ValueError("Structured action arguments require a boolean or closed object schema")
            base_validator.check_schema(schema)
            registry = Registry()
            policy = schema.get("x-leaf-format-policy", "annotation") if isinstance(schema, dict) else "annotation"
            if policy not in ("annotation", "assertion"):
                raise ValueError("Unsupported action format policy")
            has_patterns, resolver = _check_local_schema_refs(
                schema, registry, policy, base_validator, specification)
            checker = FormatChecker(formats=_ASSERTED_FORMATS) if policy == "assertion" else None
            validator = _pinned_validator_type(base_validator)(
                schema, registry=registry, _resolver=resolver, format_checker=checker)
        with self._lock:
            self._handlers[slot] = _Registration(
                _identity(reference), form_digest(registered_form), registered_form["fields"],
                handler, form is not None or validator is not None, validator, has_patterns)

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
            # Selection stores canonical ASCII JSON. This cap bounds both
            # value strings and every property name before any matching.
            if binding.has_patterns and len(arguments) > REGEX_ARGUMENT_LIMIT:
                raise ReadFailure("schema_regex_limit")
            values = json.loads(arguments)
            if binding.argument_validator is None:
                _check_arguments(binding.fields, values)
            else:
                try:
                    exact_values = json.loads(arguments, parse_float=_exact_json_number,
                                              parse_constant=_exact_json_number)
                except (ValueError, ArithmeticError):
                    raise ReadFailure("invalid_input") from None
                token = _remaining_evaluations.set(SCHEMA_EVALUATION_LIMIT)
                try:
                    if not binding.argument_validator.is_valid(exact_values):
                        raise ReadFailure("invalid_input")
                finally:
                    _remaining_evaluations.reset(token)
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
