-- Nullable for historical runs and cancellations before a worker attempt.
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS attempt_wall_cap_seconds INTEGER
    CHECK (attempt_wall_cap_seconds > 0);
