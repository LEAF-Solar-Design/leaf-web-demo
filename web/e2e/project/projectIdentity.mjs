export const REQUIRED_TEST_IDS = Object.freeze([
  'project_browser_create_import', 'project_browser_bearer_required',
  'project_settings_worker_readback',
])
export const CHECKPOINTS = Object.freeze([
  'ui_project_created', 'ui_dxf_imported', 'bearer_present', 'actor_header_absent',
  'bearerless_read_refused', 'bearerless_mutation_refused', 'bearerless_no_mutation',
  'ui_checkout_acquired', 'settings_submitted', 'worker_published',
  'settings_readback', 'settings_reopened',
])
const HEX = /^[a-f0-9]{64}$/
export const publicId = value => typeof value === 'string' && /^[A-Za-z0-9:|_-]{1,128}$/.test(value)
const requireValue = condition => { if (!condition) throw new Error('PP3B_METADATA') }

export function exactOrigin(value) {
  requireValue(typeof value === 'string' && /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value))
  const port = Number(value.slice(value.lastIndexOf(':') + 1))
  requireValue(port <= 65535 && new URL(value).origin === value)
  return value
}

export function validateMetadata(input) {
  requireValue(input?.profile === 'project' && typeof input.run_id === 'string' && /^[a-z0-9][a-z0-9-]{7,63}$/.test(input.run_id))
  const web_origin = exactOrigin(input.web_origin)
  const app_origin = exactOrigin(input.app_origin)
  for (const key of ['organization_id', 'binding_id', 'worker_id']) requireValue(publicId(input[key]))
  requireValue(input.worker_id === `project:${input.run_id}`)
  requireValue(typeof input.public_key_thumbprint === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(input.public_key_thumbprint))
  requireValue(JSON.stringify(input.required_test_ids) === JSON.stringify(REQUIRED_TEST_IDS))
  const source = input.source_binding
  requireValue(source && /^[a-f0-9]{40}$/.test(source.commit))
  for (const key of ['dirty_tree_sha256', 'tested_source_manifest_sha256']) requireValue(HEX.test(source[key]))
  const pathOK = value => typeof value === 'string' && value.length <= 512 &&
    !/[\u0000-\u001f\u007f\\]/.test(value) && value.split('/').every(p => p !== '' && p !== '.' && p !== '..')
  requireValue(Array.isArray(source.files) && source.files.length > 0 && source.files.length <= 20000)
  const files = source.files.map(file => {
    requireValue(pathOK(file.path) && HEX.test(file.sha256) && Number.isSafeInteger(file.size) && file.size >= 0)
    return { path: file.path, sha256: file.sha256, size: file.size }
  })
  requireValue(new Set(files.map(file => file.path)).size === files.length)
  const paths = key => {
    requireValue(Array.isArray(source[key]) && source[key].length <= 20000 && source[key].every(pathOK))
    return [...source[key]]
  }
  return Object.freeze({ profile: 'project', run_id: input.run_id, web_origin, app_origin,
    organization_id: input.organization_id, binding_id: input.binding_id, worker_id: input.worker_id,
    public_key_thumbprint: input.public_key_thumbprint, required_test_ids: [...REQUIRED_TEST_IDS],
    source_binding: { commit: source.commit, dirty_tree_sha256: source.dirty_tree_sha256,
      tested_source_manifest_sha256: source.tested_source_manifest_sha256, files,
      declared_untracked: paths('declared_untracked'), expected_migrations: paths('expected_migrations') } })
}

export function metadataFromEnvironment(env) {
  try {
    requireValue(typeof env.LEAF_PROJECT_REQUEST === 'string' && env.LEAF_PROJECT_REQUEST.length <= 4 * 1024 * 1024)
    const metadata = validateMetadata(JSON.parse(env.LEAF_PROJECT_REQUEST))
    requireValue(env.LEAF_E2E_MANAGED === '1' && env.LEAF_PROOF_PROJECT_RUN_ID === metadata.run_id)
    return metadata
  } catch { throw new Error('PP3B_METADATA') }
}

// Serialized by Playwright. No closure, network operation or application override.
export function installIdentity({ origin, bearer, organizationId }, target = window) {
  if (target.top !== target || target.location.origin !== origin) return
  if (target.sessionStorage.getItem('leaf.pp3b.identity-installed') !== null) return
  target.sessionStorage.setItem('leaf.pp3b.identity-installed', '1')
  target.localStorage.setItem('leaf.jwt', bearer)
  target.localStorage.setItem('leaf.org_id', organizationId)
}

export function removeIdentityBearer({ origin }, target = window) {
  if (target.top !== target || target.location.origin !== origin) throw new Error('PP3B_ORIGIN')
  target.sessionStorage.setItem('leaf.pp3b.identity-installed', 'removed')
  target.localStorage.removeItem('leaf.jwt')
}

export function clearIdentity({ origin }, target = window) {
  if (target.top !== target || target.location.origin !== origin) return
  target.sessionStorage.setItem('leaf.pp3b.identity-installed', 'removed')
  target.localStorage.clear()
  target.sessionStorage.clear()
  // No further document is initialized: the owning context is immediately closed.
}

export async function createProjectIdentity(browser, input, bearer) {
  const metadata = validateMetadata(input)
  requireValue(typeof bearer === 'string' && bearer.length <= 16384 && /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(bearer))
  const context = await browser.newContext({ baseURL: metadata.web_origin, serviceWorkers: 'block' })
  try {
    const page = await context.newPage()
    await page.addInitScript(installIdentity, { origin: metadata.web_origin, bearer, organizationId: metadata.organization_id })
    return { context, page, metadata }
  } catch {
    await context.close()
    throw new Error('PP3B_IDENTITY')
  }
}

export async function removeBearer(identity) {
  await identity.page.evaluate(removeIdentityBearer, { origin: identity.metadata.web_origin })
}

export async function disposeIdentity(identity) {
  let failed = false
  try {
    for (const page of identity.context.pages()) {
      try { await page.evaluate(clearIdentity, { origin: identity.metadata.web_origin }) } catch { failed = true }
    }
    await identity.context.clearCookies()
  } catch { failed = true }
  finally { await identity.context.close() }
  if (failed) throw new Error('PP3B_TEARDOWN')
}
