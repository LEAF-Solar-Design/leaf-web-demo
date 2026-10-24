-- Expand only: all existing documents remain graphs. Widget documents must be
-- removed before rolling application code back to graph-only admission.
ALTER TABLE lab_documents
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'graph'
    CONSTRAINT lab_documents_kind_check CHECK (kind IN ('graph', 'widget'));
