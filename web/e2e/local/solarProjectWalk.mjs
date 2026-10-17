import { createHash } from 'node:crypto'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const H_TITLE = 'R13-H Solar inside an open project preserves the manual electrical boundary'
export const H_FLAGS = Object.freeze(['VITE_CAD_EDIT', 'VITE_SOLAR_SETTINGS_FORM', 'VITE_SOLAR_FLOW_RAIL',
  'LEAF_SOLAR_PROJECT_RUN_ENABLED', 'LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED'])
export const UNKNOWN_ELECTRICAL = Object.freeze(['EQUIPMENT_POWER_UNKNOWN', 'EQUIPMENT_VOLTAGE_UNKNOWN'])
export const EQUIPMENT = Object.freeze({
  id: 'leaf:inverter:00000000-0000-4000-8000-000000000001', number: 1, type_key: 'INV', model: 'fixture',
  position: [5, 0, 0], rotation: 0, scale: [1, 1, 1], block_name: 'FixtureInverter', layer: '0',
  mppt_count: 1, total_dc_inputs: 1, mppt_inputs: { A: 1 }, max_dc_voltage: 1500,
  max_ac_power_kw: 100, max_dc_power_kw: 100, is_solaredge: false,
})
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$(?![\s\S])/i
const HEX = /^[0-9a-f]{64}$(?![\s\S])/i
const DIGEST = /^sha256:[0-9a-f]{64}$(?![\s\S])/i
const TOOL = /^solar-[a-z0-9-]{1,100}$(?![\s\S])/
const CODE = /^[A-Z][A-Z0-9_]{0,127}$(?![\s\S])/
const PRIVATE = /lco1\.[0-9a-f]{64}|\bBearer\s+\S+|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/i
const plain = value => value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
export const demand = (condition, diagnostic = 'contract mismatch') => {
  if (!condition) throw new Error(`R13-H ${diagnostic}`)
}
export const isUuid = value => typeof value === 'string' && UUID.test(value)
const hash = value => createHash('sha256').update(value).digest('hex')
function stable(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value)
  if (typeof value === 'number') { demand(Number.isFinite(value)); return JSON.stringify(value) }
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  demand(plain(value), 'JSON shape mismatch')
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
}
export const sameJson = (left, right) => stable(left) === stable(right)
export const previewHasParams = (preview, params) => preview.paramsHash === hash(stable(params))
export function assertPublic(value, depth = 0) {
  demand(depth < 48, 'evidence depth refused')
  if (typeof value === 'string') { demand(!PRIVATE.test(value), 'private evidence refused'); return }
  if (value === null || typeof value === 'boolean') return
  if (typeof value === 'number') { demand(Number.isFinite(value), 'evidence number refused'); return }
  demand(Array.isArray(value) || plain(value), 'evidence shape refused')
  for (const [key, child] of Object.entries(value)) {
    demand(!/authorization|checkout.?capability|bearer|access.?token|^headers$|^params$|^raw$/i.test(key), 'private evidence field refused')
    assertPublic(child, depth + 1)
  }
}
export function loopbackBase(value) {
  let url
  try { url = new URL(value) } catch { throw new Error('R13-H local URL required') }
  demand(['http:', 'https:'].includes(url.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
    && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash, 'local URL required')
  return url.origin
}
export function executionPolicy(env) {
  const required = env.LEAF_E2E_R13_H_REQUIRED === '1'
  const flags = H_FLAGS.every(key => env[key] === '1')
  if (!flags && !required) return { enabled: false, reason: 'R13-H Solar flags absent; no acceptance receipt emitted.' }
  demand(flags, 'required Solar flags absent')
  demand(env.LEAF_E2E_MANAGED === '1', 'managed stack required')
  const postures = ['LEAF_RUNTIME_ENV', 'LEAF_ENV'].map(key => (env[key] || '').trim().toLowerCase()).filter(Boolean)
  demand(postures.length > 0 && postures.every(value => ['local', 'test', 'development', 'dev'].includes(value))
    && env.LEAF_AUTH_LIVE === '1', 'verified local test posture required')
  demand(typeof env.LEAF_SOURCE_COMMIT === 'string' && /^[0-9a-f]{40}$(?![\s\S])/i.test(env.LEAF_SOURCE_COMMIT), 'full source SHA required')
  return { enabled: true, sourceCommit: env.LEAF_SOURCE_COMMIT }
}
export function projectBinding(value) {
  demand(plain(value) && ['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every(key => isUuid(value[key])), 'canonical UUID binding required')
  return Object.freeze(Object.fromEntries(['organization_id', 'project_id', 'drawing_id', 'input_version_id'].map(key => [key, value[key]])))
}
function headerProof(headers) {
  return { organization_id: headers['x-org-id'], project_id: headers['x-project-id'],
    authenticated: typeof headers.authorization === 'string' && /^Bearer \S+$/i.test(headers.authorization),
    keyPresent: typeof headers['idempotency-key'] === 'string' && headers['idempotency-key'].trim().length > 0,
    checkoutPresent: typeof headers['x-checkout-capability'] === 'string' && /^lco1\.[0-9a-f]{64}$/.test(headers['x-checkout-capability']),
    actorHeaderAbsent: !Object.hasOwn(headers, 'x-actor-binding-id') }
}
function requestParts(request, apiBase, method, path) {
  const url = new URL(request.url())
  demand(url.origin === loopbackBase(apiBase) && url.pathname === path && request.method() === method, 'endpoint mismatch')
  return { url, headers: request.headers() }
}
function signature(binding, tool, digest, params, key) {
  demand(TOOL.test(tool) && DIGEST.test(digest) && plain(params) && params.drawing_id === binding.drawing_id
    && typeof key === 'string' && key.trim().length > 0 && key.length <= 256, 'intent mismatch')
  return hash(stable([binding, tool, digest, params, hash(key)]))
}
export function inspectRun(request, apiBase, expected, tool, checkParams = () => {}) {
  const binding = projectBinding(expected)
  const { headers } = requestParts(request, apiBase, 'POST', '/api/run')
  const body = request.postDataJSON()
  const proof = headerProof(headers)
  demand(plain(body) && body.tool === tool && body.dwg === binding.input_version_id && plain(body.params)
    && body.params.drawing_id === binding.drawing_id && proof.organization_id === binding.organization_id
    && proof.project_id === binding.project_id && proof.authenticated && proof.keyPresent && proof.checkoutPresent
    && proof.actorHeaderAbsent && !['org_id', 'project_id', 'input_version_id', 'dwg_version'].some(key => Object.hasOwn(body, key)), 'run binding mismatch')
  checkParams(body.params)
  return { signature: signature(binding, tool, body.catalog_digest, body.params, headers['idempotency-key']),
    catalogDigest: body.catalog_digest, authenticated: true, keyPresent: true, checkoutPresent: true, actorHeaderAbsent: true }
}
export function inspectPreview(request, body, status, apiBase) {
  const { url, headers } = requestParts(request, apiBase, 'GET', '/api/capabilities')
  if (!url.searchParams.has('project_runs')) return []
  const binding = projectBinding({ organization_id: headers['x-org-id'], project_id: url.searchParams.get('project_id'),
    drawing_id: url.searchParams.get('drawing_id'), input_version_id: url.searchParams.get('input_version_id') })
  demand(headers['x-project-id'] === binding.project_id && headerProof(headers).authenticated, 'preview authority mismatch')
  let intents
  try { intents = JSON.parse(url.searchParams.get('project_runs')) } catch { throw new Error('R13-H preview JSON invalid') }
  demand(status === 200 && plain(intents) && plain(body) && Array.isArray(body.families), 'preview response mismatch')
  return Object.entries(intents).map(([tool, intent]) => {
    const rows = body.families.flatMap(family => { demand(Array.isArray(family.capabilities), 'preview family mismatch'); return family.capabilities }).filter(row => row.name === tool)
    demand(rows.length > 0 && rows.every(row => row.catalog_digest === intent.catalog_digest), 'preview tool mismatch')
    const codes = new Set(), genericReasons = new Set()
    const readiness = rows.map(row => {
      const a = row.availability
      demand(plain(a) && a.admission_checked === true && plain(a.admission) && Array.isArray(a.refusal_reasons), 'preview admission missing')
      const code = a.admission.error?.reason_code
      if (typeof code === 'string' && CODE.test(code)) codes.add(code)
      for (const reason of a.refusal_reasons) {
        demand(typeof reason === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(reason), 'preview refusal shape mismatch')
        genericReasons.add(reason)
      }
      return a.admission.status_code === 200 && a.admission.error === null && a.refusal_reasons.length === 0
        && ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].every(key => a[key] === true)
    })
    return { tool, binding, signature: signature(binding, tool, intent.catalog_digest, intent.params, intent.idempotency_key),
      ready: readiness.every(Boolean), codes: [...codes].sort(), admissionStatus: rows.map(row => row.availability.admission.status_code),
      genericReasons: [...genericReasons].sort(), paramsHash: hash(stable(intent.params)), catalogDigest: intent.catalog_digest }
  })
}
export function inspectContext(body, expected, { graphRequired = true } = {}) {
  const binding = projectBinding(expected)
  demand(plain(body) && plain(body.context) && plain(body.intake), 'canonical context shape mismatch')
  const context = body.context
  demand(Object.keys(binding).every(key => context[key] === binding[key]) && context.head_version_id === binding.input_version_id
    && context.is_head === true && HEX.test(context.intake_sha256), 'canonical context binding mismatch')
  const graph = body.intake.solar_design_graph
  const digest = body.intake.solar_design_graph_sha256
  demand((graph === undefined) === (digest === undefined), 'canonical graph pair mismatch')
  demand(!graphRequired || graph !== undefined, 'canonical graph required')
  if (graph !== undefined) demand(plain(graph) && HEX.test(digest) && Number.isSafeInteger(graph.rev) && graph.rev >= 0
    && ['panels', 'strings', 'inverters', 'routes', 'schedules'].every(key => Array.isArray(graph[key])), 'canonical graph shape mismatch')
  return { binding, head: context.head_version_id, intakeSha256: context.intake_sha256, graphSha256: digest ?? null, graph: graph ?? null }
}
export function inspectJob(body, expected, jobId, tool) {
  const binding = projectBinding(expected)
  // The browser-facing server route returns _record_body directly. The
  // platform's separate {job} API is not this endpoint's wire contract.
  demand(plain(body) && !Object.hasOwn(body, 'job'), 'job response shape mismatch')
  const job = body
  demand(isUuid(jobId) && job.job_id === jobId && job.org_id === binding.organization_id && job.project_id === binding.project_id
    && job.input_version_id === binding.input_version_id && job.tool_name === tool
    && job.execution_context?.schema === 'leaf.solar-project-graph-job.v1'
    && job.execution_context.drawing_id === binding.drawing_id, 'job binding mismatch')
  demand(['submitted', 'running', 'complete'].includes(job.status), 'job did not publish')
  if (job.status !== 'complete') return null
  const output = job.result?.output_version_id
  const commit = job.result?.graph_commit
  demand(isUuid(output) && output !== binding.input_version_id && (!Object.hasOwn(job, 'output_version_id') || job.output_version_id === output)
    && (!Object.hasOwn(job.result, 'graph_commit') || (plain(commit) && (!Object.hasOwn(commit, 'output_version_id') || commit.output_version_id === output)))
    && (!Object.hasOwn(job, 'error') || job.error === null), 'job publication mismatch')
  return output
}
export function assertManualGraph(graph) {
  const evidence = graph?.settings?.extra?.string_sizing
  const voltage = graph?.settings?.voc_cold
  demand(graph?.settings?.panels_in_sequence === 1 && graph.settings.global_string_sizing_confirmed === true
    && evidence?.mode === 'manual-global' && evidence.panels_in_sequence === 1 && HEX.test(evidence.basis_sha256)
    && plain(evidence.records) && Object.keys(evidence.records).length === 0 && plain(voltage)
    && voltage.override_accepted === false && ['passes', 'suggested_string_length', 'per_module', 'string_voltage', 'max_dc_voltage'].every(key => voltage[key] === null), 'manual electrical boundary mismatch')
}
export function assertUnknownElectrical(entity) {
  demand(entity?.validity?.state === 'invalid' && Array.isArray(entity.validity.reasons)
    && stable([...entity.validity.reasons].sort()) === stable([...UNKNOWN_ELECTRICAL].sort()), 'electrical uncertainty mismatch')
}
export function assertRefusalEvidence({ before, after, code, expectedCode, runCountBefore, runCountAfter }) {
  demand(CODE.test(expectedCode) && code === expectedCode && runCountBefore === runCountAfter && before.head === after.head
    && before.intakeSha256 === after.intakeSha256 && before.graphSha256 === after.graphSha256
    && before.graph.rev === after.graph.rev && after.graph.routes.length === 0 && after.graph.schedules.length === 0, 'refusal changed canonical state')
}
export function publicEvidence(input) {
  const binding = projectBinding(input.binding)
  demand(typeof input.fence === 'string' && /^[0-9]+$/.test(input.fence) && typeof input.gauge === 'string' && input.gauge.trim(), 'public evidence incomplete')
  const stepKeys = ['tool', 'job_id', 'input_version_id', 'output_version_id', 'before_rev', 'after_rev', 'intake_sha256', 'graph_sha256', 'admission_status', 'run_status']
  const refusalKeys = ['tool', 'code', 'before_head', 'after_head', 'run_count_before', 'run_count_after']
  const result = { verdict: 'pass', ...binding, imported_version_id: input.importedVersionId, checkout_fence: input.fence,
    selected_gauge: input.gauge, steps: input.steps.map(step => Object.fromEntries(stepKeys.map(key => [key, step[key]]))),
    refusals: input.refusals.map(row => Object.fromEntries(refusalKeys.map(key => [key, row[key]]))),
    authenticated_import: input.authenticatedImport === true, actor_header_absent: input.actorHeaderAbsent === true }
  demand(isUuid(result.imported_version_id) && result.authenticated_import && result.actor_header_absent
    && result.steps.length === 6 && result.refusals.length === 2
    && result.steps.every(step => TOOL.test(step.tool) && ['job_id', 'input_version_id', 'output_version_id'].every(key => isUuid(step[key]))
      && (step.before_rev === null || Number.isSafeInteger(step.before_rev)) && Number.isSafeInteger(step.after_rev)
      && HEX.test(step.intake_sha256) && HEX.test(step.graph_sha256) && step.admission_status === 200 && step.run_status === 202)
    && result.refusals.every(row => TOOL.test(row.tool) && CODE.test(row.code) && isUuid(row.before_head)
      && row.before_head === row.after_head && Number.isSafeInteger(row.run_count_before) && row.run_count_before === row.run_count_after), 'public evidence incomplete')
  assertPublic(result)
  return result
}

// All response work is tracked before it can reject. drain() is the error boundary;
// dispose() removes both listeners even when observation or a browser assertion fails.
export function observeProjectTraffic(page, apiBase) {
  const origin = loopbackBase(apiBase)
  const endpoints = new Set(), previews = [], organizations = [], readyUploads = []
  const pending = new Set()
  let failure = null, disposed = false, runCount = 0
  const onRequest = request => {
    try { const url = new URL(request.url()); if (url.origin === origin && url.pathname === '/api/run' && request.method() === 'POST') runCount += 1 }
    catch { failure = new Error('R13-H request observation failed') }
  }
  const onResponse = response => {
    if (disposed) return
    const work = (async () => {
      const request = response.request(), url = new URL(response.url())
      if (url.origin !== origin) return
      // Query strings can contain project intent material; evidence stores paths only.
      endpoints.add(`${request.method()} ${url.pathname} ${response.status()}`)
      if (request.method() === 'GET' && url.pathname === '/api/capabilities' && url.searchParams.has('project_runs')) {
        previews.push(...inspectPreview(request, await response.json(), response.status(), origin))
      } else if (request.method() === 'GET' && url.pathname === '/api/projects' && response.status() === 200) {
        const body = await response.json(), proof = headerProof(request.headers())
        demand(Array.isArray(body.projects) && isUuid(proof.organization_id) && proof.authenticated, 'workspace authority unreadable')
        organizations.push({ organization_id: proof.organization_id, authenticated: true })
      } else if (request.method() === 'GET' && /^\/api\/drawings\/[0-9a-f-]{36}\/upload-status$/.test(url.pathname) && response.status() === 200) {
        const body = await response.json()
        if (body.status === 'ready') {
          demand(isUuid(body.drawing_id) && Number.isSafeInteger(body.extracted_version) && body.extracted_version >= 1
            && url.pathname === `/api/drawings/${body.drawing_id}/upload-status`, 'upload ready identity mismatch')
          readyUploads.push({ drawing_id: body.drawing_id, version: body.extracted_version })
        }
      }
    })().catch(() => { failure = new Error('R13-H response observation failed') })
    pending.add(work)
    work.finally(() => pending.delete(work))
  }
  page.on('request', onRequest)
  page.on('response', onResponse)
  return {
    endpoints, previews, organizations, readyUploads,
    get runCount() { return runCount },
    assertHealthy() { if (failure) throw failure },
    async drain() { while (pending.size) await Promise.all([...pending]); if (failure) throw failure },
    dispose() { disposed = true; page.off('request', onRequest); page.off('response', onResponse) },
  }
}
export async function actionResponse(page, apiBase, method, path, action, timeout = 30000) {
  const origin = loopbackBase(apiBase)
  const response = page.waitForResponse(r => {
    const url = new URL(r.url())
    return url.origin === origin && url.pathname === path && r.request().method() === method
  }, { timeout })
  try { return (await Promise.all([response, Promise.resolve().then(action)]))[0] }
  catch { throw new Error('R13-H expected action response missing') }
}
export async function safeJson(response) {
  try { return await response.json() } catch { throw new Error('R13-H response JSON unreadable') }
}
export async function browserRead(page, apiBase, path, binding) {
  const origin = loopbackBase(apiBase)
  demand(path.startsWith('/api/') && !path.startsWith('//'), 'read endpoint refused')
  let result
  try {
    result = await page.evaluate(async ({ origin, path, organizationId, projectId }) => {
      const bearer = localStorage.getItem('leaf.jwt')
      if (!bearer) return { status: 0, body: null }
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), 15000)
      try {
        const response = await fetch(`${origin}${path}`, { method: 'GET', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { Authorization: `Bearer ${bearer}`, 'X-Org-Id': organizationId, 'X-Project-Id': projectId, 'X-Tenant-Id': organizationId } })
        return { status: response.status, body: await response.json() }
      } finally { clearTimeout(timer) }
    }, { origin, path, organizationId: binding.organization_id, projectId: binding.project_id })
  } catch { throw new Error('R13-H canonical read failed') }
  demand(result.status === 200, 'canonical read refused')
  return result.body
}
export async function readContext(page, apiBase, binding, options) {
  const b = projectBinding(binding)
  return inspectContext(await browserRead(page, apiBase,
    `/api/projects/${b.project_id}/drawing-versions/${b.input_version_id}/context?drawing_id=${b.drawing_id}`, b), b, options)
}
export async function runProjectTool({ page, expect, apiBase, traffic, binding, before, tool, action, checkParams }) {
  const response = await actionResponse(page, apiBase, 'POST', '/api/run', action)
  const intent = inspectRun(response.request(), apiBase, binding, tool, params => {
    demand(params.expected_rev === (before.graph?.rev ?? 0), 'run graph revision mismatch')
    checkParams?.(params)
  })
  demand(response.status() === 202, 'run not admitted')
  const accepted = await safeJson(response)
  demand(accepted.status === 'submitted' && isUuid(accepted.job_id), 'accepted job identity missing')
  await traffic.drain()
  const previews = traffic.previews.filter(row => row.signature === intent.signature)
  demand(previews.length > 0 && previews.at(-1).ready && previews.at(-1).codes.length === 0, 'exact intent preview missing')
  let output = null
  await expect.poll(async () => {
    const body = await browserRead(page, apiBase, `/api/jobs/${accepted.job_id}`, binding)
    output = inspectJob(body, binding, accepted.job_id, tool)
    return output !== null
  }, { timeout: 60000, intervals: [100, 250, 500, 1000], message: 'R13-H captured job must publish' }).toBe(true)
  const next = projectBinding({ ...binding, input_version_id: output })
  const after = await readContext(page, apiBase, next)
  demand(!before.graph || after.graph.rev === before.graph.rev + 1, 'graph revision did not advance once')
  const step = { tool, job_id: accepted.job_id, input_version_id: binding.input_version_id, output_version_id: output,
    before_rev: before.graph?.rev ?? null, after_rev: after.graph.rev, intake_sha256: after.intakeSha256,
    graph_sha256: after.graphSha256, admission_status: 200, run_status: 202 }
  return { binding: next, context: after, step }
}

