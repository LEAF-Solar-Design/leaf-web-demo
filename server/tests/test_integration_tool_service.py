"""Hermetic integration reads: real custody/validators and mock HTTP only."""
import platform as _stdlib_platform
_stdlib_platform.python_implementation()

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
import integration_tool_service as service
import agent_gate
import agent_policy
import session_policy
from test_integration_turn_grant_capture import custody
from leaf_platform.integration_turn_grant_store import (
    Authority, Binding, EncryptedGrant, LEGACY_OPERATIONS, CURRENT_OPERATIONS,
)

TOKEN = b'synthetic-bearer/plus+quote"secret'
ARN = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-4234-8234-123456789012"
REF = {"id": "sales", "version": 1, "digest": "a" * 64}
QUERY = {"fields": ["amount"], "limit": 5}
FIELD = {"id": "amount", "label": "Amount", "type": {"kind": "number", "unit": "kWh"},
         "nullable": False, "sensitivity": "internal"}
ACTION = {"connection_id": "sales", "connection_title": "Sales", "connector_ref": REF,
          "operation_ref": {**REF, "id": "create-sale"}, "title": "Create sale",
          "form": {"submit_label": "Create", "fields": [
              {"id": "amount", "label": "Amount", "kind": "number", "integer": False,
               "required": True, "nullable": False}]}}


def read():
    return {"schema_version": 1, "dataset_ref": deepcopy(REF), "fields": [deepcopy(FIELD)],
            "rows": [{"key": "one", "values": {"amount": 1.25}}], "next_cursor": None,
            "status": "ready", "freshness": {"source_updated_at": None, "synced_at": None,
                                               "fetched_at": "2026-10-20T12:00:00Z"}}


class Keys:
    def __init__(self):
        self.buffers = []

    def canonical_key_arn(self):
        return ARN

    def generate_data_key(self, arn, context):
        return self.decrypt_data_key(arn, b"wrapped", context), b"wrapped"

    def decrypt_data_key(self, arn, wrapped, context):
        key = bytearray(b"k" * 32)
        self.buffers.append(key)
        return key


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(service.crypto.KmsKeyService, "canonical_key_arn",
                        lambda self: pytest.fail("live KMS forbidden"))
    auth = Authority(*(str(uuid4()) for _ in range(4)), "auth0", "auth0|person",
                     "app-session", "turn-1", "https://ops.example.com")
    current = service.Current(auth, "pro", {"converse": True})
    binding = Binding(auth, str(uuid4()), 1000, 1120, 1120, 1100)
    keys = Keys()
    envelope = service.crypto.seal(TOKEN, binding, keys)
    state = SimpleNamespace(current=current, resolutions=0, gates=[], requests=[], response=read(),
                            decision="allow", custody=EncryptedGrant(binding, envelope))

    def resolve(*headers):
        state.resolutions += 1
        assert headers == (auth.tenant_id, auth.session_id, auth.turn_id)
        return state.current

    def gate(*args, **kwargs):
        state.gates.append((deepcopy(args), kwargs))
        return {"decision": state.decision, "reason": "fixture", "policy": "auto", "rung": 1,
                **({"confirmation_id": "a" * 16} if state.decision == "awaiting_approval" else {})}

    async def http(request):
        state.requests.append(request)
        return httpx.Response(200, json=state.response)

    store = SimpleNamespace(load_active=lambda authority, now: state.custody)
    state.svc = service.IntegrationToolService(resolve=resolve, store=store, keys=keys, gate=gate,
                                              clock=lambda: 1001, transport=httpx.MockTransport(http))
    state.call = lambda value=None: state.svc.execute("query", value if value is not None else
        {"dataset_ref": deepcopy(REF), "query": deepcopy(QUERY)}, auth.tenant_id, auth.session_id, auth.turn_id)
    state.auth, state.keys = auth, keys
    return state


