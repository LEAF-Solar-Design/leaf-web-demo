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

export const ADDON_READER_VERSION = 1

export function readAddonCatalog(payload, readerVersion = ADDON_READER_VERSION) {
  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
  const isVersion = (value) => Number.isSafeInteger(value) && value > 0
  if (!isObject(payload)) throw new Error('Invalid add-on catalog.')
  if (Object.hasOwn(payload, 'schema') && payload.schema !== 'leaf.addons.v1') {
    throw new Error('Unsupported add-on catalog schema.')
  }
  const producerVersion = Object.hasOwn(payload, 'reader_version') ? payload.reader_version : 1
  if (!isVersion(readerVersion) || !isVersion(producerVersion)
    || !Object.hasOwn(payload, 'addons') || !isObject(payload.addons)) {
    throw new Error('Invalid add-on catalog.')
  }
  const settings = {}
  for (const addon of Object.keys(ADDON_LABELS)) {
    const entry = Object.hasOwn(payload.addons, addon) ? payload.addons[addon] : null
    const minimum = isObject(entry) && Object.hasOwn(entry, 'min_reader_version') ? entry.min_reader_version : 1
    if (isVersion(minimum) && minimum > readerVersion) continue
    const mode = isObject(entry) && Object.hasOwn(entry, 'mode') ? entry.mode : null
    const ref = isObject(entry) && Object.hasOwn(entry, 'byo_ref') ? entry.byo_ref : null
    if (!isVersion(minimum) || !['managed', 'byo', 'off'].includes(mode)
      || (mode === 'byo' && (typeof ref !== 'string' || !ref.length))) {
      settings[addon] = { mode: 'off', byo_ref: null }
    } else {
      settings[addon] = { mode, byo_ref: mode === 'byo' ? ref : null }
    }
  }
  return settings
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
    const detail = error.body?.detail
    let message = error.body?.error?.message || error.body?.message
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail)) {
      message = detail.map((item) => item?.msg).filter((msg) => typeof msg === 'string').join('; ') || message
    }
    if (typeof message === 'string') error.message = message
    throw error
  }
  return response.json()
}

export async function getAddons(orgId) {
  return readAddonCatalog(await request(orgId, 'addons'))
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
