export const DEFAULT_PRODUCT_SURFACE = 'cad'

export const SHARED_WORKSPACE_CAPABILITIES = Object.freeze([
  'conversation',
  'annotations',
  'authoring',
  'approvals',
  'versions',
  'receipts',
  'marathons',
  'one-shot execution',
])

// ---------------------------------------------------------------------------
// THE SURFACE CONTRACT (standardization slice 1, docs/convergence/
// SURFACE-CONTRACT.md). Every slot a surface can own, declared as DATA on the
// surface record instead of hidden in an inline gate. This slice FREEZES
// TODAY: every value below is read off the code site cited beside it, so the
// manifest and the running shell agree byte for byte. NO CONSUMER READS THIS
// YET — slice 2 repoints the gates — so this slice cannot change a pixel.
//
// Operator rule this exists to serve:
//   "nothing HAS to be there, but everything needs to be ABLE to be there,
//    according to agent/operator decisions, effortlessly as a key/staple
//    functionality"
//
// `null` means UNDECLARED: the code has no per-surface answer today and a
// later slice fills it. It never means "off" and it is never a guess.
//
// Plain frozen data + pure functions on purpose: no React, no functions
// inside a contract object, so the manifest stays testable and diffable.
// ---------------------------------------------------------------------------

// Freezes the whole tree, not just the top object, so a consumer cannot
// mutate a nested slot at runtime. Arrays are frozen too; null/primitives
// pass through untouched. Fails closed on nothing: it never throws.
function deepFreeze(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const key of Object.keys(value)) deepFreeze(value[key])
  return value
}

