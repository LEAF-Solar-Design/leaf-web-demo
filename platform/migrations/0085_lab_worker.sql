-- Expand only. Native forward-normal schema release applies this migration.
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS claimed_build_revision TEXT;
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS claimed_color TEXT CHECK (claimed_color IN ('active','idle'));
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS cancel_requested_by TEXT;

CREATE TABLE IF NOT EXISTS lab_worker_status (
    worker_id TEXT PRIMARY KEY,
    build_revision TEXT NOT NULL,
    color TEXT NOT NULL CHECK (color IN ('active','idle')),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    active_runs INTEGER NOT NULL,
    queued_runs INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_worker_control (
    id TEXT PRIMARY KEY CHECK (id = 'global'),
    paused BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