@pytest.fixture
def catalog(rig):
    rig.auth = replace(rig.auth, operations=CURRENT_OPERATIONS)
    rig.current = replace(rig.current, authority=rig.auth)
    binding = replace(rig.custody.binding, authority=rig.auth)
    rig.custody = EncryptedGrant(binding, service.crypto.seal(TOKEN, binding, rig.keys))
    rig.svc.resolve_write = rig.svc.resolve
    rig.response = {"actions": [deepcopy(ACTION)], "next_cursor": "opaque_1"}
    rig.catalog_call = lambda value=None: rig.svc.execute("action_catalog", {} if value is None else value,
        rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    return rig


def test_catalog_canonical_wire_one_read_gate_and_no_effect(catalog):
    result = catalog.catalog_call({"limit": 1, "after": "opaque_0"})
    assert result["result"] == catalog.response
    assert len(catalog.gates) == len(catalog.requests) == 1
    args, kwargs = catalog.gates[0]
    assert args[:4] == (catalog.auth.tenant_id, "app-session", "turn-1", "read_integration_data")
    assert args[4] == {"operation": "action_catalog", "limit": 1, "after": "opaque_0"}
    assert kwargs["subject"] == catalog.auth.subject
    request = catalog.requests[0]
    assert request.method == "GET" and request.content == b""
    assert str(request.url) == (catalog.auth.origin + "/api/v1/projects/" + catalog.auth.project_id
                                + "/integrations/action-catalog?limit=1&after=opaque_0")
    assert request.headers["authorization"] == "Bearer " + TOKEN.decode()
    assert "if-match" not in request.headers
    assert catalog.resolutions >= 5
    assert all(not any(buf) for buf in catalog.keys.buffers)


def test_catalog_empty_page_can_advance(catalog):
    catalog.response = {"actions": [], "next_cursor": "opaque_1"}
    assert catalog.catalog_call()["result"] == catalog.response
    assert catalog.gates[0][0][4] == {"operation": "action_catalog", "limit": 50}


@pytest.mark.parametrize("decision", ["deny", "awaiting_approval"])
def test_catalog_gate_nonallow_does_not_request_ops(catalog, decision):
    catalog.decision = decision
    output = catalog.catalog_call({"after": "opaque_0", "confirmation_id": "a" * 16})
    assert set(output) == {"gate"} and output["gate"]["decision"] == decision
    assert not catalog.requests and len(catalog.gates) == 1
    assert catalog.gates[0][0][4] == {"operation": "action_catalog", "limit": 50,
                                      "after": "opaque_0", "confirmation_id": "a" * 16}


@pytest.mark.parametrize("stored,current", [(LEGACY_OPERATIONS, CURRENT_OPERATIONS),
                                           (LEGACY_OPERATIONS, LEGACY_OPERATIONS),
                                           (CURRENT_OPERATIONS, LEGACY_OPERATIONS)])
def test_catalog_checks_both_grants_before_decryption(catalog, stored, current):
    binding = replace(catalog.custody.binding, authority=replace(catalog.auth, operations=stored))
    catalog.custody = EncryptedGrant(binding, b"must-not-decrypt")
    catalog.current = replace(catalog.current, authority=replace(catalog.auth, operations=current))
    decrypts = []
    catalog.svc.decrypt = lambda *args: decrypts.append(args)
    assert catalog.catalog_call()["error"]["code"] == "denied"
    assert not decrypts and not catalog.gates and not catalog.requests


@pytest.mark.parametrize("value", [
    {"limit": 0}, {"limit": 51}, {"limit": True}, {"limit": 1.5}, {"after": ""},
    {"after": "bad cursor"}, {"after": "bad\n"}, {"after": "a" * 1025},
    {"confirmation_id": "A" * 16}, {"authority": {}}, {"tenant_id": "forged"},
    {"url": "https://foreign"}, {"headers": {}}, {"operation": "action_catalog"},
])
def test_catalog_invalid_input_precedes_authority(catalog, value):
    assert catalog.catalog_call(value)["error"]["code"] == "invalid_input"
    assert catalog.resolutions == 0 and not catalog.gates and not catalog.requests


@pytest.mark.parametrize("change", ["over_limit", "extra_envelope", "extra_entry", "bad_ref", "bad_cursor",
    "empty_cursor", "long_cursor", "bad_form", "duplicate_fields", "reserved_field", "number_bounds",
    "text_bounds", "duplicate_choices", "wrong_tool", "reflected"])
def test_catalog_rejects_malformed_canonical_data(catalog, change):
    action = catalog.response["actions"][0]
    field = action["form"]["fields"][0]
    if change == "over_limit":
        catalog.response["actions"].append(deepcopy(action))
    elif change == "extra_envelope":
        catalog.response["authority"] = "forged"
    elif change == "extra_entry":
        action["url"] = "https://foreign"
    elif change == "bad_ref":
        action["operation_ref"]["digest"] = "bad"
    elif change == "bad_cursor":
        catalog.response["next_cursor"] = "bad="
    elif change == "empty_cursor":
        catalog.response["next_cursor"] = ""
    elif change == "long_cursor":
        catalog.response["next_cursor"] = "a" * 1025
    elif change == "bad_form":
        action["form"]["arguments"] = {"amount": 1}
    elif change == "duplicate_fields":
        action["form"]["fields"].append(deepcopy(field))
    elif change == "reserved_field":
        field["id"] = "__proto__"
    elif change == "number_bounds":
        field.update(minimum=2, maximum=1)
    elif change == "text_bounds":
        field.pop("integer")
        field.update(kind="text", min_length=2, max_length=1)
    elif change == "duplicate_choices":
        field.pop("integer")
        field.update(kind="choice", choices=[{"label": "A", "value": "a"}, {"label": "B", "value": "a"}])
    elif change == "wrong_tool":
        catalog.response = {"datasets": [], "next_cursor": None}
    else:
        action["connection_title"] = TOKEN.decode()
    result = catalog.catalog_call({"limit": 1})
    assert result["error"]["code"] == "invalid_response"
    assert len(catalog.requests) == len(catalog.gates) == 1
    assert TOKEN.decode() not in json.dumps(result)


@pytest.mark.parametrize("status,body,code", [
    (302, b"", "invalid_response"), (403, b"", "denied"), (409, b"", "conflict"),
    (200, b"x" * (service.RESPONSE_CAP + 1), "response_too_large"),
], ids=["redirect", "denied", "conflict", "oversized"])
def test_catalog_bounded_transport_never_reports_unknown_save(catalog, status, body, code):
    requests = []
    async def mock(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})
    catalog.svc.transport = httpx.MockTransport(mock)
    assert catalog.catalog_call()["error"]["code"] == code
    assert len(requests) == 1


