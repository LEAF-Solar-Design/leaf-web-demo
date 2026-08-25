"""Fleet-level unit-economics ledgers and decision report.

This module joins the platform's existing per-operation meters without exposing
tenant identifiers. It does not set prices. It makes the three inputs needed to
set them visible: shared cost per hosted account, marginal execution cost, and
subscription renewal signals.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from psycopg.types.json import Jsonb

from .db import run_transaction


class BillingOrgMissing(LookupError):
    """The billing event names no current platform organization."""


class BillingOrgInactive(RuntimeError):
    """Billing may not mutate an offboarding or deleted organization."""


class LedgerConflict(RuntimeError):
    """An idempotency key was replayed with different facts."""


def hash_external_ref(value: Optional[str]) -> Optional[str]:
    """Return a domain-separated digest for an external identifier."""
    if value is None or not value.strip():
        return None
    return hashlib.sha256(f"leaf-unit-economics:v1:{value}".encode("utf-8")).hexdigest()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return current.astimezone(timezone.utc)


def record_billing_tier_event(
    org_id: uuid.UUID,
    derived_tier: str,
    *,
    plan: Optional[str],
    subscription_active: Optional[bool],
    subscription_status: Optional[str],
    stripe_subscription_id: Optional[str],
    stripe_event_id: Optional[str],
    stripe_event_type: Optional[str],
    current_period_start: Optional[datetime],
    current_period_end: Optional[datetime],
    observed_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Atomically apply one tier event and append its measurement record."""
    period_start = _utc(current_period_start) if current_period_start else None
    period_end = _utc(current_period_end) if current_period_end else None
    if period_start and period_end and period_start >= period_end:
        raise ValueError("current_period_start must be before current_period_end")
    observed = _utc(observed_at)
    event_ref = hash_external_ref(stripe_event_id)
    subscription_ref = hash_external_ref(stripe_subscription_id)
    facts = {
        "org_id": str(org_id),
        "plan": plan,
        "subscription_active": subscription_active,
        "subscription_status": subscription_status,
        "stripe_event_ref_sha256": event_ref,
        "subscription_ref_sha256": subscription_ref,
        "stripe_event_type": stripe_event_type,
        "current_period_start": period_start.isoformat() if period_start else None,
        "current_period_end": period_end.isoformat() if period_end else None,
        "derived_tier": derived_tier,
    }
    payload_sha256 = _canonical_digest(facts)
    event_key = event_ref or hash_external_ref(f"unkeyed:{uuid.uuid4()}")
    assert event_key is not None

    def _operation(conn) -> Dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, tier, status FROM orgs WHERE org_id = %(org_id)s FOR UPDATE",
                {"org_id": org_id},
            )
            org = cur.fetchone()
            if org is None:
                raise BillingOrgMissing("org not found")
            if org["status"] != "active":
                raise BillingOrgInactive(str(org["status"]))

            if event_ref is not None:
                cur.execute(
                    "SELECT org_id, payload_sha256 FROM billing_subscription_events "
                    "WHERE event_key = %(event_key)s FOR UPDATE",
                    {"event_key": event_key},
                )
                replay = cur.fetchone()
                if replay is not None:
                    if replay["org_id"] != org_id or replay["payload_sha256"] != payload_sha256:
                        raise LedgerConflict("billing event id was replayed with different facts")
                    return {
                        "org_id": str(org_id),
                        "previous_tier": org["tier"],
                        "tier": org["tier"],
                        "applied": False,
                        "event_recorded": False,
                        "event_key": event_key,
                    }

            previous_tier = str(org["tier"])
            tier_changed = previous_tier != derived_tier
            cur.execute(
                "INSERT INTO billing_subscription_events "
                "(event_key, org_id, stripe_event_type, stripe_event_ref_sha256, "
                "subscription_ref_sha256, plan, subscription_active, subscription_status, "
                "current_period_start, current_period_end, previous_tier, derived_tier, "
                "tier_changed, payload_sha256, observed_at) VALUES "
                "(%(event_key)s, %(org_id)s, %(stripe_event_type)s, %(event_ref)s, "
                "%(subscription_ref)s, %(plan)s, %(subscription_active)s, "
                "%(subscription_status)s, %(period_start)s, %(period_end)s, "
                "%(previous_tier)s, %(derived_tier)s, %(tier_changed)s, "
                "%(payload_sha256)s, %(observed_at)s) "
                "ON CONFLICT (event_key) DO NOTHING RETURNING event_key",
                {
                    "event_key": event_key,
                    "org_id": org_id,
                    "stripe_event_type": stripe_event_type,
                    "event_ref": event_ref,
                    "subscription_ref": subscription_ref,
                    "plan": plan,
                    "subscription_active": subscription_active,
                    "subscription_status": subscription_status,
                    "period_start": period_start,
                    "period_end": period_end,
                    "previous_tier": previous_tier,
                    "derived_tier": derived_tier,
                    "tier_changed": tier_changed,
                    "payload_sha256": payload_sha256,
                    "observed_at": observed,
                },
            )
            if cur.fetchone() is None:
                # A concurrent delivery can race the pre-read when the same
                # Stripe event is misrouted to a different org. Refuse it and
                # leave the tier untouched instead of surfacing a raw unique
                # violation or applying facts under the wrong tenant.
                cur.execute(
                    "SELECT org_id, payload_sha256 FROM billing_subscription_events "
                    "WHERE event_key = %(event_key)s",
                    {"event_key": event_key},
                )
                replay = cur.fetchone()
                if (replay is None or replay["org_id"] != org_id
                        or replay["payload_sha256"] != payload_sha256):
                    raise LedgerConflict("billing event id was replayed with different facts")
                return {
                    "org_id": str(org_id),
                    "previous_tier": previous_tier,
                    "tier": previous_tier,
                    "applied": False,
                    "event_recorded": False,
                    "event_key": event_key,
                }
            cur.execute(
                "UPDATE orgs SET tier = %(tier)s "
                "WHERE org_id = %(org_id)s AND status = 'active' RETURNING tier",
                {"org_id": org_id, "tier": derived_tier},
            )
            updated = cur.fetchone()
            if updated is None:
                raise BillingOrgInactive("organization state changed during sync")
            return {
                "org_id": str(org_id),
                "previous_tier": previous_tier,
                "tier": str(updated["tier"]),
                "applied": tier_changed,
                "event_recorded": True,
                "event_key": event_key,
            }

    return run_transaction(_operation, isolation="serializable")