// Importing this helper into Playwright registers no Node tests and starts no process.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { default: test } = await import('node:test')
  const { default: assert } = await import('node:assert/strict')
  const { EventEmitter } = await import('node:events')
  const { spawnSync } = await import('node:child_process')
  const api = 'http://127.0.0.1:8230'
  const ids = ['00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002',
    '00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000004', '00000000-0000-4000-8000-000000000005']
  const binding = { organization_id: ids[0], project_id: ids[1], drawing_id: ids[2], input_version_id: ids[3] }
  const digest = `sha256:${'a'.repeat(64)}`
  const params = { drawing_id: ids[2], mode: 'manual-global', expected_rev: 2 }
  const headers = { 'x-org-id': ids[0], 'x-project-id': ids[1], authorization: 'Bearer test',
    'idempotency-key': 'intent-1', 'x-checkout-capability': `lco1.${'b'.repeat(64)}` }
  const request = (body = { tool: 'solar-size-strings', params, dwg: ids[3], catalog_digest: digest }, change = {}) => ({
    url: () => change.url || `${api}/api/run`, method: () => change.method || 'POST',
    headers: () => ({ ...headers, ...change.headers }), postDataJSON: () => body,
  })
  const graph = () => ({ rev: 3, panels: [], strings: [], inverters: [], routes: [], schedules: [], settings: {
    panels_in_sequence: 1, global_string_sizing_confirmed: true, extra: { string_sizing: {
      mode: 'manual-global', basis_sha256: 'b'.repeat(64), panels_in_sequence: 1, records: {} } },
    voc_cold: { passes: null, override_accepted: false, suggested_string_length: null, per_module: null, string_voltage: null, max_dc_voltage: null },
  } })
  const context = () => ({ context: { ...binding, head_version_id: ids[3], is_head: true, intake_sha256: 'c'.repeat(64) },
    intake: { solar_design_graph: graph(), solar_design_graph_sha256: 'd'.repeat(64) } })
  const job = () => ({ job_id: ids[4], tenant_id: ids[0], org_id: ids[0], project_id: ids[1], input_version_id: ids[3],
    tool_name: 'solar-size-strings', execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: ids[2] },
    status: 'complete', error: null, degraded_mode: false,
    result: { output_version_id: ids[0], graph_commit: { output_version_id: ids[0] } }, output_version_id: ids[0] })
  test('local URL boundary refuses remote origins and URL credentials', () => {
    assert.equal(loopbackBase(api), api)
    for (const url of ['https://example.com', 'http://user:secret@localhost', 'file:///x', `${api}/api`, `${api}/?x=1`]) assert.throws(() => loopbackBase(url))
  })
  test('required execution refuses missing flags while collection compatibility annotates', () => {
    assert.equal(executionPolicy({}).enabled, false)
    assert.throws(() => executionPolicy({ LEAF_E2E_R13_H_REQUIRED: '1' }))
  })
  test('binding rejects integer versions and UUID trailing data', () => {
    assert.deepEqual(projectBinding(binding), binding)
    assert.throws(() => projectBinding({ ...binding, input_version_id: 1 }))
    assert.throws(() => projectBinding({ ...binding, drawing_id: `${ids[2]}\n` }))
  })
  test('run matches current UUID, public authority and exact tool', () => {
    assert.equal(inspectRun(request(), api, binding, 'solar-size-strings').authenticated, true)
    assert.throws(() => inspectRun(request(), api, binding, 'solar-settings'))
  })
  test('run rejects wrong project, stale version, drawing and missing private authority', () => {
    for (const change of [{ headers: { 'x-project-id': ids[0] } }, { headers: { authorization: '' } },
      { headers: { 'x-checkout-capability': '' } }, { headers: { 'idempotency-key': '' } }]) assert.throws(() => inspectRun(request(undefined, change), api, binding, 'solar-size-strings'))
    for (const body of [{ ...request().postDataJSON(), dwg: ids[0] }, { ...request().postDataJSON(), params: { ...params, drawing_id: ids[0] } }]) assert.throws(() => inspectRun(request(body), api, binding, 'solar-size-strings'))
  })
  test('run refuses same path on another origin or method', () => {
    assert.throws(() => inspectRun(request(undefined, { url: 'http://localhost:9000/api/run' }), api, binding, 'solar-size-strings'))
    assert.throws(() => inspectRun(request(undefined, { method: 'GET' }), api, binding, 'solar-size-strings'))
  })
  test('canonical context binds graph to observed head and refuses wrong scope', () => {
    assert.equal(inspectContext(context(), binding).graph.rev, 3)
    for (const field of Object.keys(binding)) { const body = context(); body.context[field] = ids[4]; assert.throws(() => inspectContext(body, binding)) }
    const stale = context(); stale.context.is_head = false; assert.throws(() => inspectContext(stale, binding))
  })
  test('canonical graph pair is mandatory and graphless allowed only before initialization', () => {
    const body = context(); body.intake = {}
    assert.throws(() => inspectContext(body, binding))
    assert.equal(inspectContext(body, binding, { graphRequired: false }).graph, null)
    body.intake.solar_design_graph = graph(); assert.throws(() => inspectContext(body, binding, { graphRequired: false }))
  })
  test('flat server job publication binds exact job, input, drawing and output agreement', () => {
    assert.equal(inspectJob(job(), binding, ids[4], 'solar-size-strings'), ids[0])
    for (const [field, wrong] of [['job_id', ids[1]], ['org_id', ids[1]], ['project_id', ids[0]],
      ['input_version_id', ids[0]], ['tool_name', 'solar-settings']]) {
      const body = job(); body[field] = wrong
      assert.throws(() => inspectJob(body, binding, ids[4], 'solar-size-strings'))
    }
    const body = job(); body.execution_context.drawing_id = ids[4]; assert.throws(() => inspectJob(body, binding, ids[4], 'solar-size-strings'))
    const conflict = job(); conflict.output_version_id = ids[1]; assert.throws(() => inspectJob(conflict, binding, ids[4], 'solar-size-strings'))
    const commitConflict = job(); commitConflict.result.graph_commit.output_version_id = ids[1]
    assert.throws(() => inspectJob(commitConflict, binding, ids[4], 'solar-size-strings'))
    const staleOutput = job(); staleOutput.result.output_version_id = ids[3]; staleOutput.output_version_id = ids[3]
    assert.throws(() => inspectJob(staleOutput, binding, ids[4], 'solar-size-strings'))
  })
  test('job endpoint refuses unsupported wrapped and conflicting hybrid shapes', () => {
    assert.throws(() => inspectJob({ job: job() }, binding, ids[4], 'solar-size-strings'))
    assert.throws(() => inspectJob({ ...job(), job: { ...job(), project_id: ids[0] } }, binding, ids[4], 'solar-size-strings'))
  })
  test('pending job cannot masquerade as publication and failed job refuses', () => {
    const body = { ...job(), status: 'running', result: null, output_version_id: null }
    assert.equal(inspectJob(body, binding, ids[4], 'solar-size-strings'), null)
    body.status = 'submitted'; assert.equal(inspectJob(body, binding, ids[4], 'solar-size-strings'), null)
    body.status = 'failed'; assert.throws(() => inspectJob(body, binding, ids[4], 'solar-size-strings'))
  })
  test('manual graph boundary refuses cloud records and computed voltage', () => {
    assertManualGraph(graph())
    const cloud = graph(); cloud.settings.extra.string_sizing.records.cloud = {}; assert.throws(() => assertManualGraph(cloud))
    const computed = graph(); computed.settings.voc_cold.string_voltage = 100; assert.throws(() => assertManualGraph(computed))
  })
  test('electrical uncertainty requires both invalid reasons independent of order', () => {
    assertUnknownElectrical({ validity: { state: 'invalid', reasons: [...UNKNOWN_ELECTRICAL].reverse() } })
    assert.throws(() => assertUnknownElectrical({ validity: { state: 'valid', reasons: [] } }))
  })
  test('refusal requires no POST and unchanged canonical head/content', () => {
    const before = inspectContext(context(), binding)
    const evidence = { before, after: before, code: 'ROUTING_TOPOLOGY_REQUIRED', expectedCode: 'ROUTING_TOPOLOGY_REQUIRED', runCountBefore: 6, runCountAfter: 6 }
    assertRefusalEvidence(evidence)
    assert.throws(() => assertRefusalEvidence({ ...evidence, runCountAfter: 7 }))
    assert.throws(() => assertRefusalEvidence({ ...evidence, after: { ...before, head: ids[0] } }))
  })
  test('preview fingerprint binds exact params, key and digest to admitted run', () => {
    const intent = { params, catalog_digest: digest, idempotency_key: 'intent-1' }
    const url = new URL(`${api}/api/capabilities`)
    for (const [key, value] of Object.entries({ project_id: ids[1], drawing_id: ids[2], input_version_id: ids[3], project_runs: JSON.stringify({ 'solar-size-strings': intent }) })) url.searchParams.set(key, value)
    const previewRequest = request(undefined, { url: String(url), method: 'GET' })
    const row = { name: 'solar-size-strings', catalog_digest: digest, availability: { admission_checked: true,
      admission: { status_code: 200, error: null }, refusal_reasons: [], entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true } }
    const body = { families: [{ capabilities: [row] }] }
    assert.equal(inspectPreview(previewRequest, body, 200, api)[0].signature, inspectRun(request(), api, binding, row.name).signature)
    row.availability.runnable = false; assert.equal(inspectPreview(previewRequest, body, 200, api)[0].ready, false)
    row.availability.admission = { status_code: 409, error: { reason_code: 'EQUIPMENT_ASSIGNMENT_REQUIRED' } }
    row.availability.refusal_reasons = ['capability_not_ready']
    const refused = inspectPreview(previewRequest, body, 200, api)[0]
    assert.deepEqual(refused.codes, ['EQUIPMENT_ASSIGNMENT_REQUIRED'])
    assert.deepEqual(refused.genericReasons, ['capability_not_ready'])
    assert.equal(previewHasParams(refused, params), true)
    assert.equal(previewHasParams(refused, { ...params, expected_rev: 99 }), false)
    row.catalog_digest = `sha256:${'f'.repeat(64)}`; assert.throws(() => inspectPreview(previewRequest, body, 200, api))
  })
  test('response observer drains asynchronous errors and disposes listeners', async () => {
    const page = new EventEmitter(), observer = observeProjectTraffic(page, api)
    page.emit('response', { request: () => request(undefined, { url: `${api}/api/projects`, method: 'GET' }),
      url: () => `${api}/api/projects`, status: () => 200, json: async () => { throw new Error('private payload') } })
    await assert.rejects(observer.drain(), error => error.message === 'R13-H response observation failed')
    observer.dispose(); assert.equal(page.listenerCount('request'), 0); assert.equal(page.listenerCount('response'), 0)
  })
  test('response observer binds extracted upload version and omits query material', async () => {
    const page = new EventEmitter(), observer = observeProjectTraffic(page, api)
    const url = `${api}/api/drawings/${ids[2]}/upload-status?privateQuery=not-for-evidence`
    page.emit('response', { request: () => request(undefined, { url, method: 'GET' }), url: () => url,
      status: () => 200, json: async () => ({ drawing_id: ids[2], status: 'ready', extracted_version: 7 }) })
    await observer.drain()
    assert.deepEqual(observer.readyUploads, [{ drawing_id: ids[2], version: 7 }])
    assert.equal([...observer.endpoints].some(endpoint => endpoint.includes('?')), false)
    observer.dispose()
  })
  test('response observer refuses malformed or absent extracted upload versions', async () => {
    for (const extractedVersion of [undefined, null, 0, -1, 1.5, '1']) {
      const page = new EventEmitter(), observer = observeProjectTraffic(page, api)
      const url = `${api}/api/drawings/${ids[2]}/upload-status`
      page.emit('response', { request: () => request(undefined, { url, method: 'GET' }), url: () => url,
        status: () => 200, json: async () => ({ drawing_id: ids[2], status: 'ready', extracted_version: extractedVersion }) })
      try {
        await assert.rejects(observer.drain(), error => error.message === 'R13-H response observation failed')
        assert.deepEqual(observer.readyUploads, [])
      } finally { observer.dispose() }
    }
  })
  test('public evidence projects allowlisted fields and refuses private values', () => {
    const step = { tool: 'solar-settings', job_id: ids[4], input_version_id: ids[3], output_version_id: ids[0],
      before_rev: 0, after_rev: 1, intake_sha256: 'a'.repeat(64), graph_sha256: 'b'.repeat(64), admission_status: 200, run_status: 202,
      raw: { authorization: 'Bearer test' } }
    const input = { binding, importedVersionId: ids[3], fence: '1', gauge: '10 AWG', steps: Array(6).fill(step),
      refusals: Array(2).fill({ tool: 'solar-homeruns', code: 'EQUIPMENT_ASSIGNMENT_REQUIRED', before_head: ids[3], after_head: ids[3], run_count_before: 6, run_count_after: 6 }),
      authenticatedImport: true, actorHeaderAbsent: true, headers }
    const result = publicEvidence(input)
    assert.equal(JSON.stringify(result).includes('fixture-only'), false)
    assert.throws(() => publicEvidence({ ...input, gauge: `lco1.${'a'.repeat(64)}` }))
  })
  test('sibling Playwright spec parses without importing or starting it', () => {
    const result = spawnSync(process.execPath, ['--check', fileURLToPath(new URL('./solar-project-workspace-walk.spec.mjs', import.meta.url))], { encoding: 'utf8', windowsHide: true, timeout: 10000 })
    assert.equal(result.status, 0, 'sibling spec must parse')
  })
}
