"""POST /api/orgs/{org_id}/billing/tier-sync — flag-gated stored-tier feed.

Proves the census item-5 billing skeleton end to end against a real DB:
dark by default (flag), fail-closed configuration (secret), constant-time
hop auth, single-derivation-path tier writes, offboarding safety, and
idempotency. Mapping SEMANTICS are covered DB-free in
server/tests/test_billing_tiers.py — here we prove the wire + the write.
"""
import uuid

import pytest

from leaf_platform import store
from leaf_platform.billing import TIER_SYNC_FLAG_ENV, TIER_SYNC_SECRET_ENV
from leaf_platform.db import cursor

SECRET = "test-billing-sync-secret"


@pytest.fixture
def live_sync(monkeypatch):
    monkeypatch.setenv(TIER_SYNC_FLAG_ENV, "1")
    monkeypatch.setenv(TIER_SYNC_SECRET_ENV, SECRET)


def _sync(client, org_id, body, secret=SECRET, header=True):
    headers = {"X-Billing-Sync-Secret": secret} if header else {}
    return client.post(f"/api/orgs/{org_id}/billing/tier-sync", json=body, headers=headers)


# --------------------------------------------------------------------------- #
# activation gates (fail closed)
# --------------------------------------------------------------------------- #
def test_flag_off_is_503(client, make_org, monkeypatch):
    monkeypatch.delenv(TIER_SYNC_FLAG_ENV, raising=False)
    monkeypatch.setenv(TIER_SYNC_SECRET_ENV, SECRET)
    org = make_org()
    r = _sync(client, org.org_id, {"plan": "pro"})
    assert r.status_code == 503
    assert store.get_org(org.org_id).tier == "hosted_starter"  # untouched


def test_flag_on_but_no_secret_is_503(client, make_org, monkeypatch):
    monkeypatch.setenv(TIER_SYNC_FLAG_ENV, "1")
    monkeypatch.delenv(TIER_SYNC_SECRET_ENV, raising=False)
    org = make_org()
    r = _sync(client, org.org_id, {"plan": "pro"})
    assert r.status_code == 503


def test_missing_header_is_403(client, make_org, live_sync):
    org = make_org()
    r = _sync(client, org.org_id, {"plan": "pro"}, header=False)
    assert r.status_code == 403


def test_wrong_secret_is_403(client, make_org, live_sync):
    org = make_org()
    r = _sync(client, org.org_id, {"plan": "pro"}, secret="not-the-secret")
    assert r.status_code == 403
    assert store.get_org(org.org_id).tier == "hosted_starter"


# --------------------------------------------------------------------------- #
# the write
# --------------------------------------------------------------------------- #
def test_upgrade_applies_plan_tier(client, make_org, live_sync):
    org = make_org(tier="hosted_starter")
    r = _sync(client, org.org_id, {"plan": "pro", "subscription_active": True,
                                   "subscription_status": "active",
                                   "stripe_event_id": "evt_test_1"})
    assert r.status_code == 200
    body = r.json()
    assert body["previous_tier"] == "hosted_starter"
    assert body["tier"] == "hosted_pro"
    assert body["applied"] is True
    assert body["stripe_event_id"] == "evt_test_1"
    assert store.get_org(org.org_id).tier == "hosted_pro"


def test_lapse_forces_restricted(client, make_org, live_sync):
    org = make_org(tier="hosted_pro")
    r = _sync(client, org.org_id, {"plan": "pro", "subscription_active": False})
    assert r.status_code == 200
    assert r.json()["tier"] == "restricted"
    assert store.get_org(org.org_id).tier == "restricted"


def test_canceled_status_forces_restricted(client, make_org, live_sync):
    org = make_org(tier="hosted_pro")
    r = _sync(client, org.org_id, {"plan": "pro", "subscription_active": True,
                                   "subscription_status": "canceled"})
    assert r.status_code == 200
    assert store.get_org(org.org_id).tier == "restricted"


