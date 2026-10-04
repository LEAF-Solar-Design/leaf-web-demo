// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import * as model from './solarCivilModel.js'
import { CIVIL_REQUEST_MAX_BYTES, CIVIL_RESPONSE_MAX_BYTES, CIVIL_ROUTE_REASONS, civilReason, createSolarCivilClient } from './solarCivilClient.js'
import { TERRAIN_ROUTE_REASONS, TERRAIN_CLIENT_REASONS } from './solarTerrainClient.js'

// Synthetic fixtures use the closed server contracts; no fixture comes from another worktree.
const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'd'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const headOf = (artifact = H, index = 0, parent = null, drawing = 'solar', project = 'p') => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index, parent,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: D,
    media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024, source_version: 1,
    download: `/api/drawings/${drawing}/artifacts/${artifact}` },
})
const previewOf = (frames = 242, piles = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-preview.v1', maturity: 'preview', frames, piles,
  collision_markers: 0, range_markers: 0, terrain,
})
const standingOf = (frames = 242, piles = 0, stale = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: terrain ? D : null,
  frames: { state: frames === 0 ? 'absent' : stale ? 'stale' : 'current', checked: frames, stale },
  piles: { state: piles === 0 ? 'absent' : 'current', checked: piles, stale: 0 },
})
const viewOf = (head = headOf(), frames = 242, piles = 0) => ({
  schema: 'leaf.solar-civil-view-response.v1', stored: true, head, preview: previewOf(frames, piles),
  standing: standingOf(frames, piles), grade_pads: 0,
})
const absent = () => ({ schema: 'leaf.solar-civil-view-response.v1', stored: false, head: null, preview: null, standing: null, grade_pads: 0 })
const square = [[0, 0], [100, 0], [100, 100], [0, 100]]
const preset = { Name: 'Full', PileTemplateName: 'Full', Rows: 2, Columns: 1, Piling: { MinPileLengthM: 1, MaxPileLengthM: 4 } }
const template = { Name: 'Full', Stations: [{ Position: 0.5, Length: 2 }] }
const bodyOf = (operation = 'frame-generate', expected = H) => ({
  operation, expected_head: expected,
  ...(operation === 'frame-generate' ? { boundary: clone(square), preset: clone(preset), drawing_units: 'm' } : {}),
  ...(operation === 'piling-generate' ? { preset: clone(preset), pile_template: clone(template) } : {}),
  ...(operation === 'pile-length-range-check' ? { preset: clone(preset) } : {}),
  ...(operation === 'grade-pad' ? { boundary: clone(square), mode: 'Auto', value_du: null } : {}),
})
const resultOf = (operation = 'frame-generate', outcome = 'published', base = H) => ({
  schema: operation === 'grade-pad' ? 'leaf.solar-civil-operation.v1' : 'leaf.solar-frames-piles.v1',
  operation, maturity: 'preview', outcome, created: outcome === 'published',
  drawing_id: 'solar', project_id: 'p', base, units: { drawing_units: 'm', meters_per_unit: 1 },
  terrain: { present: true, sampled: ['frame-generate', 'piling-generate', 'grade-pad'].includes(operation), rows: 2, cols: 2, grid_sha256: D },
  summary: outcome === 'retry' ? null : {
    'frame-generate': { frames_added: 242, frames_off_terrain: 0, preset_name: 'Full' },
    'frame-collision-detect': { frames_checked: 242, collisions: 0 },
    'piling-generate': { native_frames: 242, piles: 1936, piles_replaced: 0, grid_piles: 1936, joint_piles: 0, station_piles: 0, short_trackers: 0, template_name: 'Full' },
    'pile-length-range-check': { total_piles: 1936, out_of_range: 0, min_m: 1, max_m: 4 },
    'grade-pad': { pads_added: 1, grade_pads: 1, mode: 'Auto', elevation_m: 0, label: 'Pad', total_cut_m3: 0, total_fill_m3: 0, net_m3: 0 },
  }[operation],
  preview: previewOf(242, operation === 'piling-generate' ? 1936 : 0),
  standing: standingOf(242, operation === 'piling-generate' ? 1936 : 0),
  head: outcome === 'unchanged' ? headOf(base) : headOf(H2, base === null ? 0 : 1, base),
})
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }

