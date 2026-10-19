export const PROD_HOSTS = Object.freeze(['platform.leafdesign.ai', 'app.leafdesign.ai', 'studio.leafautomation.ai'])

const SHA = /^[0-9a-f]{40}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const INPUTS = Object.freeze(['LEAF_E2E_PROD_BASE_URL', 'LEAF_E2E_PROD_ACCEPTANCE_TOKEN', 'LEAF_E2E_EXPECTED_SHA', 'LEAF_E2E_PROD_PROJECT_ID'])
const invalid = (name) => new Error(`PROD_CONFIG_INVALID ${name}`)

function parseProdUrl(value, base = false) {
  const name = 'LEAF_E2E_PROD_BASE_URL'
  if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') > 4096 || /[\s\x00-\x1f\x7f]/u.test(value)) throw invalid(name)
  let parsed
  try { parsed = new URL(value) } catch { throw invalid(name) }
  if (parsed.protocol !== 'https:' || !PROD_HOSTS.includes(parsed.hostname) || parsed.port || parsed.username || parsed.password || /^https:\/\/[^/?#]*@/iu.test(value)
      || (base && (parsed.pathname !== '/' || parsed.search || parsed.hash || !/^https:\/\/[^/?#]+\/?$/iu.test(value)))) throw invalid(name)
  return parsed
}

export function resolveProdBaseUrl(env = process.env) {
  return parseProdUrl(env.LEAF_E2E_PROD_BASE_URL, true).origin
}

export function assertProdResponse(url) {
  return parseProdUrl(url)
}

// Staging uses this optional helper. Production admission is stricter.
export function resolveExpectedSha(env = process.env) {
  const value = env.LEAF_E2E_EXPECTED_SHA
  if (value === undefined || value === '') return null
  if (typeof value !== 'string' || value.length !== 40 || !SHA.test(value)) throw invalid('LEAF_E2E_EXPECTED_SHA')
  return value
}

export function validateProdConfig(env = process.env) {
  const flag = env.LEAF_E2E_PROD_REQUIRED
  if (flag !== undefined && flag !== '0' && flag !== '1') throw invalid('LEAF_E2E_PROD_REQUIRED')
  const required = flag === '1'
  if (!required && INPUTS.every((name) => env[name] === undefined)) return Object.freeze({ enabled: false, required })
  for (const name of INPUTS) {
    if (typeof env[name] !== 'string' || env[name] === '') throw invalid(name)
  }
  const origin = resolveProdBaseUrl(env)
  const token = env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN
  if (Buffer.byteLength(token, 'utf8') > 16384 || /[\s\x00-\x1f\x7f-\x9f]/u.test(token)) throw invalid('LEAF_E2E_PROD_ACCEPTANCE_TOKEN')
  const expectedSha = resolveExpectedSha(env)
  const projectId = env.LEAF_E2E_PROD_PROJECT_ID
  if (projectId.length !== 36 || !UUID.test(projectId) || projectId === '00000000-0000-0000-0000-000000000000') throw invalid('LEAF_E2E_PROD_PROJECT_ID')
  return Object.freeze({ enabled: true, required, origin, token, expectedSha, projectId })
}

export function requireProdTarget(env = process.env) {
  validateProdConfig(env)
}

export function requireFixtureProject(projects, projectId) {
  if (typeof projectId !== 'string' || projectId.length !== 36 || !UUID.test(projectId) || projectId === '00000000-0000-0000-0000-000000000000') throw new Error('PROD_FIXTURE_ID')
  if (!Array.isArray(projects) || projects.some((project) => !project || typeof project !== 'object' || Array.isArray(project))) throw new Error('PROD_FIXTURE_LIST')
  const matches = projects.filter((project) => project.project_id === projectId)
  if (matches.length !== 1) throw new Error('PROD_FIXTURE_ID')
  const project = matches[0]
  if (typeof project.name !== 'string' || !project.name.trim() || /[\x00-\x1f\x7f]/u.test(project.name)) throw new Error('PROD_FIXTURE_NAME')
  return project
}
