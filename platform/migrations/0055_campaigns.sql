-- Durable campaign admission and single-use project questions. No dispatch here.
CREATE TABLE IF NOT EXISTS campaigns (
  campaign_id UUID PRIMARY KEY,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  tenant_id TEXT NOT NULL,
  principal_id UUID NOT NULL,
  title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
  prompt TEXT NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 32768),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
  submission_fingerprint TEXT NOT NULL CHECK (submission_fingerprint ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL DEFAULT 'accepted'
    CHECK (status IN ('accepted', 'running', 'succeeded', 'failed', 'cancelled')),
  dispatch_ref UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT campaigns_scope_idempotency_unique UNIQUE (org_id, project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_campaigns_scope
  ON campaigns(org_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS campaign_questions (
  question_id UUID PRIMARY KEY,
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id) ON DELETE CASCADE,
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  question_key TEXT NOT NULL CHECK (char_length(question_key) BETWEEN 1 AND 128
    AND question_key ~ '^[A-Za-z0-9._-]+$'),
  prompt TEXT NOT NULL CHECK (char_length(prompt) BETWEEN 1 AND 4096),
  options JSONB CHECK (CASE WHEN options IS NULL THEN TRUE
    WHEN jsonb_typeof(options) = 'array' THEN jsonb_array_length(options) <= 16
    ELSE FALSE END),
  asked_by TEXT NOT NULL CHECK (asked_by IN ('operator', 'worker')),
  blocks_dispatch BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id) ON DELETE CASCADE,
  CONSTRAINT campaign_questions_key_unique UNIQUE (campaign_id, question_key)
);

CREATE TABLE IF NOT EXISTS campaign_answers (
  answer_id UUID PRIMARY KEY,
  question_id UUID NOT NULL REFERENCES campaign_questions(question_id),
  campaign_id UUID NOT NULL REFERENCES campaigns(campaign_id),
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  principal_id UUID NOT NULL,
  answer TEXT NOT NULL CHECK (char_length(answer) BETWEEN 1 AND 8192),
  answer_fingerprint TEXT NOT NULL CHECK (answer_fingerprint ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (org_id, project_id) REFERENCES projects(org_id, project_id),
  CONSTRAINT campaign_answers_question_unique UNIQUE (question_id)
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'campaign_answers'::regclass
      AND tgname = 'campaign_answers_immutable'
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER campaign_answers_immutable
      BEFORE UPDATE OR DELETE ON campaign_answers
      FOR EACH ROW EXECUTE FUNCTION leaf_reject_ledger_mutation();
  END IF;
END
$$;
