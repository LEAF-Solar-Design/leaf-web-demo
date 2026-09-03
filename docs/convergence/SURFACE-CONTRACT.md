# The Surface Contract

Standardization slice 1 of 13. Plan: `C:/Users/ehaug/.claude/plans/staged-wiggling-fairy.md`,
section "The Surface Contract", element table row 1.

This slice freezes the contract as DATA on `web/src/site/productSurfaces.js`, with every
default EQUAL TO TODAY. No component reads the new `contract` object yet, so this slice
cannot change a pixel; slice 2 repoints the inline gates at the manifest.

The operator rule the contract exists to serve, verbatim:

> "nothing HAS to be there, but everything needs to be ABLE to be there, according to
> agent/operator decisions, effortlessly as a key/staple functionality"

`docs/convergence/ACCEPTANCE.md` is frozen and is not edited by this slice.

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

| field | type | meaning | where it is defined today |
| --- | --- | --- | --- |
| `ground` | `'drawing' \| 'board' \| 'device-stage'` | the canvas kind under the shell | `web/src/site/SurfaceGrounds.jsx:99` `DRAWING_SURFACES = new Set(['cad','solar'])`, read by `groundShowsDrawing()` at `:102`; the two non-drawing kinds are named in `web/e2e/local/one-shell-mount.spec.mjs:131` and rendered by `ProjectBoardGround` (`SurfaceGrounds.jsx:124`) and `DeviceGround` (`:213`) |
| `chrome.productFrame` | boolean | the `<ProductSurfaceFrame>` wrapper renders | `web/src/App.jsx:2798` `activeSurface !== 'cad'` |
| `chrome.workspaceCard` | boolean | the drawing workspace card is visible | `web/src/App.jsx:2819` `display: activeSurface === 'cad' \|\| activeSurface === 'solar' ? undefined : 'none'` |
| `chrome.cockpit` | boolean | the drafting cockpit bands mount | `web/src/App.jsx:2242` `const drafting = groundShowsDrawing(activeSurface)`, consumed as `studioGround && drafting` at `:2526` (top band) and `:2858` (ribbon), and ~20 further sites |
| `chrome.stageBranch` | `'cad' \| 'ios' \| 'frame'` | which arm of the stage's ternary this surface takes | `web/src/site/ToolCast.jsx:1417` (`cad`), `:2060` (`ios`), `:2122` (frame fallthrough) |
| `chrome.projectSlot` | `'ios-surface' \| null` | what fills the frame's project slot | `web/src/App.jsx:2806-2807` |
| `toolbar.ribbon` | boolean | `DraftingRibbon` mounts | `web/src/App.jsx:2858` |
| `toolbar.home` | ribbon tab id \| null | the tab the ribbon opens on | `web/src/App.jsx:2225` `useState('draw')`; the id vocabulary is `web/src/site/CockpitTopBand.jsx:17-26` `RIBBON_TABS` |
| `toolbar.quick` | array \| null | quick-access ids | undeclared: `CockpitTopBand.jsx:52` takes `before`/`after` as PROPS, built imperatively at `web/src/App.jsx:2417-2432`. There is no data source to read ids from, so `null` on every surface |
| `rails.left` | `'spine' \| 'nav' \| 'none'` | the left nav rail's posture | `web/src/App.jsx:2247` `navSpine = !!studioGround && drafting && !navExpanded && wideViewport`; the rail itself is `:2609` |
| `rails.right` | `'job-spine' \| 'job-rail' \| 'none'` | the job monitor's posture | `web/src/App.jsx:3414` `spine={!!studioGround && drafting && wideViewport && !jobRailExpanded}`; `JobRail` mounts at `:3402` |
| `rails.dock` | array of section ids \| null | the properties dock's sections | mount gate `web/src/App.jsx:3148`; section order `web/src/site/PropertiesDock.jsx:132-142` |
| `commandLine` | boolean | the docked one-line "Command:" mode | `web/src/App.jsx:3376` `commandLine={!!studioGround && drafting}` |
| `authoring` | boolean \| null | the author/build lane is reachable | `web/src/App.jsx:2699` `<AuthorPanel>`, inside the nav rail (`:2609`) which is not surface-gated |
| `versions` | `'drawing' \| 'none' \| null` | the version source and its restore route | `web/src/App.jsx:3001` `<VersionHistory>`, which sits inside the workspace card whose display gate is `:2819` |
| `conversations.scope` | `'project' \| 'drawing' \| null` | what an AI conversation is scoped to | `web/src/converse.js:129-134` `sessionCacheKey(drawingId, projectId)` caches ONE session per project+drawing pair; `ConversePanel` mounts at `web/src/App.jsx:3263` with no surface gate |
| `integrations` | null | which mounts show, how a new one is linked | undeclared: the only link surface is the header's Claude account panel (`web/src/App.jsx:2594`), which is global, not per-surface |
| `builds.routes` | array | what this surface can launch | `web/src/App.jsx:2674` `ToolsPanel onRequestRun` -> `onRequestCatalogRun`, mounted on every surface. No marathon route exists in this client at all |
| `contextMenu` | array | element kinds exposing "configure / ask the agent" | `[]` on every surface: zero `contextmenu` / `onContextMenu` handlers exist anywhere under `web/src` (ripgrep, 2026-09-03). Declared empty, not undeclared |
| `shortcuts` | null | per-surface keyboard/touch triggers | undeclared: no per-surface shortcut registry exists |
| `entitlements` | null | per-surface per-tool entitlement | undeclared: `web/src/components/EntitlementGate.jsx:15` `ROWS` are TIER capability keys (`run_read`, `run_write`, `build`, `converse`), never per-surface |
| `resetOn` | null | scope reset on tenant/project switch | undeclared: no effect in `App.jsx` keys off `activeSurface` to reset scope |
| `a11y` | null | per-surface accessibility declarations | undeclared |
| `tourAnchors` | null | tour anchor ids | undeclared: the tour is mock-gated (`web/src/App.jsx:2722`), not surface data |

