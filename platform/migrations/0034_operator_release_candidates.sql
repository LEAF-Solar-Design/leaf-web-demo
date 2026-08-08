-- Operator staging release candidates (contract/OPERATOR.md section 4.1,
-- operator.stage_release_candidate; O6, Wave 3; and section 7 production
-- unreachability). One IMMUTABLE row per (source_sha, target): the composite
-- primary key makes a second stage of the same candidate a no-op conflict, not
-- an overwrite. `target` is CHECK-constrained to non-production values only, so
-- a production release candidate is unrepresentable at the schema level. The
-- previous ECS task-definition revision is captured for the documented staging
-- rollback; no secret is stored. Expand-only: no contract-phase statement.

CREATE TABLE IF NOT EXISTS operator_release_candidates (
  source_sha                 TEXT NOT NULL,
  target                     TEXT NOT NULL
                             CHECK (target IN ('staging', 'development')),
  status                     TEXT NOT NULL DEFAULT 'staging'
                             CHECK (status IN ('staging', 'staged', 'failed')),
  previous_taskdef_revision  TEXT,
  staged_taskdef_revision    TEXT,
  staged_at                  TIMESTAMPTZ,
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_sha, target)
);
