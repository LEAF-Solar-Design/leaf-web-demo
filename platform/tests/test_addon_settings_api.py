"""Database-free add-on settings and trusted billing hop contracts."""
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from leaf_platform import api, tenant_cost_store

ORG = uuid.UUID("7b51687d-2d39-47ac-b598-54f3d1fd9e22")
SUBJECT = "auth0|verified-addon-owner"
PERIOD = "2026-10"


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Fakes exercise the HTTP boundary without opening a database."""
    yield


@pytest.fixture
def route(monkeypatch):
    state = {"known": True, "role": "owner", "account_class": "customer",
             "settings": {}, "writes": [], "connections": [], "builds": [],
             "lookups": []}
    conn = object()
    binding = SimpleNamespace(platform_tenant_id=ORG, binding_id=uuid.uuid4())

    def verify(authorization):
        assert authorization == "Bearer verified"
        return {"sub": SUBJECT}

    def resolve(authority, subject):
        assert (authority, subject) == ("auth0", SUBJECT)
        return binding

    def lookup(org):
        state["lookups"].append(org)
        assert org == ORG
        return SimpleNamespace(account_class=state["account_class"]) if state["known"] else None

    @contextmanager
    def connection():
        state["connections"].append(conn)
        yield conn

    def settings(actual_conn, org):
        assert actual_conn is conn and org == ORG
        return {addon: state["settings"].get(addon, {"mode": "off", "byo_ref": None})
                for addon in tenant_cost_store.ADDONS}

    def write(actual_conn, org_id, addon, mode, *, byo_ref, actor):
        assert actual_conn is conn and org_id == ORG
        assert actor == SUBJECT
        state["writes"].append((addon, mode, byo_ref, actor))
        state["settings"][addon] = {"mode": mode, "byo_ref": byo_ref}

    book = object()
    statement = {"period": PERIOD, "total_usd": "1.250000", "lines": []}

    def build(actual_conn, org, period, prices):
        assert actual_conn is conn and org == ORG and prices is book
        state["builds"].append(period)
        if state.get("build_error"):
            raise ValueError(state["build_error"])
        return statement

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BILLING_SYNC_LIVE", "1")
    monkeypatch.setenv("LEAF_BILLING_SYNC_SECRET", "trusted-hop")
    monkeypatch.delenv("LEAF_BILLING_SYNC_SECRET_ID", raising=False)
    monkeypatch.setattr(api.platform_deps, "_server_auth",
                        lambda: SimpleNamespace(verify_platform_token=verify))
    monkeypatch.setattr(api.store, "resolve_active_identity_binding", resolve)
    monkeypatch.setattr(api.store, "active_identity_role", lambda *args: state["role"])
    monkeypatch.setattr(api.store, "get_org", lookup)
    monkeypatch.setattr(api.db, "connection", connection)
    monkeypatch.setattr(tenant_cost_store, "get_addon_settings", settings)
    monkeypatch.setattr(tenant_cost_store, "set_addon_mode", write)
    monkeypatch.setattr(api, "load_pricebook", lambda: book)
    monkeypatch.setattr(api.billing_statement, "build_statement", build)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, headers={"Authorization": "Bearer verified"}) as client:
        yield client, state


def put(client, body, *, org=ORG, addon="aps_solve"):
    return client.put(f"/api/orgs/{org}/addons/{addon}", json=body)


def feed(client, secret="trusted-hop", period=PERIOD):
    # The hop succeeds without a user token or org header.
    return client.get(f"/api/orgs/{ORG}/billing/statement-feed",
                      params={"period": period},
                      headers={"X-Billing-Sync-Secret": secret} if secret is not None else {})


def test_get_all_addons_default_off(route):
    client, _ = route
    response = client.get(f"/api/orgs/{ORG}/addons")
    assert response.status_code == 200
    assert response.json() == {"schema": "leaf.addons.v1", "reader_version": 1, "addons": {
        addon: {"mode": "off", "byo_ref": None, "min_reader_version": 1}
        for addon in tenant_cost_store.ADDONS}}


def test_catalog_wire(route):
    client, state = route
    setting = {"mode": "byo", "byo_ref": "credentials/own-aps"}
    response = put(client, setting)
    assert response.status_code == 200
    assert response.json() == setting
    response = client.get(f"/api/orgs/{ORG}/addons")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["schema"] == "leaf.addons.v1"
    assert catalog["reader_version"] == 1
    assert set(catalog["addons"]) == set(tenant_cost_store.ADDONS)
    assert all(entry["min_reader_version"] == 1 for entry in catalog["addons"].values())
    assert catalog["addons"]["aps_solve"] == {**setting, "min_reader_version": 1}
    assert {addon: {key: entry[key] for key in ("mode", "byo_ref")}
            for addon, entry in catalog["addons"].items()} == {
        addon: setting if addon == "aps_solve" else {"mode": "off", "byo_ref": None}
        for addon in tenant_cost_store.ADDONS}
    assert state["settings"] == {"aps_solve": setting}
    state["writes"].clear()
    state["connections"].clear()

    for addon in ("future_addon", "__proto__", "constructor", "prototype"):
        response = put(client, {"mode": "off"}, addon=addon)
        assert response.status_code == 404
        assert response.json()["detail"] == "addon not found"
    for body in ({"mode": "future"}, {"mode": "byo"},
                 {"mode": "managed", "byo_ref": "credentials/own-aps"},
                 {"mode": "off", "reader_version": 99},
                 {"mode": "off", "min_reader_version": 99}):
        assert put(client, body).status_code == 422
    state["lookups"].clear()
    other_org = uuid.uuid4()
    for response in (client.get(f"/api/orgs/{other_org}/addons"),
                     put(client, {"mode": "off"}, org=other_org)):
        assert response.status_code == 404
        assert response.json()["detail"] == "org not found"
    assert state["lookups"] == []
    state["known"] = False
    for response in (client.get(f"/api/orgs/{ORG}/addons"), put(client, {"mode": "off"})):
        assert response.status_code == 404
        assert response.json()["detail"] == "org not found"
    state["known"] = True
    state["role"] = "reviewer"
    response = put(client, {"mode": "managed"})
    assert response.status_code == 403
    assert response.json()["detail"] == "platform role does not permit mutation"
    assert state["writes"] == state["connections"] == []
    assert state["settings"] == {"aps_solve": setting}


def test_get_cross_org_is_404(route):
    client, state = route
    response = client.get(f"/api/orgs/{uuid.uuid4()}/addons")
    assert response.status_code == 404
    assert state["lookups"] == state["connections"] == []


def test_get_unknown_org_is_404(route):
    client, state = route
    state["known"] = False
    assert client.get(f"/api/orgs/{ORG}/addons").status_code == 404
    assert state["connections"] == []


@pytest.mark.parametrize("setting", [
    {"mode": "managed", "byo_ref": None},
    {"mode": "byo", "byo_ref": "credentials/own-aps"},
    {"mode": "off", "byo_ref": None},
])
def test_put_round_trip_and_verified_actor(route, setting):
    client, state = route
    response = put(client, setting)
    assert response.status_code == 200
    assert response.json() == setting
    assert client.get(f"/api/orgs/{ORG}/addons").json()["addons"]["aps_solve"] == {
        **setting, "min_reader_version": 1}
    assert state["writes"] == [("aps_solve", setting["mode"], setting["byo_ref"], SUBJECT)]


@pytest.mark.parametrize("body,field", [
    ({"mode": "byo"}, "byo_ref"),
    ({"mode": "byo", "byo_ref": "a secret value"}, "byo_ref"),
    ({"mode": "byo", "byo_ref": "x" * 129}, "byo_ref"),
    ({"mode": "invalid"}, "mode"),
    ({"mode": "managed", "byo_ref": "credential"}, "byo_ref"),
    ({"mode": "byo", "byo_ref": 123}, "byo_ref"),
    ({}, "mode"),
    ({"mode": "off", "actor": "forged"}, "actor"),
])
def test_put_bad_body_names_field(route, body, field):
    client, state = route
    response = put(client, body)
    assert response.status_code == 422
    assert field in response.text
    assert state["writes"] == state["connections"] == []


def test_put_unknown_addon_is_404(route):
    client, state = route
    assert put(client, {"mode": "off"}, addon="unknown").status_code == 404
    assert state["writes"] == state["connections"] == []


def test_put_cross_org_is_404(route):
    client, state = route
    assert put(client, {"mode": "off"}, org=uuid.uuid4()).status_code == 404
    assert state["lookups"] == state["writes"] == state["connections"] == []


def test_put_unknown_org_is_404(route):
    client, state = route
    state["known"] = False
    assert put(client, {"mode": "off"}).status_code == 404
    assert state["writes"] == state["connections"] == []


@pytest.mark.parametrize("role", ["reviewer", "read_only", "member"])
def test_put_reuses_mutating_role_refusal(route, role):
    client, state = route
    state["role"] = role
    response = put(client, {"mode": "managed"})
    assert response.status_code == 403
    assert response.json()["detail"] == "platform role does not permit mutation"
    assert state["writes"] == state["connections"] == []


def test_put_editor_is_allowed(route):
    client, state = route
    state["role"] = "editor"
    assert put(client, {"mode": "off"}).status_code == 200


@pytest.mark.parametrize("live,configured,secret,status", [
    ("0", "trusted-hop", "trusted-hop", 503),
    ("1", "", "trusted-hop", 503),
    ("1", "trusted-hop", "wrong", 403),
    ("1", "trusted-hop", None, 403),
])
def test_feed_fail_closed(route, monkeypatch, live, configured, secret, status):
    client, state = route
    monkeypatch.setenv("LEAF_BILLING_SYNC_LIVE", live)
    monkeypatch.setenv("LEAF_BILLING_SYNC_SECRET", configured)
    assert feed(client, secret).status_code == status
    assert state["lookups"] == state["builds"] == state["connections"] == []


def test_feed_unknown_org_is_404(route):
    client, state = route
    state["known"] = False
    assert feed(client).status_code == 404
    assert state["builds"] == state["connections"] == []


def test_feed_acceptance_org_is_409(route):
    client, state = route
    state["account_class"] = "production_acceptance"
    assert feed(client).status_code == 409
    assert state["builds"] == state["connections"] == []


def test_feed_matches_org_statement_without_user_auth(route):
    client, state = route
    expected = client.get(f"/api/orgs/{ORG}/billing/statement", params={"period": PERIOD})
    client.headers.pop("authorization")
    response = feed(client)
    assert response.status_code == expected.status_code == 200
    assert response.json() == expected.json()
    assert response.json()["statement"]["total_usd"] == "1.250000"
    assert state["builds"] == [PERIOD, PERIOD]
    assert state["writes"] == []


def test_feed_bad_period_matches_statement(route):
    client, state = route
    response = feed(client, period="2026-13")
    assert response.status_code == 400
    assert response.json()["detail"] == api.billing_statement.PERIOD_MESSAGE
    assert state["connections"] == []


def test_feed_full_window_matches_statement(route):
    client, state = route
    state["build_error"] = api.billing_statement.EVENT_LIMIT_MESSAGE
    response = feed(client)
    assert response.status_code == 409
    assert response.json()["detail"] == api.billing_statement.EVENT_LIMIT_MESSAGE
