-- 0066: harness_confirmations gains the Kit's consumption columns (magpie AD4b, expand only).
--
-- mushy-code ad209705, the Kit commit the harness vendors, records WHO spent an
-- approved confirmation and WHEN (SessionStore.consumeApproved). Both columns are
-- nullable with no default and no backfill: an image that never consumes leaves
-- them NULL, and every earlier image ignores columns it does not name.
--
-- The status CHECK is deliberately NOT widened here. The harness image this
-- release replaces asserts the 4-value status CHECK exactly at startup
-- (harness/scripts/serve.ts -> assertHarnessCatalog), so widening it in the same
-- release would stop that image restarting and break its rollback. This release
-- ships a harness that accepts either CHECK; AD4b-2 widens it in a later release.
ALTER TABLE harness_confirmations ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;
ALTER TABLE harness_confirmations ADD COLUMN IF NOT EXISTS consumed_by TEXT;
