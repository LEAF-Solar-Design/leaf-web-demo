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

export default shouldStartTour
