-- X07 customer means the explicitly authorized project organization.
CREATE TABLE IF NOT EXISTS x07_customers (
    customer_key uuid PRIMARY KEY REFERENCES orgs(org_id),
    profile_id uuid NOT NULL UNIQUE,
    UNIQUE (customer_key, profile_id)
);

CREATE TABLE IF NOT EXISTS x07_profile_versions (
    customer_key uuid NOT NULL,
    profile_id uuid NOT NULL,
    version bigint NOT NULL CHECK (version BETWEEN 1 AND 9007199254740991),
    roles_json text NOT NULL CHECK (octet_length(roles_json) <= 131072),
    created_at text NOT NULL,
    PRIMARY KEY (customer_key, version),
    UNIQUE (customer_key, profile_id, version),
    FOREIGN KEY (customer_key, profile_id)
        REFERENCES x07_customers(customer_key, profile_id)
);

CREATE TABLE IF NOT EXISTS x07_project_applications (
    project_key uuid PRIMARY KEY,
    customer_key uuid NOT NULL,
    profile_id uuid NOT NULL,
    version bigint NOT NULL,
    FOREIGN KEY (customer_key, project_key) REFERENCES projects(org_id, project_id),
    FOREIGN KEY (customer_key, profile_id, version)
        REFERENCES x07_profile_versions(customer_key, profile_id, version)
);

CREATE OR REPLACE FUNCTION x07_immutable_role_record() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'X07 role versions and project pins are immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS x07_versions_immutable ON x07_profile_versions;
CREATE TRIGGER x07_versions_immutable BEFORE UPDATE OR DELETE ON x07_profile_versions
    FOR EACH ROW EXECUTE FUNCTION x07_immutable_role_record();
DROP TRIGGER IF EXISTS x07_pins_immutable ON x07_project_applications;
CREATE TRIGGER x07_pins_immutable BEFORE UPDATE OR DELETE ON x07_project_applications
    FOR EACH ROW EXECUTE FUNCTION x07_immutable_role_record();
