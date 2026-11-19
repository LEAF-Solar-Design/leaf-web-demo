"""Read back a lost recipe save at the original successor without replaying a write."""
import hashlib
import json
import re


def matches(description):
    if not isinstance(description, str):
        return False
    return bool(re.search(r"\b(?:recipe|table|element)\b", description, re.I)
                and re.search(r"\b(?:sav(?:e|ed|ing)|edit|update)\b", description, re.I)
                and re.search(r"\b(?:retr(?:y|ying)|recover|receipt|timed? out|disconnect\w*|"
                              r"dropped|lost|missing response|confirm whether)\b", description, re.I))


def build_body(description):
    return _BODY


_BODY = """
def run(intake, params):
    attempt = intake.get("recipe_save_recovery")
    if not attempt:
        raise ValueError("Select the original save attempt.")
    evidence = attempt.get("observed")
    resolved = bool(evidence and evidence["revision"] == attempt["successor"]
                    and evidence["digest"] == attempt["requested_digest"])
    return {"successor_observed": resolved}, None
"""


def _digest(element):
    canonical = json.dumps(element, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def recover_recipe_save(service, description, original_args, tenant, session, turn, *, read_revision=None):
    """Read-only reconciliation using the host's authorized, revision-specific reader.

    original_args are the original save request. The host reader enforces its own
    read authorization for the supplied authority, element ID and revision. A match
    establishes the desired successor exists; it does not identify its writer.
    """
    from integration_tool_service import ReadFailure, failure, normalize
    import tool_loader
    import tools_fallback

    if not matches(description):
        return failure("invalid_input")
    try:
        args = normalize("elements", original_args)
        authority = service._resolve_operation("elements", tenant, session, turn).authority
    except ReadFailure as exc:
        return failure(str(exc))
    successor = args.get("expectedRevision", 0) + 1
    element = args["recipe"]
    wanted = _digest(element)
    request = {"successor": successor, "requested_digest": wanted, "observed": None}
    if read_revision is not None:
        try:
            saved = read_revision(authority, element["id"], successor)
            if (type(saved) is dict and type(saved.get("revision")) is int
                    and isinstance(saved.get("element"), dict)):
                request["observed"] = {"revision": saved["revision"], "digest": _digest(saved["element"])}
        except ReadFailure as exc:
            if str(exc) in {"denied", "unauthenticated"}:
                return failure(str(exc))
        except Exception:
            # An unavailable read or malformed provider reply cannot settle a save.
            pass

    package, source, _ = tools_fallback.author_tool(description)
    if package["engine_op"] != "recipe_save_recovery":
        return failure("invalid_input")
    status, payload = tool_loader._run_source_in_sandbox(source, "recipe_recovery.py",
                                                        {"recipe_save_recovery": request}, {})
    try:
        if service._resolve_operation("elements", tenant, session, turn).authority != authority:
            return failure("denied")
    except ReadFailure as exc:
        return failure(str(exc))
    if status != "ok" or not payload[0]["successor_observed"]:
        return failure("outcome_unknown")
    return {"result": {"element": element, "revision": successor}}
