// Pure support diagnostics: allowlist shapes, not names. A diagnostics block
// never carries a value it did not expect.
export const DIAGNOSTICS_TITLE = 'Leaf Automation diagnostics'

function clean(value) {
  const text = String(value ?? 'unknown').replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, '')
  if (/Bearer /i.test(text)) return '[redacted]'
  return text.replace(/[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}|eyJ[A-Za-z0-9_-]{20,}/g, '[redacted]').slice(0, 200)
}

const shapes = {
  buildHash: /^(?:[0-9a-f]{7,40}|\d{4}-\d{2}-\d{2} \d{2}:\d{2})$/,
  mode: /^(?:sample data|live)$/,
  servedSourceSha: /^[0-9a-f]{40}$/,
  taskRevision: /^[A-Za-z0-9._-]{1,80}:\d{1,6}$/,
  pathname: /^\/[A-Za-z0-9._~\/-]{0,120}$/,
  at: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/,
  sessionState: /^(?:demo|signed in|signed out|checking|refused)$/,
  state: /^(?:read failed|checking|held by you|held by another editor|free)$/,
  errorCode: /^[A-Z][A-Z0-9_]{0,63}$/,
  errorId: /^[0-9a-f]{16}$/,
  method: /^[A-Z]{3,7}$/,
  endpointClass: /^\/api\/[A-Za-z0-9._-]{1,40}(\/[A-Za-z0-9._-]{1,40})?$/,
  code: /^([A-Z]+_)?REASONS\.[A-Za-z0-9_]{1,40}$/,
  label: /^[A-Za-z0-9 ._:/()+-]{1,60}$/,
}

function field(value, name) {
  const text = clean(value)
  if (name === 'status') return Number.isInteger(value) && value >= 100 && value <= 599 ? text : '[unexpected]'
  return typeof value === 'string' && shapes[name].test(text) ? text : '[unexpected]'
}

function identity(value, name) {
  return value == null ? 'unknown' : field(value, name)
}

function optional(value, prefix, name) {
  return value == null ? '' : `${prefix}${field(value, name)}`
}

export function taskRevisionOf(arn) {
  return typeof arn === 'string' && arn.includes('/') ? arn.slice(arn.lastIndexOf('/') + 1) : null
}

export function collectRefusals(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return []
  const seen = new Set()
  const records = []
  const elements = root.querySelectorAll('[data-reason-code]')
  for (let i = 0; i < Math.min(elements.length, 40); i += 1) {
    const element = elements[i]
    const label = clean((element.getAttribute('aria-label') || element.textContent || '').split(' (unavailable')[0])
    const code = clean(element.getAttribute('data-reason-code'))
    const key = JSON.stringify([label, code])
    if (seen.has(key)) continue
    seen.add(key)
    records.push({ label, code })
  }
  return records
}

export function composeDiagnostics(input = {}) {
  const { buildHash, mode, servedSourceSha, taskRevision, pathname, at, sessionState, editLock, failures, refusals } = input
  const requests = Array.isArray(failures) ? failures : []
  const controls = Array.isArray(refusals) ? refusals : []
  const lines = [
    DIAGNOSTICS_TITLE,
    `build ${identity(buildHash, 'buildHash')}`,
    `mode ${field(mode, 'mode')}`,
    `served ${servedSourceSha === null ? `not available${mode === 'sample data' ? ' (sample data)' : ''}` : identity(servedSourceSha, 'servedSourceSha')}`,
    `task ${taskRevision === null ? 'not available' : identity(taskRevision, 'taskRevision')}`,
    `page ${field(pathname, 'pathname')}`,
    `time ${field(at, 'at')}`,
    `session ${field(sessionState, 'sessionState')}`,
    editLock == null ? 'edit lock not applicable' : `edit lock ${field(editLock.state, 'state')}${editLock.errorCode != null ? optional(editLock.errorCode, ' code ', 'errorCode') : optional(editLock.status, ' code HTTP ', 'status')}${optional(editLock.errorId, ' error_id ', 'errorId')}${optional(editLock.at, ' at ', 'at')}`,
    `request failures (${requests.length})`,
  ]
  for (const record of requests.slice(0, 8)) {
    const { at, method, endpointClass, status, errorCode, errorId } = record || {}
    lines.push(`  ${field(at, 'at')} ${field(method, 'method')} ${field(endpointClass, 'endpointClass')} ${field(status, 'status')}${optional(errorCode, ' ', 'errorCode')}${optional(errorId, ' error_id=', 'errorId')}`)
  }
  if (!requests.length) lines.push('  none')
  if (requests.length > 8) lines.push(`  ... and ${requests.length - 8} more`)
  lines.push(`unavailable controls (${controls.length}) local`)
  for (const record of controls.slice(0, 40)) {
    lines.push(`  ${field(record?.label, 'label')} = ${field(record?.code, 'code')}`)
  }
  if (!controls.length) lines.push('  none')
  if (controls.length > 40) lines.push(`  ... and ${controls.length - 40} more`)
  return lines.join('\n').slice(0, 6000)
}
