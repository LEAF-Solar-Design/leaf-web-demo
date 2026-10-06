// @vitest-environment jsdom
//
// S24 (A14): the URL view keys. The claims: every boot flag (demo=off
// included), the drawing identity and the Auth0 callback keys survive every
// write BYTE FOR BYTE; `push` adds one history entry and `replace` adds none;
// a patch naming an unknown key (or a value outside its grammar) is refused
// whole and writes nothing; the pathname and hash are always kept.
import { act, cleanup, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  CAM_FOCUS,
  VIEW_DRAWERS,
  VIEW_PARAM_EVENT,
  VIEW_PARAM_KEYS,
  buildViewSearch,
  formatCamParam,
  isViewParamValue,
  parseCamParam,
  pushOnOpen,
  readViewParam,
  useViewParam,
  writeViewParams,
} from './urlState.js'

// Boot flags and Auth0 keys in shapes URLSearchParams would NOT round-trip:
// `%2F` (comes back `/`), `+` (comes back `%20`... or a space), `%3D%3D`, a
// bare flag with no `=`, and an empty value.
const BOOT = '?demo=off&fixture=edit&ops=1&dev=1&customize=1&proof=1&surface=solar&drawing=d-7%2Fa'
  + '&code=Ab%2Fc+d&state=eyJ0Ijo%3D%3D&tour&empty='
const BOOT_SEGMENTS = BOOT.slice(1).split('&')

function foreignSegments(search) {
  const body = search.startsWith('?') ? search.slice(1) : search
  if (body === '') return []
  return body.split('&').filter((segment) => !VIEW_PARAM_KEYS.includes(segment.split('=')[0]))
}

function setUrl(url) {
  window.history.replaceState(null, '', url)
}

beforeEach(() => setUrl(`/app${BOOT}#plan-7`))
afterEach(() => {
  cleanup()
  setUrl('/')
})

describe('the allow-list', () => {
  it('owns exactly tool, drawer, sel and cam', () => {
    expect([...VIEW_PARAM_KEYS]).toEqual(['tool', 'drawer', 'sel', 'cam'])
    expect(Object.isFrozen(VIEW_PARAM_KEYS)).toBe(true)
    expect(VIEW_DRAWERS).toContain('details')
  })

  it('never treats a boot flag, the drawing or an Auth0 key as a view key', () => {
    for (const key of ['demo', 'fixture', 'ops', 'dev', 'surface', 'drawing', 'code', 'state', 'proof', 'customize']) {
      expect(readViewParam(key)).toBeNull()
      expect(isViewParamValue(key, 'off')).toBe(false)
      expect(buildViewSearch(BOOT, { [key]: 'x' })).toBeNull()
    }
  })
})

