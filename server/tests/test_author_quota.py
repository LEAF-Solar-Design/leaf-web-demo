"""Per-tenant DAILY authoring quota (LEAF_DAILY_AUTHOR_QUOTA).

Mirrors the daily RUN quota suites (tests/test_hardening_quota.py for the gate,
tests/test_quota_shape.py for the wire shape), for the gate that bounds SERIAL
authoring sessions at the /api/author admission.

Why this gate exists: R5 authoring runs the Agent SDK harness and enables the
operator-funded sandbox BEFORE any broker lease, and its broker test is
aps_live=false / usd_est=null, so neither the broker's USD spend cap nor the
daily RUN quota ever observes an authoring turn. The harness's per-session
ceilings bound ONE session; nothing bounded serial sessions.

Acceptance covered here:
  * absent/empty LEAF_DAILY_AUTHOR_QUOTA -> quota OFF, the counter store is
    never touched, and authoring behaves exactly as before;
  * a configured quota admits attempts up to N and REFUSES N+1 with HTTP 429 and
    a quota_exceeded envelope, on BOTH /api/author and /api/author/stage;
  * a DIFFERENT tenant under its own cap is unaffected;
  * a new UTC day is a fresh window with no cron;
  * a refused attempt is NOT counted, and a retry under an existing idempotency
    key IS counted;
  * a misconfigured/unreachable counter store fails CLOSED (503), never open;
  * the 429 body carries the shape the frontend already understands.

Run:  cd server && python -m pytest tests/test_author_quota.py -q
"""
from __future__ import annotations

import json
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
    author_quota.reset_memory_state()
    author_quota.reset_postgres_counter()
    yield
    author_quota.reset_memory_state()
    author_quota.reset_postgres_counter()


@pytest.fixture
def r5_author(monkeypatch):
    """The R5 /api/author route with everything BUT the quota stubbed out, so a
    call either reaches the (recorded) stage dispatch or is refused by the quota."""
    staged = []
    service = SimpleNamespace(
        stage=lambda **kwargs: staged.append(kwargs) or {"status": "staged"}
    )
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: service),
    )

    def call(tenant="tenant-a", key="request-a", description="make a tool"):
        return author_router.author(
            author_router.AuthorRequest(description=description),
            tenant=tenant, idempotency_key=key,
        )

    return SimpleNamespace(call=call, staged=staged)


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


def test_counter_key_carries_day_and_tenant():
    assert author_quota.counter_key("tenant-a", "2026-07-30") == "2026-07-30:tenant-a"


def test_migration_matches_the_shared_counter_contract():
    sql = (PROJECT_ROOT / "platform" / "migrations" /
           "0023_author_quota_counters.sql").read_text(encoding="utf-8")
    assert f"CREATE TABLE IF NOT EXISTS {author_quota.COUNTER_TABLE}" in sql
    for column in ("namespace", "counter_key", "value", "updated_at"):
        assert column in sql
    assert "PRIMARY KEY (namespace, counter_key)" in sql


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
# admission: POST /api/author
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


def test_a_retry_under_the_same_idempotency_key_still_counts(monkeypatch, r5_author):
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


def test_a_refused_request_never_burns_a_slot(monkeypatch, r5_author):
    """The missing-idempotency-key refusal is decided BEFORE the charge, so the
    tenant's budget is intact afterwards."""
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
        raise author_quota.AuthorQuotaStoreError("author quota counter unavailable: OperationalError")

    monkeypatch.setattr(author_quota, "charge", unreachable)
    response = r5_author.call()
    assert response.status_code == 503
    assert json.loads(response.body)["reason_code"] == "author_quota_unavailable"
    assert r5_author.staged == []


def test_missing_policy_module_fails_closed_only_when_a_quota_is_configured(
    monkeypatch, r5_author
):
    monkeypatch.setattr(author_router, "_usage_policy", lambda: None)

    # No cap was ever asked for -> nothing to enforce, prior behavior.
    assert r5_author.call() == {"status": "staged"}

    # A cap WAS asked for and cannot be evaluated -> refuse, never unmetered.
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")
    response = r5_author.call()
    assert response.status_code == 503
    assert json.loads(response.body)["reason_code"] == "author_quota_unavailable"


def test_an_unresolvable_tier_still_produces_the_quota_envelope(monkeypatch, r5_author):
    """The tier is display-only here, so an entitlements outage must not turn a
    429 into a 500."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "0")

    def unresolvable(_tenant):
        raise RuntimeError("entitlements unavailable")

    monkeypatch.setattr(author_router.entitlements, "resolve_tier", unresolvable)
    response = r5_author.call()
    assert response.status_code == 429
    assert json.loads(response.body)["tier"] == "unknown"


def test_store_outage_response_leaks_no_detail(monkeypatch, r5_author):
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "5")

    def unreachable(*_a, **_k):
        raise author_quota.AuthorQuotaStoreError("postgres://user:secret@host/db is down")

    monkeypatch.setattr(author_quota, "charge", unreachable)
    body = json.loads(r5_author.call().body)
    assert "secret" not in json.dumps(body)
    assert "postgres" not in json.dumps(body)


# --------------------------------------------------------------------------- #
# admission: the legacy path and the sibling stage route
# --------------------------------------------------------------------------- #
def test_legacy_author_path_is_metered(monkeypatch):
    """The legacy path also delegates to the harness when LEAF_AUTHOR_HARNESS_URL
    is set, so it carries the same cap."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    calls = []
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: False)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: False)
    monkeypatch.setattr(author_router, "_legacy_author",
                        lambda *_a: calls.append(True) or {"source": "template"})

    def call():
        return author_router.author(
            author_router.AuthorRequest(description="make a tool"),
            tenant="tenant-a", idempotency_key="request-a",
        )

    assert call() == {"source": "template"}
    assert call().status_code == 429
    assert len(calls) == 1


def test_stage_route_shares_the_same_budget(monkeypatch):
    """/api/author/stage reaches the same harness, so metering only /api/author
    would leave the gate bypassable through the sibling route."""
    monkeypatch.setenv("LEAF_DAILY_AUTHOR_QUOTA", "1")
    staged = []
    service = SimpleNamespace(
        stage=lambda **kwargs: staged.append(kwargs) or {"status": "staged"}
    )
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)
    monkeypatch.setattr(author_router, "customization_enabled", lambda *_: True)
    monkeypatch.setattr(author_router.CustomizationService, "configured",
                        classmethod(lambda cls: service))

    request = author_router.StageRequest(
        description="make a tool", mode="build", idempotency_key="request-a")
    assert author_router.stage(request, tenant="tenant-a") == {"status": "staged"}

    refused = author_router.stage(request, tenant="tenant-a")
    assert refused.status_code == 429
    assert json.loads(refused.body)["quota_kind"] == "daily_author"
    assert len(staged) == 1
