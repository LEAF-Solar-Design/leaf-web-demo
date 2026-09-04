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
// THE SURFACE CONTRACT (standardization slices 1-2, docs/convergence/
// SURFACE-CONTRACT.md). Every slot a surface can own, declared as DATA on the
// surface record instead of hidden in an inline gate. Slice 1 FROZE TODAY:
// every value below is read off the code site cited beside it, so the
// manifest and the running shell agree byte for byte.
//
// Slice 2 made this manifest LOAD-BEARING: App.jsx, ToolCast.jsx and
// SurfaceGrounds.jsx now read these slots instead of comparing activeSurface
// to a string literal, so a value below is what the shell renders, not a
// description of it. Behaviour is unchanged, because surfaceGates.test.js pins every
// derived gate equal to the literal predicate it replaced, for all four ids,
// and the three console/stage divergences (D1 ios chrome, D2 ios builds,
// D3 authoring) are PRESERVED as documented. Editing a value below now
// changes the product, which is exactly how the ONE value that has since
// been changed on purpose was changed: solar's chrome.productFrame, flipped
// true -> false by the P1 studio-shell pass (see its note on the solar
// record). Slice 2 preserved that quirk and left the tripwire; the P1 pass
// fired it deliberately and rewrote the tripwire to record the divergence.
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
//
// Slice 2 correction: it RECURSES INTO ALREADY-FROZEN NODES. The slice-1
// version short-circuited on `Object.isFrozen(value)`, which is wrong the
// moment a caller nests an `Object.freeze`d literal (a shallow freeze) inside
// the tree, so the frozen parent was skipped and every child under it stayed
// writable, so "deep" quietly meant "down to the first frozen node".
// PRODUCT_SURFACES does exactly that nesting one level up, so the trap was
// one edit away. The cycle guard is now a WeakSet of visited nodes rather
// than the frozen bit, so a self-referential tree still terminates.
// Exported for its own test; it is a pure utility, never a contract value.
export function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object' || seen.has(value)) return value
  seen.add(value)
  Object.freeze(value)
  for (const key of Object.keys(value)) deepFreeze(value[key], seen)
  return value
}

