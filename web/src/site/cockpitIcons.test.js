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
    expect(manifest.platform).toBe('fluency-systems-regular')
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

  it('the sprite inventory lists only manifest keys, sorted, with a hash when non-empty', () => {
    const ids = Array.isArray(built.ids) ? built.ids : null
    expect(ids).not.toBeNull()
    expect(ids.filter((k) => !declared.has(k))).toEqual([])
    expect([...ids].sort()).toEqual(ids)
    if (ids.length) expect(built.hash).toMatch(/^[0-9a-f]{12}$/)
  })
})
