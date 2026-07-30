-- 0023_author_quota_counters.sql
-- Per-tenant DAILY authoring-attempt counters. Additive + idempotent
-- (auto-applied by platform/db.py apply_migration in sorted order).
--
-- Why it exists: R5 tool authoring runs the Agent SDK harness and the
-- operator-funded sandbox BEFORE any broker lease, and its broker test is
-- aps_live=false / usd_est=null, so neither the USD spend cap nor the daily RUN
-- quota (guest_upload_counters' sibling gate in the broker ledger) ever sees an
-- authoring turn. Per-session ceilings bound one session, not serial sessions.
-- This table is the durable, restart-surviving count that bounds them.
--
-- Shape is the leaf_platform.counters.SharedCounterStore contract, exactly as
-- 0015_guest_caps.sql binds it for guest upload caps. counter_key carries the
-- UTC day and the tenant id, so a new day reads a fresh key with no cron and
-- yesterday's rows simply stop being read. The application deletes at most 100
-- expired rows after each accepted charge.

CREATE TABLE IF NOT EXISTS author_quota_counters (
  namespace   TEXT NOT NULL,
  counter_key TEXT NOT NULL,
  value       BIGINT NOT NULL CHECK (value >= 0),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (namespace, counter_key)
);

CREATE INDEX IF NOT EXISTS idx_author_quota_counters_updated
  ON author_quota_counters (updated_at);
