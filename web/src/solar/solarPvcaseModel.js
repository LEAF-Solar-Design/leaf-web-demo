import { solarRunRefusal } from '../lib/ribbonClusters.js'
import { validateSolarArtifactRef } from './solarImportClient.js'

export const PVCASE_REASONS = Object.freeze({
  drawing: 'Open a valid drawing before importing a G33 file',
  project: 'Reopen the drawing in its project before importing the G33 file',
  media: 'Choose a G33 JSON file for this import',
  sourceAgain: 'Upload the G33 source again before continuing',
  sourceInvalid: 'The saved G33 source failed validation, so upload it again',
  paused: 'Drawing changes are paused, so try the upload again shortly',
  store: 'File storage is unavailable, so try again shortly',
  signIn: 'Sign in again before continuing this G33 step',
  forbidden: 'This session cannot use this G33 step',
  request: 'Check this G33 request and refresh the tools before trying again',
  refused: 'The G33 step was refused, so review the drawing and source before trying again',
  catalog: 'This G33 tool is not in the current catalog, so refresh the tools before running it',
  checkout: 'Take the drawing checkout before converting or solving the G33 design',
  stale: 'The Solar design changed, so refresh it before running this step again',
  timeout: 'The server did not answer in time, so check the result before trying again',
  network: 'The server could not be reached, so check the result before trying again',
  aborted: 'This request stopped here, so check the result before trying again',
  response: 'The server answer could not be verified, so check the result before trying again',
  artifactAgain: 'This file is unavailable for this result, so make the output again',
  artifactInvalid: 'The downloaded file failed its check, so it was not saved',
  digest: 'This browser cannot verify the downloaded file, so it was not saved',
  save: 'The file was verified but the browser could not save it',
  fileRequired: 'Choose a G33 JSON file before uploading',
  sourceRequired: 'Upload and validate a G33 source before converting it',
  conversionRequired: 'Convert the selected source before running the parity solve',
  solveRequired: 'Run the parity solve before making the assignment output',
  pending: 'A G33 request is in progress, so wait for it to finish',
  unknown: 'The job outcome is unknown, so check that job before submitting another change',
  retained: 'Your selected file is still available while you resolve this refusal',
  admitted: 'The source passed validation and is ready for an explicit conversion',
  converted: 'The conversion committed a new Solar design version',
  solved: 'The local parity solve committed strings without assigning inverters',
  exported: 'The assignment output is ready for a verified download',
  downloaded: 'The file was verified and saved',
  countsUnavailable: 'The graph committed, but its counts could not be read, so retry reading this version',
  electrical: 'The parity solve does not evaluate electrical sizing',
})

const HEX = /^[a-f0-9]{64}$/
const ID = /^leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const sources = new WeakSet()
const receipts = new WeakSet()
const graphs = new WeakSet()
export const pvcaseFailure = (code = 'response', status = null, retryable = false) => ({ ok: false, status, code, retryable })
export const pvcaseInteger = (n, low = 0, high = 2147483647) => Number.isInteger(n) && n >= low && n <= high
export const pvcaseDrawingId = (s) => typeof s === 'string' && /^[a-z0-9][a-z0-9_-]{0,62}$/.test(s)
export function pvcaseProjectId(s) {
  try { return s === null || (typeof s === 'string' && s.length > 0 && [...s].length <= 100 && !!encodeURIComponent(s)) } catch { return false }
}
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v) && [Object.prototype, null].includes(Object.getPrototypeOf(v))
const exact = (v, keys, optional = []) => object(v) && Reflect.ownKeys(v).every((k) => keys.includes(k) || optional.includes(k)) && keys.every((k) => Object.hasOwn(v, k))
const hash = (s) => typeof s === 'string' && HEX.test(s)
const version = (n) => Number.isSafeInteger(n) && n > 0
const copy = (v) => JSON.parse(JSON.stringify(v))
function frozenCopy(v) {
  const value = copy(v)
  const freeze = (entry) => {
    if (entry && typeof entry === 'object') {
      for (const child of Object.values(entry)) freeze(child)
      Object.freeze(entry)
    }
  }
  freeze(value)
  return value
}
const success = (value) => ({ ok: true, status: 200, value })
function guarded(work, code = 'response') { try { return work() } catch { return pvcaseFailure(code) } }

