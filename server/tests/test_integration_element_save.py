"""Native recipe writes: current write authority, one gate, no implicit retries."""
import platform as _stdlib_platform
_stdlib_platform.python_implementation()

import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from test_integration_tool_service import rig, REF, QUERY, TOKEN, SERVER
from test_integration_tool_routes import host
from leaf_platform.integration_turn_grant_store import GrantStoreError
import integration_tool_service as service
import agent_gate
import agent_policy
import session_policy


def recipe(kind="table"):
    if kind == "action":
        return {"schema_version": 1, "id": "sales-view", "title": "Create sale",
                "kind": "integration.action", "connection_id": "sales",
                "connector_ref": deepcopy(REF), "operation_ref": {**REF, "id": "create-sale"},
                "form": {"submit_label": "Create", "fields": [
                    {"id": "amount", "label": "Amount", "kind": "number",
                     "required": True, "nullable": False, "integer": False}]}}
    return {"schema_version": 1, "id": "sales-view", "title": "Sales",
            "dataset_ref": deepcopy(REF), "query": deepcopy(QUERY),
            "kind": "dataset." + kind,
            "props": ({"columns": ["amount"], "row_limit": 5} if kind == "table" else
                      {"x_field": "date", "y_field": "amount"})}


@pytest.fixture
def save(rig, request):
    # Only this trusted test injection replaces production write resolution.
    rig.svc.resolve_write = rig.svc.resolve
    rig.recipe = recipe(getattr(request, "param", "table"))
    rig.response = {"element": deepcopy(rig.recipe), "revision": 1, "discarded": "metadata"}
    rig.save = lambda value=None: rig.svc.execute("elements", value if value is not None else
        {"recipe": deepcopy(rig.recipe)}, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    return rig


@pytest.mark.parametrize("kind", ["table", "line", "action"])
@pytest.mark.parametrize("revision", [None, 7])
def test_exact_create_and_cas_wire(save, kind, revision):
    value = {"recipe": recipe(kind), "confirmation_id": "a" * 16}
    if revision is not None:
        value["expectedRevision"] = revision
    save.response["element"] = recipe(kind)
    result = save.save(value)
    assert result["result"] == {"element": recipe(kind), "revision": 1}
    assert len(save.requests) == len(save.gates) == 1
    args, _ = save.gates[0]
    assert args[:3] == (save.auth.tenant_id, save.auth.session_id, save.auth.turn_id)
    assert args[3:5] == ("save_integration_element", value)
    request = save.requests[0]
    assert request.method == ("POST" if revision is None else "PUT")
    assert str(request.url) == (save.auth.origin + "/api/v1/projects/" + save.auth.project_id
        + "/integrations/elements" + ("" if revision is None else "/sales-view"))
    assert json.loads(request.content) == recipe(kind)
    assert request.headers.get("if-match") == (None if revision is None else "7")
    assert request.headers["authorization"] == "Bearer " + TOKEN.decode()
    assert "confirmation_id" not in request.content.decode()
    assert "discarded" not in result["result"]


@pytest.mark.parametrize("decision", ["deny", "awaiting_approval"])
@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_nonallow_has_one_gate_and_no_transport(save, decision):
    save.decision = decision
    assert set(save.save()) == {"gate"}
    assert len(save.gates) == 1 and not save.requests


@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_approval_cannot_override_revocation_before_transport(save):
    gate = save.svc.gate
    def revoke(*args, **kwargs):
        verdict = gate(*args, **kwargs)
        save.current = replace(save.current, tier="revoked")
        return verdict
    save.svc.gate = revoke
    assert save.save()["error"]["code"] == "denied"
    assert len(save.gates) == 1 and not save.requests


@pytest.mark.parametrize("value", [
    {}, {"recipe": recipe(), "url": "https://foreign"},
    {"recipe": {**recipe(), "tenant_id": "foreign"}},
    {"recipe": {**recipe(), "id": "../foreign"}},
    {"recipe": {**recipe(), "kind": "code"}},
    {"recipe": {**recipe("action"), "kind": "integration.run"}},
    *[{"recipe": {**recipe("action"), key: "model-supplied"}}
      for key in ("authority", "grant", "token", "arguments", "url", "run_id")],
    *[{"recipe": {**recipe("action"), key: {**REF, "id": "../foreign"}}}
      for key in ("connector_ref", "operation_ref")],
    {"recipe": {**recipe("action"), "operation_ref": {**REF, "version": 0}}},
    {"recipe": {**recipe("action"), "connector_ref": {**REF, "digest": "invalid"}}},
    {"recipe": {**recipe("action"), "form": {**recipe("action")["form"], "arguments": {"amount": 1}}}},
    *[{"recipe": recipe(), "expectedRevision": revision}
      for revision in (True, 0, -1, 1.5, "1", 9007199254740992, None)],
    {"recipe": recipe(), "confirmation_id": "A" * 16},
    {"recipe": recipe(), "confirmation_id": "a" * 16 + "\n"},
])
def test_invalid_save_precedes_authority_and_gate(save, value):
    assert save.save(value)["error"]["code"] == "invalid_input"
    assert not save.gates and not save.requests and save.resolutions == 0


@pytest.mark.parametrize("change", ["recipe", "revision", "boolean", "missing", "reflection"])
@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_invalid_success_is_unknown_and_never_retried(save, change):
    if change == "recipe":
        save.response["element"]["title"] = "Different"
    elif change == "revision":
        save.response["revision"] = 9007199254740992
    elif change == "boolean":
        save.response["revision"] = True
    elif change == "missing":
        del save.response["element"]
    else:
        save.response["discarded"] = TOKEN.decode()
    result = save.save()
    assert result["error"]["code"] == "outcome_unknown"
    assert set(result) == {"error"} and set(result["error"]) == {"code", "correlationId"}
    assert TOKEN.decode() not in json.dumps(result)
    assert len(save.requests) == 1


@pytest.mark.parametrize("change", ["connector_ref", "operation_ref", "form"])
def test_action_response_must_match_complete_submission(save, change):
    submitted = recipe("action")
    save.response["element"] = deepcopy(submitted)
    if change == "form":
        save.response["element"]["form"]["fields"][0]["required"] = False
    else:
        save.response["element"][change]["version"] = 2
    assert save.save({"recipe": submitted})["error"]["code"] == "outcome_unknown"
    assert len(save.requests) == 1


def test_returned_filter_boolean_is_not_the_submitted_number(save):
    submitted = recipe()
    submitted["query"]["filters"] = [{"field": "amount", "op": "eq", "value": 1}]
    save.response["element"] = deepcopy(submitted)
    save.response["element"]["query"]["filters"][0]["value"] = True
    assert save.save({"recipe": submitted})["error"]["code"] == "outcome_unknown"
    assert len(save.requests) == 1


@pytest.mark.parametrize("status,code", [(400, "invalid_input"), (401, "unauthenticated"),
    (403, "denied"), (404, "not_found"), (409, "conflict"), (500, "outcome_unknown"),
    (302, "outcome_unknown")])
@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_definite_rejections_and_ambiguous_http(save, status, code):
    calls = []
    async def http(request):
        calls.append(request)
        return httpx.Response(status, content=TOKEN)
    save.svc.transport = httpx.MockTransport(http)
    assert save.save()["error"]["code"] == code
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["lost", "timeout", "oversized", "malformed"])
@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_transport_ambiguity(save, mode):
    calls = []
    async def http(request):
        calls.append(request)
        if mode == "lost":
            raise httpx.ReadError(TOKEN.decode())
        if mode == "timeout":
            raise httpx.ReadTimeout(TOKEN.decode())
        return httpx.Response(200, content=(b"x" * (service.RESPONSE_CAP + 1)
            if mode == "oversized" else b'{"revision":1,"revision":2}'),
            headers={"content-type": "application/json"})
    save.svc.transport = httpx.MockTransport(http)
    assert save.save()["error"]["code"] == "outcome_unknown"
    assert len(calls) == 1


