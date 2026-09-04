-- Authored-tool provenance on a drawing version (standardization slice 6a).
--
-- `source_ref` is the sha256 the harness recorded in a `leaf.tool-source.v1`
-- receipt over an authored tool's source + manifest
-- (harness/contract/HARNESS-CONTRACT.md). It reaches the write path as
-- `execution_provenance.source_sha256` and is stored verbatim here; the read
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
