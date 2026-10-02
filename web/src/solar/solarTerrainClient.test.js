// @vitest-environment node
// sf-w5-terrain-panel-files (W20-06a): the Studio side of GET /api/drawings/{drawing_id}/terrain and
// POST /api/drawings/{drawing_id}/terrain/operations.
// Every fixture below is SYNTHETIC: built here in the shape server/routers/solar_terrain.py,
// server/solar_ground_terrain_adapter.py and server/envelopes.py document, never captured from a
// running server. A success fixture is one the real validator accepts (the client under test
// validates it on the way through). Row C2 reads the route's own refusal map from the checkout.
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import {
  TERRAIN_CLIENT_REASONS, TERRAIN_FALLBACK_SENTENCE, TERRAIN_MAX_API_BASE_LENGTH, TERRAIN_MAX_TIMEOUT_MS,
  TERRAIN_REQUEST_MAX_BYTES, TERRAIN_RESPONSE_MAX_BYTES, TERRAIN_ROUTE_REASONS, TERRAIN_TIMEOUT_MS,
  createSolarTerrainClient, terrainReason,
} from './solarTerrainClient.js'

const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'c'.repeat(64)
const MESH = 'terrain-mesh-render'
const SLOPE = 'tracker-slope-violations'
const DEFAULTS = Object.freeze({
  MaxNsSlopePct: 8.5, MaxRowToRowEwSlopePct: 10, MaxAxialSlopePct: 8.5, MaxCrossAxisSlopePct: 10,
  MaxRowToRowSlopeDeg: 4, MaxSlopePercent: 15, Columns: 0,
})
const KERNEL_STATUS = 'Tracker slope: 0/2 axial / 0/1 cross - within ASCE 7-16 budget.'
// Built from their codes so this source stays ASCII: a two-byte character, U+FFFD, and both dashes.
const E_ACUTE = String.fromCharCode(0xe9)
const REPLACEMENT_CHARACTER = String.fromCharCode(0xfffd)
const EN_DASH = String.fromCharCode(0x2013)
const EM_DASH = String.fromCharCode(0x2014)
const CLIENT_AND_SESSION_CODES = [
  'BAD_PARAMS', 'ENTITLEMENT_POLICY_UNAVAILABLE', 'ENTITLEMENT_REQUIRED', 'FORBIDDEN', 'INTERNAL',
  'TERRAIN_CLIENT_ABORTED', 'TERRAIN_CLIENT_NETWORK', 'TERRAIN_CLIENT_REQUEST_INVALID',
  'TERRAIN_CLIENT_RESPONSE_INVALID', 'TERRAIN_CLIENT_TIMEOUT', 'UNAUTHENTICATED',
]

const clone = (value) => JSON.parse(JSON.stringify(value))
const envelope = (body) => ({ ...body, error: null, degraded_mode: false })
const normalize = (body) => {
  const copy = clone(body)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
const headOf = ({ artifact = H, index = 0, parent = null, project = 'p' } = {}) => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: 'solar', project_id: project, index, parent,
  state: {
    artifact_id: artifact, media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024,
    content_sha256: 'd'.repeat(64), source_version: 1, schema: 'leaf.solar-artifact-ref.v1',
    download: `/api/drawings/solar/artifacts/${artifact}`,
  },
})
const frameOf = () => ({
  coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1, crs: 'none',
  elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres',
})
// A 2 by 2 grid summary whose cells follow the adapter's own formulas.
const gridOf = () => ({
  rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10, cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
  elevation_min_m: 0, elevation_max_m: 0, grid_sha256: D,
})
const viewOf = ({ project = 'p', head, grid = gridOf(), mesh_faces = 0, mesh = { state: 'absent', record: null } } = {}) => envelope({
  schema: 'leaf.solar-terrain-view-response.v1', stored: true, head: head ?? headOf({ project }),
  terrain: {
    schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', frame: frameOf(), grid, mesh_faces, slope_markers: 0,
    previews: { [MESH]: mesh, [SLOPE]: { state: 'absent', record: null } }, drawing_id: 'solar', project_id: project,
  },
})
const V2 = () => viewOf()
const meshRecord = () => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: MESH, maturity: 'preview', grid_sha256: D, meters_per_unit: 1,
  faces: 1, buckets: { Green: 1, Yellow: 0, Red: 0 }, max_slope_percent: 0, mesh_sha256: 'e'.repeat(64),
})
const slopeRecord = (limits = DEFAULTS) => ({
  schema: 'leaf.solar-terrain-preview.v1', capability: SLOPE, maturity: 'preview', grid_sha256: D, meters_per_unit: 1,
  limits: { ...limits }, frames: 2, markers: 0, status: KERNEL_STATUS, report_sha256: 'f'.repeat(64),
})
const slopeReport = () => ({
  tracker_count: 2, axial_rows_checked: 2, axial_violation_rows: 0, cross_axis_pairs_checked: 1,
  cross_axis_violation_pairs: 0, row_to_row_pairs_checked: 1, row_to_row_angle_violation_pairs: 0,
  trackers_needing_terrain_following: 0, has_violations: false, status: KERNEL_STATUS,
})
// An operation result: a new child of the head H the request named.
function resultOf(operation, { limits = DEFAULTS, head } = {}) {
  const body = {
    schema: 'leaf.solar-terrain-operation.v1', maturity: 'preview', operation,
    capability: operation === 'mesh' ? MESH : SLOPE, created: true, drawing_id: 'solar', project_id: 'p',
    frame: frameOf(), grid: operation === 'slope-clear' ? null : gridOf(),
    record: operation === 'mesh' ? meshRecord() : operation === 'slope' ? slopeRecord(limits) : null, replaced: 0,
  }
  if (operation === 'slope') body.report = slopeReport()
  body.head = head ?? headOf({ artifact: H2, index: 1, parent: H })
  return envelope(body)
}
// The failure envelope of server/envelopes.py (err_envelope), with the route's reason code when it has one.
const errorEnvelope = (errorCode, message, retryable, reasonCode) => {
  const error = {
    error_code: errorCode, message, retryable, retry_class: retryable ? 'backoff' : 'after_action', actor: 'user',
    next_action: 'Review the inputs, correct them, and submit again.',
  }
  if (reasonCode !== undefined) error.reason_code = reasonCode
  return {
    ok: false, tool: null, version: null, result: null, overlay: null, timing_ms: 0, cost: null, error,
    degraded_mode: false,
  }
}
// The two entitlement answers of server/entitlements.py, which carry a marker and no reason code.
const entitlementBody = (required, errorCode, message, retryable) => ({
  entitlement_required: true, required, tier: 'demo',
  error: { error_code: errorCode, message, retryable, retry_class: retryable ? 'backoff' : 'after_action' },
  degraded_mode: false,
})
const DENIED = entitlementBody('run_write', 'ENTITLEMENT_REQUIRED',
  "the 'demo' plan does not include running tools that modify the drawing; upgrade the workspace plan to enable write tools.", false)