describe('buildViewSearch', () => {
  it('appends, updates in place and removes without touching any other byte', () => {
    const withAll = buildViewSearch(BOOT, { tool: 'place_combiner', drawer: 'details', sel: '1A2F', cam: '12.5,-3.25,1.8' })
    expect(withAll).toBe(`${BOOT}&tool=place_combiner&drawer=details&sel=1A2F&cam=12.5,-3.25,1.8`)
    const updated = buildViewSearch(withAll, { drawer: 'jobs' })
    expect(updated).toBe(`${BOOT}&tool=place_combiner&drawer=jobs&sel=1A2F&cam=12.5,-3.25,1.8`)
    const cleared = buildViewSearch(updated, { tool: null, drawer: null, sel: undefined, cam: null })
    expect(cleared).toBe(BOOT)
  })

  it('keeps a view key in its position among foreign keys and drops duplicates', () => {
    expect(buildViewSearch('?demo=off&drawer=nav&code=a%2Fb&drawer=jobs', { drawer: 'plan' }))
      .toBe('?demo=off&drawer=plan&code=a%2Fb')
  })

  it('preserves encoded foreign keys, duplicate boot flags and empty segments', () => {
    const search = '?%64emo=off&&code=a%2fb&state=a+b&demo=off&empty=&'
    const written = buildViewSearch(search, { drawer: 'details' })
    expect(written).toBe(`${search}&drawer=details`)
    expect(buildViewSearch(written, { drawer: null })).toBe(search)
    expect(buildViewSearch('?demo=off&%64rawer=nav&state=a+b', { drawer: 'details' }))
      .toBe('?demo=off&drawer=details&state=a+b')
  })

  it('returns the identical string for a no-op patch', () => {
    const search = `${BOOT}&drawer=details`
    expect(buildViewSearch(search, { drawer: 'details' })).toBe(search)
    expect(buildViewSearch(BOOT, { tool: null })).toBe(BOOT)
    expect(buildViewSearch(BOOT, {})).toBe(BOOT)
  })

  it('handles an empty search and removing the last key', () => {
    expect(buildViewSearch('', { drawer: 'nav' })).toBe('?drawer=nav')
    expect(buildViewSearch('?drawer=nav', { drawer: null })).toBe('')
  })

  it('refuses the whole patch on an unknown key or an out-of-grammar value', () => {
    for (const patch of [
      { demo: 'on' }, { drawing: 'd-1' }, { code: 'x' }, { tool: 'ok', foo: '1' },
      { drawer: 'evil' }, { drawer: 'none' }, { tool: 'a b' }, { tool: '' }, { sel: '<script>' },
      { sel: 'x'.repeat(129) }, { cam: '1,2,-3' }, { cam: '1,2,0' }, { cam: 'NaN,1,1' }, { cam: 'iso' },
      { tool: 42 }, null, [], 'drawer=nav',
    ]) {
      expect(buildViewSearch(BOOT, patch)).toBeNull()
    }
  })
})

describe('writeViewParams on the live URL', () => {
  it('keeps demo=off, the Auth0 keys, the pathname and the hash across every write, byte for byte', () => {
    const writes = [
      [{ drawer: 'details' }, 'push'],
      [{ sel: '2B' }, 'replace'],
      [{ cam: '0,0,1' }, 'replace'],
      [{ tool: 'place_combiner' }, 'push'],
      [{ drawer: 'jobs' }, 'push'],
      [{ cam: CAM_FOCUS }, 'replace'],
      [{ sel: null }, 'replace'],
      [{ drawer: null, tool: null }, 'replace'],
      [{ cam: null }, 'replace'],
    ]
    for (const [patch, mode] of writes) {
      expect(writeViewParams(patch, { mode })).toBe(true)
      expect(window.location.pathname).toBe('/app')
      expect(window.location.hash).toBe('#plan-7')
      expect(foreignSegments(window.location.search)).toEqual(BOOT_SEGMENTS)
      expect(window.location.search.startsWith(BOOT)).toBe(true)
      expect(new URLSearchParams(window.location.search).get('demo')).toBe('off')
    }
    expect(window.location.search).toBe(BOOT)
  })

  it('push adds exactly one history entry, replace adds none, a no-op adds none', () => {
    const start = window.history.length
    expect(writeViewParams({ drawer: 'details' }, { mode: 'push' })).toBe(true)
    expect(window.history.length).toBe(start + 1)
    expect(window.location.search).toBe(`${BOOT}&drawer=details`)
    expect(writeViewParams({ sel: '1A' }, { mode: 'replace' })).toBe(true)
    expect(window.history.length).toBe(start + 1)
    expect(window.location.search).toBe(`${BOOT}&drawer=details&sel=1A`)
    expect(writeViewParams({ sel: '1A' }, { mode: 'push' })).toBe(true)
    expect(window.history.length).toBe(start + 1)
    expect(writeViewParams({ drawer: null })).toBe(true)
    expect(window.history.length).toBe(start + 1)
    expect(window.location.search).toBe(`${BOOT}&sel=1A`)
  })

  it('replace keeps the entry state the router or Auth0 left there', () => {
    window.history.replaceState({ owner: 'auth0' }, '', window.location.href)
    writeViewParams({ cam: '1,1,2' }, { mode: 'replace' })
    expect(window.history.state).toEqual({ owner: 'auth0' })
  })

  it('refuses an unknown key, a bad value or a bad mode and writes nothing', () => {
    const start = window.history.length
    const href = window.location.href
    let events = 0
    const count = () => { events += 1 }
    window.addEventListener(VIEW_PARAM_EVENT, count)
    try {
      expect(writeViewParams({ demo: 'on' }, { mode: 'push' })).toBe(false)
      expect(writeViewParams({ drawing: 'other' })).toBe(false)
      expect(writeViewParams({ tool: 'ok', state: 'forged' }, { mode: 'push' })).toBe(false)
      expect(writeViewParams({ drawer: 'admin' })).toBe(false)
      expect(writeViewParams({ drawer: 'nav' }, { mode: 'assign' })).toBe(false)
    } finally {
      window.removeEventListener(VIEW_PARAM_EVENT, count)
    }
    expect(window.location.href).toBe(href)
    expect(window.history.length).toBe(start)
    expect(events).toBe(0)
  })

  it('announces a real write once and a no-op not at all', () => {
    let events = 0
    const count = () => { events += 1 }
    window.addEventListener(VIEW_PARAM_EVENT, count)
    try {
      writeViewParams({ drawer: 'nav' })
      writeViewParams({ drawer: 'nav' })
    } finally {
      window.removeEventListener(VIEW_PARAM_EVENT, count)
    }
    expect(events).toBe(1)
  })
})

