"""Per-tenant DAILY authoring quota (LEAF_DAILY_AUTHOR_QUOTA).

Mirrors the daily RUN quota suites (tests/test_hardening_quota.py for the gate,
tests/test_quota_shape.py for the wire shape), for the gate that bounds SERIAL
authoring sessions on the /api/author admission path.

Why this gate exists: R5 authoring runs the Agent SDK harness and enables the
operator-funded sandbox BEFORE any broker lease, and its broker test is
aps_live=false / usd_est=null, so neither the broker's USD spend cap nor the
daily RUN quota ever observes an authoring turn. The harness's per-session
ceilings bound ONE session; nothing bounded serial sessions.

WHERE THE CHARGE LIVES. Two lanes reach the authoring harness, so each is
metered at its own last-point-before-spend:
  * R5 (/api/author and /api/author/stage) both funnel through
    CustomizationService.stage, which charges immediately before _harness_stage.
    Everything deterministic has already refused by then -- invalid mode, blank
    description, missing binding, a tier without Build, a role authorize_stage
    denies, and an already-STAGED replay that returns its durable receipt without
    calling the harness -- so none of those spends a slot.
  * The legacy auth-off path calls the harness straight from _legacy_author, so
    the router meters it there.

Acceptance covered here:
  * absent/empty LEAF_DAILY_AUTHOR_QUOTA -> quota OFF, the counter store is
    never touched, and authoring behaves exactly as before;
  * a configured quota admits attempts up to N and REFUSES N+1 with HTTP 429 on
    BOTH R5 routes and on the legacy path;
  * a DIFFERENT tenant under its own cap is unaffected;
  * a new UTC day is a fresh window with no cron;
  * a refused attempt is NOT counted, and a retry that reaches the harness IS;
  * memory counters are refused wherever they would not be a real cap;
  * auth-off callers share ONE anonymous budget (the tenant id is a header);
  * a misconfigured/unreachable counter store fails CLOSED (503), never open;
  * the 429 body carries the shape the frontend already understands;
  * a refusal emits one greppable INFO line carrying the counter facts, on both
    metered lanes, so the cap can be audited from the log group at all.

Run:  cd server && python -m pytest tests/test_author_quota.py -q
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import author_quota  # noqa: E402
import deps  # noqa: E402
from routers import author as author_router  # noqa: E402


def _load_usage():
    """da/usage.py, loaded the way the routers load it (it is not a `server`
    package module) — mirrors tests/test_quota_shape.py::_load_usage."""
    mod = deps._load_module_from(deps.DA_DIR / "usage.py", "leaf_usage_author_quota")
    assert mod is not None, "da/usage.py failed to import"
    return mod


@pytest.fixture(autouse=True)
def clean_counter_state(monkeypatch):
    monkeypatch.delenv("LEAF_DAILY_AUTHOR_QUOTA", raising=False)
    monkeypatch.delenv("LEAF_AUTHOR_QUOTA_STORE", raising=False)
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    author_quota.reset_memory_state()
    author_quota.reset_postgres_counter()
    author_quota.reset_usage_policy()
    yield
    author_quota.reset_memory_state()
    author_quota.reset_postgres_counter()
    author_quota.reset_usage_policy()


@pytest.fixture
def allow_memory(monkeypatch):
    """Accept the memory counter. Live auth and deployed postures demand the
    durable one, which no unit test has; tests of THAT rule drive it directly."""
    monkeypatch.setattr(author_quota, "durability_required", lambda: False)


@pytest.fixture
def r5_author(monkeypatch, allow_memory):
    """The R5 /api/author route with the service replaced by a stand-in that
    charges exactly where the real CustomizationService.stage charges: last,
    immediately before the harness call. test_service_charges_immediately_before
    _the_harness_call pins that placement against the real source."""
    staged = []

    def fake_stage(*, tenant, description, mode, idempotency_key):
        tier = author_router.entitlements.resolve_tier(tenant)
        author_quota.enforce(str(tenant), tier)
        staged.append({"tenant": tenant, "description": description,
                       "mode": mode, "idempotency_key": idempotency_key})
        return {"status": "staged"}

    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: SimpleNamespace(stage=fake_stage)),
    )

    def call(tenant="tenant-a", key="request-a", description="make a tool"):
        return author_router.author(
            author_router.AuthorRequest(description=description),
            tenant=tenant, idempotency_key=key,
        )

    return SimpleNamespace(call=call, staged=staged)


@pytest.fixture
def legacy_author(monkeypatch, allow_memory):
    """The auth-off legacy path, which delegates to the harness when
    LEAF_AUTHOR_HARNESS_URL is set and so carries the same cap."""
    calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: False)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: False)
    monkeypatch.setattr(author_router, "_legacy_author",
                        lambda *_a: calls.append(True) or {"source": "template"})

    def call(tenant="tenant-a"):
        return author_router.author(
            author_router.AuthorRequest(description="make a tool"),
            tenant=tenant, idempotency_key="request-a",
        )

    return SimpleNamespace(call=call, calls=calls)


# --------------------------------------------------------------------------- #
# policy: the env switch (da/usage.py)
# --------------------------------------------------------------------------- #
def test_quota_is_off_unless_configured(monkeypatch):
    usage = _load_usage()
    monkeypatch.delenv("LEAF_DAILY_AUTHOR_QUOTA", raising=False)
    assert usage.daily_author_quota() is None
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "")
    assert usage.daily_author_quota() is None
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "   ")
    assert usage.daily_author_quota() is None


def test_configured_quota_parses_and_typos_fail_closed(monkeypatch):
    usage = _load_usage()
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "10")
    assert usage.daily_author_quota() == 10
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")
    assert usage.daily_author_quota() == 0        # a valid kill switch
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "-5")
    assert usage.daily_author_quota() == 0        # never unmetered
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "ten")
    assert usage.daily_author_quota() == 0        # never unmetered


def test_counter_day_is_the_utc_calendar_day():
    usage = _load_usage()
    # 2026-07-29T23:59:59Z and the second after it are different windows.
    assert usage.author_quota_day(1785369599.0) == "2026-07-29"
    assert usage.author_quota_day(1785369600.0) == "2026-07-30"


# --------------------------------------------------------------------------- #
# wire shape: what the frontend receives
# --------------------------------------------------------------------------- #
def test_author_quota_envelope_matches_the_daily_run_quota_shape():
    usage = _load_usage()
    runs = usage.daily_quota_envelope("tenant-a", "hosted_starter", 20, 20)
    author = usage.daily_author_envelope("tenant-a", "hosted_starter", 10, 10)

    # Same keys, same types, so no consumer needs a new branch.
    assert set(author) == set(runs)
    assert set(author["error"]) == set(runs["error"])
    assert author["error_code"] == "quota_exceeded"
    assert author["retryable"] is True
    assert author["ok"] is False
    assert (author["tier"], author["limit"], author["used"]) == ("hosted_starter", 10, 10)

    # Only the discriminator differs, top-level AND nested.
    assert runs["quota_kind"] == "daily_runs"
    assert author["quota_kind"] == "daily_author"
    assert author["error"]["quota_kind"] == "daily_author"
    assert "10/10" in author["message"] and "00:00 UTC" in author["message"]


def test_envelope_is_json_serializable_with_a_boolean_degraded_mode():
    usage = _load_usage()
    body = usage.daily_author_envelope("tenant-a", "demo", 1, 1)
    body["degraded_mode"] = False  # what the router puts on the wire
    assert json.loads(json.dumps(body))["degraded_mode"] is False


# --------------------------------------------------------------------------- #
# counter store
# --------------------------------------------------------------------------- #
def test_memory_is_default_and_postgres_is_explicit(monkeypatch):
    assert author_quota.store_mode() == "memory"
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_STORE", "postgres")
    assert author_quota.store_mode() == "postgres"


def test_invalid_store_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_STORE", "typo")
    with pytest.raises(author_quota.AuthorQuotaStoreError, match="LEAF_AUTHOR_QUOTA_STORE"):
        author_quota.charge("tenant-a", "2026-07-30", 5)


def test_charge_counts_per_tenant_per_day():
    assert author_quota.charge("tenant-a", "2026-07-30", 2) == (True, 1)
    assert author_quota.charge("tenant-a", "2026-07-30", 2) == (True, 2)
    # third attempt refused, and the refusal does NOT increment the count
    assert author_quota.charge("tenant-a", "2026-07-30", 2) == (False, 2)
    assert author_quota.charge("tenant-a", "2026-07-30", 2) == (False, 2)
    # a different tenant has its own budget
    assert author_quota.charge("tenant-b", "2026-07-30", 2) == (True, 1)
    # a new UTC day is a fresh window, with no cron
    assert author_quota.charge("tenant-a", "2026-07-31", 2) == (True, 1)


def test_memory_charge_replays_an_admission_without_double_counting():
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 2, idempotency_key="change-a"
    ) == (True, 1)
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 2, idempotency_key="change-a"
    ) == (True, 1)
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 2, idempotency_key="change-b"
    ) == (True, 2)


def test_memory_charge_replays_a_refusal_without_changing_the_counter():
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 1, idempotency_key="change-a"
    ) == (True, 1)
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 1, idempotency_key="change-b"
    ) == (False, 1)


def test_memory_attempt_identity_is_tenant_scoped_and_policy_immutable():
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 1, idempotency_key="change-a",
        tier="hosted_pro",
    ) == (True, 1)
    # The same attempt keeps its admitted outcome after midnight and after a
    # tier or limit change. It does not consume the new day's counter.
    assert author_quota.charge(
        "tenant-a", "2026-07-31", 0, idempotency_key="change-a", tier="demo"
    ) == (True, 1)
    assert "2026-07-31:tenant-a" not in author_quota._MEMORY_STATE
    # Another tenant may use the same caller key without inheriting the result.
    assert author_quota.charge(
        "tenant-b", "2026-07-31", 0, idempotency_key="change-a", tier="demo"
    ) == (False, 0)
    assert author_quota.charge(
        "tenant-a", "2026-07-30", 1, idempotency_key="change-b"
    ) == (False, 1)


def test_counter_key_carries_day_and_tenant():
    assert author_quota.counter_key("tenant-a", "2026-07-30") == "2026-07-30:tenant-a"


def test_memory_counter_is_refused_when_durability_is_required():
    """Two replicas keep two independent memory counts and a restart clears both,
    so a quota configured against memory in a real deployment reads as enforced
    while bounding almost nothing. It fails loudly instead."""
    with pytest.raises(author_quota.AuthorQuotaStoreError, match="LEAF_AUTHOR_QUOTA_STORE"):
        author_quota.charge("tenant-a", "2026-07-30", 5, require_durable=True)
    # ...and nothing was counted by the refused call.
    assert author_quota.charge("tenant-a", "2026-07-30", 5) == (True, 1)


@pytest.mark.parametrize("posture", ["staging", "production", "prod-eu", "anything-else"])
def test_deployed_postures_require_the_durable_counter(monkeypatch, posture):
    """Keyed on deployed posture, NOT on whether auth is live: an auth-off
    staging deployment still runs replicas and still restarts."""
    monkeypatch.setenv("LEAF_RUNTIME_ENV", posture)
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    assert author_quota.durability_required() is True


@pytest.mark.parametrize("posture", ["development", "test", "local", "  DEVELOPMENT  "])
def test_local_postures_accept_the_memory_counter(monkeypatch, posture):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", posture)
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    assert author_quota.durability_required() is False


def test_an_undeclared_posture_requires_the_durable_counter(monkeypatch):
    """Unset is NOT local: the app image bakes no posture and a direct container
    run enforces no required-config, so a deployment that opts into the quota
    with no declared posture would otherwise meter per process. Memory needs an
    EXPLICIT local posture — auth state does not soften this."""
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    assert author_quota.durability_required() is True


def test_a_local_posture_still_requires_durability_under_live_auth(monkeypatch):
    """A deployment claiming `development` while running live auth is a
    misdeclaration, and money-side the safe reading is `deployed`."""
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "development")
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    assert author_quota.durability_required() is True


def test_migration_matches_the_shared_counter_contract():
    sql = (PROJECT_ROOT / "platform" / "migrations" /
           "0023_author_quota_counters.sql").read_text(encoding="utf-8")
    assert f"CREATE TABLE IF NOT EXISTS {author_quota.COUNTER_TABLE}" in sql
    for column in ("namespace", "counter_key", "value", "updated_at"):
        assert column in sql
    assert "PRIMARY KEY (namespace, counter_key)" in sql

    admission_sql = (PROJECT_ROOT / "platform" / "migrations" /
                     "0027_author_quota_idempotency.sql").read_text(
                         encoding="utf-8"
                     )
    assert f"CREATE TABLE IF NOT EXISTS {author_quota.ATTEMPT_TABLE}" in admission_sql
    for column in (
        "tenant_id", "attempt_key", "counter_key", "quota_day", "quota_tier",
        "quota_limit", "accepted", "used",
    ):
        assert column in admission_sql
    assert "PRIMARY KEY (tenant_id, attempt_key)" in admission_sql
    assert "author_quota_attempts_immutable" in admission_sql
    assert "BEFORE UPDATE OR DELETE" in admission_sql


@pytest.mark.parametrize("accepted", [True, False])
def test_postgres_replay_returns_stored_policy_without_recharging(
    monkeypatch, accepted
):
    stored = {
        "tenant_id": "tenant-a", "attempt_key": "change-a",
        "counter_key": "2026-07-30:tenant-a", "quota_day": "2026-07-30",
        "quota_tier": "hosted_pro", "quota_limit": 1,
        "accepted": accepted, "used": 1,
    }
    calls = []

    class Cursor:
        def fetchone(self):
            return stored

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            if "pg_advisory_xact_lock" in sql:
                return Cursor()
            if "FROM author_quota_attempts" in sql:
                return Cursor()
            raise AssertionError("stored replay must not touch the aggregate counter")

    class Database:
        @staticmethod
        def run_transaction(operation, **_kwargs):
            return operation(Connection())

    monkeypatch.setattr(
        author_quota, "_load_platform_counters", lambda: (Database(), object)
    )
    monkeypatch.setattr(
        author_quota, "_postgres_counter",
        lambda: SimpleNamespace(
            consume_in_transaction=lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(AssertionError("counter must not charge"))
        ),
    )
    decision = author_quota._postgres_charge(
        "tenant-a", "2026-08-01", "demo", 99, "change-a"
    )
    assert (decision.accepted, decision.day, decision.tier, decision.limit) == (
        accepted, "2026-07-30", "hosted_pro", 1,
    )
    query_params = [params for sql, params in calls if "FROM author_quota_attempts" in sql]
    assert query_params == [{"tenant_id": "tenant-a", "attempt_key": "change-a"}]


def test_retention_window_is_bounded(monkeypatch):
    assert author_quota.retention_days() == 8
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_RETENTION_DAYS", "2")
    assert author_quota.retention_days() == 2
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_RETENTION_DAYS", "1")
    with pytest.raises(author_quota.AuthorQuotaStoreError):
        author_quota.retention_days()
    monkeypatch.setenv("LEAF_AUTHOR_QUOTA_RETENTION_DAYS", "nope")
    with pytest.raises(author_quota.AuthorQuotaStoreError):
        author_quota.retention_days()


# --------------------------------------------------------------------------- #
# enforce(): the charge point itself
# --------------------------------------------------------------------------- #
def test_enforce_is_a_no_op_without_a_configured_quota(monkeypatch, allow_memory):
    def explode(*_a, **_k):
        raise AssertionError("counter store must not be touched with no quota")

    monkeypatch.setattr(author_quota, "charge", explode)
    for _ in range(5):
        assert author_quota.enforce("tenant-a", "hosted_starter") is None


def test_enforce_raises_with_the_facts_the_envelope_reports(monkeypatch, allow_memory):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "2")
    author_quota.enforce("tenant-a", "hosted_starter")
    author_quota.enforce("tenant-a", "hosted_starter")
    with pytest.raises(author_quota.AuthorQuotaExceeded) as caught:
        author_quota.enforce("tenant-a", "hosted_starter")
    assert (caught.value.tier, caught.value.limit, caught.value.used) == (
        "hosted_starter", 2, 2)


def test_enforce_admitted_replay_survives_midnight_and_policy_change(
    monkeypatch, allow_memory
):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert author_quota.enforce(
        "tenant-a", "hosted_pro", now_ts=1785369599.0,
        idempotency_key="change-a",
    ) is None
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")
    assert author_quota.enforce(
        "tenant-a", "demo", now_ts=1785369600.0,
        idempotency_key="change-a",
    ) is None
    assert "2026-07-30:tenant-a" not in author_quota._MEMORY_STATE


def test_enforce_refused_replay_keeps_original_day_tier_and_limit(
    monkeypatch, allow_memory
):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")
    with pytest.raises(author_quota.AuthorQuotaExceeded) as first:
        author_quota.enforce(
            "tenant-a", "demo", now_ts=1785283200.0,
            idempotency_key="change-a",
        )
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "100")
    with pytest.raises(author_quota.AuthorQuotaExceeded) as replay:
        author_quota.enforce(
            "tenant-a", "hosted_pro", now_ts=1785369600.0,
            idempotency_key="change-a",
        )
    assert (replay.value.day, replay.value.tier, replay.value.limit,
            replay.value.used) == (
        first.value.day, "demo", 0, 0,
    )


def test_enforce_fails_closed_when_the_policy_module_is_missing(monkeypatch, allow_memory):
    monkeypatch.setattr(author_quota, "usage_policy", lambda: None)

    # No cap was ever asked for -> nothing to enforce, prior behavior.
    assert author_quota.enforce("tenant-a", "demo") is None

    # A cap WAS asked for and cannot be evaluated -> refuse, never unmetered.
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")
    with pytest.raises(author_quota.AuthorQuotaStoreError):
        author_quota.enforce("tenant-a", "demo")


@pytest.mark.parametrize("url,secret,misconfigured", [
    ("", "s3cret", True),                      # no URL at all
    ("   ", "s3cret", True),                   # whitespace-only URL
    ("harness.internal:8150", "s3cret", True), # no http(s) scheme -> requests refuses client-side
    ("http://harness.internal:bad", "s3cret", True),   # invalid port -> InvalidURL at prepare
    ("http://[::1", "s3cret", True),           # malformed IPv6 literal -> urlsplit raises
    ("http://harness.internal:8150", "", True),    # blank secret -> the caller gate 401s every retry
    ("http://harness.internal:8150", "  \r\n", True),
    ("http://harness.internal:8150", "secret\r\nX-Injected: yes", True),  # header-invalid secret
    ("http://harness.internal:8150", "s3cret-☃", True),  # non-Latin-1: http.client can never send it
    ("http://harness.internal:8150", "sécret", False),   # Latin-1 non-ASCII IS sendable
    ("http://harness.internal:8150", "s3cret", False),
    ("https://harness.internal:8150/", "s3cret", False),
    ("  http://harness.internal:8150  ", "s3cret", False),  # padding: one shared normalization
])
def test_harness_misconfiguration_is_deterministic_and_refused(
    monkeypatch, url, secret, misconfigured
):
    """The stage() admission refuses a harness that can never accept a dispatch
    BEFORE charging the quota, so a misconfigured deployment does not burn one
    slot per attempt while returning the same 503 forever."""
    import customization_service

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    monkeypatch.setenv("LEAF_HARNESS_SECRET", secret)
    assert customization_service._harness_misconfigured() is misconfigured


def test_service_charges_immediately_before_the_harness_call():
    """Pins the placement sol-critic's round-2 finding required: the charge must
    sit AFTER every deterministic refusal and IMMEDIATELY BEFORE the harness
    call, so nothing refused earlier spends a slot."""
    source = (SERVER_DIR / "customization_service.py").read_text(encoding="utf-8")
    charge = source.index("author_quota.enforce(")
    harness = source.index("self._harness_stage(tenant_id, description, change)")
    assert charge < harness
    # Nothing but the assignment sits between them.
    between = source[source.index("\n", charge):harness]
    assert between.strip() == "body =", between

    for earlier in (
        'raise CustomizationServiceError("invalid_stage_request", 422)',   # mode/description
        'raise CustomizationServiceError("builder_entitlement_missing", 403)',  # tier
        "self._authority().authorize_stage(",                             # role/binding
        "return self._receipt(change)",                                   # STAGED replay
        'raise CustomizationServiceError("stage_not_available")',         # bad state
        # An unconfigured harness 503s every attempt deterministically, so its
        # guard must also refuse before the charge (source.index finds the
        # hoisted guard in stage(), which precedes _harness_stage's own).
        'raise CustomizationServiceError("customization_harness_unavailable", 503)',
    ):
        assert source.index(earlier) < charge, earlier


# --------------------------------------------------------------------------- #
# admission: the R5 routes
# --------------------------------------------------------------------------- #
def test_no_quota_configured_never_touches_the_counter(monkeypatch, r5_author):
    def explode(*_a, **_k):
        raise AssertionError("counter store must not be touched with no quota")

    monkeypatch.setattr(author_quota, "charge", explode)
    for _ in range(5):
        assert r5_author.call() == {"status": "staged"}
    assert len(r5_author.staged) == 5


def test_attempts_are_admitted_up_to_the_cap_then_refused(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "2")

    assert r5_author.call() == {"status": "staged"}
    assert r5_author.call() == {"status": "staged"}

    response = r5_author.call()
    assert response.status_code == 429
    body = json.loads(response.body)
    assert body["error"]["error_code"] == "quota_exceeded"
    assert body["quota_kind"] == "daily_author"
    assert (body["limit"], body["used"]) == (2, 2)
    assert body["retryable"] is True
    assert body["degraded_mode"] is False
    # the refused attempt never reached the harness-backed stage path
    assert len(r5_author.staged) == 2


def test_a_refusal_is_logged_with_the_counter_facts(monkeypatch, caplog, r5_author):
    """A refusal must leave a trace an operator can query for.

    Before this, the refusal path logged nothing at all: a refused attempt was
    visible only as an access-log 429. That made "no quota events in the log
    group" evidence of nothing, because the observation could not have come out
    any other way -- it fit the cap working perfectly and the cap refusing every
    attempt equally well.

    INFO, not WARNING: a caller drives this and it is an expected budget state,
    so it must not page anyone. INFO, not DEBUG: deployments run at INFO, so a
    DEBUG line would leave the cap exactly as unobservable as it was.
    """
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    usage = _load_usage()
    assert r5_author.call(tenant="tenant-a") == {"status": "staged"}

    with caplog.at_level(logging.INFO, logger=author_router._LOG.name):
        response = r5_author.call(
            tenant="tenant-a", key="secret-key", description="secret description",
        )
    assert response.status_code == 429
    body = json.loads(response.body)

    refusals = [r for r in caplog.records
                if r.getMessage().startswith("daily_author_quota_refused:")]
    assert len(refusals) == 1, "exactly one refusal line per refused attempt"
    line = refusals[0].getMessage()
    assert refusals[0].levelno == logging.INFO
    assert "tenant=tenant-a" in line
    # the counter facts agree with the envelope the caller was handed, so the
    # log and the 429 cannot drift into telling two different stories
    assert f"tier={body['tier']}" in line
    assert f"limit={body['limit']}" in line
    assert f"used={body['used']}" in line
    # the UTC counter day the charge was actually keyed on
    assert f"day={usage.author_quota_day()}" in line
    # never free-text from the request
    assert "secret" not in line


def test_an_admitted_attempt_logs_no_refusal(monkeypatch, caplog, r5_author):
    """The token must mean what an operator queries it for. If it were emitted on
    the admitted path too, a count of it would measure traffic, not refusals."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "2")
    with caplog.at_level(logging.INFO, logger=author_router._LOG.name):
        assert r5_author.call() == {"status": "staged"}
    assert not [r for r in caplog.records
                if "daily_author_quota_refused:" in r.getMessage()]


