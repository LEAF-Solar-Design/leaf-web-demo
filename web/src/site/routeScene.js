// ACCEPTANCE route matrix, binding: Esc may eject to the marketing scene
// ONLY from the operator stage. In console mode ('app') Esc must NEVER
// navigate('/') — on APP_ONLY_HOSTS that navigation redirects to the
// marketing origin and discards live console work. Named convergence bug
// (b), fixed ahead of the W3 one-shell mount so the mount cannot widen the
// eject by accident: the keyboard handler routes through this predicate
// instead of comparing scene literals inline.
export function sceneAllowsMarketingEject(scene) {
  return scene === 'tool'
}

// Named convergence bug (a): the inert sweep's active cast, per scene. Stage
// scenes name their cast; every other scene returns null, which means DO NOT
// SWEEP — a sweep that defaults to 'site' from a third mode would inert the
// entire W3 console (everything not cast 'site'/'both'), removing it from
// the tab order and the accessibility tree wholesale.
export function activeCastForScene(scene) {
  if (scene === 'tool') return 'tool'
  if (scene === 'site') return 'site'
  return null
}

export function sceneForPath(path) {
  if (
    path === '/ty'
    || path.startsWith('/ty/')
    || path === '/app'
    || path.startsWith('/app/')
  ) return 'app'
  if (path === '/sheets' || path.startsWith('/sheets/')) return 'sheets'
  if (path === '/try') return 'tool'
  return 'site'
}
