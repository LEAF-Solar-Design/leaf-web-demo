-- 0022_sessions_active_turn_subject.sql
-- The authenticated subject that opened the currently active turn. Additive +
-- idempotent (auto-applied by platform/db.py apply_migration in sorted order).
--
-- Why it exists: the harness back-edge authenticates as a TENANT (a dispatch
-- secret plus X-Tenant-Id), never as a user, so protected tool authoring —
-- which requires a verified owner/editor binding — could never resolve a
-- subject and always failed closed. Rather than let the harness assert an
-- identity, the app records who opened the turn and derives the subject from
-- its own authority when that turn calls back.
--
-- Written and cleared in the same statements as active_turn_tier, so it is
-- alive only for the duration of one authenticated turn. NULL means "no active
-- turn, or a turn opened before this column existed". Both fail closed: the
-- back edge is refused an identity, and once auth is live a confirm-once
-- action asks for a fresh confirmation rather than matching an unbound grant.

ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS active_turn_subject TEXT;
