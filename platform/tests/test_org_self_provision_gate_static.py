"""Database-free HTTP checks for invite-only org bootstrap and server tiers."""
from types import SimpleNamespace
import uuid
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from leaf_platform import api
from leaf_platform.models import Org


CLAIM_NS = "https://leaf.test/"


@pytest.fixture
def bootstrap(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_ORG_SELF_PROVISION", raising=False)
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    state = SimpleNamespace(
        claims={"sub": "auth0|bootstrap"}, binding=None,
        org=Org(uuid.uuid4(), "Bootstrap Org"), writes=[], events=[],
    )

    def verified_claims():
        return state.claims if api.platform_deps.auth_live() else None

    def ensure(name, authority, subject, *, tier):
        assert (authority, subject) == ("auth0", "auth0|bootstrap")
        if state.binding is not None:
            return state.org, False
        state.writes.append(("ensure", name, tier))
        state.org = Org(uuid.uuid4(), name, tier=tier)
        return state.org, True

    def create_with_identity(name, authority, subject, *, tier):
        state.writes.append(("identity", name, tier))
        state.org = Org(uuid.uuid4(), name, tier=tier)
        return state.org

    def create(name):
        state.writes.append(("create", name, "hosted_starter"))
        state.org = Org(uuid.uuid4(), name)
        return state.org

    state.resolve = Mock(side_effect=lambda authority, subject: state.binding)
    state.ensure = Mock(side_effect=ensure)
    monkeypatch.setattr(api.store, "resolve_active_identity_binding", state.resolve)
    monkeypatch.setattr(api.store, "ensure_org_with_identity", state.ensure)
    monkeypatch.setattr(api.store, "create_org_with_identity", create_with_identity)
    monkeypatch.setattr(api.store, "create_org", create)
    monkeypatch.setattr(api, "_emit_org_event", lambda *args: state.events.append(args))
    monkeypatch.setattr(api.platform_deps, "_server_auth", lambda: SimpleNamespace(
        claim_ns=lambda: CLAIM_NS,
    ))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_auth_when_live] = verified_claims
    with TestClient(app) as client:
        state.client = client
        yield state


def _assert_invite_only(state):
    response = state.client.post("/api/orgs", json={"name": "Bootstrap Org"})
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "invite_only"}
    assert state.writes == []
    assert state.events == []
    state.ensure.assert_not_called()


def _assert_created(state, method="ensure"):
    response = state.client.post("/api/orgs", json={"name": "Bootstrap Org"})
    assert response.status_code == 200, response.text
    assert response.json() == {"org": state.org.to_dict(), "created": True}
    assert response.json()["org"]["tier"] == "hosted_starter"
    assert state.writes == [(method, "Bootstrap Org", "hosted_starter")]
    assert state.events == [(
        "org.created", str(state.org.org_id), {"tier": "hosted_starter"},
    )]


def test_live_unbound_defaults_to_invite_only(bootstrap):
    _assert_invite_only(bootstrap)
    bootstrap.resolve.assert_called_once_with("auth0", "auth0|bootstrap")


def test_live_unbound_explicit_opt_in(bootstrap, monkeypatch):
    monkeypatch.setenv("LEAF_ORG_SELF_PROVISION", "1")
    _assert_created(bootstrap)


@pytest.mark.parametrize("runtime", ["staging", "development", "test", "local"])
def test_named_nonproduction_runtimes_allow_bootstrap(bootstrap, monkeypatch, runtime):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    _assert_created(bootstrap)


@pytest.mark.parametrize("runtime", ["production", "prod", " Staging", "STAGING", "staging ", ""])
def test_other_runtime_values_fail_closed(bootstrap, monkeypatch, runtime):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", runtime)
    _assert_invite_only(bootstrap)


@pytest.mark.parametrize("value", ["true", "yes", "0", " 1"])
def test_self_provision_flag_requires_exact_one(bootstrap, monkeypatch, value):
    monkeypatch.setenv("LEAF_ORG_SELF_PROVISION", value)
    _assert_invite_only(bootstrap)


def test_active_binding_can_reuse_org_without_opt_in(bootstrap):
    bootstrap.binding = SimpleNamespace(platform_tenant_id=bootstrap.org.org_id)
    bootstrap.org.tier = "hosted_pro"
    response = bootstrap.client.post("/api/orgs", json={"name": "Bootstrap Org"})
    assert response.status_code == 200, response.text
    assert response.json() == {"org": bootstrap.org.to_dict(), "created": False}
    assert bootstrap.writes == []
    assert bootstrap.events == []
    bootstrap.ensure.assert_called_once_with(
        "Bootstrap Org", "auth0", "auth0|bootstrap", tier="hosted_starter",
    )


@pytest.mark.parametrize("live", [True, False], ids=["live", "auth-off"])
@pytest.mark.parametrize("tier", ["hosted_pro", "hosted_starter", "gold"])
def test_client_tier_is_always_refused(bootstrap, monkeypatch, live, tier):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1" if live else "0")
    monkeypatch.setenv("LEAF_ORG_SELF_PROVISION", "1")
    response = bootstrap.client.post("/api/orgs", json={"name": "Bootstrap Org", "tier": tier})
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "tier is server-derived; clients never set it"}
    assert bootstrap.writes == []
    assert bootstrap.events == []
    bootstrap.resolve.assert_not_called()
    bootstrap.ensure.assert_not_called()


def test_unbound_org_claim_handoff_precedes_invite_only(bootstrap):
    bootstrap.claims[CLAIM_NS + "org_id"] = str(uuid.uuid4())
    response = bootstrap.client.post("/api/orgs", json={"name": "Bootstrap Org"})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": (
        "This identity requires authorized workspace provisioning. "
        "Contact your workspace administrator to complete provisioning, "
        "then sign in again."
    )}
    assert bootstrap.writes == []
    assert bootstrap.events == []
    bootstrap.ensure.assert_not_called()


def test_auth_off_bootstrap_stays_open(bootstrap, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    _assert_created(bootstrap, method="create")
    bootstrap.resolve.assert_not_called()


def test_auth_off_identity_seam_uses_server_tier(bootstrap, monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    response = bootstrap.client.post("/api/orgs", json={
        "name": "Bootstrap Org", "external_subject": "dev|subject",
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"org": bootstrap.org.to_dict(), "created": True}
    assert response.json()["org"]["tier"] == "hosted_starter"
    assert bootstrap.writes == [("identity", "Bootstrap Org", "hosted_starter")]
    bootstrap.resolve.assert_not_called()
