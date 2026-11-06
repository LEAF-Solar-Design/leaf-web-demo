-- Server-derived queued-run authority and separate actual-reap acknowledgement.
ALTER TABLE lab_runs ADD COLUMN IF NOT EXISTS corpus_authority_json JSONB;
ALTER TABLE async_jobs ADD COLUMN IF NOT EXISTS corpus_cancel_json JSONB;
ALTER TABLE async_jobs ADD COLUMN IF NOT EXISTS corpus_cleanup_json JSONB;
