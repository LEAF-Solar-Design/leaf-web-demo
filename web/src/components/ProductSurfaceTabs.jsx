import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { PRODUCT_SURFACES, SHARED_WORKSPACE_CAPABILITIES, productSurface, surfaceContract } from '../site/productSurfaces.js'
import { EMPTY_WORKSPACE_PROJECT, WORKSPACE_PROJECT_COPY } from '../site/workspaceProjectState.js'
import { useContinuityHost } from '../site/continuityStore.js'
import { moveRovingTab } from '../lib/roving.js'

// F-8: the continuity layer, made visible. Lives in the always-mounted nav so
// it NEVER remounts on a profile switch (that persistence is the point, and
// the test asserts node identity across switches). Renders ONLY live data:
// the open project and the same catalog fold every surface consumes (F-7).
// A surface change pulses it (class toggle, never a remount); the pulse and
// every other F-8 motion is disabled under prefers-reduced-motion in CSS.
//
// Slice 4b: RENDERED by site/ContinuityStore.jsx (mounted once by SiteRoot,
// above the scene ternary), not by the tabs below. The store portals this
// rail and AccountSignOut into a host node the nav ADOPTS (see
// ProductSurfaceTabs), so the never-remounts contract now also holds across
// the /try <-> /app scene crossing. The component itself is unchanged.
export function ContinuityRail({ activeSurface, workspaceProject = null, catalog }) {
  const [pulse, setPulse] = useState(false)
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    setPulse(true)
    const t = setTimeout(() => setPulse(false), 600)
    return () => clearTimeout(t)
  }, [activeSurface])
  const families = catalog?.families || []
  const capabilityCount = families.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )
  // The rail names BOTH concepts, so a mounted drawing can never read as
  // "nothing is open" (the 2026-09-01 pilot contradiction). It CONSUMES the
  // derivation and never performs one: the previous revision accepted a legacy
  // `project` string and synthesized a placeholder openProjectId from it,
  // which fabricated an identifier inside the fix for fabricated state. That
  // prop is gone; callers pass the derived state or nothing.
  const state = workspaceProject || EMPTY_WORKSPACE_PROJECT
  return (
    <div className="tc-continuity" data-testid="continuity-rail" data-pulse={pulse ? 'true' : 'false'} data-project-state={state.kind}>
      <span className="tc-continuity-label">Carried across every profile</span>
      {state.kind === 'drawing-only' && (
        <span className="tc-continuity-item">drawing · <strong>{state.drawingName}</strong></span>
      )}
      <span className="tc-continuity-item">
        {state.kind === 'project' ? <>project · <strong>{state.label}</strong></> : state.railLabel}
      </span>
      {families.length > 0 && (
        <span className="tc-continuity-item">
          catalog · <strong>{families.length} {families.length === 1 ? 'family' : 'families'} / {capabilityCount} tools</strong>
        </span>
      )}
      <span className="tc-continuity-item tc-continuity-static">conversation · approvals · runs · receipts</span>
    </div>
  )
}

export function AccountSignOut({ signedIn = false, onSignOut = null }) {
  if (!signedIn || typeof onSignOut !== 'function') return null
  return (
    <button type="button" className="chip-danger tc-account-signout" onClick={onSignOut}>
      Sign out
    </button>
  )
}

