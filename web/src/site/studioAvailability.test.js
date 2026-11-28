// The availability manifest's own gate. A row is a promise to a visitor: an
// enabled row says "this works" and names the walk that proved it, an
// unavailable row says "not yet" in words short and honest enough to show on
// the control. These rows hold every entry to that, and hold every reader in
// web/src to an id that exists.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it, vi } from 'vitest'

import { APP_ONLY_HOSTS } from './appHosts.js'
import {
  AVAILABILITY_STATUSES,
  NOT_YET_AVAILABLE,
  STAGING_HOST,
  STUDIO_AVAILABILITY,
  STUDIO_AVAILABILITY_SCHEMA,
  availabilityEnv,
  availabilityProps,
  catalogToolDisclaimer,
  guardUnavailable,
  listStudioAvailability,
  notYetAvailable,
  studioFeature,
  studioFeatureAvailable,
  studioFeatureNote,
} from './studioAvailability.js'

const WEB_ROOT = process.cwd()
const SRC_ROOT = join(WEB_ROOT, 'src')
const WORKSPACES = ['site', 'sign-in', 'browser', 'cad', 'solar', 'ios', 'ops', 'lab', 'project', 'ask', 'shared']
const rows = Object.entries(STUDIO_AVAILABILITY)
// Where each adopted row's sentence lives. Kept HERE, never in the manifest:
// a source path in a shipped module trips the production bundle's source fence.
const ADOPTED_SOURCES = {
  'cad.print': 'src/App.jsx',
  'cad.model-tab': 'src/lib/ribbonTabs.data.js',
  'cad.snap-and-polar': 'src/site/DrawingCockpit.jsx',
}
const unavailable = rows.filter(([, row]) => row.status === 'unavailable')

// Copy law for anything a visitor reads: no em or en dash, the company name in
// its one casing, and never a promised date.
const DASH = /[–—]/
const DATE_PROMISE = /\b(20\d\d|Q[1-4]|january|february|march|april|june|july|august|september|october|november|december|next (week|month|quarter|year)|this (week|month|quarter|year)|soon|shortly|tomorrow|days?|weeks?|months?)\b/i

function sourceFiles(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name === '__fixtures__' || name === 'node_modules') continue
      sourceFiles(path, out)
    } else if (/\.(js|jsx|mjs)$/.test(name) && !/\.test\.(js|jsx|mjs)$/.test(name)) out.push(path)
  }
  return out
}