def test_the_legacy_path_refusal_is_logged_too(monkeypatch, caplog, legacy_author):
    """Both metered lanes answer through _quota_exceeded_response, so both are
    observable. The auth-off lane meters one shared anonymous budget, and the
    log names that principal rather than the unverified request header."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert legacy_author.call(tenant="tenant-a") == {"source": "template"}

    with caplog.at_level(logging.INFO, logger=author_router._LOG.name):
        assert legacy_author.call(tenant="tenant-b").status_code == 429

    refusals = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("daily_author_quota_refused:")]
    assert len(refusals) == 1
    assert f"tenant={author_router.ANONYMOUS_QUOTA_PRINCIPAL}" in refusals[0]


def test_a_retry_that_reaches_the_harness_counts_again(monkeypatch, r5_author):
    """A change set still in STAGING re-invokes the harness on retry, so counting
    change-set rows instead of attempts would leave one key looping unbounded."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert r5_author.call(key="same-key") == {"status": "staged"}
    assert r5_author.call(key="same-key").status_code == 429


def test_a_different_tenant_is_unaffected(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert r5_author.call(tenant="tenant-a") == {"status": "staged"}
    assert r5_author.call(tenant="tenant-a").status_code == 429
    assert r5_author.call(tenant="tenant-b") == {"status": "staged"}


def test_zero_quota_is_an_authoring_kill_switch(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")
    assert r5_author.call().status_code == 429
    assert r5_author.staged == []


def test_a_request_refused_before_the_charge_never_burns_a_slot(monkeypatch, r5_author):
    """The missing-idempotency-key refusal is decided in the router, before the
    service is even constructed, so the tenant's budget is intact afterwards."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    missing_key = author_router.author(
        author_router.AuthorRequest(description="make a tool"),
        tenant="tenant-a", idempotency_key=None,
    )
    assert missing_key.status_code == 422
    assert r5_author.call() == {"status": "staged"}


def test_store_outage_fails_closed(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")

    def unreachable(*_a, **_k):
        raise author_quota.AuthorQuotaStoreError(
            "author quota counter unavailable: OperationalError")

    monkeypatch.setattr(author_quota, "charge", unreachable)
    response = r5_author.call()
    assert response.status_code == 503
    assert json.loads(response.body)["reason_code"] == "author_quota_unavailable"
    assert r5_author.staged == []


def test_store_outage_response_leaks_no_detail(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")

    def unreachable(*_a, **_k):
        raise author_quota.AuthorQuotaStoreError("postgres://user:secret@host/db is down")

    monkeypatch.setattr(author_quota, "charge", unreachable)
    body = json.loads(r5_author.call().body)
    assert "secret" not in json.dumps(body)
    assert "postgres" not in json.dumps(body)


def test_stage_route_shares_the_same_budget(monkeypatch, allow_memory):
    """/api/author/stage reaches the harness through the same stage(), which is
    where the charge lives, so neither route can bypass it via the other."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    staged = []

    def fake_stage(*, tenant, description, mode, idempotency_key):
        author_quota.enforce(str(tenant), "hosted_starter")
        staged.append(idempotency_key)
        return {"status": "staged"}

    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router.CustomizationService, "configured",
                        classmethod(lambda cls: SimpleNamespace(stage=fake_stage)))

    request = author_router.StageRequest(
        description="make a tool", mode="build", idempotency_key="request-a")
    assert author_router.stage(request, tenant="tenant-a") == {"status": "staged"}

    refused = author_router.stage(request, tenant="tenant-a")
    assert refused.status_code == 429
    assert json.loads(refused.body)["quota_kind"] == "daily_author"
    assert len(staged) == 1


# --------------------------------------------------------------------------- #
# admission: the legacy (auth-off) path
# --------------------------------------------------------------------------- #
def test_legacy_author_path_is_metered(monkeypatch, legacy_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert legacy_author.call() == {"source": "template"}
    assert legacy_author.call().status_code == 429
    assert len(legacy_author.calls) == 1


def test_auth_off_tenants_share_one_anonymous_budget(monkeypatch, legacy_author):
    """With auth off the tenant id is an unverified request header. Metering it
    per-id would let a caller mint a new id per request and hand itself a fresh
    budget forever, so the open lane shares ONE budget."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    assert legacy_author.call(tenant="tenant-a") == {"source": "template"}
    assert legacy_author.call(tenant="tenant-b").status_code == 429
    assert legacy_author.call(tenant="whatever-i-invent").status_code == 429
    assert len(legacy_author.calls) == 1


def test_legacy_build_denied_tenant_never_burns_a_slot(monkeypatch, legacy_author):
    """_legacy_author refuses a tier without Build deterministically, so charging
    first would let it drain the budget on requests that reach no harness."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    monkeypatch.setattr(author_router.entitlements, "entitlements_for",
                        lambda _tier, *_roles: {"build": False})
    for _ in range(5):
        assert legacy_author.call() == {"source": "template"}  # stub; real one 403s

    monkeypatch.setattr(author_router.entitlements, "entitlements_for",
                        lambda _tier, *_roles: {"build": True})
    assert legacy_author.call() == {"source": "template"}
    assert legacy_author.call().status_code == 429


def test_legacy_unreadable_entitlement_policy_still_charges(monkeypatch, legacy_author):
    """On the money side the safe direction is to count."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")

    def unreadable(_tier, *_roles):
        raise author_router.entitlements.EntitlementsError("policy unreadable")

    monkeypatch.setattr(author_router.entitlements, "entitlements_for", unreadable)
    assert legacy_author.call() == {"source": "template"}
    assert legacy_author.call().status_code == 429


def test_legacy_unresolvable_tier_still_produces_the_quota_envelope(
    monkeypatch, legacy_author
):
    """The tier is display-only in this envelope, so a tier-resolution failure
    must not turn a 429 into a 500."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")
    monkeypatch.setattr(author_router.entitlements, "resolve_tier",
                        lambda _t: (_ for _ in ()).throw(RuntimeError("outage")))
    monkeypatch.setattr(author_router.entitlements, "entitlements_for",
                        lambda _tier, *_roles: {"build": True})
    response = legacy_author.call()
    assert response.status_code == 429
    assert json.loads(response.body)["tier"] == "unknown"


# --------------------------------------------------------------------------- #
# frontend contract: both author error paths carry the quota tag
# --------------------------------------------------------------------------- #
def test_both_web_author_error_paths_tag_the_quota():
    """The app's Generate button calls stageAuthorTool, whose errors go through
    customizationError -- so tagging only the legacy authorTool path would leave
    the calm QuotaGate unreachable in the shipped UI."""
    api = (PROJECT_ROOT / "web" / "src" / "api.js").read_text(encoding="utf-8")
    assert "function tagAuthorQuota(" in api
    # One call site per error path: the legacy authorTool client and
    # customizationError (which stageAuthorTool throws through).
    assert "tagAuthorQuota(err, res.status, body)" in api
    assert len(re.findall(r"(?<!function )tagAuthorQuota\(err, ", api)) == 2
    assert re.search(r"function customizationError\([^)]*\)\s*\{(?:[^}]|\}(?!\n\}))*"
                     r"return tagAuthorQuota\(err, status, body\)", api), api[:0]

    panel = (PROJECT_ROOT / "web" / "src" / "components" /
             "AuthorPanel.jsx").read_text(encoding="utf-8")
    assert "e.quotaExceeded" in panel and "<QuotaGate" in panel
