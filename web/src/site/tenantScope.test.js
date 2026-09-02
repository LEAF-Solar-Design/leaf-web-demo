import { describe, expect, it } from 'vitest'
import { scopeFromEntitlements, scopedTools, scopeLockActive, LOCKED_SCOPE_ATTR } from './tenantScope.js'

const TOOLS = [
  { name: 'count-by-layer', description: 'counts' },
  { name: 'timber-cutlist-preflight', description: 'preview' },
  { name: 'timber-cutlist', description: 'cut list' },
]

describe('scopeFromEntitlements', () => {
  it('reads a well-formed scope, trimming and de-duplicating names', () => {
    const scope = scopeFromEntitlements({ scope: { label: ' Cut lists ', tools: ['timber-cutlist-preflight', ' timber-cutlist ', 'timber-cutlist'] } })
    expect(scope).toEqual({ label: 'Cut lists', tools: ['timber-cutlist-preflight', 'timber-cutlist'] })
  })
  it('is null for an unscoped tenant and for every malformed shape', () => {
    expect(scopeFromEntitlements(null)).toBeNull()
    expect(scopeFromEntitlements({})).toBeNull()
    expect(scopeFromEntitlements({ scope: null })).toBeNull()
    expect(scopeFromEntitlements({ scope: 'timber-cutlist' })).toBeNull()
    expect(scopeFromEntitlements({ scope: { tools: 'timber-cutlist' } })).toBeNull()
    expect(scopeFromEntitlements({ scope: { tools: [1] } })).toBeNull()
    expect(scopeFromEntitlements({ scope: { tools: new Array(65).fill('x') } })).toBeNull()
  })
  it('falls back to a neutral label and keeps an empty allowlist locked', () => {
    const scope = scopeFromEntitlements({ scope: { tools: [] } })
    expect(scope).toEqual({ label: 'Your tools', tools: [] })
  })
})

describe('scopedTools', () => {
  it('offers only scoped catalog rows, in scope order, skipping unknown names', () => {
    const scope = { label: 'x', tools: ['timber-cutlist', 'ghost', 'timber-cutlist-preflight'] }
    expect(scopedTools(scope, TOOLS).map((t) => t.name)).toEqual(['timber-cutlist', 'timber-cutlist-preflight'])
  })
  it('offers nothing without a scope or without a catalog', () => {
    expect(scopedTools(null, TOOLS)).toEqual([])
    expect(scopedTools({ label: 'x', tools: ['timber-cutlist'] }, null)).toEqual([])
  })
})

describe('scopeLockActive', () => {
  it('reads the DOM marker the locked shell renders', () => {
    const doc = { querySelector: (sel) => (sel === `[${LOCKED_SCOPE_ATTR}="1"]` ? {} : null) }
    expect(scopeLockActive(doc)).toBe(true)
    expect(scopeLockActive({ querySelector: () => null })).toBe(false)
    expect(scopeLockActive(null)).toBe(false)
  })
})