// Slice 4b: the tabs no longer take `workspaceProject` / `catalog` /
// `signedIn` / `onSignOut`. The scene's SurfaceFrame publishes those to the
// ContinuityStore (site/continuityStore.js), which renders the rail and the
// sign-out into ONE host node; this nav adopts that node right after the
// tablist, so the DOM under `.tc-product-nav` is what it always was:
// tablist, rail, sign-out. Outside a store (a tabs band rendered alone) the
// host is null and the nav renders its tablist and nothing else: fails closed.
export default function ProductSurfaceTabs({ activeSurface, states, onSelect }) {
  const host = useContinuityHost()
  const navRef = useRef(null)
  // Layout effect: the host is in place before paint, in the same commit the
  // frame publishes (also a layout effect, and an ancestor's, so it runs
  // after this one). On unmount the host is detached with its children
  // intact and the next scene's nav adopts the SAME node, which is the
  // cross-scene never-remounts contract (continuityHoist.test.jsx). React
  // never reconciles the nav's own children after mount (the tablist is
  // unconditional), so a foreign last child is safe here by construction.
  useLayoutEffect(() => {
    const nav = navRef.current
    if (!nav || !host) return undefined
    nav.appendChild(host)
    return () => { if (host.parentNode === nav) nav.removeChild(host) }
  }, [host])
  return (
    <nav ref={navRef} className="tc-product-nav" data-cast="tool" aria-label="Product workspace">
      <div className="tc-product-tabs" role="tablist" aria-label="Workspace profile" onKeyDown={moveRovingTab}>
        {/* One tab per surface that DECLARES one (slice 5b): the band reads
            contract.chrome.tab instead of assuming every record is a tab, so
            Sheets can be a manifest row without appearing here. All four
            studio surfaces declare true, so this renders what it rendered. */}
        {PRODUCT_SURFACES.filter(({ contract }) => contract.chrome.tab).map((surface) => {
          const selected = surface.id === activeSurface
          const status = states[surface.id]
          // aria-controls points at #product-surface-panel, which is the
          // ProductSurfaceFrame — and a surface that declares no frame
          // renders no such element. A tab whose aria-controls names a
          // missing id is a broken reference to a screen reader, so the
          // attribute is carried by the surfaces that HAVE the panel and
          // omitted by the ones that do not (cad always; solar since the P1
          // pass). Read from the contract, never from a surface id.
          const controlsPanel = surface.contract.chrome.productFrame
          return (
            <button
              key={surface.id}
              id={`product-surface-tab-${surface.id}`}
              type="button"
              role="tab"
              aria-label={surface.label}
              aria-selected={selected}
              aria-controls={controlsPanel ? 'product-surface-panel' : undefined}
              tabIndex={selected ? 0 : -1}
              data-surface={surface.id}
              onClick={() => onSelect(surface.id)}
            >
              <span>{surface.label}</span>
              <small data-state={status.state}>{status.label}</small>
            </button>
          )
        })}
      </div>
      {/* The continuity rail and the persistent identity control (the ONE
          place in the always-mounted nav that lets a signed-in operator
          leave; 2026-09-02 reconciliation, row B11, same signOut path the
          Trust panel drawer calls, never raw logout()) stand HERE, right
          after the tablist, inside the host node the layout effect above
          adopts. Slice 4b moved their owner to SiteRoot's ContinuityStore,
          not their place. */}
    </nav>
  )
}

