import { validateSolarArtifactRef } from './solarImportClient.js'
import { createSolarDrawingBinding } from './solarDrawingBinding.js'
import { validateSolarProjectArtifactRef } from './solarProjectArtifactClient.js'

export const SOLAR_READ_RESULT_SCHEMA = 'leaf.solar-graph-read.v1'
export const SOLAR_READ_ADAPTER = 'local-graph-read'
export const MAX_FIELDS = 200
export const MAX_TABLE_ROWS = 200
export const MAX_COLUMNS = 12
export const MAX_TEXT = 500
export const MAX_CELL_TEXT = 200
export const MAX_LABEL = 80
// Identifiers a read output may carry (a physical head names its project) that the view never shows, at any depth.
export const PRIVATE_KEYS = Object.freeze(['tenant_id', 'project_id'])
export const READ_RESULT_UNREADABLE = 'This read finished, but its result could not be shown.'
export const ARTIFACT_UNVERIFIED = 'This result names a file that could not be verified, so no download is offered.'

export const DOWNLOAD_REASONS = Object.freeze({
  ARTIFACT_NOT_FOUND: 'This file is no longer stored, so run the tool again to make a new one.',
  ARTIFACT_STALE: 'The drawing changed after this file was made, so run the tool again.',
  ARTIFACT_STORE_UNAVAILABLE: 'File storage is unavailable right now, so try the download again shortly.',
  ARTIFACT_CORRUPT: 'The stored file failed its check, so run the tool again to make a new one.',
  ARTIFACT_ID_INVALID: 'This result names a file that could not be verified, so no download is offered.',
  SOLAREDGE_CLIENT_ARTIFACT_MISMATCH: 'The downloaded file did not match its record, so it was discarded.',
  SOLAREDGE_CLIENT_TIMEOUT: 'The download took too long, so try again.',
  SOLAREDGE_CLIENT_NETWORK: 'The download could not reach the server, so try again.',
  UNAUTHENTICATED: 'Sign in again to download this file.',
  FORBIDDEN: 'You do not have access to this file.',
  ENTITLEMENT_REQUIRED: 'Your plan does not include this download.',
  SAVE_FAILED: 'The file downloaded but the browser could not save it.',
  FALLBACK: 'The download failed, so try again.',
})