const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), { status })
const make = (fetchImpl, extra = {}) => createSolarCivilClient({ fetchImpl, headers: () => ({}), ...extra })
const args = (body = bodyOf()) => ({ drawingId: 'solar', projectId: 'p', body })
function readServer(relative) {
  const buffer = readFileSync(new URL(`../../../server/${relative}`, import.meta.url))
  expect(buffer.length).toBeLessThanOrEqual(1024 * 1024)
  return buffer.toString('utf8')
}
function routeMap(literal) {
  const text = readServer('routers/solar_terrain.py')
  const start = text.indexOf(literal + ' = {')
  expect(start).toBeGreaterThanOrEqual(0)
  const end = text.indexOf('\n}', start)
  expect(end).toBeGreaterThan(start)
  const row = new RegExp(
    '\\x22([A-Z][A-Z0-9_]{0,63})\\x22:\\s*\\((\\d{3}),\\s*ErrorCode\\.([A-Z_]+),\\s*(True|False)\\)', 'g')
  const rows = new Map()
  for (const match of text.slice(start, end).matchAll(row)) {
    rows.set(match[1], { status: Number(match[2]), envelope: match[3], retryable: match[4] === 'True' })
  }
  return rows
}
it('PC1 civil URLs', async () => {
  const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(absent()))
  const client = make(fetchImpl, { apiBase: '/base' })
  await client.getCivil({ drawingId: 'solar', projectId: 'p & q' })
  expect(fetchImpl.mock.calls[0][0]).toBe('/base/api/drawings/solar/terrain?view=civil&project_id=p%20%26%20q')
  for (const operation of model.CIVIL_OPERATIONS) {
    const body = bodyOf(operation)
    fetchImpl.mockResolvedValue(jsonResponse(resultOf(operation)))
    expect((await client.runCivilOperation(args(body))).ok).toBe(true)
    const [url, init] = fetchImpl.mock.calls.at(-1)
    expect(url).toBe('/base/api/drawings/solar/terrain/operations?project_id=p')
    expect(JSON.parse(init.body)).toEqual(body)
    expect(init.method).toBe('POST')
    expect(init.body).not.toContain('project_id')
  }
  await client.getCivil({ drawingId: 'solar' })
  expect(fetchImpl.mock.calls.at(-1)[0]).toBe('/base/api/drawings/solar/terrain?view=civil')
})
it('PC2 checkout parity', async () => {
  const source = { Authorization: 'Bearer one', 'X-Checkout-Capability': 'cap-one', 'cOnTeNt-TyPe': 'text/plain' }
  const headers = vi.fn(() => source)
  const observer = vi.fn()
  const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(absent()))
  const client = make(fetchImpl, { headers, onResponse: observer })
  await client.getCivil({ drawingId: 'solar' })
  expect(source['cOnTeNt-TyPe']).toBe('text/plain')
  source.Authorization = 'Bearer two'; source['X-Checkout-Capability'] = 'cap-two'
  fetchImpl.mockResolvedValue(jsonResponse(resultOf()))
  await client.runCivilOperation(args())
  const sent = fetchImpl.mock.calls[1][1].headers
  expect(sent).toEqual({ Authorization: 'Bearer two', 'X-Checkout-Capability': 'cap-two', 'Content-Type': 'application/json' })
  expect(observer.mock.calls.map((c) => c[2])).toEqual(['Bearer one', 'Bearer two'])
  expect(headers.mock.calls).toEqual([['solar'], ['solar']])
  expect(source).toEqual({ Authorization: 'Bearer two', 'X-Checkout-Capability': 'cap-two', 'cOnTeNt-TyPe': 'text/plain' })
  for (const value of [null, [], { Authorization: 2 }]) {
    expect((await make(fetchImpl, { headers: () => value }).getCivil({ drawingId: 'solar' })).code).toBe('TERRAIN_CLIENT_REQUEST_INVALID')
  }
})
it('PC3 request bound', async () => {
  // Isolate the transport byte guard: real model object/vertex bounds are independently covered in PC9/PC10.
  const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(resultOf()))
  const client = make(fetchImpl)
  const validator = vi.spyOn(model, 'validateCivilBody')
  try {
    const base = { operation: 'frame-generate', expected_head: H, text: '' }
    const overhead = new TextEncoder().encode(JSON.stringify(base)).byteLength
    for (const delta of [0, 1]) {
      const body = { ...base, text: 'a'.repeat(CIVIL_REQUEST_MAX_BYTES - overhead - 2 + delta) + String.fromCharCode(0xe9) }
      expect(new TextEncoder().encode(JSON.stringify(body)).byteLength).toBe(CIVIL_REQUEST_MAX_BYTES + delta)
      validator.mockReturnValue({ ok: true, body })
      const answer = await client.runCivilOperation(args(body))
      if (delta) expect(answer.code).toBe('TERRAIN_BODY_TOO_LARGE')
    }
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  } finally { validator.mockRestore() }
})
it('PC4 response bound', async () => {
  for (const response of [
    new Response('{}', { headers: { 'content-length': String(CIVIL_RESPONSE_MAX_BYTES + 1) } }),
    new Response(new Uint8Array([0xc3, 0x28])),
  ]) expect((await make(async () => response).getCivil({ drawingId: 'solar' })).code).toBe('TERRAIN_CLIENT_RESPONSE_INVALID')
  const cancel = vi.fn()
  let reads = 0
  const response = { status: 200, headers: { get: () => null }, body: { getReader: () => ({
    read: async () => { reads++; return { done: false, value: new Uint8Array(40000) } }, cancel,
  }) } }
  expect((await make(async () => response).getCivil({ drawingId: 'solar' })).code).toBe('TERRAIN_CLIENT_RESPONSE_INVALID')
  expect(reads).toBe(2); expect(cancel).toHaveBeenCalledTimes(1)
})
it('PC5 deadline', async () => {
  for (const fetchImpl of [() => new Promise(() => {}), async () => ({ status: 200, headers: { get: () => null }, body: { getReader: () => ({ read: () => new Promise(() => {}), cancel: () => {} }) } })]) {
    const answer = await make(fetchImpl, { timeoutMs: 5 }).getCivil({ drawingId: 'solar' })
    expect(answer.code).toBe('TERRAIN_CLIENT_TIMEOUT')
  }
  const pending = deferred(); const controller = new AbortController()
  const promise = make(() => pending.promise).getCivil({ drawingId: 'solar', signal: controller.signal })
  controller.abort()
  expect((await promise).code).toBe('TERRAIN_CLIENT_ABORTED')
  pending.resolve(jsonResponse(absent()))
  expect(() => make(async () => null, { timeoutMs: 600001 })).toThrow()
})
it('PC6 refusal coverage', async () => {
  for (const literal of ['TERRAIN_ROUTE_REFUSALS', 'CIVIL_ROUTE_REFUSALS']) for (const [code, row] of routeMap(literal)) {
    const response = jsonResponse({ ok: false, error: { reason_code: code, error_code: row.envelope, retryable: row.retryable } }, row.status)
    expect(await make(async () => response).getCivil({ drawingId: 'solar' })).toEqual({ ok: false, status: row.status, code, retryable: row.retryable })
    expect(civilReason(code)).toBe(CIVIL_ROUTE_REASONS[code] ?? TERRAIN_ROUTE_REASONS[code])
  }
  for (const [status, envelope, code] of [
    [401, {}, 'UNAUTHENTICATED'], [403, { entitlement_required: true }, 'ENTITLEMENT_REQUIRED'],
    [503, { entitlement_required: true }, 'ENTITLEMENT_POLICY_UNAVAILABLE'],
    [403, { error: { error_code: 'FORBIDDEN' } }, 'FORBIDDEN'],
    [500, { error: { error_code: 'INTERNAL', retryable: true } }, 'INTERNAL'],
    [422, { error: { error_code: 'BAD_PARAMS' } }, 'BAD_PARAMS'],
  ]) {
    expect((await make(async () => jsonResponse(envelope, status)).getCivil({ drawingId: 'solar' })).code).toBe(code)
    expect(civilReason(code)).toBe(TERRAIN_CLIENT_REASONS[code])
  }
  for (const code of ['UNKNOWN', 'toString', null, 'a'.repeat(1000)]) expect(civilReason(code)).toBe('The terrain request stopped')
  expect(civilReason('TERRAIN_OPERATION_INVALID')).toBe('The terrain request stopped')
})
it('PC7 malformed success', async () => {
  for (const extra of [{ extra: 1 }, { drawing_id: 'other' }, { project_id: 'other' }, { base: H2 }, { operation: 'piling-generate' }]) {
    const answer = await make(async () => jsonResponse({ ...resultOf(), ...extra })).runCivilOperation(args())
    expect(answer.code).toBe('TERRAIN_CLIENT_RESPONSE_INVALID')
  }
  expect((await make(async () => jsonResponse({ ...viewOf(), extra: 1 })).getCivil({ drawingId: 'solar' })).code).toBe('TERRAIN_CLIENT_RESPONSE_INVALID')
})
it('PC33 civil route codes follow the server civil refusal map', async () => {
  const rows = routeMap('CIVIL_ROUTE_REFUSALS')
  expect(rows.size).toBe(29)
  expect(Object.keys(CIVIL_ROUTE_REASONS).filter((k) => k !== 'TERRAIN_OPERATION_INVALID').sort()).toEqual([...rows.keys()].sort())
  for (const [code, row] of rows) {
    const client = make(async () => jsonResponse({ ok: false, error: { reason_code: code, error_code: row.envelope, retryable: row.retryable } }, row.status))
    expect(await client.runCivilOperation(args())).toEqual({ ok: false, status: row.status, code, retryable: row.retryable })
    expect(civilReason(code)).toBe(CIVIL_ROUTE_REASONS[code])
  }
})