def test_query_fixed_wire_and_exactly_one_gate(rig):
    result = rig.call()
    assert result == {"gate": {"decision": "allow", "reason": "fixture", "policy": "auto", "rung": 1},
                      "result": read()}
    assert len(rig.gates) == len(rig.requests) == 1
    args, kwargs = rig.gates[0]
    assert args[:4] == (rig.auth.tenant_id, "app-session", "turn-1", "read_integration_data")
    assert args[4] == {"operation": "query", "dataset_ref": REF, "query": QUERY}
    assert kwargs == {"tier": "pro", "subject": "auth0|person", "policy_session_id": "app-session"}
    request = rig.requests[0]
    assert request.method == "POST"
    assert str(request.url) == ("https://ops.example.com/api/v1/projects/" + rig.auth.project_id
                                + "/integrations/datasets/sales/versions/1/query")
    assert json.loads(request.content) == QUERY
    assert request.headers["authorization"] == "Bearer " + TOKEN.decode()
    assert "confirmation_id" not in request.content.decode()
    assert all(not any(buf) for buf in rig.keys.buffers)


@pytest.mark.parametrize("decision", ["deny", "awaiting_approval"])
def test_gate_nonallow_never_transports(rig, decision):
    rig.decision = decision
    result = rig.call()
    assert set(result) == {"gate"}
    assert result["gate"]["decision"] == decision
    assert len(rig.gates) == 1 and not rig.requests


