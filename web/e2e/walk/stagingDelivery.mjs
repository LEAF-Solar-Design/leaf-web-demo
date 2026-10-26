import { execFile } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolveExpectedSha } from '../prod/prodConfig.mjs'
import { assertAllowedStagingHost, StagingHostError } from '../staging/stagingConfig.mjs'

const sha = value => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value) ? value : null
export const deliveryRepo = fileURLToPath(new URL('../../../', import.meta.url))

// This adapter deliberately has no browser/request-context or credential inputs.
export async function observeDeliveryHealth(origin, path, { fetchImpl = globalThis.fetch, timeoutMs = 5000, env = process.env } = {}) {
  if (assertAllowedStagingHost(origin, env).origin !== origin || !['/api/health', '/health.json'].includes(path)) {
    throw new StagingHostError('delivery target invalid')
  }
  const empty = { source_sha: null, observation: 'unavailable', http_status: null }
  const controller = new AbortController()
  let timer, reader
  const work = async () => {
    const response = await fetchImpl(`${origin}${path}`, { method: 'GET', credentials: 'omit',
      redirect: 'error', cache: 'no-store', signal: controller.signal })
    let resolved
    try { resolved = new URL(response.url) } catch { throw new StagingHostError('delivery response origin invalid') }
    if (resolved.origin !== origin || resolved.username || resolved.password) throw new StagingHostError('delivery response origin mismatch')
    empty.http_status = Number.isInteger(response.status) && response.status >= 100 && response.status <= 599 ? response.status : null
    if (empty.http_status === null || response.status < 200 || response.status >= 300 || response.redirected) return empty
    reader = response.body?.getReader()
    if (!reader) return empty
    const chunks = []
    let size = 0
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 4096) return empty
      chunks.push(Buffer.from(value))
    }
    let body
    try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { return empty }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return empty
    const source = sha(body.source_sha)
    const result = { source_sha: source, observation: source ? 'ok' : 'invalid', http_status: empty.http_status }
    if (path === '/api/health') {
      const release = sha(body.release_source_sha)
      const identity = ['built', 'adopted', 'build_unknown', 'unattested'].includes(body.source_identity) ? body.source_identity : 'unknown'
      result.release_source_sha = release
      result.source_identity = identity
      const legacy = !Object.hasOwn(body, 'release_source_sha') && !Object.hasOwn(body, 'source_identity')
      const consistent = identity === 'built' && release !== null && release === source
        || identity === 'unattested' && body.release_source_sha === null
      if (!legacy && (!consistent || (body.release_source_sha != null && release !== source))) result.observation = 'identity_unverified'
    }
    return result
  }
  try {
    return await Promise.race([work(), new Promise(resolve => {
      timer = setTimeout(() => { controller.abort(); resolve({ ...empty }) }, Math.min(timeoutMs, 5000))
    })])
  } catch (error) {
    if (error instanceof StagingHostError) throw error
    return empty
  } finally {
    clearTimeout(timer)
    controller.abort()
    if (reader) void reader.cancel().catch(() => {})
  }
}

function runLocalGit(args, options) {
  return new Promise(resolve => {
    execFile('git', args, options, (error, stdout) => resolve({ code: error ? error.code : 0, stdout }))
  })
}

export function createLocalAncestry({ run = runLocalGit, now = () => performance.now() } = {}) {
  let deadline
  return async (deployed, expected) => {
    if (!sha(deployed) || !sha(expected)) return 'unknown'
    if (deployed === expected) return 'equal'
    deadline ??= now() + 2000
    const git = async args => {
      const remaining = deadline - now()
      if (remaining <= 0) return { code: 'timeout' }
      try {
        const result = await run(args, { cwd: deliveryRepo, shell: false, timeout: remaining, maxBuffer: 4096,
          env: { ...process.env, GIT_NO_LAZY_FETCH: '1', GIT_NO_REPLACE_OBJECTS: '1' } })
        return now() >= deadline ? { code: 'timeout' } : result
      } catch { return { code: 'unavailable' } }
    }
    for (const commit of [deployed, expected]) {
      if ((await git(['cat-file', '-e', `${commit}^{commit}`])).code !== 0) return 'unknown'
    }
    const result = await git(['merge-base', '--is-ancestor', deployed, expected])
    if (result.code === 0) return 'ancestor'
    if (result.code !== 1) return 'unknown'
    const shallow = await git(['rev-parse', '--is-shallow-repository'])
    return shallow.code === 0 && shallow.stdout?.trim() === 'false' ? 'not_ancestor' : 'unknown'
  }
}

export function decideStagingDelivery(app, web) {
  if (app.observation === 'identity_unverified') return 'unverified'
  const relations = [app.relation, web.relation]
  if (relations.every(value => value === 'equal')) return 'current'
  if (relations.every(value => ['equal', 'ancestor'].includes(value))) return 'stale'
  if (relations.includes('not_ancestor')) return 'mismatch'
  return 'unverified'
}

export async function stagingDelivery(baseURL, { env = process.env, fetchImpl, ancestry, now = () => new Date(), timeoutMs } = {}) {
  const expected = resolveExpectedSha(env)
  const evidence = { schema: 'leaf.staging-delivery.v1', checkedAt: now().toISOString(), expected_sha: expected,
    app: { source_sha: null, release_source_sha: null, source_identity: 'unknown', observation: 'not_configured', http_status: null, relation: 'unknown' },
    web: { source_sha: null, observation: 'not_configured', http_status: null, relation: 'unknown' }, decision: 'not_configured' }
  if (!expected) return evidence
  const origin = assertAllowedStagingHost(baseURL, env).origin
  // URL credentials are never forwarded, even on an otherwise allowed origin.
  const target = new URL(baseURL)
  if (target.username || target.password) throw new StagingHostError('delivery target credentials forbidden')
  Object.assign(evidence.app, await observeDeliveryHealth(origin, '/api/health', { fetchImpl, timeoutMs, env }))
  Object.assign(evidence.web, await observeDeliveryHealth(origin, '/health.json', { fetchImpl, timeoutMs, env }))
  const compare = ancestry ?? createLocalAncestry()
  for (const component of [evidence.app, evidence.web]) {
    if (component.observation !== 'ok') continue
    const relation = component.source_sha === expected ? 'equal' : await compare(component.source_sha, expected)
    component.relation = ['equal', 'ancestor', 'not_ancestor', 'unknown'].includes(relation) ? relation : 'unknown'
  }
  evidence.decision = decideStagingDelivery(evidence.app, evidence.web)
  return evidence
}
