// Pure support diagnostics: select named fields only, then sanitize each value.
export const DIAGNOSTICS_TITLE = 'Leaf Automation diagnostics'

function clean(value) {
  const text = String(value ?? 'unknown').replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, '')
  if (/Bearer /i.test(text)) return '[redacted]'
  return text.replace(/[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}/g, '[redacted]').slice(0, 200)
}

function optional(value, prefix) {
  return value == null || value === '' ? '' : `${prefix}${clean(value)}`
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
    `build ${clean(buildHash)}`,
    `mode ${clean(mode)}`,
    `served ${servedSourceSha === null ? 'not available (sample data)' : clean(servedSourceSha)}`,
    `task ${taskRevision === null ? 'not available' : clean(taskRevision)}`,
    `page ${clean(pathname)}`,
    `time ${clean(at)}`,
    `session ${clean(sessionState)}`,
    editLock == null ? 'edit lock not applicable' : `edit lock ${clean(editLock.state)}${editLock.errorCode != null && editLock.errorCode !== '' ? optional(editLock.errorCode, ' code ') : optional(editLock.status, ' code HTTP ')}${optional(editLock.errorId, ' error_id ')}${optional(editLock.at, ' at ')}`,
    `request failures (${requests.length})`,
  ]
  for (const record of requests.slice(0, 8)) {
    const { at, method, endpointClass, status, errorCode, errorId } = record || {}
    lines.push(`  ${clean(at)} ${clean(method)} ${clean(endpointClass)} ${clean(status)}${optional(errorCode, ' ')}${optional(errorId, ' error_id=')}`)
  }
  if (!requests.length) lines.push('  none')
  if (requests.length > 8) lines.push(`  ... and ${requests.length - 8} more`)
  lines.push(`unavailable controls (${controls.length}) local`)
  for (const record of controls.slice(0, 40)) {
    lines.push(`  ${clean(record?.label)} = ${clean(record?.code)}`)
  }
  if (!controls.length) lines.push('  none')
  if (controls.length > 40) lines.push(`  ... and ${controls.length - 40} more`)
  return lines.join('\n').slice(0, 6000)
}