export function downloadReason(code, status) {
  if (Object.hasOwn(DOWNLOAD_REASONS, code) && code !== 'SAVE_FAILED' && code !== 'FALLBACK') return DOWNLOAD_REASONS[code]
  if (status === 403) return DOWNLOAD_REASONS.FORBIDDEN
  if (status === 404) return DOWNLOAD_REASONS.ARTIFACT_NOT_FOUND
  return DOWNLOAD_REASONS.FALLBACK
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

export function isSolarReadResult(data) {
  try {
    return isPlainObject(data) && data.schema_version === SOLAR_READ_RESULT_SCHEMA && data.adapter === SOLAR_READ_ADAPTER
  } catch { return false }
}

export function labelOf(key) {
  const text = key.replaceAll('_', ' ')
  const label = text.charAt(0).toUpperCase() + text.slice(1)
  return label.length > MAX_LABEL ? `${label.slice(0, MAX_LABEL)} [truncated]` : label
}

export function formatValue(value, limit = MAX_TEXT) {
  let text
  if (value == null) text = 'none'
  else if (typeof value === 'boolean') text = value ? 'yes' : 'no'
  else if (typeof value === 'number') {
    text = Number.isInteger(value) || !Number.isFinite(value) ? String(value) : String(Number(value.toPrecision(6)))
  } else if (typeof value === 'string') text = value
  else if (Array.isArray(value) && value.every((item) => item === null || typeof item !== 'object')) {
    text = value.length ? value.map((item) => formatValue(item, Infinity)).join(', ') : 'none'
  } else {
    try { text = JSON.stringify(value) } catch { text = undefined }
    if (text === undefined) text = 'unreadable value'
  }
  return text.length > limit ? `${text.slice(0, limit)} [truncated]` : text
}

export function formatBytes(n) {
  if (n < 1024) return `${n} bytes`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

const PRIVATE = new Set(PRIVATE_KEYS)
const MAX_DEPTH = 64

// A copy of a parsed JSON value without the private keys, at every depth. Own keys are defined, never assigned, so a
// parsed "__proto__" key stays data. Deeper than MAX_DEPTH reads as null. Non-plain objects are outside the parsed-JSON
// contract and pass unchanged.
function withoutPrivate(value, depth) {
  if (value === null || typeof value !== 'object') return value
  if (depth >= MAX_DEPTH) return null
  if (Array.isArray(value)) return value.map((item) => withoutPrivate(item, depth + 1))
  if (!isPlainObject(value)) return value
  const copy = {}
  for (const key of Object.keys(value)) {
    if (!PRIVATE.has(key)) Object.defineProperty(copy, key, { value: withoutPrivate(value[key], depth + 1), enumerable: true, writable: true, configurable: true })
  }
  return copy
}

function tableOf(key, values) {
  const keys = []
  const seen = new Set()
  for (const row of values) {
    for (const column of Object.keys(row)) {
      if (!seen.has(column)) { seen.add(column); keys.push(column) }
    }
  }
  const kept = keys.slice(0, MAX_COLUMNS)
  return {
    key, label: labelOf(key), columns: kept.map((column) => ({ key: column, label: labelOf(column) })),
    rows: values.slice(0, MAX_TABLE_ROWS).map((row) => kept.map((column) => Object.hasOwn(row, column) ? formatValue(row[column], MAX_CELL_TEXT) : '')),
    omittedRows: Math.max(0, values.length - MAX_TABLE_ROWS),
    omittedColumns: Math.max(0, keys.length - MAX_COLUMNS),
  }
}

const DETAIL_KEYS = [
  ['tool', 'Tool'], ['drawing_id', 'Drawing'], ['source_version', 'Version'], ['representation', 'Representation'],
  ['graph_sha256', 'Graph'], ['output_sha256', 'Output'], ['request_sha256', 'Request'], ['job_id', 'Job'],
]

export function readResultView(data) {
  const view = {
    tool: typeof data?.tool === 'string' ? data.tool : null,
    drawingId: typeof data?.drawing_id === 'string' ? data.drawing_id : null,
    headline: null, oneLiner: null, fields: [], omittedFields: 0, sections: [], tables: [], artifact: null,
    details: [], refusal: null,
  }
  for (const [key, label] of DETAIL_KEYS) {
    const value = data?.[key]
    if (typeof value === 'string' || typeof value === 'number') view.details.push({ label, text: formatValue(value) })
  }
  if (!isSolarReadResult(data) || !isPlainObject(data.output)) {
    view.refusal = 'unreadable'
    return view
  }
  const output = data.output
  if (typeof output.result === 'number' && typeof output.units === 'string') view.headline = `${formatValue(output.result)} ${formatValue(output.units)}`
  if (typeof output.one_liner === 'string') view.oneLiner = formatValue(output.one_liner)
  let fieldCount = 0
  const addField = (fields, key, value) => {
    if (fieldCount < MAX_FIELDS) fields.push({ key, label: labelOf(key), text: formatValue(value) })
    else view.omittedFields++
    fieldCount++
  }
  const addEntry = (target, key, value, allowSection) => {
    if (key === 'artifact') return
    if (Array.isArray(value) && value.length > 0 && value.every(isPlainObject)) target.tables.push(tableOf(key, value))
    else if (allowSection && isPlainObject(value)) {
      const section = { key, label: labelOf(key), fields: [], tables: [] }
      for (const child of Object.keys(value)) addEntry(section, child, value[child], false)
      view.sections.push(section)
    } else addField(target.fields, key, value)
  }
  for (const key of Object.keys(output)) {
    if (PRIVATE.has(key)) continue
    if (key === 'artifact') {
      const ref = output.artifact
      view.artifact = validateSolarArtifactRef(ref, data.drawing_id)
        ? { state: 'ready', ref, filename: ref.filename, sizeText: formatBytes(ref.byte_length) }
        : { state: 'unverified' }
    } else if (view.headline !== null && (key === 'result' || key === 'units')) continue
    else if (key === 'one_liner' && view.oneLiner !== null) continue
    else addEntry(view, key, withoutPrivate(output[key], 1), true)
  }
  return view
}

export const SOLAR_PROJECT_READ_RESULT_SCHEMA = 'leaf.solar-project-read.v1'
export const SOLAR_PROJECT_READ_ADAPTER = 'project-local-graph-read'

export const PROJECT_READ_REASONS = Object.freeze({
  binding_invalid: 'Open a project drawing version before viewing this result.',
  result_unreadable: 'This project result does not match the selected drawing version.',
  artifact_unverified: 'This result names a file that could not be verified, so no download is offered.',
  scope_stale: 'The drawing or session changed, so the download was discarded.',
  client_unavailable: 'Project downloads are not available in this view.',
  catalog_digest_required: 'Refresh the Solar tools before viewing this project result.',
  request_invalid: 'This project output request could not be prepared.',
  response_invalid: 'The server did not return a complete verified file.',
  artifact_mismatch: 'The downloaded file did not match its record, so it was discarded.',
  digest_unavailable: 'This browser could not check the downloaded file, so it was discarded.',
  timeout: 'The download took too long, so try again.',
  aborted: 'The download was cancelled.',
  unauthenticated: 'Sign in again to download this file.',
  forbidden: 'You do not have access to this file.',
  not_found: 'This file or its source version is no longer available.',
  stale: 'The drawing changed after this file was made, so run the tool again.',
  unavailable: 'Project output storage is unavailable right now, so try again shortly.',
  disabled: 'Project Solar outputs are not enabled on this server.',
  unsupported: 'This Solar tool has no connected project output adapter.',
  tool_changed: 'The Solar tool changed, so refresh the tools and run it again.',
  entitlement: 'Your plan does not currently permit this project output.',
  output_refused: 'This Solar output is unavailable for the selected drawing version.',
  save_failed: 'The file downloaded but the browser could not save it.',
  fallback: 'The download failed, so try again.',
})

const PROJECT_HEX = new RegExp('^[0-9a-f]{64}$')
const PROJECT_DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const PROJECT_UUID = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
const PROJECT_TOOL = new RegExp('^[a-z0-9][a-z0-9-]{0,127}$')
const PROJECT_CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const PROJECT_PRIVATE = new Set(['organization_id', 'project_id', 'tenant_id', 'graph_project_id'])
const projectHash = (value) => typeof value === 'string' && PROJECT_HEX.test(value)

function ownValue(value, key) {
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  if (!descriptor || !Object.hasOwn(descriptor, 'value')) throw new TypeError('Invalid project result')
  return descriptor.value
}

export function isSolarProjectReadResult(data) {
  try {
    return isPlainObject(data) && ownValue(data, 'schema') === SOLAR_PROJECT_READ_RESULT_SCHEMA &&
      ownValue(data, 'adapter') === SOLAR_PROJECT_READ_ADAPTER
  } catch { return false }
}

// Bound and copy JSON before presentation. Files are opaque references, never table data.
function projectOutput(value, budget, depth = 0) {
  if (++budget.nodes > 100000 || depth > MAX_DEPTH) throw new TypeError('Large project result')
  if (value === null || typeof value === 'boolean' || typeof value === 'string' ||
      (typeof value === 'number' && Number.isFinite(value))) {
    budget.bytes += new TextEncoder().encode(JSON.stringify(value)).byteLength
    if (budget.bytes > 1_048_576) throw new TypeError('Large project result')
    return value
  }
  if (!Array.isArray(value) && !isPlainObject(value)) throw new TypeError('Invalid project output')
  const keys = Reflect.ownKeys(value)
  budget.bytes += 2 + Math.max(0, keys.length - (Array.isArray(value) ? 2 : 1))
  if (budget.bytes > 1_048_576) throw new TypeError('Large project result')
  if (Array.isArray(value)) {
    if (Object.getPrototypeOf(value) !== Array.prototype || keys.length !== value.length + 1) throw new TypeError('Invalid project array')
    return Array.from({ length: value.length }, (_, index) => projectOutput(ownValue(value, String(index)), budget, depth + 1))
  }
  const result = {}
  for (const key of keys) {
    if (typeof key !== 'string') throw new TypeError('Invalid project key')
    budget.bytes += new TextEncoder().encode(JSON.stringify(key)).byteLength + 1
    if (budget.bytes > 1_048_576) throw new TypeError('Large project result')
    // Every subtree is validated and counted against the bounds; artifact references and private keys
    // are then left out of what is presented.
    const child = projectOutput(ownValue(value, key), budget, depth + 1)
    if (key !== 'artifact' && !PROJECT_PRIVATE.has(key)) Object.defineProperty(result, key, { value: child, enumerable: true })
  }
  return result
}

export function deriveProjectReadResultState(options) {
  const refused = (reason) => ({ mode: 'refused', reason })
  try {
    const { binding: suppliedBinding, data, catalogDigest, current = true } = options
    let binding
    try { binding = createSolarDrawingBinding(suppliedBinding) }
    catch { return refused('SIP_R12_BINDING_INVALID') }
    if (typeof catalogDigest !== 'string' || !PROJECT_DIGEST.test(catalogDigest)) return refused('SIP_R12_CATALOG_DIGEST_REQUIRED')
    if (!isSolarProjectReadResult(data)) return refused('SIP_R12_RESULT_INVALID')
    const meta = {}
    for (const key of ['organization_id', 'project_id', 'drawing_id', 'input_version_id', 'tool',
      'tool_manifest_sha256', 'input_intake_sha256', 'graph_sha256', 'request_sha256', 'output_sha256',
      'graph_project_id', 'head_version_id', 'is_head', 'current_required', 'drawing_changed', 'output_bytes', 'output']) {
      meta[key] = ownValue(data, key)
    }
    if (['organization_id', 'project_id', 'drawing_id', 'input_version_id'].some((key) => meta[key] !== binding[key]) ||
        typeof meta.tool !== 'string' || !PROJECT_TOOL.test(meta.tool) || meta.tool_manifest_sha256 !== catalogDigest ||
        !['input_intake_sha256', 'graph_sha256', 'request_sha256', 'output_sha256'].every((key) => projectHash(meta[key])) ||
        typeof meta.head_version_id !== 'string' || !PROJECT_UUID.test(meta.head_version_id) ||
        typeof meta.is_head !== 'boolean' || meta.is_head !== (meta.head_version_id === binding.input_version_id) ||
        typeof meta.current_required !== 'boolean' || typeof current !== 'boolean' || meta.drawing_changed !== false ||
        typeof meta.graph_project_id !== 'string' || !meta.graph_project_id.trim() ||
        !Number.isInteger(meta.output_bytes) || meta.output_bytes < 1 || meta.output_bytes > 1_048_576 || !isPlainObject(meta.output)) return refused('SIP_R12_RESULT_INVALID')
    if (current && !meta.is_head) return refused('SIP_R12_STALE_CURRENT')
    if (meta.current_required !== current) return refused('SIP_R12_RESULT_INVALID')
    const output = projectOutput(meta.output, { nodes: 0, bytes: 0 })
    if (new TextEncoder().encode(JSON.stringify(output)).byteLength > 1_048_576) return refused('SIP_R12_RESULT_INVALID')
    const view = readResultView({ schema_version: SOLAR_READ_RESULT_SCHEMA, adapter: SOLAR_READ_ADAPTER,
      drawing_id: binding.drawing_id, tool: meta.tool, output })
    view.details = [['tool', 'Tool'], ['drawing_id', 'Drawing'], ['input_version_id', 'Version'],
      ['graph_sha256', 'Graph'], ['output_sha256', 'Output'], ['request_sha256', 'Request']]
      .map(([key, label]) => ({ label, text: formatValue(meta[key]) }))
    if (Object.hasOwn(meta.output, 'artifact')) {
      let ref
      try { ref = ownValue(meta.output, 'artifact') } catch { ref = null }
      const hashes = { inputIntakeSha256: meta.input_intake_sha256, graphSha256: meta.graph_sha256 }
      if (validateSolarProjectArtifactRef(ref, binding, hashes)) {
        const copiedRef = {}
        for (const key of Reflect.ownKeys(ref)) Object.defineProperty(copiedRef, key, { value: ownValue(ref, key), enumerable: true })
        Object.freeze(copiedRef)
        view.artifact = { state: 'ready', ref: copiedRef, filename: copiedRef.filename, sizeText: formatBytes(copiedRef.byte_length) }
      } else view.artifact = { state: 'unverified' }
    }
    return { mode: 'ready', binding, current, inputIntakeSha256: meta.input_intake_sha256, graphSha256: meta.graph_sha256, view }
  } catch { return refused('SIP_R12_RESULT_INVALID') }
}

export function projectReadReason(reason, status) {
  const code = typeof reason === 'string' && PROJECT_CODE.test(reason) ? reason : 'HTTP_ERROR'
  switch (code) {
    case 'SIP_R12_BINDING_INVALID': case 'SIP_R7_BINDING_INVALID': return PROJECT_READ_REASONS.binding_invalid
    case 'SIP_R12_RESULT_INVALID': return PROJECT_READ_REASONS.result_unreadable
    case 'SIP_R12_ARTIFACT_REF_INVALID': case 'ARTIFACT_ID_INVALID': return PROJECT_READ_REASONS.artifact_unverified
    case 'SIP_R12_SCOPE_STALE': case 'SIP_R7_SCOPE_STALE': return PROJECT_READ_REASONS.scope_stale
    case 'SIP_R12_CLIENT_UNAVAILABLE': return PROJECT_READ_REASONS.client_unavailable
    case 'SIP_R12_CATALOG_DIGEST_REQUIRED': return PROJECT_READ_REASONS.catalog_digest_required
    case 'SIP_R12_REQUEST_INVALID': case 'SIP_R7_REQUEST_INVALID': case 'SIP_R6_INVALID_REQUEST':
    case 'SIP_R6_REQUEST_TOO_LARGE': case 'INVALID_SCHEDULES_REQUEST': case 'INVALID_CABLE_EXPORT_REQUEST': return PROJECT_READ_REASONS.request_invalid
    case 'SIP_R12_RESPONSE_INVALID': case 'SIP_R7_RESPONSE_INVALID': return PROJECT_READ_REASONS.response_invalid
    case 'SIP_R12_ARTIFACT_MISMATCH': case 'ARTIFACT_CORRUPT': case 'SIP_R6_ARTIFACT_SOURCE_MISMATCH': return PROJECT_READ_REASONS.artifact_mismatch
    case 'SIP_R12_DIGEST_UNAVAILABLE': return PROJECT_READ_REASONS.digest_unavailable
    case 'SIP_R12_TIMEOUT': case 'SIP_R7_TIMEOUT': return PROJECT_READ_REASONS.timeout
    case 'SIP_R12_ABORTED': case 'SIP_R7_ABORTED': return PROJECT_READ_REASONS.aborted
    case 'UNAUTHENTICATED': return PROJECT_READ_REASONS.unauthenticated
    case 'FORBIDDEN': return PROJECT_READ_REASONS.forbidden
    case 'ARTIFACT_NOT_FOUND': return PROJECT_READ_REASONS.not_found
    case 'SIP_R12_STALE_CURRENT': case 'SIP_R6_STALE_CURRENT': case 'ARTIFACT_STALE':
    case 'SOLAR_OUTPUT_NOT_CURRENT': case 'ARTIFACT_CONFLICT': return PROJECT_READ_REASONS.stale
    case 'ARTIFACT_STORE_UNAVAILABLE': case 'ARTIFACT_WRITES_DRAINED': return PROJECT_READ_REASONS.unavailable
    case 'project_execution_disabled': return PROJECT_READ_REASONS.disabled
    case 'SIP_R6_TOOL_UNSUPPORTED': case 'project_adapter_unavailable': return PROJECT_READ_REASONS.unsupported
    case 'SIP_R6_TOOL_MANIFEST_MISMATCH': return PROJECT_READ_REASONS.tool_changed
    case 'ENTITLEMENT_REQUIRED': case 'ENTITLEMENT_DENIED': case 'ENTITLEMENT_UNAVAILABLE':
    case 'ENTITLEMENT_POLICY_UNAVAILABLE': case 'entitlement_required': case 'entitlement_policy_unavailable': return PROJECT_READ_REASONS.entitlement
    case 'UNRESOLVED_UNITS': case 'SIZING_CONFIRMATION_REQUIRED': case 'SCHEDULES_OPTIMIZERS_UNSUPPORTED':
    case 'SCHEDULES_ZONE_SIZING_UNSUPPORTED': case 'SCHEDULES_MAPPING_FAILED': case 'SCHEDULES_KERNEL_REFUSED':
    case 'READ_OUTPUT_LIMIT_EXCEEDED': case 'READ_OUTPUT_INVALID': case 'ARTIFACT_REFERENCE_RESERVED':
    case 'SIP_R6_INTERNAL': return PROJECT_READ_REASONS.output_refused
    case 'SIP_R12_SAVE_FAILED': return PROJECT_READ_REASONS.save_failed
    case 'SIP_R7_TRANSPORT_FAILED': return PROJECT_READ_REASONS.fallback
    default: break
  }
  if (status === 401) return PROJECT_READ_REASONS.unauthenticated
  if (status === 403) return PROJECT_READ_REASONS.forbidden
  if (status === 404) return PROJECT_READ_REASONS.not_found
  return PROJECT_READ_REASONS.fallback
}