def test_unknown_plan_defaults_to_starter(client, make_org, live_sync):
    org = make_org(tier="hosted_pro")
    r = _sync(client, org.org_id, {"plan": "some-future-plan", "subscription_active": True})
    assert r.status_code == 200
    assert r.json()["tier"] == "hosted_starter"


def test_idempotent_repost_applied_false(client, make_org, live_sync):
    org = make_org(tier="hosted_starter")
    first = _sync(client, org.org_id, {"plan": "pro"})
    again = _sync(client, org.org_id, {"plan": "pro"})
    assert first.status_code == again.status_code == 200
    assert first.json()["applied"] is True
    assert again.json()["applied"] is False
    assert again.json()["tier"] == "hosted_pro"


# --------------------------------------------------------------------------- #
# org-state safety
# --------------------------------------------------------------------------- #
def test_unknown_org_is_404(client, live_sync):
    r = _sync(client, uuid.uuid4(), {"plan": "pro"})
    assert r.status_code == 404


def test_non_active_org_is_409_and_untouched(client, make_org, live_sync):
    org = make_org(tier="hosted_pro")
    with cursor() as cur:
        cur.execute("UPDATE orgs SET status = 'offboarding' WHERE org_id = %(o)s",
                    {"o": org.org_id})
    r = _sync(client, org.org_id, {"plan": "pro", "subscription_active": False})
    assert r.status_code == 409
    assert store.get_org(org.org_id).tier == "hosted_pro"  # never resurrected/lapsed


def test_set_org_tier_refuses_non_active_rows(make_org):
    org = make_org(tier="hosted_starter")
    with cursor() as cur:
        cur.execute("UPDATE orgs SET status = 'deleted' WHERE org_id = %(o)s",
                    {"o": org.org_id})
    assert store.set_org_tier(org.org_id, "hosted_pro") is None
    assert store.get_org(org.org_id).tier == "hosted_starter"


# --------------------------------------------------------------------------- #
# POST /api/billing/org-resolve — durable linkage (contract/BILLING.md §6)
# --------------------------------------------------------------------------- #
def _resolve(client, subject, secret=SECRET, header=True, authority="auth0"):
    headers = {"X-Billing-Sync-Secret": secret} if header else {}
    return client.post("/api/billing/org-resolve",
                       json={"external_authority": authority, "external_subject": subject},
                       headers=headers)


def _unique_subject():
    return f"auth0|resolve-test-{uuid.uuid4().hex}"


def test_resolve_flag_off_is_503(client, monkeypatch):
    monkeypatch.delenv(TIER_SYNC_FLAG_ENV, raising=False)
    monkeypatch.setenv(TIER_SYNC_SECRET_ENV, SECRET)
    assert _resolve(client, _unique_subject()).status_code == 503


def test_resolve_no_secret_is_503(client, monkeypatch):
    monkeypatch.setenv(TIER_SYNC_FLAG_ENV, "1")
    monkeypatch.delenv(TIER_SYNC_SECRET_ENV, raising=False)
    assert _resolve(client, _unique_subject()).status_code == 503


def test_resolve_missing_header_is_403(client, live_sync):
    assert _resolve(client, _unique_subject(), header=False).status_code == 403


def test_resolve_wrong_secret_is_403(client, live_sync):
    assert _resolve(client, _unique_subject(), secret="not-the-secret").status_code == 403


def test_resolve_unknown_identity_is_404(client, live_sync):
    r = _resolve(client, _unique_subject())
    assert r.status_code == 404


def test_resolve_returns_bound_org(client, live_sync):
    subject = _unique_subject()
    org = store.create_org_with_identity("Resolve Test Org", "auth0", subject)
    r = _resolve(client, subject)
    assert r.status_code == 200
    assert r.json() == {"org_id": str(org.org_id)}


