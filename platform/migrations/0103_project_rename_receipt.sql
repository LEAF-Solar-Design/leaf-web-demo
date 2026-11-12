-- Project rename writes a durable lifecycle receipt like every other lifecycle action.
-- Additive: the nine existing action names stay valid and no row changes.
-- expand-contract: expand-only
ALTER TABLE project_lifecycle_receipts DROP CONSTRAINT IF EXISTS project_lifecycle_receipts_action_check;
ALTER TABLE project_lifecycle_receipts ADD CONSTRAINT project_lifecycle_receipts_action_check CHECK (action IN (
  'project_created', 'member_invited', 'member_revoked', 'file_put', 'file_deleted',
  'project_cloned', 'project_exported', 'project_reset', 'project_deleted',
  'project_renamed'
));