def test_slow_save_body_cancelled_and_closed(save, monkeypatch):
    state = {"closed": False, "cancelled": False}
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b"{"
                await asyncio.sleep(10)
            finally:
                state["cancelled"] = True
        async def aclose(self):
            state["closed"] = True
    async def http(request):
        save.requests.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Slow())
    original = service.bounded_http
    async def short(method, url, token, body, deadline, transport, **kwargs):
        return await original(method, url, token, body, 0.01, transport, **kwargs)
    monkeypatch.setattr(service, "bounded_http", short)
    save.svc.transport = httpx.MockTransport(http)
    assert save.save()["error"]["code"] == "outcome_unknown"
    assert len(save.requests) == 1 and all(state.values())


@pytest.mark.parametrize("save", ["table", "action"], indirect=True)
def test_missing_expired_or_wrong_operation_before_gate(save):
    original = save.custody
    for custody in (None, replace(original, binding=replace(original.binding, issued_at=800,
            verified_jwt_exp=920, turn_deadline=920, expires_at=900))):
        save.custody = custody
        assert "error" in save.save()
    save.custody = original
    # Grants must explicitly contain the S10 fixed operation.
    with pytest.raises(GrantStoreError):
        replace(save.auth, operations=("dataset_query",))
    assert not save.gates and not save.requests


