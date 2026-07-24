export class FetchTimeoutError extends Error {
  constructor(timeoutMs) {
    super(`The service did not respond within ${timeoutMs} ms.`)
    this.name = 'FetchTimeoutError'
    this.timeoutMs = timeoutMs
  }
}

export async function fetchWithBudget(fetchImpl, input, init = {}, timeoutMs = 5000) {
  const controller = new AbortController()
  const upstream = init.signal
  let timedOut = false
  const abortFromUpstream = () => controller.abort(upstream?.reason)
  if (upstream?.aborted) abortFromUpstream()
  else upstream?.addEventListener('abort', abortFromUpstream, { once: true })
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  try {
    return await fetchImpl(input, { ...init, signal: controller.signal })
  } catch (error) {
    if (timedOut) throw new FetchTimeoutError(timeoutMs)
    throw error
  } finally {
    clearTimeout(timer)
    upstream?.removeEventListener('abort', abortFromUpstream)
  }
}