// ---------------------------------------------------------------------------
// TOUR ANCHORS (standardization slice 4b). Per shell, a map from a guided-tour
// STEP id to the `data-tour` id of the element that step spotlights.
// DemoTour resolves `[data-tour="<id>"]` first and falls back to the step's
// own className chain, so the step arrays (src/demo/tourScript.js TOUR_STEPS,
// site/ToolCast.jsx UNIFIED_TOUR_STEPS) are byte-identical to before; the
// contract carries the anchor, not the script. Both shells expose the SAME
// anchor vocabulary on their own elements:
//
//   shell        the scene root: App.jsx `.app` / StageScene.jsx `.stage-root`
//   viewer       the drawing: Viewer.jsx `.viewer-canvas` / StageLayer.jsx
//                `.stage-viewer` (which wraps the shared Viewer; tree order
//                makes the outer one win on the stage)
//   command-bar  PromptBox.jsx `.bar` / ToolCast.jsx `.tc-bar`
//   right-rail   ToolCast.jsx `.tc-rail-r` only (the console's own job rail,
//                JobRail.jsx `aside.rail`, mounts no step that spotlights it,
//                so it carries no anchor of its own; `.tc-rail-r` wraps the
//                shared JobRail on the stage, tree order making the wrapper
//                win there too)
//
// The rule an anchored step obeys: its `data-tour` sits on the element its
// className chain resolves to on THAT shell, which the oracle pins at the
// source level (the tag carrying the anchor names one of the chain's classes;
// check 8 in check_tour_anchors.mjs). Chains that pick among candidates by
// presence are anchored only where the shell renders exactly one of them
// (`.viewer-canvas, .workspace-card` on the console viewer; `.bar, .bar-dock,
// .workspace-card` on the console command bar); a chain whose winner varies
// per surface (`.strip-decision, .bar-dock`, `.author-section, .workspace-card`,
// `.converse-confirm, .tc-operator-rail`) stays a chain and is absent from the map. A
// `left-rail` anchor existed through slice 4b's first cut (NavRail.jsx
// `aside.nav`, ToolCast.jsx `.tc-operator-rail`) but named no such step on
// either shell — the console never spotlights the nav rail at all, and the
// stage's only rail-adjacent step (`approval`) is exactly the multi-candidate
// chain above — so the attribute was dead vocabulary and was removed rather
// than kept as a reservation; see check_tour_anchors.mjs's orphan check.
//
// scripts/check_tour_anchors.mjs is the gate: every mapped step id exists in
// that shell's step array, every anchor id is one of the declared ones above,
// every anchor id is present as `data-tour="<id>"` in that shell's source,
// and every `data-tour` id a shell's source carries is referenced by some
// step map (the reverse direction, so a leftover or mistyped attribute with
// no consumer fails loudly instead of sitting unused).
// ---------------------------------------------------------------------------
// The console tour (App.jsx, mock-gated, mounts on every studio surface).
const CONSOLE_TOUR_ANCHORS = Object.freeze({
  welcome: 'shell',
  viewer: 'viewer',
  count: 'command-bar',
  edge: 'command-bar',
  measure: 'command-bar',
})
// The stage walk (ToolCast.jsx UNIFIED_TOUR_STEPS), mounted inside the cad
// arm only (ToolCast.jsx `stageBranch === 'cad'`), so cad is the one surface
// that declares it; browser, solar and ios declare null on the stage.
const STAGE_TOUR_ANCHORS = Object.freeze({
  welcome: 'shell',
  viewer: 'viewer',
  request: 'command-bar',
  versions: 'right-rail',
  trust: 'right-rail',
})

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
      // ground: SurfaceGrounds.jsx:106 DRAWING_SURFACES = new Set(['cad','solar'])
      //   excludes browser; e2e/local/one-shell-mount.spec.mjs:131 names it
      //   "the project board for Browser".
      ground: 'board',
      // scene: SiteRoot.jsx:228-232 mounts <App/> for scene 'app'; this
      //   surface is one of the studio's tabs, so the arm that hosts it is
      //   the console. (Slice 5b added the slot; see the sheets record.)
      scene: 'app',
      chrome: {
        // productFrame: App.jsx:2838 `activeSurface !== 'cad'` -> true.
        //   (The stage agrees: ToolCast.jsx:2139 renders the frame here too.)
        productFrame: true,
        // workspaceCard: App.jsx:2859 display gate `cad || solar` -> hidden.
        workspaceCard: false,
        // cockpit: App.jsx:2269 drafting = groundShowsDrawing(activeSurface);
        //   the cockpit bands mount under `studioGround && drafting`
        //   (App.jsx:2562, 2898) -> false. Declared separately from `ground`
        //   on purpose: every slot is declarable per surface.
        cockpit: false,
        // stageBranch: ToolCast.jsx:1434 / 2077 ternary falls through to the
        //   frame branch (ToolCast.jsx:2139).
        stageBranch: 'frame',
        // projectSlot: App.jsx:2846 fills the slot for 'ios' only.
        projectSlot: null,
        // tab: ProductSurfaceTabs.jsx:72 renders one tab per record whose
        //   contract declares `chrome.tab`. Slice 5b added the slot and the
        //   filter together; the four studio surfaces all declared `true`, so
        //   the band is byte-identical to the un-filtered map it replaced.
        tab: true,
      },
      toolbar: {
        // ribbon: App.jsx:2898 `studioGround && drafting` -> no DraftingRibbon.
        ribbon: false,
        // home: no ribbon here, so no home tab is declared. (App.jsx:2234
        //   useState('draw') is console-global state, not a per-surface value;
        //   CockpitTopBand.jsx:17 RIBBON_TABS[0] is 'draw'.)
        home: null,
        // quick: CockpitTopBand.jsx takes `before`/`after` as PROPS built in
        //   App.jsx:2453-2468 — code, not data — so there is no data source
        //   to read ids from. Slice 3 promotes them to a registry.
        quick: null,
      },
      rails: {
        // left: App.jsx:2281 navSpine = studioGround && drafting && !navExpanded
        //   && wideViewport -> false here, so the full nav rail renders
        //   (App.jsx:2645). Wide-viewport default; see the doc.
        left: 'nav',
        // right: App.jsx:3460 JobRail spine prop -> false, so the expanded
        //   job rail renders (App.jsx:3445). Wide-viewport default.
        right: 'job-rail',
        // dock: PropertiesDock mounts only under `studioGround && drafting
        //   && wideViewport` (App.jsx:3191) — nothing declares a board dock.
        //   `paneOpen` (App.jsx:2241, default true) is the dock's SECOND gate
        //   inside that branch: the user closes the pane from its own title
        //   row and the View tab reopens it, so a null dock and a closed pane
        //   render the same nothing for different reasons.
        dock: null,
      },
      // groundMaterial: the ground's per-surface material overrides. Both
      // fields are the SURFACE term of their gate only; the mock / fixture /
      // sample terms beside them are honesty gates, not surface data.
      //   layerAccent: App.jsx:2296 `studioGround && activeSurface === 'solar'`
      //     recolours the Panels layer. Every other surface returns the SAME
      //     colorForLayer reference, which is what keeps the old shell's
      //     canvas bytes untouched.
      //   solarStrings: App.jsx:2313 solarStringsEligible.
      groundMaterial: { layerAccent: null, solarStrings: false },
      // commandLine: App.jsx:3419 PromptBox commandLine={!!studioGround && drafting}.
      commandLine: false,
      // authoring: App.jsx:2735 AuthorPanel sits in the nav rail, which is not
      //   surface-gated (App.jsx:2645, 2653). (Stage divergence: the build lane
      //   is cad-only, ToolCast.jsx:1434/1523.)
      authoring: true,
      // versions: App.jsx:3041 VersionHistory lives inside the workspace card,
      //   whose display gate (App.jsx:2859) hides it off cad/solar.
      versions: 'none',
      conversations: {
        // scope: converse.js:129-134 caches ONE session per project+drawing pair
        //   ('default' when there is no drawing); ConversePanel (App.jsx:3306)
        //   is not surface-gated, so the scope is the same on every surface.
        scope: 'drawing',
      },
      // integrations: the only link surface today is the header's Claude
      //   account panel (App.jsx:2630), which is global, not per-surface.
      integrations: null,
      builds: {
        // routes: the nav rail's ToolsPanel run path (App.jsx:2710 ->
        //   onRequestCatalogRun) is mounted on every surface; no marathon
        //   route exists in this client at all.
        routes: ['one-shot'],
        // card: slice 11a. WHERE the BuildQueueCard is hosted: the job
        //   monitor (components/JobRail.jsx) renders one card per record on
        //   every studio surface, expanded here (rails.right 'job-rail').
        //   The toolbar badge (SurfaceFrame.Builds) reads this slot too.
        card: 'job-rail',
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
      // tourAnchors: the console tour mounts on every studio surface
      //   (App.jsx, mock-gated); the stage walk lives in the cad arm only, so
      //   the stage declares no tour here. See CONSOLE_TOUR_ANCHORS above.
      tourAnchors: { console: CONSOLE_TOUR_ANCHORS, stage: null },
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
      // ground: SurfaceGrounds.jsx:106 DRAWING_SURFACES includes 'cad'.
      ground: 'drawing',
      scene: 'app', // SiteRoot.jsx:228-232, a studio tab
      chrome: {
        // productFrame: App.jsx:2838 `activeSurface !== 'cad'` -> false.
        productFrame: false,
        // workspaceCard: App.jsx:2859 `cad || solar` -> shown.
        workspaceCard: true,
        // cockpit: App.jsx:2269 drafting -> true (App.jsx:2562, 2898 mount).
        cockpit: true,
        // stageBranch: ToolCast.jsx:1434 `activeSurface === 'cad'`.
        stageBranch: 'cad',
        projectSlot: null,
        tab: true, // ProductSurfaceTabs.jsx:72
      },
      toolbar: {
        // ribbon: App.jsx:2898 `studioGround && drafting` -> DraftingRibbon mounts.
        ribbon: true,
        // home: App.jsx:2234 useState('draw'); CockpitTopBand.jsx:18 tab id 'draw'.
        home: 'draw',
        // quick: props, not data (App.jsx:2453-2468). See browser's note.
        quick: null,
      },
      rails: {
        // left: App.jsx:2281 navSpine true at the wide-viewport default
        //   (navExpanded starts false, App.jsx:2217).
        left: 'spine',
        // right: App.jsx:3460 spine true at the wide-viewport default
        //   (jobRailExpanded starts false, App.jsx:2224).
        right: 'job-spine',
        // dock: App.jsx:3191 mounts PropertiesDock; sections in the order
        //   PropertiesDock.jsx:132-142 renders them (drawing and plan are
        //   conditional on their props, both supplied here at App.jsx:3196).
        //   FOUR sections is the WITH-DRAWING case: `drawing` renders only
        //   when paneDrawingFacts is non-null (App.jsx:2481 returns null until
        //   `shown` exists), so an honest-empty drafting surface shows three.
        //   `paneOpen` (App.jsx:2241, default true) is the dock's SECOND gate
        //   inside the mount branch, closed by the dock's own title row,
        //   reopened by the View tab's Properties tool.
        dock: ['layers', 'drawing', 'selection', 'plan'],
      },
      // groundMaterial: no solar accent and no string overlay off the solar
      //   surface (App.jsx:2296, :2313 both test for 'solar').
      groundMaterial: { layerAccent: null, solarStrings: false },
      // commandLine: App.jsx:3419 -> true.
      commandLine: true,
      // authoring: App.jsx:2735 AuthorPanel (reachable from the ribbon's
      //   author cluster, App.jsx:2429, while the rail is a spine).
      authoring: true,
      // versions: App.jsx:3041 VersionHistory inside the shown workspace card.
      versions: 'drawing',
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      // builds.card: slice 11a, the job monitor hosts the card (a spine here
      //   by default, rails.right 'job-spine'; the toolbar badge opens it).
      builds: { routes: ['one-shot'], card: 'job-rail' }, // App.jsx:2710 catalog run path
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      // tourAnchors: the ONE surface both tours run on: the console tour
      //   (mock-gated) and the stage walk (ToolCast.jsx, the cad arm).
      tourAnchors: { console: CONSOLE_TOUR_ANCHORS, stage: STAGE_TOUR_ANCHORS },
    }),
  }),
  Object.freeze({
    id: 'solar',
    label: 'Solar CAD',
    eyebrow: 'Leaf Automation template',
    title: 'Apply the Leaf Automation solar tool set',
    description: 'Start from a versioned solar template with standards, catalog tools, and project-owned versions.',
    familyIds: Object.freeze(['stringing', 'placement']),
    contract: deepFreeze({
      // ground: SurfaceGrounds.jsx:106 DRAWING_SURFACES includes 'solar'.
      ground: 'drawing',
      scene: 'app', // SiteRoot.jsx:228-232, a studio tab
      chrome: {
        // productFrame: FALSE — a DELIBERATE divergence from the old literal
        //   `activeSurface !== 'cad'` (App.jsx:2838), which is what the P1
        //   pass fixes. Measured at 1512x950 before the fix: the frame was a
        //   450px opaque white block at y=28..478 in the console's flow, over
        //   a studio ground that starts at y=155 — it buried the ribbon,
        //   pushed the document band from y~123 to y=585, and covered 323px
        //   of canvas. Solar IS the CAD workspace on the solar tool set
        //   (operator directive 2026-09-01, cited at App.jsx's workspace-card
        //   mount), so a surface that declares a cockpit, a ribbon and a
        //   drawing ground cannot also declare a page-sized frame over them.
        //   The slice-2 parity tripwires (surfaceGates.test.js, and the
        //   fixture in productSurfaces.test.js) are updated to RECORD this
        //   divergence, never deleted to stop them firing.
        productFrame: false,
        // workspaceCard: App.jsx:2859 `cad || solar` -> shown.
        workspaceCard: true,
        // cockpit: App.jsx:2269 drafting -> true.
        cockpit: true,
        // stageBranch: ToolCast.jsx:1434/2077 fall through to the frame branch
        //   (ToolCast.jsx:2139) — the stage has NO drafting cockpit for solar.
        stageBranch: 'frame',
        projectSlot: null,
        tab: true, // ProductSurfaceTabs.jsx:72
      },
      toolbar: {
        ribbon: true, // App.jsx:2898
        home: 'draw', // App.jsx:2234
        quick: null,
      },
      rails: {
        left: 'spine', // App.jsx:2281
        right: 'job-spine', // App.jsx:3460
        // Four sections is the with-drawing case; paneOpen (App.jsx:2241) is
        // the dock's second gate. See the cad record for the full note.
        dock: ['layers', 'drawing', 'selection', 'plan'], // App.jsx:3191, PropertiesDock.jsx:132-142
      },
      // groundMaterial: the ONE surface that carries both. The Panels layer
      //   takes the solar accent (App.jsx:2296) and the 135 bundled solved
      //   string routes are eligible here (App.jsx:2313), eligible and not
      //   shown: the mock / demo-sample / head-v1 honesty gates beside the
      //   surface term still decide, and none of them is surface data.
      groundMaterial: { layerAccent: 'solar', solarStrings: true },
      commandLine: true, // App.jsx:3419
      authoring: true, // App.jsx:2735
      versions: 'drawing', // App.jsx:3041 inside the shown card (App.jsx:2859)
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      builds: { routes: ['one-shot'], card: 'job-rail' }, // App.jsx:2710; card: slice 11a, the job monitor hosts it
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      // tourAnchors: the console tour mounts here; the stage takes the frame
      //   arm for solar (no walk), so the stage declares none.
      tourAnchors: { console: CONSOLE_TOUR_ANCHORS, stage: null },
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
      // ground: SurfaceGrounds.jsx:106 excludes ios; one-shell-mount.spec.mjs:131
      //   names it "the device stage for iOS" (DeviceGround, SurfaceGrounds.jsx:222).
      ground: 'device-stage',
      scene: 'app', // SiteRoot.jsx:228-232, a studio tab
      chrome: {
        // productFrame: App.jsx:2838 -> true (with the iOS project slot).
        //   Stage divergence: ToolCast.jsx:2077 gives ios its own rail instead.
        productFrame: true,
        // workspaceCard: App.jsx:2859 -> hidden.
        workspaceCard: false,
        // cockpit: App.jsx:2269 drafting -> false.
        cockpit: false,
        // stageBranch: ToolCast.jsx:2077 `activeSurface === 'ios'`.
        stageBranch: 'ios',
        // projectSlot: App.jsx:2846-2847 mounts <IosSurface> into the frame.
        projectSlot: 'ios-surface',
        tab: true, // ProductSurfaceTabs.jsx:72
      },
      toolbar: {
        ribbon: false, // App.jsx:2898
        home: null,
        quick: null,
      },
      rails: {
        left: 'nav', // App.jsx:2281 navSpine false
        right: 'job-rail', // App.jsx:3460 spine false
        dock: null, // App.jsx:3191 drafting-only (paneOpen is its second gate)
      },
      // groundMaterial: the device stage carries neither (App.jsx:2296, :2313).
      groundMaterial: { layerAccent: null, solarStrings: false },
      commandLine: false, // App.jsx:3419
      authoring: true, // App.jsx:2735 (the nav rail is not surface-gated)
      versions: 'none', // App.jsx:3041 inside the hidden card (App.jsx:2859)
      conversations: { scope: 'drawing' }, // converse.js:129-134
      integrations: null,
      builds: {
        // routes: the console mounts IosSurface, which is props-only and
        //   carries NO launch control (IosSurface.jsx:3-4 "no fetch, no
        //   polling, no client-side state math"); the only ship-lane launch
        //   in the repo is the STAGE's (ToolCast.jsx:2114
        //   data-testid="ios-ship-launch"). Recorded as a divergence in the
        //   doc, never invented into the console contract.
        routes: ['one-shot'],
        // card: slice 11a, the job monitor hosts the card (expanded here,
        //   rails.right 'job-rail'), on the stage's Jobs tab as well.
        card: 'job-rail',
      },
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      // tourAnchors: the console tour mounts here; the stage's ios arm has no
      //   walk, so the stage declares none.
      tourAnchors: { console: CONSOLE_TOUR_ANCHORS, stage: null },
    }),
  }),
  // -------------------------------------------------------------------------
  // SHEETS (standardization slice 5b). The fifth surface, and the first one
  // the studio does NOT host: /sheets is its own SiteRoot arm
  // (SiteRoot.jsx:234-237, routeScene.js:30), a public page with no session,
  // no drawing and no chrome of any kind. It is in the manifest because the
  // operator rule says everything must be ABLE to be there, and it ships
  // `chrome.tab: false` because today it genuinely is not: flipping that one
  // value is the whole config change, and nothing else here would move.
  //
  // Every slot below is FALSE or 'none' rather than null, because a
  // chrome-free page is a DECLARED absence, not an undeclared one. The
  // genuinely-undeclared slots stay null, exactly as on the four studio
  // records.
  // -------------------------------------------------------------------------
  Object.freeze({
    id: 'sheets',
    label: 'Sheets',
    // Copy read off the page itself, never invented: SheetsPage.jsx:1-2
    // ("renders the Website Sheets drawing set as one scrolling page", anchor
    // ids l-000 · g-000 · a-101 · a-102 · e-401 · c-201 · s-501) and
    // SheetsPage.jsx:16 (the /sheets/<code> deep link).
    eyebrow: 'Website drawing set',
    title: 'Read the Website Sheets drawing set as one scrolling page',
    description: 'Seven sheets (l-000, g-000, a-101, a-102, e-401, c-201, s-501) on one public page, each deep-linkable at /sheets/<code>.',
    // familyIds folds the API tool-family catalog (the same `catalog.families`
    //   fold every studio surface reads), which the page never reads: S501Sheet
    //   renders its own static capabilities list (sheetsContent.jsx:554-556),
    //   not a family from that catalog. So the fold here is empty: declared,
    //   not "the whole catalog" (which is what null means).
    familyIds: Object.freeze([]),
    contract: deepFreeze({
      // ground: a new enum value. SheetsPage renders <main className="sheets-root">
      //   (SheetsPage.jsx:54), not a drawing canvas, not a project board, not
      //   a device stage. SurfaceGrounds.jsx:110-112 derives DRAWING_SURFACES
      //   from `contract.ground === 'drawing'`, so 'sheet' is excluded from the
      //   drawing set BY CONSTRUCTION: no edit to that file, and no chance of
      //   the two drifting.
      ground: 'sheet',
      // scene: SiteRoot.jsx:234-237 `scene === 'sheets'` renders <SheetsPage/>
      //   as a sibling arm of the console, routeScene.js:30 maps /sheets and
      //   /sheets/<code> to it. This is the slot that says the studio does not
      //   host this surface, and it is why chrome.tab is false below.
      scene: 'sheets',
      chrome: {
        // Nothing wraps SheetsPage: SiteRoot.jsx:234-237 is <Suspense> around
        // the bare component, with no ProductSurfaceFrame, no workspace card
        // and no cockpit band anywhere in the arm.
        productFrame: false,
        workspaceCard: false,
        cockpit: false,
        // stageBranch: null, genuinely undeclared, not "no branch". The stage
        //   ternary (ToolCast.jsx:1434 / :2077 / :2139) is never reached: this
        //   surface is not a stage cast at all, so no arm of it is the answer.
        stageBranch: null,
        projectSlot: null,
        // tab: ProductSurfaceTabs.jsx:72 filters on this slot. False today, so
        //   slice 5b renders the same four tabs it rendered before.
        tab: false,
      },
      // toolbar: false/null, not "no markup". SheetFrame.jsx:81-92 renders a
      // per-sheet sheet-nav, sheetsContent.jsx:84-85 a print button, and
      // sheetsContent.jsx:169 a /try CTA: real chrome exists on the page. None
      // of it is DraftingRibbon / NavRail / JobRail / dock, the contract's own
      // toolbar vocabulary, which is what this slot declares the absence of.
      toolbar: { ribbon: false, home: null, quick: null },
      // No rails: nothing in the sheets arm mounts a nav rail, job rail or dock.
      rails: { left: 'none', right: 'none', dock: null },
      // groundMaterial: the solar accent and the string overlay are drawing-
      //   ground material (App.jsx:2336, :2353); this ground carries neither.
      groundMaterial: { layerAccent: null, solarStrings: false },
      // commandLine: App.jsx:3479's PromptBox is inside the console; the sheets
      //   arm mounts no prompt at all.
      commandLine: false,
      // authoring: no AuthorPanel is reachable from a page with no rail.
      authoring: false,
      versions: 'none',
      // conversations.scope: null, undeclared. No ConversePanel is mounted in
      //   this arm and no drawing identity is consumed for one, so a scope
      //   here would be a guess.
      conversations: { scope: null },
      integrations: null,
      // builds.routes: declared EMPTY. SheetsPage has no run or build control
      //   of any kind, so this is an absence that was read, not one assumed.
      // builds.card: null, slice 11a. No rail mounts in the sheets arm
      //   (rails.right 'none'), so there is no host for a BuildQueueCard and
      //   the toolbar badge has no toolbar to sit in; the doc's field table
      //   carries the reason.
      builds: { routes: [], card: null },
      // contextMenu: zero contextmenu handlers exist under web/src.
      contextMenu: [],
      shortcuts: null,
      entitlements: null,
      resetOn: null,
      a11y: null,
      // tourAnchors: neither shell hosts this surface (scene: 'sheets'), so
      //   neither declares a tour. Both keys present, both absent on purpose.
      tourAnchors: { console: null, stage: null },
    }),
  }),
])

