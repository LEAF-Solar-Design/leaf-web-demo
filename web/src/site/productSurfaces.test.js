import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import {
  DEFAULT_PRODUCT_SURFACE,
  PRODUCT_SURFACES,
  SHARED_WORKSPACE_CAPABILITIES,
  normalizeProductSurface,
  productSurface,
  productSurfaceFromSearch,
  productSurfaceStates,
  searchForProductSurface,
  surfaceContract,
  surfaceGround,
} from './productSurfaces.js'
import { groundShowsDrawing } from './SurfaceGrounds.jsx'

describe('product surface contract', () => {
  it('defines the four profiles once over one shared capability substrate', () => {
    expect(PRODUCT_SURFACES.map(({ id }) => id)).toEqual(['browser', 'cad', 'solar', 'ios'])
    expect(new Set(PRODUCT_SURFACES.map(({ id }) => id)).size).toBe(4)
    expect(SHARED_WORKSPACE_CAPABILITIES).toEqual(expect.arrayContaining([
      'conversation', 'annotations', 'approvals', 'marathons', 'one-shot execution',
    ]))
  })

  it('fails closed to the live CAD profile for missing or invalid selectors', () => {
    expect(normalizeProductSurface('unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurfaceFromSearch('?surface=unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurface('unknown').id).toBe(DEFAULT_PRODUCT_SURFACE)
  })

  it('preserves auth and proof parameters while changing only the product surface', () => {
    const next = searchForProductSurface('?code=abc&state=xyz&proof=1&surface=cad', 'ios')
    const params = new URLSearchParams(next)
    expect(params.get('code')).toBe('abc')
    expect(params.get('state')).toBe('xyz')
    expect(params.get('proof')).toBe('1')
    expect(params.get('surface')).toBe('ios')
  })

  it('reports capability truth without inventing unavailable routes', () => {
    expect(productSurfaceStates({ sessionActive: false, hasDrawing: true, apsLive: true }).cad.state).toBe('sign-in')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: false, apsLive: true }).cad.state).toBe('setup')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: false }).cad.state).toBe('unavailable')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: true }).cad.state).toBe('available')
    expect(productSurfaceStates({ sessionActive: true }).solar.state).toBe('beta')
    expect(productSurfaceStates({ sessionActive: true }).ios).toEqual({ state: 'setup', label: 'Setup required' })
  })

  it('describes the browser projection without a bare "arrive in the next product wave" placeholder', () => {
    const frame = readFileSync(`${process.cwd()}/src/components/ProductSurfaceTabs.jsx`, 'utf8')
    expect(frame).toContain('Project-scoped files, conversation, and browser composition')
    expect(frame).not.toContain('arrive in the next product wave')
  })

  it('places the profile rail below the persistent header', () => {
    const css = readFileSync(`${process.cwd()}/src/site/landing.css`, 'utf8')
    expect(css).toMatch(/\.lp-topbar\s*\{[^}]*height:\s*52px;[^}]*z-index:\s*50;/s)
    expect(css).toMatch(/\.tc-product-nav\s*\{[^}]*inset:\s*52px 0 auto;[^}]*z-index:\s*34;/s)
    expect(css).toMatch(/\.tc-product-frame\s*\{[^}]*inset:\s*96px 0 0;/s)
  })
})