describe('STUDIO_AVAILABILITY', () => {
  it('names its schema and is frozen all the way down', () => {
    expect(STUDIO_AVAILABILITY_SCHEMA).toBe('leaf.studio-availability.v1')
    expect(Object.isFrozen(STUDIO_AVAILABILITY)).toBe(true)
    for (const [, row] of rows) expect(Object.isFrozen(row)).toBe(true)
  })

  it.each(rows)('%s is a complete row', (id, row) => {
    expect(id).toMatch(/^[a-z][a-z-]*\.[a-z0-9][a-z0-9-]*$/)
    expect(WORKSPACES).toContain(row.workspace)
    expect(id.startsWith(`${row.workspace}.`)).toBe(true)
    expect(typeof row.label).toBe('string')
    expect(row.label.trim().length).toBeGreaterThan(0)
    expect(AVAILABILITY_STATUSES).toContain(row.status)
    // Evidence is a path under a named root, never a bare claim.
    expect(row.evidence).toMatch(/^(audit|capability|source):\S.+$/)
  })

  it.each(rows.filter(([, row]) => row.status === 'enabled'))('%s is enabled and carries no disclaimer', (id, row) => {
    expect(row.disclaimer).toBeNull()
    expect(row.adopted).toBeUndefined()
    expect(row.hosts).toBeUndefined()
    expect(row.tool).toBeUndefined()
    // An enabled row is proven by a staging walk, never by the source alone.
    expect(row.evidence).toMatch(/^(audit|capability):/)
  })

  it.each(unavailable)('%s is unavailable and says so in a sentence a visitor can read', (id, row) => {
    expect(typeof row.disclaimer).toBe('string')
    expect(row.disclaimer.length).toBeGreaterThanOrEqual(12)
    expect(row.disclaimer.length).toBeLessThanOrEqual(140)
    expect(row.disclaimer).not.toMatch(/\b(TODO|TBD|FIXME)\b|\?\?\?/i)
    // An unavailable feature has no standing prerequisite to hint at.
    expect(row.note).toBeUndefined()
    // A row speaks only for hosts this product is served from.
    if (row.hosts) {
      expect(row.hosts.length).toBeGreaterThan(0)
      for (const host of row.hosts) expect(APP_ONLY_HOSTS.has(host)).toBe(true)
    }
    if (row.adopted) return
    expect(row.disclaimer.toLowerCase()).toContain(NOT_YET_AVAILABLE)
    expect(row.disclaimer).toMatch(/^[A-Z]/)
    expect(row.disclaimer).toMatch(/\.$/)
  })

  it.each(unavailable.filter(([, row]) => row.adopted))('%s adopts the exact sentence its own control shows', (id, row) => {
    // The control keeps its own gate; this pin stops the manifest and the
    // control from telling two different stories about the same feature.
    expect(row.adopted).toBe(true)
    expect(ADOPTED_SOURCES[id], `${id} has no source file in ADOPTED_SOURCES`).toBeTruthy()
    const source = readFileSync(join(WEB_ROOT, ADOPTED_SOURCES[id]), 'utf8')
    expect(source).toContain(row.disclaimer)
    expect(row.evidence).toMatch(/^source:/)
  })

  it('ships no readable source path, because this module is in the production bundle', () => {
    // The element source fence (src/cadedit/elementSourceFence.test.js) refuses
    // these in the built output; this is the same rule, checked without a build.
    for (const [id, row] of rows) {
      for (const value of Object.values(row).flat()) {
        if (typeof value !== 'string') continue
        expect(value, id).not.toMatch(/src\/[\w./-]+\.(?:jsx?|mjs|css)/)
        expect(value, id).not.toMatch(/src\/site\//)
      }
    }
    expect(Object.keys(ADOPTED_SOURCES).sort()).toEqual(unavailable.filter(([, row]) => row.adopted).map(([id]) => id).sort())
  })

  it('keeps every visitor-facing string inside the copy law', () => {
    for (const [, row] of rows) {
      for (const text of [row.label, row.disclaimer, row.note].filter(Boolean)) {
        expect(text).not.toMatch(DASH)
        expect(text).not.toMatch(/LEAF Automation|Leaf automation|leaf automation/)
        expect(text).not.toMatch(DATE_PROMISE)
      }
    }
  })

  it('declares every feature id that a source file reads', () => {
    const reader = /\b(?:notYetAvailable|studioFeature|studioFeatureAvailable|studioFeatureNote|availabilityProps|guardUnavailable)\(\s*'([^']+)'/g
    const read = new Map()
    for (const file of sourceFiles(SRC_ROOT)) {
      const source = readFileSync(file, 'utf8')
      for (const match of source.matchAll(reader)) {
        const at = relative(WEB_ROOT, file).split(sep).join('/')
        read.set(match[1], [...(read.get(match[1]) || []), at])
      }
    }
    // The manifest is load-bearing: the shell reads it, in more than one file.
    expect(read.size).toBeGreaterThanOrEqual(4)
    for (const [id, files] of read) {
      expect(studioFeature(id).declared, `${id} read by ${files.join(', ')}`).toBe(true)
    }
  })
})

const STAGING = Object.freeze({ hostname: STAGING_HOST, walk: false })
const PRODUCTION = Object.freeze({ hostname: 'platform.leafdesign.ai', walk: false })
const LOCAL = Object.freeze({ hostname: 'localhost', walk: false })
const WALKING = Object.freeze({ hostname: STAGING_HOST, walk: true })

