-- Preserve immutable originals and project membership; admit bounded gzip JSONL.
ALTER TABLE project_reference_materials
 DROP CONSTRAINT IF EXISTS project_reference_materials_media_type_check;
ALTER TABLE project_reference_materials
 ADD CONSTRAINT project_reference_materials_media_type_check
 CHECK (media_type IN ('video/quicktime','text/plain','application/json','application/gzip'));
ALTER TABLE project_reference_materials DROP CONSTRAINT IF EXISTS reference_text_size;
ALTER TABLE project_reference_materials ADD CONSTRAINT reference_text_size
 CHECK (media_type IN ('video/quicktime','application/gzip') OR byte_size <= 8388608);
