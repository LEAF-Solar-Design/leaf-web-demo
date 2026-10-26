import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { performance } from 'node:perf_hooks'
import { setTimeout as delay } from 'node:timers/promises'
import { isDeepStrictEqual } from 'node:util'
import { expect, test } from '@playwright/test'
import {
  allowedStagingOrigins,
  assertAllowedStagingHost,
  assertResponseOnAllowedOrigin,
  assertStagingTokenIdentity,
  PRODUCTION_HOSTS,
  resolveStagingBaseURL,
  stagingProofPath,
} from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'
import { writeProofReceipt } from '../proofReceipt.mjs'

const SAMPLE_COUNT = 20
const DOCUMENT = 'lab-sse-proof'
function proofGraph() {
  // server/lab_evaluate.py dispatches through lab_ops.has_operator:
  // this is server/lab_ops.py's OPERATOR_IDS set.
  const knownBuiltins = new Set([
    'constant', 'identity', 'field', 'select', 'filter', 'sort', 'take', 'count',
    'sum', 'mean', 'min', 'max', 'add', 'subtract', 'multiply', 'divide',
    'latest', 'window', 'collect',
  ])
  // server/tests/test_lab_worker.py::test_finishes_values_pins_and_status
  // evaluates operator-chain (including constant) and asserts durable value events.
  const fixture = JSON.parse(readFileSync(new URL('../../../contract/fixtures/lab/blocks.json', import.meta.url), 'utf8'))
  const manifest = fixture.manifests.find((item) =>
    item.effect === 'pure' && item.kind === 'builtin' && knownBuiltins.has(item.id) &&
    Object.values(item.inputs).every((input) => !input.required || input.has_default) &&
    Object.keys(item.outputs).length > 0)
  if (!manifest) {
    throw new Error('Lab SSE proof requires a pure known builtin in contract/fixtures/lab/blocks.json with no required inputs lacking defaults and at least one output; no eligible manifest found')
  }
  const settings = Object.fromEntries(Object.entries(manifest.settings_schema.properties || {})
    .filter(([, schema]) => Object.hasOwn(schema, 'default'))
    .map(([name, schema]) => [name, schema.default]))
  const output = Object.keys(manifest.outputs)[0]
  return {
    schema: 'leaf.lab-graph.v1',
    blocks: [{
      id: 'block-0',
      registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 },
      settings,
    }],
    pipes: [],
    outputs: { [output]: { node: 'block-0', port: output } },
  }
}

let token, origin, identity, tenantHash, revision, finishedRun
const evidence = {}

function stagingURL(path) {
  const url = new URL(path, origin)
  if (url.username || url.password || PRODUCTION_HOSTS.includes(url.hostname) ||
      !allowedStagingOrigins().has(url.origin.toLowerCase())) {
    throw new Error('Lab SSE proof refuses credentials in URLs or a non-staging origin')
  }
  return url
}

