"""PostgreSQL proofs for server-owned acceptance classification and accounting."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import uuid

import pytest
from psycopg.errors import CheckViolation, NotNullViolation
from psycopg.types.json import Jsonb

from leaf_platform import api, billing, db, store, unit_economics as economics


@pytest.fixture
def period():
    # This file runs alone and again in the platform suite on the same DB.
    start = datetime(3000, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=uuid.uuid4().int % 10_000_000_000, microseconds=uuid.uuid4().int % 1_000_000)
    return start, start + timedelta(seconds=10)


def _acceptance():
    return store.create_org("Acceptance " + uuid.uuid4().hex, tier="hosted_pro",
                            account_class="production_acceptance")


def _observation(period, org_id=None, **overrides):
    facts = dict(idempotency_key=uuid.uuid4().hex, period_start=period[0],
                 period_end=period[1], kind="revenue", category=uuid.uuid4().hex,
                 amount_usd=Decimal("7"), quantity=None, unit=None,
                 source="acceptance-test", source_ref=None, metadata={})
    if org_id is not None:
        facts["org_id"] = org_id
    facts.update(overrides)
    return facts


def _billing(org_id, **overrides):
    facts = dict(plan="pro", subscription_active=True, subscription_status="active",
                 stripe_subscription_id=uuid.uuid4().hex, stripe_event_id=uuid.uuid4().hex,
                 stripe_event_type="invoice.paid", current_period_start=None,
                 current_period_end=None)
    facts.update(overrides)
    return economics.record_billing_tier_event(org_id, "hosted_pro", **facts)


def _event_count(org_id):
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM billing_subscription_events WHERE org_id = %s",
                    (org_id,))
        return cur.fetchone()["n"]


def _meters(org, ts, amount, *, job_status="succeeded", aps_status="ok"):
    """Write the real meter schemas without changing their operational readers."""
    key = uuid.uuid4().hex
    project = store.create_project(org.org_id, f"Accounting test {key}")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_usage_turns (usage_key, tenant_id, session_id, turn_id, ts, record) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (key, str(org.org_id), key, key, ts, Jsonb({"usd_est": amount})))
        cur.execute(
            "INSERT INTO broker_run_admissions "
            "(event_key, tenant_id, request_fingerprint, state, lease_token, lease_expires_at, aps_live) "
            "VALUES (%s, %s, %s, 'leased', %s, NOW() + INTERVAL '1 minute', false)",
            (key, str(org.org_id), uuid.uuid4().hex * 2, key))
        cur.execute(
            "INSERT INTO broker_usage_ledger "
            "(event_key, ts, tenant_id, engine_op, aps_endpoint, aps_live, engine_seconds, usd_est, status) "
            "VALUES (%s, %s, %s, 'run', 'test', false, %s, %s, %s)",
            (key, ts.timestamp(), str(org.org_id), amount * 10, amount, aps_status))
        cur.execute(
            "INSERT INTO jobs (job_id, project_id, org_id, kind, status, cost_usd, updated_at) "
            "VALUES (%s, %s, %s, 'run', %s, %s, %s)",
            (uuid.uuid4(), project.project_id, org.org_id, job_status, amount, ts))


@pytest.fixture
def accounting(period):
    before = economics.fleet_report(*period)["hosted_accounts"]
    customer = store.create_org("Customer " + uuid.uuid4().hex, tier="hosted_pro")
    acceptance = _acceptance()
    categories = {}
    for org, amount in ((customer, 2), (acceptance, 9)):
        categories[org.org_id] = uuid.uuid4().hex
        for _ in range(1 if org == customer else 2):
            _meters(org, period[0], amount)
            # Historical acceptance billing rows can predate the new writer guard.
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_subscription_events "
                    "(event_key, org_id, stripe_event_type, subscription_status, previous_tier, "
                    "derived_tier, tier_changed, payload_sha256, observed_at) "
                    "VALUES (%s, %s, %s, 'active', 'hosted_starter', 'hosted_pro', true, %s, %s)",
                    (uuid.uuid4().hex * 2, org.org_id,
                     "invoice.paid" if org == customer else "invoice.payment_failed",
                     uuid.uuid4().hex * 2, period[0]))
            for kind in ("shared_fixed", "usage_variable", "revenue"):
                economics.append_observation(**_observation(
                    period, org.org_id, kind=kind, category=categories[org.org_id],
                    amount_usd=Decimal(amount)))
    return customer, acceptance, categories, before


def test_acceptance_migration_reapply_and_constraints(period):
    org = _acceptance()
    observation = economics.append_observation(**_observation(period, org.org_id))
    oid = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute("INSERT INTO orgs (org_id, name) VALUES (%s, 'Default customer')", (oid,))
        sql = (db._PKG_DIR / "migrations/0075_org_acceptance_classification.sql").read_text()
        cur.execute(sql)
        cur.execute(sql)
        cur.execute("SELECT account_class FROM orgs WHERE org_id = %s", (oid,))
        assert cur.fetchone()["account_class"] == "customer"
        cur.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'unit_economics_observations' AND column_name = 'org_id'")
        assert dict(cur.fetchone()) == {"data_type": "uuid", "is_nullable": "YES"}
        cur.execute("SELECT COUNT(*) AS n FROM pg_constraint WHERE "
                    "conrelid = 'unit_economics_observations'::regclass AND contype = 'f'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT org_id, org_account_class FROM unit_economics_observations "
                    "WHERE observation_key = %s", (observation["observation_key"],))
        assert dict(cur.fetchone()) == {
            "org_id": org.org_id, "org_account_class": "production_acceptance"}
        cur.execute("SELECT COUNT(*) AS n FROM pg_constraint WHERE "
                    "conrelid = 'unit_economics_observations'::regclass "
                    "AND conname = 'unit_economics_observations_org_class_check'")
        assert cur.fetchone()["n"] == 1
    for value, error in ((None, NotNullViolation), ("synthetic", CheckViolation)):
        with pytest.raises(error):
            with db.cursor() as cur:
                cur.execute("INSERT INTO orgs (org_id, name, account_class) VALUES (%s, 'Invalid', %s)",
                            (uuid.uuid4(), value))
    for org_id, org_class in ((org.org_id, None), (None, "production_acceptance")):
        with pytest.raises(CheckViolation) as exc:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO unit_economics_observations "
                    "(observation_key, period_start, period_end, kind, category, amount_usd, "
                    "source, payload_sha256, org_id, org_account_class) "
                    "VALUES (%s, %s, %s, 'revenue', 'invalid-class', 7, 'acceptance-test', %s, %s, %s)",
                    (uuid.uuid4().hex * 2, *period, uuid.uuid4().hex * 2, org_id, org_class))
        assert exc.value.diag.constraint_name == "unit_economics_observations_org_class_check"


def test_acceptance_classification_is_server_owned(client, monkeypatch):
    org = _acceptance()
    assert org.account_class == "production_acceptance"
    assert store.get_org(org.org_id).account_class == "production_acceptance"
    assert store.create_org("Ordinary").account_class == "customer"
    assert store.create_org_with_identity("Identity", "auth0", uuid.uuid4().hex).account_class == "customer"
    subject = uuid.uuid4().hex
    identity, created = store.ensure_org_with_identity("Ensure", "auth0", subject)
    assert created and identity.account_class == "customer"
    assert store.ensure_org_with_identity("Ensure", "auth0", subject) == (identity, False)
    with pytest.raises(ValueError, match="invalid organization account class"):
        store.create_org("Invalid", account_class="other")
    try:
        for live in ("0", "1"):
            monkeypatch.setenv("LEAF_AUTH_LIVE", live)
            auth = {"sub": uuid.uuid4().hex} if live == "1" else None
            client.app.dependency_overrides[api.require_auth_when_live] = lambda: auth
            name = "Injection " + uuid.uuid4().hex
            response = client.post("/api/orgs", json={"name": name, "account_class": "production_acceptance"})
            assert response.status_code == 422
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM orgs WHERE name = %s", (name,))
                assert cur.fetchone()["n"] == 0
    finally:
        client.app.dependency_overrides.clear()


def test_acceptance_tier_writers_refuse():
    org = _acceptance()
    assert store.set_org_tier(org.org_id, "hosted_starter") is None
    event = uuid.uuid4().hex
    # Even a replay of an existing historical event must hit the class guard.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO billing_subscription_events "
            "(event_key, org_id, previous_tier, derived_tier, tier_changed, payload_sha256, observed_at) "
            "VALUES (%s, %s, 'hosted_pro', 'hosted_pro', false, %s, NOW())",
            (economics.hash_external_ref(event), org.org_id, uuid.uuid4().hex * 2))
    for _ in range(2):
        with pytest.raises(economics.BillingOrgAcceptance):
            _billing(org.org_id, stripe_event_id=event)
    assert store.get_org(org.org_id).tier == "hosted_pro"
    assert _event_count(org.org_id) == 1


def test_demo_classification_tier_and_observation(period):
    with pytest.raises(ValueError, match="invalid organization account class"):
        store.create_org("Public demo injection", account_class="demo")
    org_id = uuid.uuid4()
    with db.cursor() as cur:
        cur.execute("INSERT INTO orgs (org_id, name, tier, account_class) "
                    "VALUES (%s, 'Demo accounting', 'demo_live', 'demo')", (org_id,))
    assert store.set_org_tier(org_id, "hosted_pro") is None
    assert store.get_org(org_id).tier == "demo_live"
    observation = economics.append_observation(**_observation(period, org_id))
    with db.cursor() as cur:
        cur.execute("SELECT org_id, org_account_class FROM unit_economics_observations "
                    "WHERE observation_key = %s", (observation["observation_key"],))
        assert dict(cur.fetchone()) == {"org_id": org_id, "org_account_class": "demo"}


def test_acceptance_billing_http_refuses_and_cannot_resolve(client, monkeypatch):
    monkeypatch.setenv(billing.TIER_SYNC_FLAG_ENV, "1")
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ENV, "acceptance-secret")
    org = _acceptance()
    subject = uuid.uuid4().hex
    store.create_identity_binding(org.org_id, "auth0", subject)
    emitted = []
    monkeypatch.setattr(api, "_emit_org_event", lambda *args: emitted.append(args))
    headers = {"X-Billing-Sync-Secret": "acceptance-secret"}
    for _ in range(2):
        response = client.post(f"/api/orgs/{org.org_id}/billing/tier-sync",
                               json={"plan": "starter", "subscription_active": True,
                                     "stripe_event_id": subject}, headers=headers)
        assert response.status_code == 409
        assert response.json() == {"detail": "production acceptance organization is excluded from billing; tier not updated"}
    response = client.post("/api/billing/org-resolve",
                           json={"external_authority": "auth0", "external_subject": subject}, headers=headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "no platform org for this identity"}
    assert emitted == []
    assert _event_count(org.org_id) == 0
    assert store.get_org(org.org_id).tier == "hosted_pro"


def test_acceptance_customer_report_excludes_all_sources(period, accounting):
    customer, acceptance, categories, before = accounting
    report = economics.fleet_report(*period)
    assert report["hosted_accounts"] == before + 1
    metrics = report["decision_metrics"]
    meters = metrics["marginal_cost_meters"]
    assert meters["agent"] == {"turns": 1, "usd_est": 2.0}
    assert meters["aps"] == {"runs": 1, "engine_seconds": 20.0, "usd_est": 2.0}
    assert meters["hosted_jobs_cross_check"] == {
        "succeeded_jobs": 1, "costed_jobs": 1, "usd_est": 2.0, "additive": False}
    assert metrics["renewal_signals"] == {
        "events": 1, "invoice_paid": 1, "payment_failed": 0, "canceled": 0,
        "tier_changes": 1, "event_types": {"invoice.paid": 1}}
    assert metrics["shared_fixed_cost_usd"] == 2.0
    assert metrics["shared_fixed_cost_per_hosted_account_usd"] == round(2 / (before + 1), 6)
    for kind in ("shared_fixed", "usage_variable", "revenue"):
        assert report["observations"][kind] == {categories[customer.org_id]: {"lines": 1, "amount_usd": 2.0}}
        assert categories[acceptance.org_id] not in report["observations"][kind]


def test_acceptance_cost_is_retained_and_labelled(period, accounting):
    customer, acceptance, categories, _ = accounting
    report = economics.fleet_report(*period)
    synthetic = report["synthetic_acceptance"]
    assert synthetic["account_class"] == "production_acceptance"
    assert synthetic["agent"] == {"turns": 2, "usd_est": 18.0}
    assert synthetic["aps"] == {"runs": 2, "engine_seconds": 180.0, "usd_est": 18.0}
    assert synthetic["hosted_jobs_cross_check"] == {
        "succeeded_jobs": 2, "costed_jobs": 2, "usd_est": 18.0, "additive": False}
    for kind in ("shared_fixed", "usage_variable", "revenue"):
        assert synthetic["observations"][kind] == {categories[acceptance.org_id]: {"lines": 2, "amount_usd": 18.0}}
    with db.cursor() as cur:
        for table, time_column, start, end in (
            ("agent_usage_turns", "ts", *period),
            ("broker_usage_ledger", "ts", period[0].timestamp(), period[1].timestamp()),
            ("jobs", "updated_at", *period)):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {time_column} >= %s AND {time_column} < %s", (start, end))
            assert cur.fetchone()["n"] == 3
    serialized = json.dumps(report)
    assert str(customer.org_id) not in serialized
    assert str(acceptance.org_id) not in serialized


def test_acceptance_observation_attribution_and_replay(period):
    customer = store.create_org("Observation customer")
    acceptance = _acceptance()
    fleet = _observation(period)
    first = economics.append_observation(**fleet)
    assert economics.append_observation(**fleet) == {**first, "recorded": False}
    # Freeze the pre-attribution digest, including omission of an org key.
    with db.cursor() as cur:
        cur.execute("SELECT payload_sha256, org_id, org_account_class FROM unit_economics_observations WHERE observation_key = %s",
                    (first["observation_key"],))
        row = cur.fetchone()
    assert row["org_id"] is None
    assert row["org_account_class"] is None
    assert row["payload_sha256"] == economics._canonical_digest({
        "period_start": period[0].isoformat(), "period_end": period[1].isoformat(),
        "kind": fleet["kind"], "category": fleet["category"], "amount_usd": "7",
        "quantity": None, "unit": None, "source": fleet["source"],
        "source_ref_sha256": None, "metadata": {}})
    attributed = _observation(period, customer.org_id)
    attributed_result = economics.append_observation(**attributed)
    with db.cursor() as cur:
        cur.execute("SELECT org_account_class FROM unit_economics_observations WHERE observation_key = %s",
                    (attributed_result["observation_key"],))
        assert cur.fetchone()["org_account_class"] == "customer"
    assert economics.append_observation(**attributed)["recorded"] is False
    with pytest.raises(economics.LedgerConflict):
        economics.append_observation(**{**attributed, "org_id": acceptance.org_id})
    with pytest.raises(economics.LedgerConflict):
        economics.append_observation(**{**fleet, "org_id": customer.org_id})
    for kind in ("shared_fixed", "usage_variable", "revenue"):
        economics.append_observation(**_observation(period, acceptance.org_id, kind=kind, category="synthetic"))
    with pytest.raises(ValueError, match="^observation org not found$"):
        economics.append_observation(**_observation(period, uuid.uuid4()))
    report = economics.fleet_report(*period)
    assert report["observations"]["revenue"] == {
        fleet["category"]: {"lines": 1, "amount_usd": 7.0},
        attributed["category"]: {"lines": 1, "amount_usd": 7.0}}
    for kind in ("shared_fixed", "usage_variable", "revenue"):
        assert "synthetic" not in report["observations"][kind]
        assert report["synthetic_acceptance"]["observations"][kind]["synthetic"] == {"lines": 1, "amount_usd": 7.0}


def test_acceptance_observation_replay_after_org_removal(period):
    org = _acceptance()
    facts = _observation(period, org.org_id)
    observation = economics.append_observation(**facts)
    assert observation["recorded"] is True
    live_replay = economics.append_observation(**facts)
    assert live_replay == {**observation, "recorded": False}
    with db.cursor() as cur:
        cur.execute("DELETE FROM orgs WHERE org_id = %s RETURNING org_id", (org.org_id,))
        assert cur.fetchone()["org_id"] == org.org_id
    assert economics.append_observation(**facts) == live_replay
    with pytest.raises(economics.LedgerConflict):
        economics.append_observation(**{**facts, "amount_usd": Decimal("8")})
    with pytest.raises(economics.LedgerConflict):
        economics.append_observation(**{**facts, "org_id": uuid.uuid4(), "amount_usd": Decimal("9")})
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM unit_economics_observations WHERE observation_key = %s",
                    (observation["observation_key"],))
        assert cur.fetchone()["n"] == 1


def test_acceptance_observation_class_survives_org_removal(period):
    org = _acceptance()
    facts = _observation(period, org.org_id)
    observation = economics.append_observation(**facts)
    with db.cursor() as cur:
        cur.execute("SELECT org_account_class FROM unit_economics_observations WHERE observation_key = %s",
                    (observation["observation_key"],))
        assert cur.fetchone()["org_account_class"] == "production_acceptance"
        # Raw deletion is confined to this test database.
        cur.execute("DELETE FROM orgs WHERE org_id = %s RETURNING org_id", (org.org_id,))
        assert cur.fetchone()["org_id"] == org.org_id
    report = economics.fleet_report(*period)
    assert facts["category"] not in report["observations"][facts["kind"]]
    assert report["synthetic_acceptance"]["observations"][facts["kind"]][facts["category"]] == {
        "lines": 1, "amount_usd": float(facts["amount_usd"])}


def test_acceptance_meter_time_boundaries(period):
    for org in (store.create_org("Boundary customer"), _acceptance()):
        _meters(org, period[0], 3)
        _meters(org, period[1], 100)
        _meters(org, period[0] - timedelta(seconds=1), 100)
        _meters(org, period[0] + timedelta(seconds=1), 5, job_status="failed", aps_status="error")
    report = economics.fleet_report(*period)
    for meters in (report["decision_metrics"]["marginal_cost_meters"], report["synthetic_acceptance"]):
        assert meters["agent"] == {"turns": 2, "usd_est": 8.0}
        assert meters["aps"] == {"runs": 1, "engine_seconds": 30.0, "usd_est": 3.0}
        assert meters["hosted_jobs_cross_check"] == {
            "succeeded_jobs": 1, "costed_jobs": 1, "usd_est": 3.0, "additive": False}
