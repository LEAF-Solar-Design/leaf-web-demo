// M5 — guided-tour entry predicate.
//
// PURE and dependency-free on purpose: no `import.meta`, no React, no JSON
// import, so `node scripts/check_tourscript.mjs` can import this headless.
//
// The tour is a deep-link, never a default: only an explicit `?demo=tour`
// (or the shorthand `?demo=1`) opens the guided walkthrough. Everything else —
// including a bare `?demo=` or `?demo=off` — lands in the normal mock demo.

export function shouldStartTour(search) {
  if (typeof search !== 'string' || search === '') return false
  let params
  try {
    params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  } catch {
    return false
  }
  const demo = params.get('demo')
  return demo === 'tour' || demo === '1'
}

// HP-01 — additive, pure predicate for the first-run coach mark. Same rules
// as shouldStartTour: no `import.meta`, no React, so this stays importable
// headless. This does NOT change shouldStartTour or its semantics.
//
// The coach is for a fresh, signed-out visitor with no dismissal recorded.
// ANY `?demo=` param (tour, 1, off, or even a bare `?demo=`) keeps absolute
// priority over the coach and suppresses it, matching the tour's own
// deep-link-first rule above.
export function shouldOfferCoach({ search, dismissed, signedIn } = {}) {
  if (dismissed) return false
  if (signedIn) return false
  if (typeof search === 'string' && search !== '') {
    let params
    try {
      params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
    } catch {
      params = null
    }
    if (params && params.has('demo')) return false
  }
  return true
}

export default shouldStartTour