function serverReason(code) {
  try {
    if (typeof code !== 'string' || code.length > 64) return null
    if (!/^[A-Z][A-Z0-9_]*$/.test(code) && !/^[a-z][a-z0-9_]*$/.test(code)) return null
    const key = code.toUpperCase()
    const local = {
      PVS_DRAWING_ID_INVALID: 'drawing', PVS_DRAWING_NOT_FOUND: 'drawing',
      PVS_PROJECT_ID_INVALID: 'project', PVS_PROJECT_MISMATCH: 'project', PVS_MEDIA_TYPE_REFUSED: 'media',
      PVS_SOURCE_ID_INVALID: 'sourceAgain', PVS_SOURCE_NOT_FOUND: 'sourceAgain', PVS_SOURCE_KIND_MISMATCH: 'sourceAgain',
      PVS_WRITES_DRAINED: 'paused', PVS_STORE_UNAVAILABLE: 'store', PVS_SOURCE_CONFLICT: 'sourceInvalid',
      PVS_SOURCE_CORRUPT: 'sourceInvalid', PVS_SOURCE_INVALID: 'sourceInvalid',
      UNAUTHENTICATED: 'signIn', FORBIDDEN: 'forbidden', STALE_GRAPH_REVISION: 'stale', CHECKOUT_REQUIRED: 'checkout',
      BAD_PARAMS: 'request', UNKNOWN_TOOL: 'catalog', ARTIFACT_ID_INVALID: 'artifactAgain', ARTIFACT_NOT_FOUND: 'artifactAgain',
      ARTIFACT_STALE: 'artifactAgain', ARTIFACT_STORE_UNAVAILABLE: 'store', ARTIFACT_CORRUPT: 'artifactInvalid',
      SOLAREDGE_CLIENT_ARTIFACT_MISMATCH: 'artifactInvalid', SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE: 'digest',
      SOLAREDGE_CLIENT_TIMEOUT: 'timeout', SOLAREDGE_CLIENT_NETWORK: 'network', SOLAREDGE_CLIENT_ABORTED: 'aborted',
      SOLAREDGE_CLIENT_RESPONSE_INVALID: 'response', SOLAREDGE_CLIENT_REQUEST_INVALID: 'request',
    }[key]
    if (local) return PVCASE_REASONS[local]
    if (key === 'PVS_GRAPH_REQUIRED') return solarRunRefusal('graph_seed_required')
    const shared = solarRunRefusal(code)
    if (shared) return shared
  } catch { /* Only closed copy leaves this seam. */ }
  return null
}
export function pvcaseServerRefusalCode(code) {
  return serverReason(code) ? code : null
}
export function pvcaseReason(code, status = null) {
  if (typeof code !== 'string' || code.length > 64) return PVCASE_REASONS.refused
  if (Object.hasOwn(PVCASE_REASONS, code)) return PVCASE_REASONS[code]
  const shared = serverReason(code)
  if (shared) return shared
  if (code.toUpperCase() === 'HTTP') return status === 401 ? PVCASE_REASONS.signIn : status === 403 ? PVCASE_REASONS.forbidden : PVCASE_REASONS.refused
  return PVCASE_REASONS.refused
}

export function validatePvcaseSource(body, binding) {
  return guarded(() => {
    const { drawingId, projectId = null } = binding
    if (!pvcaseDrawingId(drawingId) || !pvcaseProjectId(projectId)
      || !exact(body, ['schema', 'kind', 'drawing_id', 'project_id', 'source_version', 'graph_rev', 'graph_sha256', 'source'], ['error', 'degraded_mode'])
      || body.schema !== 'leaf.pvcase-g33-source.v1' || body.kind !== 'pvcase-g33'
      || body.drawing_id !== drawingId || body.project_id !== projectId
      || !pvcaseInteger(body.source_version, 1) || !pvcaseInteger(body.graph_rev, 0, 1000000) || !hash(body.graph_sha256)
      || (Object.hasOwn(body, 'error') && body.error !== null)
      || (Object.hasOwn(body, 'degraded_mode') && typeof body.degraded_mode !== 'boolean')
      || !validateSolarArtifactRef(body.source, drawingId, { mediaType: 'application/json', filename: 'pvcase-g33-source.json', maxBytes: 16777216 })
      || body.source.source_version !== body.source_version) return pvcaseFailure()
    const value = frozenCopy(body)
    sources.add(value)
    return success(value)
  })
}

