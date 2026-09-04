# The Surface Contract

Standardization slices 1-2 and 5b of 13. Plan: `C:/Users/ehaug/.claude/plans/staged-wiggling-fairy.md`,
section "The Surface Contract", element table row 1.

**Slice 1** froze the contract as DATA on `web/src/site/productSurfaces.js`, with every
default EQUAL TO TODAY, read off the code site cited beside it. No component read the
`contract` object, so slice 1 could not change a pixel.

**Slice 2** made the manifest LOAD-BEARING. `App.jsx`, `ToolCast.jsx` and
`SurfaceGrounds.jsx` now read the declared slots, so no shell component decides chrome by
comparing `activeSurface` to a string literal any more. Behaviour is unchanged, and that is
a test rather than a claim: `web/src/site/surfaceGates.test.js` pins every derived gate
equal to the literal predicate it replaced, for all four surface ids, with the old
predicates written out as literals so a later edit to a default fails loudly instead of
agreeing with itself. The three console/stage divergences (D1, D2, D3) and the solar
`productFrame` quirk are PRESERVED, each still a later-slice item. From here on, editing a
value in the manifest changes the product.

The operator rule the contract exists to serve, verbatim:

> "nothing HAS to be there, but everything needs to be ABLE to be there, according to
> agent/operator decisions, effortlessly as a key/staple functionality"

**Slice 5b** added the FIFTH surface record, `sheets`, and two new slots on every record:
`chrome.tab` (does this surface render in the tab band) and `scene` (which `SiteRoot` arm
hosts it). Sheets ships `tab: false` and `scene: 'sheets'`, so the slice renders nothing
new: `ProductSurfaceTabs` now filters on `chrome.tab`, and all four studio surfaces declare
`true`, so the band is byte-identical. The manifest now covers `/sheets`; the studio still
does not host it. See "Sheets, the fifth surface" below.

`docs/convergence/ACCEPTANCE.md` is frozen and is not edited by any slice.

## How to read a value

Every value in the manifest was read off a code site, and that site is cited in a comment
beside the field group in `productSurfaces.js` and again in the table below. Where today's
behaviour is not derivable per surface, the value is `null`, which means **undeclared, a
later slice fills it**. `null` never means "off" and is never a guess.

Two shells render these surfaces today and they do not agree everywhere. The manifest
carries the **console** value (`web/src/App.jsx`, the `/app` surface). Every place the
**stage** (`web/src/site/ToolCast.jsx`, the `/try` surface) differs is recorded under
"Divergences between the two shells" and is a slice-2 item, not something this slice
papers over.

## Field table

`where it is read` is the slice-2 consumer: the site that now asks the contract.
The literal each one replaced is named beside it, and is pinned equal to the
contract value for the four STUDIO ids by `web/src/site/surfaceGates.test.js`.
Sheets (`scene: 'sheets'`) is pinned by its own column in the same file, against
the sheets arm rather than against the console predicates, which never ran for it.

