// Standalone byte oracle for R7 (planner-supplied; do not edit).
// The fixture beside this file was captured from UNCHANGED main 0c5a044e by
// running these exact calls with a recording fetch. This file re-runs the same
// calls against the current tree and requires every request (URL, method field,
// ordered header entries, body presence and bytes, cache field, signal presence,
// init keys) to equal the capture. It never writes or regenerates the fixture.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import fs from 'node:fs'
import {
  config,
  getCapabilities,
  getDrawingIntake,
  getDrawingVersions,
  takeCheckout,
  releaseCheckout,
  runToolAsync,
  getJob,
} from '../api.js'
import { createRunSubmissionRequest } from '../runIntent.js'

const FIXTURE_PATH = `${process.cwd()}/src/solar/__fixtures__/solarStandaloneRequests.main-0c5a044e.json`
const FIXTURE = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'))
const TOKEN = 'r7-tok'
const CAP = 'lco1.' + 'a'.repeat(64)
const DIGEST = 'sha256:' + 'b'.repeat(64)
const JOB = '3f1c2b4a-1d2e-4f5a-8b9c-0d1e2f3a4b5c'
const IDEM = 'r7-idem-key-0001'
const PARAMS = { drawing_id: 'rooftop_demo', zip: '80301' }
const READ_OPTS = { catalogDigest: DIGEST }
const WRITE_OPTS = { catalogDigest: DIGEST, dwgVersion: 7, checkoutCapability: CAP, idempotencyKey: IDEM }

let calls = []

function headerEntries(headers) {
  if (!headers) return null
  if (typeof Headers !== 'undefined' && headers instanceof Headers) return [...headers.entries()]
  return Object.entries(headers)
}

function respond(url, init) {
  const method = (init && init.method) || 'GET'
  const json = (status, body) => new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  })
  if (url.includes('/api/capabilities') && url.includes('legacy-fallback')) return json(404, {})
  if (url.includes('/api/capabilities')) return json(200, { families: [] })
  if (url.endsWith('/api/tools')) return json(200, { tools: [] })
  if (url.includes('/intake')) return json(200, {})
  if (url.includes('/versions')) return json(200, { versions: [] })
  if (url.includes('/checkout') && method === 'POST') return json(200, { acquired: true })
  if (url.includes('/checkout') && method === 'DELETE') return json(200, { released: true, checkout: null })
  if (url.endsWith('/api/run')) {
    return json(400, { ok: false, error: { error_code: 'R7_CAPTURE', message: 'capture', retryable: false } })
  }
  if (url.includes('/api/jobs/')) return json(200, { job_id: JOB, status: 'running' })
  return json(599, {})
}

beforeEach(() => {
  calls = []
  vi.stubGlobal('fetch', async (input, init) => {
    const url = typeof input === 'string' ? input : String(input?.url ?? input)
    if (!url.includes('/api/telemetry')) {
      calls.push({
        url,
        method_field: init && 'method' in init ? init.method ?? null : '<absent>',
        headers: headerEntries(init?.headers),
        body_key_present: !!init && Object.prototype.hasOwnProperty.call(init, 'body'),
        body: init && init.body !== undefined ? String(init.body) : null,
        cache: init && 'cache' in init ? init.cache ?? null : '<absent>',
        has_signal: !!(init && init.signal),
        init_keys: init ? Object.keys(init).sort() : null,
      })
    }
    return respond(url, init)
  })
  localStorage.clear()
})