def append_observation(
    *,
    idempotency_key: str,
    period_start: datetime,
    period_end: datetime,
    kind: str,
    category: str,
    amount_usd: Decimal,
    quantity: Optional[Decimal],
    unit: Optional[str],
    source: str,
    source_ref: Optional[str],
    metadata: Mapping[str, Any],
) -> Dict[str, Any]:
    """Append one externally observed fleet cost or revenue line."""
    start, end = _utc(period_start), _utc(period_end)
    if start >= end:
        raise ValueError("period_start must be before period_end")
    observation_key = hash_external_ref(f"observation:{idempotency_key}")
    source_ref_sha256 = hash_external_ref(source_ref)
    assert observation_key is not None
    facts = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "kind": kind,
        "category": category,
        "amount_usd": str(amount_usd),
        "quantity": str(quantity) if quantity is not None else None,
        "unit": unit,
        "source": source,
        "source_ref_sha256": source_ref_sha256,
        "metadata": dict(metadata),
    }
    payload_sha256 = _canonical_digest(facts)

    def _operation(conn) -> Dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO unit_economics_observations "
                "(observation_key, period_start, period_end, kind, category, amount_usd, "
                "quantity, unit, source, source_ref_sha256, payload_sha256, metadata) VALUES "
                "(%(key)s, %(start)s, %(end)s, %(kind)s, %(category)s, %(amount)s, "
                "%(quantity)s, %(unit)s, %(source)s, %(source_ref)s, %(payload)s, %(metadata)s) "
                "ON CONFLICT (observation_key) DO NOTHING RETURNING observation_key",
                {
                    "key": observation_key,
                    "start": start,
                    "end": end,
                    "kind": kind,
                    "category": category,
                    "amount": amount_usd,
                    "quantity": quantity,
                    "unit": unit,
                    "source": source,
                    "source_ref": source_ref_sha256,
                    "payload": payload_sha256,
                    "metadata": Jsonb(dict(metadata)),
                },
            )
            if cur.fetchone() is not None:
                return {"observation_key": observation_key, "recorded": True}
            cur.execute(
                "SELECT payload_sha256 FROM unit_economics_observations "
                "WHERE observation_key = %(key)s",
                {"key": observation_key},
            )
            replay = cur.fetchone()
            if replay is None or replay["payload_sha256"] != payload_sha256:
                raise LedgerConflict("observation idempotency key was reused")
            return {"observation_key": observation_key, "recorded": False}

    return run_transaction(_operation)


def _money(value: Any) -> float:
    return round(float(value or 0), 6)


