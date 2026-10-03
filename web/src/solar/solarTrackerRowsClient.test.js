import { afterEach, describe, it, expect, vi } from 'vitest'
import * as clientModule from './solarTrackerRowsClient.js'
import { TRACKER_ROWS_REASONS } from './solarTrackerRowsReasons.js'
import { validateTrackerRowsResult } from './solarTrackerRowsModel.js'

const { createSolarTrackerRowsClient, trackerRowsReason } = clientModule
const request = () => ({ operation: 'manual-create', rows: [{ axis_start: [0, 0], axis_end: [0, 6],
  cross_axis_width_du: 2, slots: 3 }], module_power_watts: 450, expected_head: null })
const options = (extra = {}) => ({ drawingId: 'solar', projectId: 'p', drawingUnits: 'm', request: request(), ...extra })
const domain = (retry = false) => ({
  schema: 'leaf.solar-tracker-rows.v1', operation: 'manual-create', outcome: retry ? 'retry' : 'published',
  created: !retry, drawing_id: 'solar', project_id: 'p', expected_head: null,
  head: { schema: 'leaf.solar-physical-head.v1', drawing_id: 'solar', project_id: 'p', index: 0, parent: null,
    state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: 'a'.repeat(64), media_type: 'application/json',
      filename: 'physical-state.json', byte_length: 1024, content_sha256: 'b'.repeat(64), source_version: 1,
      download: `/api/drawings/solar/artifacts/${'a'.repeat(64)}` } },
  frame: { coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1,
    crs: 'none', elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres' },
  summary: { rows: 1, slots: 3, module_power_watts: 450 },
  terrain_standing: { schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: null,
    frames: { state: 'absent', checked: 0, stale: 0 }, piles: { state: 'absent', checked: 0, stale: 0 } },
})
const envelope = (retry = false) => ({ ...domain(retry), error: null, degraded_mode: false })
const wire = (value = envelope(), status = 201) => ({ status, body: null,
  headers: { get: () => null }, text: async () => JSON.stringify(value) })
const failure = (status, suffix, retryable = false) => ({ ok: false, status,
  code: suffix.startsWith('TRACKER_ROWS_') || !suffix.startsWith('CLIENT_') ? suffix : `TRACKER_ROWS_${suffix}`, retryable })
const responseFailure = (status = 201, retryable = false) => failure(status, 'CLIENT_RESPONSE_INVALID', retryable)
const make = (fetchImpl = vi.fn(async () => wire()), extra = {}) => createSolarTrackerRowsClient({ fetchImpl, headers: () => ({}), ...extra })
const send = (answer, status = 201, extraOptions = {}) => make(async () => wire(answer, status)).createTrackerRows(options(extraOptions))
const never = () => new Promise(() => {})
const flush = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve() }

function streamed(chunks, status = 201, length = null) {
  let index = 0
  const reader = { read: vi.fn(async () => index < chunks.length ? { done: false, value: chunks[index++] } : { done: true }),
    cancel: vi.fn(async () => {}) }
  const body = { getReader: vi.fn(() => reader), cancel: vi.fn(async () => {}) }
  return { response: { status, body, headers: { get: (name) => name === 'content-length' ? length : null } }, reader, body }
}

function controlledSignal(remove) {
  let listener
  const signal = { aborted: false,
    addEventListener: vi.fn((name, callback) => { listener = callback }),
    removeEventListener: vi.fn((name, callback) => { if (remove) remove(signal, () => { if (listener === callback) listener = null })
      else if (listener === callback) listener = null }),
    abort() { signal.aborted = true; listener?.() },
  }
  return signal
}

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