## The matrix (console values, equal to today)

| slot | browser | cad | solar | ios |
| --- | --- | --- | --- | --- |
| `ground` | `board` | `drawing` | `drawing` | `device-stage` |
| `chrome.productFrame` | `true` | `false` | `true` | `true` |
| `chrome.workspaceCard` | `false` | `true` | `true` | `false` |
| `chrome.cockpit` | `false` | `true` | `true` | `false` |
| `chrome.stageBranch` | `frame` | `cad` | `frame` | `ios` |
| `chrome.projectSlot` | `null` | `null` | `null` | `ios-surface` |
| `toolbar.ribbon` | `false` | `true` | `true` | `false` |
| `toolbar.home` | `null` | `draw` | `draw` | `null` |
| `toolbar.quick` | `null` | `null` | `null` | `null` |
| `rails.left` | `nav` | `spine` | `spine` | `nav` |
| `rails.right` | `job-rail` | `job-spine` | `job-spine` | `job-rail` |
| `rails.dock` | `null` | `[layers, drawing, selection, plan]` | `[layers, drawing, selection, plan]` | `null` |
| `commandLine` | `false` | `true` | `true` | `false` |
| `authoring` | `true` | `true` | `true` | `true` |
| `versions` | `none` | `drawing` | `drawing` | `none` |
| `conversations.scope` | `drawing` | `drawing` | `drawing` | `drawing` |
| `integrations` | `null` | `null` | `null` | `null` |
| `builds.routes` | `[one-shot]` | `[one-shot]` | `[one-shot]` | `[one-shot]` |
| `contextMenu` | `[]` | `[]` | `[]` | `[]` |
| `shortcuts` / `entitlements` / `resetOn` / `a11y` / `tourAnchors` | `null` | `null` | `null` | `null` |

### Notes on values that surprise

- **`rails.left`, `rails.right` and `rails.dock` are the WIDE-VIEWPORT defaults.** All three
  gates carry `wideViewport` (`App.jsx:2247`, `:3414`, `:3148`), which is
  `matchMedia('(min-width: 981px)')` (`App.jsx:2232-2233`). At or below 980px the console stacks
  into one column and the postures neutralise: the nav rail expands, the job rail expands,
  and the properties dock is replaced by its inline arm (`App.jsx:3170`). The manifest
  declares the wide default; a responsive dimension is a later slice.