def fleet_report(period_start: datetime, period_end: datetime) -> Dict[str, Any]:
    """Return a tenant-free decision report for one half-open UTC period."""
    start, end = _utc(period_start), _utc(period_end)
    if start >= end:
        raise ValueError("period_start must be before period_end")

    def _read(conn) -> Dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS hosted_accounts FROM orgs "
                "WHERE status = 'active' AND tier LIKE 'hosted_%'"
            )
            hosted_accounts = int(cur.fetchone()["hosted_accounts"])

            cur.execute(
                "SELECT COUNT(*) AS events, "
                "COUNT(*) FILTER (WHERE stripe_event_type = 'invoice.paid') AS invoice_paid, "
                "COUNT(*) FILTER (WHERE stripe_event_type = 'invoice.payment_failed') AS payment_failed, "
                "COUNT(*) FILTER (WHERE subscription_status = 'canceled') AS canceled, "
                "COUNT(*) FILTER (WHERE tier_changed) AS tier_changes "
                "FROM billing_subscription_events "
                "WHERE observed_at >= %(start)s AND observed_at < %(end)s",
                {"start": start, "end": end},
            )
            billing = dict(cur.fetchone())

            cur.execute(
                "SELECT COALESCE(stripe_event_type, 'unknown') AS label, COUNT(*) AS count "
                "FROM billing_subscription_events "
                "WHERE observed_at >= %(start)s AND observed_at < %(end)s "
                "GROUP BY COALESCE(stripe_event_type, 'unknown') ORDER BY label",
                {"start": start, "end": end},
            )
            billing_event_types = {row["label"]: int(row["count"]) for row in cur.fetchall()}

            cur.execute(
                "SELECT COUNT(*) AS turns, "
                "COALESCE(SUM(CASE WHEN jsonb_typeof(record->'usd_est') = 'number' "
                "THEN (record->>'usd_est')::numeric ELSE 0 END), 0) AS usd_est "
                "FROM agent_usage_turns WHERE ts >= %(start)s AND ts < %(end)s",
                {"start": start, "end": end},
            )
            agent = dict(cur.fetchone())

            cur.execute(
                "SELECT COUNT(*) AS runs, COALESCE(SUM(engine_seconds), 0) AS engine_seconds, "
                "COALESCE(SUM(usd_est), 0) AS usd_est FROM broker_usage_ledger "
                "WHERE ts >= %(start)s AND ts < %(end)s AND status = 'ok'",
                {"start": start, "end": end},
            )
            aps = dict(cur.fetchone())

            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded_jobs, "
                "COUNT(*) FILTER (WHERE status = 'succeeded' AND cost_usd IS NOT NULL) "
                "AS costed_jobs, COALESCE(SUM(cost_usd) FILTER (WHERE status = 'succeeded'), 0) "
                "AS usd_est FROM jobs WHERE updated_at >= %(start)s AND updated_at < %(end)s",
                {"start": start, "end": end},
            )
            jobs = dict(cur.fetchone())

            cur.execute(
                "SELECT kind, category, COUNT(*) AS lines, COALESCE(SUM(amount_usd), 0) AS amount_usd "
                "FROM unit_economics_observations "
                "WHERE period_start >= %(start)s AND period_end <= %(end)s "
                "GROUP BY kind, category ORDER BY kind, category",
                {"start": start, "end": end},
            )
            observation_rows = [dict(row) for row in cur.fetchall()]

        observations: Dict[str, Dict[str, Dict[str, Any]]] = {
            "shared_fixed": {}, "usage_variable": {}, "revenue": {},
        }
        for row in observation_rows:
            observations[row["kind"]][row["category"]] = {
                "lines": int(row["lines"]),
                "amount_usd": _money(row["amount_usd"]),
            }
        fixed_cost = sum(row["amount_usd"] for row in observations["shared_fixed"].values())
        fixed_per_account = (
            round(fixed_cost / hosted_accounts, 6)
            if hosted_accounts and fixed_cost > 0 else None
        )
        gaps = []
        if fixed_per_account is None:
            gaps.append("shared fixed costs have not been observed for this period")
        if int(aps["runs"]) + int(agent["turns"]) + int(jobs["succeeded_jobs"]) == 0:
            gaps.append("no hosted work was metered for this period")
        if int(billing["events"]) == 0:
            gaps.append("no subscription lifecycle events were recorded for this period")

        return {
            "schema": "leaf.unit-economics-report.v1",
            "scope": "fleet",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "hosted_accounts": hosted_accounts,
            "decision_metrics": {
                "shared_fixed_cost_usd": round(fixed_cost, 6),
                "shared_fixed_cost_per_hosted_account_usd": fixed_per_account,
                "marginal_cost_meters": {
                    "agent": {"turns": int(agent["turns"]), "usd_est": _money(agent["usd_est"])},
                    "aps": {"runs": int(aps["runs"]), "engine_seconds": _money(aps["engine_seconds"]),
                            "usd_est": _money(aps["usd_est"])},
                    "hosted_jobs_cross_check": {
                        "succeeded_jobs": int(jobs["succeeded_jobs"]),
                        "costed_jobs": int(jobs["costed_jobs"]),
                        "usd_est": _money(jobs["usd_est"]),
                        "additive": False,
                    },
                },
                "renewal_signals": {
                    "events": int(billing["events"]),
                    "invoice_paid": int(billing["invoice_paid"]),
                    "payment_failed": int(billing["payment_failed"]),
                    "canceled": int(billing["canceled"]),
                    "tier_changes": int(billing["tier_changes"]),
                    "event_types": billing_event_types,
                },
            },
            "observations": observations,
            "coverage_gaps": gaps,
            "notes": [
                "Agent and APS estimates are additive; hosted job cost is a cross-check and may overlap.",
                "Hosted accounts are a current snapshot compared with costs from the selected period.",
                "This report measures pricing inputs and does not recommend or change a price.",
            ],
        }

    return run_transaction(
        _read, isolation="repeatable read", read_only=True, max_attempts=2,
    )