describe('tracker rows client', () => {
  it('c01_exports_and_constants', () => {
    expect([clientModule.TRACKER_ROWS_RESPONSE_MAX_BYTES, clientModule.TRACKER_ROWS_TIMEOUT_MS,
      clientModule.TRACKER_ROWS_MAX_TIMEOUT_MS, clientModule.TRACKER_ROWS_MAX_API_BASE_LENGTH]).toEqual([65536, 120000, 600000, 2048])
    expect(clientModule.TRACKER_ROWS_FALLBACK_SENTENCE).toBe('The tracker row request stopped')
    expect(Object.isFrozen(clientModule.TRACKER_ROWS_CLIENT_REASONS)).toBe(true)
    expect(Object.keys(clientModule.TRACKER_ROWS_CLIENT_REASONS)).toHaveLength(11)
    for (const [code, sentence] of [
      ['UNAUTHENTICATED', 'Sign in again to create tracker rows'],
      ['FORBIDDEN', 'This session cannot create tracker rows'],
      ['ENTITLEMENT_REQUIRED', 'Your workspace plan does not include creating tracker rows'],
      ['ENTITLEMENT_POLICY_UNAVAILABLE', 'The workspace policy could not be read, so this action stays off'],
      ['INTERNAL', 'The server could not complete this tracker row request'],
      ['BAD_PARAMS', 'The server could not read this tracker row request'],
      ['TRACKER_ROWS_CLIENT_REQUEST_INVALID', 'This tracker row action was given input it cannot send'],
      ['TRACKER_ROWS_CLIENT_TIMEOUT', 'The server did not answer in time, so refresh the physical state'],
      ['TRACKER_ROWS_CLIENT_NETWORK', 'The server could not be reached, so refresh before trying again'],
      ['TRACKER_ROWS_CLIENT_ABORTED', 'This tracker row request was cancelled here'],
      ['TRACKER_ROWS_CLIENT_RESPONSE_INVALID', 'The server answer could not be read, so refresh the physical state'],
    ]) expect(trackerRowsReason(code)).toBe(sentence)
    const client = make()
    expect(Object.keys(client)).toEqual(['createTrackerRows'])
    expect(Object.isFrozen(client)).toBe(true)
  })

  it('c02_constructor_guards', () => {
    expect(() => createSolarTrackerRowsClient()).toThrow(TypeError)
    for (const extra of [{ fetchImpl: null }, { headers: {} }, { apiBase: 1 }, { apiBase: 'x'.repeat(2049) },
      { onResponse: true }, { timeoutMs: 0 }, { timeoutMs: 600001 }, { timeoutMs: 1.5 }]) {
      expect(() => make(undefined, extra)).toThrow(TypeError)
    }
    expect(() => make(undefined, { timeoutMs: 600000, apiBase: 'x'.repeat(2048) })).not.toThrow()
  })

  it('c03_publication_201', async () => {
    expect(await send(envelope())).toEqual({ ok: true, status: 201, value: domain() })
  })

  it('c04_retry_200_same_head', async () => {
    expect(await send(envelope(true), 200)).toEqual({ ok: true, status: 200, value: domain(true) })
    const head = 'c'.repeat(64)
    const answer = envelope(true)
    answer.expected_head = head
    answer.head.index = 2
    answer.head.parent = head
    const sent = { ...request(), expected_head: head }
    expect((await send(answer, 200, { request: sent })).value.head).toEqual(answer.head)
    answer.head.parent = 'd'.repeat(64)
    expect(await send(answer, 200, { request: sent })).toEqual(responseFailure(200))
  })

  it('c05_exact_url_and_four_key_body', async () => {
    const fetch = vi.fn(async () => wire())
    await make(fetch, { apiBase: '/base' }).createTrackerRows(options())
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][0]).toBe('/base/api/drawings/solar/tracker-rows?project_id=p')
    expect(fetch.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(request())
    expect(Object.keys(JSON.parse(fetch.mock.calls[0][1].body))).toHaveLength(4)
    const unicode = 'p /\u{1f331}'
    const answer = envelope()
    answer.project_id = unicode
    answer.head.project_id = unicode
    const encodedFetch = vi.fn(async () => wire(answer))
    expect((await make(encodedFetch).createTrackerRows(options({ projectId: unicode }))).ok).toBe(true)
    expect(encodedFetch.mock.calls[0][0]).toBe(`/api/drawings/solar/tracker-rows?project_id=${encodeURIComponent(unicode)}`)
    const noProject = vi.fn(async () => wire())
    await make(noProject).createTrackerRows(options({ projectId: null }))
    expect(noProject.mock.calls[0][0]).toBe('/api/drawings/solar/tracker-rows')
  })

  it('c06_capability_present_and_absent', async () => {
    for (const proof of [undefined, ' opaque  proof ']) {
      const fetch = vi.fn(async () => wire())
      const headers = () => proof === undefined ? {} : { 'X-Checkout-Capability': proof }
      expect((await make(fetch, { headers }).createTrackerRows(options())).ok).toBe(true)
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(fetch.mock.calls[0][1].headers['X-Checkout-Capability']).toBe(proof)
      expect(Object.keys(JSON.parse(fetch.mock.calls[0][1].body))).toEqual(Object.keys(request()))
    }
  })

  it('c07_fresh_headers_and_response_observer', async () => {
    let token = 'first'
    const response = wire()
    const headers = vi.fn((id) => ({ authorization: token, Drawing: id }))
    const observer = vi.fn()
    const fetch = vi.fn(async () => response)
    const client = make(fetch, { headers, onResponse: observer })
    await client.createTrackerRows(options())
    token = 'second'
    await client.createTrackerRows(options())
    expect(headers.mock.calls).toEqual([['solar'], ['solar']])
    expect(observer.mock.calls).toEqual([[response, '/api/drawings/solar/tracker-rows?project_id=p', 'first'],
      [response, '/api/drawings/solar/tracker-rows?project_id=p', 'second']])
  })

  it('c08_null_prototype_headers_and_content_type', async () => {
    const source = Object.create(null)
    Object.assign(source, { __proto__: 'ignored by object literal', 'content-type': 'text/plain', 'CONTENT-TYPE': 'stale' })
    source.__proto__ = 'own header'
    const fetch = vi.fn(async () => wire())
    await make(fetch, { headers: () => source }).createTrackerRows(options())
    const sent = fetch.mock.calls[0][1].headers
    expect(Object.getPrototypeOf(sent)).toBeNull()
    expect(Object.hasOwn(sent, '__proto__')).toBe(true)
    expect(sent.__proto__).toBe('own header')
    expect(Object.keys(sent).filter((key) => key.toLowerCase() === 'content-type')).toEqual(['Content-Type'])
    expect(sent['Content-Type']).toBe('application/json')
    expect(source['content-type']).toBe('text/plain')
  })

  it('c09_all_30_route_refusals_and_sentences', async () => {
    const rows = [
      ['OPERATION_UNSUPPORTED', 400, false], ['REQUEST_INVALID', 400, false], ['ROWS_INVALID', 400, false],
      ['ROW_INVALID', 400, false], ['POWER_INVALID', 400, false], ['HEAD_INVALID', 400, false],
      ['UNITS_UNSUPPORTED', 409, false], ['UNITS_MISMATCH', 409, false], ['FRAME_UNSUPPORTED', 409, false],
      ['SLOTS_INVALID', 400, false], ['AXIS_INVALID', 400, false], ['WIDTH_INVALID', 400, false],
      ['LIMIT_EXCEEDED', 413, false], ['PROJECT_ID_INVALID', 400, false], ['PROJECT_MISMATCH', 409, false],
      ['DRAWING_NOT_FOUND', 404, false], ['GRAPH_REQUIRED', 409, false], ['GROUND_REQUIRED', 409, false],
      ['GRAPH_CONVERTED', 409, false], ['DEPENDENT_STATE', 409, false], ['ALREADY_EXISTS', 409, false],
      ['STALE_HEAD', 409, true], ['STATE_INVALID', 500, false], ['WRITES_DRAINED', 503, true],
      ['STORE_UNAVAILABLE', 503, true], ['STORE_UNSAFE', 500, false], ['LOG_FULL', 409, false],
      ['CHECKOUT_REQUIRED', 403, false], ['CHECKOUT_UNAVAILABLE', 503, true], ['CONTENT_TYPE_UNSUPPORTED', 415, false],
    ]
    expect(rows).toHaveLength(30)
    expect(Object.keys(TRACKER_ROWS_REASONS)).toHaveLength(30)
    for (const [suffix, status, retryable] of rows) {
      const code = `TRACKER_ROWS_${suffix}`
      expect(await send({ error: { reason_code: code, retryable, message: 'private', next_action: 'private' } }, status))
        .toEqual(failure(status, code, retryable))
      expect(trackerRowsReason(code)).toBe(TRACKER_ROWS_REASONS[code])
      expect(trackerRowsReason(code)).not.toContain('private')
    }
  })

  it('c10_unknown_and_malformed_reason_codes', async () => {
    const code = 'TRACKER_ROWS_FUTURE_CODE'
    expect(await send({ error: { reason_code: code, retryable: true } }, 409)).toEqual(failure(409, code, true))
    expect(trackerRowsReason(code)).toBe(`The tracker row request stopped (${code})`)
    for (const code of ['bad code', 'A'.repeat(65), '', null, {}, 'lowercase']) {
      expect(trackerRowsReason(code)).toBe('The tracker row request stopped')
      expect(await send({ error: { reason_code: code, retryable: true } }, 400)).toEqual(responseFailure(400))
    }
    expect(await send({ error: { reason_code: 'TRACKER_ROWS_STALE_HEAD', retryable: 1 }, entitlement_required: true }, 403))
      .toEqual(failure(403, 'TRACKER_ROWS_STALE_HEAD', false))
  })

  it('c11_platform_and_entitlement_refusals', async () => {
    const cases = [[401, {}, 'UNAUTHENTICATED', false], [403, { error: { error_code: 'FORBIDDEN' } }, 'FORBIDDEN', false],
      [403, { entitlement_required: true }, 'ENTITLEMENT_REQUIRED', false],
      [503, { entitlement_required: true }, 'ENTITLEMENT_POLICY_UNAVAILABLE', true],
      [503, { error: { error_code: 'INTERNAL', retryable: true } }, 'INTERNAL', true],
      [422, { error: { error_code: 'BAD_PARAMS' } }, 'BAD_PARAMS', false]]
    for (const [status, value, code, retryable] of cases) expect(await send(value, status)).toEqual(failure(status, code, retryable))
    expect(await make(async () => ({ status: 401, get body() { throw new Error('private') } })).createTrackerRows(options()))
      .toEqual(failure(401, 'UNAUTHENTICATED'))
  })

  it('c12_status_outcome_created_agreement', async () => {
    expect(await send(envelope(), 200)).toEqual(responseFailure(200))
    expect(await send(envelope(true), 201)).toEqual(responseFailure())
    expect(await send(envelope(), 202)).toEqual(responseFailure(202))
    for (const [key, value] of [['created', 1], ['outcome', 'other']]) {
      expect(await send({ ...envelope(), [key]: value })).toEqual(responseFailure())
    }
  })

  it('c13_closed_success_shape', async () => {
    for (const key of Object.keys(envelope())) {
      const value = envelope()
      delete value[key]
      expect(await send(value)).toEqual(responseFailure())
    }
    for (const value of [{ ...envelope(), extra: 1 }, { ...envelope(), error: {} },
      { ...envelope(), degraded_mode: true }, { ...envelope(), schema: 'other' }]) {
      expect(await send(value)).toEqual(responseFailure())
    }
    for (const path of ['head', 'frame', 'summary', 'terrain_standing']) {
      const value = envelope()
      value[path].extra = 1
      expect(await send(value)).toEqual(responseFailure())
    }
    for (const path of [['head'], ['head', 'state'], ['frame'], ['summary'], ['terrain_standing'],
      ['terrain_standing', 'frames'], ['terrain_standing', 'piles']]) {
      const sample = path.reduce((value, key) => value[key], envelope())
      for (const key of Object.keys(sample)) {
        const value = envelope()
        const nested = path.reduce((current, part) => current[part], value)
        delete nested[key]
        expect(await send(value)).toEqual(responseFailure())
      }
    }
  })

  it('c14_request_scope_and_summary_binding', async () => {
    for (const alter of [(v) => { v.drawing_id = 'wrong' }, (v) => { v.project_id = 'wrong' },
      (v) => { v.head.project_id = 'wrong' }, (v) => { v.expected_head = 'c'.repeat(64) },
      (v) => { v.summary.rows = 2 }, (v) => { v.summary.slots = 4 }, (v) => { v.summary.module_power_watts = 451 }]) {
      const value = envelope()
      alter(value)
      expect(await send(value)).toEqual(responseFailure())
    }
    expect((await send(envelope(), 201, { projectId: null })).ok).toBe(true)
    const sent = request()
    const fetch = vi.fn(async () => { sent.rows[0].slots = 4; return wire() })
    expect((await make(fetch).createTrackerRows(options({ request: sent }))).ok).toBe(true)
    expect(JSON.parse(fetch.mock.calls[0][1].body).rows[0].slots).toBe(3)
  })

  it('c15_head_and_artifact_reference_validation', async () => {
    const mutations = [(v) => { v.head.extra = 1 }, (v) => { v.head.drawing_id = 'other' },
      (v) => { v.head.schema = 'other' }, (v) => { v.head.index = 4096 }, (v) => { v.head.index = -1 },
      (v) => { v.head.index = true }, (v) => { v.head.index = 1 }, (v) => { v.head.parent = 'c'.repeat(64) }]
    for (const [key, bad] of [['schema', 'other'], ['artifact_id', 'A'.repeat(64)], ['content_sha256', 'bad'],
      ['media_type', 'text/plain'], ['filename', 'other.json'], ['byte_length', 0], ['byte_length', 16777217],
      ['source_version', 0], ['source_version', Number.MAX_SAFE_INTEGER + 1], ['download', '/other'], ['extra', 1]]) {
      mutations.push((v) => { v.head.state[key] = bad })
    }
    for (const alter of mutations) { const value = envelope(); alter(value); expect(await send(value)).toEqual(responseFailure()) }
    const valid = envelope()
    valid.head.state.byte_length = 16777216
    valid.head.state.source_version = Number.MAX_SAFE_INTEGER
    expect((await send(valid)).ok).toBe(true)
  })

  it('c16_frame_validation', async () => {
    for (const [key, bad] of [['coordinate_system', 'local'], ['transform', 'shift'], ['drawing_units', 'ft'],
      ['meters_per_unit', 0.3048], ['crs', 'EPSG:0'], ['crs', 'EPSG:1000000'], ['elevation_datum', 'epsg:4326'],
      ['horizontal', 'metres'], ['elevation', 'drawing-units']]) {
      const value = envelope()
      value.frame[key] = bad
      expect(await send(value)).toEqual(responseFailure())
    }
    const feet = envelope()
    Object.assign(feet.frame, { drawing_units: 'ft', meters_per_unit: 0.3048, crs: 'EPSG:4326', elevation_datum: 'EPSG:1' })
    expect((await send(feet, 201, { drawingUnits: 'ft' })).ok).toBe(true)
  })

  it('c17_terrain_standing_validation', async () => {
    for (const entry of [{ state: 'absent', checked: 0, stale: 0 }, { state: 'current', checked: 1000000, stale: 0 },
      { state: 'stale', checked: 50, stale: 7 }]) {
      const value = envelope()
      value.terrain_standing.frames = entry
      value.terrain_standing.grid_sha256 = 'd'.repeat(64)
      expect((await send(value)).ok).toBe(true)
    }
    for (const entry of [{ state: 'absent', checked: 1, stale: 0 }, { state: 'current', checked: 0, stale: 0 },
      { state: 'current', checked: 1, stale: 1 }, { state: 'stale', checked: 1, stale: 0 },
      { state: 'stale', checked: 1, stale: 2 }, { state: 'other', checked: 0, stale: 0 },
      { state: 'current', checked: 1000001, stale: 0 }, { state: 'current', checked: true, stale: 0 },
      { state: 'current', checked: 1, stale: 0, extra: 0 }]) {
      const value = envelope()
      value.terrain_standing.piles = entry
      expect(await send(value)).toEqual(responseFailure())
    }
    for (const [key, bad] of [['schema', 'other'], ['maturity', 'complete'], ['grid_sha256', 'bad']]) {
      const value = envelope()
      value.terrain_standing[key] = bad
      expect(await send(value)).toEqual(responseFailure())
    }
  })

  it('c18_detached_success_value', () => {
    const value = envelope()
    const result = validateTrackerRowsResult(value, { ...options(), status: 201, signal: undefined })
    expect(result).toBeNull()
    const normalized = validateTrackerRowsResult(value, { drawingId: 'solar', projectId: 'p', drawingUnits: 'm', request: request(), status: 201 })
    expect(normalized).toEqual(domain())
    normalized.head.state.artifact_id = 'c'.repeat(64)
    normalized.frame.crs = 'EPSG:1'
    normalized.summary.rows = 4
    normalized.terrain_standing.frames.checked = 9
    expect(value).toEqual(envelope())
  })

  it('c19_invalid_request_sends_nothing', async () => {
    const fetch = vi.fn(async () => wire())
    const client = make(fetch)
    for (const drawingId of ['Solar', '', 'a'.repeat(64), 'a/b']) {
      expect(await client.createTrackerRows(options({ drawingId }))).toEqual(failure(null, 'TRACKER_ROWS_REQUEST_INVALID'))
    }
    for (const projectId of ['', 'x'.repeat(101), '\ud800', true]) {
      expect(await client.createTrackerRows(options({ projectId }))).toEqual(failure(null, 'TRACKER_ROWS_PROJECT_ID_INVALID'))
    }
    expect(await client.createTrackerRows(options({ request: { ...request(), module_power_watts: 0 } })))
      .toEqual(failure(null, 'TRACKER_ROWS_POWER_INVALID'))
    expect(await client.createTrackerRows(options({ request: { ok: true, body: request() } })))
      .toEqual(failure(null, 'TRACKER_ROWS_REQUEST_INVALID'))
    expect(await client.createTrackerRows(options({ extra: 1 }))).toEqual(failure(null, 'CLIENT_REQUEST_INVALID'))
    expect(fetch).not.toHaveBeenCalled()
  })

  it('c20_hostile_options_and_signal_registration', async () => {
    const fetch = vi.fn(async () => wire())
    const client = make(fetch)
    const hostile = new Proxy({}, { ownKeys() { throw new Error('private') } })
    expect(await client.createTrackerRows(hostile)).toEqual(failure(null, 'CLIENT_REQUEST_INVALID'))
    expect(await client.createTrackerRows(Object.defineProperty(options(), 'drawingId', { get() { throw new Error('private') } })))
      .toEqual(failure(null, 'CLIENT_REQUEST_INVALID'))
    vi.useFakeTimers()
    const signal = controlledSignal()
    const register = signal.addEventListener.getMockImplementation()
    signal.addEventListener.mockImplementation((...args) => { register(...args); throw new Error('retained listener') })
    expect(await client.createTrackerRows(options({ signal }))).toEqual(failure(null, 'CLIENT_REQUEST_INVALID'))
    expect(signal.removeEventListener).toHaveBeenCalledTimes(1)
    expect(signal.removeEventListener.mock.calls[0][1]).toBe(signal.addEventListener.mock.calls[0][1])
    expect(vi.getTimerCount()).toBe(0)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('c21_network_failures_never_reject', async () => {
    for (const fetch of [() => { throw new Error('private') }, async () => { throw new Error('private') }]) {
      expect(await make(fetch).createTrackerRows(options())).toEqual(failure(null, 'CLIENT_NETWORK', true))
    }
    for (const headers of [() => { throw new Error('private') }, () => ({ Authorization: 5 }),
      () => Object.defineProperty({}, 'Authorization', { get() { throw new Error('private') } })]) {
      const fetch = vi.fn()
      expect(await make(fetch, { headers }).createTrackerRows(options())).toEqual(failure(null, 'CLIENT_REQUEST_INVALID'))
      expect(fetch).not.toHaveBeenCalled()
    }
  })

  it('c22_unreadable_bodies_and_response_getters', async () => {
    for (const status of [201, 200, 502]) {
      const response = wire({}, status)
      response.text = async () => '<html>private</html>'
      expect(await make(async () => response).createTrackerRows(options())).toEqual(responseFailure(status, status >= 500))
    }
    for (const [response, status] of [[{ get status() { throw new Error('private') } }, null], [{ status: '201' }, null],
      [{ status: 201, get body() { throw new Error('private') } }, 201], [{ status: 201, body: {}, headers: {} }, 201],
      [{ status: 201, body: null, text: async () => { throw new Error('private') } }, 201]]) {
      expect(await make(async () => response).createTrackerRows(options())).toEqual(responseFailure(status))
    }
  })

  it('c23_streamed_response_exact_limit_and_overflow', async () => {
    const json = JSON.stringify(envelope())
    const exact = new TextEncoder().encode(json + ' '.repeat(65536 - json.length))
    const valid = streamed([exact.subarray(0, 100), exact.subarray(100)])
    expect(await make(async () => valid.response).createTrackerRows(options())).toEqual({ ok: true, status: 201, value: domain() })
    expect(valid.reader.cancel).not.toHaveBeenCalled()
    const over = streamed([exact, new Uint8Array([32])])
    expect(await make(async () => over.response).createTrackerRows(options())).toEqual(responseFailure())
    expect(over.reader.cancel).toHaveBeenCalledTimes(1)
    const invalid = streamed([new Uint8Array([123, 255, 125])])
    expect(await make(async () => invalid.response).createTrackerRows(options())).toEqual(responseFailure())
    const refusal = streamed([new Uint8Array(65537)], 409)
    expect(await make(async () => refusal.response).createTrackerRows(options())).toEqual(responseFailure(409))
    expect(refusal.reader.cancel).toHaveBeenCalledTimes(1)
  })

  it('c24_declared_lengths_and_leading_zeroes', async () => {
    for (const length of ['65537', '0000065537', '9'.repeat(40)]) {
      const value = streamed([], 201, length)
      expect(await make(async () => value.response).createTrackerRows(options())).toEqual(responseFailure())
      expect(value.body.getReader).not.toHaveBeenCalled()
      expect(value.body.cancel).toHaveBeenCalledTimes(1)
    }
    for (const length of ['00065536', 'invalid', '-1']) {
      const value = streamed([new TextEncoder().encode(JSON.stringify(envelope()))], 201, length)
      expect((await make(async () => value.response).createTrackerRows(options())).ok).toBe(true)
    }
  })

  it('c25_text_fallback_utf8_bounds', async () => {
    const json = JSON.stringify(envelope())
    for (const [text, accepted] of [[json + ' '.repeat(65536 - json.length), true],
      [json + ' '.repeat(65537 - json.length), false]]) {
      const response = wire()
      response.text = async () => text
      expect((await make(async () => response).createTrackerRows(options())).ok).toBe(accepted)
    }
    // Two-byte characters in a value the result binds: at 65,536 UTF-8 bytes the body is accepted, and one
    // byte more is refused although its UTF-16 length (65,337) is far under the bound.
    const projectId = '\u00e9'.repeat(100)
    const wide = { ...envelope(), project_id: projectId, head: { ...envelope().head, project_id: projectId } }
    const wideJson = JSON.stringify(wide)
    const extra = new TextEncoder().encode(wideJson).byteLength - wideJson.length
    expect(extra).toBe(200)
    for (const [bytes, accepted] of [[65536, true], [65537, false]]) {
      const text = wideJson + ' '.repeat(bytes - wideJson.length - extra)
      expect(new TextEncoder().encode(text).byteLength).toBe(bytes)
      expect(text.length).toBe(bytes - 200)
      const response = wire()
      response.text = async () => text
      expect((await make(async () => response).createTrackerRows(options({ projectId }))).ok).toBe(accepted)
    }
    // A literal U+FFFD in an otherwise valid refusal body is refused, not read as the refusal it names.
    const replaced = wire({ error: { reason_code: 'TRACKER_ROWS_STALE_HEAD', retryable: true,
      message: String.fromCharCode(0xfffd) } }, 409)
    expect(await make(async () => replaced).createTrackerRows(options())).toEqual(failure(409, 'CLIENT_RESPONSE_INVALID'))
  })

  it('c26_headers_and_body_deadline', async () => {
    vi.useFakeTimers()
    const stalledFetch = make(never, { timeoutMs: 30 }).createTrackerRows(options())
    await vi.advanceTimersByTimeAsync(31)
    expect(await stalledFetch).toEqual(failure(null, 'CLIENT_TIMEOUT', true))
    const stream = streamed([])
    stream.reader.read.mockImplementation(never)
    const stalledBody = make(async () => stream.response, { timeoutMs: 30 }).createTrackerRows(options())
    await vi.advanceTimersByTimeAsync(31)
    expect(await stalledBody).toEqual(failure(201, 'CLIENT_TIMEOUT', true))
    expect(stream.reader.cancel).toHaveBeenCalledTimes(1)
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const fetch = vi.fn(async () => wire())
    const slowHeaders = make(fetch, { timeoutMs: 30, headers: () => { now = 31; return {} } })
    expect(await slowHeaders.createTrackerRows(options())).toEqual(failure(null, 'CLIENT_TIMEOUT', true))
    expect(fetch).not.toHaveBeenCalled()
  })

  it('c27_abort_before_and_after_headers', async () => {
    const before = new AbortController()
    before.abort()
    const fetch = vi.fn(async () => wire())
    expect(await make(fetch).createTrackerRows(options({ signal: before.signal }))).toEqual(failure(null, 'CLIENT_ABORTED'))
    expect(fetch).not.toHaveBeenCalled()
    const after = new AbortController()
    const stream = streamed([])
    stream.reader.read.mockImplementation(() => { after.abort(); return never() })
    expect(await make(async () => stream.response).createTrackerRows(options({ signal: after.signal })))
      .toEqual(failure(201, 'CLIENT_ABORTED'))
    expect(stream.reader.cancel).toHaveBeenCalledTimes(1)
  })

  it('c28_ready_and_empty_chunk_floods', async () => {
    for (const chunk of [new Uint8Array(0), new Uint8Array([32])]) {
      let now = 0
      const clock = vi.spyOn(performance, 'now').mockImplementation(() => now)
      const stream = streamed([])
      stream.reader.read.mockImplementation(async () => { now += 1; return { done: false, value: chunk } })
      expect(await make(async () => stream.response, { timeoutMs: 10 }).createTrackerRows(options()))
        .toEqual(failure(201, 'CLIENT_TIMEOUT', true))
      expect(stream.reader.read.mock.calls.length).toBeLessThanOrEqual(11)
      expect(stream.reader.cancel).toHaveBeenCalledTimes(1)
      clock.mockRestore()
    }
  })

  it('c29_assembly_parse_and_validation_deadline', async () => {
    for (const stage of ['assembly', 'parse', 'validation']) {
      let now = 0
      const clock = vi.spyOn(performance, 'now').mockImplementation(() => now)
      const stream = streamed([new TextEncoder().encode(JSON.stringify(envelope()))])
      let hook
      if (stage === 'assembly') {
        const set = Uint8Array.prototype.set
        hook = vi.spyOn(Uint8Array.prototype, 'set').mockImplementation(function (...args) { now = 31; return set.apply(this, args) })
      } else if (stage === 'parse') {
        const parse = JSON.parse
        hook = vi.spyOn(JSON, 'parse').mockImplementation((...args) => { now = 31; return parse(...args) })
      } else {
        const parse = JSON.parse
        hook = vi.spyOn(JSON, 'parse').mockImplementation((...args) => {
          const value = parse(...args)
          Object.defineProperty(value.summary, 'rows', { enumerable: true, get() { now = 31; return 1 } })
          return value
        })
      }
      expect(await make(async () => stream.response, { timeoutMs: 30 }).createTrackerRows(options()))
        .toEqual(failure(201, 'CLIENT_TIMEOUT', true))
      hook.mockRestore()
      clock.mockRestore()
    }
  })

  it('c30_cleanup_deadline_and_cleanup_abort', async () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const slow = controlledSignal(() => { now += 60 })
    expect(await make(undefined, { timeoutMs: 30 }).createTrackerRows(options({ signal: slow })))
      .toEqual(failure(201, 'CLIENT_TIMEOUT', true))
    expect(slow.removeEventListener).toHaveBeenCalledTimes(1)
    for (const afterRemoval of [false, true]) {
      now = 0
      const signal = controlledSignal((self, remove) => { if (afterRemoval) remove(); self.abort(); if (!afterRemoval) remove() })
      expect(await make().createTrackerRows(options({ signal }))).toEqual(failure(201, 'CLIENT_ABORTED'))
      expect(signal.removeEventListener).toHaveBeenCalledTimes(1)
    }
    now = 0
    const throwing = controlledSignal(() => { throw new Error('cleanup') })
    expect(await make().createTrackerRows(options({ signal: throwing }))).toEqual({ ok: true, status: 201, value: domain() })
  })

  it('c31_listener_timer_and_late_body_cleanup', async () => {
    vi.useFakeTimers()
    let resolve
    const pendingResponse = new Promise((done) => { resolve = done })
    const signal = controlledSignal()
    const pending = make(() => pendingResponse, { timeoutMs: 30 }).createTrackerRows(options({ signal }))
    await vi.advanceTimersByTimeAsync(31)
    expect(await pending).toEqual(failure(null, 'CLIENT_TIMEOUT', true))
    expect(signal.removeEventListener).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
    const late = streamed([])
    resolve(late.response)
    await flush()
    expect(late.body.cancel).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('c32_observer_failure_and_decided_refusal_stability', async () => {
    for (const onResponse of [() => { throw new Error('observer') }, async () => { throw new Error('observer') }]) {
      expect(await make(undefined, { onResponse }).createTrackerRows(options())).toEqual({ ok: true, status: 201, value: domain() })
    }
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const signal = controlledSignal((self) => { now = 60; self.abort() })
    const response = wire({ error: { reason_code: 'TRACKER_ROWS_STALE_HEAD', retryable: true } }, 409)
    expect(await make(async () => response, { timeoutMs: 30 }).createTrackerRows(options({ signal })))
      .toEqual(failure(409, 'TRACKER_ROWS_STALE_HEAD', true))
  })

  it('c33_refusal_decided_after_the_deadline_is_a_timeout', async () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const stale = { error: { reason_code: 'TRACKER_ROWS_STALE_HEAD', retryable: true } }
    const parse = JSON.parse
    // Inside the budget the server's refusal is returned as it was sent.
    expect(await make(async () => wire(stale, 409), { timeoutMs: 30 }).createTrackerRows(options()))
      .toEqual(failure(409, 'TRACKER_ROWS_STALE_HEAD', true))
    // The refusal body is parsed after the 30 ms budget.
    now = 0
    let hook = vi.spyOn(JSON, 'parse').mockImplementation((...args) => { now = 31; return parse(...args) })
    expect(await make(async () => wire(stale, 409), { timeoutMs: 30 }).createTrackerRows(options()))
      .toEqual(failure(409, 'CLIENT_TIMEOUT', true))
    hook.mockRestore()
    // The refusal is chosen after the budget: reading the parsed body spends it.
    now = 0
    hook = vi.spyOn(JSON, 'parse').mockImplementation((...args) => {
      const value = parse(...args)
      Object.defineProperty(value, 'error', { enumerable: true, get() { now = 31; return stale.error } })
      return value
    })
    expect(await make(async () => wire(stale, 409), { timeoutMs: 30 }).createTrackerRows(options()))
      .toEqual(failure(409, 'CLIENT_TIMEOUT', true))
    hook.mockRestore()
    // A 401, and an answer with no readable status, after an observer that spends the budget.
    now = 0
    expect(await make(async () => wire(envelope(), 401), { timeoutMs: 30, onResponse: () => { now = 31 } })
      .createTrackerRows(options())).toEqual(failure(401, 'CLIENT_TIMEOUT', true))
    now = 0
    expect(await make(async () => wire(envelope(), 'x'), { timeoutMs: 30, onResponse: () => { now = 31 } })
      .createTrackerRows(options())).toEqual(failure(null, 'CLIENT_TIMEOUT', true))
    // A caller abort while the refusal body is parsed.
    now = 0
    const controller = new AbortController()
    hook = vi.spyOn(JSON, 'parse').mockImplementation((...args) => { controller.abort(); return parse(...args) })
    expect(await make(async () => wire(stale, 409)).createTrackerRows(options({ signal: controller.signal })))
      .toEqual(failure(409, 'CLIENT_ABORTED'))
    hook.mockRestore()
  })

  it('c34_every_refusal_after_the_budget_is_the_stop_failure', async () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const spend = () => { now = 31 }
    const timeout = (status) => failure(status, 'CLIENT_TIMEOUT', true)
    const run = (client, opts = options()) => { now = 0; return client.createTrackerRows(opts) }
    const fetch = vi.fn(async () => wire())
    const quick = (fetchImpl, extra = {}) => make(fetchImpl, { timeoutMs: 30, ...extra })
    // Before the call opens: an options getter spends the budget and returns an invalid id.
    const slowOptions = Object.defineProperty(options(), 'drawingId', { enumerable: true, get() { spend(); return 'Not An Id' } })
    expect(await run(quick(fetch), slowOptions)).toEqual(timeout(null))
    const throwingOptions = Object.defineProperty(options(), 'drawingId', { enumerable: true, get() { spend(); throw new Error('private') } })
    expect(await run(quick(fetch), throwingOptions)).toEqual(timeout(null))
    // The request check, the header injector and the fetch each spend it and refuse.
    const slowPower = options({ request: Object.defineProperty(request(), 'module_power_watts',
      { enumerable: true, get() { spend(); return 0 } }) })
    expect(await run(quick(fetch), slowPower)).toEqual(timeout(null))
    expect(await run(quick(fetch, { headers: () => { spend(); throw new Error('private') } }))).toEqual(timeout(null))
    expect(fetch).not.toHaveBeenCalled()
    expect(await run(quick(async () => { spend(); throw new Error('private') }))).toEqual(timeout(null))
    // The observer or the status getter spends it, and the body is unreadable.
    expect(await run(quick(async () => ({ status: 201, body: {}, headers: {} }), { onResponse: spend }))).toEqual(timeout(201))
    expect(await run(quick(async () => ({ get status() { spend(); return 201 }, body: {}, headers: {} })))).toEqual(timeout(201))
    // A stream read, the text read and the assembly each spend it and fail.
    const nullRead = streamed([])
    nullRead.reader.read.mockImplementation(async () => { spend(); return null })
    expect(await run(quick(async () => nullRead.response))).toEqual(timeout(201))
    expect(await run(quick(async () => ({ status: 201, body: null, headers: { get: () => null },
      text: async () => { spend(); throw new Error('late') } })))).toEqual(timeout(201))
    const encoded = new TextEncoder().encode(JSON.stringify(envelope()))
    let hook = vi.spyOn(Uint8Array.prototype, 'set').mockImplementation(() => { spend(); throw new Error('assembly') })
    const assembled = streamed([encoded])
    expect(await run(quick(async () => assembled.response))).toEqual(timeout(201))
    hook.mockRestore()
    // The parse and the result check each spend it and refuse.
    const parse = JSON.parse
    hook = vi.spyOn(JSON, 'parse').mockImplementation(() => { spend(); return {} })
    expect(await run(quick(async () => wire()))).toEqual(timeout(201))
    hook.mockRestore()
    const summaryGetter = (get) => vi.spyOn(JSON, 'parse').mockImplementation((...args) => {
      const value = parse(...args)
      Object.defineProperty(value.summary, 'rows', { enumerable: true, get })
      return value
    })
    hook = summaryGetter(() => { spend(); return 2 })
    expect(await run(quick(async () => wire()))).toEqual(timeout(201))
    hook.mockRestore()
    // A caller abort during the result check, and an abort before an invalid request, are ABORTED.
    const controller = new AbortController()
    hook = summaryGetter(() => { controller.abort(); return 2 })
    expect(await run(quick(async () => wire()), options({ signal: controller.signal }))).toEqual(failure(201, 'CLIENT_ABORTED'))
    hook.mockRestore()
    // A signal that reads aborted without dispatching its event is still honored.
    const quiet = controlledSignal()
    hook = summaryGetter(() => { quiet.aborted = true; return 2 })
    expect(await run(quick(async () => wire()), options({ signal: quiet }))).toEqual(failure(201, 'CLIENT_ABORTED'))
    hook.mockRestore()
    const before = new AbortController()
    before.abort()
    expect(await run(quick(fetch), options({ drawingId: 'Not An Id', signal: before.signal })))
      .toEqual(failure(null, 'CLIENT_ABORTED'))
    expect(fetch).not.toHaveBeenCalled()
  })

  it('c35_stream_chunks_are_measured_and_copied_when_read', async () => {
    // An own byteLength cannot shrink an oversized chunk: it is refused on its first read.
    const lying = new Uint8Array(65537)
    Object.defineProperty(lying, 'byteLength', { value: 0 })
    const big = streamed([lying, new Uint8Array(0)])
    expect(await make(async () => big.response).createTrackerRows(options())).toEqual(responseFailure())
    expect(big.reader.read).toHaveBeenCalledTimes(1)
    expect(big.reader.cancel).toHaveBeenCalledTimes(1)
    // Nor can it hide trailing bytes: a valid answer followed by one more byte is read at its true length.
    const answer = new TextEncoder().encode(JSON.stringify(envelope()))
    const padded = new Uint8Array([...answer, 120])
    Object.defineProperty(padded, 'byteLength', { value: answer.length })
    expect(await make(async () => streamed([padded]).response).createTrackerRows(options())).toEqual(responseFailure())
    // A tag alone does not make an object a byte chunk, even when its indexed bytes are a valid answer.
    const bytes = new TextEncoder().encode(JSON.stringify(envelope()))
    const fake = Object.assign({ [Symbol.toStringTag]: 'Uint8Array', byteLength: bytes.length, length: bytes.length },
      Array.from(bytes))
    expect(Object.prototype.toString.call(fake)).toBe('[object Uint8Array]')
    const tagged = streamed([fake])
    expect(await make(async () => tagged.response).createTrackerRows(options())).toEqual(responseFailure())
    expect(tagged.reader.cancel).toHaveBeenCalledTimes(1)
    // Only a Uint8Array is a byte chunk: an Int8Array carrying the same valid answer is refused.
    const signed = streamed([new Int8Array(bytes.buffer.slice(0))])
    expect(await make(async () => signed.response).createTrackerRows(options())).toEqual(responseFailure())
    // A chunk is copied when it is read: rewriting it before the next read cannot change the answer.
    const first = bytes.slice(0, 100)
    const reused = streamed([first, bytes.slice(100)])
    const read = reused.reader.read.getMockImplementation()
    let reads = 0
    reused.reader.read.mockImplementation(async () => { if (reads++ === 1) first.fill(32); return read() })
    expect(await make(async () => reused.response).createTrackerRows(options())).toEqual({ ok: true, status: 201, value: domain() })
  })
})
