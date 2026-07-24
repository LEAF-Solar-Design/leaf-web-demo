-- 0018_sessions_model.sql
-- Per-session "mount your LLM" model choice. Additive + idempotent (auto-applied
-- by platform/db.py apply_migration in sorted order): a session may pin its own
-- Claude-family model, overriding the runner's env default for its turns. NULL
-- means "use the env default" (LEAF_SPINE_MODEL, else claude-sonnet-5).

ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS model TEXT;