describe('reading the manifest', () => {
  it('hides drawing sync only on production hosts', () => {
    expect(studioFeatureAvailable('cad.drawing-sync', PRODUCTION)).toBe(false)
    expect(studioFeatureAvailable('cad.drawing-sync', STAGING)).toBe(true)
    expect(studioFeatureAvailable('cad.drawing-sync', LOCAL)).toBe(true)
  })

  it('reports an enabled feature as available with no disclaimer on every host', () => {
    for (const env of [STAGING, PRODUCTION, LOCAL, WALKING]) {
      expect(studioFeatureAvailable('site.sample-rooftop', env)).toBe(true)
      expect(notYetAvailable('site.sample-rooftop', env)).toBeNull()
      expect(availabilityProps('site.sample-rooftop', env)).toEqual({})
    }
  })

  it('takes an unavailable feature away with its disclaimer and disabled props', () => {
    const disclaimer = STUDIO_AVAILABILITY['site.sheets'].disclaimer
    expect(studioFeatureAvailable('site.sheets', STAGING)).toBe(false)
    expect(notYetAvailable('site.sheets', STAGING)).toBe(disclaimer)
    expect(availabilityProps('site.sheets', STAGING)).toEqual({
      'aria-disabled': true, title: disclaimer, 'data-availability': 'unavailable', 'data-availability-id': 'site.sheets',
    })
  })

  it('applies a row only on the hosts its evidence speaks for', () => {
    // Staging evidence disables on staging and nowhere else: a later
    // production promotion must not inherit a staging-only finding.
    expect(notYetAvailable('shared.author-tool', STAGING)).toBe(STUDIO_AVAILABILITY['shared.author-tool'].disclaimer)
    expect(notYetAvailable('shared.author-tool', PRODUCTION)).toBeNull()
    expect(notYetAvailable('shared.author-tool', LOCAL)).toBeNull()
    // The sheets hub leaves every app host, so that row covers all of them.
    expect(notYetAvailable('site.sheets', PRODUCTION)).toBe(STUDIO_AVAILABILITY['site.sheets'].disclaimer)
    expect(notYetAvailable('site.sheets', LOCAL)).toBeNull()
    // A row with no hosts is decided by the code itself and holds everywhere.
    for (const env of [STAGING, PRODUCTION, LOCAL]) {
      expect(notYetAvailable('project.create-from-sample-drawing', env))
        .toBe(STUDIO_AVAILABILITY['project.create-from-sample-drawing'].disclaimer)
    }
    // Host matching ignores case.
    expect(notYetAvailable('shared.author-tool', { hostname: 'PLATFORM-STAGING.LEAFDESIGN.AI', walk: false })).not.toBeNull()
  })

  it('lifts every disable for a verification walk, and for nothing else', () => {
    for (const [id, row] of unavailable) {
      expect(studioFeatureAvailable(id, WALKING), id).toBe(true)
      expect(notYetAvailable(id, WALKING), id).toBeNull()
      // The table still states the truth while the override is on.
      expect(studioFeature(id, WALKING).status).toBe('unavailable')
      expect(studioFeature(id, WALKING).disclaimer).toBe(row.disclaimer)
    }
    expect(notYetAvailable('site.sheets', { hostname: STAGING_HOST, walk: 'yes' })).not.toBeNull()
    expect(notYetAvailable('site.sheets', { hostname: STAGING_HOST })).not.toBeNull()
  })

  it('reads the override from the address bar or storage, and nothing by default', () => {
    expect(availabilityEnv()).toEqual({ hostname: window.location.hostname, walk: false, investorDemo: false })
    window.localStorage.setItem('leaf.availability.walk', '1')
    try {
      expect(availabilityEnv().walk).toBe(true)
      window.localStorage.setItem('leaf.availability.walk', 'true')
      expect(availabilityEnv().walk).toBe(false)
    } finally { window.localStorage.removeItem('leaf.availability.walk') }
    const before = window.location.href
    window.history.replaceState({}, '', '/app?availability=walk')
    try { expect(availabilityEnv().walk).toBe(true) } finally { window.history.replaceState({}, '', before) }
    window.history.replaceState({}, '', '/app?availability=1')
    try { expect(availabilityEnv().walk).toBe(false) } finally { window.history.replaceState({}, '', before) }
  })

  it('treats an undeclared id as no opinion, never as off', () => {
    const feature = studioFeature('cad.no-such-feature', STAGING)
    expect(feature.declared).toBe(false)
    expect(feature.available).toBe(true)
    expect(notYetAvailable('cad.no-such-feature', STAGING)).toBeNull()
    // Object.prototype keys are not rows.
    expect(studioFeature('constructor', STAGING).declared).toBe(false)
    expect(studioFeature('__proto__', STAGING).declared).toBe(false)
  })

  it('maps a catalog tool to its row, and only a tool a row names', () => {
    const disclaimer = STUDIO_AVAILABILITY['solar.arlo-design'].disclaimer
    expect(catalogToolDisclaimer('arlo-design', STAGING)).toBe(disclaimer)
    expect(catalogToolDisclaimer('arlo-design', LOCAL)).toBeNull()
    expect(catalogToolDisclaimer('arlo-design', WALKING)).toBeNull()
    expect(catalogToolDisclaimer('count-by-layer', STAGING)).toBeNull()
    for (const name of ['constructor', '__proto__', '', null, undefined, 7]) expect(catalogToolDisclaimer(name, STAGING)).toBeNull()
    // Every `tool` names one row, so two rows can never fight over a tool.
    const tools = rows.filter(([, row]) => row.tool).map(([, row]) => row.tool)
    expect(new Set(tools).size).toBe(tools.length)
  })

  it('shows the view-only disclaimer for every investor tool, even during a walk', () => {
    for (const env of [STAGING, WALKING]) {
      for (const name of ['arlo-design', 'count-by-layer', 'unknown']) {
        expect(catalogToolDisclaimer(name, { ...env, investorDemo: true })).toBe('Not available in the view-only demo')
        expect(catalogToolDisclaimer(name, { ...env, investorDemo: false })).toBe(catalogToolDisclaimer(name, env))
      }
    }
  })

  it('skips the address bar for a tool without a row and no investor session', () => {
    const readSearch = vi.fn(() => '')
    const location = { hostname: STAGING_HOST, get search() { return readSearch() } }
    vi.stubGlobal('window', { location, localStorage: window.localStorage })
    try {
      expect(catalogToolDisclaimer('count-by-layer')).toBeNull()
      expect(readSearch).not.toHaveBeenCalled()
    } finally { vi.unstubAllGlobals() }
  })

  it('hands an enabled feature its prerequisite note and an unavailable one none', () => {
    expect(studioFeatureNote('cad.connect-autocad', STAGING)).toBe(STUDIO_AVAILABILITY['cad.connect-autocad'].note)
    expect(studioFeatureNote('site.sheets', STAGING)).toBeNull()
    expect(studioFeatureNote('site.sheets', WALKING)).toBeNull()
    expect(studioFeatureNote('site.sample-rooftop', STAGING)).toBeNull()
  })

  it('never runs a handler for an unavailable feature', () => {
    let ran = 0
    let prevented = 0
    const handler = () => { ran += 1 }
    guardUnavailable('site.sheets', handler, STAGING)({ preventDefault: () => { prevented += 1 } })
    expect(ran).toBe(0)
    expect(prevented).toBe(1)
    expect(guardUnavailable('site.sample-rooftop', handler, STAGING)).toBe(handler)
    expect(guardUnavailable('site.sheets', handler, LOCAL)).toBe(handler)
  })

  it('lists every row once, in table order, as the table states it', () => {
    const list = listStudioAvailability()
    expect(list.map((feature) => feature.id)).toEqual(Object.keys(STUDIO_AVAILABILITY))
    expect(list.every((feature) => feature.declared)).toBe(true)
    // The list is the table, on no host: a staging-only row still reads unavailable.
    expect(list.filter((feature) => !feature.available).length).toBe(unavailable.length)
  })
})