// Every id the module ships a RECORD for. Record lookup only, never the
// studio's selection set, which is the next constant.
const SURFACE_IDS = new Set(PRODUCT_SURFACES.map(({ id }) => id))

// The ids the STUDIO can select, derived from the same slot the tab band
// renders from (`chrome.tab`), so a selector can only ever name a tab that
// exists. Computed once at module load; `has` stays O(1) on every route parse.
//
// Slice 5b decision, and the reason these are two sets rather than one:
// /app never hosts sheets (`scene: 'sheets'`), so `?surface=sheets` on /app
// must NOT select a tab that does not render, it falls closed to the default
// surface, exactly like `?surface=nonsense` does. Resolving it to a real-but-
// invisible surface would leave the tablist with no selected tab and the frame
// describing a page the console cannot show. productSurfaces.test.js pins both
// halves: the normalize-to-default, and the fact that the RECORD is still
// addressable so `surfaceContract('sheets')` returns the sheets contract
// rather than CAD's.
const SELECTABLE_SURFACE_IDS = new Set(
  // Unguarded, matching every other chrome.tab reader (ProductSurfaceTabs.jsx:72):
  // the schema test (Surface Contract — schema) pins every id in this same
  // module-scope array to declare `chrome.tab` exactly once, so a missing slot
  // here would already be a failing test elsewhere, not a runtime possibility.
  PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab).map(({ id }) => id),
)

