from app import app
from route_flatten import iter_leaf_routes


def test_project_conversation_crud_routes_are_mounted() -> None:
    routes = {(path, method)
              for path, route in iter_leaf_routes(app.routes)
              for method in (getattr(route, "methods", None) or ())}

    assert ("/api/projects/{project_id}/conversations", "POST") in routes
    assert ("/api/projects/{project_id}/conversations", "GET") in routes


import json
from uuid import uuid4

import pytest
import conversations
import demo_actor
import deps
import platform_link


@pytest.mark.parametrize("operation,stage", [
    ("create", "project"), ("create", "binding"), ("list", "project"),
    ("get", "project"), ("recover", "project"), ("recover", "binding")])
@pytest.mark.parametrize("failure", [demo_actor.DemoActorRefused, demo_actor.DemoActorUnavailable])
def test_demo_conversation_actor_exception_transport(monkeypatch, operation, failure, stage):
    tenant = deps.TenantContext("demo-" + str(uuid4()), subject="demo:verified",
                               org_id=str(uuid4()), verified_demo=True)
    monkeypatch.setattr(conversations, "conv_durable_enabled", lambda: True)
    def fail(*a, **kw):
        raise failure()
    def never(*a, **kw):
        pytest.fail("store operation after actor failure")
    monkeypatch.setattr(platform_link, "require_project_access",
                        fail if stage == "project" else lambda *a, **kw: tenant.org_id)
    monkeypatch.setattr(platform_link, "resolve_caller_binding", fail)
    monkeypatch.setattr(conversations, "platform_store", never)
    for name in ("create_conversation", "list_conversations", "get_conversation",
                 "recover_or_create_conversation"):
        monkeypatch.setattr(conversations, name, never)
    if operation == "create":
        response = conversations.api_create_conversation("project", conversations.CreateConversationRequest(), tenant)
    elif operation == "list":
        response = conversations.api_list_conversations("project", tenant)
    elif operation == "get":
        response = conversations.api_get_conversation("project", "conversation", tenant)
    else:
        response = conversations.api_recover_conversation("project", tenant)
    assert response.status_code == (403 if failure is demo_actor.DemoActorRefused else 503)
    body = json.loads(response.body)
    assert body["error"]["retryable"] is (failure is demo_actor.DemoActorUnavailable)
    if failure is demo_actor.DemoActorRefused:
        assert body["error"]["reason_code"] == "demo_actor_refused"


@pytest.mark.parametrize("operation", ["create", "recover"])
def test_demo_conversation_writes_use_leaf_demo_binding(monkeypatch, operation):
    from types import SimpleNamespace
    tenant = deps.TenantContext("demo-" + str(uuid4()), subject="demo:verified",
                               org_id=str(uuid4()), verified_demo=True)
    binding_id = str(uuid4())
    monkeypatch.setattr(conversations, "conv_durable_enabled", lambda: True)
    monkeypatch.setattr(platform_link, "require_project_access", lambda *a, **kw: tenant.org_id)
    monkeypatch.setattr(platform_link, "resolve_caller_binding",
                        lambda caller: SimpleNamespace(binding_id=binding_id))
    monkeypatch.setattr(conversations, "platform_store", lambda: pytest.fail("Auth0 fallback"))
    writes = []
    def write(org, project, actor, *args):
        writes.append((org, project, actor))
        return {"conversation_id": "conversation"}
    monkeypatch.setattr(conversations, "create_conversation", write)
    monkeypatch.setattr(conversations, "recover_or_create_conversation", write)
    if operation == "create":
        response = conversations.api_create_conversation("project", conversations.CreateConversationRequest(), tenant)
        assert response.status_code == 201
    else:
        response = conversations.api_recover_conversation("project", tenant)
        assert response["conversation_id"] == "conversation"
    assert writes == [(tenant.org_id, "project", binding_id)]