- **`rails.left` and `rails.right` are also first-render values.** `navExpanded`
  (`App.jsx:2215`) and `jobRailExpanded` (`App.jsx:2222`) both start `false` and are
  in-memory only, so the posture resets per page load by design.
- **`chrome.productFrame` is `true` on solar.** `App.jsx:2798` tests `!== 'cad'`, so the
  product frame renders over the shown workspace card on Solar CAD. That is today's
  behaviour, pinned as-is. It is a candidate for slice 2 to make declarative, not a defect
  this slice may quietly fix.
- **`chrome.cockpit` equals `ground === 'drawing'` today.** It is declared separately anyway,
  per the operator rule: every slot must be declarable per surface, so a future surface can
  take a drawing ground without the drafting cockpit, or the reverse.
- **`versions` is `none` on browser and iOS because of a display gate, not an unmount.**
  `VersionHistory` (`App.jsx:3001`) is inside the workspace card, which is hidden with
  `display: none` (`App.jsx:2819`) rather than unmounted, so live drawing, lock and job state
  survive a tab switch. The user-visible answer is still "no version history here".
- **`authoring` is `true` on every surface.** `AuthorPanel` (`App.jsx:2699`) sits in the nav
  rail, which is not surface-gated. On cad and solar the rail collapses to a spine by
  default, so the panel is reached through the ribbon's author cluster (`App.jsx:2393`)
  rather than the rail itself.
- **`builds.routes` carries no `ship-lane` on iOS.** See the divergence table: the console
  mounts `IosSurface`, which is props-only with no launch control.
- **`conversations.scope` is `drawing` even where there is no drawing.**
  `sessionCacheKey` falls back to the literal `'default'` drawing key
  (`converse.js:132`), so the scope shape is drawing-keyed on every surface.

## Divergences between the two shells

The console (`App.jsx`, `/app`) and the stage (`ToolCast.jsx`, `/try`) disagree on three
rows. The manifest carries the console value; each row below is a slice-2 item.

| # | surface | slot | console (`App.jsx`) | stage (`ToolCast.jsx`) | slice-2 action |
| --- | --- | --- | --- | --- | --- |
| D1 | ios | `chrome.productFrame` / `chrome.projectSlot` | `true` with `projectSlot: 'ios-surface'`: the frame renders and `IosSurface` fills its slot (`App.jsx:2798`, `:2806-2807`) | `false`: the ios arm of the ternary (`ToolCast.jsx:2060`) renders its own top cluster and `tc-operator-rail` ship lane instead of the frame | one wrapper renders the declared frame; the ios ship lane becomes a declared slot, not a hard-coded branch |
| D2 | ios | `builds.routes` | `['one-shot']`: `IosSurface.jsx:3-4` is props-only ("no fetch, no polling, no client-side state math") and carries NO launch control | additionally a real ship-lane launch: `ToolCast.jsx:2097` `data-testid="ios-ship-launch"` -> `launchIosShip` | promote `ship-lane` to a declared route and mount ONE control from it, so the two shells stop disagreeing about whether a user can ship |
| D3 | cad | `authoring` reach | `true` on all four surfaces (`App.jsx:2699`, un-gated rail) | cad only: the Workspace rail's Author tab and `CapabilityCatalog` live inside the `activeSurface === 'cad'` arm (`ToolCast.jsx:1417`, `:1506`) | the author lane mounts from the contract on every surface it declares, scoped to that surface's `familyIds` |

A fourth, weaker mismatch: solar takes the stage's frame fallthrough
(`ToolCast.jsx:2122`), so the stage gives Solar CAD no drafting cockpit at all while the
console does (`chrome.cockpit: true`). Recorded here so slice 2 resolves it deliberately.

## Hardcoded forever

Three things are NOT contract slots and must never become configurable by a surface record.
The contract writes this into itself so a later slice cannot quietly relax it.

1. **The engine-session mount and its boundary.** ONE engine session owner
   (`docs/convergence/ACCEPTANCE.md`, "Engine-session ownership"): the engine-session store
   owns `EngineBoundary` construction, worker lifetime, and save target. In the console that
   is the single `EngineSessionProvider` wrap at `web/src/App.jsx:2514-2515`, applied through
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

## What this slice changed

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

Nothing else. No consumer reads `contract` in this slice.
