-- Keep the original comment (and denial attribution) across a decision-write retry.
ALTER TABLE customization_publication_requests ADD COLUMN IF NOT EXISTS review_details_json text;

CREATE TABLE IF NOT EXISTS customization_review_decisions (
    tenant_id text NOT NULL,
    change_set_id uuid NOT NULL,
    staged_commit char(40) NOT NULL,
    catalog_digest text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approve', 'request_changes')),
    approval_kind text NOT NULL CHECK (approval_kind IN ('self', 'independent')),
    approver_subject text NOT NULL,
    approver_role text NOT NULL CHECK (approver_role IN ('owner', 'reviewer')),
    comment text CHECK (char_length(comment) <= 500),
    confirmation_id uuid,
    decided_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, change_set_id, staged_commit)
);

CREATE OR REPLACE FUNCTION customization_review_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Customization review decisions are immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS customization_review_immutable ON customization_review_decisions;
CREATE TRIGGER customization_review_immutable BEFORE UPDATE OR DELETE ON customization_review_decisions
    FOR EACH ROW EXECUTE FUNCTION customization_review_immutable();