| field | type | meaning | where it is read (and the literal it replaced) |
| --- | --- | --- | --- |
| `ground` | `'drawing' \| 'board' \| 'device-stage' \| 'sheet'` | the canvas kind under the shell | `web/src/site/SurfaceGrounds.jsx:106` derives `DRAWING_SURFACES` from the contract (`contract.ground === 'drawing'`), read by `groundShowsDrawing()` at `:111`; it replaced the literal `new Set(['cad','solar'])`; the two non-drawing kinds are named in `web/e2e/local/one-shell-mount.spec.mjs:131` and rendered by `ProjectBoardGround` (`SurfaceGrounds.jsx:133`, active on `surfaceGround(surface) === 'board'` at `:298`, was `surface === 'browser'`) and `DeviceGround` (`:222`, active on `=== 'device-stage'` at `:306`, was `surface === 'ios'`) |
| `chrome.productFrame` | boolean | the `<ProductSurfaceFrame>` wrapper renders | `web/src/App.jsx:2838` `surfaceSlots.chrome.productFrame`; replaced `activeSurface !== 'cad'` |
| `chrome.workspaceCard` | boolean | the drawing workspace card is visible | `web/src/App.jsx:2859` `display: surfaceSlots.chrome.workspaceCard ? undefined : 'none'`; replaced `activeSurface === 'cad' \|\| activeSurface === 'solar'` |
| `chrome.cockpit` | boolean | the drafting cockpit bands mount | `web/src/App.jsx:2269` `const drafting = surfaceSlots.chrome.cockpit` (was `groundShowsDrawing(activeSurface)`), consumed as `studioGround && drafting` at `:2562` (top band) and `:2898` (ribbon), and ~20 further sites. The name is kept because the App wiring pin guards that exact shape, and because the W4f cockpit owner asked that `drawingCommandOnRef` (`:2280`) and the three `ENV_CAD_EDIT` mounts stay on this one predicate |
| `chrome.stageBranch` | `'cad' \| 'ios' \| 'frame'` | which arm of the stage's ternary this surface takes | `web/src/site/ToolCast.jsx:223` `const stageBranch = surfaceContract(activeSurface).chrome.stageBranch`, switched at `:1434` (`cad`), `:2077` (`ios`), `:2139` (frame fallthrough); it replaced the inline surface-literal ternary. The ios readiness effects (`:1302`, `:1361`) and the `useIosSurface` enabled gate (`:1328`) read the same binding, so the hook still mirrors its render gate exactly |
| `chrome.projectSlot` | `'ios-surface' \| null` | what fills the frame's project slot | `web/src/App.jsx:2846-2847` `surfaceSlots.chrome.projectSlot === 'ios-surface'`; replaced `activeSurface === 'ios'` |
| `chrome.tab` | boolean | this surface renders a tab in the profile band | `web/src/components/ProductSurfaceTabs.jsx:68` `PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab)`; replaced an unfiltered `.map` over every record. Also the STUDIO'S SELECTION SET: `productSurfaces.js` derives the ids `normalizeProductSurface()` accepts from this slot, so `?surface=<id>` can only ever name a tab that exists (slice 5b) |
| `scene` | `'app' \| 'sheets'` | which `SiteRoot` arm hosts this surface | `web/src/site/SiteRoot.jsx:228-232` (the console arm, all four studio surfaces) and `:234-237` (`scene === 'sheets'`, bare `<SheetsPage/>`); the path mapping is `web/src/site/routeScene.js:30`. Declarative only today: `SiteRoot` still switches on its own `scene` value, and repointing that switch onto this slot is later-slice work (slice 5b) |
| `toolbar.ribbon` | boolean | `DraftingRibbon` mounts | `web/src/App.jsx:2898` |
| `toolbar.home` | ribbon tab id \| null | the tab the ribbon opens on | `web/src/App.jsx:2234` `useState(() => surfaceContract(activeSurface).toolbar.home ?? surfaceContract(DEFAULT_PRODUCT_SURFACE).toolbar.home)`; replaced the literal `useState('draw')`. The fallback is load-bearing, not decoration: the ribbon tab is console-GLOBAL state, so a surface that declares no home tab (its ribbon never mounts) must still leave a real tab selected for the next surface that does. The id vocabulary is `web/src/site/CockpitTopBand.jsx:17-26` `RIBBON_TABS` |
| `toolbar.quick` | array \| null | quick-access ids | undeclared: `CockpitTopBand.jsx:52` takes `before`/`after` as PROPS, built imperatively at `web/src/App.jsx:2453-2468`. There is no data source to read ids from, so `null` on every surface |
| `rails.left` | `'spine' \| 'nav' \| 'none'` | the left nav rail's posture | `web/src/App.jsx:2281` `navSpine = !!studioGround && surfaceSlots.rails.left === 'spine' && !navExpanded && wideViewport` (the surface term was `drafting`); the rail itself is `:2645` |
| `rails.right` | `'job-spine' \| 'job-rail' \| 'none'` | the job monitor's posture | `web/src/App.jsx:2274` `const jobSpine = surfaceSlots.rails.right === 'job-spine'` (was `drafting`), used on all three JobRail props at `:3460-3462`; `JobRail` mounts at `:3445` |
| `rails.dock` | array of section ids \| null | the properties dock's sections | mount gate `web/src/App.jsx:3191` `studioGround && dockSections && wideViewport`, where `dockSections` (`:2272`) is `surfaceSlots.rails.dock` and its TRUTHINESS replaced `drafting`; `paneOpen` (`:2241`, default `true`) is the SECOND gate inside that branch, so a null dock and a user-closed pane render the same nothing for different reasons; section order `web/src/site/PropertiesDock.jsx:132-142` |
| `groundMaterial.layerAccent` | `'solar' \| null` | a per-surface recolour of the drawing ground | `web/src/App.jsx:2296` `surfaceSlots.groundMaterial.layerAccent === 'solar'`; replaced `activeSurface === 'solar'`. Added by slice 2, which had to declare the slot before it could repoint the gate |
| `groundMaterial.solarStrings` | boolean | the bundled solved string overlay is eligible | `web/src/App.jsx:2313` `surfaceSlots.groundMaterial.solarStrings`; replaced `activeSurface === 'solar'`. The SURFACE term only: the `mock`, demo-sample and head-v1 honesty gates beside it still decide whether anything renders, and none of them is surface data |
| `commandLine` | boolean | the docked one-line "Command:" mode | `web/src/App.jsx:3419` `commandLine={!!studioGround && surfaceSlots.commandLine}` (the surface term was `drafting`) |
| `authoring` | boolean \| null | the author/build lane is reachable | `web/src/App.jsx:2735` `<AuthorPanel>`, inside the nav rail (`:2645`) which is not surface-gated |
| `versions` | `'drawing' \| 'none' \| null` | the version source and its restore route | `web/src/App.jsx:3041` `<VersionHistory>`, which sits inside the workspace card whose display gate is `:2859` |
| `conversations.scope` | `'project' \| 'drawing' \| null` | what an AI conversation is scoped to | `web/src/converse.js:129-134` `sessionCacheKey(drawingId, projectId)` caches ONE session per project+drawing pair; `ConversePanel` mounts at `web/src/App.jsx:3306` with no surface gate |
| `integrations` | null | which mounts show, how a new one is linked | undeclared: the only link surface is the header's Claude account panel (`web/src/App.jsx:2630`), which is global, not per-surface |
| `builds.routes` | array | what this surface can launch | `web/src/App.jsx:2710` `ToolsPanel onRequestRun` -> `onRequestCatalogRun`, mounted on every surface. No marathon route exists in this client at all |
| `contextMenu` | array | element kinds exposing "configure / ask the agent" | `[]` on every surface: zero `contextmenu` / `onContextMenu` handlers exist anywhere under `web/src` (ripgrep, 2026-09-03). Declared empty, not undeclared |
| `shortcuts` | null | per-surface keyboard/touch triggers | undeclared: no per-surface shortcut registry exists |
| `entitlements` | null | per-surface per-tool entitlement | undeclared: `web/src/components/EntitlementGate.jsx:15` `ROWS` are TIER capability keys (`run_read`, `run_write`, `build`, `converse`), never per-surface |
| `resetOn` | null | scope reset on tenant/project switch | undeclared: no effect in `App.jsx` keys off `activeSurface` to reset scope |
| `a11y` | null | per-surface accessibility declarations | undeclared |
| `tourAnchors` | null | tour anchor ids | undeclared: the tour is mock-gated (`web/src/App.jsx:2761`), not surface data |

