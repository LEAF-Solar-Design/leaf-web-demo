-- Expand only; rollback stops using custody. Tombstones retain no ciphertext.
-- Deliberately independent of the project-session storage tenant namespace.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='leaf_integration_grant_app') THEN
    CREATE ROLE leaf_integration_grant_app NOLOGIN NOSUPERUSER NOBYPASSRLS
      NOCREATEROLE NOCREATEDB NOREPLICATION;
  ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='leaf_integration_grant_app'
      AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole
        OR rolcreatedb OR rolreplication)) THEN
    RAISE EXCEPTION 'INTEGRATION_RUNTIME_ROLE_UNSAFE';
  END IF;
END $$;

CREATE TABLE public.integration_turn_grants (
  tenant_id UUID NOT NULL,
  project_id UUID NOT NULL,
  session_id TEXT NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 200),
  turn_id TEXT NOT NULL CHECK (char_length(turn_id) BETWEEN 1 AND 200),
  grant_id UUID NOT NULL,
  binding JSONB NOT NULL,
  created_at DOUBLE PRECISION NOT NULL,
  expires_at DOUBLE PRECISION NOT NULL,
  revoked_at DOUBLE PRECISION,
  envelope BYTEA,
  PRIMARY KEY (tenant_id, project_id, grant_id),
  UNIQUE (tenant_id, project_id, session_id, turn_id),
  CHECK (created_at >= 0 AND created_at < 'Infinity'::float8
    AND expires_at > created_at AND expires_at <= created_at + 120
    AND (revoked_at IS NULL OR (revoked_at >= 0 AND revoked_at < 'Infinity'::float8))),
  CHECK (envelope IS NULL OR octet_length(envelope) BETWEEN 1 AND 32768),
  CHECK (revoked_at IS NULL OR envelope IS NULL),
  CHECK (jsonb_typeof(binding) = 'object'
    AND binding ?& ARRAY['tenant_id','project_id','platform_user_id','binding_id',
      'external_authority','subject','session_id','turn_id','origin','operations',
      'grant_id','issued_at','verified_jwt_exp','turn_deadline','expires_at','schema','purpose']
    AND binding - ARRAY['tenant_id','project_id','platform_user_id','binding_id',
      'external_authority','subject','session_id','turn_id','origin','operations',
      'grant_id','issued_at','verified_jwt_exp','turn_deadline','expires_at','schema','purpose'] = '{}'::jsonb),
  CHECK ((binding->>'tenant_id' = tenant_id::text
    AND binding->>'project_id' = project_id::text
    AND binding->>'session_id' = session_id AND binding->>'turn_id' = turn_id
    AND binding->>'grant_id' = grant_id::text
    AND binding->>'external_authority' = 'auth0'
    AND binding->'schema' = '1'::jsonb
    AND binding->>'purpose' = 'studio-integration-turn-grant'
    AND binding->'operations' = '["dataset_discovery","dataset_query","element_save"]'::jsonb
    AND jsonb_typeof(binding->'issued_at') = 'number'
    AND jsonb_typeof(binding->'expires_at') = 'number'
    AND jsonb_typeof(binding->'verified_jwt_exp') = 'number'
    AND jsonb_typeof(binding->'turn_deadline') = 'number'
    AND (binding->>'issued_at')::float8 = created_at
    AND (binding->>'expires_at')::float8 = expires_at
    AND expires_at <= (binding->>'verified_jwt_exp')::float8
    AND expires_at <= (binding->>'turn_deadline')::float8) IS TRUE)
);

CREATE INDEX integration_turn_grants_expiry
  ON public.integration_turn_grants (tenant_id, project_id, expires_at, grant_id)
  WHERE envelope IS NOT NULL;

CREATE FUNCTION public.guard_integration_turn_grant() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF ROW(NEW.tenant_id, NEW.project_id, NEW.session_id, NEW.turn_id, NEW.grant_id,
      NEW.binding, NEW.created_at, NEW.expires_at)
      IS DISTINCT FROM ROW(OLD.tenant_id, OLD.project_id, OLD.session_id, OLD.turn_id,
      OLD.grant_id, OLD.binding, OLD.created_at, OLD.expires_at)
    OR (NEW.envelope IS NOT NULL AND NEW.envelope IS DISTINCT FROM OLD.envelope)
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
  THEN
    RAISE EXCEPTION 'GRANT_IMMUTABLE';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER integration_turn_grant_immutable BEFORE UPDATE ON public.integration_turn_grants
  FOR EACH ROW EXECUTE FUNCTION public.guard_integration_turn_grant();

ALTER TABLE public.integration_turn_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_turn_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY integration_turn_grant_scope ON public.integration_turn_grants
  FOR ALL TO leaf_integration_grant_app
  USING (tenant_id::text = current_setting('leaf.integration_tenant_id', true)
    AND project_id::text = current_setting('leaf.integration_project_id', true))
  WITH CHECK (tenant_id::text = current_setting('leaf.integration_tenant_id', true)
    AND project_id::text = current_setting('leaf.integration_project_id', true));
REVOKE ALL ON public.integration_turn_grants FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO leaf_integration_grant_app;
GRANT SELECT, INSERT, UPDATE ON public.integration_turn_grants TO leaf_integration_grant_app;
