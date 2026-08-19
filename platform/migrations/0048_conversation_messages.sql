-- 0048_conversation_messages.sql
-- Wave B durable conversation: the message store companion to 0046's
-- conversations table (card B-C1b; B-C2's write path targets this table).
-- Expand-only: creates one table and its indexes, drops nothing.

CREATE TABLE IF NOT EXISTS conversation_messages (
  message_id            UUID PRIMARY KEY,
  org_id                UUID NOT NULL,
  project_id            UUID NOT NULL,
  conversation_id       UUID NOT NULL,
  content               TEXT NOT NULL,
  metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by_binding_id UUID NOT NULL,
  idempotency_key       TEXT NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT conversation_messages_conversation_fk
    FOREIGN KEY (conversation_id)
    REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  -- The write path's ON CONFLICT arbiter: one row per idempotency key per
  -- conversation, scoped by tenant + project so keys never collide across
  -- tenants.
  CONSTRAINT conversation_messages_idempotency_unique
    UNIQUE (org_id, project_id, conversation_id, idempotency_key),
  -- Defense in depth behind the router's 32KB byte cap; octet_length matches
  -- the app-level UTF-8 byte measurement.
  CONSTRAINT conversation_messages_content_cap
    CHECK (octet_length(content) <= 32768),
  CONSTRAINT conversation_messages_idempotency_key_shape
    CHECK (char_length(idempotency_key) BETWEEN 1 AND 200)
);

-- Recovery reads walk a conversation's tail in order, tenant-scoped.
CREATE INDEX IF NOT EXISTS idx_conversation_messages_scope_order
  ON conversation_messages (org_id, project_id, conversation_id, created_at);
