-- Expand only. Release clears ownership but retains the fence.
-- Rollback stops serving these operations and retains the table.
CREATE TABLE IF NOT EXISTS project_drawing_checkouts (
  org_id UUID NOT NULL,
  project_id UUID NOT NULL,
  drawing_id UUID NOT NULL,
  holder TEXT,
  holder_binding_id UUID,
  acquired_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  fence BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT project_drawing_checkouts_pkey
    PRIMARY KEY (org_id, project_id, drawing_id),
  CONSTRAINT project_drawing_checkouts_artifact_fk
    FOREIGN KEY (drawing_id, project_id, org_id)
    REFERENCES drawing_artifacts(drawing_id, project_id, org_id)
    ON DELETE CASCADE,
  CONSTRAINT project_drawing_checkouts_binding_fk
    FOREIGN KEY (org_id, holder_binding_id)
    REFERENCES identity_bindings(platform_tenant_id, binding_id),
  CONSTRAINT project_drawing_checkouts_fence_check CHECK (fence >= 0),
  CONSTRAINT project_drawing_checkouts_holder_check CHECK (
    holder IS NULL OR (
      char_length(btrim(holder)) BETWEEN 1 AND 200
      AND holder <> 'anonymous:unnamed-writer'
    )
  ),
  CONSTRAINT project_drawing_checkouts_state_check CHECK (
    (holder IS NULL AND holder_binding_id IS NULL
      AND acquired_at IS NULL AND expires_at IS NULL)
    OR (holder IS NOT NULL AND holder_binding_id IS NOT NULL
      AND acquired_at IS NOT NULL AND expires_at IS NOT NULL
      AND isfinite(acquired_at) AND isfinite(expires_at)
      AND expires_at > acquired_at
      AND expires_at <= acquired_at + INTERVAL '24 hours'
      AND fence > 0)
  )
);

CREATE OR REPLACE FUNCTION guard_project_drawing_checkout()
RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.org_id, NEW.project_id, NEW.drawing_id)
     IS DISTINCT FROM ROW(OLD.org_id, OLD.project_id, OLD.drawing_id) THEN
    RAISE EXCEPTION 'checkout identity is immutable';
  END IF;
  IF NEW.holder IS NULL THEN
    IF NEW.fence <> OLD.fence THEN
      RAISE EXCEPTION 'checkout release must retain its fence';
    END IF;
  ELSIF ROW(NEW.holder, NEW.holder_binding_id,
      NEW.acquired_at, NEW.expires_at, NEW.fence)
    IS DISTINCT FROM ROW(OLD.holder, OLD.holder_binding_id,
      OLD.acquired_at, OLD.expires_at, OLD.fence) THEN
    IF OLD.fence = 9223372036854775807 OR NEW.fence <> OLD.fence + 1 THEN
      RAISE EXCEPTION 'checkout grant must advance its fence';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'project_drawing_checkouts_guard'
      AND tgrelid = 'project_drawing_checkouts'::regclass
  ) THEN
    CREATE TRIGGER project_drawing_checkouts_guard
      BEFORE UPDATE ON project_drawing_checkouts
      FOR EACH ROW EXECUTE FUNCTION guard_project_drawing_checkout();
  END IF;
END;
$$;