// Use Node fetch, not Playwright's authenticated request fixture: retained
// Playwright traces must never serialize a real Authorization header.
async function call(path, { bearer = token, ...options } = {}) {
  const url = stagingURL(path)
  let response
  try {
    response = await fetch(url, {
      ...options,
      redirect: 'error',
      signal: options.signal || AbortSignal.timeout(15_000),
      headers: {
        ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    })
  } catch {
    // Fetch errors (including causes) can contain request details. Never
    // propagate those details or arbitrary server response bodies to logs.
    throw new Error(`Lab SSE ${options.method || 'GET'} ${url.pathname} failed or timed out`)
  }
  assertResponseOnAllowedOrigin({ url: () => response.url })
  return response
}

async function json(path, options, status = 200) {
  const response = await call(path, options)
  expect(response.status, 'Lab API status').toBe(status)
  try {
    return await response.json()
  } catch {
    throw new Error('Lab API did not return valid JSON')
  }
}

function streamPath(runId) {
  return `/api/lab/stream/runs?runs=${encodeURIComponent(runId)}&after_seq=-1`
}

async function openStream(runId, lastId) {
  const controller = new AbortController()
  const started = performance.now()
  const timer = setTimeout(() => controller.abort(), 20_000)
  let reader
  const close = async () => {
    clearTimeout(timer)
    controller.abort()
    if (reader) {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
  }
  try {
    const response = await call(streamPath(runId), {
      signal: controller.signal,
      headers: { Accept: 'text/event-stream', ...(lastId ? { 'Last-Event-ID': lastId } : {}) },
    })
    expect(response.status).toBe(200)
    expect(response.headers.get('content-type')).toContain('text/event-stream')
    if (!response.body) throw new Error('Lab SSE response has no streaming body')
    reader = response.body.getReader()
  } catch (error) {
    await close()
    throw error
  }

  async function* frames() {
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      let chunk
      try {
        chunk = await reader.read()
      } catch {
        throw new Error('Lab SSE reader failed or exceeded its 20 second deadline')
      }
      if (chunk.done) throw new Error('Lab SSE ended before the required evidence arrived')
      buffer += decoder.decode(chunk.value, { stream: true })
      if (buffer.length > 2 * 1024 * 1024) throw new Error('Lab SSE frame exceeded the proof buffer limit')
      let boundary
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        const frame = { event: '', id: '', comments: [], data: '', at: performance.now() }
        const data = []
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith(':')) frame.comments.push(line.slice(1).trim())
          else {
            const colon = line.indexOf(':')
            const field = colon < 0 ? line : line.slice(0, colon)
            const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
            if (field === 'data') data.push(value)
            else if (field === 'event' || field === 'id') frame[field] = value
          }
        }
        frame.data = data.join('\n')
        // Timestamp before yielding, including when several frames share a chunk.
        yield frame
      }
    }
  }
  return { frames: frames(), close, started }
}

function runFrame(frame, runId) {
  expect(frame.event, 'unexpected stream control event').toBe('run')
  const prefix = `${runId}:`
  expect(frame.id.startsWith(prefix), 'frame belongs to the requested run').toBe(true)
  const seq = Number(frame.id.slice(prefix.length))
  expect(Number.isSafeInteger(seq) && seq >= 0, 'valid sequence').toBe(true)
  let data
  try { data = JSON.parse(frame.data) } catch { throw new Error('Lab SSE run frame has invalid JSON') }
  expect(data.seq).toBe(seq)
  expect(data.run_id === runId, 'payload belongs to the requested run').toBe(true)
  return { seq, data }
}

async function waitFinished(runId) {
  const deadline = performance.now() + 30_000
  while (performance.now() < deadline) {
    const row = await json(`/api/lab/runs/${encodeURIComponent(runId)}`)
    if (row.state === 'finished') return
    if (['failed', 'cancelled', 'suspended'].includes(row.state)) {
      throw new Error('Pure Lab proof run terminated without finishing')
    }
    await delay(250)
  }
  throw new Error('Pure Lab proof run did not finish within 30 seconds')
}