## The matrix (console values, equal to today)

The four studio columns are the console values, equal to today. The **sheets** column is
the `/sheets` arm, added by slice 5b and read off `SiteRoot.jsx` / `SheetsPage.jsx`, not off
`App.jsx` (the console never renders this surface).

| slot | browser | cad | solar | ios | sheets |
| --- | --- | --- | --- | --- | --- |
| `scene` | `app` | `app` | `app` | `app` | `sheets` |
| `ground` | `board` | `drawing` | `drawing` | `device-stage` | `sheet` |
| `chrome.tab` | `true` | `true` | `true` | `true` | `false` |
| `chrome.productFrame` | `true` | `false` | `true` | `true` | `false` |
| `chrome.workspaceCard` | `false` | `true` | `true` | `false` | `false` |
| `chrome.cockpit` | `false` | `true` | `true` | `false` | `false` |
| `chrome.stageBranch` | `frame` | `cad` | `frame` | `ios` | `null` |
| `chrome.projectSlot` | `null` | `null` | `null` | `ios-surface` | `null` |
| `toolbar.ribbon` | `false` | `true` | `true` | `false` | `false` |
| `toolbar.home` | `null` | `draw` | `draw` | `null` | `null` |
| `toolbar.quick` | `null` | `null` | `null` | `null` | `null` |
| `rails.left` | `nav` | `spine` | `spine` | `nav` | `none` |
| `rails.right` | `job-rail` | `job-spine` | `job-spine` | `job-rail` | `none` |
| `rails.dock` | `null` | `[layers, drawing, selection, plan]` | `[layers, drawing, selection, plan]` | `null` | `null` |
| `groundMaterial.layerAccent` | `null` | `null` | `solar` | `null` | `null` |
| `groundMaterial.solarStrings` | `false` | `false` | `true` | `false` | `false` |
| `commandLine` | `false` | `true` | `true` | `false` | `false` |
| `authoring` | `true` | `true` | `true` | `true` | `false` |
| `versions` | `none` | `drawing` | `drawing` | `none` | `none` |
| `conversations.scope` | `drawing` | `drawing` | `drawing` | `drawing` | `null` |
| `integrations` | `null` | `null` | `null` | `null` | `null` |
| `builds.routes` | `[one-shot]` | `[one-shot]` | `[one-shot]` | `[one-shot]` | `[]` |
| `contextMenu` | `[]` | `[]` | `[]` | `[]` | `[]` |
| `shortcuts` / `entitlements` / `resetOn` / `a11y` / `tourAnchors` | `null` | `null` | `null` | `null` | `null` |

