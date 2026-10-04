import { validateSolarArtifactRef } from './solarImportClient.js'

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
