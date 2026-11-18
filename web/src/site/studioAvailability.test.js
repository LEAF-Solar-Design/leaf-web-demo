// The availability manifest's own gate. A row is a promise to a visitor: an
// enabled row says "this works" and names the walk that proved it, an
// unavailable row says "not yet" in words short and honest enough to show on
// the control. These rows hold every entry to that, and hold every reader in
// web/src to an id that exists.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  AVAILABILITY_STATUSES,
  NOT_YET_AVAILABLE,
  STUDIO_AVAILABILITY,
  STUDIO_AVAILABILITY_SCHEMA,
  availabilityProps,
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
    if (row.adopted) return
    expect(row.disclaimer.toLowerCase()).toContain(NOT_YET_AVAILABLE)
    expect(row.disclaimer).toMatch(/^[A-Z]/)
    expect(row.disclaimer).toMatch(/\.$/)
  })

  it.each(unavailable.filter(([, row]) => row.adopted))('%s adopts the exact sentence its own control shows', (id, row) => {
    // The control keeps its own gate; this pin stops the manifest and the
    // control from telling two different stories about the same feature.
    const source = readFileSync(join(WEB_ROOT, row.adopted), 'utf8')
    expect(source).toContain(row.disclaimer)
    expect(row.evidence).toBe(`source:web/${row.adopted}`)
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

describe('reading the manifest', () => {
  it('reports an enabled feature as available with no disclaimer', () => {
    expect(studioFeatureAvailable('site.sample-rooftop')).toBe(true)
    expect(notYetAvailable('site.sample-rooftop')).toBeNull()
    expect(availabilityProps('site.sample-rooftop')).toEqual({})
  })

  it('reports an unavailable feature with its disclaimer and disabled props', () => {
    const disclaimer = STUDIO_AVAILABILITY['site.sheets'].disclaimer
    expect(studioFeatureAvailable('site.sheets')).toBe(false)
    expect(notYetAvailable('site.sheets')).toBe(disclaimer)
    expect(availabilityProps('site.sheets')).toEqual({
      'aria-disabled': true, title: disclaimer, 'data-availability': 'unavailable', 'data-availability-id': 'site.sheets',
    })
  })

  it('treats an undeclared id as no opinion, never as off', () => {
    const feature = studioFeature('cad.no-such-feature')
    expect(feature.declared).toBe(false)
    expect(feature.available).toBe(true)
    expect(notYetAvailable('cad.no-such-feature')).toBeNull()
    // Object.prototype keys are not rows.
    expect(studioFeature('constructor').declared).toBe(false)
    expect(studioFeature('__proto__').declared).toBe(false)
  })

  it('hands an enabled feature its prerequisite note and an unavailable one none', () => {
    expect(studioFeatureNote('cad.connect-autocad')).toBe(STUDIO_AVAILABILITY['cad.connect-autocad'].note)
    expect(studioFeatureNote('site.sheets')).toBeNull()
    expect(studioFeatureNote('site.sample-rooftop')).toBeNull()
  })

  it('never runs a handler for an unavailable feature', () => {
    let ran = 0
    let prevented = 0
    const handler = () => { ran += 1 }
    guardUnavailable('site.sheets', handler)({ preventDefault: () => { prevented += 1 } })
    expect(ran).toBe(0)
    expect(prevented).toBe(1)
    expect(guardUnavailable('site.sample-rooftop', handler)).toBe(handler)
  })

  it('lists every row once, in table order, for the capability measure', () => {
    const list = listStudioAvailability()
    expect(list.map((feature) => feature.id)).toEqual(Object.keys(STUDIO_AVAILABILITY))
    expect(list.every((feature) => feature.declared)).toBe(true)
    expect(list.filter((feature) => !feature.available).length).toBe(unavailable.length)
  })
})
