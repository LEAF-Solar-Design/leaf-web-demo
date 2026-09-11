-- App conversations must not share model transcripts merely because their
-- drawing IDs match. Leave legacy tables and indexes intact for rollback.
CREATE TABLE IF NOT EXISTS harness_app_sdk_sessions (
    tenant_id text NOT NULL,
    app_session_id text NOT NULL,
    sdk_session_id text,
    PRIMARY KEY (tenant_id, app_session_id)
);
