-- Additive original-reference metadata; photo schema and private objects survive.
CREATE TABLE IF NOT EXISTS project_reference_materials (
 material_id UUID PRIMARY KEY,
 org_id UUID NOT NULL,
 project_id UUID NOT NULL,
 actor_binding_id UUID NOT NULL,
 name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 255),
 media_type TEXT NOT NULL CHECK (media_type IN ('video/quicktime','text/plain','application/json')),
 byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 67108864),
 CONSTRAINT reference_text_size CHECK (media_type = 'video/quicktime' OR byte_size <= 8388608),
 sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CONSTRAINT reference_scope_digest UNIQUE (org_id, project_id, sha256),
 CONSTRAINT reference_project_fk FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
 CONSTRAINT reference_actor_fk FOREIGN KEY (org_id, actor_binding_id) REFERENCES identity_bindings(platform_tenant_id, binding_id)
);
CREATE INDEX IF NOT EXISTS reference_scope_page ON project_reference_materials(org_id, project_id, material_id);