@pytest.mark.parametrize("value", [
    {}, {"dataset_ref": REF, "query": QUERY, "tenant_id": "foreign"},
    {"dataset_ref": {**REF, "id": "../foreign"}, "query": QUERY},
    {"dataset_ref": REF, "query": {**QUERY, "url": "https://foreign"}},
    {"dataset_ref": REF, "query": QUERY, "confirmation_id": "A" * 16},
])
def test_invalid_input_before_authority_or_gate(rig, value):
    assert rig.call(value)["error"]["code"] == "invalid_input"
    assert not rig.gates and not rig.requests and rig.resolutions == 0


def test_missing_and_expired_custody(rig):
    rig.custody = None
    assert rig.call()["error"]["code"] == "unavailable"
    assert not rig.gates and not rig.requests


def test_expiry_checked_even_if_store_returns_expired(rig):
    rig.svc.clock = lambda: 1100
    assert rig.call()["error"]["code"] == "denied"
    assert not rig.gates and not rig.requests


@pytest.mark.parametrize("operation", ["query", "datasets", "elements"])
def test_legacy_binding_serves_original_operations_under_current_authority_unchanged(rig, operation):
    original = rig.custody
    rig.current = replace(rig.current, authority=replace(rig.auth, operations=CURRENT_OPERATIONS))
    if operation == "query":
        result = rig.call()
    else:
        if operation == "datasets":
            value = {}
            rig.response = {"records": [], "nextCursor": None}
        else:
            recipe = {"schema_version": 1, "id": "sales-view", "title": "Sales",
                      "dataset_ref": deepcopy(REF), "query": deepcopy(QUERY),
                      "kind": "dataset.table", "props": {"columns": ["amount"], "row_limit": 5}}
            value = {"recipe": recipe}
            rig.response = {"element": recipe, "revision": 1}
            rig.svc.resolve_write = rig.svc.resolve
        result = rig.svc.execute(operation, value, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert "result" in result
    assert len(rig.gates) == len(rig.requests) == 1
    assert rig.custody is original
    assert rig.custody.binding.authority.operations == LEGACY_OPERATIONS


def test_production_current_resolution_explicitly_includes_action_discovery(custody, monkeypatch):
    h = custody
    # Public session reads decode the PostgreSQL project storage marker.
    h.session["tenant_id"] = str(h.tenant)
    monkeypatch.setattr(service.deps, "auth_live", lambda: True)
    monkeypatch.setattr(service.deps, "resolve_active_platform_tenant_authority",
                        lambda subject: (str(h.tenant), "pro"))
    monkeypatch.setattr(service.entitlements, "entitlements_for", lambda tier: {"converse": True})
    monkeypatch.setattr(service.turn_runner, "turn_max_s", lambda: 300)
    current = service.resolve_current(str(h.tenant), "s", "t", write=True)
    assert current.authority.operations == CURRENT_OPERATIONS
    assert h.capture().code == "captured"
    assert h.puts[0].authority == current.authority


@pytest.mark.parametrize("stored_ops,current_ops,required,allowed", [
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS, "dataset_discovery", True),
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS, "dataset_query", True),
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS, "element_save", True),
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS, "action_discovery", False),
    (LEGACY_OPERATIONS, LEGACY_OPERATIONS, "action_discovery", False),
    (CURRENT_OPERATIONS, CURRENT_OPERATIONS, "action_discovery", True),
    (CURRENT_OPERATIONS, LEGACY_OPERATIONS, "dataset_query", False),
    (CURRENT_OPERATIONS, CURRENT_OPERATIONS, "unknown", False),
])
def test_bound_capability_condition_directly(rig, stored_ops, current_ops, required, allowed):
    binding = replace(rig.custody.binding, authority=replace(rig.auth, operations=stored_ops))
    current = replace(rig.current, authority=replace(rig.auth, operations=current_ops))
    if allowed:
        assert service._check_bound_authority(binding, current, current, required, 1001) is current
    else:
        with pytest.raises(service.ReadFailure, match="denied"):
            service._check_bound_authority(binding, current, current, required, 1001)


