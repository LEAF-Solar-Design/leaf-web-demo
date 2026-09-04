import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import {
  DEFAULT_PRODUCT_SURFACE,
  PRODUCT_SURFACES,
  SHARED_WORKSPACE_CAPABILITIES,
  deepFreeze,
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
  it('defines the five profiles once over one shared capability substrate', () => {
    expect(PRODUCT_SURFACES.map(({ id }) => id)).toEqual(['browser', 'cad', 'solar', 'ios', 'sheets'])
    expect(new Set(PRODUCT_SURFACES.map(({ id }) => id)).size).toBe(5)
    expect(SHARED_WORKSPACE_CAPABILITIES).toEqual(expect.arrayContaining([
      'conversation', 'annotations', 'approvals', 'marathons', 'one-shot execution',
    ]))
  })

  it('fails closed to the live CAD profile for missing or invalid selectors', () => {
    expect(normalizeProductSurface('unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurfaceFromSearch('?surface=unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurface('unknown').id).toBe(DEFAULT_PRODUCT_SURFACE)
  })

  // Slice 5b's one behavioural decision, pinned in both directions.
  //
  // /app never hosts sheets (`scene: 'sheets'`, SiteRoot.jsx:234-237), and the
  // tab band renders only records that declare `chrome.tab`. So a sheets id
  // arriving at the studio must fall closed to the default exactly like a
  // nonsense one: resolving it would leave the tablist with no selected tab
  // and the frame describing a page /app cannot show. The RECORD stays
  // addressable, though, selection and lookup are different questions.
  it('a sheets selector on /app falls closed to the default, but the record stays addressable', () => {
    expect(normalizeProductSurface('sheets')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurfaceFromSearch('?surface=sheets')).toBe(DEFAULT_PRODUCT_SURFACE)
    // ...and the search writer cannot mint a link that would do it either.
    expect(new URLSearchParams(searchForProductSurface('', 'sheets')).get('surface'))
      .toBe(DEFAULT_PRODUCT_SURFACE)

    // Lookup is NOT selection: the contract of an unselectable surface is its
    // own, never the default's. Without this the whole slice would be a lie:
    // every sheets assertion below would be reading CAD's contract.
    expect(productSurface('sheets').id).toBe('sheets')
    expect(surfaceContract('sheets')).not.toBe(surfaceContract(DEFAULT_PRODUCT_SURFACE))
    expect(surfaceGround('sheets')).toBe('sheet')

    // The rule is derived from the slot, not from the id: every selectable id
    // is exactly a tab-declaring id.
    for (const { id, contract } of PRODUCT_SURFACES) {
      expect(normalizeProductSurface(id) === id).toBe(contract.chrome.tab)
    }
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
    // sheets has no precondition at all (SheetsPage.jsx renders immediately;
    // loadDemoSolve is a soft enhancement with static fallbacks), so its row
    // must be the SAME under every input, a branch here would be invented.
    for (const input of [
      {}, { sessionActive: false }, { sessionActive: true },
      { sessionActive: false, hasDrawing: false, apsLive: false, iosReady: false },
      { sessionActive: true, hasDrawing: true, apsLive: true, iosReady: true },
    ]) {
      expect(productSurfaceStates(input).sheets).toEqual({ state: 'available', label: 'Ready' })
    }
    // Every shipped surface has a status row, or a tab that is later flipped
    // on would read `undefined.label` and crash the band.
    for (const { id } of PRODUCT_SURFACES) {
      expect(productSurfaceStates({ sessionActive: true })[id]).toBeTruthy()
    }
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
//   ground          SurfaceGrounds.jsx:106 DRAWING_SURFACES = new Set(['cad','solar'])
//                   e2e/local/one-shell-mount.spec.mjs:131 (board / device stage)
//   productFrame    App.jsx:2838  activeSurface !== 'cad'
//   workspaceCard   App.jsx:2859  activeSurface === 'cad' || activeSurface === 'solar'
//   cockpit         App.jsx:2269  drafting = groundShowsDrawing(activeSurface),
//                   mounted at App.jsx:2562 (band) and App.jsx:2898 (ribbon)
//   stageBranch     ToolCast.jsx:1434 (cad) / 2077 (ios) / 2139 (frame)
//   projectSlot     App.jsx:2846-2847 IosSurface for 'ios'
//   toolbar.ribbon  App.jsx:2898
//   toolbar.home    App.jsx:2234 useState('draw'); CockpitTopBand.jsx:17-18
//   toolbar.quick   App.jsx:2453-2468 — props, not data, so null everywhere
//   rails.left      App.jsx:2281 navSpine (wide-viewport default)
//   rails.right     App.jsx:3460 JobRail spine (wide-viewport default)
//   rails.dock      App.jsx:3191 (paneOpen is the second gate, App.jsx:2241);
//                   section order PropertiesDock.jsx:132-142, four sections
//                   being the WITH-DRAWING case
//   groundMaterial  App.jsx:2296 layer accent / App.jsx:2313 solar strings:
//                   the SURFACE term of each gate only (slice 2 added the slot)
//   commandLine     App.jsx:3419 PromptBox commandLine
//   authoring       App.jsx:2735 AuthorPanel in the un-gated nav rail
//   versions        App.jsx:3041 VersionHistory inside the card gated at 2859
//   conversations   converse.js:129-134 sessionCacheKey(project, drawing)
//   builds.routes   App.jsx:2710 catalog run path; no marathon route exists
//   contextMenu     zero contextmenu handlers under web/src
//   everything else undeclared today (null)
// ---------------------------------------------------------------------------
const CONTRACT_FIXTURE = {
  browser: {
    ground: 'board',
    scene: 'app',
    chrome: { productFrame: true, workspaceCard: false, cockpit: false, stageBranch: 'frame', projectSlot: null, tab: true },
    toolbar: { ribbon: false, home: null, quick: null },
    rails: { left: 'nav', right: 'job-rail', dock: null },
    groundMaterial: { layerAccent: null, solarStrings: false },
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
    scene: 'app',
    chrome: { productFrame: false, workspaceCard: true, cockpit: true, stageBranch: 'cad', projectSlot: null, tab: true },
    toolbar: { ribbon: true, home: 'draw', quick: null },
    rails: { left: 'spine', right: 'job-spine', dock: ['layers', 'drawing', 'selection', 'plan'] },
    groundMaterial: { layerAccent: null, solarStrings: false },
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
    scene: 'app',
    // productFrame is TRUE on solar: App.jsx:2838 tests `!== 'cad'`, so the
    // frame renders over the shown workspace card. Today's behaviour, pinned.
    chrome: { productFrame: true, workspaceCard: true, cockpit: true, stageBranch: 'frame', projectSlot: null, tab: true },
    toolbar: { ribbon: true, home: 'draw', quick: null },
    rails: { left: 'spine', right: 'job-spine', dock: ['layers', 'drawing', 'selection', 'plan'] },
    groundMaterial: { layerAccent: 'solar', solarStrings: true },
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
    scene: 'app',
    chrome: { productFrame: true, workspaceCard: false, cockpit: false, stageBranch: 'ios', projectSlot: 'ios-surface', tab: true },
    toolbar: { ribbon: false, home: null, quick: null },
    rails: { left: 'nav', right: 'job-rail', dock: null },
    groundMaterial: { layerAccent: null, solarStrings: false },
    commandLine: false,
    authoring: true,
    versions: 'none',
    conversations: { scope: 'drawing' },
    integrations: null,
    // The console carries NO ship-lane launch control (IosSurface.jsx:3-4 is
    // props-only); the repo's only one is the stage's ToolCast.jsx:2114.
    builds: { routes: ['one-shot'] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
  // SHEETS (slice 5b). Written from the sheets arm, not from the module:
  //   scene           SiteRoot.jsx:234-237 `scene === 'sheets'` renders bare
  //                   <SheetsPage/>; routeScene.js:30 maps /sheets to it
  //   ground          'sheet', SheetsPage.jsx:54 <main className="sheets-root">
  //   chrome.*        SiteRoot.jsx:234-237 wraps it in NOTHING: no frame, no
  //                   card, no cockpit band; and it is not a stage cast, so
  //                   stageBranch is undeclared rather than one of the arms
  //   chrome.tab      false, ProductSurfaceTabs.jsx:72 renders no sheets tab
  //   toolbar/rails   nothing in src/site/sheets/** mounts either
  //   commandLine     App.jsx:3479's PromptBox is console-only
  //   authoring       no rail, so no AuthorPanel is reachable
  //   builds.routes   [], SheetsPage has no run or build control at all
  sheets: {
    ground: 'sheet',
    scene: 'sheets',
    chrome: { productFrame: false, workspaceCard: false, cockpit: false, stageBranch: null, projectSlot: null, tab: false },
    toolbar: { ribbon: false, home: null, quick: null },
    rails: { left: 'none', right: 'none', dock: null },
    groundMaterial: { layerAccent: null, solarStrings: false },
    commandLine: false,
    authoring: false,
    versions: 'none',
    conversations: { scope: null },
    integrations: null,
    builds: { routes: [] },
    contextMenu: [],
    shortcuts: null,
    entitlements: null,
    resetOn: null,
    a11y: null,
    tourAnchors: null,
  },
}

const SURFACE_IDS = ['browser', 'cad', 'solar', 'ios', 'sheets']

// The four the STUDIO hosts (`scene: 'app'`). Kept separate from SURFACE_IDS
// so a row about the console cannot silently start asserting about a surface
// the console never renders.
const STUDIO_SURFACE_IDS = ['browser', 'cad', 'solar', 'ios']

const CONTRACT_KEYS = [
  'ground', 'scene', 'chrome', 'toolbar', 'rails', 'groundMaterial', 'commandLine', 'authoring',
  'versions', 'conversations', 'integrations', 'builds', 'contextMenu', 'shortcuts',
  'entitlements', 'resetOn', 'a11y', 'tourAnchors',
]

const ENUMS = {
  // 'sheet' is slice 5b's addition: a chrome-free public page is none of the
  // three studio grounds, and SurfaceGrounds derives its drawing set from this
  // value, so a fourth kind excludes sheets there by construction.
  ground: ['drawing', 'board', 'device-stage', 'sheet'],
  scene: ['app', 'sheets'],
  // null joins the stage arms: a surface the stage never casts declares no arm
  // rather than being forced into the frame fallthrough. Loosening this enum
  // does not loosen the pin on the four studio surfaces: surfaceGates.test.js
  // still asserts each one's stageBranch against its OLD three-arm ternary
  // literal (OLD.stageBranch), so null is reachable only through sheets.
  stageBranch: ['cad', 'ios', 'frame', null],
  projectSlot: ['ios-surface', null],
  left: ['spine', 'nav', 'none'],
  right: ['job-spine', 'job-rail', 'none'],
  versions: ['drawing', 'none', null],
  scope: ['project', 'drawing', null],
  layerAccent: ['solar', null],
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
        .toEqual(['cockpit', 'productFrame', 'projectSlot', 'stageBranch', 'tab', 'workspaceCard'])
      expect(Object.keys(contract.toolbar).sort()).toEqual(['home', 'quick', 'ribbon'])
      expect(Object.keys(contract.rails).sort()).toEqual(['dock', 'left', 'right'])
      expect(Object.keys(contract.groundMaterial).sort()).toEqual(['layerAccent', 'solarStrings'])
      expect(Object.keys(contract.conversations)).toEqual(['scope'])
      expect(Object.keys(contract.builds)).toEqual(['routes'])
    })

    it(`${id} carries only legal enum values and honest types`, () => {
      const c = surfaceContract(id)
      expect(ENUMS.ground).toContain(c.ground)
      expect(ENUMS.scene).toContain(c.scene)
      expect(ENUMS.stageBranch).toContain(c.chrome.stageBranch)
      expect(ENUMS.projectSlot).toContain(c.chrome.projectSlot)
      expect(ENUMS.left).toContain(c.rails.left)
      expect(ENUMS.right).toContain(c.rails.right)
      expect(ENUMS.versions).toContain(c.versions)
      expect(ENUMS.scope).toContain(c.conversations.scope)
      expect(ENUMS.layerAccent).toContain(c.groundMaterial.layerAccent)
      expect(typeof c.groundMaterial.solarStrings).toBe('boolean')
      for (const flag of [c.chrome.productFrame, c.chrome.workspaceCard, c.chrome.cockpit,
        c.chrome.tab, c.toolbar.ribbon, c.commandLine]) {
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

  // Slice 2 nit: deepFreeze used to short-circuit on Object.isFrozen(value),
  // which made "deep" mean "down to the first frozen node". PRODUCT_SURFACES
  // nests Object.freeze'd (SHALLOW-frozen) literals, so the trap was one edit
  // away from leaving a live contract slot writable.
  describe('deepFreeze', () => {
    it('recurses THROUGH an already-frozen node into its mutable children', () => {
      // The exact shape the old short-circuit skipped: a shallow-frozen parent
      // whose child object is still writable.
      const child = { slot: 'live' }
      const parent = Object.freeze({ child })
      expect(Object.isFrozen(parent)).toBe(true)
      expect(Object.isFrozen(child)).toBe(false) // shallow freeze, as shipped

      deepFreeze(parent)

      expect(Object.isFrozen(child)).toBe(true)
      // ESM is strict mode, so a write to a frozen slot THROWS rather than
      // silently no-opping. Either way the value must survive.
      expect(() => { child.slot = 'mutated' }).toThrow(TypeError)
      expect(child.slot).toBe('live')
    })

    it('freezes arrays and nested arrays, not just plain objects', () => {
      const tree = { rails: { dock: ['layers', 'plan'] } }
      deepFreeze(tree)
      expect(Object.isFrozen(tree.rails)).toBe(true)
      expect(Object.isFrozen(tree.rails.dock)).toBe(true)
    })

    it('terminates on a cyclic tree (the WeakSet, not the frozen bit, guards)', () => {
      // Dropping the isFrozen short-circuit also dropped the accidental cycle
      // guard it provided. Without the WeakSet this recurses forever.
      const a = { name: 'a' }
      const b = { name: 'b', a }
      a.b = b
      expect(() => deepFreeze(a)).not.toThrow()
      expect(Object.isFrozen(a)).toBe(true)
      expect(Object.isFrozen(b)).toBe(true)
    })

    it('passes null and primitives through untouched and never throws', () => {
      expect(deepFreeze(null)).toBe(null)
      expect(deepFreeze(7)).toBe(7)
      expect(deepFreeze('draw')).toBe('draw')
      expect(deepFreeze(undefined)).toBe(undefined)
    })
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

// ---------------------------------------------------------------------------
// SHEETS, the fifth surface (standardization slice 5b).
//
// The claim this slice makes is "zero UI change": a fifth manifest row that
// today renders nothing new anywhere. These rows are what turn that into a
// test. They are deliberately NOT written against the module's own values:
// each one names the code site the value was read from.
// ---------------------------------------------------------------------------
describe('Surface Contract, sheets is the fifth surface, and today it renders nothing new', () => {
  it('ships a record with honest copy read off SheetsPage, not invented', () => {
    const sheets = productSurface('sheets')
    expect(sheets.id).toBe('sheets')
    expect(sheets.label).toBe('Sheets')
    // SheetsPage.jsx:1-2 names the set and its seven anchor ids; :16 is the
    // /sheets/<code> deep link. The copy may say only what those lines say.
    expect(sheets.title).toContain('Website Sheets drawing set')
    for (const code of ['l-000', 'g-000', 'a-101', 'a-102', 'e-401', 'c-201', 's-501']) {
      expect(sheets.description).toContain(code)
    }
    expect(sheets.description).toContain('/sheets/')
    // Same record shape as the four: a fifth surface must not grow a field.
    expect(Object.keys(sheets).sort())
      .toEqual(['contract', 'description', 'eyebrow', 'familyIds', 'id', 'label', 'title'])
    // familyIds [] is a DECLARED empty fold. null would mean "the whole
    // catalog", which is what iOS and CAD say, and would be false here.
    expect(sheets.familyIds).toEqual([])
  })

  it('declares a chrome-free contract: every chrome slot false or undeclared', () => {
    const c = surfaceContract('sheets')
    // SiteRoot.jsx:234-237 wraps <SheetsPage/> in <Suspense> and nothing else.
    expect(c.chrome.productFrame).toBe(false)
    expect(c.chrome.workspaceCard).toBe(false)
    expect(c.chrome.cockpit).toBe(false)
    expect(c.chrome.stageBranch).toBe(null)
    expect(c.chrome.projectSlot).toBe(null)
    expect(c.toolbar).toEqual({ ribbon: false, home: null, quick: null })
    expect(c.rails).toEqual({ left: 'none', right: 'none', dock: null })
    expect(c.commandLine).toBe(false)
    expect(c.authoring).toBe(false)
    expect(c.versions).toBe('none')
    expect(c.conversations.scope).toBe(null)
    expect(c.builds.routes).toEqual([])
    expect(c.groundMaterial).toEqual({ layerAccent: null, solarStrings: false })
    expect(c.contextMenu).toEqual([])
  })

  it('the scene slot says which SiteRoot arm hosts each surface', () => {
    // SiteRoot.jsx:234-237 is the sheets arm; :228-232 is the console arm that
    // hosts all four studio surfaces.
    expect(surfaceContract('sheets').scene).toBe('sheets')
    for (const id of STUDIO_SURFACE_IDS) expect(surfaceContract(id).scene).toBe('app')
    // Not vacuous: the slot actually separates the two arms.
    expect(new Set(SURFACE_IDS.map((id) => surfaceContract(id).scene)).size).toBe(2)
  })

  it('chrome.tab hides sheets from the band, and is the ONLY thing that does', () => {
    expect(surfaceContract('sheets').chrome.tab).toBe(false)
    for (const id of STUDIO_SURFACE_IDS) expect(surfaceContract(id).chrome.tab).toBe(true)
    // Flipping this one value is the whole config change the operator rule
    // asks for ("everything needs to be ABLE to be there"): nothing else in
    // the record gates the tab.
    expect(PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab).map(({ id }) => id))
      .toEqual(STUDIO_SURFACE_IDS)
  })

  it("sheets is excluded from the drawing ground BY CONSTRUCTION, not by a list", () => {
    // SurfaceGrounds.jsx:110-112 derives DRAWING_SURFACES from
    // `contract.ground === 'drawing'`, so a fourth ground kind cannot leak in.
    expect(surfaceGround('sheets')).toBe('sheet')
    expect(groundShowsDrawing('sheets')).toBe(false)
    // ...and it lights up neither of the other two grounds either.
    expect(surfaceGround('sheets') === 'board').toBe(false)
    expect(surfaceGround('sheets') === 'device-stage').toBe(false)
    // The drawing set is still exactly cad and solar with a fifth row present.
    expect(SURFACE_IDS.filter(groundShowsDrawing)).toEqual(['cad', 'solar'])
    // The file itself must keep deriving it rather than re-growing a literal.
    const grounds = readFileSync(`${process.cwd()}/src/site/SurfaceGrounds.jsx`, 'utf8')
    expect(grounds).toContain("contract?.ground === 'drawing'")
    // The set is DERIVED, never re-declared as a literal membership list.
    // (The file's comment quotes the old literal to say what it replaced, so
    // the probe targets the assignment, not any mention of the old text.)
    expect(grounds).not.toMatch(/DRAWING_SURFACES\s*=\s*new Set\(\s*\[/)
  })

  it('scene has no other source of truth: SiteRoot.jsx and routeScene.js still name the ids the contract declares', () => {
    // `scene` has no derivation to probe (unlike `ground`, it stays a literal
    // switch in SiteRoot until a later slice repoints it), so the pin here is
    // that the two literal ids ENUMS.scene declares are still the ones the
    // arm and the router actually name, not that any structure derives them.
    const siteRoot = readFileSync(`${process.cwd()}/src/site/SiteRoot.jsx`, 'utf8')
    expect(siteRoot).toContain("scene === 'sheets'")
    const routeScene = readFileSync(`${process.cwd()}/src/site/routeScene.js`, 'utf8')
    for (const id of ENUMS.scene) {
      expect(routeScene).toContain(`return '${id}'`)
    }
  })
})
