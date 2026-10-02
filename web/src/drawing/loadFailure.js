function read(value, key) {
  try { return value?.[key] } catch { return undefined }
}

// Classify transport metadata before humanizeError discards it for display.
export function classifyDrawingLoadFailure(error) {
  const status = read(error, 'status')
  const body = read(error, 'body')
  const envelope = read(body, 'error')
  const codes = [
    read(envelope, 'error_code'), read(envelope, 'code'),
    read(body, 'error_code'), read(error, 'error_code'), read(error, 'code'),
  ]
  if ([400, 404, 410].includes(status) || codes.some((code) => code === 'BAD_PARAMS' || code === 'NOT_FOUND')) {
    return 'permanent'
  }
  if (status === 429 || (typeof status === 'number' && status >= 500 && status <= 599)) {
    return 'transient'
  }
  const name = read(error, 'name')
  const message = typeof error === 'string' ? error : read(error, 'message')
  if (['FetchTimeoutError', 'TimeoutError', 'AbortError', 'NetworkError'].includes(name) ||
      (typeof message === 'string' && /failed to fetch|network(?: request failed|error)|load failed|timed? ?out|timeout/i.test(message))) {
    return 'transient'
  }
  return 'unknown'
}