def test_resolve_authority_mismatch_is_404(client, live_sync):
    subject = _unique_subject()
    store.create_org_with_identity("Authority Test Org", "auth0", subject)
    assert _resolve(client, subject, authority="google").status_code == 404


def test_resolve_non_active_org_is_404(client, live_sync):
    """Offboarding marks the org before revoking bindings; resolving in that
    window must NOT hand out a UUID the caller would durably cache while
    tier-sync forever answers 409 for it."""
    subject = _unique_subject()
    org = store.create_org_with_identity("Offboarding Org", "auth0", subject)
    assert _resolve(client, subject).status_code == 200  # sanity: resolvable while active
    with cursor() as cur:
        cur.execute("UPDATE orgs SET status = 'offboarding' WHERE org_id = %(o)s",
                    {"o": org.org_id})
    assert _resolve(client, subject).status_code == 404


def test_resolve_non_owner_binding_is_404(client, make_org, live_sync):
    """The contract keys the linkage on the org OWNER's identity; an editor/
    reviewer/read_only binding must not resolve (enumeration surface)."""
    org = make_org(name="Role Test Org")
    subject = _unique_subject()
    store.create_identity_binding(org.org_id, "auth0", subject, role="editor")
    assert _resolve(client, subject).status_code == 404


# --------------------------------------------------------------------------- #
# dev bootstrap seam: POST /api/orgs with external_subject binds the identity,
# making the full webhook path (bootstrap -> resolve -> tier-sync) provable
# without Auth0 — the shape leaf_website's e2e harness drives.
# --------------------------------------------------------------------------- #
def test_dev_seam_bootstrap_resolves_and_syncs(client, live_sync):
    subject = _unique_subject()
    created = client.post("/api/orgs", json={"name": "Dev Seam Org",
                                             "external_subject": subject})
    assert created.status_code == 200
    org_id = created.json()["org"]["org_id"]

    resolved = _resolve(client, subject)
    assert resolved.status_code == 200
    assert resolved.json()["org_id"] == org_id

    synced = _sync(client, org_id, {"plan": "pro", "subscription_active": True,
                                    "subscription_status": "active"})
    assert synced.status_code == 200
    assert synced.json()["tier"] == "hosted_pro"
    assert store.get_org(uuid.UUID(org_id)).tier == "hosted_pro"


def test_dev_seam_duplicate_subject_is_409(client, live_sync):
    subject = _unique_subject()
    first = client.post("/api/orgs", json={"name": "Dup A", "external_subject": subject})
    assert first.status_code == 200
    again = client.post("/api/orgs", json={"name": "Dup B", "external_subject": subject})
    assert again.status_code == 409


def test_dev_seam_refused_422_under_live_auth_with_valid_token(client, live_sync, monkeypatch):
    """The 422 refusal fires AFTER successful authentication (unauthenticated
    calls are already 401 at the gate) and leaves no write behind — neither
    the verified subject nor the body-supplied one gains a binding."""
    t1b = pytest.importorskip("test_wave_hardening_1b")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_AUTH0_ISSUER", t1b.ISS)
    monkeypatch.setenv("LEAF_AUTH0_AUDIENCE", t1b.AUD)
    monkeypatch.setenv("LEAF_TENANT_CLAIM_NS", t1b.NS)
    monkeypatch.setenv("LEAF_AUTH0_JWKS_FILE", str(t1b._JWKS_FILE))
    verified_subject = _unique_subject()
    body_subject = _unique_subject()
    r = client.post(
        "/api/orgs",
        json={"name": "Live Seam Refusal", "external_subject": body_subject},
        headers={"Authorization": "Bearer " + t1b._mint(
            "unused", with_org=False, subject=verified_subject)},
    )
    assert r.status_code == 422, r.text
    assert store.resolve_active_identity_binding("auth0", verified_subject) is None
    assert store.resolve_active_identity_binding("auth0", body_subject) is None
