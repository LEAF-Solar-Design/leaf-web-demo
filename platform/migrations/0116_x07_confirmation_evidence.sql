-- Evidence is created only with a newly confirmed project pin.
CREATE TABLE x07_confirmation_evidence (
    project_key uuid PRIMARY KEY REFERENCES x07_project_applications(project_key),
    drawing_key uuid NOT NULL,
    input_version_id uuid NOT NULL,
    design_window_json text NOT NULL CHECK (octet_length(design_window_json) <= 256)
);

CREATE TRIGGER x07_confirmation_evidence_immutable
    BEFORE UPDATE OR DELETE ON x07_confirmation_evidence
    FOR EACH ROW EXECUTE FUNCTION x07_immutable_role_record();
