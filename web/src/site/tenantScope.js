// Tenant tool scope (server/tenant_scope.py): when /api/entitlements carries a
// `scope`, this tenant is locked to a single-purpose app. The catalog is already
// narrowed server-side; these helpers only decide what CHROME the shell drops
// and which tools the locked panel offers. Fail closed: anything that is not a
// well-formed scope reads as "not locked" for the chrome, but the catalog stays
// whatever the server returned, so a malformed scope can never widen access.

const MAX_TOOLS = 64
const MAX_LABEL = 120

// { label, tools: string[] } or null. Names only, de-duplicated, order kept.
export function scopeFromEntitlements(entitlements) {
  const raw = entitlements && entitlements.scope
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  if (!Array.isArray(raw.tools) || raw.tools.length > MAX_TOOLS) return null
  const seen = new Set()
  const tools = []
  for (const name of raw.tools) {
    if (typeof name !== 'string') return null
    const clean = name.trim()
    if (!clean || seen.has(clean)) continue
    seen.add(clean)
    tools.push(clean)
  }
  const label = typeof raw.label === 'string' && raw.label.trim()
    ? raw.label.trim().slice(0, MAX_LABEL)
    : 'Your tools'
  return { label, tools }
}

// The catalog rows the locked panel offers, in scope order. A scoped name with
// no catalog row is skipped (the server did not serve it, so it cannot run).
export function scopedTools(scope, tools) {
  if (!scope || !Array.isArray(tools)) return []
  const byName = new Map()
  for (const tool of tools) {
    if (tool && typeof tool.name === 'string') byName.set(tool.name, tool)
  }
  return scope.tools.map((name) => byName.get(name)).filter(Boolean)
}

// The locked shell's Esc key must not eject to the marketing cover. SiteRoot's
// handler reads this DOM marker (it already consults the DOM for owned
// surfaces), so the lock needs no new prop plumbing through the scene tree.
export const LOCKED_SCOPE_ATTR = 'data-scope-locked'

export function scopeLockActive(doc = typeof document === 'undefined' ? null : document) {
  return !!(doc && doc.querySelector(`[${LOCKED_SCOPE_ATTR}="1"]`))
}
