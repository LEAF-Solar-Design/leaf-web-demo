"""Read back a lost recipe save at the original successor without replaying a write."""
import hashlib
import json
import math
import re

from json_resource_limits import within_depth_budget

JOURNAL_FORMAT_VERSION = 1
JOURNAL_MAX_DEPTH = 64  # The root object counts as the first container.


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


def request_retry_key(authority, request):
    return _digest({"operation": "inspection-recipe-save-v1", "origin": authority.origin,
                    "tenant": authority.tenant_id, "project": authority.project_id,
                    "subject": authority.subject, "request": request})


def finite_seconds(value):
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _journal_request(raw, digest, authority, clock, max_age):
    """A host-provided checksum pins the bytes; the retry key pins their scoped operation."""
    from authored_recipe_edit import prepare_inspection_edit
    from integration_tool_service import REQUEST_CAP, ReadFailure, normalize, strict_json

    if type(raw) is not bytes:
        return None
    if len(raw) > 2 * REQUEST_CAP + 8192:
        raise ReadFailure("journal_byte_limit")
    if hashlib.sha256(raw).hexdigest() != digest:
        return None
    if not within_depth_budget(raw, JOURNAL_MAX_DEPTH):
        raise ReadFailure("journal_depth_limit")
    try:
        intent = strict_json(raw)
    except (ValueError, RecursionError, ReadFailure):
        return None
    if type(intent) is not dict:
        return None
    # Legacy journals had the same fields and semantics without a version tag.
    version = intent.pop("format_version", 1)
    if type(version) is not int or version != JOURNAL_FORMAT_VERSION:
        raise ReadFailure("unsupported_journal_version")
    if "recorded_at" in intent and not finite_seconds(intent["recorded_at"]):
        return None
    recorded_at = intent.pop("recorded_at", None)
    if max_age is not None:
        if recorded_at is None or not finite_seconds(max_age) or max_age < 0:
            return None
        now = clock()
        if not finite_seconds(now) or not 0 <= now - recorded_at <= max_age:
            return None
    if set(intent) != {"description", "snapshot", "retry_key"}:
        return None
    try:
        args, _ = prepare_inspection_edit(intent["description"], intent["snapshot"])
        request = normalize("elements", args)
        return request if request_retry_key(authority, request) == intent["retry_key"] else None
    except (ValueError, RecursionError, ReadFailure):
        return None


def recover_recipe_save(service, description, original_args, tenant, session, turn, *, read_revision=None,
                        expected_authority=None, journal_sha256=None, journal_max_age_seconds=None,
                        journal_adapter=None):
    """Read-only reconciliation using the host's authorized, revision-specific reader.

    original_args are the original save request. The host reader enforces its own
    read authorization for the supplied authority, element ID and revision. A match
    establishes the desired successor exists; it does not identify its writer.
    If the host supplies a maximum journal age, the pinned creation time must
    fall within the inclusive retention window at the start of recovery.
    A trusted adapter may supply bytes by (authority, digest) and discard that
    exact record after confirmation. Cleanup does not determine the save outcome.
    """
    from integration_tool_service import ReadFailure, failure, normalize
    from platform_link import ProjectSessionForbidden
    import tool_loader
    import tools_fallback

    if not matches(description):
        return failure("invalid_input")
    try:
        authority = service._resolve_operation("elements", tenant, session, turn).authority
        if expected_authority is not None and authority != expected_authority:
            return failure("denied")
        if journal_adapter is not None:
            if original_args is not None:
                return failure("invalid_input")
            if type(journal_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", journal_sha256):
                try:
                    original_args = journal_adapter.read(authority, journal_sha256)
                except (OSError, KeyError):
                    pass
        if (journal_adapter is not None or isinstance(original_args, bytes)
                or journal_sha256 is not None or journal_max_age_seconds is not None):
            args = _journal_request(original_args, journal_sha256, authority, service.clock, journal_max_age_seconds)
        else:
            args = normalize("elements", original_args)
    except ReadFailure as exc:
        return failure(str(exc))
    request = {"observed": None}
    if args is not None:
        successor = args.get("expectedRevision", 0) + 1
        element = args["recipe"]
        request.update(successor=successor, requested_digest=_digest(element))
    if args is not None and read_revision is not None:
        try:
            saved = read_revision(authority, element["id"], successor)
            if (type(saved) is dict and type(saved.get("revision")) is int
                    and isinstance(saved.get("element"), dict)):
                request["observed"] = {"revision": saved["revision"], "digest": _digest(saved["element"])}
        except ReadFailure as exc:
            if str(exc) in {"denied", "unauthenticated"}:
                return failure(str(exc))
        except ProjectSessionForbidden:
            return failure("denied")
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
    if args is None or status != "ok" or not payload[0]["successor_observed"]:
        return failure("outcome_unknown")
    if journal_adapter is not None:
        try:
            journal_adapter.discard(authority, journal_sha256)
        except (OSError, KeyError):
            pass
    return {"result": {"element": element, "revision": successor}}
