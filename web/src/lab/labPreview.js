// Build-time opt-in only. Neither URL parameters nor stored preferences participate.
export function labPreviewEnabled(env = import.meta.env) {
  return env.DEV === true && env.VITE_LAB_PREVIEW === '1'
}

const concrete = (value) => typeof value === 'string' && value.trim().length > 0

export function resolveLabPreviewScope({
  enabled, mock, session, accountScope, tenant, orgId, openProjectId, workspace, wsLoading,
} = {}) {
  const unavailable = (reason) => ({ scopeKey: null, reason })
  if (enabled !== true) return unavailable('Lab is unavailable in this build.')
  if (mock !== false) return unavailable('Lab editing is unavailable in the offline demo. Sign in and open a project in a local development session.')
  if (session?.status !== 'active') return unavailable('Sign in with an active session to edit widgets.')
  if (!concrete(accountScope) || accountScope.trim() === 'guest') return unavailable('Sign in to resolve your account for widget drafts.')
  if (!Number.isSafeInteger(session.generation) || session.generation < 0) return unavailable('Wait for the session to finish loading.')
  if (!concrete(tenant)) return unavailable('Wait for the session tenant to finish loading.')
  if (!concrete(orgId)) return unavailable('Select an organization to edit widgets.')
  if (!concrete(openProjectId)) return unavailable('Open a project to edit widgets.')
  if (wsLoading !== false || !workspace?.project) return unavailable('Wait for project loading to finish.')
  if (workspace.project.project_id !== openProjectId) return unavailable('Wait for the selected project to finish loading.')
  if (workspace.project.org_id != null && workspace.project.org_id !== orgId) return unavailable('The loaded project does not match the selected organization.')
  // JSON arrays preserve tuple boundaries even when an identifier contains separators.
  // Account namespace partitions drafts; session.status supplies the readiness decision.
  return {
    scopeKey: JSON.stringify([accountScope, session.generation, tenant, orgId, openProjectId]),
    reason: null,
  }
}