export function buildPvcaseParams(operation, context, source) {
  return guarded(() => {
    if (!object(context) || !pvcaseDrawingId(context.drawingId) || !pvcaseProjectId(context.projectId ?? null)
      || !version(context.drawingVersion) || typeof context.tenantId !== 'string' || !context.tenantId
      || !['convert', 'solve', 'export'].includes(operation)) return pvcaseFailure('request')
    if (!validatePvcaseSource(source, context).ok) return pvcaseFailure('sourceRequired')
    if (operation !== 'export' && !pvcaseInteger(context.graphRev)) return pvcaseFailure('request')
    const params = { drawing_id: context.drawingId }
    if (operation !== 'export') params.expected_rev = context.graphRev
    if (operation === 'convert') params.source_artifact_id = source.source.artifact_id
    return success(params)
  }, 'request')
}

function runResult(envelope, binding) {
  if (!exact(envelope, ['ok', 'tool', 'version', 'result', 'overlay', 'timing_ms', 'cost', 'error', 'degraded_mode'], ['execution_provenance'])
    || envelope.ok !== true || envelope.tool !== binding.tool || envelope.version !== '1.0.0'
    || (Object.hasOwn(envelope, 'execution_provenance') && !object(envelope.execution_provenance))
    || envelope.error !== null || typeof envelope.degraded_mode !== 'boolean'
    || typeof envelope.timing_ms !== 'number' || !Number.isFinite(envelope.timing_ms) || envelope.timing_ms < 0
    || !(envelope.cost === null || object(envelope.cost) || (typeof envelope.cost === 'number' && Number.isFinite(envelope.cost) && envelope.cost >= 0))) return null
  return envelope.result
}
function boundReceipt(r, b) {
  const c = b.context ?? b
  return r.tenant_id === c.tenantId && r.project_id === (c.projectId ?? null) && r.drawing_id === c.drawingId
    && r.job_id === b.jobId && r.tool === b.tool && hash(r.request_sha256) && hash(r.graph_sha256)
}
export function validatePvcaseCommit(envelope, binding) {
  return guarded(() => {
    const c = binding.context ?? binding
    const r = runResult(envelope, binding)
    if (!exact(r, ['schema_version', 'adapter', 'tenant_id', 'job_id', 'tool', 'project_id', 'drawing_id', 'request_sha256', 'new_version', 'before_graph_sha256', 'graph_sha256', 'intake_sha256', 'before_rev', 'after_rev', 'drawing_changed', 'replayed'])
      || !['solar-pvcase-convert', 'solar-pvcase-solve'].includes(binding.tool)
      || !boundReceipt(r, binding) || r.schema_version !== 'leaf.solar-graph-commit.v1' || r.adapter !== 'local-graph-commit'
      || !exact(r.new_version, ['drawing_id', 'version', 'parent']) || r.new_version.drawing_id !== c.drawingId
      || !version(c.drawingVersion) || r.new_version.parent !== c.drawingVersion || !version(r.new_version.version)
      || r.new_version.version <= r.new_version.parent || !pvcaseInteger(r.before_rev, 0, 1000000)
      || r.before_rev !== c.graphRev || !pvcaseInteger(r.after_rev, 0, 1000000) || r.after_rev !== r.before_rev + 1
      || !hash(r.before_graph_sha256) || !hash(r.intake_sha256) || r.drawing_changed !== true || typeof r.replayed !== 'boolean') return pvcaseFailure()
    const value = frozenCopy(r)
    receipts.add(value)
    return success(value)
  })
}
export function pvcaseAcceptedCommit(receipt, context) {
  try {
    return receipts.has(receipt) && receipt.drawing_id === context.drawingId && receipt.tenant_id === context.tenantId
      && receipt.project_id === (context.projectId ?? null) && receipt.new_version.parent === context.drawingVersion && receipt.before_rev === context.graphRev
  } catch { return false }
}

