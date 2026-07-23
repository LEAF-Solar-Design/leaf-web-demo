-- Fleet-visible authority for mutable drawing metadata and upload lifecycle.
-- Immutable drawing and intake bytes remain in the configured blob backend.

CREATE TABLE IF NOT EXISTS drawing_store_manifests (
    tenant_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    head BIGINT,
    latest BIGINT NOT NULL DEFAULT 0 CHECK (latest >= 0),
    checkout_holder TEXT,
    checkout_fence BIGINT NOT NULL DEFAULT 0 CHECK (checkout_fence >= 0),
    checkout_acquired_at TIMESTAMPTZ,
    checkout_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, drawing_id),
    CHECK (head IS NULL OR (head >= 1 AND head <= latest)),
    CHECK ((checkout_holder IS NULL AND checkout_acquired_at IS NULL AND checkout_expires_at IS NULL)
        OR (checkout_holder IS NOT NULL AND checkout_acquired_at IS NOT NULL AND checkout_expires_at IS NOT NULL))
);
ALTER TABLE drawing_store_manifests
    ADD COLUMN IF NOT EXISTS checkout_fence BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS drawing_store_versions (
    tenant_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version >= 1),
    parent_version BIGINT,
    object_key TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    workitem_id TEXT,
    tool TEXT,
    note TEXT,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'ready', 'orphaned')),
    reservation_token TEXT,
    reservation_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ready_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, drawing_id, version),
    UNIQUE (object_key),
    FOREIGN KEY (tenant_id, drawing_id)
        REFERENCES drawing_store_manifests (tenant_id, drawing_id)
        ON DELETE CASCADE
);
ALTER TABLE drawing_store_versions
    ADD COLUMN IF NOT EXISTS reservation_token TEXT;
ALTER TABLE drawing_store_versions
    ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS drawing_store_versions_ready_idx
    ON drawing_store_versions (tenant_id, drawing_id, version)
    WHERE state = 'ready';

CREATE TABLE IF NOT EXISTS drawing_upload_attempts (
    tenant_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    marker JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('extracting', 'ready', 'failed', 'purging', 'purged')),
    retention_expires_at TIMESTAMPTZ,
    extraction_owner TEXT,
    extraction_fence BIGINT NOT NULL DEFAULT 0,
    extraction_expires_at TIMESTAMPTZ,
    purge_owner TEXT,
    purge_fence BIGINT NOT NULL DEFAULT 0,
    purge_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, drawing_id)
);

CREATE INDEX IF NOT EXISTS drawing_upload_expiry_idx
    ON drawing_upload_attempts (retention_expires_at)
    WHERE status <> 'purged' AND retention_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS drawing_purge_receipts (
    tenant_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    purge_fence BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('deleted', 'failed')),
    freed_bytes BIGINT NOT NULL DEFAULT 0 CHECK (freed_bytes >= 0),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, drawing_id, attempt, purge_fence)
);
