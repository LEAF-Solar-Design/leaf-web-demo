import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash, webcrypto } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import { createSolarPvcaseClient, readCommittedIntake, PVCASE_INTAKE_RESPONSE_MAX_BYTES } from './solarPvcaseClient.js'
import { validatePvcaseCommit, pvcaseReason, PVCASE_REASONS } from './solarPvcaseModel.js'

const H = 'a'.repeat(64)
const B = 'b'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const context = (extra = {}) => ({ tenantId: 'synthetic-tenant', orgId: 'synthetic-org', projectId: 'p', drawingId: 'solar', drawingVersion: 7, graphRev: 0, checkoutCapability: 'synthetic-checkout', ...extra })
const artifact = (assignment = false, v = 7, bytes = 274972) => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: assignment ? B : H, media_type: 'application/json',
  filename: assignment ? 'PVcaseAssignments.json' : 'pvcase-g33-source.json', byte_length: bytes,
  content_sha256: B, source_version: v, download: `/api/drawings/solar/artifacts/${assignment ? B : H}`,
})
const source = () => ({ schema: 'leaf.pvcase-g33-source.v1', kind: 'pvcase-g33', drawing_id: 'solar', project_id: 'p',
  source_version: 7, graph_rev: 0, graph_sha256: H, source: artifact() })
