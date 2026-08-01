// The live workbench drawing id is supplied by a completed upload or seeded by
// a protected operator before the page loads. An empty browser session has no
// drawing, so this module must never invent one.
//
// A seeded id is honoured when it is already canonical under the ONE shared id
// rule the server enforces (server/tenant_id_validator.py, mirrored by
// da/store.sanitize_id): lowercase alphanumeric start, then alphanumeric,
// hyphen or underscore, 1..63 characters. That rejects uppercase, spaces, dots
// and every `/` or `\` traversal, so the sanitizing purpose holds -- but it no
// longer demands our own `cat-workbench-` prefix. The deployed authored-CAD
// acceptance driver (web/scripts/deployed_authored_cad_acceptance.mjs) seeds
// `acceptance-<run-id>-<a|b>`, a server-valid id, and requires the surface to
// open exactly that drawing; the prefix-only rule silently replaced it with a
// fresh id, which made the protected staging acceptance preflight unpassable.
export const WORKBENCH_ID_KEY = 'leaf.cat.workbench.id.v1'
export const CANONICAL_DRAWING_ID = /^[a-z0-9][a-z0-9_-]{0,62}$/

export function liveDrawingId(scope = globalThis) {
  try {
    const existing = scope.sessionStorage?.getItem(WORKBENCH_ID_KEY)
    if (CANONICAL_DRAWING_ID.test(existing || '')) return existing
  } catch {
    // Storage can be unavailable in private browsing. That is still an empty
    // state, not permission to fabricate a drawing identity.
  }
  return null
}

export function rememberLiveDrawingId(drawingId, scope = globalThis) {
  if (!CANONICAL_DRAWING_ID.test(drawingId || '')) return false
  try {
    scope.sessionStorage?.setItem(WORKBENCH_ID_KEY, drawingId)
    return true
  } catch {
    return false
  }
}