describe('readViewParam', () => {
  it('reads valid values and fails closed on hostile ones', () => {
    expect(readViewParam('drawer', '?demo=off&drawer=details')).toBe('details')
    expect(readViewParam('drawer', '?drawer=%3Cscript%3E')).toBeNull()
    expect(readViewParam('drawer', '?drawer=none')).toBeNull()
    expect(readViewParam('tool', '?tool=place_combiner&tool=other')).toBe('place_combiner')
    expect(readViewParam('sel', '?sel=%E0%A4%A')).toBeNull()
    expect(readViewParam('cam', '?cam=1,2,3')).toBe('1,2,3')
    expect(readViewParam('cam', '?cam=focus')).toBe('focus')
    expect(readViewParam('cam', '?cam=1,2')).toBeNull()
    expect(readViewParam('nope', '?nope=1')).toBeNull()
  })
})

describe('camera values', () => {
  it('formats a pose and parses it back to a setView argument', () => {
    const value = formatCamParam({ target: [12.34567, -0.0001, 0], zoom: 1.23456789 })
    expect(value).toBe('12.346,0,1.23457')
    expect(parseCamParam(value)).toEqual({ center: { x: 12.346, y: 0 }, zoom: 1.23457 })
  })

  it('refuses a pose it cannot carry', () => {
    expect(formatCamParam(null)).toBeNull()
    expect(formatCamParam({ target: [Infinity, 0], zoom: 1 })).toBeNull()
    expect(formatCamParam({ target: [0, 0], zoom: 0 })).toBeNull()
    expect(formatCamParam({ target: [1e30, 0], zoom: 1 })).toBeNull()
    expect(parseCamParam('focus')).toBeNull()
  })

  it('carries small positive zooms without exponential notation', () => {
    expect(formatCamParam({ target: [0, 0], zoom: 0.0000001 })).toBe('0,0,0.0000001')
  })
})

describe('pushOnOpen', () => {
  it('pushes an open and replaces a close', () => {
    expect(pushOnOpen(null, 'details')).toBe('push')
    expect(pushOnOpen('nav', 'jobs')).toBe('push')
    expect(pushOnOpen('details', null)).toBe('replace')
  })
})

describe('useViewParam', () => {
  it('follows writes and Back/Forward (popstate) on its own', async () => {
    const seen = []
    function Probe() {
      seen.push(useViewParam('drawer'))
      return null
    }
    render(createElement(Probe))
    expect(seen.at(-1)).toBeNull()
    act(() => { writeViewParams({ drawer: 'details' }, { mode: 'push' }) })
    expect(seen.at(-1)).toBe('details')
    act(() => {
      window.history.replaceState(null, '', `/app${BOOT}#plan-7`)
      window.dispatchEvent(new Event('popstate'))
    })
    expect(seen.at(-1)).toBeNull()
  })
})
