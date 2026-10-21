"""Fixed, turn-bound integration reads and recipe saves. No setup at import.

Only trusted application code can inject dependencies. Request data never selects
custody, authority, HTTP clients, origins, or a permission implementation.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import time
import httpx
from urllib.parse import quote, quote_plus, unquote, unquote_plus
from uuid import UUID, uuid4

import deps
import entitlements
import platform_link
import session_store
import turn_runner
import agent_gate

try:
    from . import integration_turn_grant_crypto as crypto
except ImportError:
    import integration_turn_grant_crypto as crypto

platform_link._ensure_platform_package()
from leaf_platform.integration_turn_grant_store import Authority, IntegrationTurnGrantStore

REQUEST_CAP = 128 * 1024
RESPONSE_CAP = 256 * 1024
STATUS = {"invalid_input": 400, "unauthenticated": 401, "denied": 403,
          "unavailable": 503, "conflict": 409, "invalid_response": 502,
          "timeout": 504, "response_too_large": 502, "not_found": 404,
          "outcome_unknown": 502}


class ReadFailure(Exception):
    """Only a fixed category may cross an internal boundary."""


class DefiniteHTTPFailure(ReadFailure):
    """A save was explicitly rejected by the application service."""


def failure(code):
    return {"error": {"code": code, "correlationId": str(uuid4())}}


def project_gate(verdict):
    """Project the shared gate producer onto the integration wire contract."""
    if type(verdict) is not dict or verdict.get("decision") not in {"allow", "deny", "awaiting_approval"}:
        raise ReadFailure("invalid_response")
    projected = {"decision": verdict["decision"]}
    for key, limit in (("reason", 512), ("policy", 128)):
        value = verdict.get(key)
        if value is not None:
            if type(value) is not str:
                raise ReadFailure("invalid_response")
            projected[key] = value[:limit]
    rung = verdict.get("rung")
    if rung is not None:
        if type(rung) is not int or not 0 <= rung <= 7:
            raise ReadFailure("invalid_response")
        projected["rung"] = rung
    if verdict["decision"] == "awaiting_approval":
        confirmation = verdict.get("confirmation_id")
        if type(confirmation) is not str or not re.fullmatch(r"[a-f0-9]{16}", confirmation):
            raise ReadFailure("invalid_response")
        projected["confirmation_id"] = confirmation
    return projected


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError()


def strict_json(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
    # Also reject numeric overflow (1e999 is accepted by json.loads).
    json.dumps(value, allow_nan=False)
    return value


def _local_refs(value):
    if isinstance(value, dict):
        if "$ref" in value and (not isinstance(value["$ref"], str)
                                or not value["$ref"].startswith("#/$defs/")):
            raise ValueError()
        for item in value.values():
            _local_refs(item)
    elif isinstance(value, list):
        for item in value:
            _local_refs(item)


@lru_cache(maxsize=4)
def _validator(name):
    # Lazy: startup and disabled integration calls do not read contracts.
    from jsonschema import Draft202012Validator
    document = strict_json((Path(__file__).resolve().parents[1]
                            / "contract/assistant.schema.json").read_bytes())
    _local_refs(document)
    schema = {"$defs": document["$defs"], "$ref": "#/$defs/" + name}
    return Draft202012Validator(schema)


def validate(name, value):
    if not _validator(name).is_valid(value):
        raise ReadFailure("invalid_input")


def normalize(operation, value):
    if type(value) is not dict:
        raise ReadFailure("invalid_input")
    confirmation = value.get("confirmation_id")
    if "confirmation_id" in value and (type(confirmation) is not str
                                        or re.fullmatch(r"[0-9a-f]{16}", confirmation) is None):
        raise ReadFailure("invalid_input")
    if operation == "datasets":
        if set(value) - {"limit", "after", "confirmation_id"}:
            raise ReadFailure("invalid_input")
        limit = value.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ReadFailure("invalid_input")
        args = {"operation": "datasets", "limit": limit}
        if "after" in value:
            after = value["after"]
            if (type(after) is not str or not 1 <= len(after) <= 1024
                    or re.fullmatch(r"[A-Za-z0-9_-]+", after) is None):
                raise ReadFailure("invalid_input")
            args["after"] = after
    elif operation == "elements":
        if set(value) - {"recipe", "expectedRevision", "confirmation_id"} or "recipe" not in value:
            raise ReadFailure("invalid_input")
        validate("IntegrationElementV1", value["recipe"])
        args = {"recipe": value["recipe"]}
        if "expectedRevision" in value:
            revision = value["expectedRevision"]
            if type(revision) is not int or not 1 <= revision <= 9007199254740991:
                raise ReadFailure("invalid_input")
            args["expectedRevision"] = revision
        # Snapshot the complete recipe before any permission callback runs.
        try:
            args = strict_json(json.dumps(args, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            raise ReadFailure("invalid_input") from None
        if len(json.dumps(args, separators=(",", ":")).encode("utf-8")) > REQUEST_CAP:
            raise ReadFailure("invalid_input")
    elif operation == "query":
        if set(value) - {"dataset_ref", "query", "confirmation_id"} or not {"dataset_ref", "query"} <= set(value):
            raise ReadFailure("invalid_input")
        validate("IntegrationRefV1", value["dataset_ref"])
        validate("DatasetQueryV1", value["query"])
        args = {"operation": "query", "dataset_ref": value["dataset_ref"], "query": value["query"]}
    else:
        raise ReadFailure("invalid_input")
    if confirmation is not None:
        args["confirmation_id"] = confirmation
    return args


def canonical_uuid(value):
    if type(value) is not str or len(value) != 36 or str(UUID(value)) != value or UUID(value).int == 0:
        raise ReadFailure("denied")
    return value


def authority_headers(tenant, session, turn):
    canonical_uuid(tenant)
    for value in (session, turn):
        if (type(value) is not str or not 1 <= len(value) <= 200
                or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ReadFailure("invalid_input")
    return tenant, session, turn


@dataclass(frozen=True)
class Current:
    authority: Authority
    tier: str
    capabilities: dict


def resolve_current(tenant_id, session_id, turn_id, *, write=False):
    if os.environ.get("LEAF_INTEGRATION_TURN_GRANTS_ENABLED") != "1":
        raise ReadFailure("unavailable")
    if not deps.auth_live():
        raise ReadFailure("unauthenticated")
    authority_headers(tenant_id, session_id, turn_id)
    session = session_store.get_session(session_id)
    if (not session or session.get("session_id") != session_id
            or session.get("org_id") != tenant_id or not session.get("project_id")):
        raise ReadFailure("denied")
    project = canonical_uuid(session["project_id"])
    subject = session_store.active_turn_subject(
        session_id, turn_id, session["tenant_id"], turn_runner.turn_max_s())
    if not subject:
        raise ReadFailure("conflict")
    tenant, tier = deps.resolve_active_platform_tenant_authority(subject)
    if tenant != tenant_id:
        raise ReadFailure("denied")
    context = deps.TenantContext(tenant_id, org_id=tenant_id, tier=tier,
                               subject=subject, authority_resolved=True)
    binding = platform_link.resolve_caller_binding(context)
    if (binding is None or binding.status != "active" or binding.external_authority != "auth0"
            or binding.external_subject != subject or str(binding.platform_tenant_id) != tenant_id):
        raise ReadFailure("denied")
    allowed = None
    try:
        allowed = platform_link.require_project_session_access(session, context, write=write, binding=binding)
    except (platform_link.ProjectSessionForbidden, LookupError):
        pass
    if allowed is None:
        raise ReadFailure("denied")
    caps = entitlements.entitlements_for(tier)
    if caps.get("converse") is not True:
        raise ReadFailure("denied")
    authority = Authority(tenant_id, project, str(binding.platform_user_id), str(binding.binding_id),
                          "auth0", subject, session_id, turn_id,
                          os.environ.get("LEAF_INTEGRATION_SERVICE_ORIGIN", ""))
    return Current(authority, tier, caps)


def _date(value, instant=False):
    pattern = (r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?Z"
               if instant else r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        return False
    year, month, day = map(int, value[:10].split("-"))
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return 1 <= month <= 12 and 1 <= day <= days[month - 1]


def _matches(field, value):
    if value is None:
        return field["nullable"]
    kind = field["type"]["kind"]
    if kind in {"text", "identifier"}:
        return type(value) is str and (kind == "text" or len(value) > 0)
    if kind == "boolean":
        return type(value) is bool
    if kind in {"date", "instant"}:
        return _date(value, kind == "instant")
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    if kind == "count" or (kind == "money" and field["type"]["scale"] == "minor"):
        return value == int(value) and abs(value) <= 9007199254740991 and (kind != "count" or value >= 0)
    return True


def _fields(fields):
    if type(fields) is not list or not 1 <= len(fields) <= 100:
        raise ReadFailure("invalid_response")
    for field in fields:
        validate("DatasetFieldV1", field)
    if len({field["id"] for field in fields}) != len(fields):
        raise ReadFailure("invalid_response")


def validate_read(value, reference):
    validate("DatasetReadV1", value)
    _fields(value["fields"])
    if value["dataset_ref"] != reference:
        raise ReadFailure("invalid_response")
    rows = value["rows"]
    if len({row["key"] for row in rows}) != len(rows):
        raise ReadFailure("invalid_response")
    if value["status"] in {"empty", "unavailable", "forbidden", "invalid", "failed"} and rows:
        raise ReadFailure("invalid_response")
    fields = {field["id"]: field for field in value["fields"]}
    for row in rows:
        if set(row["values"]) - set(fields):
            raise ReadFailure("invalid_response")
        for key, field in fields.items():
            if key not in row["values"]:
                if not field["nullable"]:
                    raise ReadFailure("invalid_response")
            elif not _matches(field, row["values"][key]):
                raise ReadFailure("invalid_response")
    if any(v is not None and not _date(v, True) for v in value["freshness"].values()):
        raise ReadFailure("invalid_response")
    return value


def project_discovery(value, limit):
    """Project the Ops application-service discovery envelope, without receipts."""
    if type(value) is not dict or set(value) != {"records", "nextCursor"}:
        raise ReadFailure("invalid_response")
    records, cursor = value["records"], value["nextCursor"]
    if type(records) is not list or len(records) > limit:
        raise ReadFailure("invalid_response")
    if cursor is not None and (type(cursor) is not str or not 1 <= len(cursor) <= 1024
                              or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None):
        raise ReadFailure("invalid_response")
    datasets = []
    identities = set()
    for record in records:
        if type(record) is not dict:
            raise ReadFailure("invalid_response")
        reference = record.get("reference")
        validate("IntegrationRefV1", reference)
        identity = (reference["id"], reference["version"], reference["digest"])
        if identity in identities:
            raise ReadFailure("invalid_response")
        identities.add(identity)
        title, fields = record.get("title"), record.get("fields")
        if type(title) is not str or not 1 <= len(title) <= 120:
            raise ReadFailure("invalid_response")
        _fields(fields)
        if not {"lastAttempt", "freshness"} <= set(record):
            raise ReadFailure("invalid_response")
        attempt, freshness = record["lastAttempt"], record["freshness"]
        if freshness is not None:
            # Reuse the canonical read schema, plus its semantic date checks.
            validate_read({"schema_version": 1, "dataset_ref": reference,
                           "fields": fields, "rows": [], "next_cursor": None,
                           "status": "empty", "freshness": freshness}, reference)
        if attempt is None:
            if freshness is not None:
                raise ReadFailure("invalid_response")
            status = "unavailable"
        else:
            if (type(attempt) is not dict or attempt.get("outcome") not in {"success", "failure"}
                    or type(attempt.get("row_count")) is not int or attempt["row_count"] < 0):
                raise ReadFailure("invalid_response")
            if attempt["outcome"] == "success":
                if freshness is None:
                    raise ReadFailure("invalid_response")
                status = "empty" if attempt["row_count"] == 0 else "ready"
            else:
                status = "stale" if freshness is not None else "unavailable"
        datasets.append({"dataset_ref": reference, "title": title, "fields": fields,
                         "freshness": freshness, "status": status})
    return {"datasets": datasets, "next_cursor": cursor}


async def bounded_http(method, url, token, body, deadline, transport=None, *, save=False,
                       expected_revision=None):
    import httpx
    headers = {"Authorization": "Bearer " + token.decode("utf-8"),
               "Accept": "application/json", "Accept-Encoding": "identity",
               "Content-Type": "application/json"}
    if save and expected_revision is not None:
        headers["If-Match"] = str(expected_revision)
    async with asyncio.timeout(deadline):
        async with httpx.AsyncClient(verify=True, follow_redirects=False, trust_env=False,
                                    timeout=deadline, transport=transport) as client:
            async with client.stream(method, url, headers=headers,
                                     content=body) as response:
                if 300 <= response.status_code < 400:
                    raise ReadFailure("invalid_response")
                if response.status_code != 200:
                    if save and response.status_code in {400, 401, 403, 404, 409}:
                        raise DefiniteHTTPFailure({400: "invalid_input", 401: "unauthenticated",
                                                   403: "denied", 404: "not_found",
                                                   409: "conflict"}[response.status_code])
                    raise ReadFailure({400: "invalid_input", 401: "unauthenticated",
                                       403: "denied", 409: "conflict", 504: "timeout"
                                       }.get(response.status_code, "unavailable"))
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ReadFailure("invalid_response")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ReadFailure("invalid_response")
                raw = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > RESPONSE_CAP:
                            raise ReadFailure("response_too_large")
                        raw.extend(chunk)
                    parsed = None
                    try:
                        parsed = strict_json(raw)
                    except Exception:
                        pass
                    if parsed is None:
                        raise ReadFailure("invalid_response")
                    return parsed
                finally:
                    crypto._scrub(raw)


def _reflection(value, token):
    text = token.decode("utf-8")
    variants = {text, json.dumps(text, ensure_ascii=True)[1:-1],
                json.dumps(text, ensure_ascii=False)[1:-1], quote(text, safe=""), quote_plus(text, safe="")}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(token).decode("ascii")
        variants.update((encoded, encoded.rstrip("=")))
    def scan(item):
        if type(item) is str:
            candidates = (item, unquote(item), unquote_plus(item))
            return any(variant and variant in candidate for variant in variants for candidate in candidates)
        if type(item) is dict:
            return any(scan(key) or scan(val) for key, val in item.items())
        if type(item) is list:
            return any(scan(val) for val in item)
        return False
    return scan(value)


class IntegrationToolService:
    def __init__(self, *, resolve=resolve_current, store=None, decrypt=crypto.with_decrypted,
                 gate=agent_gate.gate, clock=time.time, transport=None, keys=None, resolve_write=None):
        self.resolve = resolve
        self.resolve_write = resolve_write
        self.store = store if store is not None else IntegrationTurnGrantStore()
        self.decrypt, self.gate, self.clock = decrypt, gate, clock
        self.transport, self.keys = transport, keys

    def execute(self, operation, value, tenant, session, turn):
        result = None
        code = "unavailable"
        try:
            args = normalize(operation, value)
            saving = operation == "elements"
            def resolve():
                if saving:
                    if self.resolve_write is not None:
                        return self.resolve_write(tenant, session, turn)
                    return resolve_current(tenant, session, turn, write=True)
                return self.resolve(tenant, session, turn)
            current = resolve()
            custody = self.store.load_active(current.authority, self.clock())
            if custody is None:
                raise ReadFailure("unavailable")
            binding = custody.binding
            binding.__post_init__()
            required = ("element_save" if saving else
                        "dataset_discovery" if operation == "datasets" else "dataset_query")

            def check():
                fresh = resolve()
                now = self.clock()
                if (fresh != current or binding.authority != fresh.authority
                        or required not in fresh.authority.operations
                        or not binding.issued_at <= now < binding.expires_at):
                    raise ReadFailure("denied")
                return fresh

            check()

            def consume(token):
                # All provider exceptions die here; only bounded, nonsecret
                # result objects leave the synchronous custody callback.
                output = None
                category = "unavailable"
                started = False
                try:
                    fresh = check()
                    verdict = self.gate(
                        fresh.authority.tenant_id, fresh.authority.session_id, fresh.authority.turn_id,
                        "save_integration_element" if saving else "read_integration_data",
                        args, fresh.capabilities, tier=fresh.tier,
                        subject=fresh.authority.subject, policy_session_id=fresh.authority.session_id)
                    if _reflection(verdict, token):
                        raise ReadFailure("invalid_response")
                    output = {"gate": project_gate(verdict)}
                    if verdict["decision"] == "allow":
                        check()
                        base = fresh.authority.origin + "/api/v1/projects/" + fresh.authority.project_id + "/integrations/datasets"
                        if saving:
                            method = "PUT" if "expectedRevision" in args else "POST"
                            url = fresh.authority.origin + "/api/v1/projects/" + fresh.authority.project_id + "/integrations/elements"
                            if method == "PUT":
                                url += "/" + args["recipe"]["id"]
                            body = json.dumps(args["recipe"], allow_nan=False, separators=(",", ":")).encode("utf-8")
                        elif operation == "datasets":
                            method, body = "GET", None
                            url = base + "?limit=" + str(args["limit"])
                            if "after" in args:
                                url += "&after=" + quote(args["after"], safe="")
                        else:
                            ref = args["dataset_ref"]
                            method = "POST"
                            url = base + "/" + ref["id"] + "/versions/" + str(int(ref["version"])) + "/query"
                            body = json.dumps(args["query"], allow_nan=False, separators=(",", ":")).encode("utf-8")
                            if len(body) > REQUEST_CAP:
                                raise ReadFailure("invalid_input")
                        remaining = min(10.0, binding.expires_at - self.clock())
                        if remaining <= 0:
                            raise ReadFailure("denied")
                        started = True
                        if saving:
                            value = asyncio.run(bounded_http(method, url, token, body, remaining, self.transport,
                                save=True, expected_revision=args.get("expectedRevision")))
                        else:
                            value = asyncio.run(bounded_http(method, url, token, body, remaining, self.transport))
                        check()
                        # Scan the entire provider object, including discarded extras.
                        if _reflection(value, token):
                            raise ReadFailure("invalid_response")
                        try:
                            if saving:
                                if type(value) is not dict:
                                    raise ReadFailure("invalid_response")
                                validate("IntegrationElementV1", value.get("element"))
                                revision = value.get("revision")
                                if (type(revision) is not int or not 1 <= revision <= 9007199254740991
                                        or json.dumps(value["element"], sort_keys=True, allow_nan=False)
                                        != json.dumps(args["recipe"], sort_keys=True, allow_nan=False)):
                                    raise ReadFailure("invalid_response")
                                projected = {"element": value["element"], "revision": revision}
                            else:
                                projected = (project_discovery(value, args["limit"]) if operation == "datasets"
                                             else validate_read(value, args["dataset_ref"]))
                        except ReadFailure:
                            raise ReadFailure("invalid_response") from None
                        output["result"] = projected
                    if _reflection(output, token):
                        raise ReadFailure("invalid_response")
                    if verdict["decision"] == "allow":
                        check()
                    return output
                except DefiniteHTTPFailure as exc:
                    category = str(exc)
                except ReadFailure as exc:
                    category = ("outcome_unknown" if saving and started else
                                str(exc) if str(exc) in STATUS else "unavailable")
                except (TimeoutError, httpx.TimeoutException):
                    category = "outcome_unknown" if saving and started else "timeout"
                except Exception:
                    if saving and started:
                        category = "outcome_unknown"
                return failure(category)

            result = self.decrypt(custody.envelope, binding, consume, self.keys)
        except ReadFailure as exc:
            code = str(exc) if str(exc) in STATUS else "unavailable"
        except Exception:
            pass
        # No raw exception/context or plaintext-bearing request escapes.
        return result if result is not None else failure(code)