test.describe('SEL-220 authenticated staging Lab fetch-SSE proof', () => {
  test.describe.configure({ mode: 'serial', timeout: 180_000 })

  // All environment checks and network activity are deferred to execution.
  // --list must work with no JWT and no staging connectivity.
  test.beforeAll(async ({ playwright }) => {
    token = process.env.LEAF_E2E_STAGING_JWT || ''
    if (!token.trim()) throw new Error('LEAF_E2E_STAGING_JWT is required: the Lab SSE proof fails without real staging credentials')
    assertStagingTokenIdentity(token, process.env)
    const base = assertAllowedStagingHost(resolveStagingBaseURL())
    if (base.username || base.password || PRODUCTION_HOSTS.includes(base.hostname)) {
      throw new Error('Lab SSE proof refuses production hosts or URL credentials')
    }
    origin = base.origin
    // First network operation binds all subsequent measurements to a deployment.
    const request = await playwright.request.newContext({ baseURL: origin, maxRedirects: 0 })
    try { identity = await captureStagingIdentity(request) } finally { await request.dispose() }
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
    const tenant = claims['https://leafdesign.ai/tenant_id']
    if (typeof tenant !== 'string' || !tenant) throw new Error('Lab SSE proof requires a tenant claim in the staging JWT')
    tenantHash = createHash('sha256').update(tenant, 'utf8').digest('hex')
  })

  test('credentials and deployed source_revision are required', async () => {
    expect(identity.source_revision).toBeTruthy()
    expect(tenantHash).toMatch(/^[0-9a-f]{64}$/)
  })

  test('20 run samples have POST-to-first-run-frame p95 <= 1000 ms', async () => {
    const GRAPH = proofGraph()
    const head = await call(`/api/lab/docs/${DOCUMENT}/head`)
    expect([200, 404]).toContain(head.status)
    const expectedHead = head.status === 404 ? null : (await head.json()).revision
    const saved = await json(`/api/lab/docs/${DOCUMENT}/save`, {
      method: 'POST', body: JSON.stringify({ graph: GRAPH, expected_head: expectedHead }),
    })
    revision = saved.revision
    expect(Number.isInteger(revision) && revision > 0).toBe(true)
    const samples = []
    for (let index = 0; index < SAMPLE_COUNT; index += 1) {
      const postStarted = performance.now()
      const response = await call('/api/lab/runs', {
        method: 'POST', body: JSON.stringify({ doc_id: DOCUMENT, graph_revision: revision }),
      })
      expect(response.status).toBe(202)
      const run = await response.json()
      expect(typeof run.run_id === 'string' && run.run_id.length > 0).toBe(true)
      const stream = await openStream(run.run_id)
      try {
        for await (const frame of stream.frames) {
          if (frame.comments.length) continue
          runFrame(frame, run.run_id)
          samples.push(frame.at - postStarted)
          break
        }
      } finally { await stream.close() }
      await waitFinished(run.run_id)
      finishedRun = run.run_id
    }
    expect(samples).toHaveLength(SAMPLE_COUNT)
    const sorted = [...samples].sort((a, b) => a - b)
    evidence.latency = {
      sample_count: samples.length, samples_ms: samples,
      percentile_method: 'nearest rank',
      p50_ms: sorted[Math.ceil(samples.length * 0.50) - 1],
      p95_ms: sorted[Math.ceil(samples.length * 0.95) - 1],
    }
    expect(evidence.latency.p95_ms).toBeLessThanOrEqual(1000)
  })

  test('finished run stays open for 16 seconds with heartbeat and health by 17 seconds', async () => {
    const stream = await openStream(finishedRun)
    let heartbeat, health
    try {
      for await (const frame of stream.frames) {
        if (frame.comments.includes('hb')) heartbeat ??= frame.at - stream.started
        if (frame.event === 'health') health ??= frame.at - stream.started
        if (frame.event === 'run') runFrame(frame, finishedRun)
        if (['gap', 'close'].includes(frame.event)) throw new Error('Unexpected gap or close during heartbeat proof')
        if (heartbeat !== undefined && health !== undefined) break
      }
      expect(heartbeat).toBeLessThanOrEqual(17_000)
      expect(health).toBeLessThanOrEqual(17_000)
      while (performance.now() - stream.started < 16_000) {
        await delay(Math.max(1, Math.ceil(16_000 - (performance.now() - stream.started))))
      }
      evidence.heartbeat = { heartbeat_ms: heartbeat, health_ms: health, held_open_ms: performance.now() - stream.started }
      expect(evidence.heartbeat.held_open_ms).toBeGreaterThanOrEqual(16_000)
    } finally { await stream.close() }
  })

  test('abort then Last-Event-ID resume repeats no sequence and loses no later event', async () => {
    const initial = await openStream(finishedRun)
    let first
    try {
      for await (const frame of initial.frames) {
        if (frame.comments.length) continue
        runFrame(frame, finishedRun)
        first = frame
        break
      }
    } finally { await initial.close() }
    const cursor = runFrame(first, finishedRun).seq
    const snapshot = await json(`/api/lab/runs/${encodeURIComponent(finishedRun)}/events?after_seq=-1&limit=500`)
    expect(snapshot.events.some((event) => event.kind === 'finished')).toBe(true)
    const expected = snapshot.events.filter((event) => event.seq > cursor)
    const resumed = await openStream(finishedRun, first.id)
    const observed = []
    try {
      // Read through the next health boundary, even after the expected suffix,
      // so extra or repeated frames cannot hide after the final expected frame.
      for await (const frame of resumed.frames) {
        if (frame.event === 'health') break
        if (frame.comments.length) continue
        const { seq, data } = runFrame(frame, finishedRun)
        expect(seq).toBeGreaterThan(cursor)
        const { run_id, ...event } = data
        observed.push(event)
      }
    } finally { await resumed.close() }
    // Compare the full payload without letting Playwright print event data
    // (which can contain tenant details) when the replay differs.
    expect(isDeepStrictEqual(observed, expected),
      'resumed events match the complete durable event suffix').toBe(true)
    evidence.resume = { passed: true, cursor_seq: cursor, expected_events: expected.length, observed_events: observed.length, mechanism: 'client abort then Last-Event-ID' }
  })

  test('auth rejects missing bearer, query credentials, and a validly shaped wrong bearer', async () => {
    const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url')
    const wrong = `${encode({ alg: 'HS256', typ: 'JWT' })}.${encode({ sub: 'lab-sse-invalid', exp: Math.floor(Date.now() / 1000) + 300 })}.${Buffer.alloc(32).toString('base64url')}`
    const cases = [
      ['missing_authorization', streamPath(finishedRun), '', 401],
      // This sentinel is deliberately not the real token; the real JWT never
      // enters a URL. Supply real header auth so the route's 400 guard runs.
      ['query_token', `${streamPath(finishedRun)}&token=forbidden-proof-sentinel`, token, 400],
      ['wrong_bearer', streamPath(finishedRun), wrong, 401],
    ]
    evidence.auth = {}
    for (const [name, path, bearer, expected] of cases) {
      const response = await call(path, { bearer })
      const status = response.status
      await response.body?.cancel()
      expect(status, name).toBe(expected)
      evidence.auth[name] = status
    }
  })

  test('cursor gap is explicitly unobserved with MAX_EVENTS unit coverage', async () => {
    evidence.gap = {
      gap_observed: false,
      reason: 'A small pure staging graph cannot exceed server/lab_run_store.py MAX_EVENTS=10000; no gap is fabricated.',
      unit_test: 'server/tests/test_lab_stream.py::test_gap_then_public_events',
    }
    expect(evidence.gap.gap_observed).toBe(false)
  })

  test('receipt binds deployed SHA, sample count, timing, resume, auth and gap limitations', async () => {
    expect(evidence.latency.sample_count).toBe(SAMPLE_COUNT)
    expect(evidence.resume.passed).toBe(true)
    writeProofReceipt(stagingProofPath('lab-sse', 'receipt.json'), {
      capability_ids: ['JB-02'], evidence_tier: 'staging',
      route: '/api/lab/stream/runs', runtime: 'Node fetch streaming body reader through authenticated staging proxy',
      source_commit: identity.source_revision,
      api_endpoints: [identity.endpoint, 'POST /api/lab/docs/{doc}/save', 'POST /api/lab/runs', 'GET /api/lab/stream/runs', 'GET /api/lab/runs/{id}', 'GET /api/lab/runs/{id}/events'],
      assertions: ['20 POST-response-to-first-run-frame samples have p95 <= 1000 ms', 'heartbeat and health arrive by 17 seconds on a finished run', 'Last-Event-ID resume matches the complete durable event suffix', 'auth statuses are 401 / 400 / 401'],
      // The ledger has no Lab stream capability. Do not claim its converse/job
      // fallback and browser reattach subcases from this API-only proof.
      sub_cases: { proven: [], not_proven: ['stream loss', 'fallback', 'reload reattach', 'duplicate suppression'] },
      result: { verdict: 'pass', proof: 'SEL-220 Lab SSE', observed_source_revision: identity.source_revision, tenant_sha256: tenantHash, ...evidence },
      limitations: [
        evidence.gap.reason, evidence.gap.unit_test,
        'The minimal empty graph can have only a terminal event: resume suffix counts are recorded, and a zero suffix does not prove nonempty replay.',
        'Client abort/resume is measured; an actual deployment drain and credential expiry are not induced by this spec.',
        'JB-02 ledger subcases remain unproven: this receipt measures the Lab API, not converse/job UI fallback or reattach.',
      ],
    })
  })
})
