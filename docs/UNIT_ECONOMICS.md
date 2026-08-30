# Unit economics measurement

Leaf is the Solar revenue beachhead inside the larger natural-language CAD
platform. This lane measures whether hosted work and platform maintenance can
support a transparent price without hiding variable service cost inside an
unlimited subscription.

It answers three questions. It does not set a price.

1. What shared cost does each active hosted account carry?
2. What does one more unit of LLM, APS, or hosted job work cost?
3. Do paid invoices, payment failures, and cancellations support a recurring
   maintenance or service relationship?

## Durable inputs

Migration `0051_unit_economics.sql` adds two append-only ledgers.

- `billing_subscription_events` records the subscription facts used for each
  tier decision. Stripe event and subscription IDs are SHA-256 digests. A
  Stripe event replay is measured once and cannot overwrite a newer tier.
- `unit_economics_observations` accepts observed shared costs, external
  variable costs, and revenue. The caller supplies an idempotency key. Provider
  invoice references are stored only as digests.

The report also reads existing meters:

- `agent_usage_turns.record.usd_est` for estimated model cost
- `broker_usage_ledger` for APS runs, engine seconds, and estimated APS cost
- `jobs.cost_usd` as a hosted-job cross-check

The hosted-job value can overlap the APS value, so the report marks it
`additive: false` and never folds it into a total.

## Ops API

Both routes use the existing fail-closed `X-Ops-Secret` gate.

- `GET /api/ops/unit-economics?period_start=<UTC>&period_end=<UTC>` returns a
  fleet report. It contains no tenant IDs or raw external identifiers.
- `POST /api/ops/unit-economics/observations` appends one observed fleet line.

Example shared-cost observation:

```json
{
  "idempotency_key": "aws-production-2026-08-shared",
  "period_start": "2026-08-01T00:00:00Z",
  "period_end": "2026-09-01T00:00:00Z",
  "kind": "shared_fixed",
  "category": "hosting",
  "amount_usd": "125.50",
  "source": "aws-cost-explorer",
  "source_ref": "monthly-export-2026-08",
  "metadata": {"environment": "production"}
}
```

Use `usage_variable` for an external per-use bill that is not already present
in an internal meter. Use `revenue` for settled revenue observations.

## Ownership loop

`.github/workflows/unit-economics-report.yml` runs each Monday and on manual
dispatch. It fetches the last closed calendar month, retains the sanitized JSON
and Markdown for 90 days, and creates or updates one issue named
`Leaf unit economics measurement`.

Repository configuration:

- Variable `LEAF_UNIT_ECONOMICS_REPORT_URL`, the full ops report endpoint
- Secret `LEAF_UNIT_ECONOMICS_OPS_SECRET`, the matching ops secret

Missing configuration and an unavailable endpoint still produce the standing
issue. Missing fixed-cost observations, usage events, or billing events appear
as explicit coverage gaps. This makes lack of measurement visible work rather
than an implied zero.

The issue is the owner surface. The database ledgers and workflow artifacts are
the evidence. Price changes remain a separate product decision.
