-- Accept explicit action discovery on new grants without rewriting stored bindings.
ALTER TABLE public.integration_turn_grants
  DROP CONSTRAINT integration_turn_grants_check2,
  ADD CONSTRAINT integration_turn_grants_binding_authority_check
  CHECK ((binding->>'tenant_id' = tenant_id::text
    AND binding->>'project_id' = project_id::text
    AND binding->>'session_id' = session_id AND binding->>'turn_id' = turn_id
    AND binding->>'grant_id' = grant_id::text
    AND binding->>'external_authority' = 'auth0'
    AND binding->'schema' = '1'::jsonb
    AND binding->>'purpose' = 'studio-integration-turn-grant'
    AND binding->'operations' IN (
      '["dataset_discovery","dataset_query","element_save"]'::jsonb,
      '["dataset_discovery","dataset_query","element_save","action_discovery"]'::jsonb)
    AND jsonb_typeof(binding->'issued_at') = 'number'
    AND jsonb_typeof(binding->'expires_at') = 'number'
    AND jsonb_typeof(binding->'verified_jwt_exp') = 'number'
    AND jsonb_typeof(binding->'turn_deadline') = 'number'
    AND (binding->>'issued_at')::float8 = created_at
    AND (binding->>'expires_at')::float8 = expires_at
    AND expires_at <= (binding->>'verified_jwt_exp')::float8
    AND expires_at <= (binding->>'turn_deadline')::float8) IS TRUE);