export function normalizeProductSurface(value) {
  return SELECTABLE_SURFACE_IDS.has(value) ? value : DEFAULT_PRODUCT_SURFACE
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
    // sheets: the one surface with NO precondition. SheetsPage.jsx renders its
    // content immediately with no auth gate, no drawing and no readiness
    // check; loadDemoSolve (SheetsPage.jsx:31) is a soft enhancement with static fallbacks
    // (:26-28), never a gate. So this row takes no argument and has no branch:
    // inventing a sign-in or setup state here would be a fabricated condition.
    sheets: { state: 'available', label: 'Ready' },
  }
}

/**
 * The RECORD for a surface id. Distinct from selection: every id the module
 * ships is addressable here, including ones the studio cannot select
 * (`chrome.tab: false`), so a contract lookup returns that surface's own
 * contract rather than the default's. An id the module does not ship still
 * falls closed to the default record rather than returning undefined.
 */
export function productSurface(id) {
  const known = SURFACE_IDS.has(id) ? id : DEFAULT_PRODUCT_SURFACE
  return PRODUCT_SURFACES.find((surface) => surface.id === known)
}

/**
 * The Surface Contract for a surface. Resolves exactly like productSurface(),
 * so an unknown id falls closed to the CAD contract rather than returning
 * undefined into a consumer's slot lookup, and a shipped-but-unselectable id
 * ('sheets') returns its OWN contract.
 */
export function surfaceContract(id) {
  return productSurface(id).contract
}

/**
 * The declared ground kind: 'drawing' | 'board' | 'device-stage' | 'sheet'.
 * ('sheet' is slice 5b's addition, and the reason SurfaceGrounds excludes the
 * sheets surface from its drawing set without a line of its own.) Equal to
 * groundShowsDrawing(id) === (surfaceGround(id) === 'drawing') by
 * construction today; productSurfaces.test.js pins that equality.
 */
export function surfaceGround(id) {
  return surfaceContract(id).ground
}
