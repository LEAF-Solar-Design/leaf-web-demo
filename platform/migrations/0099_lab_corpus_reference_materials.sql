-- Preserve immutable originals and project membership; admit bounded gzip JSONL.
-- expand-contract: contract-of=0096
-- Replace the original-reference table's checks with supersets of their inputs.
-- Existing media types and byte limits remain valid; gzip originals are added.
ALTER TABLE project_reference_materials
 DROP CONSTRAINT IF EXISTS project_reference_materials_media_type_check;
ALTER TABLE project_reference_materials
 ADD CONSTRAINT project_reference_materials_media_type_check
 CHECK (media_type IN ('video/quicktime','text/plain','application/json','application/gzip'));
ALTER TABLE project_reference_materials DROP CONSTRAINT IF EXISTS reference_text_size;
ALTER TABLE project_reference_materials ADD CONSTRAINT reference_text_size
 CHECK (media_type IN ('video/quicktime','application/gzip') OR byte_size <= 8388608);