const POLICY = entitlementBody('run_read', 'INTERNAL', 'entitlement policy is unavailable; request refused (fail closed).', true)
const GUEST = errorEnvelope('FORBIDDEN',
  'guest sessions are upload-only: upload, upload-status, intake and versions reads; create an account for everything else', false)
const UNAUTH = errorEnvelope('UNAUTHENTICATED', 'missing bearer token (Authorization header)', false)

const byteLength = (text) => new TextEncoder().encode(text).byteLength
const textResponse = (text, status = 200, headers = {}) => new Response(
  text, { status, headers: { 'content-type': 'application/json', ...headers } },
)
const jsonResponse = (body, status = 200) => textResponse(JSON.stringify(body), status)
const bodiless = (text, status = 200) => ({ status, headers: new Headers(), body: null, text: async () => text })
const answering = (makeResponse) => vi.fn(async () => makeResponse())
const neverAnswers = () => vi.fn((url, init) => new Promise((resolve, reject) => {
  init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
}))
const stalledResponse = (status = 200) => new Response(new ReadableStream({ start() {} }), {
  status, headers: { 'content-type': 'application/json' },
})
const tenantHeaders = () => ({ 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' })
function clientWith(fetchImpl, extra = {}) {
  const onResponse = vi.fn()
  const client = createSolarTerrainClient({
    fetchImpl, apiBase: 'https://studio.test', headers: tenantHeaders, onResponse, ...extra,
  })
  return { client, onResponse }
}
const refused = (status, code, retryable) => ({ ok: false, status, code, retryable })
const VIEW_ARGS = Object.freeze({ drawingId: 'solar', projectId: 'p' })
const MESH_ARGS = Object.freeze({ drawingId: 'solar', projectId: 'p', operation: 'mesh', expectedHead: H })
// Both methods, each with a success body its own request accepts.
const METHODS = [
  ['getTerrain', (client, signal) => client.getTerrain({ ...VIEW_ARGS, signal }), V2()],
  ['runTerrainOperation', (client, signal) => client.runTerrainOperation({ ...MESH_ARGS, signal }), resultOf('mesh')],
]
const HUNG = 'hung'
async function settleWithin(promise, ms = 5000) {
  let timer
  const bound = new Promise((resolve) => { timer = setTimeout(() => resolve(HUNG), ms) })
  try {
    return await Promise.race([promise, bound])
  } finally {
    clearTimeout(timer)
  }
}
const spin = (ms) => {
  const until = performance.now() + ms
  while (performance.now() < until) { /* a synchronous delay no timer can interrupt */ }
}
// Every read ends inside the budget. The chunk's length is read again while the chunks are
// assembled, after the read that ends the body, and `afterEnd` runs once inside that work.
const slowAssembly = (text, status, afterEnd) => answering(() => {
  const chunk = new TextEncoder().encode(text)
  const length = chunk.byteLength
  let ended = false
  let ran = false
  Object.defineProperty(chunk, 'byteLength', {
    get() {
      if (ended && !ran) {
        ran = true
        afterEnd()
      }
      return length
    },
  })
  let sent = false
  const reader = {
    async read() {
      if (!sent) {
        sent = true
        return { done: false, value: chunk }
      }
      ended = true
      return { done: true, value: undefined }
    },
    async cancel() {},
  }
  return { status, headers: new Headers({ 'content-type': 'application/json' }), body: { getReader: () => reader } }
})
// A real caller signal whose removeEventListener does something before or after the real removal.
const wrappedSignal = (before, after = () => {}) => {
  const caller = new AbortController()
  const { signal } = caller
  const remove = signal.removeEventListener.bind(signal)
  signal.removeEventListener = (...rest) => {
    before(caller)
    remove(...rest)
    after(caller)
  }
  return signal
}
function readServer(relative) {
  const buffer = readFileSync(new URL(`../../../server/${relative}`, import.meta.url))
  expect(buffer.length).toBeLessThanOrEqual(1024 * 1024)
  return buffer.toString('utf8')
}
// Every CODE: (status, ErrorCode.X, bool) row of the route's TERRAIN_ROUTE_REFUSALS literal. The
// pattern is built from a string so no quote character sits in a regular expression literal.
function routeMap() {
  const text = readServer('routers/solar_terrain.py')
  const start = text.indexOf('TERRAIN_ROUTE_REFUSALS = {')
  expect(start).toBeGreaterThanOrEqual(0)
  const end = text.indexOf('\n}', start)
  expect(end).toBeGreaterThan(start)
  const row = new RegExp(
    '\\x22([A-Z][A-Z0-9_]{0,63})\\x22:\\s*\\((\\d{3}),\\s*ErrorCode\\.([A-Z_]+),\\s*(True|False)\\)', 'g')
  const rows = new Map()
  for (const match of text.slice(start, end).matchAll(row)) {
    rows.set(match[1], { status: Number(match[2]), envelope: match[3], retryable: match[4] === 'True' })
  }
  expect(rows.size).toBeGreaterThan(0)
  return rows
}

describe('solar terrain client', () => {
  it('CHECKOUT-GATE CG5 terrain checkout refusals carry their sentences and retry flags', async () => {
    for (const [code, status, retryable, sentence] of [
      ['TERRAIN_CHECKOUT_DENIED', 403, false, 'The drawing checkout is held elsewhere or has ended, so take the checkout and try again'],
      ['TERRAIN_CHECKOUT_UNAVAILABLE', 503, true, 'The drawing checkout could not be confirmed, so try the terrain operation again'],
    ]) {
      expect(terrainReason(code)).toBe(sentence)
      expect(routeMap().get(code)).toEqual({ status, envelope: status === 503 ? 'INTERNAL' : 'BAD_PARAMS', retryable })
      const { client } = clientWith(answering(() => jsonResponse(
        errorEnvelope(status === 503 ? 'INTERNAL' : 'BAD_PARAMS', code, retryable, code), status)))
      expect(await client.runTerrainOperation(MESH_ARGS)).toEqual(refused(status, code, retryable))
    }
  })
  it('W20-06a C1 the view is a GET with the project in the query, and an operation posts the head it was given as JSON', async () => {
    const headers = vi.fn(() => ({ 'content-type': 'text/plain', 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' }))
    const viewBody = viewOf({ project: 'p & q' })
    const fetchImpl = answering(() => jsonResponse(viewBody))
    const { client, onResponse } = clientWith(fetchImpl, { headers })
    expect(await client.getTerrain({ drawingId: 'solar', projectId: 'p & q' }))
      .toEqual({ ok: true, status: 200, value: normalize(viewBody) })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [viewUrl, viewInit] = fetchImpl.mock.calls[0]
    expect(viewUrl).toBe('https://studio.test/api/drawings/solar/terrain?project_id=p%20%26%20q')
    expect(viewInit.method).toBe('GET')
    expect('body' in viewInit).toBe(false)
    expect(viewInit.headers).toEqual({ 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' })
    expect(Object.keys(viewInit.headers).some((name) => /^content-type$/i.test(name))).toBe(false)
    expect(headers).toHaveBeenCalledTimes(1)
    expect(headers).toHaveBeenCalledWith('solar')
    expect(onResponse).toHaveBeenCalledTimes(1)
    expect(onResponse.mock.calls[0][1]).toBe(viewUrl)
    expect(onResponse.mock.calls[0][2]).toBe('Bearer t')
    // No project named: no query, and the head's own project is accepted.
    const plain = clientWith(answering(() => jsonResponse(V2())))
    expect((await plain.client.getTerrain({ drawingId: 'solar' })).ok).toBe(true)
    // The stored view of a drawing with no terrain is a success too.
    const emptyBody = envelope({ schema: 'leaf.solar-terrain-view-response.v1', stored: false, head: null, terrain: null })
    const empty = clientWith(answering(() => jsonResponse(emptyBody)))
    expect(await empty.client.getTerrain(VIEW_ARGS)).toEqual({ ok: true, status: 200, value: normalize(emptyBody) })

    const overrides = { ...DEFAULTS, MaxAxialSlopePct: 12.5, Columns: 3 }
    const cases = [
      ['mesh', undefined, { operation: 'mesh', expected_head: H }],
      ['slope', undefined, { operation: 'slope', expected_head: H, limits: DEFAULTS }],
      ['slope', null, { operation: 'slope', expected_head: H, limits: DEFAULTS }],
      ['slope', { MaxAxialSlopePct: 12.5, Columns: 3 }, { operation: 'slope', expected_head: H, limits: overrides }],
      ['slope', { MaxNsSlopePct: -0 }, { operation: 'slope', expected_head: H, limits: { ...DEFAULTS, MaxNsSlopePct: 0 } }],
      ['slope-clear', undefined, { operation: 'slope-clear', expected_head: H }],
    ]
    for (const [operation, limits, sent] of cases) {
      const body = resultOf(operation, { limits: sent.limits })
      const post = answering(() => jsonResponse(body))
      const made = clientWith(post, { headers })
      const options = { drawingId: 'solar', projectId: 'p', operation, expectedHead: H }
      if (limits !== undefined) options.limits = limits
      expect(await made.client.runTerrainOperation(options)).toEqual({ ok: true, status: 200, value: normalize(body) })
      expect(post).toHaveBeenCalledTimes(1)
      const [url, init] = post.mock.calls[0]
      expect(url).toBe('https://studio.test/api/drawings/solar/terrain/operations?project_id=p')
      expect(init.method).toBe('POST')
      // The body is exactly the operation, the head it was given and, for slope only, all seven limits.
      expect(init.body).toBe(JSON.stringify(sent))
      expect(Object.keys(JSON.parse(init.body)))
        .toEqual(operation === 'slope' ? ['operation', 'expected_head', 'limits'] : ['operation', 'expected_head'])
      if (operation === 'slope') expect(Object.keys(JSON.parse(init.body).limits)).toEqual(Object.keys(DEFAULTS))
      expect(byteLength(init.body)).toBeLessThanOrEqual(TERRAIN_REQUEST_MAX_BYTES)
      expect(Object.keys(init.headers).filter((name) => /^content-type$/i.test(name))).toEqual(['Content-Type'])
      expect(init.headers).toEqual({
        'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t', 'Content-Type': 'application/json',
      })
      expect(Object.keys(init.headers).some((name) => /checkout|guest|content-length/i.test(name))).toBe(false)
      expect(made.onResponse.mock.calls[0][2]).toBe('Bearer t')
    }
    // A request without a project sends no query.
    const bare = answering(() => jsonResponse(resultOf('mesh')))
    expect((await clientWith(bare).client.runTerrainOperation({ drawingId: 'solar', operation: 'mesh', expectedHead: H })).ok)
      .toBe(true)
    expect(bare.mock.calls[0][0]).toBe('https://studio.test/api/drawings/solar/terrain/operations')
    // Headers are read afresh for every request, so a new bearer is the one sent and reported.
    let bearer = 'Bearer first'
    const rotating = clientWith(answering(() => jsonResponse(V2())), { headers: () => ({ Authorization: bearer }) })
    await rotating.client.getTerrain(VIEW_ARGS)
    bearer = 'Bearer second'
    await rotating.client.getTerrain(VIEW_ARGS)
    expect(rotating.onResponse.mock.calls.map((call) => call[2])).toEqual(['Bearer first', 'Bearer second'])
    expect(TERRAIN_REQUEST_MAX_BYTES).toBe(8_192)
    expect(TERRAIN_RESPONSE_MAX_BYTES).toBe(65_536)
    expect(TERRAIN_TIMEOUT_MS).toBe(120_000)
    expect(TERRAIN_MAX_TIMEOUT_MS).toBe(600_000)
    expect(TERRAIN_MAX_API_BASE_LENGTH).toBe(2_048)
  })

  it('W20-06a C2 route codes follow the server refusal map, and every refusal keeps its status and retry flag', async () => {
    const rows = routeMap()
    expect(rows.size).toBe(36)
    expect(Object.keys(TERRAIN_ROUTE_REASONS).sort()).toEqual([...rows.keys()].sort())
    expect(Object.keys(TERRAIN_CLIENT_REASONS).sort()).toEqual(CLIENT_AND_SESSION_CODES)
    expect(Object.keys(TERRAIN_CLIENT_REASONS).filter((code) => rows.has(code))).toEqual([])
    expect(Object.isFrozen(TERRAIN_ROUTE_REASONS)).toBe(true)
    expect(Object.isFrozen(TERRAIN_CLIENT_REASONS)).toBe(true)
    for (const sentence of [...Object.values(TERRAIN_ROUTE_REASONS), ...Object.values(TERRAIN_CLIENT_REASONS)]) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence.length).toBeLessThanOrEqual(120)
      for (const mark of [';', EN_DASH, EM_DASH]) expect(sentence).not.toContain(mark)
      expect(sentence.endsWith('.')).toBe(false)
      // No sentence claims compliance, a design revision, or what the server did not write.
      expect(sentence).not.toMatch(/ASCE|complian|certif|revision|nothing was (saved|written|changed)/i)
    }
    for (const [code, row] of rows) {
      expect(['BAD_PARAMS', 'INTERNAL']).toContain(row.envelope)
      const body = errorEnvelope(row.envelope, code, row.retryable, code)
      for (const [, call] of METHODS) {
        const { client } = clientWith(answering(() => jsonResponse(body, row.status)))
        expect(await call(client)).toEqual(refused(row.status, code, row.retryable))
      }
      expect(terrainReason(code)).toBe(TERRAIN_ROUTE_REASONS[code])
    }
    // The answers of the gates in front of the route, which carry prose and no reason code.
    const session = [
      [UNAUTH, 401, refused(401, 'UNAUTHENTICATED', false)],
      [GUEST, 403, refused(403, 'FORBIDDEN', false)],
      [errorEnvelope('FORBIDDEN', 'verified subject has no active platform identity binding', false), 403,
        refused(403, 'FORBIDDEN', false)],
      [DENIED, 403, refused(403, 'ENTITLEMENT_REQUIRED', false)],
      [POLICY, 503, refused(503, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)],
      [errorEnvelope('INTERNAL', 'platform identity binding authority is unavailable', true), 503,
        refused(503, 'INTERNAL', true)],
      [errorEnvelope('INTERNAL', 'verified tenant claim conflicts with the active platform binding', false), 409,
        refused(409, 'INTERNAL', false)],
      [errorEnvelope('INTERNAL', 'internal server error (error_id: 0123456789abcdef)', false), 500,
        refused(500, 'INTERNAL', false)],
      [errorEnvelope('BAD_PARAMS', '[{loc: [query, project_id], msg: field required, type: missing}]', false), 422,
        refused(422, 'BAD_PARAMS', false)],
      // The same bodies at a status their gate never uses are unreadable answers.
      [DENIED, 400, refused(400, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)],
      [GUEST, 400, refused(400, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)],
      [errorEnvelope('BAD_PARAMS', 'validation', false), 500, refused(500, 'TERRAIN_CLIENT_RESPONSE_INVALID', true)],
      [errorEnvelope('BAD_PARAMS', 'bad code', false, 'bad code'), 400, refused(400, 'BAD_PARAMS', false)],
      [{ error: { reason_code: 'bad code' } }, 400, refused(400, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)],
      // A code this page has no sentence for still passes through, bounded.
      [errorEnvelope('BAD_PARAMS', 'TERRAIN_FUTURE_CODE', false, 'TERRAIN_FUTURE_CODE'), 400,
        refused(400, 'TERRAIN_FUTURE_CODE', false)],
    ]
    for (const [body, status, expected] of session) {
      for (const [, call] of METHODS) {
        const { client } = clientWith(answering(() => jsonResponse(body, status)))
        expect(await call(client)).toEqual(expected)
      }
    }
    const gateway = clientWith(answering(() => textResponse('<html>Bad gateway</html>', 502)))
    expect(await gateway.client.getTerrain(VIEW_ARGS)).toEqual(refused(502, 'TERRAIN_CLIENT_RESPONSE_INVALID', true))
    const created = clientWith(answering(() => jsonResponse(V2(), 201)))
    expect(await created.client.getTerrain(VIEW_ARGS)).toEqual(refused(201, 'TERRAIN_CLIENT_RESPONSE_INVALID', false))
    // terrainReason gives the sentence, or a bounded fallback that never echoes a malformed value.
    for (const code of CLIENT_AND_SESSION_CODES) expect(terrainReason(code)).toBe(TERRAIN_CLIENT_REASONS[code])
    expect(terrainReason('TERRAIN_HEAD_MOVED'))
      .toBe('The terrain changed, so review the refreshed preview before running again')
    expect(TERRAIN_FALLBACK_SENTENCE).toBe('The terrain request stopped')
    expect(terrainReason('TERRAIN_FUTURE_CODE')).toBe('The terrain request stopped (TERRAIN_FUTURE_CODE)')
    for (const value of ['bad code', 'A'.repeat(65), null, undefined, 7, 'constructor', 'toString', 'hasOwnProperty']) {
      expect(terrainReason(value)).toBe('The terrain request stopped')
    }
  })

  it('W20-06a C3 a request the route would refuse is refused here and nothing is sent', async () => {
    const operations = [
      [{ ...MESH_ARGS, expectedHead: undefined }, 'TERRAIN_EXPECTED_HEAD_INVALID'],
      [{ drawingId: 'solar', operation: 'mesh' }, 'TERRAIN_EXPECTED_HEAD_INVALID'],
      [{ ...MESH_ARGS, expectedHead: 'A'.repeat(64) }, 'TERRAIN_EXPECTED_HEAD_INVALID'],
      [{ ...MESH_ARGS, expectedHead: 'a'.repeat(63) }, 'TERRAIN_EXPECTED_HEAD_INVALID'],
      [{ ...MESH_ARGS, expectedHead: 0 }, 'TERRAIN_EXPECTED_HEAD_INVALID'],
      [{ ...MESH_ARGS, operation: 'piles' }, 'TERRAIN_OPERATION_INVALID'],
      [{ ...MESH_ARGS, operation: 'Mesh' }, 'TERRAIN_OPERATION_INVALID'],
      [{ ...MESH_ARGS, operation: undefined }, 'TERRAIN_OPERATION_INVALID'],
      [{ ...MESH_ARGS, operation: ['mesh'] }, 'TERRAIN_OPERATION_INVALID'],
      // Limits belong to slope alone, and the route checks that before the operation itself.
      [{ ...MESH_ARGS, limits: DEFAULTS }, 'TERRAIN_BODY_INVALID'],
      [{ ...MESH_ARGS, limits: null }, 'TERRAIN_BODY_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope-clear', limits: {} }, 'TERRAIN_BODY_INVALID'],
      [{ ...MESH_ARGS, operation: 'piles', limits: {} }, 'TERRAIN_BODY_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { Columns: true } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { Columns: 1.5 } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxAxialSlopePct: '8.5' } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxAxialSlopePct: NaN } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxAxialSlopePct: Infinity } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxRowToRowSlopeDeg: 90.5 } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxSlopePercent: -1 } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { Rows: 1 } }, 'TERRAIN_LIMITS_INVALID'],
      [{ ...MESH_ARGS, operation: 'slope', limits: [] }, 'TERRAIN_LIMITS_INVALID'],
      // An input key this client does not know is never dropped silently.
      [{ ...MESH_ARGS, expected_head: H }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
      [{ ...MESH_ARGS, checkout: 'held' }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
      [{ ...MESH_ARGS, signal: {} }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
      [{ ...MESH_ARGS, projectId: '' }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ ...MESH_ARGS, projectId: 'p'.repeat(101) }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ ...MESH_ARGS, projectId: 7 }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ ...MESH_ARGS, projectId: '\ud800' }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ ...MESH_ARGS, drawingId: 'Bad!' }, 'TERRAIN_DRAWING_ID_INVALID'],
      [{ ...MESH_ARGS, drawingId: 'x'.repeat(64) }, 'TERRAIN_DRAWING_ID_INVALID'],
      [{ ...MESH_ARGS, drawingId: undefined }, 'TERRAIN_DRAWING_ID_INVALID'],
    ]
    for (const [options, code] of operations) {
      const fetchImpl = vi.fn()
      const { client } = clientWith(fetchImpl)
      expect(await client.runTerrainOperation(options)).toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    const views = [
      [{ drawingId: 'Bad!' }, 'TERRAIN_DRAWING_ID_INVALID'],
      [{ drawingId: undefined }, 'TERRAIN_DRAWING_ID_INVALID'],
      [{ drawingId: 'solar', projectId: '' }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ drawingId: 'solar', projectId: '\ud800' }, 'TERRAIN_PROJECT_ID_INVALID'],
      [{ drawingId: 'solar', operation: 'mesh' }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
      [{ drawingId: 'solar', signal: {} }, 'TERRAIN_CLIENT_REQUEST_INVALID'],
    ]
    for (const [options, code] of views) {
      const fetchImpl = vi.fn()
      const { client } = clientWith(fetchImpl)
      expect(await client.getTerrain(options)).toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    // Arguments that throw or are not objects resolve; neither method ever rejects.
    const fetchImpl = vi.fn()
    const { client } = clientWith(fetchImpl)
    const throwing = { get drawingId() { throw new Error('getter') } }
    const trapped = new Proxy({}, { get() { throw new Error('trap') } })
    for (const options of [null, 7, 'x', throwing, trapped, [MESH_ARGS]]) {
      await expect(client.getTerrain(options)).resolves.toEqual(refused(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false))
      await expect(client.runTerrainOperation(options)).resolves
        .toEqual(refused(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false))
    }
    await expect(client.getTerrain()).resolves.toEqual(refused(null, 'TERRAIN_DRAWING_ID_INVALID', false))
    await expect(client.runTerrainOperation()).resolves.toEqual(refused(null, 'TERRAIN_DRAWING_ID_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
    // The positive control: the same checks at their edges send the request.
    const notFound = errorEnvelope('BAD_PARAMS', 'TERRAIN_DRAWING_NOT_FOUND', false, 'TERRAIN_DRAWING_NOT_FOUND')
    const edges = [
      [{ ...MESH_ARGS, projectId: 'p'.repeat(100) }, `?project_id=${'p'.repeat(100)}`],
      [{ ...MESH_ARGS, projectId: '\u{1F600}'.repeat(100) }, `?project_id=${'%F0%9F%98%80'.repeat(100)}`],
      [{ ...MESH_ARGS, drawingId: 'a'.repeat(63) }, `/api/drawings/${'a'.repeat(63)}/terrain/operations?`],
      [{ ...MESH_ARGS, limits: undefined }, '/terrain/operations?project_id=p'],
      [{ ...MESH_ARGS, operation: 'slope', limits: { MaxAxialSlopePct: 1000, MaxRowToRowSlopeDeg: 90, Columns: 10000 } },
        '/terrain/operations?project_id=p'],
    ]
    for (const [options, fragment] of edges) {
      const sent = answering(() => jsonResponse(notFound, 404))
      expect(await clientWith(sent).client.runTerrainOperation(options))
        .toEqual(refused(404, 'TERRAIN_DRAWING_NOT_FOUND', false))
      expect(sent).toHaveBeenCalledTimes(1)
      expect(sent.mock.calls[0][0]).toContain(fragment)
    }
  })

  it('W20-06a C4 the response bound is 65,536 bytes, counted in bytes while reading, and an overflow is cancelled', async () => {
    const text = JSON.stringify(V2())
    const success = { ok: true, status: 200, value: normalize(V2()) }
    const invalid = refused(200, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)
    const padded = (bytes) => new TextEncoder().encode(`${text}${' '.repeat(bytes - byteLength(text))}`)
    expect(padded(65_536).byteLength).toBe(TERRAIN_RESPONSE_MAX_BYTES)
    const fits = clientWith(answering(() => new Response(padded(65_536), { status: 200 })))
    expect(await fits.client.getTerrain(VIEW_ARGS)).toEqual(success)
    const over = clientWith(answering(() => new Response(padded(65_537), { status: 200 })))
    expect(await over.client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    // By chunks: the read that crosses the bound is the last one, and the body is cancelled.
    const chunked = (chunks) => {
      let index = 0
      const reader = {
        read: vi.fn(async () => {
          if (index >= chunks.length) return { done: true, value: undefined }
          index += 1
          return { done: false, value: chunks[index - 1] }
        }),
        cancel: vi.fn(async () => {}),
      }
      return { reader, response: { status: 200, headers: new Headers(), body: { getReader: () => reader } } }
    }
    const head = new TextEncoder().encode(text)
    const pad = new Uint8Array(65_536 - head.byteLength).fill(0x20)
    const space = () => new Uint8Array([0x20])
    const exact = chunked([head, pad])
    expect(await clientWith(answering(() => exact.response)).client.getTerrain(VIEW_ARGS)).toEqual(success)
    expect(exact.reader.read).toHaveBeenCalledTimes(3)
    expect(exact.reader.cancel).not.toHaveBeenCalled()
    const flood = chunked([head, pad, space(), space(), space()])
    expect(await clientWith(answering(() => flood.response)).client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    expect(flood.reader.read).toHaveBeenCalledTimes(3)
    expect(flood.reader.cancel).toHaveBeenCalledTimes(1)
    // By declared length: refused before a reader is taken, and the body is released.
    const declared = { status: 200, headers: new Headers({ 'content-length': '65537' }), body: { cancel: vi.fn(), getReader: vi.fn() } }
    expect(await clientWith(answering(() => declared)).client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    expect(declared.body.cancel).toHaveBeenCalledTimes(1)
    expect(declared.body.getReader).not.toHaveBeenCalled()
    // A content length with leading zeros is read as its value.
    const zeros = '0'.repeat(17)
    const honest = clientWith(answering(() => textResponse(text, 200, { 'Content-Length': zeros + String(byteLength(text)) })))
    expect(await honest.client.getTerrain(VIEW_ARGS)).toEqual(success)
    const inflated = clientWith(answering(() => textResponse(text, 200, { 'Content-Length': `${zeros}65537` })))
    expect(await inflated.client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    // Bytes, not string length: a stale stored record of 33,000 two-byte characters is under
    // 65,536 UTF-16 units and over 65,536 bytes.
    const wide = (count) => JSON.stringify(viewOf({ mesh: { state: 'stale', record: E_ACUTE.repeat(count) } }))
    expect(wide(33_000).length).toBeLessThan(65_536)
    expect(byteLength(wide(33_000))).toBeGreaterThan(65_536)
    for (const answer of [() => bodiless(wide(33_000)), () => textResponse(wide(33_000))]) {
      expect(await clientWith(answering(answer)).client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    }
    for (const answer of [() => bodiless(wide(100)), () => textResponse(wide(100))]) {
      expect((await clientWith(answering(answer)).client.getTerrain(VIEW_ARGS)).ok).toBe(true)
    }
    // Malformed UTF-8 is refused, streamed or already decoded into a replacement character.
    const badUtf8 = clientWith(answering(() => new Response(new Uint8Array([0x7b, 0xff, 0x7d]), { status: 200 })))
    expect(await badUtf8.client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    const replaced = JSON.stringify(viewOf({ mesh: { state: 'stale', record: REPLACEMENT_CHARACTER } }))
    expect(await clientWith(answering(() => bodiless(replaced))).client.getTerrain(VIEW_ARGS)).toEqual(invalid)
    const [prefix, suffix] = replaced.split(REPLACEMENT_CHARACTER)
    const broken = new Uint8Array([...new TextEncoder().encode(prefix), 0xff, ...new TextEncoder().encode(suffix)])
    expect(await clientWith(answering(() => new Response(broken, { status: 200 }))).client.getTerrain(VIEW_ARGS))
      .toEqual(invalid)
    // The bound holds for an operation answer and for a refusal body too.
    const result = JSON.stringify(resultOf('mesh'))
    const bigResult = clientWith(answering(() => new Response(
      new TextEncoder().encode(`${result}${' '.repeat(65_537 - byteLength(result))}`), { status: 200 })))
    expect(await bigResult.client.runTerrainOperation(MESH_ARGS)).toEqual(invalid)
    const moved = JSON.stringify(errorEnvelope('BAD_PARAMS', 'TERRAIN_HEAD_MOVED', true, 'TERRAIN_HEAD_MOVED'))
    const bigRefusal = clientWith(answering(() => textResponse(`${moved}${' '.repeat(65_537 - byteLength(moved))}`, 409)))
    expect(await bigRefusal.client.runTerrainOperation(MESH_ARGS))
      .toEqual(refused(409, 'TERRAIN_CLIENT_RESPONSE_INVALID', false))
    const bigOutage = clientWith(answering(() => textResponse('x'.repeat(65_537), 503)))
    expect(await bigOutage.client.runTerrainOperation(MESH_ARGS))
      .toEqual(refused(503, 'TERRAIN_CLIENT_RESPONSE_INVALID', true))
  })

  it('W20-06a C5 one deadline covers the whole call: a success never leaves a call that ran out of time or was aborted', async () => {
    for (const [, call, body] of METHODS) {
      const text = JSON.stringify(body)
      const success = { ok: true, status: 200, value: normalize(body) }
      const late = refused(200, 'TERRAIN_CLIENT_TIMEOUT', true)
      const cancelled = refused(200, 'TERRAIN_CLIENT_ABORTED', false)
      // The bound, on a clock the row controls: one millisecond inside the budget is a success (the
      // positive control, through the same reader) and the budget itself is not.
      vi.useFakeTimers()
      try {
        const within = clientWith(slowAssembly(text, 200, () => vi.advanceTimersByTime(999)), { timeoutMs: 1000 })
        expect(await call(within.client)).toEqual(success)
        expect(vi.getTimerCount()).toBe(0)
        const atBudget = clientWith(slowAssembly(text, 200, () => vi.advanceTimersByTime(1000)), { timeoutMs: 1000 })
        expect(await call(atBudget.client)).toEqual(late)
        expect(vi.getTimerCount()).toBe(0)
        // A stopped call leaves no timer behind either.
        const pending = vi.fn(() => new Promise(() => {}))
        const caller = new AbortController()
        const stopped = call(clientWith(pending, { timeoutMs: 1000 }).client, caller.signal)
        await vi.advanceTimersByTimeAsync(0)
        expect(pending).toHaveBeenCalledTimes(1)
        caller.abort()
        await expect(stopped).resolves.toEqual(refused(null, 'TERRAIN_CLIENT_ABORTED', false))
        await vi.advanceTimersByTimeAsync(0)
        expect(vi.getTimerCount()).toBe(0)
      } finally {
        vi.useRealTimers()
      }
      // The work after the last read is inside the deadline, on the real clock too.
      const slow = clientWith(slowAssembly(text, 200, () => spin(60)), { timeoutMs: 30 })
      expect(await settleWithin(call(slow.client))).toEqual(late)
      // A caller abort that lands in the same place, with the whole default budget left.
      const duringWork = new AbortController()
      const aborted = clientWith(slowAssembly(text, 200, () => duringWork.abort()))
      expect(await settleWithin(call(aborted.client, duringWork.signal))).toEqual(cancelled)
      // The cleanup of the caller signal is inside the deadline: slow cleanup answers the deadline, and
      // an abort made during it, before or after the listener is removed, counts.
      const quick = () => answering(() => bodiless(text))
      expect(await settleWithin(call(clientWith(quick(), { timeoutMs: 30 }).client, wrappedSignal(() => spin(60)))))
        .toEqual(late)
      expect(await settleWithin(call(clientWith(quick()).client, wrappedSignal((caller) => caller.abort()))))
        .toEqual(cancelled)
      expect(await settleWithin(call(clientWith(quick()).client, wrappedSignal(() => {}, (caller) => caller.abort()))))
        .toEqual(cancelled)
      // Positive control: the same wrapped signal with a cleanup that does nothing succeeds.
      expect(await call(clientWith(quick()).client, wrappedSignal(() => {}))).toEqual(success)
      // A signal whose removeEventListener throws does not change the result.
      const sticky = { aborted: false, addEventListener() {}, removeEventListener() { throw new Error('boom') } }
      expect(await call(clientWith(quick()).client, sticky)).toEqual(success)
      // A request that never answers, and a body that never ends, settle at the deadline.
      expect(await settleWithin(call(clientWith(neverAnswers(), { timeoutMs: 30 }).client)))
        .toEqual(refused(null, 'TERRAIN_CLIENT_TIMEOUT', true))
      expect(await settleWithin(call(clientWith(answering(() => stalledResponse()), { timeoutMs: 30 }).client)))
        .toEqual(late)
      expect(await settleWithin(call(clientWith(answering(() => stalledResponse(409)), { timeoutMs: 30 }).client)))
        .toEqual(refused(409, 'TERRAIN_CLIENT_TIMEOUT', true))
      // A caller abort before the request, and after the headers, settles the call.
      const early = new AbortController()
      early.abort()
      const unsent = neverAnswers()
      expect(await settleWithin(call(clientWith(unsent).client, early.signal)))
        .toEqual(refused(null, 'TERRAIN_CLIENT_ABORTED', false))
      expect(unsent).not.toHaveBeenCalled()
      const midBody = new AbortController()
      const stalling = answering(() => {
        setTimeout(() => midBody.abort(), 5)
        return stalledResponse()
      })
      expect(await settleWithin(call(clientWith(stalling).client, midBody.signal))).toEqual(cancelled)
      // A body whose chunks are always ready settles at the deadline instead of being read to its end.
      const bytes = new TextEncoder().encode(text)
      let index = 0
      const ready = new ReadableStream({
        pull(controller) {
          if (index === 0) spin(60)
          if (index < bytes.length) {
            controller.enqueue(bytes.subarray(index, index + 1))
            index += 1
          } else {
            controller.close()
          }
        },
      }, { highWaterMark: 0 })
      const flooding = clientWith(answering(() => new Response(ready, { status: 200 })), { timeoutMs: 30 })
      expect(await settleWithin(call(flooding.client))).toEqual(late)
      expect(index).toBeLessThan(8)
    }
  })

  it('W20-06a C6 transport and answer failures resolve to their code, and a decided refusal is never replaced', async () => {
    const invalid = refused(200, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)
    const unsendable = refused(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
    const movedBody = errorEnvelope('BAD_PARAMS', 'TERRAIN_HEAD_MOVED', true, 'TERRAIN_HEAD_MOVED')
    const movedText = JSON.stringify(movedBody)
    const moved = refused(409, 'TERRAIN_HEAD_MOVED', true)
    for (const [, call, body] of METHODS) {
      const text = JSON.stringify(body)
      const success = { ok: true, status: 200, value: normalize(body) }
      const network = clientWith(vi.fn(async () => { throw new TypeError('Failed to fetch') }))
      await expect(call(network.client)).resolves.toEqual(refused(null, 'TERRAIN_CLIENT_NETWORK', true))
      const synchronous = clientWith(vi.fn(() => { throw new TypeError('Failed to fetch') }))
      await expect(call(synchronous.client)).resolves.toEqual(refused(null, 'TERRAIN_CLIENT_NETWORK', true))
      // Headers that cannot be read are an unsendable request, and nothing is sent.
      for (const headers of [() => { throw new Error('no session') }, () => ({ 'X-Tenant-Id': 7 }), () => null, () => []]) {
        const unused = vi.fn()
        await expect(call(clientWith(unused, { headers }).client)).resolves.toEqual(unsendable)
        expect(unused).not.toHaveBeenCalled()
      }
      // A response that cannot be read is an unreadable answer.
      const answers = [
        [() => ({ status: 200, headers: new Headers(), get body() { throw new Error('body') } }), invalid],
        [() => ({ get status() { throw new Error('status') }, headers: new Headers() }),
          refused(null, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)],
        [() => ({ status: '200', headers: new Headers() }), refused(null, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)],
        [() => ({ status: 200, headers: new Headers(), body: 'text' }), invalid],
        [() => ({ status: 200, headers: new Headers(), body: null }), invalid],
        [() => bodiless(7), invalid],
        [() => textResponse('not json'), invalid],
        [() => textResponse(''), invalid],
        [() => textResponse('[]'), invalid],
        [() => textResponse('null'), invalid],
        [() => textResponse(JSON.stringify({ ...body, ok: true })), invalid],
        [() => textResponse(JSON.stringify({ ...body, error: { reason_code: 'X' } })), invalid],
        [() => bodiless(text), success],
      ]
      for (const [answer, expected] of answers) {
        await expect(call(clientWith(answering(answer)).client)).resolves.toEqual(expected)
      }
      // The observer's failure never changes the result, success or refusal.
      const observer = () => { throw new Error('observer') }
      await expect(call(clientWith(answering(() => textResponse(text)), { onResponse: observer }).client))
        .resolves.toEqual(success)
      await expect(call(clientWith(answering(() => textResponse(movedText, 409)), { onResponse: observer }).client))
        .resolves.toEqual(moved)
      // A refusal already decided is returned as it is: late work and a late abort do not replace it.
      const lateWork = clientWith(slowAssembly(movedText, 409, () => spin(60)), { timeoutMs: 30 })
      expect(await settleWithin(call(lateWork.client))).toEqual(moved)
      const lateAbort = clientWith(answering(() => bodiless(movedText, 409)))
      expect(await settleWithin(call(lateAbort.client, wrappedSignal((caller) => caller.abort())))).toEqual(moved)
      const unreadable = clientWith(slowAssembly('{', 200, () => spin(60)), { timeoutMs: 30 })
      expect(await settleWithin(call(unreadable.client))).toEqual(invalid)
      // The caller abort listener is removed on every settle.
      const controller = new AbortController()
      const add = vi.spyOn(controller.signal, 'addEventListener')
      const remove = vi.spyOn(controller.signal, 'removeEventListener')
      for (const outcome of [() => textResponse(text), () => textResponse(movedText, 409), () => { throw new TypeError('Failed to fetch') }]) {
        await call(clientWith(vi.fn(async () => outcome())).client, controller.signal)
      }
      const added = add.mock.calls.filter(([type]) => type === 'abort').length
      expect(added).toBeGreaterThanOrEqual(3)
      expect(remove.mock.calls.filter(([type]) => type === 'abort').length).toBe(added)
      // A signal that throws while registering leaves no listener and sends nothing.
      const kept = []
      const dropped = []
      const hostile = {
        aborted: false,
        addEventListener(type, listener) { kept.push(listener); throw new Error('boom') },
        removeEventListener(type, listener) { dropped.push(listener) },
      }
      const unused = vi.fn()
      await expect(call(clientWith(unused).client, hostile)).resolves.toEqual(unsendable)
      expect(unused).not.toHaveBeenCalled()
      expect(kept).toHaveLength(1)
      expect(dropped).toContain(kept[0])
    }
    // A success that does not describe the request it answers is not a success.
    const scopes = [
      [(client) => client.getTerrain({ drawingId: 'other', projectId: 'p' }), V2()],
      [(client) => client.getTerrain({ drawingId: 'solar', projectId: 'q' }), V2()],
      [(client) => client.getTerrain(VIEW_ARGS), resultOf('mesh')],
      [(client) => client.runTerrainOperation({ ...MESH_ARGS, expectedHead: H2 }), resultOf('mesh')],
      [(client) => client.runTerrainOperation({ ...MESH_ARGS, projectId: 'q' }), resultOf('mesh')],
      [(client) => client.runTerrainOperation({ ...MESH_ARGS, operation: 'slope-clear' }), resultOf('mesh')],
      [(client) => client.runTerrainOperation({ ...MESH_ARGS, operation: 'slope', limits: { Columns: 3 } }), resultOf('slope')],
      [(client) => client.runTerrainOperation(MESH_ARGS), V2()],
    ]
    for (const [call, body] of scopes) {
      await expect(call(clientWith(answering(() => jsonResponse(body))).client)).resolves.toEqual(invalid)
    }
    // Creation guards: a client is made only from usable dependencies, and is closed.
    const fetchImpl = vi.fn()
    expect(() => createSolarTerrainClient()).toThrow(TypeError)
    expect(() => createSolarTerrainClient({ fetchImpl })).toThrow(TypeError)
    expect(() => createSolarTerrainClient({ headers: tenantHeaders })).toThrow(TypeError)
    expect(() => createSolarTerrainClient({ fetchImpl, headers: tenantHeaders, apiBase: 'x'.repeat(2049) })).toThrow(TypeError)
    expect(() => createSolarTerrainClient({ fetchImpl, headers: tenantHeaders, apiBase: 7 })).toThrow(TypeError)
    expect(() => createSolarTerrainClient({ fetchImpl, headers: tenantHeaders, onResponse: 'x' })).toThrow(TypeError)
    for (const timeoutMs of [0, 600_001, 1.5, '30', null]) {
      expect(() => createSolarTerrainClient({ fetchImpl, headers: tenantHeaders, timeoutMs })).toThrow(TypeError)
    }
    const made = createSolarTerrainClient({ fetchImpl, headers: tenantHeaders, timeoutMs: 600_000 })
    expect(Object.isFrozen(made)).toBe(true)
    expect(Object.keys(made)).toEqual(['getTerrain', 'runTerrainOperation'])
  })
})
