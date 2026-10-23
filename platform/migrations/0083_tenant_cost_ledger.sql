-- Managed add-on costs are immutable; settings describe the current choice.
CREATE TABLE IF NOT EXISTS tenant_cost_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES orgs(org_id),
    addon TEXT NOT NULL,
    mode TEXT NOT NULL,
    vendor TEXT NOT NULL,
    unit TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    unit_cost_usd NUMERIC(18,6) NOT NULL,
    cost_usd NUMERIC(18,6) NOT NULL,
    cost_basis TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT tenant_cost_events_addon_check CHECK (addon IN (
        'aps_solve', 'e2b_exec', 'marathon', 'forge_hosting', 'aws_resources',
        'storage', 'canonical_solve', 'tool_ci', 'gpu_inference', 'ios_build', 'app_auth')),
    CONSTRAINT tenant_cost_events_mode_check CHECK (mode IN ('managed', 'byo')),
    -- Vendor names have 1 to 100 characters; units have 1 to 50 characters.
    CONSTRAINT tenant_cost_events_vendor_check CHECK (char_length(btrim(vendor)) BETWEEN 1 AND 100),
    CONSTRAINT tenant_cost_events_unit_check CHECK (char_length(btrim(unit)) BETWEEN 1 AND 50),
    -- Zero is the minimum quantity and monetary cost; numeric special values are forbidden.
    CONSTRAINT tenant_cost_events_quantity_check CHECK (
        quantity >= 0 AND quantity <> 'Infinity'::numeric AND quantity <> 'NaN'::numeric),
    CONSTRAINT tenant_cost_events_unit_cost_check CHECK (
        unit_cost_usd >= 0 AND unit_cost_usd <> 'Infinity'::numeric AND unit_cost_usd <> 'NaN'::numeric),
    CONSTRAINT tenant_cost_events_cost_check CHECK (
        cost_usd >= 0 AND cost_usd <> 'Infinity'::numeric AND cost_usd <> 'NaN'::numeric),
    CONSTRAINT tenant_cost_events_basis_check CHECK (cost_basis IN ('measured', 'estimated', 'allocated')),
    -- A SHA-256 event key has exactly 64 lowercase hexadecimal characters.
    CONSTRAINT tenant_cost_events_key_check CHECK (event_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT tenant_cost_events_provenance_check CHECK (jsonb_typeof(provenance) = 'object'),
    -- BYO events cost the platform zero dollars.
    CONSTRAINT tenant_cost_events_byo_check CHECK (mode <> 'byo' OR cost_usd = 0)
);

CREATE INDEX IF NOT EXISTS idx_tenant_cost_events_org_period
    ON tenant_cost_events(org_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_tenant_cost_events_org_addon_period
    ON tenant_cost_events(org_id, addon, occurred_at);

CREATE OR REPLACE FUNCTION leaf_reject_tenant_cost_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'immutable tenant cost ledger record'
        USING ERRCODE = '55000';
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER tenant_cost_events_immutable
    BEFORE UPDATE OR DELETE ON tenant_cost_events
    FOR EACH ROW EXECUTE FUNCTION leaf_reject_tenant_cost_mutation();

CREATE TABLE IF NOT EXISTS tenant_addon_settings (
    org_id UUID NOT NULL REFERENCES orgs(org_id),
    addon TEXT NOT NULL,
    mode TEXT NOT NULL,
    byo_ref TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (org_id, addon),
    CONSTRAINT tenant_addon_settings_addon_check CHECK (addon IN (
        'aps_solve', 'e2b_exec', 'marathon', 'forge_hosting', 'aws_resources',
        'storage', 'canonical_solve', 'tool_ci', 'gpu_inference', 'ios_build', 'app_auth')),
    CONSTRAINT tenant_addon_settings_mode_check CHECK (mode IN ('managed', 'byo', 'off')),
    -- Credential names have 1 to 128 characters and never contain spaces or values.
    CONSTRAINT tenant_addon_settings_ref_check CHECK (
        byo_ref IS NULL OR byo_ref ~ '^[A-Za-z0-9_./:-]{1,128}$'),
    CONSTRAINT tenant_addon_settings_byo_check CHECK (
        (mode = 'byo' AND byo_ref IS NOT NULL) OR (mode <> 'byo' AND byo_ref IS NULL)),
    -- An audit actor has 1 to 200 characters.
    CONSTRAINT tenant_addon_settings_actor_check CHECK (char_length(btrim(updated_by)) BETWEEN 1 AND 200)
);