def test_missing_action_capability_denies_before_decrypt_gate_or_http(rig):
    rig.current = replace(rig.current, authority=replace(rig.auth, operations=CURRENT_OPERATIONS))
    rig.svc.resolve_write = rig.svc.resolve
    rig.svc.decrypt = lambda *args: pytest.fail("decryption before capability check")
    result = rig.svc.execute("action_catalog", {}, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert result["error"]["code"] == "denied"
    assert not rig.gates and not rig.requests


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "11111111-1111-4111-8111-111111111111"),
    ("project_id", "11111111-1111-4111-8111-111111111111"),
    ("platform_user_id", "11111111-1111-4111-8111-111111111111"),
    ("binding_id", "11111111-1111-4111-8111-111111111111"),
    ("subject", "auth0|other"), ("session_id", "other-session"),
    ("turn_id", "other-turn"), ("origin", "https://foreign.example.com"),
])
def test_legacy_binding_denies_every_current_identity_mismatch_before_decrypt(rig, monkeypatch, field, value):
    rig.current = replace(rig.current, authority=replace(rig.auth, operations=CURRENT_OPERATIONS, **{field: value}))
    rig.svc.decrypt = lambda *args: pytest.fail("foreign authority decrypted")
    assert rig.call()["error"]["code"] == "denied"
    assert not rig.gates and not rig.requests


@pytest.mark.parametrize("at", [2, 3, 4, 5])
def test_authority_drift_prevents_next_effect(rig, at):
    initial = rig.svc.resolve
    def drift(*headers):
        result = initial(*headers)
        if rig.resolutions >= at:
            return replace(result, authority=replace(result.authority, project_id=str(uuid4())))
        return result
    rig.svc.resolve = drift
    assert "result" not in rig.call()
    assert len(rig.requests) == (1 if at == 5 else 0)
    assert len(rig.gates) == (1 if at >= 4 else 0)


@pytest.mark.parametrize("patch", [
    {"dataset_ref": {**REF, "digest": "b" * 64}},
    {"rows": [{"key": "1", "values": {"amount": "1.25"}}]},
    {"rows": [{"key": "1", "values": {"amount": True}}]},
    {"rows": [{"key": "1", "values": {"amount": None}}]},
    {"rows": [{"key": "1", "values": {"unknown": 1}}]},
    {"status": "failed"}, {"status": "unrecognized"},
    {"fields": [FIELD, FIELD]},
    {"freshness": {"source_updated_at": None, "synced_at": None, "fetched_at": "2026-02-30T12:00:00Z"}},
])
def test_invalid_provider_data_is_not_tool_data(rig, patch):
    rig.response.update(patch)
    assert rig.call()["error"]["code"] == "invalid_response"


@pytest.mark.parametrize("kind,good,bad", [
    ({"kind": "text"}, "abc", 1), ({"kind": "identifier"}, "abc", ""),
    ({"kind": "boolean"}, False, 0), ({"kind": "count"}, 0, -1),
    ({"kind": "money", "currency": "USD", "scale": "minor"}, -10, 0.1),
    ({"kind": "date"}, "2000-02-29", "1900-02-29"),
    ({"kind": "instant"}, "2026-10-20T12:00:00.123Z", "2026-10-20T24:00:00Z"),
    ({"kind": "ratio", "scale": "percent"}, 55.5, "55%"),
    ({"kind": "duration", "unit": "s"}, 1.5, "1s"),
])
def test_declared_semantic_values(rig, kind, good, bad):
    rig.response["fields"][0]["type"] = kind
    rig.response["rows"][0]["values"]["amount"] = good
    assert "result" in rig.call()
    rig.response["rows"][0]["values"]["amount"] = bad
    assert rig.call()["error"]["code"] == "invalid_response"