const receipt = (operation = 'convert', c = context(), jobId = 'job-1') => ({
  schema_version: 'leaf.solar-graph-commit.v1', adapter: 'local-graph-commit', tenant_id: c.tenantId,
  job_id: jobId, tool: `solar-pvcase-${operation}`, project_id: c.projectId, drawing_id: c.drawingId,
  request_sha256: H, new_version: { drawing_id: c.drawingId, version: c.drawingVersion + 6, parent: c.drawingVersion },
  before_graph_sha256: H, graph_sha256: B, intake_sha256: H, before_rev: c.graphRev, after_rev: c.graphRev + 1,
  drawing_changed: true, replayed: false,
})
const runEnvelope = (r) => ({ ok: true, tool: r.tool, version: '1.0.0', result: r, overlay: null, timing_ms: 1, cost: 0, error: null, degraded_mode: false })
const binding = (r = receipt(), c = context()) => ({ context: c, source: source(), tool: r.tool, jobId: r.job_id })
const readReceipt = (c = context()) => ({
  schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read', tenant_id: c.tenantId, job_id: 'job-1',
  tool: 'solar-pvcase-export', project_id: c.projectId, drawing_id: c.drawingId, request_sha256: H, source_version: c.drawingVersion,
  representation: 'intake', graph_sha256: B, output_sha256: H, output_bytes: 900, drawing_changed: false,
  output: { summary: { schema: 'leaf.pvcase-g33-export.v1', panels: 2345, strings: 88, electrical_sizing: 'not-evaluated',
    source: { artifact_id: H, content_sha256: B } }, artifact: artifact(true, c.drawingVersion, 475691) },
})
const entityId = (kind, n) => `leaf:${kind}:00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
// Synthetic graph projection of the checked-in Roof inventory, not a server conversion fixture.
const roofIntakeText = readFileSync(resolve(process.cwd(), '..', 'docs', 'parity', 'evidence', 'rooftop', 'pvcase', 'intake.json'), 'utf8').trim()
const roofIntake = JSON.parse(roofIntakeText)
function roofGraph(solved = false, rev = 1) {
  const g = { graph_schema_version: 1, rev, parent_rev: rev - 1, project: { id: 'p' },
    frames: [], panels: [], strings: [], inverters: [], electrical_zones: [], routes: [], schedules: [], extra: { synthetic: true } }
  let n = 1
  for (const group of roofIntake.panel_groups) {
    const f = { id: entityId('frame', n++), kind: 'frame', rev, panel_refs: [], panel_assignments: [], matrix: [], sequences: [], electrical_zone_ref: null }
    for (const row of group.rows) for (const cell of row) {
      if (cell?.code !== 1 || !cell.id) continue
      const p = { id: entityId('panel', n++), kind: 'panel', rev, frame_ref: f.id, assignment: { string_ref: null, seq: null }, matrix_cell: { row: 0, col: f.panel_refs.length } }
      g.panels.push(p); f.panel_refs.push(p.id)
    }
    if (f.panel_refs.length) g.frames.push(f)
  }
  if (solved) {
    // Synthetic 88-string membership used only to exercise collection proofs.
    for (let i = 0, at = 0; i < 88; i++) {
      const length = Math.ceil((g.panels.length - at) / (88 - i))
      const refs = g.panels.slice(at, at + length).map((p, seq) => {
        p.assignment = { string_ref: entityId('string', n), seq }; return p.id
      })
      g.strings.push({ id: entityId('string', n++), kind: 'string', rev, module_count: length, ordered_panel_refs: refs, inverter_ref: null })
      at += length
    }
  }
  const panels = new Map(g.panels.map((p) => [p.id, p]))
  for (const f of g.frames) {
    f.panel_assignments = f.panel_refs.map((ref) => ({ panel_ref: ref, ...panels.get(ref).assignment, inverter_id: null, string_input_number: null }))
    f.matrix = [f.panel_assignments.map((a) => ({ ...a }))]
    f.sequences = g.strings.map((s) => ({ string_ref: s.id, ordered_panel_refs: s.ordered_panel_refs.filter((ref) => f.panel_refs.includes(ref)) })).filter((s) => s.ordered_panel_refs.length)
  }
  return g
}
const view = (g = roofGraph(), v = 13) => ({ intake: { solar_design_graph: g }, version: v, head: 99, latest: 100, error: null, degraded_mode: false })
const catalog = (operation) => ({ name: `solar-pvcase-${operation}`, version: '1.0.0', catalog_digest: H, capabilities: [] })
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r }); return { promise, resolve } }

const json = (value, status = 200, extra = {}) => new Response(JSON.stringify(value), { status, headers: extra })
const make = (fetchImpl, options = {}) => createSolarPvcaseClient({ fetchImpl, headers: () => ({ Authorization: 'Bearer synthetic', 'X-Tenant-Id': 'synthetic-tenant' }), ...options })
const streamResponse = (read, status = 200, length = null, cancel = vi.fn(), releaseLock = vi.fn()) => ({
  status, headers: { get: (key) => key === 'content-length' ? length : null },
  body: { getReader: () => ({ read, cancel, releaseLock }), cancel },
})
const committed = () => validatePvcaseCommit(runEnvelope(receipt()), binding()).value
const intakeTransport = (fetchImpl) => ({ fetchImpl, headers: () => ({ Authorization: 'Bearer synthetic' }) })
async function responseBoundary(value, cap, invoke) {
  const serialized = JSON.stringify(value)
  const padded = serialized + ' '.repeat(cap - new TextEncoder().encode(serialized).byteLength)
  const atBound = new TextEncoder().encode(padded)
  const overflow = new TextEncoder().encode(padded + ' ')
  expect(atBound.byteLength).toBe(cap); expect(overflow.byteLength).toBe(cap + 1)
  for (const headers of [{}, { 'content-length': String(cap) }]) expect((await invoke(new Response(atBound, { headers }))).ok).toBe(true)
  const read = vi.fn().mockResolvedValueOnce({ done: false, value: overflow.subarray(0, cap) })
    .mockResolvedValueOnce({ done: false, value: overflow.subarray(cap) }).mockResolvedValueOnce({ done: true })
  const cancel = vi.fn(); const releaseLock = vi.fn()
  expect(await invoke(streamResponse(read, 200, null, cancel, releaseLock))).toMatchObject({ ok: false, code: 'response', status: 200 })
  expect(read).toHaveBeenCalledTimes(2); expect(cancel).toHaveBeenCalled(); expect(releaseLock).toHaveBeenCalled()
  const declared = streamResponse(async () => ({ done: false, value: overflow }), 200, String(cap + 1))
  declared.body.getReader = vi.fn(declared.body.getReader)
  expect(await invoke(declared)).toMatchObject({ ok: false, code: 'response', status: 200 })
  expect(declared.body.getReader).not.toHaveBeenCalled(); expect(declared.body.cancel).toHaveBeenCalled()
}
const runFetch = (operation = 'convert', r = receipt(operation)) => vi.fn()
  .mockResolvedValueOnce(json({ job_id: 'job-1', status: 'submitted' }, 202))
  .mockResolvedValueOnce(json({ job_id: 'job-1', status: 'complete', result: runEnvelope(r) }))
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
it('PVC01 uploads the exact source bytes', async () => {
  const bytes = new TextEncoder().encode('{"intake":' + roofIntakeText + ',"schema":"leaf.pvcase-g33.v1"}')
  expect(bytes.byteLength).toBe(274972)
  const projectId = 'p /☀'; const answer = source(); answer.project_id = projectId
  let auth = 'Bearer first'
  const fetchImpl = vi.fn(async () => json(answer))
  const client = make(fetchImpl, { headers: () => ({ Authorization: auth, 'X-Tenant-Id': 'synthetic-tenant', 'X-Checkout-Capability': 'must-be-removed' }) })
  auth = 'Bearer fresh'
  expect((await client.uploadSource({ drawingId: 'solar', projectId, file: bytes })).ok).toBe(true)
  expect(fetchImpl).toHaveBeenCalledTimes(1)
  const [url, init] = fetchImpl.mock.calls[0]
  expect(url).toBe('/api/drawings/solar/imports/pvcase-g33?project_id=' + encodeURIComponent(projectId))
  expect(init.body).toBe(bytes); expect(init.headers.Authorization).toBe('Bearer fresh')
  expect(init.headers['Content-Type']).toBe('application/json'); expect(init.headers).not.toHaveProperty('X-Checkout-Capability')
})
it('PVC18 measures source bytes from any realm and refuses a lookalike', async () => {
  const answer = source()
  const fetchImpl = vi.fn(async () => json(answer))
  const client = make(fetchImpl)
  const foreign = new TextEncoder().encode('{"a":1}')
  expect((await client.uploadSource({ drawingId: 'solar', projectId: 'p', file: foreign })).ok).toBe(true)
  expect((await client.uploadSource({ drawingId: 'solar', projectId: 'p', file: foreign.buffer })).ok).toBe(true)
  expect(fetchImpl).toHaveBeenCalledTimes(2)
  for (const file of [{ [Symbol.toStringTag]: 'Uint8Array', byteLength: 3 }, [1, 2, 3], 'abc', new Uint16Array(2)]) {
    expect((await client.uploadSource({ drawingId: 'solar', projectId: 'p', file })).code).toBe('fileRequired')
  }
  expect(fetchImpl).toHaveBeenCalledTimes(2)
})
it('PVC02 refuses invalid upload arguments before fetch', async () => {
  const fetchImpl = vi.fn(); const client = make(fetchImpl)
  const base = { drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) }
  for (const change of [{ drawingId: 'Solar' }, { projectId: '' }, { projectId: 'x'.repeat(101) }, { projectId: '\ud800' }, { file: new Uint8Array() }, { file: new Uint8Array(16777217) }, { extra: true }]) expect((await client.uploadSource({ ...base, ...change })).ok).toBe(false)
  expect(fetchImpl).not.toHaveBeenCalled()
  expect((await client.uploadSource({ ...base, file: new Uint8Array() })).code).toBe('PVG_INVALID_JSON')
})
it('PVC03 bounds and validates upload responses', async () => {
  const base = { drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) }
  const good = { ...source(), error: null, degraded_mode: false }
  expect((await make(vi.fn(async () => json(good))).uploadSource(base)).ok).toBe(true)
  const missing = source(); delete missing.graph_rev
  for (const body of [missing, { ...source(), extra: true }, { ...source(), drawing_id: 'other' }, { ...source(), project_id: null }]) expect((await make(vi.fn(async () => json(body))).uploadSource(base)).code).toBe('response')
  expect((await make(vi.fn(async () => new Response(new Uint8Array([255])))).uploadSource(base)).code).toBe('response')
  await responseBoundary(good, 65536, (response) => make(vi.fn(async () => response)).uploadSource(base))
})
it('PVC04 applies one upload deadline', async () => {
  const args = { drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) }
  const late = deferred(); const cancel = vi.fn()
  const headers = make(vi.fn(() => late.promise), { runOptions: { uploadTimeoutMs: 5 } })
  expect((await headers.uploadSource(args)).code).toBe('timeout')
  late.resolve(streamResponse(async () => ({ done: true }), 200, null, cancel)); await new Promise((resolve) => setTimeout(resolve, 0))
  expect(cancel).toHaveBeenCalled()
  const bodyCancel = vi.fn()
  const body = make(vi.fn(async () => streamResponse(() => new Promise(() => {}), 200, null, bodyCancel)), { runOptions: { uploadTimeoutMs: 5 } })
  expect((await body.uploadSource(args)).code).toBe('timeout'); expect(bodyCancel).toHaveBeenCalled()
  let tick = 0
  vi.spyOn(performance, 'now').mockImplementation(() => tick++)
  const ready = make(vi.fn(async () => streamResponse(async () => ({ done: false, value: new Uint8Array([32]) }))), { runOptions: { uploadTimeoutMs: 20 } })
  expect((await ready.uploadSource(args)).code).toBe('timeout')
})
it('PVC05 maps every source refusal without prose', async () => {
  const codes = ['PVS_DRAWING_ID_INVALID', 'PVS_PROJECT_ID_INVALID', 'PVS_MEDIA_TYPE_REFUSED', 'PVG_INVALID_JSON', 'PVG_INPUT_BYTES_EXCEEDED', 'PVG_ENVELOPE_FIELDS', 'PVG_ENVELOPE_SCHEMA', 'PVG_INVALID_INTAKE', 'PVG_LIST_LIMIT', 'PVG_DEPTH_LIMIT', 'PVG_NODE_LIMIT', 'PVS_DRAWING_NOT_FOUND', 'PVS_GRAPH_REQUIRED', 'PVS_PROJECT_MISMATCH', 'PVS_SOURCE_ID_INVALID', 'PVS_SOURCE_NOT_FOUND', 'PVS_SOURCE_KIND_MISMATCH', 'PVS_WRITES_DRAINED', 'PVS_STORE_UNAVAILABLE', 'PVS_SOURCE_CONFLICT', 'PVS_SOURCE_CORRUPT', 'PVS_SOURCE_INVALID']
  expect(codes).toHaveLength(22)
  for (const code of codes) {
    const fetchImpl = vi.fn(async () => json({ reason_code: code, error: { error_code: 'INTERNAL', message: 'hostile-prose', retryable: true } }, 503))
    const result = await make(fetchImpl).uploadSource({ drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) })
    expect(result.code).toBe(code); expect(result.retryable).toBe(['PVS_WRITES_DRAINED', 'PVS_STORE_UNAVAILABLE'].includes(code))
    expect(pvcaseReason(result.code)).not.toContain('hostile-prose'); expect(pvcaseReason(result.code)).not.toBe(PVCASE_REASONS.refused)
  }
  const result = await make(vi.fn(async () => json({ error: { error_code: 'BAD_PARAMS', message: 'catalog prose' } }, 409))).uploadSource({ drawingId: 'solar', file: new Uint8Array([1]) })
  expect(pvcaseReason(result.code, result.status)).toBe(PVCASE_REASONS.request)
})
it('PVC06 submits conversion through the job path', async () => {
  const fetchImpl = runFetch()
  const result = await make(fetchImpl).convert({ context: context(), source: source(), catalogRow: catalog('convert') })
  expect(result.ok).toBe(true)
  const [url, init] = fetchImpl.mock.calls[0]
  expect(url).toBe('/api/run')
  expect(JSON.parse(init.body)).toEqual({ tool: 'solar-pvcase-convert', params: { drawing_id: 'solar', expected_rev: 0, source_artifact_id: H }, dwg: 'solar', catalog_digest: H, dwg_version: 7 })
  expect(init.headers).toMatchObject({ 'X-Checkout-Capability': 'synthetic-checkout', 'X-Org-Id': 'synthetic-org', 'X-Project-Id': 'p' })
})
it('PVC07 submits solve through the job path', async () => {
  const fetchImpl = runFetch('solve')
  expect((await make(fetchImpl).solve({ context: context(), source: source(), catalogRow: catalog('solve') })).ok).toBe(true)
  expect(JSON.parse(fetchImpl.mock.calls[0][1].body).params).toEqual({ drawing_id: 'solar', expected_rev: 0 })
})
it('PVC08 submits export through the read adapter', async () => {
  const fetchImpl = runFetch('export', readReceipt())
  const result = await make(fetchImpl).exportAssignments({ context: context({ checkoutCapability: null }), source: source(), catalogRow: catalog('export') })
  expect(result.ok).toBe(true); expect(result.value.drawing_changed).toBe(false)
  expect(JSON.parse(fetchImpl.mock.calls[0][1].body).params).toEqual({ drawing_id: 'solar' })
  expect(fetchImpl.mock.calls[0][1].headers).not.toHaveProperty('X-Checkout-Capability')
})
it('PVC09 refuses missing or mismatched catalog rows', async () => {
  const fetchImpl = vi.fn(); const client = make(fetchImpl)
  const hostile = { name: 'solar-pvcase-convert', get catalog_digest() { throw new Error('hostile') } }
  for (const row of [undefined, catalog('solve'), { ...catalog('convert'), catalog_digest: '' }, { ...catalog('convert'), catalog_digest: 'invalid' }, { ...catalog('convert'), catalog_digest: 1 }, hostile]) {
    expect((await client.convert({ context: context(), source: source(), catalogRow: row })).code).toBe('catalog')
  }
  expect(fetchImpl).not.toHaveBeenCalled()
})
it('PVC10 observes one job without resubmission', async () => {
  const final = { ...runEnvelope(receipt()), cost: null, execution_provenance: { execution_mode: 'local_graph_commit' } }
  const fetchImpl = vi.fn()
    .mockResolvedValueOnce(json({ job_id: 'job-1', status: 'submitted' }, 202))
    .mockResolvedValueOnce(json({ job_id: 'job-1', status: 'running' }))
    .mockResolvedValueOnce(json({ job_id: 'job-1', tenant_id: 'synthetic-tenant', org_id: 'synthetic-org', project_id: 'p', dwg: 'solar', dwg_version: 7, tool: 'solar-pvcase-convert', status: 'complete', result: final }))
  expect((await make(fetchImpl).convert({ context: context(), source: source(), catalogRow: catalog('convert') })).ok).toBe(true)
  expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual(['/api/run', '/api/jobs/job-1', '/api/jobs/job-1'])
  expect(fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  expect(fetchImpl.mock.calls.slice(1).every(([, init]) => init.method === 'GET')).toBe(true)
  const failed = vi.fn(async () => json({ job_id: 'job-1', tool: 'solar-pvcase-convert', status: 'failed', result: null, error: { reason_code: 'PVCASE_EMPTY_TARGET_REQUIRED', error_code: 'BAD_PARAMS', message: 'hostile-prose' } }))
  const refusal = await make(failed).observeJob({ context: context(), source: source(), catalogRow: catalog('convert'), operation: 'convert', jobId: 'job-1' })
  expect(refusal.code).toBe('PVCASE_EMPTY_TARGET_REQUIRED')
  expect(pvcaseReason(refusal.code)).not.toContain('hostile-prose')
})
it('PVC11 retains uncertain job identity', async () => {
  const args = { context: context(), source: source(), catalogRow: catalog('convert') }
  for (const mode of ['network', 'timeout', 'aborted']) {
    const controller = new AbortController()
    const fetchImpl = vi.fn().mockResolvedValueOnce(json({ job_id: 'job-1', status: 'submitted' }, 202))
      .mockImplementationOnce(() => { if (mode === 'network') return Promise.reject(new Error('offline')); if (mode === 'aborted') controller.abort(); return new Promise(() => {}) })
    const result = await make(fetchImpl, { runOptions: { timeoutMs: 15 } }).convert({ ...args, signal: controller.signal })
    expect(result.code).toBe(mode); expect(result.jobId).toBe('job-1')
  }
  const fetchImpl = vi.fn(async () => json({ job_id: 'job-1', status: 'complete', result: runEnvelope(receipt()) }))
  expect((await make(fetchImpl).observeJob({ ...args, operation: 'convert', jobId: 'job-1' })).ok).toBe(true)
  expect(fetchImpl.mock.calls[0][1].method).toBe('GET')
})
it('PVC12 rejects foreign or malformed job results', async () => {
  for (const mutate of [(x) => x.job_id = 'foreign', (x) => x.result.tool = 'solar-pvcase-solve', (x) => x.result.version = '2.0.0', (x) => x.result.result.tenant_id = 'foreign', (x) => x.result.result.new_version.parent = 8, (x) => x.result = x.result.result]) {
    const record = { job_id: 'job-1', status: 'complete', result: runEnvelope(receipt()) }; mutate(record)
    const fetchImpl = vi.fn().mockResolvedValueOnce(json({ job_id: 'job-1', status: 'submitted' }, 202)).mockResolvedValueOnce(json(record))
    const answer = await make(fetchImpl).convert({ context: context(), source: source(), catalogRow: catalog('convert') })
    expect(answer.ok).toBe(false); expect(fetchImpl.mock.calls.some(([url]) => url.includes('/intake'))).toBe(false)
  }
  const fetchImpl = vi.fn().mockResolvedValueOnce(json({ job_id: 'job-1', status: 'submitted' }, 202)).mockResolvedValueOnce(new Response(new Uint8Array(1048577)))
  expect((await make(fetchImpl).convert({ context: context(), source: source(), catalogRow: catalog('convert') })).code).toBe('response')
})
it('PVC13 public promises never reject', async () => {
  const client = make(vi.fn())
  const hostile = new Proxy({}, { ownKeys() { throw new Error('hostile') } })
  for (const method of ['uploadSource', 'convert', 'solve', 'exportAssignments', 'observeJob', 'readCommittedGraph', 'download']) {
    for (const input of [null, hostile]) expect((await client[method](input)).ok).toBe(false)
  }
  for (const input of [null, hostile]) expect((await readCommittedIntake(input)).ok).toBe(false)
  const cancel = vi.fn(() => { throw new Error('cleanup') })
  const response = streamResponse(async () => { throw new Error('read') }, 200, null, cancel)
  expect((await readCommittedIntake({ drawingId: 'solar', version: 13, transport: intakeTransport(async () => response) })).ok).toBe(false)
  const good = make(vi.fn(async () => json(source())), { onResponse: () => Promise.reject(new Error('observer')) })
  expect((await good.uploadSource({ drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) })).ok).toBe(true)
})
it('PVC14 reuses authenticated artifact verification', async () => {
  vi.stubGlobal('crypto', webcrypto)
  // Synthetic assignment content at the measured export size with its own digest.
  const bytes = new Uint8Array(475691).fill(65)
  const ref = artifact(true, 13, bytes.byteLength); ref.content_sha256 = createHash('sha256').update(bytes).digest('hex')
  const fetchImpl = vi.fn(async () => new Response(bytes, { headers: { 'x-leaf-artifact-id': B } }))
  const result = await make(fetchImpl).download({ drawingId: 'solar', ref })
  expect(result.ok).toBe(true); expect(result.value.bytes).toEqual(bytes)
  expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe('Bearer synthetic')
  expect(fetchImpl.mock.calls[0][0]).toBe(ref.download)
  bytes[0] = 66
  expect((await make(fetchImpl).download({ drawingId: 'solar', ref })).code).toBe('SOLAREDGE_CLIENT_ARTIFACT_MISMATCH')
}, 30000)
it('PVC15 reads the committed integer version', async () => {
  const fetchImpl = vi.fn(async () => json(view()))
  const r = committed(); const result = await make(fetchImpl).readCommittedGraph({ context: context(), receipt: r })
  expect(result.ok).toBe(true); expect(result.value.version).toBe(13)
  expect(fetchImpl).toHaveBeenCalledTimes(1); expect(fetchImpl.mock.calls[0][0]).toBe('/api/drawings/solar/intake?version=13')
  for (const version of ['13', 13.5, 'head', 'latest', 0]) expect((await readCommittedIntake({ drawingId: 'solar', version, transport: intakeTransport(fetchImpl) })).ok).toBe(false)
})
it('PVC16 bounds committed graph reads', async () => {
  expect(PVCASE_INTAKE_RESPONSE_MAX_BYTES).toBe(33554432)
  const options = (fetchImpl, extra = {}) => ({ drawingId: 'solar', version: 13, transport: intakeTransport(fetchImpl), timeoutMs: 10, ...extra })
  expect((await readCommittedIntake(options(() => new Promise(() => {})))).code).toBe('timeout')
  const cancel = vi.fn(); const releaseLock = vi.fn()
  expect((await readCommittedIntake(options(async () => streamResponse(() => new Promise(() => {}), 200, null, cancel, releaseLock)))).code).toBe('timeout')
  expect(cancel).toHaveBeenCalled(); expect(releaseLock).toHaveBeenCalled()
  const controller = new AbortController(); controller.abort()
  expect((await readCommittedIntake(options(vi.fn(), { signal: controller.signal }))).code).toBe('aborted')
  expect((await readCommittedIntake(options(async () => new Response(new Uint8Array([255])), { timeoutMs: 120000 }))).code).toBe('response')
  await responseBoundary(view(), 33554432, (response) => readCommittedIntake(options(async () => response, { timeoutMs: 120000 })))
  const closeController = new AbortController()
  const remove = closeController.signal.removeEventListener.bind(closeController.signal)
  vi.spyOn(closeController.signal, 'removeEventListener').mockImplementation((...args) => { remove(...args); closeController.abort() })
  expect((await readCommittedIntake(options(async () => json(view()), { signal: closeController.signal, timeoutMs: 120000 }))).code).toBe('aborted')
  const late = deferred(); const lateCancel = vi.fn()
  expect((await readCommittedIntake(options(() => late.promise))).ok).toBe(false)
  late.resolve(streamResponse(async () => ({ done: true }), 200, null, lateCancel))
  await vi.waitFor(() => expect(lateCancel).toHaveBeenCalled(), { timeout: 5000 })
  const r = committed()
  const failure = await make(vi.fn(async () => new Response(new Uint8Array([255])))).readCommittedGraph({ context: context(), receipt: r })
  expect(failure.code).toBe('countsUnavailable'); expect(r.new_version.version).toBe(13)
}, 30000)
it('PVC17 retries only the committed read', async () => {
  const fetchImpl = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(json(view()))
  const client = make(fetchImpl); const r = committed(); const options = { context: context(), receipt: r }
  expect((await client.readCommittedGraph(options)).code).toBe('countsUnavailable')
  expect((await client.readCommittedGraph(options)).ok).toBe(true)
  expect(fetchImpl.mock.calls.map(([url, init]) => [url, init.method])).toEqual([['/api/drawings/solar/intake?version=13', 'GET'], ['/api/drawings/solar/intake?version=13', 'GET']])
})
it('PVC19 server refusals cannot select local success copy', async () => {
  const fields = [(code) => ({ reason_code: code }), (code) => ({ error: { reason_code: code } }), (code) => ({ error: { error_code: code } })]
  const args = { drawingId: 'solar', projectId: 'p', file: new Uint8Array([1]) }
  for (const code of ['admitted', 'converted', 'solved', 'exported', 'downloaded']) for (const field of fields) for (const status of [400, 401, 403]) {
    const result = await make(vi.fn(async () => json(field(code), status))).uploadSource(args)
    expect(result).toEqual({ ok: false, status, code: 'refused', retryable: false })
    expect(pvcaseReason(result.code, result.status)).toBe(PVCASE_REASONS.refused)
  }
  for (const reason_code of ['converted', 'UNKNOWN_CODE', 'refused']) {
    const result = await make(vi.fn(async () => json({ reason_code, error: { reason_code: 'PVS_SOURCE_INVALID' } }, 403))).uploadSource(args)
    expect(result.code).toBe('PVS_SOURCE_INVALID')
  }
})
it('PVC20 terminal job refusals cannot select success copy', async () => {
  for (const status of ['failed', 'complete']) for (const code of ['admitted', 'converted', 'solved', 'exported', 'downloaded']) {
    const fetchImpl = vi.fn(async () => json({ job_id: 'job-1', tool: 'solar-pvcase-convert', status,
      result: { ok: false, tool: 'solar-pvcase-convert', reason_code: code } }))
    const result = await make(fetchImpl).observeJob({ context: context(), source: source(), catalogRow: catalog('convert'), operation: 'convert', jobId: 'job-1' })
    expect(result).toEqual({ ok: false, status: 200, code: 'refused', retryable: false, jobId: 'job-1' })
    expect(pvcaseReason(result.code)).toBe(PVCASE_REASONS.refused)
    expect(fetchImpl.mock.calls.map(([url, init]) => [url, init.method])).toEqual([['/api/jobs/job-1', 'GET']])
  }
})
it('PVC21 measures real Blob bytes despite shadow properties', async () => {
  const fetchImpl = vi.fn(async () => json(source())); const client = make(fetchImpl)
  const oversized = new Blob([new Uint8Array(16777217)]); Object.defineProperty(oversized, 'size', { value: 1 })
  const empty = new Blob([]); Object.defineProperty(empty, 'size', { value: 1 })
  for (const [file, code] of [[oversized, 'PVG_INPUT_BYTES_EXCEEDED'], [empty, 'PVG_INVALID_JSON'],
    [{ size: 1 }, 'fileRequired'], [{ size: 1, [Symbol.toStringTag]: 'Blob' }, 'fileRequired'], [Object.create(Blob.prototype), 'fileRequired']]) {
    expect((await client.uploadSource({ drawingId: 'solar', projectId: 'p', file })).code).toBe(code)
  }
  expect(fetchImpl).not.toHaveBeenCalled()
  const file = new Blob([new Uint8Array(16777216)])
  const shadow = vi.fn(() => { throw new Error('shadow size') }); Object.defineProperty(file, 'size', { get: shadow })
  expect((await client.uploadSource({ drawingId: 'solar', projectId: 'p', file })).ok).toBe(true)
  expect(fetchImpl).toHaveBeenCalledTimes(1); expect(fetchImpl.mock.calls[0][1].body).toBe(file); expect(shadow).not.toHaveBeenCalled()
}, 30000)
