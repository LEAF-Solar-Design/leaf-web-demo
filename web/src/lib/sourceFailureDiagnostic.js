// Source diagnostics explain a failed request. They never authorize another POST.
const MESSAGE = 'Project source initialization failed. Review the original request before retrying.'
const UUID = /^(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const CORRELATION = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const PHASES = new Set(['initialize_binding', 'initialize_authority', 'initialize_http',
  'initialize_transport', 'initialize_response', 'initialization'])
const diagnostics = new WeakMap()
const object = (value) => value && typeof value === 'object' && !Array.isArray(value)

export async function readSourceFailureBody(response) {
  if (response.status !== 503 || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') return null
  const reader = response.body?.getReader()
  if (!reader) return null
  try {
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let raw = '', bytes = 0
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      bytes += value.byteLength
      if (bytes > 4096) return null
      raw += decoder.decode(value, { stream: true })
    }
    return JSON.parse(raw + decoder.decode())
  } catch { return null } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export function sourceInitializationError(value, requestId) {
  if (!object(value) || Object.keys(value).length !== 1 || !object(value.error)) return null
  const detail = value.error
  const keys = ['code', 'message', 'source_request_id', 'correlation_id', 'phase', 'author_subphase']
  if (Object.hasOwn(detail, 'author_http_status')) keys.push('author_http_status')
  if (Object.keys(detail).length !== keys.length || !keys.every((key) => Object.hasOwn(detail, key))
    || detail.code !== 'author_unavailable' || detail.message !== MESSAGE || detail.phase !== 'stage'
    || typeof requestId !== 'string' || !UUID.test(requestId) || detail.source_request_id !== requestId
    || typeof detail.correlation_id !== 'string' || !CORRELATION.test(detail.correlation_id)
    || !PHASES.has(detail.author_subphase)
    || (Object.hasOwn(detail, 'author_http_status') && (!Number.isInteger(detail.author_http_status)
      || detail.author_http_status < 100 || detail.author_http_status > 599))) return null
  const error = new Error(MESSAGE)
  error.status = 503
  diagnostics.set(error, Object.freeze({ requestId, correlation: detail.correlation_id }))
  return error
}

export function sourceInitializationMessage(error, requestId) {
  const diagnostic = diagnostics.get(error)
  return diagnostic?.requestId === requestId
    ? `Source initialization failed. Reference: ${diagnostic.correlation}. ` : ''
}
