# The Surface Contract

Standardization slices 1-2, 4b, 5a and 5b of 13. Plan: `C:/Users/ehaug/.claude/plans/staged-wiggling-fairy.md`,
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

**Slice 4b** hoisted the OWNER of the continuity rail and the sign-out control above the
scene ternary (`SiteRoot` mounts one `ContinuityStore`; the scene's `SurfaceFrame` publishes,
the tabs adopt), so both survive the `/try` <-> `/app` crossing as the same nodes with the
same markup in the same place, and filled `tourAnchors` on every record: the guided tours
now spotlight by `data-tour` anchor first with the className chain as the fallback, the step
arrays untouched. See "What slice 4b changed" below.

**Slice 5a** made the command bar ONE component. The stage (`/try`, `ToolCast.jsx`) used to
hand-roll its own bar (`.tc-bar-input-row` with a plain `<input>`, a `.tc-run` button and a
static controls row); it now mounts the console's `PromptBox` inside its `.tc-bar`, through
`SurfaceFrame`'s `commandBar` slot, and the console passes nothing new, so `/app` renders
byte for byte as before (pinned). See "The stage's command bar" below.

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
| `ground` | `'drawing' \| 'board' \| 'device-stage' \| 'sheet'` | the canvas kind under the shell | `web/src/site/SurfaceGrounds.jsx:111` derives `DRAWING_SURFACES` from the contract (`contract.ground === 'drawing'`), read by `groundShowsDrawing()` at `:116`; it replaced the literal `new Set(['cad','solar'])`; the two non-drawing kinds are named in `web/e2e/local/one-shell-mount.spec.mjs:131` and rendered by `ProjectBoardGround` (`SurfaceGrounds.jsx:137`, active on `surfaceGround(surface) === 'board'` at `:303`, was `surface === 'browser'`) and `DeviceGround` (`:226`, active on `=== 'device-stage'` at `:311`, was `surface === 'ios'`) |
| `chrome.productFrame` | boolean | the `<ProductSurfaceFrame>` wrapper renders | `web/src/App.jsx:2898` `surfaceSlots.chrome.productFrame`; replaced `activeSurface !== 'cad'` |
| `chrome.workspaceCard` | boolean | the drawing workspace card is visible | `web/src/App.jsx:2859` `display: surfaceSlots.chrome.workspaceCard ? undefined : 'none'`; replaced `activeSurface === 'cad' \|\| activeSurface === 'solar'` |
| `chrome.cockpit` | boolean | the drafting cockpit bands mount | `web/src/App.jsx:2269` `const drafting = surfaceSlots.chrome.cockpit` (was `groundShowsDrawing(activeSurface)`), consumed as `studioGround && drafting` at `:2562` (top band) and `:2898` (ribbon), and ~20 further sites. The name is kept because the App wiring pin guards that exact shape, and because the W4f cockpit owner asked that `drawingCommandOnRef` (`:2280`) and the three `ENV_CAD_EDIT` mounts stay on this one predicate |
| `chrome.stageBranch` | `'cad' \| 'ios' \| 'frame'` | which arm of the stage's ternary this surface takes | `web/src/site/ToolCast.jsx:223` `const stageBranch = surfaceContract(activeSurface).chrome.stageBranch`, switched at `:1434` (`cad`), `:2077` (`ios`), `:2139` (frame fallthrough); it replaced the inline surface-literal ternary. The ios readiness effects (`:1302`, `:1361`) and the `useIosSurface` enabled gate (`:1328`) read the same binding, so the hook still mirrors its render gate exactly |
| `chrome.projectSlot` | `'ios-surface' \| null` | what fills the frame's project slot | `web/src/App.jsx:2846-2847` `surfaceSlots.chrome.projectSlot === 'ios-surface'`; replaced `activeSurface === 'ios'` |
| `chrome.tab` | boolean | this surface renders a tab in the profile band | `web/src/components/ProductSurfaceTabs.jsx:72` `PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab)`; replaced an unfiltered `.map` over every record. Also the STUDIO'S SELECTION SET: `productSurfaces.js` derives the ids `normalizeProductSurface()` accepts from this slot, so `?surface=<id>` can only ever name a tab that exists (slice 5b) |
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
| `versions` | `'drawing' \| 'none' \| null` | the version source and its restore route | Slice 6a: BOTH shells mount the one primitive `web/src/components/VersionList.jsx`, gated on `surfaceContract(id).versions !== 'none'` — /app through the `<VersionHistory>` drawer (`web/src/App.jsx`, the `vh-anchor` block), /try through the Versions tab (`web/src/site/ToolCast.jsx`, `versionsMounted`) |
| `conversations.scope` | `'project' \| 'drawing' \| null` | what an AI conversation is scoped to | `web/src/converse.js:129-134` `sessionCacheKey(drawingId, projectId)` caches ONE session per project+drawing pair; `ConversePanel` mounts at `web/src/App.jsx:3306` with no surface gate |
| `integrations` | null | which mounts show, how a new one is linked | undeclared: the only link surface is the header's Claude account panel (`web/src/App.jsx:2630`), which is global, not per-surface |
| `builds.routes` | array | what this surface can launch | `web/src/App.jsx:2710` `ToolsPanel onRequestRun` -> `onRequestCatalogRun`, mounted on every surface. No marathon route exists in this client at all |
| `contextMenu` | array | element kinds exposing "configure / ask the agent" | `[]` on every surface: zero `contextmenu` / `onContextMenu` handlers exist anywhere under `web/src` (ripgrep, 2026-09-03). Declared empty, not undeclared |
| `shortcuts` | null | per-surface keyboard/touch triggers | undeclared: no per-surface shortcut registry exists |
| `entitlements` | null | per-surface per-tool entitlement | undeclared: `web/src/components/EntitlementGate.jsx:15` `ROWS` are TIER capability keys (`run_read`, `run_write`, `build`, `converse`), never per-surface |
| `resetOn` | null | scope reset on tenant/project switch | undeclared: no effect in `App.jsx` keys off `activeSurface` to reset scope |
| `a11y` | null | per-surface accessibility declarations | undeclared: `web/src/site/productSurfaces.js:179` states no per-surface a11y declaration exists, so `null` on every surface |
| `tourAnchors` | `{ console, stage }` | per shell, the guided tour's step id -> `data-tour` anchor id map | slice 4b. `web/src/demo/DemoTour.jsx` `resolveTourTarget` resolves `[data-tour="<id>"]` FIRST and the step's className chain second; `web/src/App.jsx` passes `surfaceSlots.tourAnchors?.console`, `web/src/site/ToolCast.jsx` passes `surfaceContract(activeSurface).tourAnchors?.stage`. The two step arrays are byte-identical to before; the contract carries the anchor. Vocabulary (both shells, their own elements): `shell`, `viewer`, `command-bar`, `right-rail` (`left-rail` existed through this slice's first cut and was removed as unreferenced vocabulary, see "What slice 4b changed" below). A step whose className chain picks among candidates by presence is deliberately NOT mapped. Gate: `web/scripts/check_tour_anchors.mjs` (`check:tour-anchors`), which fails on a `data-tour` id present in source that no step map references, not only on a mapped id absent from source; the pairing itself (an anchored step resolves to the same element its className chain would have picked) is pinned by `web/src/demo/demoTourAnchors.test.jsx`, not the e2e walk |
| `tourAnchors.console` | map \| null | the console tour's anchors on this surface | the console tour (`App.jsx`, mock-gated) mounts on every studio surface, so the four `scene: 'app'` records carry the same map (`CONSOLE_TOUR_ANCHORS` in `productSurfaces.js`); `null` on sheets, which the console never hosts |
| `tourAnchors.stage` | map \| null | the stage walk's anchors on this surface | the stage walk (`ToolCast.jsx` `UNIFIED_TOUR_STEPS`) mounts inside the cad arm only (`chrome.stageBranch === 'cad'`), so cad declares `STAGE_TOUR_ANCHORS` and browser / solar / ios / sheets declare `null`: no walk mounts there, so no anchor can be spotlit there. The gate pins the map to exactly the surfaces whose stage arm mounts the walk |

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
| `shortcuts` / `entitlements` / `resetOn` / `a11y` | `null` | `null` | `null` | `null` | `null` |
| `tourAnchors.console` | console map | console map | console map | console map | `null` |
| `tourAnchors.stage` | `null` | stage map | `null` | `null` | `null` |

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
- **`versions` is now a real mount gate on both shells (slice 6a).** It was a display gate:
  `VersionHistory` sat inside the workspace card, hidden with `display: none` rather than
  unmounted, and /try's version tab was not gated at all. Both now read
  `surfaceContract(id).versions !== 'none'`, so on browser and iOS the history button, the
  drawer and the Versions tab are absent rather than merely invisible. The workspace card
  still hides rather than unmounts, so live drawing, lock and job state survive a tab switch.
- **One version list, two skins.** `web/src/components/VersionList.jsx` owns the behaviour:
  newest-first ordering, the delta chip, the authored-tool provenance chip, the two-step
  restore/recover confirm machine with its single-flight guard, and the read-only preview
  strip. Each surface keeps its own markup (`vh-row-v{n}` in the drawer, `try-version-v{n}`
  in the tab), pinned byte for byte by `web/src/components/versionList.test.jsx` against a
  capture of the pre-slice drawer. /try's loader now forwards `include_deltas=1`, so the two
  shells show the same deltas; the e2e row that asserted its absence is re-pinned.
- **`source_ref` is provenance, never a guess.** A version row carries the sha256 of the
  writing tool's published body when the server holds one, and `null` otherwise. The server
  bounds and charset-validates it on the way out (`server/routers/drawings.py` `_source_ref`),
  a restore carries the source version's value forward because the new head's bytes ARE that
  version's bytes (the row is threaded out of the one manifest read that proved the version
  exists, `da/store.py` `resolve_version_entry`), and nothing anywhere invents an author for a
  version that has none. The rule that makes this literal on EVERY execution tier:
  **server-held or absent.** The digest is measured by the server itself, at stamp time, over
  the published tool body it resolves for the tool id the mutation binding names
  (`server/tool_loader.py` `published_tool_source_sha256`: the same UTF-8 text every sandbox
  tier is fed, the same text the harness's `leaf.tool-source.v1` receipt hashed when the tool
  was submitted, and the same text a genuine microvm receipt hashes), measured BEFORE the
  planner runs. Nothing the sandbox returned is ever the value: on the in-process and
  `subprocess` tiers `execution_provenance` is whatever the tool body chose to say, since
  `tool_loader` adopts a tool's own `{ok, result}` return whole, and a shape check of a claim
  the attacker controls is not a fence, so `tool_loader` now drops a tool-supplied
  `execution_provenance` at the seam it enters and `write_loop` never reads the envelope for
  the stamp (`_server_held_source_ref`). If the server holds no published body for that tool
  id (an APS-only or dangling package, a staged design-time source, a platform builtin, an
  unreadable file) the row is `null`: honest absence. Where a verified
  `leaf.tool-execution.v1` microvm receipt exists it is only cross-checked against the
  server's digest; a mismatch withholds the stamp with a warning log, never the sandbox value.
  **The chip claims only what the digest proves.** `ToolSourceReceipt`
  (`harness/contract/HARNESS-CONTRACT.md`) is paths, byte counts and digests, with no author,
  model or session identity in it, so `SourceRefChip` renders `authored tool · {sha8}` and
  never names a model or a person.
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
`<SheetsSet>` and nothing else (`:54-55`); `loadDemoSolve()` (`:31`) is a soft enhancement
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

