-- Additive customization target authority. Rollback retains this nullable
-- column and its data so older binaries can ignore it without data loss.
-- NULL means studio.
ALTER TABLE customization_change_sets ADD COLUMN IF NOT EXISTS tool_target TEXT;
