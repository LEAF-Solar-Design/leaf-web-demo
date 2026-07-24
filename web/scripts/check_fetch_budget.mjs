import { FetchTimeoutError, fetchWithBudget } from '../src/fetchBudget.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

const neverFetch = (_url, { signal }) => new Promise((_resolve, reject) => {
  signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
})

const started = Date.now()
let timeout = null
try {
  await fetchWithBudget(neverFetch, '/never', {}, 25)
} catch (error) {
  timeout = error
}

assert(timeout instanceof FetchTimeoutError, `expected FetchTimeoutError, got ${timeout?.constructor?.name}`)
assert(Date.now() - started < 250, 'fetch timeout must be bounded')

const response = await fetchWithBudget(async () => ({ ok: true }), '/fast', {}, 100)
assert(response.ok === true, 'fast fetch result must pass through')

console.log('FETCH_BUDGET_OK')