## The stage's command bar (slice 5a)

`web/src/components/PromptBox.jsx` is the ONE command bar. Before this slice the stage
rendered its own (`ToolCast.jsx`, the `.tc-bar` block): a plain `<input>` whose only key
handling was Enter, a Run button on a six-rung inline predicate, and a controls row of
static copy. The console's `PromptBox` had the slash picker, `@` mounts, session-keyed
prompt history, the IME guard, Shift+Enter, the combobox aria wiring and a live scope
chip, none of which the stage had. Slice 5a mounts `PromptBox` where the stage's rows
stood, and the stage GAINS all of that.

What the stage KEEPS, because 36 e2e rows query it (the scout table is in the PR):

| hook | where it rides now |
| --- | --- |
| `aria-label="Command bar"`, `data-testid="command-bar"` | PromptBox's `<textarea>` (always did) |
| `.tc-bar-wrap`, `.tc-bar` | still the stage's own wrapper divs; the strips, `RoutePanel` and the DWG/DXF drop handler stay inside `.tc-bar` exactly where they stood |
| `.tc-bar-input` | PromptBox's textarea, as a second class beside `bar-field` (`classNames.input`) |
| `.tc-bar-input-row` | PromptBox's `.bar-input` row (`classNames.wrap`) |
| `.tc-run` | PromptBox's Run chip beside `chip-act` (`classNames.run`), its text `Run` / `Send` (public demo) / `Routing` |
| `.tc-bar-proj` | the stage's `projectSlot` node, `bar-proj tc-bar-proj`, still the drawing id or `No drawing` |
| `.tc-bar-key` | the stage's static `keycap` node, literal `⌘K` (staging polish-pins asserts the text on a Linux runner, where PromptBox's own platform-aware cap would print `Ctrl+K`); `SiteRoot.jsx` still binds the real chord and focuses `.tc-bar-input` |
| the Run ladder | the OLD predicate (`session active`, `drawing`, `busy`, `job`, `routing`, `phase loading`), rung for rung, as ONE sentence through PromptBox's `disabledReason` (`web/src/site/stageRunReasons.js`, frozen `STAGE_RUN_REASONS`, held by the honesty-ladder gate); `null` is all-clear, and the stage never had an empty-prompt rung (the public demo's `Send` is asserted enabled on an empty bar) |
| `RoutePanel` | still the stage's resolver; PromptBox gets `routeActive={!!route}` so Enter in the well is a no-op while a decision shows, and `hintLane={route?.lane}` colours the scope menu's dots off the same route |
| phone legibility | `.tc-bar .bar-input .tc-bar-input { font-size: 16px }` at 600px wide, the floor `standards-surface` and `responsive-keyboard` read off `.tc-bar-input` |

