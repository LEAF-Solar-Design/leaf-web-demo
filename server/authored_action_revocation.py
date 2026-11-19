"""Submit a host-selected action only while its exact operation and grant are current."""
from dataclasses import dataclass
import json
import re
from threading import RLock

from integration_tool_service import ReadFailure


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


@dataclass(frozen=True)
class Selection:
    authority: object
    reference: str


class AuthorizedLocalActions:
    """Host-owned local operations. A grant is scoped to a subject, project and immutable operation."""
    def __init__(self):
        self._lock = RLock()
        self._handlers = {}
        self._role_grants = {}
        self._memberships = {}

    def register(self, tenant, project, reference, handler):
        slot = (tenant, project, reference["connection_id"], reference["operation_ref"]["id"])
        with self._lock:
            self._handlers[slot] = (_identity(reference), handler)

    def set_role_grants(self, tenant, project, role, references):
        with self._lock:
            self._role_grants[tenant, project, role] = frozenset(_identity(r) for r in references)

    def assign_roles(self, tenant, project, subject, roles):
        with self._lock:
            self._memberships[tenant, project, subject] = frozenset(roles)

    def submit(self, authority, reference):
        ref = json.loads(reference)
        scope = authority.tenant_id, authority.project_id
        slot = (*scope, ref["connection_id"], ref["operation_ref"]["id"])
        with self._lock:
            binding = self._handlers.get(slot)
            roles = self._memberships.get((*scope, authority.subject), ())
            allowed = any(reference in self._role_grants.get((*scope, role), ()) for role in roles)
            if binding is None or binding[0] != reference or not allowed:
                raise ReadFailure("denied")
            return binding[1]()


def select_action(service, description, tenant, session, turn, *, selected):
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
    return Selection(authority, _identity(ref))


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
                found = True
    if not found or service._resolve_operation("action_catalog", tenant, session, turn).authority != authority:
        raise ReadFailure("denied")
    return host.submit(authority, selection.reference)