// Bound JSON before walking references. Entity extensions remain intact.
function boundedGraph(g) {
  const stack = [[g, 0]]
  let nodes = 0
  while (stack.length) {
    const [v, depth] = stack.pop()
    if (++nodes > 500000 || depth > 32) return false
    if (Array.isArray(v)) {
      if (v.length > 100000) return false
      for (const child of v) stack.push([child, depth + 1])
    } else if (object(v)) {
      for (const child of Object.values(v)) stack.push([child, depth + 1])
    } else if (!(v === null || typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v)))) return false
  }
  return new TextEncoder().encode(JSON.stringify(g)).byteLength <= 16777216
}
function collections(g) {
  const all = new Map()
  const maps = {}
  for (const [name, kind] of Object.entries({ frames: 'frame', panels: 'panel', strings: 'string', inverters: 'inverter', electrical_zones: 'zone-el', routes: 'route', schedules: 'schedule' })) {
    if (!Array.isArray(g[name])) return null
    const map = new Map()
    for (const e of g[name]) {
      if (!object(e) || typeof e.id !== 'string' || !ID.test(e.id) || e.kind !== kind || all.has(e.id)
        || !pvcaseInteger(e.rev, 0, 1000000)) return null
      map.set(e.id, e); all.set(e.id, e)
    }
    maps[name] = map
  }
  const member = new Map()
  const inputByString = new Map()
  for (const i of g.inverters) {
    if (!Array.isArray(i.input_assignments)) return null
    for (const a of i.input_assignments) {
      if (!object(a) || inputByString.has(a.string_ref) || !maps.strings.has(a.string_ref)) return null
      inputByString.set(a.string_ref, { inverterId: i.id, inputNumber: a.input_number })
    }
  }
  for (const f of g.frames) {
    if (!Array.isArray(f.panel_refs) || !Array.isArray(f.panel_assignments) || !Array.isArray(f.matrix) || !Array.isArray(f.sequences) || f.matrix.length > 10000) return null
    let slots = 0
    const seen = new Set()
    const frameMembers = new Set(f.panel_refs)
    for (const ref of f.panel_refs) {
      if (!maps.panels.has(ref) || member.has(ref) || maps.panels.get(ref).frame_ref !== f.id) return null
      member.set(ref, f.id)
    }
    for (let rowIndex = 0; rowIndex < f.matrix.length; rowIndex++) {
      const row = f.matrix[rowIndex]
      if (!Array.isArray(row) || row.length > 10000 || (slots += row.length) > 1000000) return null
      for (let col = 0; col < row.length; col++) {
        const cell = row[col]
        if (!object(cell) || !Object.hasOwn(cell, 'panel_ref')) return null
        if (cell.panel_ref !== null && (!frameMembers.has(cell.panel_ref) || seen.has(cell.panel_ref))) return null
        if (cell.panel_ref !== null) {
          const p = maps.panels.get(cell.panel_ref)
          if (!object(p.matrix_cell) || p.matrix_cell.row !== rowIndex || p.matrix_cell.col !== col
            || cell.seq !== p.assignment?.seq || !Object.hasOwn(cell, 'inverter_id')
            || cell.inverter_id !== (inputByString.get(p.assignment?.string_ref)?.inverterId ?? null)
            || cell.string_input_number !== (inputByString.get(p.assignment?.string_ref)?.inputNumber ?? null)) return null
          seen.add(cell.panel_ref)
        }
      }
    }
    if (seen.size !== f.panel_refs.length || f.panel_assignments.length !== f.panel_refs.length) return null
    const assigned = new Set()
    for (const a of f.panel_assignments) {
      const p = maps.panels.get(a?.panel_ref)
      if (!object(a) || !p || p.frame_ref !== f.id || assigned.has(p.id) || !object(p.assignment)
        || a.string_ref !== p.assignment.string_ref || a.seq !== p.assignment.seq
        || !Object.hasOwn(a, 'inverter_id') || a.inverter_id !== (inputByString.get(p.assignment.string_ref)?.inverterId ?? null)
        || a.string_input_number !== (inputByString.get(p.assignment.string_ref)?.inputNumber ?? null)) return null
      assigned.add(p.id)
    }
    const sequenceRefs = new Set()
    for (const sequence of f.sequences) {
      const s = maps.strings.get(sequence?.string_ref)
      if (!object(sequence) || !s || sequenceRefs.has(s.id) || !Array.isArray(sequence.ordered_panel_refs)) return null
      const expected = s.ordered_panel_refs.filter((ref) => member.get(ref) === f.id)
      if (expected.length !== sequence.ordered_panel_refs.length || expected.some((ref, i) => ref !== sequence.ordered_panel_refs[i])) return null
      sequenceRefs.add(s.id)
    }
    for (const ref of f.panel_refs) {
      const stringRef = maps.panels.get(ref).assignment?.string_ref
      if (stringRef != null && !sequenceRefs.has(stringRef)) return null
    }
    if (f.electrical_zone_ref != null && !maps.electrical_zones.has(f.electrical_zone_ref)) return null
  }
  if (member.size !== g.panels.length) return null
  const strings = new Map()
  for (const s of g.strings) {
    if (!Array.isArray(s.ordered_panel_refs) || !pvcaseInteger(s.module_count, 0, 1000000) || s.module_count !== s.ordered_panel_refs.length
      || !Object.hasOwn(s, 'inverter_ref') || (s.inverter_ref !== null && (!maps.inverters.has(s.inverter_ref) || inputByString.get(s.id)?.inverterId !== s.inverter_ref))) return null
    for (let seq = 0; seq < s.ordered_panel_refs.length; seq++) {
      const ref = s.ordered_panel_refs[seq]
      const p = maps.panels.get(ref)
      if (!p || strings.has(ref) || p.assignment?.string_ref !== s.id || p.assignment?.seq !== seq) return null
      strings.set(ref, s.id)
    }
  }
  for (const p of g.panels) {
    if (!object(p.assignment) || !Object.hasOwn(p.assignment, 'string_ref') || !Object.hasOwn(p.assignment, 'seq')) return null
    if (p.assignment.string_ref === null) { if (p.assignment.seq !== null || strings.has(p.id)) return null }
    else if (strings.get(p.id) !== p.assignment.string_ref || !pvcaseInteger(p.assignment.seq, 0, 1000000)) return null
  }
  for (const i of g.inverters) {
    if (!Array.isArray(i.input_assignments) || typeof i.is_l2 !== 'boolean' || (i.l1_assignments !== undefined && !Array.isArray(i.l1_assignments))) return null
    const assigned = new Set()
    for (const a of i.input_assignments) {
      if (!object(a) || !maps.strings.has(a.string_ref) || assigned.has(a.string_ref) || maps.strings.get(a.string_ref).inverter_ref !== i.id
        || typeof a.mppt_letter !== 'string' || a.mppt_letter.length < 1 || a.mppt_letter.length > 32 || !pvcaseInteger(a.input_number, 0, 1000000)) return null
      assigned.add(a.string_ref)
    }
    for (const a of i.l1_assignments ?? []) if (!object(a) || !maps.inverters.has(a.inverter_ref) || a.inverter_ref === i.id || !pvcaseInteger(a.mppt_index, 0, 1000000)) return null
    if (i.l2_ref != null && !maps.inverters.has(i.l2_ref)) return null
  }
  return {
    frames: g.frames.length, panels: g.panels.length, strings: g.strings.length, inverters: g.inverters.length,
    assignedPanels: strings.size,
    assignedInverters: g.inverters.filter((i) => i.input_assignments.length > 0 || (i.l1_assignments?.length ?? 0) > 0).length,
  }
}