### Notes on values that surprise

- **`rails.left`, `rails.right` and `rails.dock` are the WIDE-VIEWPORT defaults.** All three
  gates carry `wideViewport` (`App.jsx:2281`, `:3460`, `:3191`), which is
  `matchMedia('(min-width: 981px)')` (`App.jsx:2244-2245`). At or below 980px the console stacks
  into one column and the postures neutralise: the nav rail expands, the job rail expands,
  and the properties dock is replaced by its inline arm (`App.jsx:3213`). The manifest
  declares the wide default; a responsive dimension is a later slice.
- **`rails.dock` has a SECOND gate, and its four sections are the with-drawing case.**
  The mount branch (`App.jsx:3191`) asks the contract; inside it, `paneOpen`
  (`App.jsx:2241`, default `true`) decides whether the dock is actually drawn. The user
  closes it from the dock's own title row and the View tab's Properties tool brings it
  back, so "no dock declared" and "dock closed by the user" render the same nothing for
  entirely different reasons, and only the first is a contract fact. Separately, the
  declared four sections are the WITH-DRAWING case: `PropertiesDock.jsx:133` renders the
  `drawing` section only when `paneDrawingFacts` is non-null, and that is null until a
  drawing is `shown` (`App.jsx:2481`), so an honest-empty drafting surface shows three.
  The `plan` section is conditional on its prop too, and that prop is always supplied here.
- **`rails.left` and `rails.right` are also first-render values.** `navExpanded`
  (`App.jsx:2217`) and `jobRailExpanded` (`App.jsx:2224`) both start `false` and are
  in-memory only, so the posture resets per page load by design.
- **`chrome.productFrame` is `true` on solar.** `App.jsx:2838` tests `!== 'cad'`, so the
  product frame renders over the shown workspace card on Solar CAD. That is today's
  behaviour, pinned as-is. It is a candidate for slice 2 to make declarative, not a defect
  this slice may quietly fix.
- **`chrome.cockpit` equals `ground === 'drawing'` today.** It is declared separately anyway,
  per the operator rule: every slot must be declarable per surface, so a future surface can
  take a drawing ground without the drafting cockpit, or the reverse.
- **`versions` is `none` on browser and iOS because of a display gate, not an unmount.**
  `VersionHistory` (`App.jsx:3041`) is inside the workspace card, which is hidden with
  `display: none` (`App.jsx:2859`) rather than unmounted, so live drawing, lock and job state
  survive a tab switch. The user-visible answer is still "no version history here".
- **`authoring` is `true` on every surface.** `AuthorPanel` (`App.jsx:2735`) sits in the nav
  rail, which is not surface-gated. On cad and solar the rail collapses to a spine by
  default, so the panel is reached through the ribbon's author cluster (`App.jsx:2429`)
  rather than the rail itself.
- **`builds.routes` carries no `ship-lane` on iOS.** See the divergence table: the console
  mounts `IosSurface`, which is props-only with no launch control.
- **`conversations.scope` is `drawing` even where there is no drawing.**
  `sessionCacheKey` falls back to the literal `'default'` drawing key
  (`converse.js:132`), so the scope shape is drawing-keyed on every surface.

