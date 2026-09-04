// ---------------------------------------------------------------------------
// THE SURFACE FRAME (standardization slice 4a, docs/convergence/
// SURFACE-CONTRACT.md). ONE component both scenes mount — App.jsx (the
// console) and site/ToolCast.jsx (the stage) — that owns every shell-chrome
// decision the two scenes used to hand-roll twice, each from its own copy of
// the same predicate.
//
// ZERO VISUAL CHANGE is the hard contract of this slice. That is what dictates
// the shape below, so read this before proposing a "simpler" monolith:
//
//   The six shared shell elements do NOT sit next to each other in either
//   scene. In App.jsx today the tabs are inside <main> (App.jsx:2868), the
//   product frame right after it (:2898), the entitlement panel ~480 lines
//   later at the end of <main> (:3376) or inside the properties dock (:3263),
//   the toast in a sibling div (:3502), the job rail a sibling of THAT
//   (:3505), and the drafting toggles inside <footer> (:3578). ToolCast is
//   scattered the same way (:1422, :1759, :1893, :2040, :2139). A single
//   <SurfaceFrame> element that RENDERED all six in one place would move every
//   one of them to a new depth and a new order, which is a DOM rewrite, not a
//   refactor, and the one-shell byte-identity rows would be right to fail it.
//
// So SurfaceFrame contributes NO DOM of its own. It is a provider that each
// scene mounts ONCE around its whole tree, carrying the normalized prop
// contract, plus a set of SLOTS the scene mounts where that element already
// stands. The gates move; the elements do not. What this buys is exactly what
// the slice is for: every chrome decision for both scenes is stated once, in
// this file, read off the Surface Contract, and a seventh consumer gets it
// right by mounting a slot instead of by copying a predicate.
//
// House rule inherited from slice 2 and pinned by src/site/surfaceGates.test.js:
// nothing here compares a surface id to a string literal. Every gate reads a
// declared slot off surfaceContract(activeSurface).
// ---------------------------------------------------------------------------
import { createContext, useContext } from 'react'

import EntitlementGate from '../components/EntitlementGate.jsx'
import JobRailComponent from '../components/JobRail.jsx'
import ProductSurfaceTabs, { ProductSurfaceFrame } from '../components/ProductSurfaceTabs.jsx'
import ToastComponent from '../components/Toast.jsx'

import { StatusToggles } from './DrawingCockpit.jsx'
import { surfaceContract } from './productSurfaces.js'

// Undefined (not null) is the "no provider" sentinel: a slot mounted outside a
// frame renders nothing rather than throwing into the scene's tree. Fails
// closed on every slot, by construction, with one branch.
const SurfaceFrameContext = createContext(undefined)

/**
 * The frame's normalized view of its scene. Read by the slots below; exported
 * for the slices that grow this contract (4b's continuity hoist reads
 * `session` and `workspaceProject` from here). Returns undefined outside a
 * provider — every caller must treat that as "render nothing".
 */
export function useSurfaceFrame() {
  return useContext(SurfaceFrameContext)
}

// The console passes a posture; the stage passes null. That is the ONE
// discriminator the frame needs, and it is a real difference (the console has
// a studio rail with expand/collapse postures; the stage has none), never a
// scene name smuggled in as data.
const isConsole = (frame) => !!frame && frame.posture !== null && frame.posture !== undefined

// Fail closed: a slot whose frame is missing, or whose surface record cannot
// be resolved, renders nothing instead of throwing. `surfaceContract` already
// normalizes an unknown id to the default surface, so this only ever catches a
// slot mounted outside a provider.
function useSlot() {
  const frame = useSurfaceFrame()
  if (!frame) return null
  return frame
}

