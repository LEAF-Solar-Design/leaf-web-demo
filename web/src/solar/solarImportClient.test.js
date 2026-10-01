// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import {
  SOLAREDGE_IMPORT_FALLBACK,
  SOLAREDGE_IMPORT_REASONS,
  SOLAREDGE_MAX_ALIGNMENT_TOLERANCE,
  SOLAREDGE_MIN_ALIGNMENT_TOLERANCE,
  SOLAREDGE_PDF_MAX_BYTES,
  SOLAREDGE_REPORT_REQUEST_MAX_BYTES,
  SOLAREDGE_RESPONSE_MAX_BYTES,
  SOLAREDGE_SELECTION_ORDERS,
  SOLAREDGE_TIMEOUTS_MS,
  SOLAR_ARTIFACT_MAX_BYTES,
  createSolarImportClient,
  solarEdgeReason,
  validateSolarArtifactRef,
} from './solarImportClient.js'

// Fixtures captured from the real routes (planner measurement, fixtures.json and small_report.txt).
const UPLOAD_SMALL = JSON.parse('{"degraded_mode":false,"drawing_id":"solar","error":null,"graph_sha256":"72a71ff696ea5695388cf83e66120cb3a7442a655e73c0c15b38473b50e304ad","kind":"solaredge-pdf","page_count":1,"project_id":"leaf:project:00000000-0000-4000-8000-000000000001","schema":"leaf.solar-import-source.v1","source":{"artifact_id":"949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd","byte_length":191,"content_sha256":"82285a8f6ebe74ec978fd7dacb2bf52d7d5c20a9cc8945766072c12e1656111f","download":"/api/drawings/solar/artifacts/949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd","filename":"solaredge-source.pdf","media_type":"application/pdf","schema":"leaf.solar-artifact-ref.v1","source_version":1},"source_version":1}')
const UPLOAD_C14 = JSON.parse('{"degraded_mode":false,"drawing_id":"solar","error":null,"graph_sha256":"ed057934bc0b4b1a723cadece08e86e31c3c5e6733e3b4d14c1c2e2a70037342","kind":"solaredge-pdf","page_count":1,"project_id":"leaf:project:00000000-0000-4000-8000-000000000001","schema":"leaf.solar-import-source.v1","source":{"artifact_id":"4887eecc076fd8206933323059e7c05ad01c3045963935ddc504f3a7b8d89a8a","byte_length":1019229,"content_sha256":"2e8076086b8e494295e5523b3bb94325924b3196d069e62d2f517275d678a1c1","download":"/api/drawings/solar/artifacts/4887eecc076fd8206933323059e7c05ad01c3045963935ddc504f3a7b8d89a8a","filename":"solaredge-source.pdf","media_type":"application/pdf","schema":"leaf.solar-artifact-ref.v1","source_version":1},"source_version":1}')
const REPORT_SMALL = JSON.parse('{"counts":{"assigned_panels":6,"bridge_grids":0,"bridge_strings":0,"frames":2,"group_strings":2,"matchable_grids":2,"matched_frames":2,"partial_strings":0,"pdf_matrices":2,"pdf_panels":6,"strings":2,"unassigned_panels":0},"degraded_mode":false,"drawing_id":"solar","error":null,"graph_sha256":"72a71ff696ea5695388cf83e66120cb3a7442a655e73c0c15b38473b50e304ad","kind":"solaredge-report","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","report":{"artifact_id":"9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0","byte_length":1803,"content_sha256":"f82e2d8ca1688a3a84f7227ed44a0d168c565fb038ee81882497442545b8d334","download":"/api/drawings/solar/artifacts/9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0","filename":"solaredge-report.json","media_type":"application/json","schema":"leaf.solar-artifact-ref.v1","source_version":1},"schema":"leaf.solar-solaredge-report-result.v1","source_artifact_id":"949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd","source_version":1}')
const REPORT_C14 = JSON.parse('{"counts":{"assigned_panels":3526,"bridge_grids":8,"bridge_strings":24,"frames":25,"group_strings":92,"matchable_grids":25,"matched_frames":25,"partial_strings":0,"pdf_matrices":14,"pdf_panels":3526,"strings":116,"unassigned_panels":0},"degraded_mode":false,"drawing_id":"solar","error":null,"graph_sha256":"ed057934bc0b4b1a723cadece08e86e31c3c5e6733e3b4d14c1c2e2a70037342","kind":"solaredge-report","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","report":{"artifact_id":"ef9ebf0c14a8a747cc6a0851f3e6e3dd6f86fac2631347983065c3683c549a60","byte_length":227335,"content_sha256":"7d76ddbfc97111f159b7f0dca803d9f77a118e96e2a2372d02d1c06be78ba00e","download":"/api/drawings/solar/artifacts/ef9ebf0c14a8a747cc6a0851f3e6e3dd6f86fac2631347983065c3683c549a60","filename":"solaredge-report.json","media_type":"application/json","schema":"leaf.solar-artifact-ref.v1","source_version":1},"schema":"leaf.solar-solaredge-report-result.v1","source_artifact_id":"4887eecc076fd8206933323059e7c05ad01c3045963935ddc504f3a7b8d89a8a","source_version":1}')
const REPORT_AMBIGUOUS = JSON.parse('{"cost":null,"degraded_mode":false,"error":{"actor":"user","error_code":"BAD_PARAMS","message":"REPORT_AMBIGUOUS_MATCH","next_action":"Review the inputs, correct them, and submit again.","reason_code":"REPORT_AMBIGUOUS_MATCH","retry_class":"after_action","retryable":false},"ok":false,"overlay":null,"result":null,"timing_ms":0,"tool":null,"version":null}')
const REPORT_BUSY = JSON.parse('{"cost":null,"degraded_mode":false,"error":{"actor":"service","error_code":"INTERNAL","message":"REPORT_BUSY","next_action":"Wait a short time, then retry the request.","reason_code":"REPORT_BUSY","retry_class":"backoff","retryable":true},"ok":false,"overlay":null,"result":null,"timing_ms":0,"tool":null,"version":null}')
const ENTITLEMENT_DENIED = {
  degraded_mode: false,
  entitlement_required: true,
  error: {
    actor: 'workspace_admin',
    error_code: 'ENTITLEMENT_REQUIRED',
    message: "the 'free' plan does not include uploading drawings; upgrade the workspace plan to enable uploads.",
    next_action: 'Enable this capability for the workspace, then retry.',
    retry_class: 'after_action',
    retryable: false,
  },
  required: 'upload',
  tier: 'free',
}
const POLICY_UNAVAILABLE = {
  degraded_mode: false,
  entitlement_required: true,
  error: {
    actor: 'service',
    error_code: 'INTERNAL',
    message: 'entitlement policy is unavailable; request refused (fail closed).',
    next_action: 'Wait a short time, then retry the request.',
    retry_class: 'backoff',
    retryable: true,
  },
  required: 'run_read',
  tier: 'free',
}
const SMALL_REPORT_TEXT = '{"counts":{"assigned_panels":6,"bridge_grids":0,"bridge_strings":0,"frames":2,"group_strings":2,"matchable_grids":2,"matched_frames":2,"partial_strings":0,"pdf_matrices":2,"pdf_panels":6,"strings":2,"unassigned_panels":0},"drawing_id":"solar","graph_sha256":"72a71ff696ea5695388cf83e66120cb3a7442a655e73c0c15b38473b50e304ad","matches":[{"candidate_count":2,"candidates":[0,1],"frame_ref":"leaf:frame:00000000-0000-4000-8000-000000000001","pdf_grid":0,"pdf_matrix":0,"pdf_sub_grid":-1},{"candidate_count":1,"candidates":[1],"frame_ref":"leaf:frame:00000000-0000-4000-8000-000000000002","pdf_grid":1,"pdf_matrix":1,"pdf_sub_grid":-1}],"project_id":"leaf:project:00000000-0000-4000-8000-000000000001","request":{"alignment_tolerance":1.0,"selection_order":"recorded"},"row_angle":0.0,"schema":"leaf.solar-solaredge-report.v1","source":{"artifact_id":"949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd","byte_length":191,"content_sha256":"82285a8f6ebe74ec978fd7dacb2bf52d7d5c20a9cc8945766072c12e1656111f"},"source_version":1,"strings":[{"frame_ref":"leaf:frame:00000000-0000-4000-8000-000000000001","index":0,"panel_handles":["A02","A01","A00"],"panel_refs":["leaf:panel:00000000-0000-4000-8000-000000000003","leaf:panel:00000000-0000-4000-8000-000000000002","leaf:panel:00000000-0000-4000-8000-000000000001"],"partial":false,"pdf_inverter_id":1,"pdf_matrix":0,"pdf_string_input":1,"source":"group"},{"frame_ref":"leaf:frame:00000000-0000-4000-8000-000000000002","index":1,"panel_handles":["B00","B01","B02"],"panel_refs":["leaf:panel:00000000-0000-4000-8000-000000000004","leaf:panel:00000000-0000-4000-8000-000000000005","leaf:panel:00000000-0000-4000-8000-000000000006"],"partial":false,"pdf_inverter_id":2,"pdf_matrix":1,"pdf_string_input":1,"source":"group"}],"unassigned_panel_refs":[]}'
const DOWNLOAD_HEADERS = Object.freeze({
  'content-type': 'application/json',
  'content-length': '1803',
  etag: '"f82e2d8ca1688a3a84f7227ed44a0d168c565fb038ee81882497442545b8d334"',
  'x-leaf-artifact-id': '9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0',
  'x-leaf-source-version': '1',
  'content-disposition': 'attachment; filename="solaredge-report.json"',
})
const SMALL_SOURCE_ID = '949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd'
const C14_SOURCE_ID = '4887eecc076fd8206933323059e7c05ad01c3045963935ddc504f3a7b8d89a8a'
const REPORT_REF = REPORT_SMALL.report
const PROJECT = 'leaf:project:00000000-0000-4000-8000-000000000001'