export function validatePvcaseCommittedGraph(view, binding) {
  return guarded(() => {
    const { receipt, context } = binding
    if (!pvcaseAcceptedCommit(receipt, context) || !object(context) || receipt.drawing_id !== context.drawingId
      || receipt.project_id !== (context.projectId ?? null) || receipt.tenant_id !== context.tenantId
      || !object(view) || !version(view.version) || view.version !== receipt.new_version.version || !object(view.intake)
      || (Object.hasOwn(view, 'error') && view.error !== null)
      || (Object.hasOwn(view, 'degraded_mode') && typeof view.degraded_mode !== 'boolean')) return pvcaseFailure('countsUnavailable')
    for (const v of [view, view.intake]) {
      for (const key of ['tenant_id', 'tenantId']) if (Object.hasOwn(v, key) && v[key] !== context.tenantId) return pvcaseFailure('countsUnavailable')
      for (const key of ['org_id', 'organization_id', 'orgId']) if (Object.hasOwn(v, key) && v[key] !== (context.orgId ?? null)) return pvcaseFailure('countsUnavailable')
    }
    const g = view.intake.solar_design_graph
    if (!object(g) || g.graph_schema_version !== 1 || !pvcaseInteger(g.rev, 0, 1000000) || g.rev !== receipt.after_rev
      || !(g.parent_rev === null || pvcaseInteger(g.parent_rev, 0, 1000000))
      || !object(g.project) || (receipt.project_id !== null && g.project.id !== receipt.project_id) || !boundedGraph(g)) return pvcaseFailure('countsUnavailable')
    const counts = collections(g)
    if (!counts) return pvcaseFailure('countsUnavailable')
    const value = Object.freeze({ graph: copy(g), counts: Object.freeze(counts), version: view.version, revision: g.rev, receipt })
    graphs.add(value)
    return success(value)
  }, 'countsUnavailable')
}