## Sheets, the fifth surface (slice 5b)

`/sheets` is a public page: `SiteRoot.jsx:234-237` renders a bare `<SheetsPage/>` inside a
`<Suspense>` with no wrapper of any kind, reached from `routeScene.js:30`. It has no
session gate, no drawing, no toolbar, no rail and no prompt. `SheetsPage.jsx` mounts
`<SheetsSet>` and nothing else (`:54-55`); `loadDemoSolve()` (`:30`) is a soft enhancement
with static fallbacks, never a gate.

**The decision: it stays its own `SiteRoot` arm, and the contract covers it anyway.**

Evidence for keeping it a separate arm, which is what shipped:

- `SiteRoot.jsx:48` draws the ownership boundary in the source: "Built by a sibling agent
  (`src/site/sheets/**` is theirs), referenced only".
- `web/e2e/local/one-shell-mount.spec.mjs:236` makes "the studio branch lives ONLY in the
  scene-app arm" an invariant, using `/sheets` as the negative control. Folding sheets into
  the studio would invert that test's premise, not extend it. That row is unchanged here.
- It is the only surface with no session concept at all. The other four each read
  `sessionActive` in `productSurfaceStates`; sheets has no precondition to read.

Evidence for covering it in the contract, which is also what shipped:

- The operator rule: everything must be ABLE to be there. A surface the manifest cannot
  describe is a surface no later slice can move, and the schema already models "declare
  everything even where this surface has none of it" (see the browser and iOS rows).
- A future consumer reading `PRODUCT_SURFACES` would otherwise need a hand-written carve-out
  for the one route the manifest does not know about.

The two are reconciled by the two new slots. `scene` records WHICH arm hosts a surface, so
the contract can describe `/sheets` without claiming the studio renders it. `chrome.tab`
records whether the band shows it, and sheets ships `false`, so slice 5b changes no pixel.
Flipping that one value is the entire config change if the answer ever becomes yes.

### `?surface=sheets` on `/app` normalizes to the DEFAULT

`normalizeProductSurface()` now derives its accepted set from `chrome.tab` rather than from
"every id the module ships". A sheets id arriving at the console therefore falls closed to
`cad`, exactly like `?surface=nonsense` does.

The honest reason: `/app` never hosts sheets. Resolving the id would select a tab the band
does not render, leaving the tablist with no selected tab and `ProductSurfaceFrame`
describing a page the console cannot show, a worse failure than the fall-closed, and one
no user could act on. `searchForProductSurface()` shares the normalizer, so the console
cannot mint such a link either.