/**
 * SurfaceFrame — mounted ONCE per scene, wrapping that scene's whole tree.
 * Renders a context provider and its children: no element, no wrapper div, no
 * fragment boundary that React can observe in the DOM.
 *
 * Prop contract (slice 4a spec §4a). Each scene aliases its local names at
 * this call boundary — ToolCast's `platformSession` -> `session`,
 * `capabilityCatalog` -> `catalog` — so the frame never learns a scene's
 * private vocabulary:
 *
 *   activeSurface     the declared surface id driving every gate below
 *   states            productSurfaceStates() result for the tabs
 *   catalog           the ONE tenant capability catalog (F-7)
 *   catalogError      its load error, or null
 *   workspaceProject  derived project state for the tabs and the frame
 *   onSelect          tab selection handler
 *   onCreateProject   the frame's create affordance
 *   projectSlot       the element a surface's contract.chrome.projectSlot
 *                     mounts inside the frame (the caller builds it, because
 *                     only the caller has the env flags and the contract)
 *   session           the platform session; carried for 4b's continuity hoist,
 *                     read by no slot in 4a (declared, not guessed)
 *   posture           console only: { studio, navExpanded, onNavExpand,
 *                     wideViewport, jobRailExpanded, onJobRailExpand,
 *                     onJobRailCollapse }. NULL on the stage, which is the
 *                     console/stage discriminator. `studio` is the truthiness
 *                     of App's useStudioGround() portal: every console gate
 *                     below is ANDed with it exactly as App.jsx does today,
 *                     so a rail-OFF build renders the old shell untouched.
 *   entitlement       { tier, entitlements, loading, mock, placement } where
 *                     placement is 'inline' | 'docked' — the ONE declaration
 *                     of where the gate renders, replacing the two scenes'
 *                     copies of `studioGround && drafting && wideViewport`
 *   commandBar        render prop (or node): App's PromptBox, ToolCast's
 *                     .tc-bar block, until slice 5 unifies them
 *   jobRail           { mock, jobs, currentJob, inflight, reattaching,
 *                      onSelectJob } or null. The stage gates its own
 *                      `rightView === 'jobs'` OUTSIDE the frame by passing
 *                      null, so the frame never assumes rightView exists.
 *   toast             { toast, onDone }
 *   signedIn/onSignOut  the tabs' AccountSignOut (the stage passes them; the
 *                     console's sign-out lives in its own header today)
 *
 * The context value is rebuilt each render, exactly like the props objects it
 * replaces. Its only consumers are the slots below, which are already inside
 * the scene's render, so this adds no work a re-render did not already do.
 */
export default function SurfaceFrame({
  activeSurface,
  states = null,
  catalog = null,
  catalogError = null,
  workspaceProject = null,
  onSelect = null,
  onCreateProject = null,
  projectSlot = null,
  session = null,
  posture = null,
  entitlement = null,
  commandBar = null,
  jobRail = null,
  toast = null,
  signedIn = false,
  onSignOut = null,
  children = null,
}) {
  const value = {
    activeSurface,
    // The declared slots for this surface, resolved once per render and shared
    // by every slot below, so two slots can never read two different records.
    contract: surfaceContract(activeSurface),
    states,
    catalog,
    catalogError,
    workspaceProject,
    onSelect,
    onCreateProject,
    projectSlot,
    session,
    posture,
    entitlement,
    commandBar,
    jobRail,
    toast,
    signedIn,
    onSignOut,
  }
  return <SurfaceFrameContext.Provider value={value}>{children}</SurfaceFrameContext.Provider>
}

// --- slots -----------------------------------------------------------------
// Each slot renders EXACTLY the element its scene renders today, with the same
// props, so the rendered subtree is byte-identical. The line citations are the
// pre-slice-4a source each slot replaces.

/** ProductSurfaceTabs. App.jsx:2868-2873 / ToolCast.jsx:1422-1430. */
function Tabs() {
  const frame = useSlot()
  // Fail closed: the tabs index `states[surface.id]` for all four surfaces, so
  // a missing table is a broken caller, not an empty tab bar to render anyway.
  if (!frame || !frame.states || typeof frame.states !== 'object') return null
  return (
    <ProductSurfaceTabs
      activeSurface={frame.activeSurface}
      states={frame.states}
      onSelect={frame.onSelect}
      workspaceProject={frame.workspaceProject}
      catalog={frame.catalog}
      signedIn={frame.signedIn}
      onSignOut={frame.onSignOut}
    />
  )
}

/**
 * ProductSurfaceFrame, under its declared slot.
 *
 * The console gate is `contract.chrome.productFrame` (App.jsx:2898). The stage
 * gate is `contract.chrome.stageBranch === 'frame'`, which is the arm ToolCast
 * already falls through to (ToolCast.jsx:2138-2147) — the two scenes genuinely
 * diverge here (the stage gives iOS its own rail instead of the frame, D1 in
 * the contract doc), and slice 4a preserves that divergence rather than
 * inventing a merge. Both terms are declared slots, never surface ids.
 */
