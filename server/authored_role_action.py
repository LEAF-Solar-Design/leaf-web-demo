"""Compare trusted role assignments within the viewer's turn-authorized action catalog."""
import json
import re


def matches(description):
    return bool(re.search(r"\bactions?\b", description, re.I) and
                re.search(r"\b(?:compare|versus|both|shared?|exclusive|differences?|overlap)\b",
                          description, re.I) and re.search(r"\b(?:and|versus|vs)\b", description, re.I))


def build_body(description):
    if re.search(r"\b(?:share[d]?|both|overlap)\b", description, re.I):
        mode = "shared"
    elif re.search(r"\b(?:exclusive|only|differences?)\b", description, re.I):
        mode = "exclusive"
    else:
        mode = "all"
    return _BODY.replace("__MODE__", repr(mode))


_BODY = """
def run(intake, params):
    bound = intake.get("role_action_catalog")
    if not bound or len(bound["roles"]) != 2:
        raise ValueError("Select two authorized role catalogs in this project.")
    first, second = bound["roles"]
    shared = []
    exclusive = {first: [], second: []}
    for row in bound["actions"]:
        if len(row["roles"]) == 2:
            shared.append(row["reference"])
        elif len(row["roles"]) == 1:
            exclusive[row["roles"][0]].append(row["reference"])
    result = {"project_id": bound["project_id"], "roles": [first, second]}
    if __MODE__ in ("all", "shared"):
        result["shared"] = shared
    if __MODE__ in ("all", "exclusive"):
        result["exclusive"] = exclusive
    return result, None
"""


def compare_role_actions(service, description, tenant, session, turn, *, policy, limit=50):
    """A trusted host supplies a viewer-authorized policy snapshot, never request parameters."""
    from integration_tool_service import ReadFailure, RESPONSE_CAP
    import tools_fallback
    import tool_loader

    if not isinstance(description, str) or not 1 <= len(description) <= 4000:
        raise ReadFailure("invalid_input")
    package, source, _ = tools_fallback.author_tool(description)
    if package["engine_op"] != "role_action_comparison":
        raise ReadFailure("invalid_input")
    authority = service._resolve_operation("action_catalog", tenant, session, turn).authority
    if (type(policy) is not dict or policy.get("project_id") != authority.project_id
            or policy.get("tenant_id") != authority.tenant_id or policy.get("subject") != authority.subject
            or type(policy.get("roles")) is not dict):
        raise ReadFailure("denied")
    roles = [role for role in policy["roles"] if isinstance(role, str) and role and
             re.search(r"(?<!\w)" + re.escape(role) + r"(?!\w)", description, re.I)]
    if len(roles) != 2:
        raise ReadFailure("invalid_input")
    # Compare complete provider-issued refs, including both pinned digests.
    def key(reference):
        return json.dumps(reference, sort_keys=True, separators=(",", ":"))
    assignments = {}
    size = 0
    for role in roles:
        if type(policy["roles"][role]) is not list:
            raise ReadFailure("denied")
        for ref in policy["roles"][role]:
            if type(ref) is not dict or set(ref) != {"connection_id", "connector_ref", "operation_ref"}:
                raise ReadFailure("denied")
            identity = key(ref)
            size += len(identity.encode("utf-8"))
            if size > RESPONSE_CAP:
                raise ReadFailure("response_too_large")
            assigned = assignments.setdefault(identity, [])
            if role in assigned:
                raise ReadFailure("invalid_response")
            assigned.append(role)
    actions = []
    for page in service._catalog_pages("action_catalog", tenant, session, turn, limit, authority):
        for entry in page["actions"]:
            ref = {k: entry[k] for k in ("connection_id", "connector_ref", "operation_ref")}
            grant = assignments.pop(key(ref), None)
            if grant:
                actions.append({"reference": ref, "roles": grant})
    if assignments:
        raise ReadFailure("denied")
    intake = {"role_action_catalog": {"project_id": authority.project_id, "roles": roles, "actions": actions}}
    status, payload = tool_loader._run_source_in_sandbox(source, "role_actions.py", intake, {})
    if service._resolve_operation("action_catalog", tenant, session, turn).authority != authority:
        raise ReadFailure("denied")
    if status != "ok":
        raise ReadFailure("unavailable")
    return payload[0]
