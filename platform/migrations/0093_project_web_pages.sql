-- Expand only. Rollback retains the user's page revisions.
CREATE TABLE IF NOT EXISTS project_web_page_revisions (
    org_id UUID NOT NULL,
    project_id UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    digest TEXT NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    actor_binding_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, project_id, revision),
    FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, actor_binding_id) REFERENCES identity_bindings(platform_tenant_id, binding_id)
);

CREATE FUNCTION leaf_web_page_revision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'page revisions cannot be overwritten'; END;
$$;
CREATE TRIGGER project_web_page_revisions_immutable
BEFORE UPDATE ON project_web_page_revisions
FOR EACH ROW EXECUTE FUNCTION leaf_web_page_revision_immutable();