// Per-surface projection of the ONE tenant capability catalog (F-7).
// `familyIds` filters the live folded families each surface features in its
// frame; null means the surface presents the whole catalog (iOS ships the
// full tenant tool set). This is presentation-level focus, never a second
// scoping model: the server catalog is tenant-scoped only, and every surface
// reads the same fold (GET /api/capabilities; CONTRACT-ADDENDUM §25).
export const PRODUCT_SURFACES = Object.freeze([
  Object.freeze({
    id: 'browser',
    label: 'Browser',
    eyebrow: 'Blank slate',
    title: 'Build from an open project workspace',
    description: 'Shape files, conversations, annotations, tools, and automations without leaving the project.',
    familyIds: Object.freeze(['custom', 'measurement', 'selection']),
    contract: deepFreeze({
      // ground: SurfaceGrounds.jsx:99 DRAWING_SURFACES = new Set(['cad','solar'])
      //   excludes browser; e2e/local/one-shell-mount.spec.mjs:131 names it
      //   "the project board for Browser".
      ground: 'board',
      chrome: {
        // productFrame: App.jsx:2798 `activeSurface !== 'cad'` -> true.
        //   (The stage agrees: ToolCast.jsx:2122 renders the frame here too.)
        productFrame: true,
        // workspaceCard: App.jsx:2819 display gate `cad || solar` -> hidden.
        workspaceCard: false,
        // cockpit: App.jsx:2242 drafting = groundShowsDrawing(activeSurface);
        //   the cockpit bands mount under `studioGround && drafting`
        //   (App.jsx:2526, 2858) -> false. Declared separately from `ground`
        //   on purpose: every slot is declarable per surface.
        cockpit: false,
        // stageBranch: ToolCast.jsx:1417 / 2060 ternary falls through to the
        //   frame branch (ToolCast.jsx:2122).
        stageBranch: 'frame',
        // projectSlot: App.jsx:2806 fills the slot for 'ios' only.
        projectSlot: null,
      },
      toolbar: {
        // ribbon: App.jsx:2858 `studioGround && drafting` -> no DraftingRibbon.
        ribbon: false,
        // home: no ribbon here, so no home tab is declared. (App.jsx:2225
        //   useState('draw') is console-global state, not a per-surface value;
        //   CockpitTopBand.jsx:17 RIBBON_TABS[0] is 'draw'.)
        home: null,
        // quick: CockpitTopBand.jsx takes `before`/`after` as PROPS built in
        //   App.jsx:2417-2432 — code, not data — so there is no data source
        //   to read ids from. Slice 3 promotes them to a registry.
        quick: null,
      },
      rails: {
        // left: App.jsx:2247 navSpine = studioGround && drafting && !navExpanded
        //   && wideViewport -> false here, so the full nav rail renders
        //   (App.jsx:2609). Wide-viewport default; see the doc.
        left: 'nav',
        // right: App.jsx:3414 JobRail spine prop -> false, so the expanded
        //   job rail renders (App.jsx:3402). Wide-viewport default.
        right: 'job-rail',
        // dock: PropertiesDock mounts only under `studioGround && drafting
        //   && wideViewport` (App.jsx:3148) — nothing declares a board dock.
        dock: null,
      },
      // commandLine: App.jsx:3376 PromptBox commandLine={!!studioGround && drafting}.
      commandLine: false,
      // authoring: App.jsx:2699 AuthorPanel sits in the nav rail, which is not
      //   surface-gated (App.jsx:2609, 2617). (Stage divergence: the build lane
      //   is cad-only, ToolCast.jsx:1417/1506.)
      authoring: true,
      // versions: App.jsx:3001 VersionHistory lives inside the workspace card,
      //   whose display gate (App.jsx:2819) hides it off cad/solar.
      versions: 'none',
      conversations: {
        // scope: converse.js:129-134 caches ONE session per project+drawing pair
        //   ('default' when there is no drawing); ConversePanel (App.jsx:3263)
        //   is not surface-gated, so the scope is the same on every surface.
        scope: 'drawing',
      },
      // integrations: the only link surface today is the header's Claude
      //   account panel (App.jsx:2596), which is global, not per-surface.
      integrations: null,
      builds: {
        // routes: the nav rail's ToolsPanel run path (App.jsx:2674 ->
        //   onRequestCatalogRun) is mounted on every surface; no marathon
        //   route exists in this client at all.
        routes: ['one-shot'],
      },
      // contextMenu: zero contextmenu handlers exist under web/src (grep,
      //   2026-09-03) — declared empty, not undeclared.
      contextMenu: [],
      // shortcuts: no per-surface shortcut registry exists.
      shortcuts: null,
      // entitlements: EntitlementGate.jsx:15 ROWS are TIER capability keys
      //   (run_read / run_write / build / converse), never per-surface.
      entitlements: null,
      // resetOn: no effect in App.jsx keys off activeSurface to reset scope.
      resetOn: null,
      // a11y: no per-surface a11y declaration exists.
      a11y: null,
      // tourAnchors: the tour is mock-gated (App.jsx:2722), not surface data.
      tourAnchors: null,
    }),
  }),
  Object.freeze({
    id: 'cad',
    label: 'CAD',
    eyebrow: 'Drawing workspace',
    title: 'Work directly with a project drawing',
    description: 'Use the live drawing, layers, tools, approvals, jobs, versions, and receipts in one scene.',
    familyIds: null,
    contract: deepFreeze({
      // ground: SurfaceGrounds.jsx:99 DRAWING_SURFACES includes 'cad'.
      ground: 'drawing',
      chrome: {
        // productFrame: App.jsx:2798 `activeSurface !== 'cad'` -> false.
        productFrame: false,
        // workspaceCard: App.jsx:2819 `cad || solar` -> shown.
        workspaceCard: true,
        // cockpit: App.jsx:2242 drafting -> true (App.jsx:2526, 2858 mount).
        cockpit: true,
        // stageBranch: ToolCast.jsx:1417 `activeSurface === 'cad'`.
        stageBranch: 'cad',
        projectSlot: null,
      },
      toolbar: {
        // ribbon: App.jsx:2858 `studioGround && drafting` -> DraftingRibbon mounts.
        ribbon: true,
        // home: App.jsx:2225 useState('draw'); CockpitTopBand.jsx:18 tab id 'draw'.
        home: 'draw',
        // quick: props, not data (App.jsx:2417-2432). See browser's note.
        quick: null,
      },
      rails: {
        // left: App.jsx:2247 navSpine true at the wide-viewport default
        //   (navExpanded starts false, App.jsx:2216).
        left: 'spine',
        // right: App.jsx:3414 spine true at the wide-viewport default
        //   (jobRailExpanded starts false, App.jsx:2222).
        right: 'job-spine',
        // dock: App.jsx:3148 mounts PropertiesDock; sections in the order
        //   PropertiesDock.jsx:132-142 renders them (drawing and plan are
        //   conditional on their props, both supplied here at App.jsx:3153).
        dock: ['layers', 'drawing', 'selection', 'plan'],
      },
      // commandLine: App.jsx:3376 -> true.
      commandLine: true,
      // authoring: App.jsx:2699 AuthorPanel (reachable from the ribbon's
      //   author cluster, App.jsx:2396, while the rail is a spine).
      authoring: true,
      // versions: App.jsx:3001 VersionHistory inside the shown workspace card.
      versions: 'drawing',
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      builds: { routes: ['one-shot'] }, // App.jsx:2674 catalog run path
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      tourAnchors: null,
    }),
  }),
  Object.freeze({
    id: 'solar',
    label: 'Solar CAD',
    eyebrow: 'LEAF template',
    title: 'Apply the LEAF solar tool set',
    description: 'Start from a versioned solar template with standards, catalog tools, and project-owned versions.',
    familyIds: Object.freeze(['stringing', 'placement']),
    contract: deepFreeze({
      // ground: SurfaceGrounds.jsx:99 DRAWING_SURFACES includes 'solar'.
      ground: 'drawing',
      chrome: {
        // productFrame: App.jsx:2798 `activeSurface !== 'cad'` -> TRUE on solar.
        //   The frame renders over the shown workspace card; this is today's
        //   behaviour, not a defect this slice may fix.
        productFrame: true,
        // workspaceCard: App.jsx:2819 `cad || solar` -> shown.
        workspaceCard: true,
        // cockpit: App.jsx:2242 drafting -> true.
        cockpit: true,
        // stageBranch: ToolCast.jsx:1417/2060 fall through to the frame branch
        //   (ToolCast.jsx:2122) — the stage has NO drafting cockpit for solar.
        stageBranch: 'frame',
        projectSlot: null,
      },
      toolbar: {
        ribbon: true, // App.jsx:2858
        home: 'draw', // App.jsx:2225
        quick: null,
      },
      rails: {
        left: 'spine', // App.jsx:2247
        right: 'job-spine', // App.jsx:3414
        dock: ['layers', 'drawing', 'selection', 'plan'], // App.jsx:3148, PropertiesDock.jsx:132-142
      },
      commandLine: true, // App.jsx:3376
      authoring: true, // App.jsx:2699
      versions: 'drawing', // App.jsx:3001 inside the shown card (App.jsx:2819)
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      builds: { routes: ['one-shot'] }, // App.jsx:2674
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      tourAnchors: null,
    }),
  }),
  Object.freeze({
    id: 'ios',
    label: 'iOS',
    eyebrow: 'One-shot ship lane',
    title: 'Turn an approved project revision into a TestFlight build',
    description: 'Use mounted Apple readiness and resumable ship receipts. Credentials never enter this browser.',
    familyIds: null,
    contract: deepFreeze({
      // ground: SurfaceGrounds.jsx:99 excludes ios; one-shell-mount.spec.mjs:131
      //   names it "the device stage for iOS" (DeviceGround, SurfaceGrounds.jsx:213).
      ground: 'device-stage',
      chrome: {
        // productFrame: App.jsx:2798 -> true (with the iOS project slot).
        //   Stage divergence: ToolCast.jsx:2060 gives ios its own rail instead.
        productFrame: true,
        // workspaceCard: App.jsx:2819 -> hidden.
        workspaceCard: false,
        // cockpit: App.jsx:2242 drafting -> false.
        cockpit: false,
        // stageBranch: ToolCast.jsx:2060 `activeSurface === 'ios'`.
        stageBranch: 'ios',
        // projectSlot: App.jsx:2806-2807 mounts <IosSurface> into the frame.
        projectSlot: 'ios-surface',
      },
      toolbar: {
        ribbon: false, // App.jsx:2858
        home: null,
        quick: null,
      },
      rails: {
        left: 'nav', // App.jsx:2247 navSpine false
        right: 'job-rail', // App.jsx:3414 spine false
        dock: null, // App.jsx:3148 drafting-only
      },
      commandLine: false, // App.jsx:3376
      authoring: true, // App.jsx:2699 (the nav rail is not surface-gated)
      versions: 'none', // App.jsx:3001 inside the hidden card (App.jsx:2819)
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      builds: {
        // routes: the console mounts IosSurface, which is props-only and
        //   carries NO launch control (IosSurface.jsx:3-4 "no fetch, no
        //   polling, no client-side state math"); the only ship-lane launch
        //   in the repo is the STAGE's (ToolCast.jsx:2097
        //   data-testid="ios-ship-launch"). Recorded as a divergence in the
        //   doc, never invented into the console contract.
        routes: ['one-shot'],
      },
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      tourAnchors: null,
    }),
  }),
])

