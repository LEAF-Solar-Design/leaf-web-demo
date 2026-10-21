-- phase: expand

ALTER TABLE broker_run_admissions ADD COLUMN terminal_intent_json JSONB;
ALTER TABLE broker_run_admissions ADD CONSTRAINT broker_terminal_intent_object
    CHECK (terminal_intent_json IS NULL OR jsonb_typeof(terminal_intent_json) = 'object');

CREATE FUNCTION leaf_guard_broker_terminal_intent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.terminal_intent_json IS NOT NULL THEN
            RAISE EXCEPTION 'broker terminal intent is immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.terminal_intent_json IS NOT NULL THEN
            RAISE EXCEPTION 'broker terminal intent requires an executing admission';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.terminal_intent_json IS NOT NULL THEN
        IF NEW.terminal_intent_json IS DISTINCT FROM OLD.terminal_intent_json
           OR NEW.event_key IS DISTINCT FROM OLD.event_key
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
           OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
           OR NEW.aps_live IS DISTINCT FROM OLD.aps_live THEN
            RAISE EXCEPTION 'broker terminal intent is immutable';
        END IF;
    ELSIF NEW.terminal_intent_json IS NOT NULL THEN
        IF OLD.state <> 'executing' OR NEW.state <> 'executing'
           OR NEW.event_key IS DISTINCT FROM OLD.event_key
           OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
           OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
           OR NEW.aps_live IS DISTINCT FROM OLD.aps_live THEN
            RAISE EXCEPTION 'broker terminal intent requires an executing admission';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER broker_terminal_intent_write_once
BEFORE INSERT OR UPDATE OR DELETE ON broker_run_admissions
FOR EACH ROW EXECUTE FUNCTION leaf_guard_broker_terminal_intent();