PromptBox's new props, every one optional and defaulting to the console's render
(`promptBox.stage.test.jsx` pins the default element sequence against a capture taken from
origin/main BEFORE the edit): `classNames` (`bar`, `wrap`, `input`, `run`, each ADDED beside
the box's own class, never a rename), `projectSlot`, `keycap`, `disabledReason` (undefined
keeps the box's rule; a string disables Run with the sentence as its title; null enables),
`runLabel`, `routingLabel`, `placeholder`, `dropIngestEnabled`.

Two decisions recorded here because nothing else records them:

- **The G2 drop catcher is OFF on the stage** (`dropIngestEnabled={false}`). PromptBox's well
  is the console's ONE drop target for manifests, and it answers a drop with the honest
  "ingest isn't connected" strip. On the stage a drop on `.tc-bar` already means "open this
  DWG or DXF" (`cat-standards-surface.spec.mjs:68` dispatches a `DragEvent` on that node and
  expects a second upload). One gesture cannot carry two meanings, so with the flag off
  PromptBox registers no drag handlers at all and the event reaches the stage's handler
  untouched.
- **Retired copy.** The static `.tc-bar-chip` "Scope · this drawing" and the `.tc-bar-scopes`
  lane string ("plan · approve · execute · version", "message · review · run · version" in
  the demo) are gone; nothing asserted either. PromptBox's live `.bar-scope` chip (find ·
  act · build, with lane hit dots) replaces both. `landing.css` drops the `.tc-bar-caret`,
  `.tc-bar-controls` and `.tc-bar-scopes` rules with them and seats the well inside `.tc-bar`
  by specificity (styles.css loads after it).

