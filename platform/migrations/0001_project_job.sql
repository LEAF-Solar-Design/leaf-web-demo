-- 0001_project_job.sql
-- Canonical Project/Job entity for the Leaf web-CAD platform.
-- Five tables: orgs, projects, drawing_versions, jobs, built_tools.
-- Idempotent (CREATE TABLE IF NOT EXISTS) so a concurrent orgs-defining sibling
-- and re-applies never hard-crash. Applies cleanly to a fresh Postgres/Neon DB.

-- orgs = the canonical tenant anchor (MATRIX: tenant identity is unowned/net-new; claim it here)
CREATE TABLE IF NOT EXISTS orgs (
  org_id        UUID PRIMARY KEY,
  name          TEXT NOT NULL,
  tier          TEXT NOT NULL DEFAULT 'hosted_starter',   -- mirrors DeploymentTier
  status        TEXT NOT NULL DEFAULT 'active',           -- active | offboarding | deleted
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  offboarded_at TIMESTAMPTZ
);

-- projects = holds a drawing + its versions + jobs + built tools
CREATE TABLE IF NOT EXISTS projects (
  project_id  UUID PRIMARY KEY,
  org_id      UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',             -- active | archived | deleted
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id);

-- drawing_versions = append-only version CHAIN (DWG is an unmergeable blob; single-writer, NOT git-merge)
CREATE TABLE IF NOT EXISTS drawing_versions (
  version_id  UUID PRIMARY KEY,
  project_id  UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  org_id      UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,   -- denormalized for isolation + offboard
  seq         INTEGER NOT NULL,        -- monotonic per project
  oss_object  TEXT,                    -- APS OSS / S3 key (bytes live out of band)
  intake_ref  TEXT,                    -- cached intake JSON key/ref
  created_by  TEXT,                    -- 'agent' | 'user' | user id
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (project_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_drawing_versions_project ON drawing_versions(project_id);

-- jobs = one async run; links to the async job spine (sibling gap #1) via nullable spine_ref
CREATE TABLE IF NOT EXISTS jobs (
  job_id            UUID PRIMARY KEY,
  project_id        UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  org_id            UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  kind              TEXT NOT NULL,       -- run | solve | build | extract
  tool_name         TEXT,
  status            TEXT NOT NULL DEFAULT 'queued',  -- queued|running|succeeded|failed|cancelled
  spine_ref         TEXT,                -- opaque handle into the async spine; nullable, no FK
  params            JSONB,
  result            JSONB,               -- result envelope (CONTRACT.md §3) when done
  input_version_id  UUID REFERENCES drawing_versions(version_id),
  output_version_id UUID REFERENCES drawing_versions(version_id),  -- write-path result
  cost_usd          NUMERIC(10,6),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_org_status ON jobs(org_id, status);

-- built_tools = per-tenant registry of agent-built tool packages (CONTRACT.md §2 shape)
CREATE TABLE IF NOT EXISTS built_tools (
  tool_id     UUID PRIMARY KEY,
  project_id  UUID NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  org_id      UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
  name        TEXT NOT NULL,             -- kebab-case
  version     TEXT NOT NULL DEFAULT '1.0.0',
  manifest    JSONB NOT NULL,            -- the tool package (CONTRACT.md §2)
  source_ref  TEXT,                      -- pointer into the mushy git repo / source store
  provenance  JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (project_id, name, version)
);
CREATE INDEX IF NOT EXISTS idx_built_tools_project ON built_tools(project_id);

-- (defense-in-depth, commented in v1) optional Row-Level Security:
-- ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY org_isolation ON projects USING (org_id = current_setting('app.current_org')::uuid);