@pytest.mark.parametrize("encode", [
    lambda s: s, lambda s: json.dumps(s)[1:-1], lambda s: quote(s, safe=""),
    lambda s: base64.b64encode(s.encode()).decode(),
    lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("="),
])
@pytest.mark.parametrize("where", ["key", "value"])
def test_reflection_including_discarded_extras(rig, caplog, encode, where):
    reflected = encode(TOKEN.decode())
    rig.response[reflected if where == "key" else "extra"] = "extra" if where == "key" else reflected
    output = rig.call()
    assert output["error"]["code"] == "invalid_response"
    assert TOKEN.decode() not in json.dumps(output) + caplog.text


def test_secret_exception_dies_inside_callback(rig, caplog):
    def failing_gate(*args, **kwargs):
        raise RuntimeError(TOKEN.decode())
    rig.svc.gate = failing_gate
    output = rig.call()
    assert output["error"]["code"] == "unavailable"
    assert TOKEN.decode() not in json.dumps(output) + caplog.text
    assert not rig.requests


@pytest.mark.parametrize("status,body,code", [
    (302, b"", "invalid_response"), (500, TOKEN, "unavailable"),
    (401, TOKEN, "unauthenticated"), (403, TOKEN, "denied"), (409, TOKEN, "conflict"),
    (200, b"not json", "invalid_response"),
    (200, b"x" * (service.RESPONSE_CAP + 1), "response_too_large"),
], ids=["redirect", "server", "unauthenticated", "denied", "conflict", "malformed", "oversized"])
def test_transport_failures_are_fixed(rig, status, body, code):
    requests = []
    async def mock(request):
        requests.append(request)
        return httpx.Response(status, content=body,
                              headers={"content-type": "application/json", "location": "https://foreign.test"})
    rig.svc.transport = httpx.MockTransport(mock)
    assert rig.call()["error"]["code"] == code
    assert len(requests) == 1


def test_overall_timeout_cancels_and_closes_stream():
    state = {"cancelled": False, "closed": False}
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b"{"
                await asyncio.sleep(10)
            finally:
                state["cancelled"] = True
        async def aclose(self):
            state["closed"] = True
    async def mock(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Slow())
    with pytest.raises(TimeoutError):
        asyncio.run(service.bounded_http("GET", "https://ops.example.com/fixed", bytearray(TOKEN),
                                         None, 0.01, httpx.MockTransport(mock)))
    assert state == {"cancelled": True, "closed": True}


def test_real_gate_plan_first_replay_and_argument_drift(rig, tmp_path, monkeypatch):
    for env, path in {"LEAF_AGENT_KILL_FILE": "kill", "LEAF_AGENT_APPROVALS_DIR": "approvals",
                      "LEAF_AGENT_GRANTS_FILE": "grants.json", "LEAF_AGENT_RATE_FILE": "rate.json",
                      "LEAF_AGENT_AUDIT": "audit.jsonl", "LEAF_AGENT_LEDGER": "ledger.jsonl",
                      "LEAF_AGENT_TENANTS_FILE": "tenants.json"}.items():
        monkeypatch.setenv(env, str(tmp_path / path))
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    def policy(session, tenant):
        assert (session, tenant) == (rig.auth.session_id, rig.auth.tenant_id)
        return "plan_first"
    monkeypatch.setattr(session_policy, "get_policy", policy)
    rig.svc.gate = agent_gate.gate
    pending = rig.call()["gate"]
    assert pending["decision"] == "awaiting_approval" and not rig.requests
    confirmation = pending["confirmation_id"]
    record = agent_gate.read_pending(confirmation)
    assert record["args"] == {"operation": "query", "dataset_ref": REF, "query": QUERY}
    ok, _, _ = agent_gate.grant_approval(confirmation, by=rig.auth.subject)
    assert ok
    value = {"dataset_ref": REF, "query": QUERY, "confirmation_id": confirmation}
    drift = {**value, "query": {**QUERY, "limit": 6}}
    assert rig.call(drift)["gate"]["decision"] == "deny"
    approved = rig.call(value)["gate"]
    assert approved["decision"] == "allow"
    assert "confirmation_id" not in approved
    assert rig.call(value)["gate"]["decision"] == "deny"
    assert len(rig.requests) == 1