The `SurfaceFrame` render fixture (`surfaceFrame.today-fixture.json`) was recaptured with
`SURFACE_FRAME_CAPTURE=1` after the edit and is byte-identical: the fixture's `commandBar`
slot is a sentinel render prop on both scenes by design (the frame emits what the scene
hands it, untouched), so the stage's new bar is not observable there. The stage bar's own
shape is pinned by `promptBox.stage.test.jsx` and `toolCastStageBar.test.js` instead.

## What slice 5a changed

| # | file | change |
| --- | --- | --- |
| 1 | `web/src/components/PromptBox.jsx` | the optional stage props above; `runDisabled` derived once (caller ladder when passed, else routing / empty prompt); history is appended only for a dispatch the ladder lets through; drag handlers register only while `dropIngestEnabled` |
| 2 | `web/src/site/ToolCast.jsx` | `.tc-bar-input-row` and `.tc-bar-controls` replaced by one `<PromptBox>` inside `.tc-bar`; `runOnEnter` deleted (PromptBox owns Enter); `STAGE_BAR_CLASSES` and `STAGE_BAR_KEYCAP` module constants; the drop handler, strips and `RoutePanel` untouched |
| 3 | `web/src/site/stageRunReasons.js` | `STAGE_RUN_REASONS` (frozen) and `stageRunDisabledReason()`, the old predicate as sentences in its evaluation order |
| 4 | `web/src/site/landing.css` | dead `.tc-bar-caret` / `.tc-bar-controls` / `.tc-bar-scopes` rules removed; the `.tc-bar .bar` seating block; the 600px block re-pinned at the seated specificity |
| 5 | `web/src/components/promptBox.stage.test.jsx` | the console's default sequence pinned from a pre-edit capture; the stage aliases, ladder, keycap, labels, `routeActive` Enter guard, drop catcher off, `commandLine` caret |
| 6 | `web/src/site/stageRunReasons.test.js` | exact sentences, rung order, the 64-state equivalence with the old OR, frozen map |
| 7 | `web/src/site/toolCastStageBar.test.js` | source-shape: one PromptBox inside `.tc-bar` after `RoutePanel`, the props above, the drop handler still the stage's, the hand-rolled rows gone, `landing.css` retired and seated |
| 8 | this file | this section |