function Frame() {
  const frame = useSlot()
  if (!frame || !frame.states || typeof frame.states !== 'object') return null
  const declared = isConsole(frame)
    ? frame.contract.chrome.productFrame
    : frame.contract.chrome.stageBranch === 'frame'
  if (!declared) return null
  return (
    <ProductSurfaceFrame
      activeSurface={frame.activeSurface}
      states={frame.states}
      catalog={frame.catalog}
      catalogError={frame.catalogError}
      workspaceProject={frame.workspaceProject}
      onCreateProject={frame.onCreateProject}
      projectSlot={frame.projectSlot}
    />
  )
}

/**
 * EntitlementGate at the DECLARED placement. `at` names where this mount
 * stands; the gate renders only when the caller declared that placement.
 *
 * Console: the drafting surfaces host it in the properties dock's Plan section
 * (App.jsx:3263-3268) and everywhere else it renders at the end of <main>
 * (App.jsx:3376-3388) — one `studioGround && drafting && wideViewport`
 * predicate, spelled twice, now stated once as `placement`.
 * Stage: always inline, inside the trust panel (ToolCast.jsx:1893-1898).
 */
function Entitlement({ at = 'inline' }) {
  const frame = useSlot()
  const ent = frame?.entitlement
  if (!ent) return null
  if (ent.placement !== at) return null
  return (
    <EntitlementGate
      tier={ent.tier}
      entitlements={ent.entitlements}
      loading={ent.loading}
      mock={ent.mock}
    />
  )
}

/**
 * The scene's command bar. A render prop so the scene keeps ownership of its
 * own bar until slice 5 makes it one PromptBox everywhere; the frame's job in
 * 4a is only to own WHERE it stands, not what it is.
 */
function CommandBar() {
  const frame = useSlot()
  const bar = frame?.commandBar
  if (!bar) return null
  return typeof bar === 'function' ? bar() : bar
}

/**
 * JobRail, with the spine posture derived from the contract and the console's
 * posture. App.jsx:3505-3521 spelled this inline; the stage passes no posture
 * at all, so every spine prop resolves to the JobRail defaults and its render
 * is unchanged (ToolCast.jsx:1759-1766).
 *
 * `jobRail === null` renders nothing, which is how the stage keeps its
 * `rightView === 'jobs'` gate outside the frame.
 */
function JobRail() {
  const frame = useSlot()
  const rail = frame?.jobRail
  if (!rail) return null
  const posture = frame.posture
  // contract.rails.right === 'job-spine' is App's `jobSpine` (App.jsx:2314).
  const jobSpine = frame.contract.rails.right === 'job-spine'
  const studio = !!posture && !!posture.studio
  const spined = studio && jobSpine
  return (
    <JobRailComponent
      mock={rail.mock}
      jobs={rail.jobs}
      currentJob={rail.currentJob}
      inflight={rail.inflight}
      reattaching={rail.reattaching}
      onSelectJob={rail.onSelectJob}
      spine={spined && !!posture.wideViewport && !posture.jobRailExpanded}
      onExpand={spined ? posture.onJobRailExpand : undefined}
      onCollapse={spined && posture.jobRailExpanded ? posture.onJobRailCollapse : undefined}
    />
  )
}

/** Toast. App.jsx:3502 / ToolCast.jsx:2040. */
function FrameToast() {
  const frame = useSlot()
  const toast = frame?.toast
  if (!toast) return null
  return <ToastComponent toast={toast.toast} onDone={toast.onDone} />
}

/**
 * The drafting status toggles. Console only, and only where the contract
 * declares a cockpit: App.jsx:3578 `studioGround && drafting`, where `drafting`
 * IS contract.chrome.cockpit since slice 2 (App.jsx:2309). The stage has no
 * cockpit band at all, so posture === null renders nothing here.
 */
function Cockpit() {
  const frame = useSlot()
  if (!frame || !isConsole(frame)) return null
  if (!frame.posture.studio || !frame.contract.chrome.cockpit) return null
  return <StatusToggles />
}

// The slots are statics on the frame, not separate named exports: one import
// gives a scene the wrapper AND every slot, and `<SurfaceFrame.Tabs />` reads
// at the mount site as what it is. Each reference is a stable module-level
// function, so a surface switch re-renders these fibers and never remounts
// them — which is the F-8 never-remounts contract the continuity rail and the
// sign-out control inside ProductSurfaceTabs depend on.
SurfaceFrame.Tabs = Tabs
SurfaceFrame.Frame = Frame
SurfaceFrame.Entitlement = Entitlement
SurfaceFrame.CommandBar = CommandBar
SurfaceFrame.JobRail = JobRail
SurfaceFrame.Toast = FrameToast
SurfaceFrame.Cockpit = Cockpit