// Live per-surface projection of the tenant capability catalog (F-7): the
// SAME fold the CAD rail renders (GET /api/capabilities families), filtered
// by the surface's familyIds — never a second data source, never hardcoded
// capability strings. Loading renders a quiet skeleton; a catalog error
// degrades to an honest note (no fake list). Fails closed on absent data.
export function SurfaceCapabilities({ surface, catalog, catalogError }) {
  const families = catalog?.families || []
  const featured = surface.familyIds
    ? families.filter((family) => surface.familyIds.includes(family.family_id))
    : families
  const capabilityCount = featured.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )
  if (catalogError) {
    return (
      <div data-testid="surface-capabilities-error" className="tc-product-note">
        Couldn’t load the live capability catalog · {catalogError}
      </div>
    )
  }
  if (!families.length) {
    return (
      <div className="skeleton-stack" aria-label="Loading capability families" data-testid="surface-capabilities-loading">
        <div className="skeleton-row" />
        <div className="skeleton-row" />
      </div>
    )
  }
  // The catalog is live but none of this surface's featured families are in
  // this tenant's fold yet: say so, and still show the live whole-catalog
  // count — never a bare zero that reads as a broken load.
  if (!featured.length) {
    const totalCount = families.reduce(
      (count, family) => count + (family.capabilities?.length || 0),
      0,
    )
    return (
      <div data-testid="surface-capabilities-live">
        <p className="tc-product-catalog-count">
          No {surface.familyIds.join(' or ')} tools are registered in this tenant’s catalog yet
          · {families.length} {families.length === 1 ? 'family' : 'families'} · {totalCount} capabilities live
        </p>
      </div>
    )
  }
  return (
    <div data-testid="surface-capabilities-live">
      <p className="tc-product-catalog-count">
        {featured.length} {featured.length === 1 ? 'family' : 'families'} · {capabilityCount} capabilities · live tenant catalog
      </p>
      <ul>
        {featured.map((family) => (
          <li key={family.family_id} data-family={family.family_id}>
            <strong>{family.label}</strong>
            {(family.capabilities || []).length > 0 && (
              <span> · {(family.capabilities || []).map((c) => c.label || c.name).join(', ')}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

// The Browser / Solar CAD project line, made legible (the F-9 fix). Renders the
// SAME derivation the header chip and the rail render, so the three can no
// longer disagree. When a drawing is mounted but no workspace project is open
// it says which of the two is missing, why that matters, and offers the one
// action that closes the gap — never a bare "No project open" next to a header
// that is plainly showing an open, editable drawing.
export function WorkspaceProjectSlot({ state, onCreateProject }) {
  if (!state) return null
  if (state.kind === 'project') {
    return <span className="dim" data-testid="surface-project-state" data-project-state="project">{state.label}</span>
  }
  const action = state.action
  // ONE disabled decision, and the reason is derived from the SAME value.
  // sol-critic finding 3: these were computed separately, so a state that
  // allowed the create while the caller supplied no handler rendered a
  // disabled button with NO stated blocker -- exactly the dead end this slot
  // replaced. A missing handler is now a stated blocker like any other.
  const missingHandler = Boolean(action) && !onCreateProject
  const disabled = Boolean(action?.disabled) || missingHandler
  const reason = action?.disabled
    ? action.reason
    : missingHandler
      ? WORKSPACE_PROJECT_COPY.reasonNoHandler
      : null
  const showReason = Boolean(disabled && reason)
  // A disabled button's `title` is not reliably announced, so the blocker is
  // rendered as real text and ASSOCIATED with the control — a screen-reader
  // user must not be the only one who cannot find out why it is dead.
  const reasonId = showReason ? 'surface-project-reason' : undefined
  return (
    <div className="tc-product-project-state" data-testid="surface-project-state" data-project-state={state.kind}>
      <strong>{state.headline}</strong>
      {state.explainer && <p>{state.explainer}</p>}
      {action && (
        <>
          <button
            type="button"
            className="chip-act tc-product-project-act"
            data-testid="surface-project-action"
            disabled={disabled}
            aria-describedby={reasonId}
            onClick={() => onCreateProject?.(action.projectName)}
          >
            {action.label}
          </button>
          {/* An unexplained disabled button is the same dead end as the bare
              state line it replaced, so the blocker is always spelled out. */}
          {showReason && (
            <span id={reasonId} className="tc-product-project-reason" data-testid="surface-project-reason">{reason}</span>
          )}
        </>
      )}
    </div>
  )
}

// `workspaceProject` is rendered by the FRAME, not handed in as a slot by each
// caller. sol-critic RED on PR #888 caught why: /app passed WorkspaceProjectSlot
// into projectSlot while /try passed its header switcher, so the /try Browser
// and Solar cards silently lost the explainer and the create action. A slot the
// caller must remember to fill correctly is a contract no test of this
// component can enforce; owning it here means every surface gets it by
// construction. projectSlot stays for what genuinely IS caller-specific -- the
// iOS ship lane, /try's switcher chip -- and renders above it.
// Per-surface COPY under the capability columns. Copy, not chrome: it chooses
// prose, never whether an element mounts, so it lives in a lookup keyed by id
// rather than behind a surface-id comparison (the surfaceGates scan forbids
// those in every shell file, this one included). Text is byte-identical to
// the three inline notes it replaced.
const SURFACE_NOTES = Object.freeze({
  browser: (workspaceProject) => (
    <p className="tc-product-note" data-testid="browser-composition-note">
      {workspaceProject.kind === 'project'
        ? 'Project-scoped files, conversation, and browser composition are live on the shared identity and project rail.'
        : 'A workspace project adds project-scoped files, conversation, and browser composition on the shared identity and project rail.'}
    </p>
  ),
  solar: () => <p className="tc-product-note">Solar work runs the shared tenant catalog’s stringing and placement families against the versioned Leaf Automation solar template in the CAD workspace.</p>,
  ios: () => <p className="tc-product-note">A mounted Apple grant and terminal ship-lane readiness receipt are required. This browser never asks for Apple credentials.</p>,
})

function surfaceNote(surfaceId, workspaceProject) {
  const render = Object.prototype.hasOwnProperty.call(SURFACE_NOTES, surfaceId) ? SURFACE_NOTES[surfaceId] : null
  return render ? render(workspaceProject) : null
}

export function ProductSurfaceFrame({
  activeSurface, states, projectSlot, catalog, catalogError,
  workspaceProject = EMPTY_WORKSPACE_PROJECT, onCreateProject = null,
}) {
  const surface = productSurface(activeSurface)
  const status = states[surface.id]
  // The surface whose contract mounts the ios ship lane in projectSlot owns its
  // whole project line; every other surface ALWAYS renders a state: sol-critic
  // finding 2 was that a null default let a call site drop the Browser/Solar
  // state silently. Omitting the prop now degrades to the honest empty state,
  // never to nothing. Read from the contract (slice 2 fix-forward): this was
  // the one mount gate still keyed on a surface id after #978.
  const showProjectState = surfaceContract(surface.id).chrome.projectSlot !== 'ios-surface'
  return (
    <section
      id="product-surface-panel"
      className="tc-product-frame"
      role="tabpanel"
      aria-labelledby={`product-surface-tab-${surface.id}`}
      data-cast="tool"
      data-surface={surface.id}
    >
      {/* F-8 morph: the key remounts this wrapper per surface, so the CSS
          enter animation runs on every profile switch. The section above
          stays put (stable tabpanel identity); prefers-reduced-motion
          disables the animation in CSS, never in JS. */}
      <div key={surface.id} className="tc-product-morph">
      <div className="tc-product-frame-head">
        <span>{surface.eyebrow}</span>
        <strong>{status.label}</strong>
      </div>
      <h1>{surface.title}</h1>
      <p>{surface.description}</p>
      <div className="tc-product-project">
        {projectSlot}
        {showProjectState && (
          <WorkspaceProjectSlot state={workspaceProject} onCreateProject={onCreateProject} />
        )}
      </div>
      <div className="tc-product-columns">
        <div>
          <h2>Shared everywhere</h2>
          <ul>
            {SHARED_WORKSPACE_CAPABILITIES.map((capability) => <li key={capability}>{capability}</li>)}
          </ul>
        </div>
        <div>
          <h2>{surface.label} capabilities</h2>
          <SurfaceCapabilities surface={surface} catalog={catalog} catalogError={catalogError} />
        </div>
      </div>
      {/* Present tense only when a workspace project is genuinely open (kind
          === 'project'). PR #888 fixed the same contradiction one card up
          (WorkspaceProjectSlot's drawing-only explainer) but left this note
          unconditional, so a drawing-only screen claimed these were already
          live one paragraph below saying they were not. */}
      {surfaceNote(surface.id, workspaceProject)}
      {/* No "Open ..." buttons here (operator directive 2026-09-01): the TABS
          are the navigation — each tab opens its surface inline, client-side.
          Solar renders the live CAD workspace beneath this frame; iOS mounts
          its ship lane in projectSlot above. */}
      </div>
    </section>
  )
}