def test_real_catalog_gate_approval_binds_limit_cursor_and_is_consumed_once(catalog, tmp_path, monkeypatch):
    for env, path in {"LEAF_AGENT_KILL_FILE": "kill", "LEAF_AGENT_APPROVALS_DIR": "approvals",
                      "LEAF_AGENT_GRANTS_FILE": "grants.json", "LEAF_AGENT_RATE_FILE": "rate.json",
                      "LEAF_AGENT_AUDIT": "audit.jsonl", "LEAF_AGENT_LEDGER": "ledger.jsonl",
                      "LEAF_AGENT_TENANTS_FILE": "tenants.json"}.items():
        monkeypatch.setenv(env, str(tmp_path / path))
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(session_policy, "get_policy", lambda session, tenant: "plan_first")
    calls = []
    def gate(*args, **kwargs):
        calls.append(deepcopy(args))
        return agent_gate.gate(*args, **kwargs)
    catalog.svc.gate = gate
    original = {"limit": 1, "after": "opaque_0"}
    pending = catalog.catalog_call(original)["gate"]
    assert pending["decision"] == "awaiting_approval"
    assert len(calls) == 1 and not catalog.requests
    confirmation = pending["confirmation_id"]
    record = agent_gate.read_pending(confirmation)
    assert record["action"] == "read_integration_data"
    assert record["args"] == {"operation": "action_catalog", **original}
    ok, _, _ = agent_gate.grant_approval(confirmation, by=catalog.auth.subject)
    assert ok
    approved_args = {**original, "confirmation_id": confirmation}
    assert catalog.catalog_call({**approved_args, "limit": 2})["gate"]["decision"] == "deny"
    assert catalog.catalog_call({**approved_args, "after": "opaque_1"})["gate"]["decision"] == "deny"
    assert not catalog.requests
    approved = catalog.catalog_call(approved_args)
    assert approved["gate"]["decision"] == "allow" and approved["result"] == catalog.response
    assert catalog.catalog_call(approved_args)["gate"]["decision"] == "deny"
    assert len(calls) == 5 and len(catalog.requests) == 1
    assert catalog.requests[0].method == "GET" and catalog.requests[0].content == b""


@pytest.mark.parametrize("phase", [2, 3, 4, 5, 6])
@pytest.mark.parametrize("mutation", ["revoked", "expired", "identity", "operations"])
def test_catalog_rechecks_authority_and_expiry_at_each_boundary(catalog, phase, mutation):
    original = catalog.svc.resolve_write
    resolutions = 0
    def resolve(*headers):
        nonlocal resolutions
        resolutions += 1
        if resolutions == phase:
            if mutation == "revoked":
                raise service.ReadFailure("denied")
            if mutation == "expired":
                catalog.svc.clock = lambda: 1100
            elif mutation == "identity":
                catalog.current = replace(catalog.current, authority=replace(catalog.auth, subject="auth0|other"))
            elif mutation == "operations":
                catalog.current = replace(catalog.current, authority=replace(catalog.auth, operations=LEGACY_OPERATIONS))
        return original(*headers)
    catalog.svc.resolve_write = resolve
    decrypts = []
    decrypt = catalog.svc.decrypt
    def tracked(*args):
        decrypts.append(True)
        return decrypt(*args)
    catalog.svc.decrypt = tracked
    assert catalog.catalog_call()["error"]["code"] == "denied"
    assert len(catalog.gates) == (0 if phase <= 3 else 1)
    assert len(catalog.requests) == (0 if phase <= 4 else 1)
    if phase == 2:
        assert not decrypts