const SURFACE_IDS = new Set(PRODUCT_SURFACES.map(({ id }) => id))

export function normalizeProductSurface(value) {
  return SURFACE_IDS.has(value) ? value : DEFAULT_PRODUCT_SURFACE
}

export function productSurfaceFromSearch(search = '') {
  try {
    return normalizeProductSurface(new URLSearchParams(search).get('surface'))
  } catch {
    return DEFAULT_PRODUCT_SURFACE
  }
}

export function searchForProductSurface(search, surfaceId) {
  const params = new URLSearchParams(search || '')
  params.set('surface', normalizeProductSurface(surfaceId))
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

export function productSurfaceStates({ sessionActive, hasDrawing, apsLive, iosReady = false } = {}) {
  return {
    browser: sessionActive
      ? { state: 'available', label: 'Ready' }
      : { state: 'sign-in', label: 'Sign in' },
    cad: !sessionActive
      ? { state: 'sign-in', label: 'Sign in' }
      : !hasDrawing
        ? { state: 'setup', label: 'Add drawing' }
        : apsLive === false
          ? { state: 'unavailable', label: 'Execution paused' }
          : { state: 'available', label: 'Ready' },
    solar: !sessionActive
      ? { state: 'sign-in', label: 'Sign in' }
      : { state: 'beta', label: hasDrawing ? 'Beta' : 'Template pending' },
    ios: iosReady
      ? { state: 'available', label: 'Ready' }
      : { state: 'setup', label: 'Setup required' },
  }
}

export function productSurface(id) {
  const normalized = normalizeProductSurface(id)
  return PRODUCT_SURFACES.find((surface) => surface.id === normalized)
}

/**
 * The Surface Contract for a surface. Normalizes exactly like
 * productSurface(), so an unknown id falls closed to the CAD contract rather
 * than returning undefined into a consumer's slot lookup.
 */
export function surfaceContract(id) {
  return productSurface(id).contract
}

/**
 * The declared ground kind: 'drawing' | 'board' | 'device-stage'. Equal to
 * groundShowsDrawing(id) === (surfaceGround(id) === 'drawing') by
 * construction today; productSurfaces.test.js pins that equality.
 */
export function surfaceGround(id) {
  return surfaceContract(id).ground
}