export function validatePvcaseExport(envelope, binding) {
  return guarded(() => {
    const c = binding.context ?? binding
    const r = runResult(envelope, binding)
    const admitted = validatePvcaseSource(binding.source, c)
    if (!admitted.ok || !exact(r, ['schema_version', 'adapter', 'tenant_id', 'job_id', 'tool', 'project_id', 'drawing_id', 'request_sha256', 'source_version', 'representation', 'graph_sha256', 'output', 'output_sha256', 'output_bytes', 'drawing_changed'])
      || binding.tool !== 'solar-pvcase-export' || !boundReceipt(r, binding) || r.schema_version !== 'leaf.solar-graph-read.v1'
      || r.adapter !== 'local-graph-read' || r.drawing_changed !== false || r.source_version !== c.drawingVersion
      || !version(r.source_version) || r.representation !== 'intake' || !hash(r.output_sha256)
      || !pvcaseInteger(r.output_bytes, 1, 1048576) || !exact(r.output, ['summary', 'artifact'])) return pvcaseFailure()
    const { summary, artifact } = r.output
    if (!exact(summary, ['schema', 'panels', 'strings', 'electrical_sizing', 'source']) || summary.schema !== 'leaf.pvcase-g33-export.v1'
      || !pvcaseInteger(summary.panels, 0, 100000) || !pvcaseInteger(summary.strings, 0, 100000) || summary.electrical_sizing !== 'not-evaluated'
      || !exact(summary.source, ['artifact_id', 'content_sha256']) || summary.source.artifact_id !== admitted.value.source.artifact_id
      || summary.source.content_sha256 !== admitted.value.source.content_sha256
      || !validateSolarArtifactRef(artifact, c.drawingId, { mediaType: 'application/json', filename: 'PVcaseAssignments.json', maxBytes: 16777216 })
      || artifact.source_version !== r.source_version) return pvcaseFailure()
    return success(frozenCopy(r))
  })
}
export function sourceSummary(source) {
  return guarded(() => sources.has(source) ? success({ bytes: source.source.byte_length, schema: source.schema, artifactId: source.source.artifact_id, sha256: source.source.content_sha256, version: source.source_version, revision: source.graph_rev, graphSha256: source.graph_sha256 }) : pvcaseFailure())
}
export function commitSummary(receipt, validatedGraph) {
  return guarded(() => receipts.has(receipt) && graphs.has(validatedGraph) && validatedGraph.receipt === receipt
    ? success({ version: receipt.new_version.version, revision: receipt.after_rev, frames: validatedGraph.counts.frames, panels: validatedGraph.counts.panels, strings: validatedGraph.counts.strings, inverters: validatedGraph.counts.inverters }) : pvcaseFailure('countsUnavailable'), 'countsUnavailable')
}
export function solveSummary(receipt, validatedGraph) {
  return guarded(() => receipts.has(receipt) && graphs.has(validatedGraph) && validatedGraph.receipt === receipt
    ? success({ version: receipt.new_version.version, revision: receipt.after_rev, strings: validatedGraph.counts.strings, assignedPanels: validatedGraph.counts.assignedPanels, assignedInverters: validatedGraph.counts.assignedInverters }) : pvcaseFailure('countsUnavailable'), 'countsUnavailable')
}
