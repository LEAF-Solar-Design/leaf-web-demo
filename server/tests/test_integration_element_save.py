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
    return {"schema_version": 1, "id": "sales-view", "title": "Sales",
            "dataset_ref": deepcopy(REF), "query": deepcopy(QUERY),
            "kind": "dataset." + kind,
            "props": ({"columns": ["amount"], "row_limit": 5} if kind == "table" else
                      {"x_field": "date", "y_field": "amount"})}


@pytest.fixture
def save(rig):
    # Only this trusted test injection replaces production write resolution.
    rig.svc.resolve_write = rig.svc.resolve
    rig.response = {"element": recipe(), "revision": 1, "discarded": "metadata"}
    rig.save = lambda value=None: rig.svc.execute("elements", value if value is not None else
        {"recipe": recipe()}, rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    return rig


@pytest.mark.parametrize("kind", ["table", "line"])
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
def test_nonallow_has_one_gate_and_no_transport(save, decision):
    save.decision = decision
    assert set(save.save()) == {"gate"}
    assert len(save.gates) == 1 and not save.requests


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
    *[{"recipe": recipe(), "expectedRevision": revision}
      for revision in (True, 0, -1, 1.5, "1", 9007199254740992, None)],
    {"recipe": recipe(), "confirmation_id": "A" * 16},
    {"recipe": recipe(), "confirmation_id": "a" * 16 + "\n"},
])
def test_invalid_save_precedes_authority_and_gate(save, value):
    assert save.save(value)["error"]["code"] == "invalid_input"
    assert not save.gates and not save.requests and save.resolutions == 0


@pytest.mark.parametrize("change", ["recipe", "revision", "boolean", "missing", "reflection"])
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
def test_definite_rejections_and_ambiguous_http(save, status, code):
    calls = []
    async def http(request):
        calls.append(request)
        return httpx.Response(status, content=TOKEN)
    save.svc.transport = httpx.MockTransport(http)
    assert save.save()["error"]["code"] == code
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["lost", "timeout", "oversized", "malformed"])
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


def test_production_write_resolution_and_reader_denial(host):
    host.write_checks.clear()
    response = host.client.post("/internal/integrations/elements", headers=host.headers,
                                json={"recipe": recipe()})
    assert response.status_code == 200
    assert host.write_checks and all(host.write_checks)
    assert len(host.ops) == len(host.gates) == 1
    assert response.headers["cache-control"] == "no-store, private"
    host.ops.clear()
    host.gates.clear()
    host.write_access = False
    response = host.client.post("/internal/integrations/elements", headers=host.headers,
                                json={"recipe": recipe(), "confirmation_id": "a" * 16})
    assert response.status_code == 403
    assert not host.ops and not host.gates


@pytest.mark.parametrize("mutation", ["binding", "turn", "origin", "grant", "expiry"])
def test_production_save_authority_denies_before_gate(host, monkeypatch, mutation):
    if mutation == "binding":
        host.binding = None
    elif mutation == "turn":
        host.turn = "turn-2"
    elif mutation == "origin":
        monkeypatch.setenv("LEAF_INTEGRATION_SERVICE_ORIGIN", "https://other.example.com")
    elif mutation == "grant":
        host.custody = None
    else:
        host.custody = replace(host.custody, binding=replace(host.custody.binding, issued_at=800,
            verified_jwt_exp=920, turn_deadline=920, expires_at=900))
    result = host.client.post("/internal/integrations/elements", headers=host.headers,
                              json={"recipe": recipe()})
    assert result.status_code in {403, 409, 503}
    assert not host.gates and not host.ops


@pytest.mark.parametrize("mutation", ["write", "binding", "turn", "origin"])
def test_accepted_write_then_revocation_is_unknown(host, monkeypatch, mutation):
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
                                json={"recipe": recipe()})
    assert response.status_code == 502 and response.json()["error"]["code"] == "outcome_unknown"
    assert set(response.json()) == {"error"}
    assert len(host.ops) == len(host.gates) == 1


def test_real_plan_first_approval_exact_recipe_revision_and_one_use(save, tmp_path, monkeypatch):
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
    value = {"recipe": recipe(), "expectedRevision": 7}
    pending = save.save(value)["gate"]
    assert pending["decision"] == "awaiting_approval" and not save.requests
    confirmation = pending["confirmation_id"]
    assert agent_gate.read_pending(confirmation)["args"] == value
    assert agent_gate.grant_approval(confirmation, by=save.auth.subject)[0]
    replay = {**value, "confirmation_id": confirmation}
    assert save.save({**replay, "expectedRevision": 8})["gate"]["decision"] == "deny"
    assert save.save({**replay, "recipe": {**recipe(), "title": "Drift"}})["gate"]["decision"] == "deny"
    assert save.save(replay)["gate"]["decision"] == "allow"
    assert save.save(replay)["gate"]["decision"] == "deny"
    assert len(save.requests) == 1


def test_save_policy_sources_and_closed_pinned_definitions():
    raw = json.loads((SERVER / "agent_policy.json").read_text())["actions"]["save_integration_element"]
    assert raw == agent_policy._HARDCODED_DEFAULTS["actions"]["save_integration_element"]
    definitions = json.loads((SERVER.parent / "contract/assistant.schema.json").read_text())["$defs"]
    assert raw["args_schema"]["$defs"] == {key: definitions[key] for key in
        ("IntegrationRefV1", "DatasetQueryV1", "DatasetElementV1")}
    assert raw["rung"] == 3 and raw["timeout_s"] == 10
    assert raw["audit_extra"] == ["expectedRevision"]
    assert raw["dispatch"] == {"kind": "app_api", "routes": ["POST /internal/integrations/elements"]}
    action = agent_policy.load_policy().actions["save_integration_element"]
    assert agent_policy.apply_tenant_overlay(action, {action.name: {"policy": "always-confirm"}}).policy == "always-confirm"
