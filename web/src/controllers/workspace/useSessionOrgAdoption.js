import { useEffect } from 'react'

/**
 * Adopt the server-owned org id echoed by a live /api/session into the
 * workspace controller (which persists it under `leaf.org_id`).
 *
 * Live auth derives the org from the verified identity binding server-side
 * and ignores the client's X-Org-Id, so the stored id is presentation state
 * that live mode must self-heal: without this, a fresh browser with a valid
 * `leaf.jwt` but no `leaf.org_id` renders the "Create workspace org"
 * bootstrap affordance — whose POST /api/orgs 409s in ensure_org_with_identity
 * on a name mismatch — instead of listing the caller's projects.
 *
 * Auth-off and mock sessions echo no org (deps.tenant_echo no-ops), so the
 * null guard keeps the dev-seam behavior byte-identical.
 */
export default function useSessionOrgAdoption(sessionOrg, adoptOrgId) {
  useEffect(() => {
    if (sessionOrg) adoptOrgId(sessionOrg)
  }, [sessionOrg, adoptOrgId])
}
