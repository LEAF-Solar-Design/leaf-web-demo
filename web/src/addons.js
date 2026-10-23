import { authHeaders, config, noteUnauthorized } from './api.js'

export const ADDON_LABELS = {
  aps_solve: { label: 'Autodesk processing', description: 'Process drawings with Leaf Automation, or your own Autodesk integration.' },
  e2b_exec: { label: 'Hosted execution', description: "Run your tools in Leaf Automation's sandboxes, or bring your own E2B key." },
  marathon: { label: 'Long-running tasks', description: 'Run extended tasks with Leaf Automation, or your own task runner.' },
  forge_hosting: { label: 'Repository hosting', description: "Leaf Automation's Forge, or your own GitHub." },
  aws_resources: { label: 'Cloud resources', description: 'Use cloud resources from Leaf Automation, or your own AWS account.' },
  storage: { label: 'File storage', description: 'Store files with Leaf Automation, or your own storage provider.' },
  canonical_solve: { label: 'Drawing solves', description: 'Solve drawings with Leaf Automation, or your own solver.' },
  tool_ci: { label: 'Tool checks', description: 'Build and check tools with Leaf Automation, or your own CI service.' },
  gpu_inference: { label: 'GPU inference', description: 'Run models with Leaf Automation, or your own GPU provider.' },
  ios_build: { label: 'iOS builds', description: 'Build iOS apps with Leaf Automation, or your own build service.' },
  app_auth: { label: 'App sign-in', description: 'Manage app sign-in with Leaf Automation, or your own authentication provider.' },
}

async function request(orgId, suffix, options = {}) {
  if (!orgId) throw new Error('Choose a workspace to manage add-ons.')
  const path = `/api/orgs/${encodeURIComponent(orgId)}/${suffix}`
  const headers = { ...options.headers, 'X-Org-Id': orgId, ...authHeaders() }
  const response = noteUnauthorized(await fetch(`${config.apiBase}${path}`, { ...options, headers }), path, headers.Authorization)
  if (!response.ok) {
    const error = new Error(`${options.method || 'GET'} ${path} -> ${response.status}`)
    error.status = response.status
    try { error.body = await response.clone().json() } catch { /* Keep status for non-JSON failures. */ }
    const message = error.body?.error?.message || error.body?.message
    if (typeof message === 'string') error.message = message
    throw error
  }
  return response.json()
}

export async function getAddons(orgId) {
  return (await request(orgId, 'addons')).addons
}

export async function setAddonMode(orgId, addon, mode, byoRef) {
  if (!Object.hasOwn(ADDON_LABELS, addon) || !['managed', 'byo', 'off'].includes(mode)) {
    throw new Error('Choose a valid add-on and mode.')
  }
  const body = { mode }
  if (mode === 'byo') {
    if (!byoRef?.trim()) throw new Error('Enter a credential name.')
    body.byo_ref = byoRef.trim()
  }
  return request(orgId, `addons/${encodeURIComponent(addon)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
}

export async function getStatement(orgId, period) {
  return (await request(orgId, `billing/statement?period=${encodeURIComponent(period)}`)).statement
}