@pytest.mark.parametrize("kind", ["table", "action"])
def test_production_write_resolution_and_reader_denial(host, kind):
    host.write_checks.clear()
    response = host.client.post("/internal/integrations/elements", headers=host.headers,
                                json={"recipe": recipe(kind)})
    assert response.status_code == 200
    assert host.write_checks and all(host.write_checks)
    assert len(host.ops) == len(host.gates) == 1
    assert response.headers["cache-control"] == "no-store, private"
    host.ops.clear()
    host.gates.clear()
    host.write_access = False
    response = host.client.post("/internal/integrations/elements", headers=host.headers,
                                json={"recipe": recipe(kind), "confirmation_id": "a" * 16})
    assert response.status_code == 403
    assert not host.ops and not host.gates


@pytest.mark.parametrize("kind", ["table", "action"])
@pytest.mark.parametrize("mutation", ["binding", "turn", "origin", "grant", "expiry", "tenant", "project", "membership"])
def test_production_save_authority_denies_before_gate(host, monkeypatch, mutation, kind):
    if mutation == "binding":
        host.binding = None
    elif mutation == "turn":
        host.turn = "turn-2"
    elif mutation == "origin":
        monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", "https://other.example.com")
    elif mutation == "grant":
        host.custody = None
    elif mutation == "tenant":
        host.session["org_id"] = "12345678-1234-4234-8234-123456789abc"
    elif mutation == "project":
        host.session["project_id"] = "12345678-1234-4234-8234-123456789abc"
    elif mutation == "membership":
        host.role = False
    else:
        host.custody = replace(host.custody, binding=replace(host.custody.binding, issued_at=800,
            verified_jwt_exp=920, turn_deadline=920, expires_at=900))
    result = host.client.post("/internal/integrations/elements", headers=host.headers,
                              json={"recipe": recipe(kind)})
    assert result.status_code in {403, 409, 503}
    assert not host.gates and not host.ops


