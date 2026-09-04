-- Authored-tool provenance on a drawing version (standardization slice 6a).
--
-- `source_ref` is the sha256 the SERVER measures over the published tool body it
-- holds for the tool id (server/tool_loader.py published_tool_source_sha256),
-- the same bytes every sandbox tier is fed and a genuine `leaf.tool-source.v1`
-- receipt hashes. Nothing a sandbox returned ever reaches this column; a
-- microvm receipt is only cross-checked against the server's digest. The read
-- path (server/routers/drawings.py `_source_ref`) bounds and charset-validates
-- it before it can reach a client, so free text in this column can never be
-- rendered as provenance.
--
-- Nullable on purpose and forever: a version written by a tool with no receipt
-- carries NULL, and NULL means "not established", never "unauthored". No
-- backfill exists or ever will, because a receipt cannot be reconstructed
-- after the fact without inventing it.
ALTER TABLE drawing_store_versions
  ADD COLUMN IF NOT EXISTS source_ref TEXT;
