"""Every mounted operator handler must refuse callers before execution."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import jobs  # noqa: E402
import operator_deps  # noqa: E402
import operator_principals  # noqa: E402
import platform_link  # noqa: E402
import tenancy  # noqa: E402
from route_flatten import iter_leaf_routes  # noqa: E402
from test_auth import _ENV, bearer, mint  # noqa: E402

TENANT = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"
REQUIRED_ROUTERS = {
    "operator_credential", "operator_external", "operator_identity_bindings",
    "operator_overlay", "operator_release", "operator_runbooks",
    "operator_sessions",
}


def _uses_operator(route):
    stack = [getattr(route, "dependant", None)]
    while stack:
        dependency = stack.pop()
        if getattr(dependency, "call", None) is operator_deps.require_operator:
            return True
        stack.extend(getattr(dependency, "dependencies", ()) or ())
    return False


def _runtime_routes():
    # Load a separate composition root: do not reload or change the cached
    # `app` module used by other tests. Both independent mount flags are needed.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEAF_OPERATOR_ENABLED", "1")
        patch.setenv("LEAF_OPERATOR_IDENTITY_BINDING_ENABLED", "1")
        patch.setenv("LEAF_RUNTIME_ENV", "test")
        patch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
        patch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
        patch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
        patch.setenv("LEAF_GUEST_PURGE_DISABLED", "1")
        patch.setenv("LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED", "1")
        # Route discovery must not validate a developer's real database.
        patch.setattr(platform_link, "validate_postgres_startup", lambda: None)
        patch.setattr(jobs, "validate_store_startup", lambda: None)
        spec = importlib.util.spec_from_file_location(
            "_operator_authz_runtime", SERVER_DIR / "app.py")
        module = importlib.util.module_from_spec(spec)
        patch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        return list(iter_leaf_routes(module.app.routes))


RUNTIME_ROUTES = _runtime_routes()
OPERATOR_ROUTES = [
    (path, route) for path, route in RUNTIME_ROUTES
    if getattr(getattr(route, "endpoint", None), "__module__", "").startswith(
        "routers.operator") and _uses_operator(route)
]
ROUTE_CASES = [
    pytest.param(method, path, route, id=f"{method} {path}")
    for path, route in OPERATOR_ROUTES
    for method in sorted(route.methods)
]


def _request_path(path):
    values = {
        "tenant_id": TENANT, "session_id": "authz-session", "verb": "pause",
        "handle": "authz-handle", "target": "staging", "source_sha": "a" * 40,
    }
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}",
                  lambda match: values.get(match[1], "authz-placeholder"), path)


def _body(method, path, route):
    if method not in {"POST", "PUT", "PATCH"}:
        return None
    module = route.endpoint.__module__.rsplit(".", 1)[-1]
    bodies = {
        "operator_credential": {"credential_handle": "authz-handle"},
        "operator_external": {
            "destination": "staging", "token_handle": "authz-handle",
            "adapter": "authz-adapter",
        },
        "operator_overlay": {"tenant_id": TENANT, "overlay": {}},
        "operator_release": {"source_sha": "a" * 40, "target": "staging"},
        "operator_runbooks": {"tenant_id": TENANT},
    }
    body = dict(bodies.get(module, {}))
    if path.endswith("/execute"):
        body["authority_id"] = "authz-authority"
    if module == "operator_sessions" and path.endswith("/messages"):
        body = {"text": "authz message"}
    if module == "operator_worker":
        body = ({"commands": ["true"]} if path.endswith("/dispatch") else
                {"worker_id": "authz-worker", "run_id": "authz-run"})
    if module == "operator_identity_bindings":
        # This body is parsed by a dependency BEFORE require_operator.
        body = {"environment": "staging", "bindings": [
            {"tenant_id": TENANT, "subject": "auth0|synthetic-a",
             "role": "read_only"},
            {"tenant_id": "c86bebca-4337-4efe-b4ba-01f4a99d9aac",
             "subject": "auth0|synthetic-b", "role": "read_only"},
        ]}
    return body


@pytest.fixture()
def live_auth(monkeypatch):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.setattr(deps, "resolve_active_platform_tenant_authority",
                        lambda subject: (TENANT, "hosted_pro"))
    monkeypatch.setattr(deps, "admin_subjects", lambda: frozenset())
    monkeypatch.setattr(tenancy, "get_store", lambda: SimpleNamespace(
        resolve_workspace=lambda tenant_id: None))


def test_operator_route_inventory_covers_required_routers():
    assert OPERATOR_ROUTES, "operator route discovery must not be empty"
    modules = {route.endpoint.__module__.rsplit(".", 1)[-1]
               for _, route in OPERATOR_ROUTES}
    assert REQUIRED_ROUTERS <= modules


def test_every_operator_path_requires_operator():
    paths = [(path, route) for path, route in RUNTIME_ROUTES
             if path.startswith("/api/operator/")]
    assert paths, "operator path inventory must not be empty"
    assert not [path for path, route in paths if not _uses_operator(route)]


@pytest.mark.parametrize("method,path,route", ROUTE_CASES)
@pytest.mark.parametrize("mode,status,detail", [
    ("anonymous", 401, "missing bearer token (Authorization header)"),
    ("non-operator", 404, "operator_not_found"),
    ("store-down", 503, "operator_store_unavailable"),
    ("inactive", 404, "operator_not_found"),
])
def test_operator_admission_precedes_handler(
    live_auth, monkeypatch, method, path, route, mode, status, detail,
):
    body = _body(method, path, route)
    lookups = []

    def resolve(subject):
        lookups.append(subject)
        if mode == "anonymous":
            pytest.fail("anonymous caller reached the operator principal store")
        if mode == "store-down":
            raise RuntimeError("private store failure")
        if mode == "inactive":
            return operator_principals.OperatorPrincipal(
                subject=subject, role="operator", role_revision=1,
                status="suspended", profiles=("default",), environment="staging")
        return None

    def forbidden(*args, **kwargs):
        pytest.fail(f"{method} {path}: refused caller reached the handler")

    monkeypatch.setattr(operator_principals, "resolve_principal", resolve)
    monkeypatch.setattr(route, "endpoint", forbidden)
    monkeypatch.setattr(route.dependant, "call", forbidden)
    # Serve the discovered runtime leaf directly. This keeps the exact captured
    # dependency graph and handler sentinel, without unrelated app startup or
    # _IncludedRouter cloning that could retain a different handler callable.
    assert route.path == path, "a new mount prefix needs a matching request harness"
    app = FastAPI()
    app.router.routes.append(route)
    headers = {"Idempotency-Key": "authz-negative-request"}
    if mode != "anonymous":
        headers["Authorization"] = bearer(mint(tenant_id=TENANT))
    with TestClient(app) as client:
        response = client.request(method, _request_path(path), json=body,
                                  headers=headers)
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert lookups == ([] if mode == "anonymous" else ["auth0|tester"])