def test_real_gate_early_denial_projects_nullable_metadata(rig, tmp_path, monkeypatch):
    kill = tmp_path / "kill"
    kill.write_text("enabled", encoding="utf-8")
    monkeypatch.setenv("LEAF_AGENT_KILL_FILE", str(kill))
    monkeypatch.setenv("LEAF_AGENT_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    rig.svc.gate = agent_gate.gate
    verdict = rig.call()["gate"]
    assert verdict["decision"] == "deny"
    assert isinstance(verdict["reason"], str)
    assert "policy" not in verdict and "rung" not in verdict
    assert "confirmation_id" not in verdict
    assert not rig.requests


def test_policy_entry_matches_fallback_and_pinned_definitions():
    raw = json.loads((SERVER / "agent_policy.json").read_text())["actions"]["read_integration_data"]
    assert raw == agent_policy._HARDCODED_DEFAULTS["actions"]["read_integration_data"]
    definitions = json.loads((SERVER.parent / "contract/assistant.schema.json").read_text())["$defs"]
    assert raw["args_schema"]["$defs"] == {key: definitions[key] for key in ("IntegrationRefV1", "DatasetQueryV1")}
    assert raw["tenant_tightenable"] is True
    assert raw["audit_extra"] == ["operation"]
    action = agent_policy.load_policy().actions["read_integration_data"]
    assert agent_policy.apply_tenant_overlay(action, {action.name: {"policy": "always-confirm"}}).policy == "always-confirm"


def test_dataset_normalization_retains_after():
    assert service.normalize("datasets", {}) == {"operation": "datasets", "limit": 50}
    assert service.normalize("datasets", {"after": "opaque_1"}) == {
        "operation": "datasets", "limit": 50, "after": "opaque_1"}


@pytest.mark.parametrize("attempt,has_freshness,status", [
    (None, False, "unavailable"),
    ({"outcome": "success", "row_count": 0}, True, "empty"),
    ({"outcome": "success", "row_count": 2}, True, "ready"),
    ({"outcome": "failure", "row_count": 0}, True, "stale"),
    ({"outcome": "failure", "row_count": 0}, False, "unavailable"),
])
def test_discovery_uses_actual_ops_metadata_and_projects_only_permitted_fields(rig, attempt, has_freshness, status):
    freshness = read()["freshness"] if has_freshness else None
    rig.response = {"records": [{"reference": REF, "revision": 7, "title": "Power",
        "fields": [FIELD], "lastAttempt": attempt, "freshness": freshness,
        "provider_metadata": "discarded"}], "nextCursor": "opaque_1"}
    result = rig.svc.execute("datasets", {"limit": 1, "after": "opaque_0"},
        rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert result["result"] == {"datasets": [{"dataset_ref": REF, "title": "Power",
        "fields": [FIELD], "freshness": freshness, "status": status}], "next_cursor": "opaque_1"}
    assert len(rig.requests) == len(rig.gates) == 1
    request = rig.requests[0]
    assert request.method == "GET" and request.content == b""
    assert str(request.url).endswith("/integrations/datasets?limit=1&after=opaque_0")
    assert "lastAttempt" not in json.dumps(result) and "provider_metadata" not in json.dumps(result)


@pytest.mark.parametrize("change", ["over_limit", "wrong_cursor", "bad_count", "bad_outcome",
                                   "invalid_date", "missing_freshness", "secret_extra"])
def test_discovery_rejects_malformed_or_reflected_provider_data(rig, change):
    record = {"reference": REF, "revision": 7, "title": "Power", "fields": [FIELD],
              "lastAttempt": {"outcome": "success", "row_count": 1}, "freshness": read()["freshness"]}
    rig.response = {"records": [record], "nextCursor": None}
    if change == "over_limit":
        rig.response["records"].append(deepcopy(record))
    elif change == "wrong_cursor":
        rig.response["nextCursor"] = "wrong cursor"
    elif change == "bad_count":
        record["lastAttempt"]["row_count"] = True
    elif change == "bad_outcome":
        record["lastAttempt"]["outcome"] = "unknown"
    elif change == "invalid_date":
        record["freshness"]["fetched_at"] = "2026-02-30T12:00:00Z"
    elif change == "missing_freshness":
        del record["freshness"]
    else:
        record["discarded"] = TOKEN.decode()
    result = rig.svc.execute("datasets", {"limit": 1},
        rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert result["error"]["code"] == "invalid_response"
    assert TOKEN.decode() not in json.dumps(result)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'])
def test_strict_json(raw):
    with pytest.raises(ValueError):
        service.strict_json(raw)