const ROUTE_CODES_TODAY = [
  'ARTIFACT_CORRUPT', 'ARTIFACT_ID_INVALID', 'ARTIFACT_NOT_FOUND', 'ARTIFACT_STALE', 'ARTIFACT_STORE_UNAVAILABLE',
  'IMPORT_DRAWING_ID_INVALID', 'IMPORT_DRAWING_NOT_FOUND', 'IMPORT_GRAPH_REQUIRED', 'IMPORT_MEDIA_TYPE_REFUSED',
  'IMPORT_NOT_A_PDF', 'IMPORT_PDF_EMPTY', 'IMPORT_PDF_ENCRYPTED', 'IMPORT_PDF_MALFORMED', 'IMPORT_PDF_NO_PAGES',
  'IMPORT_PDF_TOO_LARGE', 'IMPORT_PDF_TOO_MANY_PAGES', 'IMPORT_PROJECT_ID_INVALID', 'IMPORT_PROJECT_MISMATCH',
  'IMPORT_QUOTA_EXCEEDED', 'IMPORT_SOURCE_CONFLICT', 'IMPORT_SOURCE_CORRUPT', 'IMPORT_SOURCE_ID_INVALID',
  'IMPORT_SOURCE_INVALID', 'IMPORT_SOURCE_KIND_MISMATCH', 'IMPORT_SOURCE_NOT_FOUND', 'IMPORT_STORE_UNAVAILABLE',
  'IMPORT_WRITES_DRAINED',
  'REPORT_AMBIGUOUS_MATCH', 'REPORT_ARTIFACT_CONFLICT', 'REPORT_ARTIFACT_CORRUPT', 'REPORT_ARTIFACT_INVALID',
  'REPORT_BRIDGE_UNRESOLVED', 'REPORT_BUSY', 'REPORT_DRAWING_ID_INVALID', 'REPORT_DRAWING_NOT_FOUND',
  'REPORT_FRAMES_REQUIRED', 'REPORT_FRAME_EMPTY', 'REPORT_GRAPH_REQUIRED', 'REPORT_HANDLE_ALIAS',
  'REPORT_LIMIT_EXCEEDED', 'REPORT_MEDIA_TYPE_REFUSED', 'REPORT_NO_MATCH', 'REPORT_PANEL_HANDLE_DUPLICATE',
  'REPORT_PANEL_HANDLE_INVALID', 'REPORT_PARSE_TIMEOUT', 'REPORT_PARSE_UNAVAILABLE', 'REPORT_PDF_UNSUPPORTED', 'REPORT_PROJECT_MISMATCH', 'REPORT_REQUEST_INVALID',
  'REPORT_REQUEST_TOO_LARGE', 'REPORT_ROW_ANGLE_UNRESOLVED', 'REPORT_STORE_UNAVAILABLE', 'REPORT_UNITS_UNRESOLVED',
  'REPORT_WRITES_DRAINED',
]
const ACCEPT_CODES = [
  'INVALID_SOLAREDGE_ACCEPT_REQUEST', 'SOLAREDGE_REPORT_AMBIGUOUS', 'SOLAREDGE_REPORT_CORRUPT',
  'SOLAREDGE_REPORT_INVALID', 'SOLAREDGE_REPORT_KIND_MISMATCH', 'SOLAREDGE_REPORT_NOT_FOUND',
  'SOLAREDGE_REPORT_REQUIRED', 'SOLAREDGE_REPORT_STALE', 'SOLAREDGE_REPORT_UNAVAILABLE',
]
const CLIENT_AND_SESSION_CODES = [
  'ENTITLEMENT_POLICY_UNAVAILABLE', 'ENTITLEMENT_REQUIRED', 'SOLAREDGE_CLIENT_ABORTED',
  'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', 'SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE', 'SOLAREDGE_CLIENT_NETWORK',
  'SOLAREDGE_CLIENT_REQUEST_INVALID', 'SOLAREDGE_CLIENT_RESPONSE_INVALID', 'SOLAREDGE_CLIENT_TIMEOUT',
  'STALE_GRAPH_REVISION', 'UNAUTHENTICATED',
]

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}
const canonicalSha = (value) => createHash('sha256').update(canonical(value)).digest('hex')
const clone = (value) => JSON.parse(JSON.stringify(value))
const withoutEnvelope = (body) => {
  const copy = clone(body)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
const sameShape = (code, base = REPORT_AMBIGUOUS) => {
  const copy = clone(base)
  copy.error.message = code
  copy.error.reason_code = code
  return copy
}
const jsonResponse = (body, status = 200, headers = {}) => new Response(
  typeof body === 'string' ? body : JSON.stringify(body),
  { status, headers: { 'content-type': 'application/json', ...headers } },
)
const answering = (makeResponse) => vi.fn(async () => makeResponse())
const neverAnswers = () => vi.fn((url, init) => new Promise((resolve, reject) => {
  init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
}))
const tenantHeaders = () => ({ 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' })
function clientWith(fetchImpl, extra = {}) {
  const onResponse = vi.fn()
  const client = createSolarImportClient({
    fetchImpl, apiBase: 'https://studio.test', headers: tenantHeaders, onResponse, ...extra,
  })
  return { client, onResponse }
}
const refused = (status, code, retryable) => ({ ok: false, status, code, retryable })
const pdfBlob = () => new Blob([new Uint8Array(191)], { type: 'application/pdf' })
const reportArgs = (overrides = {}) => ({
  drawingId: 'solar', sourceArtifactId: SMALL_SOURCE_ID, alignmentTolerance: 1, selectionOrder: 'recorded',
  ...overrides,
})
const reportBytes = () => new TextEncoder().encode(SMALL_REPORT_TEXT)
function readServer(relative) {
  const buffer = readFileSync(new URL(`../../../server/${relative}`, import.meta.url))
  expect(buffer.length).toBeLessThanOrEqual(1024 * 1024)
  return buffer.toString('utf8')
}
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const hasLine = (text, line) => new RegExp(`^${escapeRegExp(line)}\\r?$`, 'm').test(text)
// The union of the three SolarEdge route refusal maps in server/routers/drawings.py.
function routeMapUnion() {
  const text = readServer('routers/drawings.py')
  const union = new Set()
  for (const marker of ['async def import_solaredge_pdf(', 'async def solaredge_pdf_report(', 'def get_artifact(']) {
    const start = text.indexOf(marker)
    expect(start).toBeGreaterThanOrEqual(0)
    const end = text.indexOf('\n@router.', start)
    const section = text.slice(start, end === -1 ? text.length : end)
    const codes = [...section.matchAll(/"([A-Z][A-Z0-9_]{0,63})":\s*\((\d{3}),\s*ErrorCode\.[A-Z_]+,\s*(True|False)\)/g)]
      .map((match) => match[1])
    expect(codes.length).toBeGreaterThanOrEqual(5)
    for (const code of codes) union.add(code)
  }
  return union
}
// Bounds how long a call may take to settle; a hung call resolves to HUNG instead of stalling the row.
const HUNG = 'hung'
async function settleWithin(promise, ms = 1000) {
  let timer
  const bound = new Promise((resolve) => { timer = setTimeout(() => resolve(HUNG), ms) })
  try {
    return await Promise.race([promise, bound])
  } finally {
    clearTimeout(timer)
  }
}
const STALL_HEADERS = Object.freeze({
  'content-type': 'application/json', 'x-leaf-artifact-id': REPORT_SMALL.report.artifact_id,
})
// A Response whose headers arrive and whose body never ends.
const stalledResponse = (status = 200) => new Response(new ReadableStream({ start() {} }), {
  status, headers: STALL_HEADERS,
})
const FAST_BUDGETS = Object.freeze({ timeouts: { upload: 30, report: 30, artifact: 30 } })
const everyRoute = [
  (client, signal) => client.uploadPdf({ drawingId: 'solar', file: pdfBlob(), signal }),
  (client, signal) => client.requestReport(reportArgs({ signal })),
  (client, signal) => client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF, signal }),
]
// A body whose next chunk is always ready: pull() enqueues one byte synchronously, so every read
// settles on the microtask queue and no timer runs between reads. spinMs models a slow producer.
const oneBytePerPull = (bytes, spinMs = 0) => {
  let index = 0
  return new ReadableStream({
    pull(controller) {
      const until = performance.now() + spinMs
      while (spinMs > 0 && performance.now() < until) {
        // A synchronous producer: the next byte is ready without yielding to any timer.
      }
      if (index < bytes.length) {
        controller.enqueue(bytes.subarray(index, index + 1))
        index += 1
      } else {
        controller.close()
      }
    },
  })
}
// A body of 100,000 empty chunks, all enqueued at once, then the real bytes.
const emptyChunksThen = (bytes) => new ReadableStream({
  start(controller) {
    for (let index = 0; index < 100_000; index += 1) controller.enqueue(new Uint8Array(0))
    controller.enqueue(bytes)
    controller.close()
  },
})
const spacesThen = (body) => new TextEncoder().encode(`${' '.repeat(60_000)}${JSON.stringify(body)}`)
// Each route with the body a flood test serves on a 200.
const floodRoutes = [
  { run: everyRoute[0], bytes: () => spacesThen(UPLOAD_SMALL), json: UPLOAD_SMALL, spinMs: 0 },
  { run: everyRoute[1], bytes: () => spacesThen(REPORT_SMALL), json: REPORT_SMALL, spinMs: 0 },
  // 1803 bytes alone read faster than the budget, so the producer is slowed to outlast it.
  { run: everyRoute[2], bytes: reportBytes, json: null, spinMs: 0.1 },
]
const floodHeaders = (json) => (json === null ? DOWNLOAD_HEADERS : { 'content-type': 'application/json' })
const countAbort = (spy) => spy.mock.calls.filter(([type]) => type === 'abort').length

describe('solar import client', () => {
  it('SI1 constants and the reason map', () => {
    expect(SOLAREDGE_PDF_MAX_BYTES).toBe(16_777_216)
    expect(SOLAREDGE_REPORT_REQUEST_MAX_BYTES).toBe(4096)
    expect(SOLAR_ARTIFACT_MAX_BYTES).toBe(16_777_216)
    expect(SOLAREDGE_RESPONSE_MAX_BYTES).toBe(65_536)
    expect(SOLAREDGE_MIN_ALIGNMENT_TOLERANCE).toBe(1e-6)
    expect(SOLAREDGE_MAX_ALIGNMENT_TOLERANCE).toBe(1e6)
    expect(SOLAREDGE_SELECTION_ORDERS).toEqual(['unknown', 'recorded'])
    expect(SOLAREDGE_TIMEOUTS_MS).toEqual({ upload: 120_000, report: 120_000, artifact: 60_000 })
    expect(SOLAREDGE_IMPORT_FALLBACK).toBe('The SolarEdge import stopped')
    expect(Object.isFrozen(SOLAREDGE_SELECTION_ORDERS)).toBe(true)
    expect(Object.isFrozen(SOLAREDGE_TIMEOUTS_MS)).toBe(true)
    expect(Object.isFrozen(SOLAREDGE_IMPORT_REASONS)).toBe(true)
    for (const list of [ROUTE_CODES_TODAY, ACCEPT_CODES, CLIENT_AND_SESSION_CODES]) {
      expect(list).toEqual([...list].sort())
    }
    const allCodes = [...ROUTE_CODES_TODAY, ...ACCEPT_CODES, ...CLIENT_AND_SESSION_CODES].sort()
    expect(allCodes).toHaveLength(74)
    expect(Object.keys(SOLAREDGE_IMPORT_REASONS).sort()).toEqual(allCodes)
    for (const sentence of Object.values(SOLAREDGE_IMPORT_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence.length).toBeLessThanOrEqual(120)
      expect(sentence).not.toMatch(/[;–—]/)
      expect(sentence.endsWith('.')).toBe(false)
    }
  })

  it('SI2 route codes follow the server route maps', () => {
    const text = readServer('routers/drawings.py')
    const union = new Set()
    for (const marker of ['async def import_solaredge_pdf(', 'async def solaredge_pdf_report(', 'def get_artifact(']) {
      const start = text.indexOf(marker)
      expect(start).toBeGreaterThanOrEqual(0)
      const end = text.indexOf('\n@router.', start)
      const section = text.slice(start, end === -1 ? text.length : end)
      const codes = [...section.matchAll(/"([A-Z][A-Z0-9_]{0,63})":\s*\((\d{3}),\s*ErrorCode\.[A-Z_]+,\s*(True|False)\)/g)]
        .map((match) => match[1])
      expect(codes.length).toBeGreaterThanOrEqual(5)
      for (const code of codes) union.add(code)
    }
    for (const code of ROUTE_CODES_TODAY) expect(union.has(code)).toBe(true)
    const routeKeys = Object.keys(SOLAREDGE_IMPORT_REASONS)
      .filter((code) => /^(IMPORT_|REPORT_|ARTIFACT_)/.test(code))
    expect(routeKeys.filter((code) => !union.has(code))).toEqual([])
  })

  it('SI3 accept codes and limits follow the server constants', () => {
    const tracking = readServer('solar_solaredge_tracking.py')
    const accept = [...tracking.matchAll(/^(?:INVALID|REPORT_[A-Z_]+) = "([A-Z][A-Z0-9_]{0,63})"\r?$/gm)]
      .map((match) => match[1]).sort()
    expect(accept).toEqual(ACCEPT_CODES)
    for (const code of ACCEPT_CODES) expect(Object.hasOwn(SOLAREDGE_IMPORT_REASONS, code)).toBe(true)
    expect(readServer('solar_design_graph.py')).toContain('raise GraphValidationError("STALE_GRAPH_REVISION")')
    const sources = readServer('solar_import_sources.py')
    for (const line of ['MAX_IMPORT_PDF_BYTES = 16_777_216', 'MAX_IMPORT_PAGES = 50', 'MAX_SOURCES_PER_DRAWING = 8']) {
      expect(hasLine(sources, line)).toBe(true)
    }
    const report = readServer('solar_solaredge_report.py')
    for (const line of [
      'MAX_REPORT_REQUEST_BYTES = 4096', 'MIN_ALIGNMENT_TOLERANCE = 1e-6', 'MAX_ALIGNMENT_TOLERANCE = 1e6',
      'SELECTION_ORDERS = ("unknown", "recorded")',
    ]) {
      expect(hasLine(report, line)).toBe(true)
    }
    expect(hasLine(readServer('solar_artifacts.py'), 'MAX_ARTIFACT_BYTES = 16_777_216')).toBe(true)
  })

  it('SI4 solarEdgeReason gives a sentence or a bounded fallback', () => {
    expect(solarEdgeReason('REPORT_BUSY')).toBe('Other PDFs are being read right now, so try again shortly')
    expect(solarEdgeReason('REPORT_PARSE_FUTURE')).toBe('The SolarEdge import stopped (REPORT_PARSE_FUTURE)')
    for (const value of ['bad code', 'x'.repeat(65), 'A'.repeat(65), null, 7, 'constructor', 'toString']) {
      expect(solarEdgeReason(value)).toBe('The SolarEdge import stopped')
    }
  })

  it('SI5 upload of the C15 PDF', async () => {
    const fetchImpl = answering(() => jsonResponse(UPLOAD_SMALL))
    const { client, onResponse } = clientWith(fetchImpl)
    const file = pdfBlob()
    const result = await client.uploadPdf({ drawingId: 'solar', file })
    expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(UPLOAD_SMALL) })
    expect(Object.keys(result.value).sort()).toEqual([
      'drawing_id', 'graph_sha256', 'kind', 'page_count', 'project_id', 'schema', 'source', 'source_version',
    ])
    expect(canonicalSha(result.value)).toBe('15c3cd4fbc42b6328716c348360eff7b3556d3e23ca090acdac89a7a75a81bec')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('https://studio.test/api/drawings/solar/imports/solaredge-pdf')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(file)
    expect(init.headers).toEqual({
      'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t', 'Content-Type': 'application/pdf',
    })
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(onResponse).toHaveBeenCalledTimes(1)
    const [response, path, authorization] = onResponse.mock.calls[0]
    expect(response).toBeInstanceOf(Response)
    expect(path).toBe('/api/drawings/solar/imports/solaredge-pdf')
    expect(authorization).toBe('Bearer t')
  })

  it('SI6 upload of the C14 PDF with a project', async () => {
    const fetchImpl = answering(() => jsonResponse(UPLOAD_C14))
    const { client, onResponse } = clientWith(fetchImpl)
    const result = await client.uploadPdf({ drawingId: 'solar', file: new Uint8Array(1019229), projectId: PROJECT })
    expect(result.ok).toBe(true)
    expect(result.value).toEqual(withoutEnvelope(UPLOAD_C14))
    expect(canonicalSha(result.value)).toBe('2e0094156d47960c1485d0611c2e5a445017ba3d002755e9555aa08d15090b22')
    expect(result.value.source.byte_length).toBe(1019229)
    const [url] = fetchImpl.mock.calls[0]
    expect(url.endsWith('/imports/solaredge-pdf?project_id=leaf%3Aproject%3A00000000-0000-4000-8000-000000000001'))
      .toBe(true)
    expect(onResponse.mock.calls[0][1]).toBe('/api/drawings/solar/imports/solaredge-pdf')
  })

  it('SI7 upload prechecks send nothing', async () => {
    const cases = [
      [{ drawingId: 'Solar', file: pdfBlob() }, 'IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: 'a'.repeat(64), file: pdfBlob() }, 'IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: 'solar', file: pdfBlob(), projectId: '' }, 'IMPORT_PROJECT_ID_INVALID'],
      [{ drawingId: 'solar', file: pdfBlob(), projectId: 'p'.repeat(101) }, 'IMPORT_PROJECT_ID_INVALID'],
      [{ drawingId: 'solar', file: 'text' }, 'SOLAREDGE_CLIENT_REQUEST_INVALID'],
      [{ drawingId: 'solar', file: null }, 'SOLAREDGE_CLIENT_REQUEST_INVALID'],
      [{ drawingId: 'solar', file: { size: 10 } }, 'SOLAREDGE_CLIENT_REQUEST_INVALID'],
      [{ drawingId: 'solar', file: new Blob([]) }, 'IMPORT_PDF_EMPTY'],
      [{ drawingId: 'solar', file: new ArrayBuffer(0) }, 'IMPORT_PDF_EMPTY'],
      [{ drawingId: 'solar', file: new Uint8Array(16_777_217) }, 'IMPORT_PDF_TOO_LARGE'],
    ]
    for (const [args, code] of cases) {
      const fetchImpl = answering(() => jsonResponse(UPLOAD_SMALL))
      const { client } = clientWith(fetchImpl)
      await expect(client.uploadPdf(args)).resolves.toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    const fetchImpl = answering(() => jsonResponse(UPLOAD_SMALL))
    const { client } = clientWith(fetchImpl)
    const atLimit = await client.uploadPdf({ drawingId: 'solar', file: new Uint8Array(16_777_216) })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(atLimit.ok).toBe(true)
  })

  it('SI8 upload refusals carry the server code and its retry flag', async () => {
    const cases = [
      [sameShape('IMPORT_MEDIA_TYPE_REFUSED'), 415, refused(415, 'IMPORT_MEDIA_TYPE_REFUSED', false)],
      [sameShape('IMPORT_NOT_A_PDF'), 400, refused(400, 'IMPORT_NOT_A_PDF', false)],
      [sameShape('IMPORT_PDF_EMPTY'), 400, refused(400, 'IMPORT_PDF_EMPTY', false)],
      [clone(REPORT_BUSY), 503, refused(503, 'REPORT_BUSY', true)],
    ]
    for (const [body, status, expected] of cases) {
      const { client } = clientWith(answering(() => jsonResponse(body, status)))
      await expect(client.uploadPdf({ drawingId: 'solar', file: pdfBlob() })).resolves.toEqual(expected)
    }
  })

  it('SI9 upload success bodies that break the shape are refused', async () => {
    const id = UPLOAD_SMALL.source.artifact_id
    const edits = [
      (b) => { b.schema = 'leaf.solar-import-source.v2' },
      (b) => { b.kind = 'pdf' },
      (b) => { b.drawing_id = 'other' },
      (b) => { b.page_count = 0 },
      (b) => { b.page_count = 51 },
      (b) => { b.page_count = 1.5 },
      (b) => { b.source_version = 0 },
      (b) => { b.graph_sha256 = b.graph_sha256.toUpperCase() },
      (b) => { b.x = 1 },
      (b) => { b.error = {} },
      (b) => { b.degraded_mode = 'no' },
      (b) => { delete b.page_count },
      (b) => { b.source.download = `/api/drawings/other/artifacts/${id}` },
      (b) => { b.source.media_type = 'application/json' },
      (b) => { b.source.filename = '../solaredge-source.pdf' },
      (b) => { b.source.byte_length = 16777217 },
      (b) => { b.source.source_version = 2 },
      (b) => { b.source.extra = 1 },
    ]
    const invalid = refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)
    for (const edit of edits) {
      const body = clone(UPLOAD_SMALL)
      edit(body)
      const { client } = clientWith(answering(() => jsonResponse(body)))
      await expect(client.uploadPdf({ drawingId: 'solar', file: pdfBlob() })).resolves.toEqual(invalid)
    }
    const notJson = clientWith(answering(() => jsonResponse('not json'))).client
    await expect(notJson.uploadPdf({ drawingId: 'solar', file: pdfBlob() })).resolves.toEqual(invalid)
    const text = vi.fn(async () => JSON.stringify(UPLOAD_SMALL))
    const oversized = clientWith(answering(() => ({
      status: 200, headers: new Headers({ 'content-length': '70000' }), text,
    }))).client
    await expect(oversized.uploadPdf({ drawingId: 'solar', file: pdfBlob() })).resolves.toEqual(invalid)
    expect(text).not.toHaveBeenCalled()
    const created = clientWith(answering(() => jsonResponse(UPLOAD_SMALL, 201))).client
    await expect(created.uploadPdf({ drawingId: 'solar', file: pdfBlob() }))
      .resolves.toEqual(refused(201, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))
  })

  it('SI10 report for the C15 PDF', async () => {
    const fetchImpl = answering(() => jsonResponse(REPORT_SMALL))
    const { client, onResponse } = clientWith(fetchImpl)
    const result = await client.requestReport(reportArgs())
    expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(REPORT_SMALL) })
    expect(canonicalSha(result.value)).toBe('71afc006edd3f2dcbb8ed46156d3b2a1ad4ab009427844faeb7c06c60092494f')
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('https://studio.test/api/drawings/solar/imports/solaredge-pdf/report')
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({
      'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t', 'Content-Type': 'application/json',
    })
    expect(init.body).toBe(
      '{"source_artifact_id":"949533a2c62f973034fbb40ab738ca4f3267997236b20a09e437dc07665c61fd","alignment_tolerance":1,"selection_order":"recorded"}',
    )
    expect(onResponse.mock.calls[0][1]).toBe('/api/drawings/solar/imports/solaredge-pdf/report')
    const withProject = answering(() => jsonResponse(REPORT_SMALL))
    await clientWith(withProject).client.requestReport(reportArgs({ projectId: 'p' }))
    expect(withProject.mock.calls[0][1].body.endsWith(',"project_id":"p"}')).toBe(true)
  })

  it('SI11 report for the C14 PDF', async () => {
    const { client } = clientWith(answering(() => jsonResponse(REPORT_C14)))
    const result = await client.requestReport({
      drawingId: 'solar', sourceArtifactId: C14_SOURCE_ID, alignmentTolerance: 12, selectionOrder: 'unknown',
    })
    expect(result.ok).toBe(true)
    expect(result.value).toEqual(withoutEnvelope(REPORT_C14))
    expect(canonicalSha(result.value)).toBe('459ec2ed0e7df64557fa4a4159f33054d76a8354721e79df25c0db15fc0232db')
    expect(result.value.counts.strings).toBe(116)
    expect(result.value.counts.assigned_panels).toBe(3526)
    expect(result.value.report.byte_length).toBe(227335)
  })

  it('SI12 report prechecks send nothing', async () => {
    const cases = [
      [{ alignmentTolerance: 0 }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: 1e-7 }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: 1000001 }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: NaN }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: Infinity }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: '12' }, 'REPORT_REQUEST_INVALID'],
      [{ alignmentTolerance: true }, 'REPORT_REQUEST_INVALID'],
      [{ sourceArtifactId: 'A'.repeat(64) }, 'REPORT_REQUEST_INVALID'],
      [{ sourceArtifactId: 'a'.repeat(63) }, 'REPORT_REQUEST_INVALID'],
      [{ selectionOrder: 'first' }, 'REPORT_REQUEST_INVALID'],
      [{ selectionOrder: null }, 'REPORT_REQUEST_INVALID'],
      [{ projectId: '' }, 'REPORT_REQUEST_INVALID'],
      [{ drawingId: 'Solar' }, 'REPORT_DRAWING_ID_INVALID'],
    ]
    for (const [overrides, code] of cases) {
      const fetchImpl = answering(() => jsonResponse(REPORT_SMALL))
      const { client } = clientWith(fetchImpl)
      await expect(client.requestReport(reportArgs(overrides))).resolves.toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    for (const alignmentTolerance of [1e-6, 1e6]) {
      const fetchImpl = answering(() => jsonResponse(REPORT_SMALL))
      const { client } = clientWith(fetchImpl)
      const result = await client.requestReport(reportArgs({ alignmentTolerance }))
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      expect(result.ok).toBe(true)
    }
  })

  it('SI13 report refusals carry the server code and its retry flag', async () => {
    const future = sameShape('REPORT_PARSE_FUTURE', REPORT_BUSY)
    const cases = [
      [REPORT_AMBIGUOUS, 409, refused(409, 'REPORT_AMBIGUOUS_MATCH', false)],
      [REPORT_BUSY, 503, refused(503, 'REPORT_BUSY', true)],
      [sameShape('IMPORT_SOURCE_NOT_FOUND'), 404, refused(404, 'IMPORT_SOURCE_NOT_FOUND', false)],
      [sameShape('REPORT_REQUEST_INVALID'), 400, refused(400, 'REPORT_REQUEST_INVALID', false)],
      [future, 503, refused(503, 'REPORT_PARSE_FUTURE', true)],
    ]
    for (const [body, status, expected] of cases) {
      const { client } = clientWith(answering(() => jsonResponse(body, status)))
      await expect(client.requestReport(reportArgs())).resolves.toEqual(expected)
    }
    expect(solarEdgeReason('REPORT_PARSE_FUTURE')).toBe('The SolarEdge import stopped (REPORT_PARSE_FUTURE)')
  })

  it('SI14 report success bodies that break the shape are refused', async () => {
    const edits = [
      (b) => { b.source_artifact_id = 'a'.repeat(64) },
      (b) => { b.kind = 'report' },
      (b) => { delete b.counts.strings },
      (b) => { b.counts.extra = 1 },
      (b) => { b.counts.frames = -1 },
      (b) => { b.counts.frames = 1.5 },
      (b) => { b.counts.frames = true },
      (b) => { b.counts.frames = 10000001 },
      (b) => { b.report.filename = 'x.json' },
      (b) => { b.report.media_type = 'application/pdf' },
      (b) => { b.report.source_version = 2 },
    ]
    for (const edit of edits) {
      const body = clone(REPORT_SMALL)
      edit(body)
      const { client } = clientWith(answering(() => jsonResponse(body)))
      await expect(client.requestReport(reportArgs()))
        .resolves.toEqual(refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))
    }
  })

  it('SI15 session and plan refusals', async () => {
    const unauthorized = clientWith(answering(() => jsonResponse({ anything: true }, 401)))
    await expect(unauthorized.client.uploadPdf({ drawingId: 'solar', file: pdfBlob() }))
      .resolves.toEqual(refused(401, 'UNAUTHENTICATED', false))
    expect(unauthorized.onResponse).toHaveBeenCalledTimes(1)
    const denied = clientWith(answering(() => jsonResponse(ENTITLEMENT_DENIED, 403))).client
    await expect(denied.uploadPdf({ drawingId: 'solar', file: pdfBlob() }))
      .resolves.toEqual(refused(403, 'ENTITLEMENT_REQUIRED', false))
    const policy = clientWith(answering(() => jsonResponse(POLICY_UNAVAILABLE, 503))).client
    await expect(policy.requestReport(reportArgs()))
      .resolves.toEqual(refused(503, 'ENTITLEMENT_POLICY_UNAVAILABLE', true))
    const odd = clientWith(answering(() => jsonResponse(POLICY_UNAVAILABLE, 500))).client
    await expect(odd.requestReport(reportArgs()))
      .resolves.toEqual(refused(500, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', true))
    const throwing = clientWith(answering(() => jsonResponse(UPLOAD_SMALL)), {
      onResponse: () => { throw new Error('observer failed') },
    }).client
    await expect(throwing.uploadPdf({ drawingId: 'solar', file: pdfBlob() }))
      .resolves.toEqual({ ok: true, status: 200, value: withoutEnvelope(UPLOAD_SMALL) })
  })

  it('SI16 transport failures resolve and never reject', async () => {
    const network = clientWith(vi.fn(async () => { throw new TypeError('failed') })).client
    await expect(network.requestReport(reportArgs()))
      .resolves.toEqual(refused(null, 'SOLAREDGE_CLIENT_NETWORK', true))

    const slow = clientWith(neverAnswers(), { timeouts: { report: 20 } }).client
    const started = Date.now()
    await expect(slow.requestReport(reportArgs())).resolves.toEqual(refused(null, 'SOLAREDGE_CLIENT_TIMEOUT', true))
    expect(Date.now() - started).toBeLessThan(2000)

    const cancelled = clientWith(neverAnswers()).client
    const controller = new AbortController()
    setTimeout(() => controller.abort(), 5)
    await expect(cancelled.requestReport(reportArgs({ signal: controller.signal })))
      .resolves.toEqual(refused(null, 'SOLAREDGE_CLIENT_ABORTED', false))

    const gateway = clientWith(answering(() => new Response('Bad gateway', { status: 502 }))).client
    await expect(gateway.requestReport(reportArgs()))
      .resolves.toEqual(refused(502, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', true))

    const badCode = sameShape('REPORT_BUSY')
    badCode.error.reason_code = 'bad-code'
    const malformed = clientWith(answering(() => jsonResponse(badCode, 409))).client
    await expect(malformed.requestReport(reportArgs()))
      .resolves.toEqual(refused(409, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))

    const fetchImpl = answering(() => jsonResponse(REPORT_SMALL))
    const badHeaders = clientWith(fetchImpl, { headers: () => ({ 'X-Tenant-Id': 7 }) }).client
    await expect(badHeaders.requestReport(reportArgs()))
      .resolves.toEqual(refused(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('SI17 download of the C15 report', async () => {
    const fetchImpl = answering(() => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS }))
    const { client, onResponse } = clientWith(fetchImpl)
    const result = await client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF })
    expect(result.ok).toBe(true)
    expect(result.status).toBe(200)
    expect(Object.keys(result).sort()).toEqual(['ok', 'status', 'value'])
    const { value } = result
    expect(value.artifactId).toBe('9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0')
    expect(value.mediaType).toBe('application/json')
    expect(value.filename).toBe('solaredge-report.json')
    expect(value.byteLength).toBe(1803)
    expect(value.bytes).toBeInstanceOf(Uint8Array)
    expect(value.bytes.length).toBe(1803)
    expect(createHash('sha256').update(value.bytes).digest('hex'))
      .toBe('f82e2d8ca1688a3a84f7227ed44a0d168c565fb038ee81882497442545b8d334')
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe('https://studio.test/api/drawings/solar/artifacts/9b1ac1ff97a87f33eedb38096a1a654ab0fc3ea10ddaefc52920a7b6d0953dd0')
    expect(init.method).toBe('GET')
    expect(init.headers).toEqual({ 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' })
    expect(onResponse.mock.calls[0][1]).toBe(REPORT_REF.download)
    const current = answering(() => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS }))
    const again = await clientWith(current).client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF, current: true })
    expect(again.ok).toBe(true)
    expect(current.mock.calls[0][0].endsWith('?current=true')).toBe(true)
  })

  it('SI18 download bytes that do not match their record are refused', async () => {
    const mismatch = refused(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
    const changed = reportBytes()
    changed[0] = 0x5b
    const short = reportBytes().slice(0, 1802)
    const cases = [
      () => new Response(changed, { status: 200, headers: DOWNLOAD_HEADERS }),
      () => new Response(short, { status: 200, headers: { ...DOWNLOAD_HEADERS, 'content-length': '1802' } }),
      () => new Response(reportBytes(), {
        status: 200, headers: { ...DOWNLOAD_HEADERS, 'x-leaf-artifact-id': 'c'.repeat(64) },
      }),
    ]
    for (const makeResponse of cases) {
      const { client } = clientWith(answering(makeResponse))
      await expect(client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF })).resolves.toEqual(mismatch)
    }
    const arrayBuffer = vi.fn(async () => new ArrayBuffer(0))
    const huge = clientWith(answering(() => ({
      status: 200,
      headers: new Headers({ 'content-length': '16777217', 'x-leaf-artifact-id': REPORT_REF.artifact_id }),
      arrayBuffer,
    }))).client
    await expect(huge.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF })).resolves.toEqual(mismatch)
    expect(arrayBuffer).not.toHaveBeenCalled()
    const noDigest = clientWith(
      answering(() => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS })),
      { sha256Hex: () => Promise.reject(new Error('no digest')) },
    ).client
    await expect(noDigest.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF }))
      .resolves.toEqual(refused(200, 'SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE', false))
  })

  it('SI19 download prechecks send nothing', async () => {
    const edited = (edit) => {
      const ref = clone(REPORT_REF)
      edit(ref)
      return ref
    }
    const cases = [
      { drawingId: 'solar', ref: edited((r) => { r.download = `/api/drawings/other/artifacts/${r.artifact_id}` }) },
      { drawingId: 'solar', ref: edited((r) => { r.artifact_id = r.artifact_id.toUpperCase() }) },
      { drawingId: 'solar', ref: edited((r) => { r.byte_length = 0 }) },
      { drawingId: 'solar', ref: REPORT_REF, maxBytes: 1000 },
      { drawingId: 'solar', ref: edited((r) => { r.filename = '../x.json' }) },
      { drawingId: 'solar', ref: edited((r) => { r.media_type = 'text/html' }) },
      { drawingId: 'solar', ref: edited((r) => { r.schema = 'leaf.solar-artifact-ref.v2' }) },
      { drawingId: 'solar', ref: edited((r) => { r.extra = 1 }) },
      { drawingId: 'solar', ref: REPORT_REF, current: 'yes' },
      { drawingId: 'Solar', ref: REPORT_REF },
    ]
    for (const args of cases) {
      const fetchImpl = answering(() => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS }))
      const { client } = clientWith(fetchImpl)
      await expect(client.downloadArtifact(args)).resolves.toEqual(refused(null, 'ARTIFACT_ID_INVALID', false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
  })

  it('SI20 download server refusals', async () => {
    const cases = [
      [sameShape('ARTIFACT_NOT_FOUND'), 404, refused(404, 'ARTIFACT_NOT_FOUND', false)],
      [sameShape('ARTIFACT_STALE'), 409, refused(409, 'ARTIFACT_STALE', false)],
      [sameShape('ARTIFACT_ID_INVALID'), 400, refused(400, 'ARTIFACT_ID_INVALID', false)],
    ]
    for (const [body, status, expected] of cases) {
      const { client } = clientWith(answering(() => jsonResponse(body, status)))
      await expect(client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF })).resolves.toEqual(expected)
    }
  })

  it('SI21 creation guards', () => {
    const fetchImpl = vi.fn()
    const base = { fetchImpl, headers: tenantHeaders }
    const bad = [
      { headers: tenantHeaders },
      { fetchImpl, headers: 'x' },
      { ...base, apiBase: 'a'.repeat(2049) },
      { ...base, apiBase: 7 },
      { ...base, onResponse: 5 },
      { ...base, sha256Hex: 'x' },
      { ...base, timeouts: { upload: 0 } },
      { ...base, timeouts: { upload: 600001 } },
      { ...base, timeouts: { upload: 1.5 } },
      { ...base, timeouts: { other: 10 } },
    ]
    for (const deps of bad) expect(() => createSolarImportClient(deps)).toThrow(TypeError)
    const client = createSolarImportClient(base)
    expect(Object.isFrozen(client)).toBe(true)
    expect(Object.keys(client).sort()).toEqual(['downloadArtifact', 'requestReport', 'uploadPdf'])
  })

  it('SI22 a body that never ends settles at the deadline', async () => {
    for (const run of everyRoute) {
      const { client } = clientWith(answering(() => stalledResponse(200)), FAST_BUDGETS)
      const result = await settleWithin(run(client))
      expect(result).toEqual(refused(200, 'SOLAREDGE_CLIENT_TIMEOUT', true))
    }
    const { client } = clientWith(answering(() => stalledResponse(503)), FAST_BUDGETS)
    expect(await settleWithin(client.requestReport(reportArgs())))
      .toEqual(refused(503, 'SOLAREDGE_CLIENT_TIMEOUT', true))
  })

  it('SI23 a caller abort after the headers settles the call', async () => {
    for (const run of everyRoute) {
      const controller = new AbortController()
      let received = null
      const fetchImpl = vi.fn(async (url, init) => {
        received = init.signal
        return stalledResponse(200)
      })
      const { client } = clientWith(fetchImpl, { onResponse: () => { setTimeout(() => controller.abort(), 5) } })
      const result = await settleWithin(run(client, controller.signal))
      expect(result).toEqual(refused(200, 'SOLAREDGE_CLIENT_ABORTED', false))
      expect(received).toBeInstanceOf(AbortSignal)
      expect(received.aborted).toBe(true)
    }
  })

  it('SI24 an oversized declared length is refused before any read', async () => {
    const lengths = ['10000000000000000', String(SOLAREDGE_RESPONSE_MAX_BYTES + 1)]
    for (const [length, status, expected] of [
      [lengths[0], 200, refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)],
      [lengths[1], 200, refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)],
      [lengths[0], 409, refused(409, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)],
    ]) {
      const text = vi.fn(async () => JSON.stringify(status === 200 ? UPLOAD_SMALL : REPORT_AMBIGUOUS))
      const arrayBuffer = vi.fn(async () => new ArrayBuffer(0))
      const getReader = vi.fn(() => { throw new Error('the body was read') })
      const { client } = clientWith(answering(() => ({
        status, headers: new Headers({ 'content-length': length }), text, arrayBuffer, body: { getReader },
      })))
      await expect(client.uploadPdf({ drawingId: 'solar', file: pdfBlob() })).resolves.toEqual(expected)
      expect(text).not.toHaveBeenCalled()
      expect(arrayBuffer).not.toHaveBeenCalled()
      expect(getReader).not.toHaveBeenCalled()
    }
  })

  it('SI25 the response bound counts bytes, not string length', async () => {
    const pad = 'é'.repeat(40_000)
    // A duplicate key parses to the valid body, so only the byte bound can refuse it.
    const padded = `{"project_id":"${pad}",${JSON.stringify(UPLOAD_SMALL).slice(1)}`
    expect(JSON.parse(padded)).toEqual(UPLOAD_SMALL)
    expect(padded.length).toBeLessThan(SOLAREDGE_RESPONSE_MAX_BYTES)
    const encoded = new TextEncoder().encode(padded)
    expect(encoded.byteLength).toBeGreaterThan(SOLAREDGE_RESPONSE_MAX_BYTES)
    const streamed = () => new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoded)
        controller.close()
      },
    }), { status: 200, headers: { 'content-type': 'application/json' } })
    const bodiless = () => ({
      status: 200, headers: new Headers({ 'content-type': 'application/json' }), text: async () => padded,
    })
    for (const makeResponse of [streamed, bodiless]) {
      const { client } = clientWith(answering(makeResponse))
      await expect(client.uploadPdf({ drawingId: 'solar', file: pdfBlob() }))
        .resolves.toEqual(refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))
    }
    const envelope = `{"cost":"${pad}",${JSON.stringify(REPORT_AMBIGUOUS).slice(1)}`
    expect(JSON.parse(envelope)).toEqual(REPORT_AMBIGUOUS)
    const { client } = clientWith(answering(() => new Response(new TextEncoder().encode(envelope), { status: 409 })))
    await expect(client.requestReport(reportArgs()))
      .resolves.toEqual(refused(409, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))
  })

  it('SI26 media_type must be a string', () => {
    expect(validateSolarArtifactRef(REPORT_REF, 'solar')).toBe(true)
    const ref = clone(REPORT_REF)
    ref.media_type = ['application/json']
    expect(validateSolarArtifactRef(ref, 'solar')).toBe(false)
    expect(validateSolarArtifactRef(ref, 'solar', { filename: 'solaredge-report.json' })).toBe(false)
  })

  it('SI27 the digest must be a string', async () => {
    const hex = REPORT_REF.content_sha256
    for (const digest of [42, Object(hex), { toString: () => hex }]) {
      const { client } = clientWith(
        answering(() => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS })),
        { sha256Hex: async () => digest },
      )
      await expect(client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF }))
        .resolves.toEqual(refused(200, 'SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE', false))
    }
  })

  it('SI28 the artifact bytes must be an ArrayBuffer', async () => {
    const bodiless = (value) => () => ({
      status: 200, headers: new Headers(DOWNLOAD_HEADERS), body: null, arrayBuffer: async () => value,
    })
    const exact = clientWith(answering(bodiless(reportBytes().slice().buffer))).client
    const kept = await exact.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF })
    expect(kept.ok).toBe(true)
    expect(kept.value.bytes.length).toBe(1803)
    for (const value of [reportBytes(), SMALL_REPORT_TEXT]) {
      expect(value.length).toBe(1803)
      const { client } = clientWith(answering(bodiless(value)))
      await expect(client.downloadArtifact({ drawingId: 'solar', ref: REPORT_REF }))
        .resolves.toEqual(refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false))
    }
  })

  it('SI29 every server route code has a sentence, and the corrected copy', () => {
    const union = routeMapUnion()
    expect(union.size).toBeGreaterThanOrEqual(ROUTE_CODES_TODAY.length)
    expect([...union].filter((code) => !Object.hasOwn(SOLAREDGE_IMPORT_REASONS, code))).toEqual([])
    expect(SOLAREDGE_IMPORT_REASONS.REPORT_AMBIGUOUS_MATCH)
      .toBe('A panel group fits more than one layout in the PDF, so try the recorded selection order')
    expect(SOLAREDGE_IMPORT_REASONS.REPORT_NO_MATCH)
      .toBe('A panel group in this drawing has no matching layout left in the PDF')
  })

  it('SI30 a body whose chunks are always ready settles at the deadline', async () => {
    for (const { run, bytes, json, spinMs } of floodRoutes) {
      let received = null
      const fetchImpl = vi.fn(async (url, init) => {
        received = init.signal
        return new Response(oneBytePerPull(bytes(), spinMs), { status: 200, headers: floodHeaders(json) })
      })
      const { client } = clientWith(fetchImpl, FAST_BUDGETS)
      const result = await settleWithin(run(client))
      expect(result).toMatchObject({ ok: false, code: 'SOLAREDGE_CLIENT_TIMEOUT', retryable: true })
      expect(received).toBeInstanceOf(AbortSignal)
      expect(received.aborted).toBe(true)
    }
  })

  it('SI31 a stream of empty ready chunks settles at the deadline', async () => {
    for (const { run, json } of floodRoutes) {
      const body = json === null ? reportBytes() : new TextEncoder().encode(JSON.stringify(json))
      const fetchImpl = vi.fn(async () => new Response(emptyChunksThen(body), {
        status: 200, headers: floodHeaders(json),
      }))
      const { client } = clientWith(fetchImpl, FAST_BUDGETS)
      const result = await settleWithin(run(client))
      expect(result).toMatchObject({ ok: false, code: 'SOLAREDGE_CLIENT_TIMEOUT', retryable: true })
    }
  })

  it('SI32 the caller abort listener is removed on every settle', async () => {
    const controller = new AbortController()
    const { signal } = controller
    const add = vi.spyOn(signal, 'addEventListener')
    const remove = vi.spyOn(signal, 'removeEventListener')
    const invalid = refused(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)
    const declaredOver = (length) => () => ({
      status: 200,
      headers: new Headers({ 'content-length': length, 'x-leaf-artifact-id': REPORT_REF.artifact_id }),
      text: async () => '{}',
      arrayBuffer: async () => new ArrayBuffer(0),
    })
    const cases = [
      [everyRoute[0], () => jsonResponse(UPLOAD_SMALL), declaredOver('70000'), invalid],
      [everyRoute[1], () => jsonResponse(REPORT_SMALL), declaredOver('70000'), invalid],
      [
        everyRoute[2], () => new Response(reportBytes(), { status: 200, headers: DOWNLOAD_HEADERS }),
        declaredOver('16777217'), refused(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false),
      ],
    ]
    let settles = 0
    const settled = async (client, run, expected) => {
      const result = await settleWithin(run(client, signal))
      expect(result).not.toBe(HUNG)
      expect(result).toMatchObject(expected)
      settles += 1
      expect(countAbort(add)).toBe(settles)
      expect(countAbort(remove)).toBe(countAbort(add))
    }
    for (const [run, success, oversize, oversizeResult] of cases) {
      await settled(clientWith(answering(success)).client, run, { ok: true, status: 200 })
      await settled(clientWith(answering(() => jsonResponse(REPORT_BUSY, 503))).client, run,
        refused(503, 'REPORT_BUSY', true))
      await settled(clientWith(answering(() => stalledResponse(200)), FAST_BUDGETS).client, run,
        { ok: false, code: 'SOLAREDGE_CLIENT_TIMEOUT', retryable: true })
      await settled(clientWith(answering(oversize)).client, run, oversizeResult)
    }
    expect(signal.aborted).toBe(false)
    expect(settles).toBe(12)
  })
})