@pytest.mark.parametrize("mutation", ["write", "binding", "turn", "origin"])
@pytest.mark.parametrize("kind", ["table", "action"])
def test_accepted_write_then_revocation_is_unknown(host, monkeypatch, mutation, kind):
    def revoke():
        if mutation == "write":
            host.write_access = False
        elif mutation == "binding":
            host.binding = None
        elif mutation == "turn":
            host.turn = "turn-2"
        else:
            monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", "https://other.example.com")
    host.on_http = revoke
    response = host.client.post("/internal/integrations/elements", headers=host.headers,
                                json={"recipe": recipe(kind)})
    assert response.status_code == 502 and response.json()["error"]["code"] == "outcome_unknown"
    assert set(response.json()) == {"error"}
    assert len(host.ops) == len(host.gates) == 1


@pytest.mark.parametrize("kind", ["table", "action"])
def test_real_plan_first_approval_exact_recipe_revision_and_one_use(save, tmp_path, monkeypatch, kind):
    for env, path in {"LEAF_AGENT_KILL_FILE": "kill", "LEAF_AGENT_APPROVALS_DIR": "approvals",
                      "LEAF_AGENT_GRANTS_FILE": "grants.json", "LEAF_AGENT_RATE_FILE": "rate.json",
                      "LEAF_AGENT_AUDIT": "audit.jsonl", "LEAF_AGENT_LEDGER": "ledger.jsonl",
                      "LEAF_AGENT_TENANTS_FILE": "tenants.json"}.items():
        monkeypatch.setenv(env, str(tmp_path / path))
    monkeypatch.setenv("LEAF_AGENT_STORE", "legacy")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(session_policy, "get_policy", lambda session, tenant: "plan_first")
    save.svc.gate = agent_gate.gate
    value = {"recipe": recipe(kind), "expectedRevision": 7}
    save.response["element"] = recipe(kind)
    pending = save.save(value)["gate"]
    assert pending["decision"] == "awaiting_approval" and not save.requests
    confirmation = pending["confirmation_id"]
    assert agent_gate.read_pending(confirmation)["args"] == value
    assert agent_gate.grant_approval(confirmation, by=save.auth.subject)[0]
    replay = {**value, "confirmation_id": confirmation}
    assert save.save({**replay, "expectedRevision": 8})["gate"]["decision"] == "deny"
    assert save.save({**replay, "recipe": {**recipe(kind), "title": "Drift"}})["gate"]["decision"] == "deny"
    if kind == "action":
        for key in ("connector_ref", "operation_ref", "form"):
            changed = recipe(kind)
            if key == "form":
                changed[key]["fields"][0]["required"] = False
            else:
                changed[key]["version"] = 2
            assert save.save({**replay, "recipe": changed})["gate"]["decision"] == "deny"
    assert not save.requests
    approved = save.save(replay)["gate"]
    assert approved["decision"] == "allow"
    assert "confirmation_id" not in approved
    assert save.save(replay)["gate"]["decision"] == "deny"
    assert len(save.requests) == 1


def test_save_policy_sources_and_closed_pinned_definitions():
    raw = json.loads((SERVER / "agent_policy.json").read_text())["actions"]["save_integration_element"]
    assert raw == agent_policy._HARDCODED_DEFAULTS["actions"]["save_integration_element"]
    definitions = json.loads((SERVER.parent / "contract/assistant.schema.json").read_text())["$defs"]
    schema = raw["args_schema"]
    assert schema["properties"]["recipe"] == {"$ref": "#/$defs/IntegrationElementV1"}
    reachable = set()
    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                key = value["$ref"].removeprefix("#/$defs/")
                if key not in reachable:
                    reachable.add(key)
                    visit(definitions[key])
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
    visit(schema["properties"]["recipe"])
    assert schema["$defs"] == {key: definitions[key] for key in reachable}
    assert raw["rung"] == 3 and raw["timeout_s"] == 10
    assert raw["audit_extra"] == ["expectedRevision"]
    assert raw["dispatch"] == {"kind": "app_api", "routes": ["POST /internal/integrations/elements"]}
    action = agent_policy.load_policy().actions["save_integration_element"]
    assert agent_policy.apply_tenant_overlay(action, {action.name: {"policy": "always-confirm"}}).policy == "always-confirm"
