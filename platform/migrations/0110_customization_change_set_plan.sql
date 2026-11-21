-- Additive. Earlier binaries can continue to read and write legacy approvals
-- and change sets. Retain these nullable columns on application rollback.
ALTER TABLE agent_approvals
  ADD COLUMN IF NOT EXISTS proposer_subject TEXT,
  ADD COLUMN IF NOT EXISTS proposed_plan JSON,
  ADD COLUMN IF NOT EXISTS consumed_turn_id TEXT,
  ADD COLUMN IF NOT EXISTS retry_of_change_set_id TEXT;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_plan_approver_matches'
                 AND conrelid = 'agent_approvals'::regclass) THEN
    ALTER TABLE agent_approvals ADD CONSTRAINT agent_plan_approver_matches
      CHECK (action <> 'propose_build_plan' OR NOT granted OR
             (proposer_subject IS NOT NULL AND decided_by IS NOT NULL AND decided_by = proposer_subject));
  END IF;
END;
$$;

ALTER TABLE customization_change_sets
  ADD COLUMN IF NOT EXISTS plan_json TEXT,
  ADD COLUMN IF NOT EXISTS plan_digest TEXT,
  ADD COLUMN IF NOT EXISTS plan_confirmation_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS customization_change_sets_plan_idx
  ON customization_change_sets (tenant_id, plan_confirmation_id)
  WHERE plan_confirmation_id IS NOT NULL;