const NETWORK_CASES = [
  ['getCapabilities.bare.noauth', false, () => getCapabilities(false, undefined)],
  ['getCapabilities.bare', true, () => getCapabilities(false, undefined)],
  ['getCapabilities.emptyDrawing', true, () => getCapabilities(false, { drawing_id: '' })],
  ['getCapabilities.drawingHead', true, () => getCapabilities(false, { drawing_id: 'rooftop_demo' })],
  ['getCapabilities.drawingVersion', true, () => getCapabilities(false, { drawing_id: 'rooftop_demo', drawing_version: 7 })],
  ['getCapabilities.encodedDrawing', true, () => getCapabilities(false, { drawing_id: 'a b/c?d&e' })],
  ['getCapabilities.legacy404Fallback', true, () => getCapabilities(false, { drawing_id: 'legacy-fallback' })],
  ['getDrawingIntake.head', true, () => getDrawingIntake(false, 'rooftop_demo')],
  ['getDrawingIntake.version', true, () => getDrawingIntake(false, 'rooftop_demo', 7)],
  ['getDrawingVersions.default', true, () => getDrawingVersions(false, 'rooftop_demo')],
  ['getDrawingVersions.deltas', true, () => getDrawingVersions(false, 'rooftop_demo', { includeDeltas: true })],
  ['takeCheckout.omitted', true, () => takeCheckout('rooftop_demo')],
  ['takeCheckout.holderTtl', true, () => takeCheckout('rooftop_demo', 'holder-a', 600)],
  ['takeCheckout.renewCapability', true, () => takeCheckout('rooftop_demo', undefined, undefined, CAP)],
  ['releaseCheckout.noCapability', true, () => releaseCheckout('rooftop_demo')],
  ['releaseCheckout.capability', true, () => releaseCheckout('rooftop_demo', CAP)],
  ['runToolAsync.read', true, () => runToolAsync('count-by-layer', PARAMS, 'rooftop_demo', READ_OPTS)],
  ['runToolAsync.write', true, () => runToolAsync('solar-settings', PARAMS, 'rooftop_demo', WRITE_OPTS)],
  ['getJob.authenticated', true, () => getJob(JOB)],
]

const PURE_CASES = [
  ['createRunSubmissionRequest.read', () => createRunSubmissionRequest('count-by-layer', PARAMS, 'rooftop_demo', READ_OPTS)],
  ['createRunSubmissionRequest.write', () => createRunSubmissionRequest('solar-settings', PARAMS, 'rooftop_demo', WRITE_OPTS)],
]

function fixtureCase(name) {
  const found = FIXTURE.cases.filter((c) => c.name === name)
  expect(found).toHaveLength(1)
  return found[0]
}

describe('R7 standalone requests equal the unchanged-main capture', () => {
  it('STD-00 the fixture is the frozen 0c5a044e capture and every case is exercised', () => {
    expect(FIXTURE.schema).toBe('leaf.r7-standalone-request-capture.v1')
    expect(FIXTURE.base).toBe('0c5a044ef71afff41e42b7c517494ea66dcc338a')
    expect(FIXTURE.config).toEqual({ apiBase: config.apiBase, tenant: config.tenant })
    const names = [...NETWORK_CASES.map(([n]) => n), ...PURE_CASES.map(([n]) => n)].sort()
    expect(FIXTURE.cases.map((c) => c.name).sort()).toEqual(names)
  })

  NETWORK_CASES.forEach(([name, auth, run], index) => {
    const id = String(index + 1).padStart(2, '0')
    it(`STD-${id} ${name}`, async () => {
      calls = []
      localStorage.clear()
      if (auth) localStorage.setItem('leaf.jwt', TOKEN)
      let outcome = 'resolved'
      try { await run() } catch (error) { outcome = `threw:${error?.status ?? error?.name ?? 'error'}` }
      const expected = fixtureCase(name)
      expect(outcome).toBe(expected.outcome)
      expect(calls).toEqual(expected.requests)
    })
  })

  PURE_CASES.forEach(([name, build], index) => {
    const id = String(NETWORK_CASES.length + index + 1).padStart(2, '0')
    it(`STD-${id} ${name}`, () => {
      const r = build()
      const expected = fixtureCase(name)
      expect({ headers: Object.entries(r.headers), body: JSON.stringify(r.body) }).toEqual(expected.pure)
    })
  })
})
