// Memoized data loads for the landing stage. Both loads resolve once and are
// shared by every caller (StageLayer, sheets, future casts) — the drawing is
// fetched exactly once per session.

// Same defaults as src/api.js:21-23 (the single seam pattern). In MOCK mode
// (the default) the app makes ZERO /api calls — the bundled capture IS the
// canonical source, so we serve it directly instead of probing a backend
// that doesn't exist.
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8130'
const MOCK_DEFAULT = import.meta.env.VITE_MOCK !== '0'

const DEMO_SOLVE_TIMEOUT_MS = 3500

let intakePromise = null

// GET /sample.intake.json (the bundled 3.2 MW rooftop). Memoized; a failed
// load clears the memo so a retry is possible.
export function loadIntake() {
  if (!intakePromise) {
    intakePromise = fetch('/sample.intake.json')
      .then((res) => {
        if (!res.ok) throw new Error('failed to load sample.intake.json')
        return res.json()
      })
      .catch((e) => { intakePromise = null; throw e })
  }
  return intakePromise
}

// The API (and the bundled capture, which is a verbatim response) wrap the
// payload as {ok, solve:{strings, stats, module, electrical, receipt, ...}}.
// Consumers read the flat solve fields, so unwrap here — the single seam.
function normalizeDemoSolve(data, degraded) {
  const solve = data && typeof data.solve === 'object' && data.solve !== null ? data.solve : data
  return { ...solve, ok: data?.ok !== false, degraded }
}

async function fetchBundledDemoSolve(degraded) {
  const res = await fetch('/demo-solve.json')
  if (!res.ok) throw new Error('failed to load bundled demo-solve.json')
  const data = await res.json()
  return normalizeDemoSolve(data, degraded)
}

let demoSolvePromise = null

// GET {API_BASE}/api/site/demo-solve with a ~3.5s timeout. On ANY failure
// (timeout, network, non-2xx, bad JSON) fall back to the bundled
// /demo-solve.json and mark the result {degraded:true}. Memoized.
export function loadDemoSolve() {
  if (!demoSolvePromise) {
    demoSolvePromise = (async () => {
      // Mock mode: the bundled capture is canonical — zero /api calls.
      if (MOCK_DEFAULT) return fetchBundledDemoSolve(false)
      const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
      const timer = ctrl ? setTimeout(() => ctrl.abort(), DEMO_SOLVE_TIMEOUT_MS) : null
      try {
        const res = await fetch(`${API_BASE}/api/site/demo-solve`, ctrl ? { signal: ctrl.signal } : undefined)
        if (!res.ok) throw new Error(`GET /api/site/demo-solve -> ${res.status}`)
        const data = await res.json()
        return normalizeDemoSolve(data, false)
      } catch {
        return fetchBundledDemoSolve(true)
      } finally {
        if (timer) clearTimeout(timer)
      }
    })().catch((e) => { demoSolvePromise = null; throw e })
  }
  return demoSolvePromise
}
