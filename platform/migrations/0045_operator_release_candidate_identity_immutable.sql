-- Candidate identity immutability, ENFORCED at the schema level
-- (contract/OPERATOR.md section 7; O4). The composite primary key already makes
-- a second stage of the same (source_sha, target) a no-op conflict, but it does
-- not stop an UPDATE from rewriting the identity of an EXISTING reviewed row.
-- This BEFORE UPDATE trigger rejects any change to source_sha or target, so the
-- reviewed identity is immutable regardless of application-code discipline,
-- dynamic SQL, a quoted identifier, or a future handler. The trigger only READS
-- the identity columns and RAISEs; it never assigns them (it is protective, not
-- a rewriting rule). Effectively expand-only: the sole DROP below is the
-- idempotent recreate of this migration's OWN trigger, never a reader-visible
-- removal. The expand-contract gate's catch-all DROP scan still requires a
-- marker; it anchors to 0034, which created operator_release_candidates, the
-- table this trigger protects.
-- expand-contract: contract-of=0034

CREATE OR REPLACE FUNCTION operator_release_candidate_identity_immutable()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.source_sha IS DISTINCT FROM OLD.source_sha
     OR NEW.target IS DISTINCT FROM OLD.target THEN
    RAISE EXCEPTION
      'operator_release_candidate identity is immutable: source_sha/target cannot be updated'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_operator_release_candidate_identity_immutable
  ON operator_release_candidates;
CREATE TRIGGER trg_operator_release_candidate_identity_immutable
  BEFORE UPDATE ON operator_release_candidates
  FOR EACH ROW
  EXECUTE FUNCTION operator_release_candidate_identity_immutable();
