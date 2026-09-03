// W4e slice G, the icon coverage gate: every icon key the cockpit names must
// be in the icons8 manifest (so the fetch resolves it), and the sprite
// inventory may only list manifest keys. Source-level, like the other
// wiring pins: the keys are read out of the modules that declare them, so a
// new tool with a typo'd icon fails here, not as a blank square on staging.
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import manifest from '../assets/icons8/manifest.json'
import built from '../assets/icons8/built.json'

const HERE = dirname(fileURLToPath(import.meta.url))
const SOURCES = [
  join(HERE, '..', 'lib', 'ribbonClusters.js'),
  join(HERE, '..', 'cadedit', 'EngineRibbonClusters.jsx'),
  join(HERE, 'DrawingCockpit.jsx'),
  join(HERE, '..', 'App.jsx'),
]

// Slice 3: the tool RECORD carries its own icon key, so the data files that
// declare tools are icon sources too, not just the modules that render them.
const DATA_SOURCES = [
  join(HERE, '..', 'mock', 'registry.json'),
  join(HERE, '..', '..', '..', 'server', 'capability_families.json'),
]

// A key that is deliberately NOT in the sprite. CockpitIcon degrades a miss to
// an honest two-letter monogram, which is fine when it is a decision; this list
// is what makes it a decision instead of a typo. Each entry carries its reason.
const ALLOWED_MONOGRAM = Object.freeze({
  // (empty) every key the cockpit and the seeded records name is in the sprite
  // today. Add a row here only with the reason the sprite cannot carry it.
})

function jsonIconKeys(source) {
  // Every `"icon": "x"` in a tool-shaped JSON file, at any nesting depth
  // (registry tools, capability_families seed_tools).
  const keys = new Set()
  for (const m of source.matchAll(/"icon"\s*:\s*"([^"]*)"/g)) keys.add(m[1])
  return keys
}

function declaredIconKeys() {
  const used = new Set()
  for (const file of SOURCES) for (const key of iconKeysIn(readFileSync(file, 'utf8'))) used.add(key)
  for (const file of DATA_SOURCES) for (const key of jsonIconKeys(readFileSync(file, 'utf8'))) used.add(key)
  return used
}

/** Keys that neither the sprite nor the allow-list accounts for. */
function unresolvedIconKeys(keys, spriteIds) {
  const have = new Set(spriteIds)
  return [...keys].filter((k) => !have.has(k) && !(k in ALLOWED_MONOGRAM)).sort()
}

function iconKeysIn(source) {
  const keys = new Set()
  // icon: 'x'   |   icon="x"   |   <CockpitIcon id="x"
  for (const m of source.matchAll(/\bicon:\s*'([a-z0-9-]+)'/g)) keys.add(m[1])
  for (const m of source.matchAll(/\bicon="([a-z0-9-]+)"/g)) keys.add(m[1])
  for (const m of source.matchAll(/<CockpitIcon\s+id="([a-z0-9-]+)"/g)) keys.add(m[1])
  return keys
}

describe('cockpit icons (W4e)', () => {
  const declared = new Set(Object.keys(manifest.icons || {}))

  it('names one platform and at least one icon', () => {
    expect(manifest.platform).toBe('fluent-systems-regular')
    expect(declared.size).toBeGreaterThan(0)
    // The sprite is either the manifest's icons8 platform or the INTERIM
    // line set (scripts/build_interim_sprite.mjs) that holds the keys until
    // the icons8 fetch runs; never a third source.
    expect([manifest.platform, 'interim-line-set']).toContain(built.platform)
  })

  it('every icon key the cockpit uses is in the manifest', () => {
    const used = new Set()
    for (const file of SOURCES) for (const key of iconKeysIn(readFileSync(file, 'utf8'))) used.add(key)
    expect(used.size).toBeGreaterThan(20)
    const missing = [...used].filter((k) => !declared.has(k)).sort()
    expect(missing).toEqual([])
  })

  it('every icon key any tool source names is in the built sprite, or allow-listed', () => {
    // Stronger than the manifest check above: the manifest says what the fetch
    // WOULD resolve; built.json says what the shipped sprite actually carries.
    // A key that is in neither renders as a monogram on staging, silently.
    const unresolved = unresolvedIconKeys(declaredIconKeys(), built.ids || [])
    expect(unresolved).toEqual([])
  })

  it('fails on an unknown key rather than passing vacuously', () => {
    // The gate above is only worth having if it can go red. Falsify it.
    expect(unresolvedIconKeys(['definitely-not-an-icon'], built.ids || []))
      .toEqual(['definitely-not-an-icon'])
    expect(declaredIconKeys().size).toBeGreaterThan(20)
  })

  it('every allow-listed monogram key carries a reason and is really absent', () => {
    const have = new Set(built.ids || [])
    for (const [key, reason] of Object.entries(ALLOWED_MONOGRAM)) {
      expect(typeof reason === 'string' && reason.length > 0).toBe(true)
      expect(have.has(key)).toBe(false)
    }
  })

  it('the sprite inventory lists only manifest keys, sorted, with a hash when non-empty', () => {
    const ids = Array.isArray(built.ids) ? built.ids : null
    expect(ids).not.toBeNull()
    expect(ids.filter((k) => !declared.has(k))).toEqual([])
    expect([...ids].sort()).toEqual(ids)
    if (ids.length) expect(built.hash).toMatch(/^[0-9a-f]{12}$/)
  })
})
