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