Not touched: `App.jsx` (the console passes nothing new), `SurfaceFrame.jsx` (the
`commandBar` slot already existed), `RoutePanel.jsx`, `composer.js`, every e2e spec (the
36 rows are the acceptance criteria, not the thing under edit).

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

## What slice 4b changed

The continuity hoist and the tour anchors. Zero visual change on every surface: the
rendered markup, classes and testids of the continuity rail and the sign-out control are
byte-identical (`web/src/site/continuityHoist.test.jsx` pins them against a fixture captured on
the untouched tree), and their DOM position is unchanged; only their OWNER moved up the tree.

| # | file | change |
| --- | --- | --- |
| 1 | `web/src/site/continuityStore.js` (new) | the ContinuityStore context and hooks: `useContinuityHost` (the nav adopts the store's host node), `useContinuityPublish` (the scene's frame publishes `activeSurface`, `workspaceProject`, `catalog`, `signedIn`, `onSignOut`), the snapshot normalizer and the host factory. Fails closed outside a store |
| 2 | `web/src/site/ContinuityStore.jsx` (new) | the provider `SiteRoot` mounts ONCE, above the scene ternary, inside `DrawingIdentityProvider`. Owns one host `<div class="tc-continuity-host">` (`display: contents`, so no box) for the life of the page and portals `<ContinuityRail>` + `<AccountSignOut>` into it. Holds a snapshot, never a controller: no second session or catalog controller exists above the scenes |
| 3 | `web/src/site/SiteRoot.jsx` | wraps the scene ternary in `<ContinuityStore search={BOOT_SEARCH}>` (the boot search string seeds `activeSurface`); the arm shape the one-shell pins assert is unchanged |
| 4 | `web/src/components/ProductSurfaceTabs.jsx` | no longer renders the rail or the sign-out, and no longer takes `workspaceProject` / `catalog` / `signedIn` / `onSignOut`; a layout effect adopts the store's host right after the tablist, so the nav's children are still tablist, rail, sign-out. `ContinuityRail` and `AccountSignOut` stay exported from here (App's header still imports the latter) |
| 5 | `web/src/site/SurfaceFrame.jsx` | the frame is the ONE publisher for its scene (`useContinuityPublish`); its Tabs slot passes only `activeSurface` / `states` / `onSelect` |
| 6 | `web/src/site/landing.css` | `.tc-continuity-host { display: contents; }` |
| 7 | `web/src/demo/DemoTour.jsx` | `anchors` prop; `resolveTourTarget` resolves `[data-tour="<id>"]` first (slug-validated), className chain second; exported for its unit test |
| 8 | `web/src/site/productSurfaces.js` | `tourAnchors` filled on all five records as `{ console, stage }` (`CONSOLE_TOUR_ANCHORS`, `STAGE_TOUR_ANCHORS`); the step arrays are untouched |
| 9 | both shells | additive `data-tour` attributes: console `shell` (`App.jsx .app`), `viewer` (`Viewer.jsx .viewer-canvas`), `command-bar` (`PromptBox.jsx .bar`); stage `shell` (`StageScene.jsx .stage-root`), `viewer` (`StageLayer.jsx .stage-viewer`), `command-bar` (`ToolCast.jsx .tc-bar`), `right-rail` (`.tc-rail-r`). Both `DemoTour` mounts pass the contract map. A `left-rail` anchor (`NavRail.jsx aside.nav`, `ToolCast.jsx .tc-operator-rail` x2) and the console's own `right-rail` (`JobRail.jsx aside.rail`) were added in this slice's first cut and then REMOVED (lens-2 review): no step on either shell ever spotlighted the nav rail, and the console never spotlights the job rail either, so both were dead vocabulary — attribute present, zero consumers, a drift class the gate below now also catches directly |
| 10 | `web/scripts/check_tour_anchors.mjs` (new) | the gate (`check:tour-anchors`, suite `web-tour-anchors`, and in `dispatch/run-local-ci.sh` beside `check_tourscript.mjs`): vocabulary, step ids, per-shell presence in source, shape, resolution order, no orphaned source vocabulary (the reverse of presence), positive controls |
| 11 | `web/e2e/local/continuity-cross-scene.spec.mjs` (new) | `/try` -> capture the rail's label -> `/app` -> attached, the SAME node (a JS expando set on `/try` reads back), the shared parts of the label byte-equal (the static label and the tenant catalog item; the project item is each scene's own F-9 derivation, and on the managed stack the stage's operator identity starts with no drawing while the console boots its drawing) -> `/try` -> the full label byte-equal again, and a coach dismissed before the crossing is not re-offered; rail ON and rail OFF. The carry of the stage's value across the crossing window is a timing race in a browser, so it is pinned in `continuityHoist.test.jsx` instead. `one-shell-mount.spec.mjs`'s W4b row now asserts the rail attached-and-hidden on CAD |
| 12 | this file | the `tourAnchors` rows and this section |

### FirstRunCoach is NOT hoisted (recorded deviation)

The slice plan's line listed the coach with the rail. It stays scene-scoped, on purpose: its
visibility contract is `sessionAuthRequired && !sessionWasActiveThisPageLoad`, both of which
are the stage's own facts; the module-scope `sessionWasActiveThisPageLoad` flag
(`ToolCast.jsx`) already survives the `/app` crossing, and its dismissal is a localStorage key
(`leaf.coach.dismissed.v1`) that survives everything. The hp01 suite (16 rows at runtime) pins its entrance and
exit choreography against the tool scene's `data-cast` fade, which a SiteRoot-level mount would
have to re-derive. It renders null on scene `app`, as before, and the new e2e row proves the
dismissal carries across the crossing without the hoist.

## Daily-session set

### Usage-telemetry consent (slice 13c)

The studio collects two classes of telemetry event and they are gated differently. **Product
events** are what the app did (a run finished, a version restored, an exception was caught):
they are the operational record, they describe the system rather than the person, and their
only gate is the build-time kill switch `VITE_TELEMETRY_DISABLED=1`, exactly as before this
slice. **Usage-shaped events** are what a person typed and picked (search queries, menu
actions, palette picks; the emitters slices 10-13 add): they describe the viewer, so **no
usage-shaped data is collected before the Plan panel's "Usage telemetry" switch is on.** The
grant is per browser, stored under a versioned key (`leaf.telemetry.usage_consent.v1`,
`web/src/lib/telemetryConsent.js`) that is bumped rather than reinterpreted whenever the
meaning of the yes changes; absent, malformed, or unreadable storage all read as NOT
consented. The rule lives in exactly one place, `buildEvent` in `web/src/telemetry.js`, and it
fails closed: an event whose class is anything but the literal `product` is treated as
usage-shaped and needs consent. A refused event is never built and never queued, so granting
consent later ships nothing that was refused while the switch was off. **A revoke reaches
events that are already queued, not just the next one built:** every buffered event carries
its class on a symbol key that JSON.stringify cannot serialize, and a revoke purges the
usage-class events out of EVERY place one can be waiting: the shared buffer, a batch whose
POST is still on the wire (registered before the request leaves, so a revoke during the
request still decides what its one retry may carry), and any batch a failed POST already
handed to the 2 s retry. All of those are held outside the buffer, which is why a buffer-only
purge would miss them. Each send seam (the 5 s flush, `flushNow`, the pagehide beacon, the
2 s retry) re-checks consent on the way out as well. So a usage event queued a moment before
the switch went off is destroyed rather than posted, and a re-grant inside the retry window
cannot resurrect it. The one thing a revoke cannot recall is a request that already left the
browser under a valid grant; that request was consented when it was sent. The switch is the permission, not a data tap:
no usage emitter exists in the tree yet, so its copy is present-tense honest about that
("Allow sharing how you use the studio (menu picks, searches) once those signals exist"),
and the emitters slices 10-13 add are what start flowing under an existing yes. When the
build-time kill switch is on, nothing usage-shaped can leave whatever is stored, and the row
says exactly that in text ("Telemetry is off for this build.") rather than being a dead
control: a stored grant stays visible and stays revocable, with the second reason naming
what a build flag does not change ("Your saved yes is kept and would resume in a build
without this fence. Turn the switch off to take it back now."), while granting under the
fence is refused.
