-- Additive photo metadata. Original and derivative bytes remain in the private store.
CREATE TABLE IF NOT EXISTS project_materials (
 material_id UUID PRIMARY KEY,
 org_id UUID NOT NULL,
 project_id UUID NOT NULL,
 actor_binding_id UUID NOT NULL,
 name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 255),
 media_type TEXT NOT NULL CHECK (media_type IN ('image/jpeg','image/png','image/heif')),
 byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 16777216),
 sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
 width INTEGER NOT NULL CHECK (width > 0),
 height INTEGER NOT NULL CHECK (height > 0 AND width::bigint * height <= 40000000),
 preview_media_type TEXT NOT NULL DEFAULT 'image/jpeg' CHECK (preview_media_type = 'image/jpeg'),
 preview_sha256 TEXT NOT NULL CHECK (preview_sha256 ~ '^[0-9a-f]{64}$'),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE (org_id, project_id, sha256),
 FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
 FOREIGN KEY (org_id, actor_binding_id) REFERENCES identity_bindings(platform_tenant_id, binding_id)
);
CREATE INDEX IF NOT EXISTS project_materials_scope_page ON project_materials(org_id, project_id, material_id);