// ---------------------------------------------------------------------------
// THE SURFACE CONTRACT (standardization slice 1).
//
// EQUALS-TODAY FIXTURE. Written by hand from the code sites below, never
// copied out of productSurfaces.js, so any later edit to a default fails
// loudly instead of silently redefining the shell.
//
//   ground          SurfaceGrounds.jsx:99 DRAWING_SURFACES = new Set(['cad','solar'])
//                   e2e/local/one-shell-mount.spec.mjs:131 (board / device stage)
//   productFrame    App.jsx:2798  activeSurface !== 'cad'
//   workspaceCard   App.jsx:2819  activeSurface === 'cad' || activeSurface === 'solar'
//   cockpit         App.jsx:2242  drafting = groundShowsDrawing(activeSurface),
//                   mounted at App.jsx:2526 (band) and App.jsx:2858 (ribbon)
//   stageBranch     ToolCast.jsx:1417 (cad) / 2060 (ios) / 2122 (frame)
//   projectSlot     App.jsx:2806-2807 IosSurface for 'ios'
//   toolbar.ribbon  App.jsx:2858
//   toolbar.home    App.jsx:2225 useState('draw'); CockpitTopBand.jsx:17-18
//   toolbar.quick   App.jsx:2417-2432 — props, not data, so null everywhere
//   rails.left      App.jsx:2247 navSpine (wide-viewport default)
//   rails.right     App.jsx:3414 JobRail spine (wide-viewport default)
//   rails.dock      App.jsx:3148; section order PropertiesDock.jsx:132-142
//   commandLine     App.jsx:3376 PromptBox commandLine
//   authoring       App.jsx:2699 AuthorPanel in the un-gated nav rail
//   versions        App.jsx:3001 VersionHistory inside the card gated at 2819
//   conversations   converse.js:129-134 sessionCacheKey(project, drawing)
//   builds.routes   App.jsx:2674 catalog run path; no marathon route exists
//   contextMenu     zero contextmenu handlers under web/src
//   everything else undeclared today (null)
// ---------------------------------------------------------------------------
const CONTRACT_FIXTURE = {
  browser: {
    ground: 'board',
    chrome: { productFrame: true, workspaceCard: false, cockpit: false, stageBranch: 'frame', projectSlot: null },
    toolbar: { ribbon: false, home: null, quick: null },
    rails: { left: 'nav', right: 'job-rail', dock: null },
    commandLine: false,
    authoring: true,
    versions: 'none',
    conversations: { scope: 'drawing' },
    integrations: null,
    builds: { routes: ['one-shot'] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
  cad: {
    ground: 'drawing',
    chrome: { productFrame: false, workspaceCard: true, cockpit: true, stageBranch: 'cad', projectSlot: null },
    toolbar: { ribbon: true, home: 'draw', quick: null },
    rails: { left: 'spine', right: 'job-spine', dock: ['layers', 'drawing', 'selection', 'plan'] },
    commandLine: true,
    authoring: true,
    versions: 'drawing',
    conversations: { scope: 'drawing' },
    integrations: null,
    builds: { routes: ['one-shot'] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
  solar: {
    ground: 'drawing',
    // productFrame is TRUE on solar: App.jsx:2798 tests `!== 'cad'`, so the
    // frame renders over the shown workspace card. Today's behaviour, pinned.
    chrome: { productFrame: true, workspaceCard: true, cockpit: true, stageBranch: 'frame', projectSlot: null },
    toolbar: { ribbon: true, home: 'draw', quick: null },
    rails: { left: 'spine', right: 'job-spine', dock: ['layers', 'drawing', 'selection', 'plan'] },
    commandLine: true,
    authoring: true,
    versions: 'drawing',
    conversations: { scope: 'drawing' },
    integrations: null,
    builds: { routes: ['one-shot'] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
  ios: {
    ground: 'device-stage',
    chrome: { productFrame: true, workspaceCard: false, cockpit: false, stageBranch: 'ios', projectSlot: 'ios-surface' },
    toolbar: { ribbon: false, home: null, quick: null },
    rails: { left: 'nav', right: 'job-rail', dock: null },
    commandLine: false,
    authoring: true,
    versions: 'none',
    conversations: { scope: 'drawing' },
    integrations: null,
    // The console carries NO ship-lane launch control (IosSurface.jsx:3-4 is
    // props-only); the repo's only one is the stage's ToolCast.jsx:2097.
    builds: { routes: ['one-shot'] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
}

const SURFACE_IDS = ['browser', 'cad', 'solar', 'ios']

const CONTRACT_KEYS = [
  'ground', 'chrome', 'toolbar', 'rails', 'commandLine', 'authoring', 'versions',
  'conversations', 'integrations', 'builds', 'contextMenu', 'shortcuts',
  'entitlements', 'resetOn', 'a11y', 'tourAnchors',
]

const ENUMS = {
  ground: ['drawing', 'board', 'device-stage'],
  stageBranch: ['cad', 'ios', 'frame'],
  projectSlot: ['ios-surface', null],
  left: ['spine', 'nav', 'none'],
  right: ['job-spine', 'job-rail', 'none'],
  versions: ['drawing', 'none', null],
  scope: ['project', 'drawing', null],
}

// Every object and array in the tree, so "frozen" is proven at EVERY level
// rather than only at the top.
function everyNode(value, out = []) {
  if (value === null || typeof value !== 'object') return out
  out.push(value)
  for (const key of Object.keys(value)) everyNode(value[key], out)
  return out
}

describe('Surface Contract — schema', () => {
  for (const id of SURFACE_IDS) {
    it(`${id} declares every contract key exactly once`, () => {
      const contract = surfaceContract(id)
      expect(Object.keys(contract).sort()).toEqual([...CONTRACT_KEYS].sort())
      expect(Object.keys(contract.chrome).sort())
        .toEqual(['cockpit', 'productFrame', 'projectSlot', 'stageBranch', 'workspaceCard'])
      expect(Object.keys(contract.toolbar).sort()).toEqual(['home', 'quick', 'ribbon'])
      expect(Object.keys(contract.rails).sort()).toEqual(['dock', 'left', 'right'])
      expect(Object.keys(contract.conversations)).toEqual(['scope'])
      expect(Object.keys(contract.builds)).toEqual(['routes'])
    })

    it(`${id} carries only legal enum values and honest types`, () => {
      const c = surfaceContract(id)
      expect(ENUMS.ground).toContain(c.ground)
      expect(ENUMS.stageBranch).toContain(c.chrome.stageBranch)
      expect(ENUMS.projectSlot).toContain(c.chrome.projectSlot)
      expect(ENUMS.left).toContain(c.rails.left)
      expect(ENUMS.right).toContain(c.rails.right)
      expect(ENUMS.versions).toContain(c.versions)
      expect(ENUMS.scope).toContain(c.conversations.scope)
      for (const flag of [c.chrome.productFrame, c.chrome.workspaceCard, c.chrome.cockpit,
        c.toolbar.ribbon, c.commandLine]) {
        expect(typeof flag).toBe('boolean')
      }
      expect(c.authoring === null || typeof c.authoring === 'boolean').toBe(true)
      expect(c.rails.dock === null || Array.isArray(c.rails.dock)).toBe(true)
      expect(c.toolbar.quick === null || Array.isArray(c.toolbar.quick)).toBe(true)
      expect(Array.isArray(c.contextMenu)).toBe(true)
      expect(Array.isArray(c.builds.routes)).toBe(true)
      // Nothing in the contract may be a function: this module is data.
      for (const node of everyNode(c)) {
        for (const key of Object.keys(node)) {
          expect(typeof node[key]).not.toBe('function')
        }
      }
    })

    it(`${id} is deep-frozen at every level`, () => {
      const nodes = everyNode(surfaceContract(id))
      // A contract with only scalars would make this test vacuous.
      expect(nodes.length).toBeGreaterThan(5)
      for (const node of nodes) expect(Object.isFrozen(node)).toBe(true)
    })
  }

  it('the record fields the shell already renders are untouched by the contract', () => {
    // A contract edit must never reshape the presentation record beside it.
    expect(Object.keys(productSurface('cad')).sort())
      .toEqual(['contract', 'description', 'eyebrow', 'familyIds', 'id', 'label', 'title'])
    expect(productSurface('solar').familyIds).toEqual(['stringing', 'placement'])
    expect(productSurface('cad').familyIds).toBe(null)
  })

  it('an unknown id normalizes to the CAD contract rather than undefined', () => {
    expect(surfaceContract('not-a-surface')).toBe(surfaceContract('cad'))
    expect(surfaceContract(undefined)).toBe(surfaceContract('cad'))
    expect(surfaceGround('not-a-surface')).toBe('drawing')
  })
})

describe('Surface Contract — equals today', () => {
  for (const id of SURFACE_IDS) {
    it(`${id} matches the hand-written fixture of today's behaviour`, () => {
      expect(surfaceContract(id)).toEqual(CONTRACT_FIXTURE[id])
    })
  }

  it('covers every surface the module ships (no row can be quietly dropped)', () => {
    expect(Object.keys(CONTRACT_FIXTURE).sort()).toEqual([...SURFACE_IDS].sort())
    expect(PRODUCT_SURFACES.map(({ id }) => id).sort()).toEqual([...SURFACE_IDS].sort())
  })
})

describe('Surface Contract — cross-check against the live ground gate', () => {
  for (const id of SURFACE_IDS) {
    it(`${id}: groundShowsDrawing agrees with the declared ground`, () => {
      expect(groundShowsDrawing(id)).toBe(surfaceGround(id) === 'drawing')
    })
  }

  it('the drawing grounds are exactly cad and solar on both sides', () => {
    expect(SURFACE_IDS.filter((id) => surfaceGround(id) === 'drawing')).toEqual(['cad', 'solar'])
    expect(SURFACE_IDS.filter(groundShowsDrawing)).toEqual(['cad', 'solar'])
  })
})