Selection and LOOKUP are kept apart, and that distinction is load-bearing: `productSurface()`
and `surfaceContract()` resolve every id the module ships, so `surfaceContract('sheets')`
returns the sheets contract rather than CAD's. Both halves are pinned in
`productSurfaces.test.js` ("a sheets selector on /app falls closed to the default, but the
record stays addressable"); without the second half, every sheets assertion in the suite
would have been silently reading CAD's contract.

### `ground: 'sheet'` excludes it from the drawing set by construction

`SurfaceGrounds.jsx:110-112` derives `DRAWING_SURFACES` from `contract.ground === 'drawing'`.
A fourth ground kind is therefore excluded with no edit to that file and no chance of the
two drifting, the alternative, a literal exclusion list, is exactly what slice 2 removed.
`groundShowsDrawing('sheets')` is `false`, and the drawing grounds are still exactly `cad`
and `solar` with a fifth row present.

## Divergences between the two shells

The console (`App.jsx`, `/app`) and the stage (`ToolCast.jsx`, `/try`) disagree on three
rows. The manifest carries the console value; each row below is a slice-2 item.

| # | surface | slot | console (`App.jsx`) | stage (`ToolCast.jsx`) | slice-2 action |
| --- | --- | --- | --- | --- | --- |
| D1 | ios | `chrome.productFrame` / `chrome.projectSlot` | `true` with `projectSlot: 'ios-surface'`: the frame renders and `IosSurface` fills its slot (`App.jsx:2838`, `:2846-2847`) | `false`: the ios arm of the ternary (`ToolCast.jsx:2077`) renders its own top cluster and `tc-operator-rail` ship lane instead of the frame | one wrapper renders the declared frame; the ios ship lane becomes a declared slot, not a hard-coded branch |
| D2 | ios | `builds.routes` | `['one-shot']`: `IosSurface.jsx:3-4` is props-only ("no fetch, no polling, no client-side state math") and carries NO launch control | additionally a real ship-lane launch: `ToolCast.jsx:2114` `data-testid="ios-ship-launch"` -> `launchIosShip` | promote `ship-lane` to a declared route and mount ONE control from it, so the two shells stop disagreeing about whether a user can ship |
| D3 | cad | `authoring` reach | `true` on all four surfaces (`App.jsx:2735`, un-gated rail) | cad only: the Workspace rail's Author tab and `CapabilityCatalog` live inside the `activeSurface === 'cad'` arm (`ToolCast.jsx:1434`, `:1523`) | the author lane mounts from the contract on every surface it declares, scoped to that surface's `familyIds` |

A fourth, weaker mismatch: solar takes the stage's frame fallthrough
(`ToolCast.jsx:2139`), so the stage gives Solar CAD no drafting cockpit at all while the
console does (`chrome.cockpit: true`). Recorded here so slice 2 resolves it deliberately.

## Hardcoded forever

Three things are NOT contract slots and must never become configurable by a surface record.
The contract writes this into itself so a later slice cannot quietly relax it.

1. **The engine-session mount and its boundary.** ONE engine session owner
   (`docs/convergence/ACCEPTANCE.md`, "Engine-session ownership"): the engine-session store
   owns `EngineBoundary` construction, worker lifetime, and save target. In the console that
   is the single `EngineSessionProvider` wrap at `web/src/App.jsx:2550-2551`, applied through
   `engineScope(...)` at `:2816`. A second mount is a correctness bug, not a layout choice,
   so no surface may declare its own.
2. **The build-time flags and the literal `VITE_CAD_EDIT` fence.** Per the ACCEPTANCE flag
   matrix, `VITE_CAD_EDIT` must stay the literal `import.meta.env.VITE_CAD_EDIT === '1'` as
   the FIRST `&&` operand at every call site, because the fence is proven by building twice
   and grepping the emitted JS (`src/cadedit/bundleFence.test.js`). A folded or data-driven
   value cannot satisfy a static grep, so the fence can never move into the manifest.
   `VITE_LIFECYCLE_UI` has the same shape; `VITE_IOS_SURFACE` is deliberately NOT a fence.
3. **The old-shell fallback until W7.** The one-shell rollout is a runtime rail
   (`LEAF_ONE_SHELL_ENABLED`), and every row must behave identically with the rail OFF after
   every wave until W7 deletes the old shell (ACCEPTANCE, "Route and boot-state matrix" and
   "Deletion criteria (W7)"). Every contract-rendered element therefore needs its own
   rail-OFF proof row from day one; the manifest describes the studio, never the fallback.

## What slice 1 changed

- `web/src/site/productSurfaces.js`: added `deepFreeze`, a `contract` object on each of the
  four records, and the `surfaceContract(id)` / `surfaceGround(id)` selectors.
  `id`, `label`, `eyebrow`, `title`, `description` and `familyIds` are byte-identical;
  `productSurface`, `normalizeProductSurface`, `productSurfaceStates`,
  `productSurfaceFromSearch` and `searchForProductSurface` are untouched. The module stays
  plain frozen data plus pure functions: no React, and no function inside a contract object.
- `web/src/site/productSurfaces.test.js`: three new suites (schema, equals-today, and a
  cross-check that `groundShowsDrawing(id) === (surfaceGround(id) === 'drawing')` for all
  four ids). The equals-today fixture is a literal matrix written from the code reading
  above, not copied out of the module, so a later edit to a default fails loudly.
- this file.

Nothing else. No consumer read `contract` in slice 1.

## What slice 2 changed

Every site below is a repoint: same value, read from the contract instead of decided
inline. Nothing renders that did not render before, and nothing stopped rendering.

| # | file | old predicate | now reads |
| --- | --- | --- | --- |
| 1 | `App.jsx:2269` | `drafting = groundShowsDrawing(activeSurface)` | `surfaceSlots.chrome.cockpit` (name kept; ~20 `studioGround && drafting` sites unchanged) |
| 2 | `App.jsx:2272` | (new binding) | `dockSections = surfaceSlots.rails.dock` |
| 3 | `App.jsx:2274` | (new binding) | `jobSpine = surfaceSlots.rails.right === 'job-spine'` |
| 4 | `App.jsx:2281` | `... && drafting && !navExpanded && wideViewport` | `surfaceSlots.rails.left === 'spine'` |
| 5 | `App.jsx:2296` | `activeSurface === 'solar'` (layer accent) | `surfaceSlots.groundMaterial.layerAccent === 'solar'` |
| 6 | `App.jsx:2313` | `activeSurface === 'solar'` (`solarStringsEligible`) | `surfaceSlots.groundMaterial.solarStrings` |
| 7 | `App.jsx:2234` | `useState('draw')` | `toolbar.home`, falling back to the default surface's home |
| 8 | `App.jsx:2838` | `activeSurface !== 'cad'` | `surfaceSlots.chrome.productFrame` |
| 9 | `App.jsx:2846` | `activeSurface === 'ios'` | `surfaceSlots.chrome.projectSlot === 'ios-surface'` |
| 10 | `App.jsx:2859` | `activeSurface === 'cad' \|\| activeSurface === 'solar'` | `surfaceSlots.chrome.workspaceCard` |
| 11 | `App.jsx:3191` | `studioGround && drafting && wideViewport` | `studioGround && dockSections && wideViewport` (`paneOpen` still the second gate) |
| 12 | `App.jsx:3419` | `commandLine={!!studioGround && drafting}` | `surfaceSlots.commandLine` |
| 13 | `App.jsx:3460-3462` | `drafting` on `spine` / `onExpand` / `onCollapse` | `jobSpine` |
| 14 | `ToolCast.jsx:223` | (new binding) | `stageBranch = surfaceContract(activeSurface).chrome.stageBranch` |
| 15 | `ToolCast.jsx:1302` | `activeSurface !== 'ios'` (ship readiness) | `stageBranch !== 'ios'` |
| 16 | `ToolCast.jsx:1328` | `activeSurface === 'cad'` (`useIosSurface` enabled) | `stageBranch === 'cad'`, which still mirrors the render gate exactly, as its comment requires |
| 17 | `ToolCast.jsx:1362` | `activeSurface !== 'ios'` (execution poll) | `stageBranch !== 'ios'` |
| 18 | `ToolCast.jsx:1434` / `:2077` | the surface-literal ternary | `stageBranch === 'cad'` / `stageBranch === 'ios'` |
| 19 | `SurfaceGrounds.jsx:106` | `new Set(['cad','solar'])` | derived from `contract.ground === 'drawing'` |
| 20 | `SurfaceGrounds.jsx:298` | `surface === 'browser'` | `surfaceGround(surface) === 'board'` |
| 21 | `SurfaceGrounds.jsx:306` | `surface === 'ios'` | `surfaceGround(surface) === 'device-stage'` |

New in the manifest, because the gate could not be repointed until the slot existed:
`groundMaterial: { layerAccent, solarStrings }`, values read off `App.jsx:2296` and `:2313`.

Also in this slice:

- `deepFreeze` now recurses INTO already-frozen nodes. The slice-1 version short-circuited
  on `Object.isFrozen(value)`, which made "deep" mean "down to the first frozen node", and
  `PRODUCT_SURFACES` nests shallow `Object.freeze`d literals, so the trap was one edit from
  leaving a live slot writable. A `WeakSet` replaces the frozen bit as the cycle guard, so
  a self-referential tree still terminates. Four tests cover it, including the cycle.
- Four line citations in `productSurfaces.js` were off by a few lines (`navExpanded`,
  `ClaudeAccountPanel`, the ribbon author cluster, and the tour gates). Every `App.jsx`,
  `ToolCast.jsx` and `SurfaceGrounds.jsx` citation in this file, in `productSurfaces.js`
  and in the test fixtures was then re-resolved against the post-slice-2 source and
  corrected, because slice 2 moved most of them.
- `scripts/run-all-gates.py`: the `web-vitest` baseline COMMENT was stale (it described
  748 collected / 17 skipped / 731 executed). The floor itself (`expected=731`) is a
  minimum and still passes, so it is left alone; only the comment is corrected.

### Left alone, on purpose

- `ProductSurfaceTabs.jsx` no longer compares `surface.id` to anything (slice 2
  fix-forward). Its project-slot gate, the one mount gate #978 missed
  (`showProjectState = surface.id !== 'ios'`), reads `chrome.projectSlot` like App.jsx does;
  the three per-surface notes (browser composition, the solar template sentence, the iOS
  credential warning) are COPY and moved to a `SURFACE_NOTES` lookup keyed by id, text
  byte-identical. `surfaceGates.test.js` now scans this file and `SurfaceGrounds.jsx` too,
  with a second probe for `surface` / `surface.id` comparisons.
- `App.jsx:3162`, `:3216` and `:3514` still call `groundShowsDrawing(activeSurface)`
  directly. They are already contract-driven (`groundShowsDrawing` is derived from
  `contract.ground` as of this slice), and `siteRootOneShell.test.js` pins their exact
  source shape as white-screen regression guards, so rewriting them would break a pin
  while changing nothing.
- `web/src/lib/surfaceRails.js` already reads the manifest.
- The literal `import.meta.env.VITE_CAD_EDIT === '1'` fence is untouched at every call
  site, as the "Hardcoded forever" section requires.

## The honesty-ladder gate (slice 13d)

`web/scripts/check_honesty_ladder.mjs` (npm script `check:honesty-ladder`, suite
`web-honesty-ladder` in `scripts/run-all-gates.py`) enforces this document against the code
rather than trusting it. Its third check walks every surface's `contract` in
`productSurfaces.js`, collects each slot whose value is `null`, `false` or `'none'`, and
fails if that slot name has no row in the "Field table" section above, so a slot cannot be
declared absent in the manifest without a written rationale a reader can find. The other
three checks enforce the same rule one level down in the product: every exported `*REASONS`
map under `web/src` is frozen and holds real sentences, every `SOME_REASONS.key` reference
resolves to a key that map defines (an undefined key renders an EMPTY reason, which is
silent at runtime), and every ribbon or tool record that sets `disabled` also sets `reason`,
which is the HONESTY CONTRACT stated in `web/src/lib/ribbonClusters.js`'s file header and
until now enforced by nothing. The gate is static, so it never renders a surface, and it
carries its own positive controls: fixture sources with a reasonless disabled record, an
unfrozen map, a placeholder reason and a dangling key each drive the same functions red.
Adding a slot to the contract therefore means adding its field-table row in the same change.

## What slice 5b changed

| # | file | change |
| --- | --- | --- |
| 1 | `web/src/site/productSurfaces.js` | the fifth record (`sheets`), every value cited to `SiteRoot.jsx` / `SheetsPage.jsx` / `routeScene.js`; the new `scene` and `chrome.tab` slots on all five records; `SELECTABLE_SURFACE_IDS` derived from `chrome.tab` and read by `normalizeProductSurface()`; `productSurface()` resolves any SHIPPED id (selection and lookup split); `productSurfaceStates()` gains a constant `sheets: { state: 'available', label: 'Ready' }` row |
| 2 | `web/src/components/ProductSurfaceTabs.jsx` | one line: the band maps over `PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab)` instead of every record |
| 3 | `web/src/site/productSurfaces.test.js` | id list and count (5), the `sheets` `CONTRACT_FIXTURE` entry, `scene` in `CONTRACT_KEYS`, `tab` in the chrome key set, `'sheet'` and the `scene` enum in `ENUMS`, the normalization decision pinned both ways, the constant sheets status row, and a sheets suite (copy, chrome-free contract, scene, tab, ground) |
| 4 | `web/src/site/surfaceGates.test.js` | `SURFACE_IDS` is all five (so the completeness row catches a sixth), `STUDIO_SURFACE_IDS` is the four the old console predicates actually ran for, and a "sheets column" suite written against the sheets arm, including a row proving the old predicates DISAGREE with it, which is why it has its own column |
| 5 | `web/src/components/ProductSurfaceTabs.test.jsx` | a jsdom suite: exactly four tabs, in manifest order, no Sheets tab, the count derived from `chrome.tab` on both sides, and every rendered tab carrying a real status label |
| 6 | this file | the `sheets` matrix column, the two new field-table rows, and the decision above |

Not touched: `SurfaceGrounds.jsx` (the derivation already excludes a new ground kind),
`App.jsx`, `ToolCast.jsx`, `SiteRoot.jsx`, `src/site/sheets/**` (a sibling agent's), and the
`/sheets` row in `web/e2e/local/one-shell-mount.spec.mjs`, whose premise this slice
deliberately preserves.
