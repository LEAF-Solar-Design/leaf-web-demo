-- B5: owner-entered member labels, optional for existing bindings.
ALTER TABLE identity_bindings ADD COLUMN IF NOT EXISTS display_name TEXT CONSTRAINT identity_bindings_display_name_check CHECK (display_name IS NULL OR (char_length(display_name) BETWEEN 1 AND 100 AND display_name = btrim(display_name) AND display_name !~ '[[:cntrl:]]'));
