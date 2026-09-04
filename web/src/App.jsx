import './structural.css'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, Suspense } from 'react'
import { createPortal } from 'react-dom'
import { track, setTourStep } from './telemetry.js'
import { useStudioGround } from './site/studioGround.js'
import SurfaceGrounds, { groundShowsDrawing } from './site/SurfaceGrounds.jsx'
import { CockpitStatus, FootRegion, StatusTabs, ViewCluster } from './site/DrawingCockpit.jsx'
// Slice 4a: the ONE shell wrapper both scenes mount, and the console nav
// rail it used to spell inline. Every shared chrome gate lives there now.
import SurfaceFrame from './site/SurfaceFrame.jsx'
import NavRail from './site/NavRail.jsx'
import CockpitTopBand from './site/CockpitTopBand.jsx'
import DraftingRibbon from './site/DraftingRibbon.jsx'
import PropertiesDock, { drawingExtents } from './site/PropertiesDock.jsx'
import { familiesForSurface, familyMonogram } from './lib/surfaceRails.js'
import { ladderListener, slashCommandHandlers } from './lib/actionRegistry.js'
import { REASONS, authorCluster, catalogClusters, catalogTabClusters, layersCluster, railCluster, versionCluster, viewCluster, referencePanels } from './lib/ribbonClusters.js'
import { isWriteTool } from './lib/toolRecord.js'
import { resolvePublishedCatalogTool } from './site/publishedCatalogTool.js'
import { entityGeometry } from './lib/entityMetrics.js'
import { setCredentialMountAvailable } from './lib/secretGuardTransport.js'
import { loadDemoSolve } from './site/intakeCache.js'
// The 3D viewer drags in `three`; loading it lazily (mirroring the auth.js
// dynamic-import pattern) keeps first paint off the critical path.
const Viewer = React.lazy(() => import('./components/Viewer.jsx'))
import Legend from './components/Legend.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import SelectionReadout from './components/SelectionReadout.jsx'
import PromptBox from './components/PromptBox.jsx'
import RoutePanel from './components/RoutePanel.jsx'
import DegradedBanner from './components/DegradedBanner.jsx'
import OperatorEntry from './components/operator/OperatorEntry.jsx'
import { EntitlementNotice } from './components/EntitlementGate.jsx'
import QuotaCard from './components/QuotaCard.jsx'
import OverlayDecisionCard from './components/OverlayDecisionCard.jsx'
import { useOverlay } from './useOverlay.js'
import AnnotationDecisionCard from './components/AnnotationDecisionCard.jsx'
import { useAnnotations } from './useAnnotations.js'
import VersionHistory from './components/VersionHistory.jsx'
import * as mockVersions from './mock/mockVersions.js'
import ProjectSwitcher from './components/ProjectSwitcher.jsx'
import WorkspaceSummary from './components/WorkspaceSummary.jsx'
import OpsDrawer from './components/OpsDrawer.jsx'
import CustomizePanel from './components/CustomizePanel.jsx'
import CheckoutControls from './components/CheckoutControls.jsx'
import ClaudeAccountPanel from './components/ClaudeAccountPanel.jsx'
import DemoBanner from './components/DemoBanner.jsx'
import { AccountSignOut } from './components/ProductSurfaceTabs.jsx'
import { deriveWorkspaceProjectState } from './site/workspaceProjectState.js'
import IosSurface from './ios/IosSurface.jsx'
import { ENV_IOS_SURFACE } from './ios/flag.js'
import CadEditSurface from './cadedit/CadEditSurface.jsx'
import EngineSessionProvider from './cadedit/EngineSessionProvider.jsx'
import EngineRibbonClusters from './cadedit/EngineRibbonClusters.jsx'
import CommandLineArmer from './cadedit/CommandLineArmer.jsx'
import EngineDocumentView from './cadedit/EngineDocumentView.jsx'
import EngineHeadOpener from './cadedit/EngineHeadOpener.jsx'
import CanvasPointPicker from './cadedit/CanvasPointPicker.jsx'
import { COCKPIT_COMMAND_EVENT, parseDrawingCommand } from './lib/commandWords.js'
import { markInstant } from './lib/instant.js'
import { agentBannerFor } from './lib/agentBanner.js'
import { selectEntity } from './lib/selectEntity.js'
import { countEntitiesByLayer } from './lib/layerCounts.js'
import { ENV_CAD_EDIT } from './cadedit/flag.js'
import {
  DEFAULT_PRODUCT_SURFACE,
  productSurfaceStates,
  productSurfaceFromSearch,
  searchForProductSurface,
  surfaceContract,
} from './site/productSurfaces.js'
import { fetchIosSurfaceStatus } from './ios/iosSurfaceStatus.js'
// `logout` is no longer imported here: the session controller owns ending a
// session (useSessionController defaults endSession to auth.js logout).
import { authConfigured, login, isSignedIn, handleRedirectCallback, isAuthRedirectCallback } from './auth.js'
import { shouldAutoDemo } from './demoState.js'
import { humanizeError } from './errorHumanize.js'
import { cadTimingRows } from './cadTimingPresentation.js'
import { getSessionHolderId } from './checkoutIdentity.js'
import {
  confirmRunIntent, createCatalogRunContext, createCatalogToolSnapshot, createRunIntentState,
  dismissRunIntent, drawingVersionForRun, mintCorrelationId, prepareCatalogRunParams, stageRunIntent,
} from './runIntent.js'
import useExit from './useExit.js'
import DetailsDrawer from './components/DetailsDrawer.jsx'
import {
  config, getSession, getTools, getCapabilities, runTool, runToolAsync,
  getJob, recordToEnvelope, publishStagedAuthor, getDrawingIntake,
  getDrawingVersions, undoDrawing, redoDrawing, takeCheckout, releaseCheckout, nlPrompt,
  createOrg, listProjects, createProject, openProject, subscribeUnauthorized,
  saveEditedDrawingVersion,
  saveDrawingVersionPlan,
  fetchDrawingDxf,
  fetchSampleDxf,
} from './api.js'
import { matchPrompt } from './mock/mockNlPrompt.js'
import { shouldStartTour } from './demo/tourEntry.js'
import DemoTour from './demo/DemoTour.jsx'
import { TOUR_STEPS } from './demo/tourScript.js'
import { editFixture, pendingEditDemo, editFixtureV2 } from './mock/editFixture.js'
import ConversePanel from './components/ConversePanel.jsx'
import {
  THRESHOLDS, fetchRegistry, fetchSkills, listPendingApprovals,
} from './converse.js'
import { useWorkspaceControllers } from './controllers/WorkspaceControllerProvider.jsx'
import { entitlementAllowed } from './controllers/platform/index.js'
import useBuildQueue from './controllers/useBuildQueue.js'
import useJobController from './controllers/useJobController.js'
import useAuthorStageController from './controllers/useAuthorStageController.js'
import useDrawingVersionController from './controllers/useDrawingVersionController.js'
import usePlatformTrustController from './controllers/platform/usePlatformTrustController.js'
import useWorkspaceController from './controllers/workspace/useWorkspaceController.js'
import useCatalogController from './controllers/catalog/useCatalogController.js'
import useSessionController from './controllers/session/useSessionController.js'
import { consoleAuthRequired, consoleSignedOut } from './controllers/session/consoleGate.js'
import useCheckoutController from './controllers/checkout/useCheckoutController.js'
import { checkoutScopeDrawingId, deriveCheckout } from './controllers/checkout/createCheckoutController.js'
import { useDrawingIdentity } from './drawing/DrawingIdentityProvider.jsx'

// Calm layer palette, re-derived at higher lightness for the DARK CADViewport
// canvas (--cv-bg #0f0f11) — same hue spacing as the retired light-paper set so
// the legend swatches stay distinguishable.
const PALETTE = ['#6b9fd4', '#8fbf9c', '#b49bd1', '#d4af6e', '#cf8fa6', '#79bcc7']

// Suspense fallback while the lazy viewer chunk arrives — L1 indeterminate:
// pulse dot + verb, top-left (the centered position is reserved for X3 failures).
function ViewerSkeleton() {
  // Material follows the host: --viewer-skeleton-bg is re-pinned by the
  // studio ground (landing.css) so the fallback sheet matches whichever
  // surface it paints over; the inline fallback keeps the old shell's
  // card material byte-identical (W4c-0 debt, ACCEPTANCE deferred list).
  return (
    <div className="viewer-skeleton" aria-hidden="true" style={{ position: 'absolute', inset: 0, background: 'var(--viewer-skeleton-bg, #0f0f11)' }}>
      <div className="loading-line dim" style={{ position: 'absolute', top: 14, left: 14 }}>
        <span className="dot live pulse" aria-hidden="true" /> Preparing the viewer
      </div>
    </div>
  )
}

// Durable pointer to the one in-flight live job, so a closed/reloaded tab can
// re-attach instead of orphaning the UI (CONTRACT-ADDENDUM §7, MATRIX gap #1).
// Elapsed wall-clock for the running strip: "4.2s" under a minute, "2:41" after.
const fmtElapsed = (ms) => {
  if (ms == null) return null
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.floor((ms % 60000) / 1000)
  return `${m}:${String(s).padStart(2, '0')}`
}

// `?fixture=edit` (mock only) loads the synthetic edit fixture that exercises
// inserts + 3DFACEs + picking + the pending/version flow.
const fixtureParam = new URLSearchParams(window.location.search).get('fixture')

// `?demo=degraded` is a DEV-only demo hook to show the §10 degraded banner
// without a real APS_LIVE=1 fallback (degraded_mode is only true when a cloud
// run falls back; at APS_LIVE=0 it never trips). It only forces the banner's
// visibility — it fabricates no result numbers.
const demoDegraded = new URLSearchParams(window.location.search).get('demo') === 'degraded'

// `?ops=1` reveals the INTERNAL ops drawer (tenant kill-switch surface). Absent
// by default — the tenant-facing app never shows it.
const opsFlag = new URLSearchParams(window.location.search).get('ops') === '1'

// `?customize=1` reveals the R7 admin self-edit drawer. Like ?ops=1 it is
// absent by default and never reachable from a public demo build; unlike ops
// the mount ALSO requires the policy read to carry platform_customize: true
// (strict — see platformCustomizeEntitled), so non-admins never see it even
// with the flag.
const customizeFlag = new URLSearchParams(window.location.search).get('customize') === '1'

// `?demo=locked` is a DEV-only hook that injects a synthetic single-writer
// checkout (held by another session) so the checkout chip + write-Run
// suppression can be exercised without touching the demo drawing's real
// manifest. It fabricates no result numbers — only a lock display.
const demoLocked = new URLSearchParams(window.location.search).get('demo') === 'locked'

// Engineering-only header controls (the Mock switch). The demo build is served
// from dist-demo with no backend, so a stray click on "Mock" in front of a cold
// audience points every call at the PROSPECT's own localhost:8130 — a dead port
// whose TypeError carries no .status, so the 401 auto-demo fallback never
// fires — and simultaneously reveals the Anthropic-credential panel. Keep the
// toggle for `npm run dev` and `?dev=1`; hide it on the demo build.
const devControls = (() => {
  try {
    if (import.meta.env?.DEV) return true
  } catch { /* no import.meta in a non-vite host */ }
  return new URLSearchParams(window.location.search).get('dev') === '1'
})()

// A self-minted author-authority turn is reusable for this long — a wide
// margin under the server's TURN_MAX_S default of 300s (server/turn_runner.py).
const AUTHOR_AUTHORITY_TTL_MS = 120_000

// Live-mode landing when there is no session: instead of a wall of red 401s with
// no way forward, a calm gate — sign-in for the live surface is coming; the demo
// is one click away. Shown only when a 401 was actually observed (not offline).
function plural(n, w) { return `${n} ${w}${n === 1 ? '' : 's'}` }

function SignedOutGate({ onDemo, onSignIn }) {
  return (
    <div className="card enter" style={{ margin: '0 0 16px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--foreground)' }}>You’re not signed in</div>
        <p className="panel-sub" style={{ margin: 0 }}>
          {onSignIn
            ? 'Sign in to load your tools and drawings from the cloud workspace, or explore the interactive demo on sample data.'
            : 'This is a live preview of Leaf against the cloud workspace. Sign-in for the live surface is coming soon — explore the interactive demo to try the prompt lanes, tool catalog, and viewer on a sample rooftop drawing.'}
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          {onSignIn && <button type="button" className="btn primary" onClick={onSignIn}>Sign in</button>}
          <button type="button" className={onSignIn ? 'btn ghost' : 'btn primary'} onClick={onDemo}>Explore the demo</button>
          <span className="dim" style={{ fontSize: 12 }}>No sign-in needed · sample data</span>
        </div>
      </div>
    </div>
  )
}


export default function App() {
  // W1 (convergence): the console's drawing identity is now owned by
  // DrawingIdentityProvider, seeded from the SAME rule these two module
  // constants encoded — the bundled rooftop is an intake source named
  // `rooftop_demo`, while default writes and checkout state use the server's
  // well-known `demo` store drawing, and `?drawing=` names its own. Uploaded
  // drawings retain their own id across both surfaces.
  //
  // The local names are preserved DELIBERATELY: every call site below (and
  // web/src/app-wiring.test.mjs's pins) reads exactly as it did before, so
  // this change moves ownership and nothing else. The console keeps its
  // module-const behavior — the seed is frozen at mount and nothing in this
  // shell promotes a new identity yet.
  const { drawingId: REQUESTED_DRAWING_ID, source: DRAWING_SOURCE } = useDrawingIdentity()
  // W3 one-shell: non-null ONLY under the studio shell (rail on). The sole
  // consumer is the Viewer render site, which portals into it; null renders
  // the old shell byte-for-byte (the rollback contract, studioGround.js).
  const studioGround = useStudioGround()
  const [mock, setMock] = useState(config.mockDefault)
  const [loadErr, setLoadErr] = useState(null)
  const [intakeRetryKey, setIntakeRetryKey] = useState(0) // X3 Retry — bumping re-runs the intake load effect
  const [selectedTool, setSelectedTool] = useState(null)
  const [selectedHandle, setSelectedHandle] = useState(null)
  const [pendingEdit, setPendingEdit] = useState(null)
  // Write-loop (§11, live mode): the current drawing/version chain from the last
  // drawing response and an undo/redo-in-flight guard. Version-completed events
  // surface as NT2 toasts (showToast), never as a persistent amber note.

  // --- platform session state ---
  const [tenant, setTenant] = useState(null)             // /api/session tenant echo (else "demo")
  const [tier, setTier] = useState(null)                 // real tier from the session echo (auth-live)
  const [org, setOrg] = useState(null)                   // org_id from the session echo (auth-live)
  // Real entitlements (GET /api/entitlements): drives the write-tool + build gates.
  // null in mock, or when the endpoint isn't deployed -> treated as full access.
  // Version-history browser (§ version chain) + read-only preview state.
  // W2b (convergence): the console's session gate is the SHARED controller, not
  // a hand-rolled twin. `authRequired` used to be a local latch that only ever
  // went true; it is now the controller's `required` status, so /app and /try
  // read ONE state machine and the console inherits the bounded post-callback
  // recovery the twin never had (D1a defect #6, createSessionController.js).
  //
  // ONE INSTANCE PER PAGE: SiteRoot renders scene 'app' (this component) and
  // scene site|tool (StageScene -> ToolCast, which mounts the other instance)
  // in mutually exclusive arms of one ternary, so the console mounting its own
  // controller keeps exactly one live instance per page load. SiteRoot itself
  // mounts none, and hoisting one there would hand the console and the stage a
  // shared latch neither owns — the opposite of the per-mode isolation
  // DrawingIdentityProvider had to grow (see its header).
  const session = useSessionController()
  const sessionActions = session.actions
  // Byte-identical to the retired boolean: "live mode with no session: 401s
  // observed -> polls stop, footer says so".
  const authRequired = consoleAuthRequired(session.status)
  // NOT the session gate: this mirrors TOKEN PRESENCE (auth.js localStorage),
  // which is a different truth from "the platform session resolved". It is true
  // at boot before /api/session has answered, and the three surfaces below
  // (converse mount, Customize entry, CustomizePanel) all want the token, not
  // the session. The controller publishes no token-presence signal, so folding
  // these into `status` would change three truth tables; they stay here.
  const [signedIn, setSignedIn] = useState(() => isSignedIn())
  const is401 = (e) => e?.status === 401 || / -> 401$/.test(String(e?.message || ''))
  const [toolsOpen, setToolsOpen] = useState(false)      // left catalog collapsed by default
  const [authorOpen, setAuthorOpen] = useState(false)    // author flow (opens on build lane)
  const [authorSeed, setAuthorSeed] = useState('')       // build-lane prefill text
  const [authorSignal, setAuthorSignal] = useState(0)    // bump to re-seed the author flow
  const [authorTargetTool, setAuthorTargetTool] = useState(null)
  // The tool the author card published most recently. The ribbon's Author
  // cluster carries it so "run the one I just made" is a ribbon command too,
  // and says honestly while it has no catalog digest yet.
  const [lastAuthoredTool, setLastAuthoredTool] = useState(null)

  // --- projects / orgs workspace (UI wave 2, item 1) ---

  // --- single-writer checkout lock (item 3) ---
  // W2c (convergence): the lock, the bearer capability, the reload handoff, the
  // Web-Lock authority and the duplicate-tab claim are ALL the shared
  // controller's now (controllers/checkout/). The holder id stays here, in the
  // shell, exactly as it does on /try — it is this surface's session identity,
  // and the controller consumes it.
  const [ownHolder, setOwnHolder] = useState(() => getSessionHolderId())
  // Read at render so a write-path call site can reach the capability without
  // waiting for the controller mount below (drawingAdapters is built first, and
  // the controller needs `drawingState` from the version controller it feeds).
  // Same render-phase ref assignment `catalogUiRef` already uses in this file.
  const checkoutCapabilityRef = useRef(() => null)

  // --- ops drawer (item 2) ---
  const [opsDismissed, setOpsDismissed] = useState(false)
  const [customizeOpen, setCustomizeOpen] = useState(customizeFlag)

  // --- NT2 toast (one slot — newest replaces) + DT2 details drawer ---
  const [toast, setToast] = useState(null)   // {id, text, action?}
  const [drawer, setDrawer] = useState(null) // {title, rows[], action?, foot?}

  // --- Claude account grant (Concern 2 — the user's Claude login) ---
  // Kept strictly apart from the platform identity above (AUTH.md §0). The token
  // is write-only: we hold linkage status only, never the token itself.
  const [claudeOpen, setClaudeOpen] = useState(false) // header Claude-account popover open
  // M5 guided tour: opened ONLY by the ?demo=tour (or ?demo=1) deep-link, and
  // only while mock is active. Exiting clears the tour and leaves you in mock.
  const [tourOn, setTourOn] = useState(() => {
    try { return shouldStartTour(typeof window !== 'undefined' ? window.location.search : '') } catch { return false }
  })
  const [tourLanded, setTourLanded] = useState(true) // did the current beat's real effect land?
  // Was this session deep-linked into the tour? Latched once so exiting the tour
  // still leaves a way back in (the tour re-enters at beat 1).
  const tourAvailable = useRef(false)
  if (tourOn) tourAvailable.current = true
  // P2 wave C-2: the tour funnel. tourStepRef mirrors DemoTour's (uncontrolled)
  // index so exit can say where; setTourStep stamps `tour_step` onto every
  // organic event while the tour is active (design: tour beats ride the REAL
  // handlers, so organic events carry the step instead of tour.* duplicates).
  // The started emit requires the tour to actually RENDER (mock && tourOn:
  // a ?demo=tour deep link in live mode shows no tour and counts nothing,
  // review #428 round-1 blocker 3); step 0 is emitted as reached so per-step
  // dropout starts at the first step.
  const tourStepRef = useRef(0)
  const tourStartedRef = useRef(false)
  useEffect(() => {
    if (tourOn && mock && !tourStartedRef.current) {
      tourStartedRef.current = true
      tourStepRef.current = 0
      setTourStep(TOUR_STEPS[0]?.id)
      track('tour.started', { entry: 'deeplink' })
      track('tour.step_reached', { step_id: TOUR_STEPS[0]?.id })
    }
  }, [tourOn, mock])

  // --- agent tier (two-tier dispatch, wire §11; LIVE only — mock has no harness) ---
  // instanceId: the provider mints one per mount for exactly this proof —
  // the footer stamps it (W4c-0 debt) so e2e can assert ONE workspace
  // controller (= one converse session) the same way it proves one checkout.
  const { converse, instanceId: workspaceInstanceId } = useWorkspaceControllers()
  const {
    sessionId: agentSessionId,
    turns: agentTurns,
    startTurn: startAgentTurn,
    clear: clearAgentSession,
  } = converse
  // T1 runtime overlay. Reads on load and applies the tenant's colour/copy
  // tokens as CSS custom properties, so an approved change is on screen
  // without a build or a deploy. LIVE only, like the agent tier: mock mode has
  // no tenant to read for, and applying a theme there would be theatre.
  const themeOverlay = useOverlay(agentSessionId, { enabled: !mock })
  const [pendingApprovalCount, setPendingApprovalCount] = useState(0)
  const [pendingApprovalsUnavailable, setPendingApprovalsUnavailable] = useState(false)
  useEffect(() => {
    if (mock || !agentSessionId) {
      setPendingApprovalCount(0)
      setPendingApprovalsUnavailable(false)
      return undefined
    }
    let closed = false
    const refresh = async () => {
      try {
        const approvals = await listPendingApprovals(agentSessionId)
        if (!closed) {
          setPendingApprovalCount(approvals.length)
          setPendingApprovalsUnavailable(false)
        }
      } catch {
        if (!closed) setPendingApprovalsUnavailable(true)
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => {
      closed = true
      window.clearInterval(timer)
    }
  }, [agentSessionId, mock])

  const viewerRef = useRef(null)
  const drawingErrorRef = useRef(null)
  const catalogUiRef = useRef({})
  const authorSectionRef = useRef(null)
  const authorPendingRef = useRef(false)
  const lastRunRef = useRef(null)       // {tool, params} for the retry affordance
  const barInputRef = useRef(null)      // ⌘K focuses the command bar input

  // Slash-menu registry: commands + skills + tools in one tenant-scoped
  // catalog. Fetched once; resolves to [] on any failure, in which case the
  // picker falls back to the catalog lane's runnable tools — today's
  // behaviour exactly, so a registry outage costs the menu nothing.
  const [registryEntries, setRegistryEntries] = useState([])
  const [catalogSkills, setCatalogSkills] = useState([])
  useEffect(() => {
    let live = true
    fetchRegistry().then((r) => { if (live) setRegistryEntries(r.entries || []) })
    fetchSkills().then((r) => { if (live) setCatalogSkills(r.skills || []) })
    return () => { live = false }
  }, [])

  const resultBlockRef = useRef(null)   // toast "View" scroll target (result)
  const workspaceCardRef = useRef(null) // toast "View" scroll target (viewer)
  const toastSeqRef = useRef(0)         // monotonic toast ids
  const cannedSeq = useRef(0)           // supersedes an in-flight tour beat (typing + dispatch)
  const runIntentSessionRef = useRef(null)
  if (!runIntentSessionRef.current) {
    runIntentSessionRef.current = `catalog-${mintCorrelationId()}`
  }
  const runIntentStateRef = useRef(null)
  if (!runIntentStateRef.current) {
    runIntentStateRef.current = createRunIntentState(runIntentSessionRef.current)
  }
  const runIntentSeqRef = useRef(0)

  const isEditFixture = mock && fixtureParam === 'edit'
  const applyDrawingIntake = useCallback((nextIntake) => {
    viewerRef.current?.applyVersion(nextIntake)
  }, [])
  const resetDrawingSelection = useCallback(() => {
    setSelectedHandle(null)
    setPendingEdit(null)
  }, [])
  const reportDrawingError = useCallback((error, { operation } = {}) => {
    if (operation === 'undo' || operation === 'redo') drawingErrorRef.current?.(error)
  }, [])
  const drawingAdapters = useMemo(() => ({
    loadHead: (drawingId) => getDrawingIntake(mock, drawingId, 'head'),
    loadVersion: (drawingId, version) => getDrawingIntake(mock, drawingId, version),
    loadVersions: (drawingId, options) => getDrawingVersions(mock, drawingId, options),
    undoVersion: (drawingId) => undoDrawing(mock, drawingId, checkoutCapabilityRef.current()),
    redoVersion: (drawingId) => redoDrawing(mock, drawingId, checkoutCapabilityRef.current()),
  }), [mock])
  const drawing = useDrawingVersionController({
    ...drawingAdapters,
    formatError: humanizeError,
    onApplyIntake: applyDrawingIntake,
    onResetSelection: resetDrawingSelection,
    onError: reportDrawingError,
  })
  const {
    intake,
    versionIntake,
    shown,
    visibleLayers,
    drawingState,
    canUndo,
    canRedo,
    versionBusy,
    overlayStale,
    historyOpen,
    history,
    historyError: historyErr,
    historyLoading,
    previewing,
    refreshFailure: refreshFail,
    unreadableHead,
    mutationsBlocked: drawingMutationsBlocked,
    actions: drawingActions,
  } = drawing
  const {
    reset: resetDrawing,
    seatIntake,
    seatVersion: seatDrawingVersion,
    setVisibleLayers,
    setOverlayStale,
    undo: undoDrawingVersion,
    redo: redoDrawingVersion,
    loadHistory,
    toggleHistory: onToggleHistory,
    closeHistory,
    previewVersion: onPreviewVersion,
    backToHead: onBackToHead,
    markRefreshFailure,
    retryRefresh: onRetryViewerRefresh,
    recordRestore: onRestoreCommitted,
    recordCommittedUnreadableHead,
    retryUnreadableHead,
  } = drawingActions
  const platform = usePlatformTrustController({ mock })
  const {
    usage,
    usageAt,
    health,
    entitlements,
    entLoading,
    grant,
    grantLoading,
    grantBusy,
    grantErr,
    actions: platformActions,
  } = platform
  const {
    loadUsage,
    loadHealth,
    linkClaude: onLinkClaude,
    unlinkClaude: onUnlinkClaude,
  } = platformActions
  const workspaceServices = useMemo(() => ({
    createOrg,
    listProjects,
    createProject,
    openProject,
  }), [])
  const workspaceController = useWorkspaceController({
    mock,
    services: workspaceServices,
    formatError: humanizeError,
  })
  const {
    orgId,
    projects,
    projectsError: projectsErr,
    projectsLoading,
    openProjectId,
    workspace,
    canonicalVersionId,
    workspaceLoading: wsLoading,
    orgBusy,
    projectBusy,
    adoptOrgId,
    openProject: onOpenProject,
    createOrg: createWorkspaceOrg,
    createProject: createWorkspaceProject,
    rehydrate,
    closeProject: onCloseProject,
    selectCanonicalVersion,
  } = workspaceController
  const annotationEnabled = Boolean(
    !mock && signedIn && openProjectId && drawingState?.drawing_id && agentSessionId,
  )
  const annotations = useAnnotations(agentSessionId, { enabled: annotationEnabled })
  // What the panels/legend/selection reflect: a read-only version PREVIEW wins,
  // else the applied write-loop version, else the base intake.
  // The MOUNTED DRAWING's own name — deliberately NOT called a project. It is
  // null when nothing is mounted, so the workspace-project derivation can tell
  // "a drawing is open, no project" apart from "nothing is open at all".
  const drawingName = shown?.dwg ? shown.dwg.split(/[\\/]/).pop().replace(/\.dwg$/i, '') : null
  // Prose fallback for the "What should Leaf do to <em>…</em>?" headline only.
  const projectName = drawingName || 'your project'
  // Honest identity: tenant id and tier are DISTINCT. tenant defaults to "demo"
  // off-auth; tier is only known when the session echo carries it (auth live).
  const tenantLabel = tenant || 'demo'
  const tierDisplay = tier || '—'
  // Entitlement tier prefers the policy read (authoritative) over the session echo.
  const entTier = entitlements?.tier || tier || 'demo'
  const gateTier = entTier

  // Real entitlement gates. Unknown capability (mock, or endpoint undeployed ->
  // entitlements null) resolves permissive (true) — byte-identical to today's
  // ungated demo. When a policy IS present, a false value genuinely disables the
  // affordance (the server enforces it too, so this only mirrors reality).
  const entOf = useCallback((key) => {
    const e = entitlements && entitlements.entitlements
    if (!e || typeof e[key] === 'undefined' || e[key] === null) return true
    return e[key] !== false
  }, [entitlements])
  const canRunWrite = entOf('run_write')
  // Platform self-edit is the ONE capability that never falls back permissive:
  // entOf treats unknown as allowed (demo parity), but an admin-only lane must
  // read as absent unless the policy read explicitly grants it. Strict `=== true`
  // keeps the drawer unreachable in mock and on tiers below admin even with
  // ?customize=1 in the URL (the server enforces the same gate regardless).
  const platformCustomizeEntitled = entitlements?.entitlements?.platform_customize === true
  const canOpenCustomize = !mock && signedIn && platformCustomizeEntitled
  // Build routes through the SHARED helper (platformTrustModel) so every
  // surface — this legacy /app shell and ToolCast's /try — applies the same
  // entitlement-AND-availability rule: a tier may hold `build` while the R5
  // authoring stage is off, and Generate must not render enabled then.
  const canBuild = entitlementAllowed(entitlements, 'build')
  // Agent tier gate: LIVE only (mock has no harness — behavior stays exactly
  // today's), and only when the plan doesn't explicitly exclude `converse`
  // (unknown/undeployed policy resolves permissive, like every other gate).
  const canConverse = entOf('converse')
  const agentDisabled = mock || !canConverse
  const catalogServices = useMemo(() => ({
    getTools,
    getCapabilities,
    routePrompt: nlPrompt,
  }), [])
  const catalogAdapters = useMemo(() => ({
    humanizeError,
    isUnauthorized: (error) => error?.status === 401 || / -> 401$/.test(String(error?.message || '')),
    thresholds: THRESHOLDS,
    previewRoute: matchPrompt,
    commitDecision: (decision) => catalogUiRef.current.armDecision?.(decision),
    dismissDecision: () => {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    },
    openAuthor: (text) => {
      if (!authorPendingRef.current) setAuthorTargetTool(null)
      setAuthorSeed(text)
      setAuthorSignal((value) => value + 1)
      setAuthorOpen(true)
      setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
    },
    startAgentTurn: (...args) => catalogUiRef.current.startAgentTurn?.(...args),
    agentBannerFor,
    // W2b: same edge, same source name /try already uses. The controller
    // records WHICH surface refused (`sources`) instead of collapsing every
    // observer onto one shared boolean.
    onAuthRequired: () => sessionActions.requireAuth('catalog'),
    // W4f slice B: a bare CAD command word on a drafting surface (LINE, C,
    // MOVE ...) is handed to the engine's CommandLineArmer through ONE window
    // event instead of the router. The surface gate rides a ref written each
    // render (drawingCommandOnRef) so this memo stays stable.
    drawingCommand: (text) => {
      if (!drawingCommandOnRef.current) return false
      const command = parseDrawingCommand(text)
      if (!command) return false
      window.dispatchEvent(new CustomEvent(COCKPIT_COMMAND_EVENT, { detail: { group: command.group, op: command.op } }))
      return true
    },
  }), [sessionActions])
  const drawingCommandOnRef = useRef(false)
  // The refusal copy's closing sentence points at the Claude accounts panel
  // only where that panel exists. It is mounted under `{!mock && ...}` below
  // and self-guards with `if (mock) return null`, so this is the same answer
  // the control gives itself. The transports read it (they are plain functions
  // with no props to thread), and it fails honest: false until answered.
  useEffect(() => { setCredentialMountAvailable(!mock) }, [mock])

  const { state: catalogState, actions: catalogActions } = useCatalogController({
    services: catalogServices,
    adapters: catalogAdapters,
    context: { mock, entitlements, running: false, agentDisabled },
  })
  const {
    tools,
    toolsError: toolsErr,
    catalog,
    catalogError: catalogErr,
    openFamilies,
    openTool,
    prompt,
    route,
    routing,
    routeError: routeErr,
    agentMode,
    agentBanner,
    secretRefusal,
    hintLane,
    runnableTools: slashTools,
    capabilityCount: capCount,
  } = catalogState
  const {
    retryTools,
    // The awaitable catalog refetch. `loadCatalog` regroups families; this is
    // the flat runnable list the published-tool resolver needs.
    loadTools: loadCatalogTools,
    loadCatalog,
    upsertTool,
    toggleFamily,
    setFamilyOpen,
    openTool: setOpenTool,
    resetTransient: resetCatalogTransient,
    openAgentMode,
    clearAgentMode,
    clearAgentBanner,
    setPrompt: onPromptChange,
    dismissRoute,
    commitDecision: commitCatalogDecision,
    pickAlternative: onPickAlternative,
    clearRouteError,
  } = catalogActions

  // Claude account (Concern 2) authoring gate. Only fires when we DEFINITELY know
  // the tenant has no linked Claude grant (live + grant read + linked === false).
  // Unknown linkage (mock, or the endpoint undeployed -> grant === null) never
  // gates — authoring stays byte-identical to today (template path unaffected).
  const claudeNotLinked = !mock && !!grant && grant.linked === false

  // Handlers for registry entries of kind "command". This map IS the gate:
  // composer.js filterRunnable drops any command whose action is missing here,
  // so the picker cannot list something that would do nothing. A command
  // becomes visible the moment its handler lands — `/stop` stays hidden until
  // the composer can reach the converse panel's interrupt, which is the next
  // chip rather than a promise made in a menu.
  //
  // Placed after the catalog-controller destructuring that binds
  // `onPromptChange` (the canonical way to set the well's text — it also
  // invalidates a stale route) and before the JSX that reads this map.
  const slashCommandActions = useMemo(() => slashCommandHandlers(['slash:help'], {
    // The menu IS the help: reopen it on a bare slash and put the caret back
    // in the well so the next keystroke filters.
    onHelp: () => {
      onPromptChange('/')
      barInputRef.current?.focus()
    },
  }), [onPromptChange])

  // --- single-writer checkout (item 3) ---
  // W2c: the SHARED controller. It owns the /versions read and its sequence
  // fence, the fail-closed `unknown` state, the derived lock (including the
  // unproven-own-lock correction this shell used to compute inline), the bearer
  // capability, take/release, the beforeunload reload handoff, the Web-Lock
  // authority and the duplicate-tab holder claim.
  //
  // ONE INSTANCE PER PAGE, the same argument W2b made for the session
  // controller: SiteRoot renders scene 'app' (this component) and scenes
  // site|tool (StageScene -> ToolCast, which mounts the other instance) in
  // mutually exclusive arms of ONE ternary, and SiteRoot itself mounts none. So
  // the console mounting its own is exactly one live instance per page load,
  // and appCheckoutWiring.test.js pins that this file contains exactly one
  // mount. Hoisting a shared instance into SiteRoot was rejected for the same
  // reason: it would hand the console and the stage one lock — one capability,
  // one holder id, one authority — that neither owns.
  const checkout = useCheckoutController({
    mock,
    // Scope-reset contract (ACCEPTANCE, binding). While the identity stands,
    // this is byte-identical to the retired `resolveCheckoutDrawingId` call:
    // the version chain wins, the booted identity is the fallback. A tenant
    // switch voids the identity and the scope goes with it — see
    // checkoutScopeDrawingId for why abandoning beats releasing.
    drawingId: checkoutScopeDrawingId({
      identityDrawingId: REQUESTED_DRAWING_ID,
      drawingState,
      requestedDrawingId: REQUESTED_DRAWING_ID,
    }),
    holder: ownHolder,
    // The console knows its boot drawing (DrawingIdentityProvider seeds console
    // mode unconditionally), so the handoff is bootstrapped in the render phase
    // and the claim deferral is decided before the first effect runs — the
    // ordering the retired block got from its own render-phase bootstrap.
    bootDrawingId: REQUESTED_DRAWING_ID,
    deferForAuthCallback: isAuthRedirectCallback(),
    onHolderRemint: setOwnHolder,
  })
  checkoutCapabilityRef.current = checkout.actions.getCapability
  // `?demo=locked` (DEV-only) fabricates ANOTHER session's lock so the chip and
  // the write suppression can be exercised without touching the demo drawing's
  // real manifest. It replaces the DERIVED state only — the controller's scope,
  // capability and server reads are untouched — which is exactly where the
  // retired block applied it (on top of lockState, never on the fetched record).
  const lock = demoLocked
    ? deriveCheckout(
      { holder: 'another-session', acquired: new Date().toISOString(), expires: new Date(Date.now() + 3600e3).toISOString() },
      ownHolder,
      Date.now(),
      // The REAL unknown, never a synthetic one: `?demo=locked` fabricates a
      // holder, never an answer. A read still in flight or failed keeps
      // suppressing writes exactly as it does without the hook.
      checkout.unknown,
      mock,
      !!checkout.actions.getCapability(),
    )
    : checkout
  const otherHeldCheckout = lock.lockedByOther
  const writeLocked = lock.writeLocked || drawingMutationsBlocked
  const heldByUs = lock.heldByUs

  // Current open project's display name (from the hydration payload, else the list).
  const currentProjectName = openProjectId
    ? (workspace?.project?.name
        || projects.find((p) => (p.project_id || p.id) === openProjectId)?.name
        || null)
    : null

  // The ONE workspace-project derivation this shell renders (F-9). Header chip,
  // continuity rail, and the Browser / Solar CAD cards all read THIS — before,
  // each derived its own answer and they contradicted each other in production.
  const workspaceProjectState = useMemo(() => deriveWorkspaceProjectState({
    openProjectId,
    projectName: currentProjectName,
    drawingName,
    orgId,
    projectsUnavailable: projectsErr,
    mock,
  }), [openProjectId, currentProjectName, drawingName, orgId, projectsErr, mock])

  // color-by-layer, stable across renders (keyed to base intake identity)
  const colorForLayer = useMemo(() => {
    const layers = intake?.layers || []
    const map = {}
    layers.forEach((l, i) => { map[l] = PALETTE[i % PALETTE.length] })
    return (layer) => map[layer] || '#9fb3c8'
  }, [intake])

  // load session (intake + tenant echo) + reset transient state on mode/fixture change
  useEffect(() => {
    let alive = true
    resetDrawing(); setLoadErr(null)
    resetCatalogTransient()
    setToast(null); setDrawer(null); setTenant(null)
    setTier(null); setOrg(null)
    clearAgentSession()
    mockVersions.reset()
    const seat = (d, options = {}) => {
      if (!alive) return
      seatIntake(d, options)
      // MOCK write loop (M3): v1 of the 'demo' chain is the intake just seated,
      // so re-running the demo always starts from a clean v1.
      if (mock && !isEditFixture) mockVersions.seedBase(d)
    }
    if (isEditFixture) {
      seat(editFixture) // synchronous local fixture — no backend
      return () => { alive = false }
    }
    // The controller is told about the LIVE session only. Mock has no platform
    // session to check, activate or refuse, and touching the state machine from
    // the demo would let a mock round trip clear a live latch.
    if (!mock) sessionActions.checking()
    getSession(mock, DRAWING_SOURCE)
      .then(async ({ intake: d, tenant: t, tier: ti, org: o }) => {
        if (!alive) return
        // A 200 from /api/session IS the platform session, so publish it before
        // any secondary request can report a newer auth failure. In particular,
        // a /versions 401 must remain `required` instead of being overwritten by
        // a late activation after the await. Live only: a mock 200 proves nothing.
        if (!mock) sessionActions.activate({ tenant: t, tier: ti, org: o })
        let drawingSummary = null
        if (!mock) {
          try {
            drawingSummary = await getDrawingVersions(false, REQUESTED_DRAWING_ID)
          } catch {
            // Keep the intake readable, but leave its version unknown. The
            // run-intent gate below refuses live legacy writes in this state.
          }
        }
        if (!alive) return
        seat(d, {
          drawingId: REQUESTED_DRAWING_ID,
          ...(drawingSummary ? { drawingState: drawingSummary } : {}),
        })
        setTenant(t); setTier(ti); setOrg(o)
        // Live auth resolves workspace identity from the verified subject.
        // Persist that server-owned org id so an already-bound account never
        // sees the create-org affordance or attempts a duplicate bootstrap.
        if (!mock && o) adoptOrgId(o)
      })
      .catch((e) => {
        if (!alive) return
        setLoadErr(humanizeError(e))
        if (!mock && is401(e)) {
          // `tokenInvalidated` stays FALSE on purpose: this is render state
          // reporting a refusal, never a verdict on the token. api.js already
          // wiped (and published) any token its own 401 proved bad, and the
          // controller's subscription latched on that. A refusal that indicts
          // nothing deletes nothing — see createSessionController.js's
          // "WHO MAY DELETE THE TOKEN".
          sessionActions.requireAuth('/api/session')
          // Auto-fallback (B1): a VITE_MOCK=0 build that hits a 401 with Auth0
          // unconfigured can't sign in — flip to the demo instead of parking on
          // the gate, so the deployed link lands zero-click. SignedOutGate is
          // kept only for the authConfigured build (the user CAN sign in there).
          if (shouldAutoDemo({ authRequired: true, authConfigured, mock, signedIn: isSignedIn() })) setMock(true)
        }
      })
    return () => { alive = false }
    // `session.recoveries` is the bounded re-entry, and the ONLY dep that
    // changes when localStorage gains a token. The controller re-opens
    // `checking` at most MAX_TOKEN_RECOVERIES times, and only for a token that
    // is present and DIFFERENT from the one the refusal latched on; that bump
    // is what re-runs getSession after a post-callback 401 instead of stranding
    // this page holding a valid token behind a signed-out surface. Identical
    // wiring to ToolCast's session effect.
  }, [mock, isEditFixture, intakeRetryKey, resetCatalogTransient, resetDrawing, seatIntake,
      sessionActions, session.recoveries])

  // Auth0 return leg: if we came back from Universal Login (?code=&state=),
  // finish the exchange + store leaf.jwt, then reload so the fresh loads send
  // the token and land a 200 session (no gate). No-op in mock / when unconfigured.
  useEffect(() => {
    if (mock) return
    handleRedirectCallback().then((stored) => { if (stored) window.location.reload() })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // A 401 clears leaf.jwt in api.js. Mirror that transport event into render
  // state so privileged browser surfaces disappear in the same turn, including
  // when the rejected request originated inside the customization drawer.
  useEffect(() => subscribeUnauthorized(() => {
    setSignedIn(false)
    setCustomizeOpen(false)
  }), [])

  // Per-tenant spend chip: poll GET /api/usage on load (live only). null hides
  // the chip (mock, or the sibling endpoint not deployed yet) — no fake numbers.
  // Real entitlements (live only): fetch on load so the write-tool + build gates
  // reflect the tenant's actual plan. null in mock (full access, all true) or when
  // the sibling endpoint isn't deployed yet (degrades to ungated — honest demo).
  // Real backend diagnostics for the footer chips (live only). Mock keeps the
  // static footer (setHealth(null)); any error -> null -> calm static fallback.
  // Claude account grant (Concern 2): read linkage status on load (live only).
  // null in mock (panel hidden) or when the sibling endpoint isn't deployed yet
  // (the affordance then degrades to today's ungated authoring — no gate, no
  // fabricated "linked" claim). Never fetches or holds the token itself.
  // Projects workspace (item 1): fetch the org's projects (live only). No org
  // stored -> empty list + no error (the switcher offers "create workspace org").
  // Platform down (no DATABASE_URL / 500) -> projectsErr drives the graceful
  // "projects unavailable" note. Mock -> zero /api calls (no surface).
  // Checkout lock (item 3): the controller reads the current drawing's version
  // manifest and picks up its `checkout` (the sibling contract adds it to
  // /versions). Unknown and failed reads stay fail-closed, and its sequence
  // fence stops a stale response re-enabling writes after the drawing changes.
  // All of that is createCheckoutController.js now, proven by its own suites
  // rather than re-derived here.

  // Prefer ending checkout authority before leaving this origin for Auth0. If
  // the release cannot complete, login still proceeds and the marked, one-use
  // auth-return handoff preserves the capability behind the Web Lock.
  //
  // The retired copy also converged the rendered lock state by hand
  // (setCheckout(null) + setCheckoutUnknown(false)) so a REJECTED redirect
  // could not leave a stale "You hold the edit lock" control behind a dead
  // capability. The controller's release does better than that: it re-reads
  // /versions, so the control shows the SERVER's answer, and a read that 401s
  // on the way to sign-in lands unknown + read-failed, which is fail-closed.
  // Byte-identical to /try's signInWithCheckoutRelease.
  const onLogin = useCallback(async () => {
    if (checkout.actions.getCapability()) await checkout.actions.release()
    await login()
  }, [checkout.actions])

  // Tab-close reap beacon: on pagehide / tab-hidden, if a durable in-flight job
  // pointer exists, sendBeacon POST /api/jobs/{id}/close so the backend flags the
  // abandoned WorkItem closable (orphan reaper fails it) instead of billing until
  // the heartbeat window. Live only (mock has no server job). Idempotent
  // server-side; the localStorage re-attach path is untouched — if the user
  // returns quickly the re-attach still re-fetches the record (which may have
  // completed before the reaper swept). Absolute URL respects VITE_API_BASE.
  // Refresh the live job rail on demand (after a run completes, so the job shows
  // immediately rather than waiting for the next poll). No-op in mock.
  // Poll the recent-jobs list (live only; zero /api calls in mock). A 401 means
  // there is NO session — polling forever would just hammer the API with error
  // traffic, so the poll STOPS on the first 401 and resumes only when the mode
  // flips (the effect re-runs). Transient non-auth errors keep polling.
  // A live job entered 'running' — begin (or resume) the wall-clock, using the
  // server's started_at when known (localhost clocks match) so a re-attach
  // continues the real elapsed time instead of restarting at zero.
  // Tick the calm "running · N.Ns" clock while a live job runs (no animated loader).
  // --- NT2 toast plumbing (one slot — newest replaces; Toast auto-fades ~5s) --
  const showToast = useCallback((t) => {
    toastSeqRef.current += 1
    setToast({ id: toastSeqRef.current, ...t })
  }, [])
  const onToastDone = useCallback((id) => {
    setToast((cur) => (cur && cur.id === id ? null : cur))
  }, [])
  const viewResult = useCallback(() => {
    resultBlockRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [])
  const viewViewer = useCallback(() => {
    workspaceCardRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [])

  const completedVersionRef = useRef(null)
  // W4g-2 (one head): true while the browser engine holds edits nobody
  // saved (EngineSessionProvider reports the change). A catalog write tool
  // would move the server head under them, so it is refused with the reason
  // until the drafter saves or discards.
  const [engineDirty, setEngineDirty] = useState(false)
  // The same fact as a ref, for the EXECUTION-time check in onRun: a confirm
  // that awaited the catalog refetch holds the onRun it started with, so a
  // closure read there could be older than the edit that made the engine
  // dirty (kimi on #1008, finding 1). The state drives the ribbon's reasons;
  // the ref drives the refusal.
  const engineDirtyRef = useRef(false)
  const onEngineDirtyChange = useCallback((dirty) => {
    engineDirtyRef.current = !!dirty
    setEngineDirty(!!dirty)
  }, [])
  const {
    jobs,
    currentJobId,
    currentJob,
    inflight: inflightPtr,
    reattaching,
    running,
    status: runStatus,
    progress: runProgress,
    elapsedMs: runElapsedMs,
    result,
    error: runErr,
    runJob,
    attachJob: attachSharedJob,
    detachJob: interruptRun,
    reportError: setRunErr,
    clearError: clearRunErr,
    adoptEnvelope,
    refreshJobs,
  } = useJobController({
    mock,
    resetKey: `${isEditFixture}:${intakeRetryKey}`,
    formatError: humanizeError,
    // W2b: the RISING edge only, exactly as /try wires it. useJobController
    // publishes a two-way boolean (`setAuth(false)` on every successful jobs
    // read), and feeding that `false` back in was the console's last-writer-
    // wins hazard: a jobs 200 racing a /api/session 401 silently dismissed the
    // gate over a session that never loaded. Leaving `required` is now the
    // controller's alone — a /api/session 200 (activate) or the bounded token
    // recovery, both of which RE-VERIFY instead of guessing.
    onAuthRequired: (required) => { if (required) sessionActions.requireAuth('jobs') },
    onNotice: ({ text }) => showToast({ text, action: { label: 'View', onClick: viewResult } }),
    onCompleteVersion: (...args) => completedVersionRef.current?.(...args),
  })
  drawingErrorRef.current = setRunErr

  // Turn-authority provider for the author panel's stage POST (server fail-
  // closes without it: X-Authority-Session-Id/-Turn-Id naming an ACTIVE turn
  // whose subject matches the caller). Reuses a self-minted turn within TTL
  // (a wide margin under the server's TURN_MAX_S default of 300s) rather than
  // reusing an arbitrary composer turn: useConverseSessionController exposes
  // no live "still active" signal for turns already in flight (its `turns`
  // entries are a one-time snapshot from turn start, never updated to
  // terminal), so trusting one here could hand the server a turn id that has
  // already ended. Minting is the safe default; the server 409s harmlessly if
  // this races and loses.
  const agentSessionIdRef = useRef(agentSessionId)
  useEffect(() => { agentSessionIdRef.current = agentSessionId }, [agentSessionId])
  const authorAuthorityRef = useRef(null) // { sessionId, turnId, mintedAt }
  const authorAuthorityProvider = useCallback(async (description, { allowSecretOnce = false } = {}) => {
    // No entitlement pre-check here: entitlements load async, and a stage
    // click can beat them (proven by the e2e). A mint against a tenant that
    // truly cannot converse just fails and falls through to null, which the
    // server answers with its own fail-closed refusal.
    const cached = authorAuthorityRef.current
    if (cached && cached.sessionId === agentSessionIdRef.current
        && Date.now() - cached.mintedAt < AUTHOR_AUTHORITY_TTL_MS) {
      return { sessionId: cached.sessionId, turnId: cached.turnId }
    }
    try {
      // `allowSecretOnce` must reach this mint too: it starts a converse turn
      // on the SAME guarded transport (startTurn -> the wire), so an
      // AuthorPanel "Send anyway" re-stage with credential-shaped text would
      // otherwise have its authority mint refused here and silently fall
      // back to null-authority — a refusal the click never saw or overrode.
      const response = await startAgentTurn(description, { source: 'author_panel', purpose: 'stage_authority' }, { allowSecretOnce })
      // The response's own session id, never the state-fed ref alone: the
      // first mint resolves before React has re-rendered the fresh sessionId.
      const sessionId = response?.session_id || agentSessionIdRef.current
      if (!sessionId || !response?.turn_id) return null
      authorAuthorityRef.current = { sessionId, turnId: response.turn_id, mintedAt: Date.now() }
      return { sessionId, turnId: response.turn_id }
    } catch {
      return null
    }
  }, [startAgentTurn])

  const authorStage = useAuthorStageController({ mock, authorityProvider: authorAuthorityProvider })
  authorPendingRef.current = !!authorStage.pointer

  useEffect(() => {
    const pending = authorStage.pointer
    if (!pending) return
    setAuthorTargetTool(pending.target_tool_name || null)
    setAuthorSeed(pending.description || '')
    setAuthorSignal((value) => value + 1)
    setAuthorOpen(true)
  }, [authorStage.pointer?.idempotency_key])

  // Tab-close survivability: on load in live mode, if a durable in-flight job
  // pointer exists, re-attach. Terminal already -> render its envelope; still
  // running -> resume calm progress + final render. The rail shows a re-attach
  // chip while this runs. Clear the pointer either way.
  /* Legacy reattach is disabled; useJobController owns this lifecycle.
  useEffect(() => {
    if (mock) { setInflightPtr(null); setReattaching(false); return }
    const saved = readInflight()
    if (!saved || !saved.job_id) return
    setInflightPtr(saved)
    let alive = true
    const seq = runSeqRef.current // Esc-interrupt / a new run bumps this to detach us
    const attached = () => alive && runSeqRef.current === seq
    ;(async () => {
      let rec
      try {
        rec = await getJob(saved.job_id)
      } catch {
        clearInflight(); setInflightPtr(null) // 404 / unreachable -> stale pointer
        return
      }
      if (!attached()) return
      setSelectedTool((t) => t || { name: saved.tool })
      setCurrentJobId(saved.job_id)
      if (rec.status === 'complete' || rec.status === 'failed') {
        setResult(recordToEnvelope(rec))
        if (rec.status === 'complete') {
          showToast({
            text: `${saved.tool || rec.tool || 'job'} complete`,
            action: { label: 'View', onClick: viewResult },
          })
        }
        clearInflight(); setInflightPtr(null)
        return
      }
      // still in flight — resume progress and await the terminal envelope
      setRunning(true); setRunErr(null); setResult(null); setReattaching(true)
      if (rec.status === 'running') markRunning(rec.started_at)
      else setRunStatus(rec.status || 'submitted')
      try {
        const env = await attachToJob(saved.job_id, {
          onStatus: (st) => {
            if (!attached()) return
            setRunProgress(st.progress || null)
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
        if (attached()) {
          setResult(env)
          if (env?.ok) {
            showToast({
              text: `${saved.tool || env.tool || 'job'} complete`,
              action: { label: 'View', onClick: viewResult },
            })
          }
        }
      } catch (e) {
        if (attached()) setRunErr(humanizeError(e))
      } finally {
        if (attached()) {
          setRunning(false); setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
          setReattaching(false); setInflightPtr(null)
        }
        clearInflight()
        refreshJobs()
      }
    })()
    return () => { alive = false }
  }, [mock, markRunning, refreshJobs, showToast, viewResult])
  */

  // Count ALL entity kinds per layer (polylines + inserts + 3DFACEs) so
  // insert/face-only layers (e.g. the ?fixture=edit Blocks/Surfaces layers)
  // stop reading 0 in the legend.
  const layerCounts = useMemo(() => countEntitiesByLayer(shown), [shown])

  // resolve the picked handle to an entity descriptor for the readout
  const selection = useMemo(() => selectEntity(shown, selectedHandle, {
    onUnresolved: (handle) => ({ handle, kind: 'entity', layer: null }),
  }), [selectedHandle, shown])
  // W4c-V2: the raw intake entity behind the selection, resolved in place -
  // selectEntity deliberately drops geometry and its descriptor shape is
  // pinned by exact-shape tests, so the dock derives from the intake here.
  // Scope-reset for free: a drawing/tenant switch clears selectedHandle and
  // replaces `shown`, so no stale geometry can survive the switch.
  const selectedEntityGeometry = useMemo(() => {
    if (!selectedHandle || !shown) return null
    const entity = (shown.polylines || []).find((e) => e.handle === selectedHandle)
      || (shown.inserts || []).find((e) => e.handle === selectedHandle)
      || (shown.faces3d || []).find((e) => e.handle === selectedHandle)
    if (!entity) return null
    return entityGeometry(entity, selection?.kind)
  }, [shown, selectedHandle, selection])

  // Swap the viewer + panels to a drawing version (§11). The completed event
  // ("Version 2 created" / "Reverted to version 1") fires the NT2 toast.
  const seatVersion = useCallback((view, drawingId, note) => {
    seatDrawingVersion(view, { drawingId, source: 'version' })
    if (note) showToast({ text: `${note} · ${drawingId}`, action: { label: 'View', onClick: viewViewer } })
  }, [seatDrawingVersion, showToast, viewViewer])

  const seatCompletedVersion = useCallback(async (newVersion, envelope) => {
    let version = newVersion?.version
    if (mock) {
      try {
        if (!mockVersions.isSeeded() && intake) mockVersions.seedBase(intake)
        const commit = mockVersions.applyDelete(envelope?.result?.removed)
        version = commit.version
      } catch {
        showToast({ text: `Version ${version} created` })
        markRefreshFailure({ drawing_id: newVersion.drawing_id, version })
        return
      }
    }
    if (envelope?.result?.new_version_readable === false) {
      recordCommittedUnreadableHead(newVersion)
      showToast({ text: `Version ${version} created` })
      return
    }
    try {
      const view = await getDrawingIntake(mock, newVersion.drawing_id, 'head')
      seatVersion(view, newVersion.drawing_id, `Version ${version} created`)
    } catch {
      showToast({ text: `Version ${version} created` })
      markRefreshFailure({ drawing_id: newVersion.drawing_id, version })
    }
  }, [intake, markRefreshFailure, mock, recordCommittedUnreadableHead, seatVersion, showToast])
  completedVersionRef.current = seatCompletedVersion

  // P2 wave C-2: engagement depth (real CAD work). ONE event for the four
  // version-navigation gestures; action is the closed vocabulary
  // undo/redo/history/preview, counted only when the navigation happened.
  const onUndo = useCallback(async () => {
    const view = await undoDrawingVersion()
    if (view) {
      track('drawing.version_navigated', { action: 'undo' })
      showToast({ text: `Reverted to version ${view.head} · ${drawingState?.drawing_id}`, action: { label: 'View', onClick: viewViewer } })
    }
  }, [drawingState, showToast, undoDrawingVersion, viewViewer])

  const onRedo = useCallback(async () => {
    const view = await redoDrawingVersion()
    if (view) {
      track('drawing.version_navigated', { action: 'redo' })
      showToast({ text: `Advanced to version ${view.head} · ${drawingState?.drawing_id}`, action: { label: 'View', onClick: viewViewer } })
    }
  }, [drawingState, redoDrawingVersion, showToast, viewViewer])

  const onToggleHistoryTracked = useCallback(() => {
    if (!historyOpen) track('drawing.version_navigated', { action: 'history' })
    onToggleHistory()
  }, [historyOpen, onToggleHistory])

  const onPreviewVersionTracked = useCallback((...args) => {
    track('drawing.version_navigated', { action: 'preview' })
    return onPreviewVersion(...args)
  }, [onPreviewVersion])

  // --- version-history browser + read-only preview -------------------------
  // --- projects / orgs workspace handlers (item 1) -------------------------
  // Re-hydrate the open project (after a terminal run so jobs[] visibly grows).
  // Both creators accept the name from an inline F1 field (ProjectSwitcher);
  // the window.prompt fallback only fires when no name is passed (legacy path —
  // native dialogs are off-standard and slated for removal with the switcher).
  const onCreateOrg = useCallback(async (givenName) => {
    const name = typeof givenName === 'string'
      ? givenName
      : window.prompt('Name your workspace org', 'My workspace')
    if (name == null) return
    return createWorkspaceOrg(name)
  }, [createWorkspaceOrg])

  const onCreateProject = useCallback(async (givenName) => {
    const name = typeof givenName === 'string'
      ? givenName
      : window.prompt('New project name', 'rooftop demo')
    if (name == null || !name.trim()) return
    return createWorkspaceProject(name)
  }, [createWorkspaceProject])

  const catalogRunContext = useMemo(() => createCatalogRunContext({
    tenantId: tenant || config.tenant,
    orgId,
    projectId: openProjectId || null,
    workspace,
    selectedVersionId: canonicalVersionId,
    drawingState,
    fallbackDrawingId: REQUESTED_DRAWING_ID,
  }), [tenant, orgId, openProjectId, workspace, canonicalVersionId, drawingState])
  const catalogRunContextRef = useRef(catalogRunContext)
  catalogRunContextRef.current = catalogRunContext

  const prepareRunParams = useCallback((tool, params) => {
    const isWrite = (tool.capabilities || []).includes('drawing.write')
    const overlays = selectedHandle
      ? { target_handle: selectedHandle, ...(isWrite ? { handle: selectedHandle } : {}) }
      : {}
    return prepareCatalogRunParams(tool, params, catalogRunContextRef.current, overlays)
  }, [selectedHandle])

  const confirmEmitRef = useRef(null) // P2: run.confirm_shown de-dupe (tour double-arm)
  const tourDispatchRef = useRef(false) // P2: true only while a tour beat's dispatch is in flight
  // P2: committed snapshot for the Esc-interrupt emit. Assigned in an effect
  // (post-commit), not during render, so an abandoned concurrent render can
  // never tear a wrong tool/elapsed pair into the handler; kept out of the
  // Esc listener's deps because elapsed ticks every 1s (useJobController).
  const interruptSnapshotRef = useRef({ tool: null, elapsedMs: null })
  useEffect(() => {
    interruptSnapshotRef.current = { tool: selectedTool?.name || null, elapsedMs: runElapsedMs }
  })
  const armDecision = useCallback((decision) => {
    if (decision?.lane !== 'run') {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      return decision
    }
    // Family catalog entries are presentation-normalized. Resolve the canonical
    // flat-catalog record before snapshotting so confirmation compares the same
    // server-sourced definition that RoutePanel will execute.
    // A caller that already refetched (the authored "Run it now" path) hands
    // its resolved record over: `tools` in this closure is the PRE-refetch
    // list, so trusting it would snapshot the provisional record the resolver
    // just rejected. Same preference ToolCast's armCatalogDecision applies.
    const refreshedTool = decision.refreshedTool?.name === decision.tool
      ? decision.refreshedTool
      : null
    const catalogTool = refreshedTool || tools.find((candidate) => candidate.name === decision.tool)
    if (!catalogTool) {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      return decision
    }
    // A resolved platform session is the authority gate. Off-auth local stacks
    // intentionally omit the auth-only tenant echo and use config.tenant for
    // their X-Tenant-Id stub, so `tenant` itself cannot be the live-run gate.
    if (!mock && session.status !== 'active') return
    if (!catalogRunContextRef.current) {
      setRunErr('This workspace has no canonical drawing version to run. Import a drawing first.')
      return
    }
    const isWrite = isWriteTool(catalogTool)
    if (
      !mock
      && isWrite
      && catalogRunContextRef.current.projectId == null
      && catalogRunContextRef.current.drawingVersion == null
    ) {
      setRunErr('The current drawing version is not ready. Refresh the drawing before running a write tool.')
      return
    }
    if (running || previewing || (isWrite && (writeLocked || !canRunWrite))) return
    // W4g-2 (one head): every run path (ribbon, rail, slash, the natural-
    // language route, the tour) arms through here, so this is the one place
    // a write tool is refused while the browser engine holds unsaved edits.
    if (isWrite && engineDirty) {
      setRunErr(`${catalogTool.name} not run: ${REASONS.unsavedEngineEdits}.`)
      return
    }
    const prepared = prepareRunParams(catalogTool, decision.params)
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `${runIntentSessionRef.current}:${++runIntentSeqRef.current}`,
      toolName: catalogTool.name,
      params: prepared,
      context: catalogRunContextRef.current,
      toolSnapshot: createCatalogToolSnapshot(catalogTool),
    })
    runIntentStateRef.current = staged.state
    const { refreshedTool: _refreshedTool, ...publicDecision } = decision
    const armed = {
      ...publicDecision,
      tool: catalogTool.name,
      params: staged.intent.params,
      runIntent: staged.intent,
    }
    // P2: confirm-to-run conversion top. `source` comes from the decision's
    // own construction site (catalog/tour stamp source; slash decisions
    // carry slash:true; the NL route is 'prompt'). A tour beat's FIRST arm
    // comes through the dispatch route, which knows nothing of the tour, so
    // the in-flight flag reattributes it; the tour's catalog re-arm is then
    // the de-duped duplicate (review #427 round-2 warn 5 + round-3 warn 1).
    const emitKey = `${catalogTool.name}`
    const nowTs = Date.now()
    const last = confirmEmitRef.current
    if (!(last && last.key === emitKey && nowTs - last.ts < 2000)) {
      const source = decision.source
        || (tourDispatchRef.current ? 'tour' : decision.slash ? 'slash' : 'prompt')
      // `source` rides the ref so run.confirmed can report the SAME
      // provenance and ms_since_shown without re-deriving either.
      confirmEmitRef.current = { key: emitKey, ts: nowTs, source }
      track('run.confirm_shown', {
        tool: catalogTool.name,
        is_write: isWrite,
        source,
      })
    }
    return armed
  }, [tools, mock, session.status, prepareRunParams, running, previewing, writeLocked, canRunWrite, catalogRunContext, engineDirty])
  catalogUiRef.current = { armDecision, startAgentTurn, running }

  const onRequestCatalogRun = useCallback((tool, params, rationale = null, source = 'catalog') => {
    if (!tool) return
    return commitCatalogDecision({
      lane: 'run', tool: tool.name, params, confidence: 1,
      rationale: rationale || 'Catalog selection. Confirm the exact tool and parameters before it runs.',
      alternatives: [],
      source, // P2: run.confirm_shown attribution (catalog by default, tour from the tour beat)
    })
  }, [commitCatalogDecision])

  /* Legacy inline run lifecycle is disabled; useJobController owns it.
  const onRun = useCallback(async (tool, params, {
    intentConfirmed = false, runContext = null, idempotencyKey = null,
  } = {}) => {
    // Read-only version preview never mutates head — Run is disabled while previewing.
    if (previewing) return
    // Single-writer lock (item 3): write tools are suppressed while another
    // session holds the checkout; read tools are unaffected. Defensive guard —
    // the Run buttons for write tools are already disabled while locked.
    if (writeLocked && (tool.capabilities || []).includes('drawing.write')) return
    const seq = ++runSeqRef.current // Esc-interrupt detaches this run's handlers
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute(null); setRouteErr(null) // the decision strip is consumed by the run
    // Race tier (wire §11): taking the chip makes the deterministic run the
    // answer — stop RENDERING the agent stream. The turn itself may complete
    // server-side and stays persisted in the transcript; it is never cancelled.
    setAgentMode((m) => (m === 'race' ? null : m))
    setSelectedTool(tool)
    setRunning(true); setRunErr(null); setResult(null)
    setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
    setOverlayStale(false); setCurrentJobId(null); setRefreshFail(null)
    const merged = intentConfirmed ? params : prepareRunParams(tool, params)
    const executionContext = runContext || catalogRunContext
    lastRunRef.current = { tool, params: merged }
    // feed the picked entity to the tool so an edit tool can target it. A write
    // tool (delete-marked-panel) reads `handle`, so map the selection onto it
    // too — that makes the pending ghost's previewed deletion the real target.
    try {
      let env
      if (mock) {
        // Mock stays fully client-side — no jobs API, no progress phases.
        env = await runTool(mock, tool, merged, shown)
      } else {
        env = await runToolAsync(tool, merged, executionContext.drawingId, {
          // When a project is open, link the run so a platform Job row is recorded
          // (X-Org-Id + X-Project-Id) and the workspace jobs[] grows.
          orgId: executionContext.orgId || undefined,
          projectId: executionContext.projectId || undefined,
          dwgVersion: executionContext.drawingVersion ?? undefined,
          idempotencyKey: idempotencyKey || undefined,
          catalogDigest: (runContext?.toolSnapshot?.catalogDigest
            || createCatalogToolSnapshot(tool).catalogDigest || undefined),
          onSubmit: (job_id) => { saveInflight(job_id, tool.name); setCurrentJobId(job_id) },
          onStatus: (st) => {
            // Richer progress string (e.g. 'executing' · 'storing version' ·
            // 'extracting') when the backend emits it; null falls back to status.
            setRunProgress(st.progress || null)
            if (st.status === 'running') markRunning()
            else setRunStatus(st.status || 'running')
          },
        })
      }
      if (runSeqRef.current !== seq) return // interrupted — the rail keeps the job
      setResult(env)
      // Completed event -> NT2 toast (bottom-center, quiet View action).
      if (env?.ok) {
        showToast({ text: `${tool.name} complete`, action: { label: 'View', onClick: viewResult } })
      }
      // Write loop (§11): a drawing.write run stamps result.new_version. Fetch
      // the fresh head intake and swap the viewer to the new version.
      if (!mock && env?.ok && env.result?.new_version) {
        const nv = env.result.new_version
        try {
          const view = await getDrawingIntake(mock, nv.drawing_id, 'head')
          seatVersion(view, nv.drawing_id, `Version ${nv.version} created`)
        } catch {
          // Completed act -> plain NT2 toast; the failed refresh surfaces as an
          // X1 red row at the viewer card (a failed act is never a toast).
          showToast({ text: `Version ${nv.version} created` })
          setRefreshFail({ drawing_id: nv.drawing_id, version: nv.version })
        }
      }
      // MOCK write loop (M3): the same beat, served by the in-memory chain —
      // commit v2 locally, then seat it through the identical seatVersion path
      // so Undo / Redo / History light up in the demo.
      if (mock && env?.ok && env.result?.new_version) {
        const nv = env.result.new_version
        // The engine's `new_version.version` is hardcoded to 2; only the chain
        // knows the real appended version, so a SECOND delete must read v3 here.
        let commit = null
        try {
          if (!mockVersions.isSeeded() && intake) mockVersions.seedBase(intake)
          commit = mockVersions.applyDelete(env.result.removed)
          const view = await getDrawingIntake(mock, nv.drawing_id, 'head')
          seatVersion(view, nv.drawing_id, `Version ${commit.version} created`)
        } catch {
          // Completed act -> plain NT2 toast; the failed refresh surfaces as an
          // X1 red row at the viewer card (a failed act is never a toast).
          showToast({ text: `Version ${commit?.version ?? nv.version} created` })
          setRefreshFail({ drawing_id: nv.drawing_id, version: commit?.version ?? nv.version })
        }
      }
    } catch (e) {
      if (runSeqRef.current === seq) setRunErr(humanizeError(e))
    } finally {
      if (runSeqRef.current === seq) {
        setRunning(false)
        setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
      }
      if (!mock) {
        clearInflight(); refreshJobs(); loadUsage(); loadCheckout()
        // re-hydrate the open project so its jobs[] reflects the just-finished run
        if (openProjectId) rehydrate()
      }
    }
  }, [mock, shown, intake, selectedHandle, markRunning, seatVersion, refreshJobs, previewing, loadUsage,
      loadCheckout, writeLocked, openProjectId, rehydrate, showToast, viewResult, prepareRunParams, catalogRunContext])
  */

  const onRun = useCallback(async (tool, params, {
    intentConfirmed = false, runContext = null, idempotencyKey = null,
  } = {}) => {
    if (previewing) return null
    const isWrite = isWriteTool(tool)
    if (writeLocked && isWrite) return null
    // W4g-2 (one head), held at EXECUTION time. armDecision refuses the click
    // that stages a run, but the confirm strip stays open while the drafter
    // keeps drawing (engine tools never touch the run intent), so an edit
    // made between arming and confirming reaches here with the engine dirty.
    // Every path to a run ends in this call (the confirm click, the mock
    // path, the tour), and the ref carries the current fact, not a closure's.
    if (isWrite && engineDirtyRef.current) {
      setRunErr(`${tool.name} not run: ${REASONS.unsavedEngineEdits}.`)
      return null
    }
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    // ranTool lets the controller resolve a shown route honestly: this run IS
    // the routed tool -> accepted; a different tool -> invalidated.
    dismissRoute({ ranTool: tool.name })
    if (agentMode === 'race') clearAgentMode()
    setSelectedTool(tool)
    setOverlayStale(false); markRefreshFailure(null)
    const merged = intentConfirmed ? params : prepareRunParams(tool, params)
    const executionContext = runContext || catalogRunContext
    lastRunRef.current = { tool, params: merged }

    const envelope = await runJob({
      toolName: tool.name,
      execute: ({ onSubmit, onStatus }) => (mock
        ? runTool(mock, tool, merged, shown)
        : runToolAsync(tool, merged, executionContext.drawingId, {
          orgId: executionContext.orgId || undefined,
          projectId: executionContext.projectId || undefined,
          dwgVersion: drawingVersionForRun(tool, executionContext, health?.aps_live),
          idempotencyKey: idempotencyKey || undefined,
          catalogDigest: (runContext?.toolSnapshot?.catalogDigest
            || createCatalogToolSnapshot(tool).catalogDigest || undefined),
          checkoutCapability: checkout.actions.getCapability() || undefined,
          onSubmit,
          onStatus,
        })),
    })

    if (!mock) {
      loadUsage(); checkout.actions.refresh()
      if (openProjectId) rehydrate()
    }
    return envelope
  }, [agentMode, catalogRunContext, checkout.actions, clearAgentMode, dismissRoute, loadUsage, mock,
    health?.aps_live, openProjectId, prepareRunParams, markRefreshFailure, previewing, rehydrate, runJob,
    shown, writeLocked])

  const onConfirmCatalogRun = useCallback(async (intent, tool, params) => {
    let currentTool = tool
    let toolSnapshot
    try {
      if (!mock) {
        const latestTools = await getTools(false)
        currentTool = latestTools.find((candidate) => candidate.name === intent?.toolName) || null
      }
      toolSnapshot = createCatalogToolSnapshot(currentTool)
    } catch {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current, intent?.intentId)
      dismissRoute({ outcome: 'invalidated' })
      setRunErr('That catalog tool changed or is no longer available. Choose Run again to create a new intent.')
      return
    }
    const confirmed = confirmRunIntent(runIntentStateRef.current, {
      intentId: intent?.intentId,
      sessionId: intent?.sessionId,
      toolName: currentTool?.name,
      params,
      context: catalogRunContextRef.current,
      toolSnapshot,
    })
    runIntentStateRef.current = confirmed.state
    if (!confirmed.ok) {
      dismissRoute({ outcome: 'invalidated' })
      setRunErr('That run confirmation is no longer valid. Choose Run again to create a new intent.')
      return
    }
    // P2 wave C-2: trust-gate friction. This is THE confirm click (the only
    // path from an armed intent to onRun with intentConfirmed). source and
    // ms_since_shown come from the confirm_shown emit's own record; when the
    // ref names a different tool (2s window expired edge), both are omitted
    // rather than guessed.
    {
      const shownRec = confirmEmitRef.current
      const matches = shownRec && shownRec.key === currentTool?.name
      track('run.confirmed', {
        tool: currentTool?.name,
        ...(matches && shownRec.source ? { source: shownRec.source } : {}),
        ...(matches ? { ms_since_shown: Date.now() - shownRec.ts } : {}),
      })
    }
    onRun(currentTool, confirmed.execution.params, {
      intentConfirmed: true,
      runContext: { ...confirmed.execution.context, toolSnapshot: confirmed.execution.toolSnapshot },
      idempotencyKey: confirmed.execution.intentId,
    })
  }, [dismissRoute, mock, onRun])

  // Retry the last run (plain affordance for retryable failures / transport hiccups).
  const onRetry = useCallback(() => {
    const last = lastRunRef.current
    if (last) onRequestCatalogRun(last.tool, last.params)
  }, [onRequestCatalogRun])

  // An agent-dispatched job (job_linked event) -> the SAME §7 attach
  // affordance the tab-close re-attach uses: subscribe to the job, stream
  // progress into the result pane, toast on completion. Never re-submits.
  /* Legacy inline attach lifecycle is disabled; useJobController owns it.
  const onAttachAgentJob = useCallback(async (jobId, toolName) => {
    if (!jobId || mock) return
    const seq = ++runSeqRef.current // Esc-interrupt detaches, like any run
    setSelectedTool({ name: toolName || 'job' })
    setCurrentJobId(jobId)
    setRunning(true); setRunErr(null); setResult(null)
    setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
    try {
      const env = await attachToJob(jobId, {
        onStatus: (st) => {
          if (runSeqRef.current !== seq) return
          setRunProgress(st.progress || null)
          if (st.status === 'running') markRunning()
          else setRunStatus(st.status || 'running')
        },
      })
      if (runSeqRef.current === seq) {
        setResult(env)
        if (env?.ok) {
          showToast({ text: `${toolName || env.tool || 'job'} complete`, action: { label: 'View', onClick: viewResult } })
        }
        // Agent-dispatched writes use the same immutable version contract as
        // catalog runs. Attaching must seat the new head, otherwise the receipt
        // says the write completed while the viewer still shows its parent.
        if (env?.ok && env.result?.new_version) {
          const nv = env.result.new_version
          try {
            const view = await getDrawingIntake(false, nv.drawing_id, 'head')
            seatVersion(view, nv.drawing_id, `Version ${nv.version} created`)
          } catch {
            showToast({ text: `Version ${nv.version} created` })
            setRefreshFail({ drawing_id: nv.drawing_id, version: nv.version })
          }
        }
      }
    } catch (e) {
      if (runSeqRef.current === seq) setRunErr(String(e.message || e))
    } finally {
      if (runSeqRef.current === seq) {
        setRunning(false)
        setRunStatus(null); setRunProgress(null); setRunElapsedMs(null); runningSinceRef.current = null
      }
      refreshJobs()
    }
  }, [mock, markRunning, refreshJobs, seatVersion, showToast, viewResult])
  */

  const onAttachAgentJob = useCallback(async (jobId, toolName) => {
    if (!jobId || mock) return null
    // P2 wave C-2: agent-to-deterministic-run conversion, at the one seam
    // where a chat-dispatched job enters the run pane.
    track('agent.job_linked', { tool: toolName || 'job' })
    setSelectedTool({ name: toolName || 'job' })
    return attachSharedJob(jobId, { toolName: toolName || 'job', persist: true })
  }, [attachSharedJob, mock])

  // X1 Retry for a failed post-write viewer refresh — re-fetch head and seat it.
  const onAuthor = useCallback(async (description, targetToolName = null, opts = {}) => {
    // R5 only stages bytes. It must not place a tool in the runnable catalog.
    // `opts.allowSecretOnce` is the AuthorPanel "Send anyway" authorisation,
    // forwarded as a parameter of this one staging call.
    return authorStage.stage(description, targetToolName, opts)
  }, [authorStage.stage])

  const onReviseAuthoredTool = useCallback((tool) => {
    if (!tool?.name || authorPendingRef.current) return
    setAuthorTargetTool(tool.name)
    setAuthorSeed('')
    setAuthorSignal((value) => value + 1)
    setAuthorOpen(true)
    setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
  }, [])

  const onCancelAuthorRevision = useCallback(() => {
    if (authorPendingRef.current) return
    setAuthorTargetTool(null)
    setAuthorSeed('')
    setAuthorSignal((value) => value + 1)
  }, [])

  const onPublishAuthor = useCallback(async (staged) => {
    try {
      const res = await publishStagedAuthor(mock, staged)
      const tool = res.tool || staged.tool
      if (res.published) {
        upsertTool(tool)
        // The ribbon's Author cluster shows this tool from here on, and says
        // honestly that it is not runnable until the catalog issues its digest.
        setLastAuthoredTool(tool)
        // Re-group the catalog so the new tool lands in "Custom authored tools"
        // (visible re-fetch of the grouped capabilities).
        loadCatalog()
        // Authoring is a ~1-2 min agent run — surface completion as an NT2 toast so
        // it is visible even when the author section is collapsed / scrolled away.
        showToast({
          text: `Tool published — ${tool.name}`,
          action: {
            label: 'View',
            onClick: () => {
              setAuthorOpen(true)
              setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
            },
          },
        })
        authorStage.completePublication()
      }
      return { ...res, tool }
    } finally {
      setTourLanded(true)
    }
  }, [authorStage.completePublication, mock, loadCatalog, showToast, upsertTool])

  // "Run it now" from the author card — prefill the RUN lane (RoutePanel) with
  // the just-authored tool so the user confirms before it runs (paid actions
  // never auto-execute). The tool is already in `tools` (onAuthor added it), so
  // RoutePanel resolves it and shows a single Run.
  // ONE honest path, the same one ToolCast.useAuthoredTool walks: refetch the
  // runnable catalog, resolve the published tool through the single oracle
  // (site/publishedCatalogTool.js, pinned by publishedCatalogTool.test.mjs),
  // and surface its sentence instead of arming a run against a record the
  // catalog has not issued a digest for. This shell used to trust the
  // provisional publish response and arm regardless.
  const onUseAuthored = useCallback(async (tool) => {
    if (!tool) return
    const refreshedTools = await loadCatalogTools()
    let runnableTool
    try {
      runnableTool = resolvePublishedCatalogTool(tool, refreshedTools)
    } catch (cause) {
      showToast({ text: cause?.message || 'The published tool is not ready to run yet.' })
      return
    }
    setLastAuthoredTool(runnableTool)
    commitCatalogDecision({
      lane: 'run', tool: runnableTool.name, params: {}, confidence: 0.99,
      rationale: `Authored just now — confirm to run “${runnableTool.name}”.`,
      alternatives: [],
      // The refetched record, so armDecision snapshots what the catalog just
      // issued rather than this render's stale `tools`.
      refreshedTool: runnableTool,
      // P2 parity with ToolCast: 'authored' is already in the
      // run.confirm_shown source vocabulary.
      source: 'authored',
    })
    setTimeout(() => document.querySelector('main')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
  }, [commitCatalogDecision, loadCatalogTools, showToast])

  // --- prompt-first dispatch (§12) -----------------------------------------
  // Live preview of which lane the text will route to (lights the hero's dots).
  // The slash-completable catalog: every runnable tool the CURRENT plan allows
  // (write tools drop out when the plan lacks run_write — the menu only offers
  // what the end user can actually complete and run).
  // `override` is an optional explicit string — the guided tour's canned prompt,
  // or the menu-picked "/tool" (state hasn't flushed yet). Click handlers pass
  // an event object, which is NOT a string, so the normal "dispatch what's in
  // the bar" path is untouched.
  /* Legacy inline catalog dispatch is disabled; useCatalogController owns it.
  const onDispatch = useCallback(async (override) => {
    const text = (typeof override === 'string' ? override : prompt).trim()
    if (!text || routing || running) return // no new decision while a run is in flight (Esc interrupts first)
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    // Slash fast-path: "/name" is an EXPLICIT invocation — no NL router call.
    // The route decision strip still asks for confirmation before anything runs.
    if (text.startsWith('/')) {
      const name = text.slice(1).split(/\s+/)[0]
      if (!name) return
      setRoute(null); setRouteErr(null)
      const t = tools.find((x) => (x.name || '').toLowerCase() === name.toLowerCase())
      if (t) {
        armDecision({
          lane: 'run', tool: t.name, params: {}, confidence: 1,
          rationale: `Explicit /${t.name} — you picked this tool.`,
          alternatives: [], slash: true,
        })
      } else {
        // Unknown name -> the resolver rows offer the nearest catalog matches:
        // substring hits first, else longest-common-prefix ≥ 3 (catches
        // trailing typos like /count-by-layre -> count-by-layer).
        const q = name.toLowerCase()
        const lcp = (a, b) => {
          let n = 0
          while (n < a.length && n < b.length && a[n] === b[n]) n++
          return n
        }
        const near = tools
          .map((x) => {
            const nm = (x.name || '').toLowerCase()
            return { x, score: nm.includes(q) ? 1000 + q.length : lcp(nm, q) }
          })
          .filter((s) => s.score >= 3)
          .sort((a, b) => b.score - a.score)
          .slice(0, 3)
          .map((s) => ({ tool: s.x.name, description: s.x.description }))
        setRoute({ lane: 'run', tool: name, params: {}, confidence: 0, alternatives: near, slash: true })
      }
      return
    }
    setRouting(true); setRoute(null); setRouteErr(null)
    try {
      const r = await nlPrompt(mock, text, tools)
      // Two-tier dispatch (wire contract §11). Tier 1 (the deterministic §12
      // classifier above) never changes; the agent tier is ADDITIVE and only
      // exists in live mode with the converse entitlement (agentDisabled).
      const conf = Number(r.confidence) || 0
      const openAuthorFlow = () => {
        setAuthorSeed(text)
        setAuthorSignal((n) => n + 1)
        setAuthorOpen(true)
        // bring the (left-rail) author flow into view
        setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
      }
      const chipOnly = agentDisabled ||
        (r.lane === 'run' && !!r.tool && conf >= THRESHOLDS.CHIP_ONLY)
      if (chipOnly) {
        // Today's behavior verbatim (also the whole story when the agent tier
        // is disabled: mock, or a plan without converse).
        armDecision(r)
        if (r.lane === 'build') openAuthorFlow()
      } else {
        const hint = { lane: r.lane, tool: r.tool || null, confidence: conf, rationale: r.rationale || null }
        setAgentBanner(null)
        if (r.lane === 'run' && !!r.tool && conf >= THRESHOLDS.RACE_MIN) {
          // RACE band: today's chip stays primary AND an agent turn starts
          // alongside. A failed agent start never degrades the chip.
          armDecision(r)
          try {
            await startAgentTurn(text, hint)
            setAgentMode('race')
          } catch (e) {
            setAgentBanner(agentBannerFor(e))
          }
        } else {
          // AGENT primary: low confidence, build/solve lane, or no match.
          try {
            await startAgentTurn(text, hint)
            setAgentMode('primary')
          } catch (e) {
            // Degraded fallback: EXACTLY today's Tier-1 rendering + calm banner.
            armDecision(r)
            if (r.lane === 'build') openAuthorFlow()
            setAgentBanner(agentBannerFor(e))
          }
        }
      }
      // main made onDispatch return the routed decision to its callers; the
      // agent tier above is additive, so the return contract is preserved.
      return r
    } catch (e) {
      // A failed routing call is a FAILED act — it rides the red strip above
      // the well (with Retry + its key), never a fake confidence-0 route card.
      setRouteErr(humanizeError(e))
    } finally {
      setRouting(false)
    }
  }, [prompt, routing, running, mock, tools, agentDisabled, startAgentTurn, armDecision])

  // Typing invalidates a shown route/failure — the decision must match the text.
  const onPromptChange = useCallback((v) => {
    setPrompt(v)
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    setRoute((r) => (r ? null : r))
    setRouteErr((e) => (e ? null : e))
  }, [])

  // Pick an alternative from a low-confidence / live-only route -> a user-picked
  // (high-confidence) run route for that capability.
  const onPickAlternative = useCallback((name) => {
    const prev = route
    armDecision({
      lane: 'run', tool: name, params: {}, confidence: 0.99,
      rationale: 'You picked this capability from the alternatives.',
      alternatives: (prev?.alternatives || []).filter((a) => a.tool !== name),
      stub: prev?.stub, stubReason: prev?.stubReason,
    })
  }, [armDecision, route])
  */

  // `options` carries the composer's per-call flags, including the
  // "Send anyway" authorisation (`allowSecretOnce`). It MUST be forwarded: the
  // override is a parameter on this one dispatch and nothing stores it, so a
  // dropped option means the click silently does nothing rather than silently
  // arming something. The `running` short-circuit above is exactly the
  // short-circuit that stranded round 2's latch; with a parameter it is safe.
  const onDispatch = useCallback((override, options) => {
    if (running) return undefined
    return catalogActions.dispatch(override, options)
  }, [catalogActions, running])

  const onOpenAuthor = useCallback(() => {
    if (!authorPendingRef.current) setAuthorTargetTool(null)
    setAuthorOpen(true)
    setTimeout(() => authorSectionRef.current?.scrollIntoView({ block: 'nearest' }), 0)
  }, [])

  // --- M5 guided tour: canned prompts ride the REAL handlers ----------------
  // The tour types its beat into the real command bar, dispatches it through the
  // real nl-prompt router (onDispatch), and — for a read-only run beat — runs it
  // through the same guarded intent path. NOTHING here fabricates a result.
  // Write beats (the versioned delete) deliberately stop at the confirm card;
  // paid/destructive actions never auto-execute, tour or not.
  const onCannedPrompt = useCallback(async (text, step) => {
    if (!text) return
    // Cancellation token: Exit/Skip must stop the bar mid-character instead of
    // typing on after the tour is gone, and a rapid Back must supersede the
    // in-flight beat rather than interleave two typing loops.
    const seq = (cannedSeq.current += 1)
    setTourLanded(false)
    // self-type into the real bar so the audience sees the sentence being written
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    // A leftover route at a tour beat's start was superseded, not user-dismissed.
    dismissRoute({ outcome: 'invalidated' })
    for (let i = 1; i <= text.length; i += 1) {
      if (cannedSeq.current !== seq) return
      onPromptChange(text.slice(0, i))
      // eslint-disable-next-line no-await-in-loop
      await new Promise((res) => setTimeout(res, 22))
    }
    if (cannedSeq.current !== seq) return
    let r = null
    try {
      // The dispatch route arms the decision itself; the flag makes that arm
      // (the one that actually emits) say 'tour' instead of 'prompt'.
      tourDispatchRef.current = true
      try {
        r = await onDispatch(text)
      } finally {
        tourDispatchRef.current = false
      }
      if (cannedSeq.current !== seq) return
      if (r && r.lane === 'run' && step?.action === 'run') {
        const toolObj = tools.find((t) => t.name === r.tool)
        const isWrite = (toolObj?.capabilities || []).includes('drawing.write')
        if (toolObj && !isWrite) {
          // Kept on ONE line: scripts/check_run_intent.mjs pins this exact
          // call shape as the PR107 intent-contract witness.
          onRequestCatalogRun(toolObj, r.params || {}, 'Guided tour selection. Confirm before it runs.', 'tour')
        }
      }
    } finally {
      // The BUILD lane hands off to AuthorPanel's auto-submit, whose onAuthor
      // finally owns `landed` — flipping it here would unlock Next before the
      // tool is actually authored, which is the whole differentiator beat.
      if (cannedSeq.current === seq && !(r && r.lane === 'build')) setTourLanded(true)
    }
  }, [dismissRoute, onDispatch, onPromptChange, onRequestCatalogRun, tools])

  const onTourStepChange = useCallback((index) => {
    tourStepRef.current = index
    const stepId = TOUR_STEPS[index]?.id
    setTourStep(stepId)
    track('tour.step_reached', { step_id: stepId })
  }, [])

  const onTourExit = useCallback(() => {
    // Leaving the tour keeps you exactly where you are — in mock, on the same
    // drawing, with your last real result on screen.
    track('tour.exited', {
      at_step: TOUR_STEPS[tourStepRef.current]?.id,
      completed: tourStepRef.current >= TOUR_STEPS.length - 1,
    })
    setTourStep(null)
    cannedSeq.current += 1   // kills any in-flight typing / dispatch
    setTourOn(false)
    setTourLanded(true)
  }, [])

  // Click a terminal job in the rail -> open its DT2 provenance drawer (over
  // the rail — the center pane's current result stays untouched; "Show in
  // result pane" is the drawer's one quiet action).
  const onSelectJob = useCallback(async (job) => {
    if (!job || (job.status !== 'complete' && job.status !== 'failed')) return
    try {
      const rec = await getJob(job.job_id)
      const env = recordToEnvelope(rec)
      const rows = [
        `job ${job.job_id}`,
        `tool ${rec.tool || job.tool || '—'}`,
        `status ${rec.status}`,
        // On the mock path the engine's new_version.version is a hardcoded 2 —
        // the chain owns the real head, so read it there.
        `version ${env?.result?.new_version
          ? (mockVersions.isSeeded() ? mockVersions.list().head : env.result.new_version.version)
          : (env?.version ?? '—')}`,
        `timing ${rec.elapsed_ms != null ? `${rec.elapsed_ms} ms` : (env?.timing_ms != null ? `${env.timing_ms} ms` : '—')}`,
        `cost ${env?.cost && env.cost.usd_est != null ? `$${Number(env.cost.usd_est).toFixed(4)}` : '—'}`,
        `degraded ${(env?.degraded_mode || rec.degraded_mode) ? 'yes — local fallback' : 'no'}`,
      ]
      // A mock envelope can carry a bare string error — don't render a blank row.
      if (env?.error) rows.push(typeof env.error === 'string'
        ? `error ${env.error}`
        : `error ${env.error.error_code || ''} · ${env.error.message || ''}`)
      rows.push(...cadTimingRows(env))
      setDrawer({
        title: `${rec.tool || job.tool || 'job'} · provenance`,
        rows,
        action: {
          label: 'Show in result pane',
          onClick: () => {
            setSelectedTool({ name: rec.tool || job.tool })
            adoptEnvelope(env, { jobId: job.job_id, toolName: rec.tool || job.tool })
            setOverlayStale(false)
            setDrawer(null)
            setTimeout(() => resultBlockRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
          },
        },
        foot: 'Esc closes — the rail behind never re-flows.',
      })
    } catch (e) {
      setRunErr(humanizeError(e))
    }
  }, [adoptEnvelope, setRunErr])

  // "Details" in the result receipt area -> the run's DT2 provenance drawer.
  const openRunDetails = useCallback(() => {
    if (!result) return
    const env = result
    const rows = [
      `job ${currentJobId || '—'}`,
      `tool ${env.tool || selectedTool?.name || '—'}`,
      // Mock path: the engine hardcodes new_version.version to 2; the chain
      // (seeded only in mock) owns the real head.
      `version ${env.result?.new_version
        ? (mockVersions.isSeeded() ? mockVersions.list().head : env.result.new_version.version)
        : (env.version ?? '—')}`,
      `timing ${env.timing_ms != null ? `${env.timing_ms} ms` : '—'}`,
      `cost ${env.cost && env.cost.usd_est != null ? `$${Number(env.cost.usd_est).toFixed(4)}` : '—'}`,
      `degraded ${env.degraded_mode ? 'yes — local fallback' : 'no'}`,
    ]
    if (env.error) rows.push(typeof env.error === 'string'
      ? `error ${env.error}`
      : `error ${env.error.error_code || ''} · ${env.error.message || ''}`)
    rows.push(...cadTimingRows(env))
    setDrawer({
      title: 'Run · provenance',
      rows,
      action: currentJobId
        ? { label: 'Copy job id', onClick: () => navigator.clipboard?.writeText(String(currentJobId)) }
        : null,
      foot: 'Esc closes — provenance is read-only.',
    })
  }, [result, currentJobId, selectedTool])

  // Header "Details" -> session identity/spend drawer (metadata demoted from
  // the permanent header chrome per the standard).
  const openSessionDetails = useCallback(() => {
    const rows = [
      `org ${org || '—'}`,
      `tenant ${tenantLabel} · tier ${tierDisplay}`,
      `mode ${mock ? 'mock (no cloud)' : `live · ${config.apiBase}`}`,
      `entitlement tier ${gateTier}`,
    ]
    if (!mock && usage) {
      rows.push(`spend $${Number(usage.today?.usd_est || 0).toFixed(3)} today · ${usage.today?.runs || 0} run${(usage.today?.runs || 0) === 1 ? '' : 's'}`)
      if (usage.cap?.enabled && typeof usage.cap?.remaining === 'number') {
        rows.push(`cap $${Number(usage.cap.remaining).toFixed(2)} left`)
      }
    }
    rows.push(`build ${__BUILD_HASH__}`)
    setDrawer({
      title: 'Session · provenance',
      rows,
      // W2b: the controller owns sign-out, exactly as /try does. Its signOut
      // still calls auth.js logout(), and additionally tells the state machine
      // the refusal reason is `signed_out` — the ONE reason the bounded token
      // recovery refuses to re-open. Ending a session through raw logout() left
      // a deliberate sign-out indistinguishable from an expiry.
      action: isSignedIn()
        ? { label: 'Sign out', onClick: sessionActions.signOut }
        : { label: 'Refresh', onClick: () => { loadUsage(); loadHealth() } },
      foot: 'Your account and usage.',
    })
  }, [org, tenantLabel, tierDisplay, mock, usage, gateTier, loadUsage, loadHealth, sessionActions])

  // Esc while a live run is in flight: detach this session from the job (the
  // rail keeps tracking it; the close beacon flags it reap-able server-side).
  /* Legacy interrupt is disabled; useJobController owns it.
  const interruptRun = useCallback(() => {
    runSeqRef.current += 1
    if (!mock && currentJobId) closeJobBeacon(currentJobId)
    clearInflight(); setInflightPtr(null); setReattaching(false)
    setRunning(false); setRunStatus(null); setRunProgress(null); setRunElapsedMs(null)
    runningSinceRef.current = null
    refreshJobs()
  }, [mock, currentJobId, refreshJobs])
  */

  // R ladder (item D): every displayed R keycap must be live, and only the
  // HIGHEST-PRIORITY visible error responds — one keypress, one retry, never
  // two. ResultPanel owns R for run/result errors via its own window listener
  // (rung 1 — the global ladder stands down for it); the rungs below cover the
  // routing strip, the history takeover, the tools-catalog row, the families
  // row, and the post-write refresh row. Rows render their R keycap only when
  // they are the active rung (rTarget), so a shown cap is never inert.
  const anyFamilyOpen = useMemo(
    () => catalog.families.some((f) => openFamilies[f.family_id]),
    [catalog, openFamilies],
  )
  // Mirrors ResultPanel's own canRetryKey condition (its listener is authoritative).
  const resultOwnsR = !running && (!!runErr ||
    !!(result && result.error && !result.entitlement_required && result.error.retryable))
  const rTarget = useMemo(() => {
    if (running) return null
    if (resultOwnsR) return 'result' // ResultPanel's listener handles it
    if (routeErr) return 'route'
    if (historyOpen && historyErr && !historyLoading) return 'history'
    if (toolsErr && (catalogErr ? toolsOpen : anyFamilyOpen)) return 'tools'
    if (catalogErr && !(!mock && authRequired)) return 'catalog'
    if (refreshFail) return 'refresh'
    return null
  }, [running, resultOwnsR, routeErr, historyOpen, historyErr, historyLoading,
      toolsErr, catalogErr, toolsOpen, anyFamilyOpen, mock, authRequired, refreshFail])

  // Global key ladder, TABLE-DRIVEN from the action registry (slice 10a):
  // ⌘K summons the bar; Esc closes the topmost surface (drawer > history >
  // route/failed strip > running run > selection > open project); R retries the
  // highest-priority visible error (outside text inputs); any OTHER bare
  // printable keystroke falls into the prompt bar (type-to-fall-through).
  //
  // The ORDER and the SKIP RULES are the registry's `ladderDecision`, a pure
  // function actionRegistry.test.js walks against the old if/else as literals,
  // and the listener is the registry's `ladderListener`. What stays here is
  // what only this shell can supply: the shell state and the handlers.
  useEffect(() => {
    // The plain shell state the decision reads, built ONCE per subscription
    // (this effect re-runs when any of it changes), never per keystroke: a key
    // that is not the ladder's allocates nothing here, as the pre-slice
    // if/else chain allocated nothing.
    const shell = {
      drawer,
      historyOpen,
      route,
      routeErr,
      runErr,
      running,
      selectedHandle,
      openProjectId,
      rTarget,
    }
    // The handlers the record names, built only once a decision came back.
    const ladderHandlers = (state) => ({
      ...state,
      focusBar: () => barInputRef.current?.focus(),
      onCloseDrawer: () => setDrawer(null),
      onCloseHistory: () => closeHistory(),
      onDismissRoute: () => dismissRoute(),
      onClearErrors: () => { clearRouteError(); clearRunErr() },
      onInterruptRun: () => {
        // P2 wave C-2: latency tolerance in the wild. Esc-on-running is the
        // ONE interrupt gesture (the rail keeps the job; nothing cancels
        // server-side). Refs, not deps: elapsed ticks every 1s and must
        // not re-subscribe this listener.
        track('run.interrupted', {
          ...(interruptSnapshotRef.current.tool ? { tool: interruptSnapshotRef.current.tool } : {}),
          ...(interruptSnapshotRef.current.elapsedMs != null
            ? { elapsed_ms: interruptSnapshotRef.current.elapsedMs } : {}),
        })
        interruptRun()
      },
      onClearSelection: () => setSelectedHandle(null),
      onCloseProject: () => onCloseProject(),
      onRetryRoute: () => onDispatch(),
      onRetryHistory: () => loadHistory(),
      onRetryTools: () => retryTools(),
      onRetryCatalog: () => loadCatalog(),
      onRetryRefresh: () => onRetryViewerRefresh(),
    })
    // Hotkey-driven changes land frame-of-keypress (data-instant, W0#7). The
    // listener stamps only a branch that will handle the key: type-to-fall-
    // through must keep normal motion, and so must an inactive retry rung.
    const onKey = ladderListener(shell, ladderHandlers, markInstant)
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawer, historyOpen, route, routeErr, runErr, running, selectedHandle,
      interruptRun, onDispatch, openProjectId, onCloseProject, rTarget,
      closeHistory, loadHistory, retryTools, loadCatalog, onRetryViewerRefresh, dismissRoute, clearRouteError])

  // Click-to-fall-through (operator rule): a click anywhere on the surface that
  // doesn't otherwise take an action activates the prompt bar. Real
  // interactions (buttons, links, fields, the viewer canvas — a click there
  // selects an entity) and popover/drawer interiors keep their clicks, and an
  // in-flight text selection is never stolen from.
  useEffect(() => {
    const onClick = (e) => {
      const t = e.target
      if (!(t instanceof Element)) return
      if (t.closest('button, a, input, textarea, select, label, summary, canvas, '
        + '[role="button"], [role="option"], [role="listbox"], [contenteditable="true"], '
        + '.drawer, .vh-pop, .proj-menu, .claude-pop, .ops-drawer, .resolver, .bar')) return
      const sel = window.getSelection && window.getSelection()
      if (sel && sel.toString()) return
      barInputRef.current?.focus()
    }
    window.addEventListener('click', onClick)
    return () => window.removeEventListener('click', onClick)
  }, [])

  const toggleLayer = useCallback((layer) => {
    setVisibleLayers((v) => ({ ...v, [layer]: !v[layer] }))
  }, [])

  const applyVersion = useCallback(() => {
    seatDrawingVersion({ intake: editFixtureV2 }, { source: 'fixture' })
  }, [seatDrawingVersion])

  // The last run's overlay only describes the version it produced; once the user
  // undoes/redoes to a different version, suppress it so the viewer never shows a
  // stale "deleted" marker over restored geometry (the result receipt still shows).
  const overlay = (result && !overlayStale) ? (result.overlay || null) : null
  const applied = versionIntake != null

  // Pending-edit ghost (live, §11 nicety): when an open write tool + a picked
  // handle line up, preview the deletion before Run.
  const writeGhost = useMemo(() => {
    if (mock || !selectedHandle) return null
    const caps = openTool?.capabilities || []
    return caps.includes('drawing.write') ? { removed: [selectedHandle] } : null
  }, [mock, selectedHandle, openTool])

  const degraded = !!result?.degraded_mode || demoDegraded

  // A run rejected by the hard SPEND cap (§ broker 402) -> calm amber quota card,
  // not a red failure. The backend's message is authoritative. The coarse DAILY
  // run-count limit (429) shares the quota_exceeded code but carries a distinct
  // `quota_kind` — it renders its OWN card below, so exclude it here.
  const quotaError = (result && !result.ok && result.error &&
    result.error.error_code === 'quota_exceeded' && result.quota_kind !== 'daily_runs')
    ? result.error : null

  // A run rejected by the coarse per-tenant DAILY run limit (HTTP 429) -> calm
  // amber "daily limit reached" card (distinct from the spend cap). Nothing ran.
  const runQuotaError = (result && !result.ok && result.quota_kind === 'daily_runs')
    ? result : null

  // NT2 self-clearing: an ongoing quota condition derives from LIVE usage, not
  // the envelope that first raised it. While one shows, re-poll GET /api/usage
  // every 60s; the banner clears itself only when a poll STRICTLY FRESHER than
  // the condition shows headroom again (cap raised / day rolled). It is never
  // user-dismissed, and a stale pre-error poll can never suppress first paint
  // (suppression requires quotaAt > 0 AND usageAt > quotaAt).
  const [quotaAt, setQuotaAt] = useState(0)
  useEffect(() => {
    setQuotaAt(quotaError || runQuotaError ? Date.now() : 0)
  }, [quotaError, runQuotaError])
  useEffect(() => {
    if (mock || (!quotaError && !runQuotaError)) return undefined
    loadUsage()
    const id = setInterval(loadUsage, 60_000)
    return () => clearInterval(id)
  }, [mock, quotaError, runQuotaError, loadUsage])
  const freshUsage = quotaAt > 0 && usageAt > quotaAt ? usage : null
  const spendCapCleared = !!(quotaError && freshUsage?.cap?.enabled &&
    typeof freshUsage.cap.remaining === 'number' && freshUsage.cap.remaining > 0)
  const dailyRunsCleared = !!(runQuotaError && freshUsage?.today &&
    Number.isFinite(Number(runQuotaError.limit)) &&
    Number(freshUsage.today.runs || 0) < Number(runQuotaError.limit))
  const quotaShown = quotaError && !spendCapCleared ? quotaError : null
  const runQuotaShown = runQuotaError && !dailyRunsCleared ? runQuotaError : null

  // A run rejected by a plan boundary (HTTP 403 entitlement_required) -> calm
  // amber plan notice, also not a red failure. Nothing ran / was billed.
  const entitlementError = (result && result.entitlement_required) ? result : null

  // NR: the active ongoing conditions, docked at the result pane. Two or more
  // collapse to ONE line with a count instead of stacking banners.
  // live, no session -> calm gate, hush the 401 red. A 401 on a SIGNED-IN
  // session of an auth-unconfigured build is different: the token was rejected
  // and there is no way to re-auth, so it is a real failure — fall through to
  // the pane-fail surface (Retry + Back to the demo), never the inert overlay
  // (round-2 review F1: that state was an unrecoverable blank).
  // W2b: the same expression, now a named contract over the shared
  // controller's `required` status (consoleGate.js proves its truth table).
  // Every console surface below (the gate, the hushed 401 red, the viewer
  // overlay, the product-surface enablement) reads THIS, so there is one
  // derivation and one state machine behind it. `isSignedIn` is handed in as a
  // function so the storage read keeps its short circuit.
  const signedOut = consoleSignedOut({ mock, authRequired, authConfigured, isSignedIn })

  // Product-surface navigation (Browser / CAD / Solar CAD / iOS). 'cad' is the
  // module default, so the drawing workspace renders exactly as before unless
  // the user picks another tab (or arrives with ?surface=). The tabs, frame,
  // IosSurface, and EditSurface existed fully built and tested but were never
  // mounted (2026-08-30 finding); this is the mount, not new surface behavior.
  const [activeSurface, setActiveSurface] = useState(() => {
    try { return productSurfaceFromSearch(window.location.search) } catch { return 'cad' }
  })
  const onSelectSurface = useCallback((id) => {
    setActiveSurface(id)
    try {
      const next = searchForProductSurface(window.location.search, id)
      window.history.replaceState(null, '', `${window.location.pathname}${next}${window.location.hash}`)
    } catch { /* URL sync is a convenience; state alone still switches the tab */ }
  }, [])
  // W4c-V1: the nav rail's spine posture on drafting surfaces under the
  // studio. IN-MEMORY on purpose: the rollback contract forbids new storage
  // keys under the studio and stale ?params, so the posture resets per page
  // load (accepted V1 cost). Default COLLAPSED on CAD/Solar — the drafting
  // ribbon carries the tool set there and an expanded catalog beside it is
  // exactly the duplication ACCEPTANCE deferred the ribbon to avoid.
  const [navExpanded, setNavExpanded] = useState(false)
  // W4c-C: the DXF import surface is a floating cockpit pane on drafting
  // surfaces (it was a full-width page block across the drawing); the ribbon
  // opens it. Rail OFF it renders inline exactly as before.
  const [importOpen, setImportOpen] = useState(false)
  // W4d Slice D: the job monitor's posture on drafting surfaces (spine by
  // default; in-memory only, like the nav posture).
  const [jobRailExpanded, setJobRailExpanded] = useState(false)
  // Slice 11a: the builds poll (GET /api/builds, validated records from
  // every lane). Mock mode makes no request; the rail hosts one
  // BuildQueueCard per record and the toolbar badge counts the open ones.
  const buildQueue = useBuildQueue({ mock })
  // W4e: the ribbon shows ONE tab's panels at a time (the reference's
  // Draw / Insert / Annotate / View / Manage); the top band switches it.
  // Slice 2: the opening tab is the contract's declared home tab, not a
  // literal. This is console-GLOBAL state (one ribbon, switched by the top
  // band), so a surface that declares no home tab because its ribbon never
  // mounts (toolbar.home null on browser/ios) falls back to the default
  // surface's home. That fallback is what makes this equal to today's
  // useState('draw') on all four ids, and it is what keeps the ribbon from
  // mounting with no tab selected after a browser -> cad switch.
  const [ribbonTab, setRibbonTab] = useState(() => (
    surfaceContract(activeSurface).toolbar.home
    ?? surfaceContract(DEFAULT_PRODUCT_SURFACE).toolbar.home
  ))
  // W4e round 3: the properties pane closes from its own title row and the
  // View tab's Properties tool reopens it; while closed the canvas takes the
  // column (cockpit.css keys off the pane's absence, never a root attribute).
  const [paneOpen, setPaneOpen] = useState(true)
  // <=980px the shell stacks into one column (styles.css) — a 44px spine
  // there is a full-width sliver, so the posture neutralizes to expanded.
  const [wideViewport, setWideViewport] = useState(() => {
    try { return window.matchMedia('(min-width: 981px)').matches } catch { return true }
  })
  useEffect(() => {
    let mq
    try { mq = window.matchMedia('(min-width: 981px)') } catch { return undefined }
    const sync = () => setWideViewport(mq.matches)
    mq.addEventListener?.('change', sync)
    return () => mq.removeEventListener?.('change', sync)
  }, [])
  // THE SURFACE CONTRACT (standardization slice 2, docs/convergence/
  // SURFACE-CONTRACT.md). Every chrome gate below this line reads a declared
  // slot instead of comparing activeSurface to a string literal, so what the
  // shell renders is the manifest, not a set of inline opinions that can drift
  // apart. `activeSurface` is ALWAYS one of the four declared ids
  // (productSurfaceFromSearch normalizes at :2201, and the tabs only ever emit
  // a declared id), so surfaceContract's fail-closed normalization is a
  // backstop here, never the live path. Identity-stable per id: the record is
  // a frozen literal, so `surfaceSlots` is a safe useMemo dependency.
  // Behaviour is IDENTICAL: src/site/surfaceGates.test.js pins each derived
  // value equal to the literal predicate it replaced, on all four ids.
  const surfaceSlots = surfaceContract(activeSurface)
  // Keeps its name: ~20 sites read `studioGround && drafting`, and the App
  // wiring pin (src/app-wiring.test.mjs) guards that exact shape against the
  // white screen it was written for. Was groundShowsDrawing(activeSurface).
  const drafting = surfaceSlots.chrome.cockpit
  // P1: the status bar is grouped into regions on exactly the surfaces that
  // have instruments to group — the studio's drafting surfaces — and only at
  // the width where the cockpit layout that styles those regions actually
  // runs. `wideViewport` IS `(min-width: 981px)`, the same breakpoint
  // cockpit.css uses, so the wrapper can never exist without its rules: below
  // it the old shell's own `footer.foot-bar > *` rules (styles.css) style the
  // segments as the bar's direct children, and a wrapper there would hide
  // them from that selector. One gate for all three FootRegions, so they can
  // never disagree; off, each is a fragment and the DOM is byte-identical.
  const footRegions = Boolean(studioGround) && drafting && wideViewport
  // The properties dock's declared sections; null off a drafting surface. Its
  // TRUTHINESS is the mount gate (paneOpen is the second gate, below).
  const dockSections = surfaceSlots.rails.dock
  // The job monitor's declared posture. Was `drafting` inline at the JobRail.
  const jobSpine = surfaceSlots.rails.right === 'job-spine'
  // Slice 6a: the version history button + drawer mount where the CONTRACT
  // says versions exist (`drawing` today on cad and solar), never on a surface
  // literal, and the same predicate now gates /try's version tab.
  const versionsMounted = surfaceSlots.versions !== 'none'
  // Typed command words arm the engine only where the cockpit is: the studio's
  // drafting surfaces with the engine built in (ENV_CAD_EDIT first, so a
  // flag-off build folds the whole feature to false). Slice 2 keeps this and
  // the three ENV_CAD_EDIT mounts on the SAME predicate as the cockpit gate
  // (W4f cockpit owner's request), so the armer and its surfaces cannot split.
  drawingCommandOnRef.current = ENV_CAD_EDIT && !!studioGround && !!drafting
  const navSpine = !!studioGround && surfaceSlots.rails.left === 'spine' && !navExpanded && wideViewport
  // The per-application fold (operator directive): under the studio each
  // tab's rail carries the families its application calls for; the old shell
  // keeps the whole catalog byte-for-byte.
  const railFamilies = useMemo(
    () => (studioGround ? familiesForSurface(catalog.families, activeSurface) : catalog.families),
    [studioGround, catalog.families, activeSurface],
  )

  // W4c-V3: the Solar tab's ground material. Under the studio on the solar
  // surface, the Panels layer takes the solar accent and every other layer
  // keeps its palette - rail OFF (and every other surface) returns the SAME
  // colorForLayer reference, so the old shell's canvas bytes are untouched
  // (viewer-interaction screenshots compare layer colors rail-OFF).
  const surfaceColorForLayer = useMemo(() => {
    if (!(studioGround && surfaceSlots.groundMaterial.layerAccent === 'solar')) return colorForLayer
    return (layer) => (layer === 'Panels' ? '#7fd6a6' : colorForLayer(layer))
    // surfaceSlots is a frozen per-id literal, so its identity changes exactly
    // when activeSurface does: same memo invalidation as before.
  }, [studioGround, surfaceSlots, colorForLayer])

  // W4c-V3: the 135 REAL solved string routes over the bundled rooftop
  // sample, on the Solar tab only. Honesty gates, all structural:
  //  - mock only: the live demo drawing is mutable and the bundled solve
  //    was computed against these exact bytes, nothing else;
  //  - the demo sample only, with the edit fixture excluded before its
  //    synthetic geometry can inherit the demo source identity, and the
  //    seated intake itself bound to the bundled sample drawing;
  //  - never over a version preview or a mutated head (StageLayer:107
  //    precedent) - a delete-panel run makes v2 and the routes go stale.
  const [demoSolveRoutes, setDemoSolveRoutes] = useState(null)
  const intakeIsRooftopSample = String(intake?.dwg || '').replace(/\\/g, '/').endsWith('/rooftop_demo.dwg')
  const solarStringsEligible = !!studioGround && surfaceSlots.groundMaterial.solarStrings && mock
    && !isEditFixture && DRAWING_SOURCE === 'rooftop_demo' && intakeIsRooftopSample
  useEffect(() => {
    if (!solarStringsEligible || demoSolveRoutes) return undefined
    let live = true
    loadDemoSolve().then((solve) => {
      if (!live || !Array.isArray(solve?.strings)) return
      setDemoSolveRoutes(solve.strings
        .filter((route) => Array.isArray(route.pts) && route.pts.length >= 2)
        .map((route) => ({ id: route.id, pts: route.pts })))
    }).catch(() => { /* no solve, no overlay - never a fabricated route */ })
    return () => { live = false }
  }, [solarStringsEligible, demoSolveRoutes])
  const solarStringRoutes = useMemo(() => {
    if (!solarStringsEligible || previewing || (drawingState?.head ?? 1) > 1) return undefined
    return demoSolveRoutes || undefined
  }, [solarStringsEligible, previewing, drawingState, demoSolveRoutes])
  // iOS ship-lane readiness contract (leaf.ios-ship-surface.v1). Fetched only
  // with the surface flag baked on and a concrete project + revision; every
  // other case stays null, which IosSurface renders truthfully as
  // "Not yet configured" and the tab label shows as "Setup required".
  const [iosContract, setIosContract] = useState(null)
  useEffect(() => {
    if (!ENV_IOS_SURFACE || mock || !openProjectId || !canonicalVersionId) {
      setIosContract(null)
      return undefined
    }
    let live = true
    fetchIosSurfaceStatus({ projectId: openProjectId, revision: canonicalVersionId })
      .then((contract) => { if (live) setIosContract(contract) })
      .catch(() => { if (live) setIosContract(null) })
    return () => { live = false }
  }, [mock, openProjectId, canonicalVersionId])
  const surfaceStates = useMemo(() => productSurfaceStates({
    sessionActive: mock || !signedOut,
    hasDrawing: !!shown,
    apsLive: health ? !!health.aps_live : undefined,
    iosReady: !!(iosContract?.readiness?.healthy && iosContract?.readiness?.launchable),
  }), [mock, signedOut, shown, health, iosContract])

  const advisories = [
    quotaShown && 'spend cap',
    runQuotaShown && 'daily limit',
    entitlementError && 'plan',
    degraded && 'local fallback',
  ].filter(Boolean)

  // Publish the MEASURED chrome heights that structural.css's drawer offsets
  // read. The header wraps on a phone (~95px, not the hardcoded 49px fallback),
  // so without this the DT2 drawer paints over the header; on desktop it fixes a
  // latent 7px gap under a 42px header. ResizeObserver keeps it live on rotate.
  useLayoutEffect(() => {
    const h = document.querySelector('header.top')
    const f = document.querySelector('footer.foot-bar')
    if (!h || !f) return undefined
    const sync = () => {
      const r = document.documentElement.style
      r.setProperty('--drawer-top', `${Math.round(h.getBoundingClientRect().height)}px`)
      r.setProperty('--drawer-bottom', `${Math.round(f.getBoundingClientRect().height)}px`)
    }
    sync()
    if (typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(sync)
    ro.observe(h); ro.observe(f)
    return () => ro.disconnect()
  }, [])

  // M1 exit fades for the parent-mounted panels: hold the mount through the
  // 180 ms .exit fade (useExit follows Toast.jsx's pattern).
  const historyExit = useExit(historyOpen)
  // The internal ops / tenant kill-switch drawer must never be reachable from a
  // public demo build — `?ops=1` is a no-op in mock.
  const opsExit = useExit(opsFlag && !mock && !opsDismissed)
  const customizeExit = useExit(customizeOpen && canOpenCustomize)

  // W4d Slice A: the ribbon's clusters as DATA (View, Version, Layers, the
  // active surface's catalog fold, Author) — each a REAL command this
  // component already owns, each disabled control carrying its reason. The
  // engine's own clusters (Drawing, Modify) render as the ribbon's children
  // so they can read the ONE engine session through context. Studio-only:
  // rail OFF the ribbon never mounts and this list is never read.
  const ribbon = useMemo(() => {
    if (!(studioGround && drafting)) return { clusters: [], quickBefore: [], quickAfter: [] }
    const view = viewCluster({ viewerRef, hasDrawing: !!shown, paneOpen, onTogglePane: () => setPaneOpen((o) => !o) })
    const version = versionCluster({
      hasVersions: !!drawingState,
      canUndo,
      canRedo,
      versionBusy: !!versionBusy,
      running: !!running,
      previewing: !!previewing,
      mutationsBlocked: !!drawingMutationsBlocked,
      historyOpen,
      onUndo,
      onRedo,
      onToggleHistory: onToggleHistoryTracked,
    })
    const layers = layersCluster({
      layers: shown?.layers, counts: layerCounts, visibleLayers, onToggle: toggleLayer, colorFor: colorForLayer,
    })
    // Seating (Slice D): the tool rail hides behind the band on drafting
    // surfaces, so the band carries the rail's two affordances: `expand`
    // (only while the rail is hidden) and every family label opening
    // that family in the rail.
    const rail = navSpine ? [railCluster({ onExpand: () => setNavExpanded(true) })] : []
    const families = catalogClusters(railFamilies, {
      onRequestRun: onRequestCatalogRun,
      onOpenFamily: (fam) => {
        setNavExpanded(true)
        setFamilyOpen(fam.family_id, true)
      },
      running: !!running,
      previewing: !!previewing,
      writeLocked,
      writeEntitled: canRunWrite,
      engineDirty,
    })
    const author = authorCluster({
      // The plan's own answer, then the folded entitlement-AND-availability
      // rule (R5 stage off): two reasons, two fixes.
      entitled: entOf('build'),
      available: canBuild,
      onOpen: () => {
        setNavExpanded(true)
        setAuthorOpen(true)
        authorSectionRef.current?.scrollIntoView?.({ block: 'nearest' })
      },
      authored: lastAuthoredTool,
      onUseAuthored,
      running: !!running,
      previewing: !!previewing,
      writeLocked,
      writeEntitled: canRunWrite,
    })
    // Per-tool placement: a tool whose record names a ribbon tab is grouped
    // into a cluster for THAT tab instead of the Manage families panel. A
    // catalog where nothing declares a placement returns {} and every tab below
    // is byte-identical to before.
    const tabFamilies = catalogTabClusters(railFamilies, {
      onRequestRun: onRequestCatalogRun,
      onOpenFamily: (fam) => {
        setNavExpanded(true)
        setFamilyOpen(fam.family_id, true)
      },
      running: !!running,
      previewing: !!previewing,
      writeLocked,
      writeEntitled: canRunWrite,
      engineDirty,
    })
    const [annotation, block, properties, groups, clipboard] = referencePanels()
    // The reference's Draw tab: Draw, Modify (engine children, rendered
    // first), Annotation, Layers, Block, Properties, Groups, Clipboard.
    const byTab = {
      draw: [annotation, layers, block, properties, groups, clipboard, ...(tabFamilies.draw || [])],
      insert: [block, ...(tabFamilies.insert || [])],
      annotate: [annotation, ...(tabFamilies.annotate || [])],
      view: [view, version, layers, ...(tabFamilies.view || [])],
      manage: [...rail, ...families, ...(tabFamilies.manage || []), author],
    }
    const [undo, redo] = version.tools
    return {
      clusters: byTab[ribbonTab] || byTab.draw,
      quickBefore: [
        { id: 'new', label: 'New drawing', icon: 'new-file', disabled: true, reason: 'new drawings start on the project board (Start tab)' },
      ],
      quickAfter: [
        { id: 'print', label: 'Print', icon: 'print', disabled: true, reason: 'printing is not in the browser engine' },
        { id: 'sep-1', kind: 'sep' },
        { ...undo, id: 'quick-undo', label: 'Undo' },
        { ...redo, id: 'quick-redo', label: 'Redo' },
        // The tool rail's expand affordance rides the band on every tab
        // (the Manage tab carries it as a panel too), so the catalog is
        // always one click away while the rail hides behind the cockpit.
        ...(navSpine ? [
          { id: 'sep-2', kind: 'sep' },
          { id: 'quick-rail', dataTool: 'rail-expand', label: 'Tool rail', icon: 'sidebar', title: 'Expand the tool rail', onClick: () => setNavExpanded(true) },
        ] : []),
      ],
    }
  }, [studioGround, drafting, shown, drawingState, canUndo, canRedo, versionBusy, running, previewing,
    drawingMutationsBlocked, historyOpen, onUndo, onRedo, onToggleHistoryTracked, layerCounts, visibleLayers,
    toggleLayer, railFamilies, onRequestCatalogRun, writeLocked, canRunWrite, canBuild, entOf, ribbonTab, colorForLayer, paneOpen,
    lastAuthoredTool, onUseAuthored, setFamilyOpen, engineDirty])
  const ribbonClusters = ribbon.clusters
  // W4e round 2: the pane's Drawing section, the document's own facts from
  // the intake (counts) and one pass over its vertices (extents). Studio
  // drafting surfaces only; null everywhere else so the dock renders as before.
  // Named apart from the version bootstrap's `drawingSummary`: a shadowing
  // name makes the build transform rename one of them and the App wiring
  // pins (src/app-wiring.test.mjs) then miss the bootstrap's pattern.
  const paneDrawingFacts = useMemo(() => {
    if (!(studioGround && drafting && shown)) return null
    const layers = Array.isArray(shown.layers) ? shown.layers : []
    const polylines = Array.isArray(shown.polylines) ? shown.polylines.length : 0
    const inserts = Array.isArray(shown.inserts) ? shown.inserts.length : 0
    const faces = Array.isArray(shown.faces3d) ? shown.faces3d.length : 0
    return {
      name: `${projectName}.dwg`,
      entities: polylines + inserts + faces,
      polylines,
      inserts,
      faces,
      layers: layers.length,
      layersShown: layers.filter((l) => visibleLayers[l] !== false).length,
      extents: drawingExtents(shown.polylines),
      source: mock ? 'sample data' : 'project drawing',
    }
  }, [studioGround, drafting, shown, projectName, visibleLayers, mock])

  // W4d Slice A: the ONE engine-session mount wraps the drawing workspace,
  // so the ribbon's engine clusters and the import pane consume the same
  // session (a second useEngineSession call is a second worker, forbidden by
  // the store's contract). ENV_CAD_EDIT is deliberately the FIRST operand so
  // a flag-off build folds the provider, the store and the worker chunk away
  // exactly as the surface's own call site does (bundleFence.test.js).
  //
  // F-3 persistence leg: live (non-mock) sessions can save edited bytes as a
  // NEW VERSION of the open drawing. The parent is fetched FRESH at save
  // time; the server's compare-and-set still guards the race. Mock/demo
  // stays download-only, honestly.
  const engineSaveTarget = !mock && intake ? {
    drawingId: REQUESTED_DRAWING_ID,
    headVersion: null,
    // W4g-3b (one head): a save that carries a `plan` (the store's diff of
    // the head document, present only when the engine holds the head) goes
    // to the plan route, where the SERVER picks the commit leg and says so
    // in the receipt; a hand-imported document keeps the F-3 sidecar route.
    save: async (bytes, _parent, digest, plan = null) => {
      const chain = await getDrawingVersions(false, REQUESTED_DRAWING_ID)
      // The store publishes ONLY under a live single-writer checkout (the
      // postgres authority fails closed without one — staging's exact
      // first-save refusal). Use the session's held capability when there
      // is one; otherwise take a checkout for exactly this save and release
      // it after, the same acquire→save→release discipline the acceptance
      // prover runs. A refused acquire surfaces the real holder instead of
      // the store's opaque 400.
      const held = checkout.actions.getCapability()
      let acquired = null
      if (!held) {
        acquired = await takeCheckout(REQUESTED_DRAWING_ID, 'cad-edit-save')
        if (!acquired.acquired) {
          const e = new Error('drawing is checked out by '
            + (acquired.locked_by || 'another session')
            + ' — try again when the lock clears')
          e.status = 409
          throw e
        }
      }
      const cap = held || acquired.checkout_capability
      try {
        if (plan && plan.mutations) {
          return await saveDrawingVersionPlan(
            REQUESTED_DRAWING_ID, bytes, chain.head, digest, plan.mutations, cap)
        }
        return await saveEditedDrawingVersion(
          REQUESTED_DRAWING_ID, bytes, chain.head, digest, cap)
      } finally {
        if (acquired) {
          releaseCheckout(REQUESTED_DRAWING_ID, cap).catch(() => {})
        }
      }
    },
  } : null
  const onEngineSaved = (receipt) => {
    if (receipt?.new_version) {
      completedVersionRef.current?.(receipt.new_version, { result: {} })
    }
  }
  const engineScope = (node) => (ENV_CAD_EDIT ? (
    <EngineSessionProvider saveTarget={engineSaveTarget} onSaved={onEngineSaved} onDirtyChange={onEngineDirtyChange}>{node}</EngineSessionProvider>
  ) : node)

  return (
    // Slice 4a: THE SURFACE FRAME (site/SurfaceFrame.jsx). One wrapper both
    // scenes mount, carrying the normalized shell contract; its slots below
    // render each shared element exactly where it already stood, so this adds
    // no DOM. Local names are aliased HERE, at the call boundary, so the frame
    // never learns App's private vocabulary.
    <SurfaceFrame
      scene="console"
      activeSurface={activeSurface}
      states={surfaceStates}
      catalog={catalog}
      catalogError={catalogErr}
      workspaceProject={workspaceProjectState}
      onSelect={onSelectSurface}
      onCreateProject={onCreateProject}
      projectSlot={surfaceSlots.chrome.projectSlot === 'ios-surface'
        ? <IosSurface enabled={ENV_IOS_SURFACE} contract={iosContract} />
        : null}
      session={session}
      posture={{
        studio: !!studioGround,
        navExpanded,
        onNavExpand: () => setNavExpanded(true),
        wideViewport,
        jobRailExpanded,
        onJobRailExpand: () => setJobRailExpanded(true),
        onJobRailCollapse: () => setJobRailExpanded(false),
      }}
      entitlement={{
        tier: entTier,
        entitlements,
        loading: entLoading,
        mock,
        // The ONE declaration of where the gate renders. Both mounts used to
        // spell `studioGround && drafting && wideViewport` themselves: the
        // dock hosts it on a wide drafting surface, main hosts it everywhere
        // else. Unchanged, stated once.
        placement: studioGround && drafting && wideViewport ? 'docked' : 'inline',
      }}
      commandBar={() => (
        <PromptBox
          value={prompt}
          onChange={onPromptChange}
          onDispatch={onDispatch}
          routing={routing}
          hintLane={hintLane}
          projectName={currentProjectName || projectName}
          inputRef={barInputRef}
          routeActive={!!route}
          onOpenAuthor={onOpenAuthor}
          // Slice 8a round 3: the bar has no guard of its own. This is the
          // refusal the TRANSPORT raised (api.nlPrompt / converse.postMessage)
          // and the controller caught, so what is on screen is the decision
          // that was actually enforced.
          secretRefusal={secretRefusal}
          // The registry supersedes the tools-only list when it loaded (its
          // entries carry `kind`, which is what groups the picker); a failed
          // fetch falls back to exactly today's list.
          tools={registryEntries.length ? registryEntries : slashTools}
          skills={catalogSkills}
          commandActions={slashCommandActions}
          sessionId={agentSessionId}
          imageAttachmentsEnabled={false}
          // W4d Slice E seating: on drafting surfaces under the studio the
          // well is the reference's one-line docked "Command:" prompt.
          // Rail OFF (and every non-drafting surface) the prop is false and
          // the well renders exactly as before.
          commandLine={!!studioGround && surfaceSlots.commandLine}
        />
      )}
      jobRail={{
        mock,
        jobs,
        currentJob,
        inflight: inflightPtr,
        reattaching,
        onSelectJob,
        builds: buildQueue.builds,
      }}
      toast={{ toast, onDone: onToastDone }}
    >
    <div className="app" data-surface={studioGround ? activeSurface : undefined} data-tour="shell">
      <header className="top">
        <div className="mark"><span className="diamond" aria-hidden="true" /> Leaf — build CAD tools with AI</div>
        {/* W4e: on the studio's drafting surfaces the header IS the
            reference's top band: quick access, then the ribbon tabs. The
            engine's Open/Save portal into the band's slot. Rail OFF and
            every other surface: nothing here. */}
        {studioGround && drafting && (
          <CockpitTopBand tab={ribbonTab} onTab={setRibbonTab} before={ribbon.quickBefore} after={ribbon.quickAfter} />
        )}
        <div className="proj">
          <ProjectSwitcher
            mock={mock}
            projectName={projectName}
            orgId={orgId}
            projects={projects}
            openProjectId={openProjectId}
            workspaceProject={workspaceProjectState}
            unavailable={projectsErr}
            loading={projectsLoading}
            orgBusy={orgBusy}
            projectBusy={projectBusy}
            onCreateOrg={onCreateOrg}
            onCreateProject={onCreateProject}
            onOpenProject={onOpenProject}
          />
          <span className="meta">
            {shown ? `${shown.polylines.length} polylines · ${shown.layers.length} layers` : 'loading'}
          </span>
          {mock && <span className="tag amber">Demo</span>}
        </div>
        <div className="spacer" />
        <div className="who">
          {/* Header metadata (org · tenant · tier · spend · API base) is demoted
              behind Details -> the DT2 session drawer, per the standard. */}
          {canOpenCustomize && (
            <button type="button" className="chip-act" onClick={() => setCustomizeOpen(true)}>Customize</button>
          )}
          {!mock && agentSessionId && (
            <button
              type="button"
              className="chip-act"
              onClick={openAgentMode}
              aria-label={`Pending approvals ${pendingApprovalCount}`}
              title={pendingApprovalsUnavailable
                ? 'Pending approvals could not be refreshed. Open the inbox to retry.'
                : 'Open pending approvals'}
            >
              Approvals
              {pendingApprovalCount > 0 && <span className="key">{pendingApprovalCount}</span>}
              {pendingApprovalsUnavailable && <span className="dot red" aria-hidden="true" />}
            </button>
          )}
          <button type="button" className="chip-act" onClick={openSessionDetails}>Details</button>
          {/* Persistent identity control: before this, sign-out lived only
              behind Details -> the session drawer, so the header carried no
              reachable exit for a signed-in operator (2026-09-02
              reconciliation, row B11). Same signOut path the drawer's action
              already calls -- never raw logout(). */}
          <AccountSignOut signedIn={isSignedIn()} onSignOut={sessionActions.signOut} />
          {devControls && (
            <label className="switch">
              <input
                type="checkbox"
                checked={mock}
                disabled={tourOn}
                onChange={(e) => setMock(e.target.checked)}
                aria-label="Use mock data (off = live backend)"
              />
              <span>Mock</span>
            </label>
          )}
          {/* Live-only chrome (Claude-account terminal panel) is hidden in the
              demo — it can't work signed-out. Guarded on !mock. */}
          {!mock && (
            <ClaudeAccountPanel
              mock={mock}
              grant={grant}
              loading={grantLoading}
              busy={grantBusy}
              error={grantErr}
              open={claudeOpen}
              onToggle={setClaudeOpen}
              onLink={onLinkClaude}
              onUnlink={onUnlinkClaude}
            />
          )}
        </div>
      </header>

      {/* Slice 4a: the rail's DOM moved to site/NavRail.jsx byte-identically.
          Its three folds stay CONTROLLED from here, because App drives all
          three from outside the rail: seven build-lane paths call
          setAuthorOpen(true), and rTarget / anyFamilyOpen read toolsOpen and
          openFamilies to pick the R-key retry rung. A rail that owned them
          would have silently cut those wires. */}
      <NavRail
        activeSurface={activeSurface}
        studio={studioGround}
        navSpine={navSpine}
        railFamilies={railFamilies}
        catalogFamilyCount={catalog.families.length}
        capCount={capCount}
        catalogSource={catalog.source}
        catalogErr={catalogErr}
        signedOut={signedOut}
        onRetryCatalog={loadCatalog}
        retryTarget={rTarget}
        tools={tools}
        toolsErr={toolsErr}
        onRetryTools={retryTools}
        writeLocked={writeLocked}
        writeEntitled={canRunWrite}
        running={running || !!previewing}
        selectedTool={selectedTool}
        onRequestRun={onRequestCatalogRun}
        onOpenTool={setOpenTool}
        onReviseTool={onReviseAuthoredTool}
        toolsOpen={toolsOpen}
        onToggleTools={() => setToolsOpen((o) => !o)}
        openFamilies={openFamilies}
        onToggleFamily={toggleFamily}
        authorOpen={authorOpen}
        onToggleAuthor={() => setAuthorOpen((o) => !o)}
        onCollapse={() => setNavExpanded(false)}
        authorSectionRef={authorSectionRef}
        onAuthor={onAuthor}
        onPublish={onPublishAuthor}
        onUseAuthored={onUseAuthored}
        authorSeed={authorSeed}
        authorSignal={authorSignal}
        authorAutoSubmit={tourOn}
        authorTargetTool={authorTargetTool}
        onCancelAuthorRevision={onCancelAuthorRevision}
        authorStage={authorStage}
        onResumeAuthor={authorStage.resume}
        claudeNotLinked={claudeNotLinked}
        onLinkClaude={() => setClaudeOpen(true)}
        buildEntitled={canBuild}
      />

      <div className="center-col">
        <main className="center-scroll">
        {/* the tour carries its own persistent banner — don't stack two */}
        {mock && !tourOn && <DemoBanner />}
        {/* There is a way back IN: leaving the tour (Skip / Exit) used to be
            one-way, with a hard reload the only re-entry — forbidden on stage. */}
        {mock && !tourOn && tourAvailable.current && !(studioGround && drafting) && (
          <button
            type="button"
            className="chip-neutral"
            onClick={() => {
              tourStartedRef.current = true
              tourStepRef.current = 0
              setTourStep(TOUR_STEPS[0]?.id)
              track('tour.started', { entry: 'button' })
              track('tour.step_reached', { step_id: TOUR_STEPS[0]?.id })
              setTourLanded(true); setTourOn(true)
            }}
          >
            Restart guided tour
          </button>
        )}
        {signedOut && authConfigured && (
          <SignedOutGate
            onDemo={() => {
              // P2 (pre-auth allowlisted): the stranger split at the front door.
              track('gate.choice', { choice: 'demo' })
              setMock(true)
            }}
            onSignIn={() => {
              // The redirect to Auth0 follows; the pagehide beacon carries this.
              track('gate.choice', { choice: 'sign_in' })
              onLogin()
            }}
          />
        )}
        <div className="kicker">Home · one prompt, two lanes</div>
        <h1 className="home-q">What should Leaf do to <em>{projectName}</em>?</h1>
        <div className="hint">
          Try <b>count panels per layer</b> — one prompt, routed across <b>Run</b> ·{' '}
          <b>Build</b>. You confirm before anything runs — paid actions never auto-execute.
        </div>

        {!mock && openProjectId && (
          <WorkspaceSummary
            workspace={workspace}
            loading={wsLoading}
            selectedVersionId={canonicalVersionId}
            onSelectVersion={selectCanonicalVersion}
            onClose={onCloseProject}
          />
        )}

        <SurfaceFrame.Tabs />
        {/* W4a surface grounds (site/SurfaceGrounds.jsx): under the studio
            shell the ground IS each tab's workspace — the project board for
            Browser, the device stage for iOS; CAD and Solar CAD keep the
            drawing (portaled above). ONLY through the ground portal: the
            old shell has no ground, so rail OFF renders none of this. */}
        {studioGround && createPortal(
          <SurfaceGrounds
            surface={activeSurface}
            workspaceProject={workspaceProjectState}
            workspace={!mock && openProjectId ? workspace : null}
            drawing={shown ? { name: projectName, polylines: shown.polylines.length, layers: shown.layers.length } : null}
            catalog={catalog}
            mock={mock}
            iosEnabled={ENV_IOS_SURFACE}
            iosContract={iosContract}
            revision={canonicalVersionId}
          />,
          studioGround,
        )}
        {/* Slice 2 read contract.chrome.productFrame here; slice 4a moved that
            read into the frame, which owns it for BOTH scenes now. Solar still
            renders it over the shown workspace card, today's quirk, preserved
            deliberately, not fixed. */}
        <SurfaceFrame.Frame />
        {/* The CAD workspace hides (not unmounts) on other tabs so live
            drawing, lock, and job state survive tab switches untouched.
            Solar shows it too: that tab IS the CAD workspace on the solar
            tool set, opened inline by the tab itself (no "Open ..." button;
            operator directive 2026-09-01). */}
        {engineScope(
        <div
          className="workspace-card enter"
          style={{ '--rank': 1, display: surfaceSlots.chrome.workspaceCard ? undefined : 'none' }}
          ref={workspaceCardRef}
          id={studioGround && drafting ? 'cockpit-import-pane' : undefined}
          data-import-open={studioGround && drafting && importOpen ? 'true' : undefined}
        >
          {unreadableHead && unreadableHead.pending && (
            // The routine post-restore load: calm progress, not a failure —
            // no alert role, no retry (the load is already in flight).
            <div
              className="strip-running"
              role="status"
              data-testid="unreadable-head-lock"
              data-head={unreadableHead.head}
              data-latest={unreadableHead.latest}
              data-pending="true"
            >
              <span className="dot square" aria-hidden="true" />
              <span className="strip-sentence">{unreadableHead.message}</span>
            </div>
          )}
          {unreadableHead && !unreadableHead.pending && (
            <div
              className="strip-failed"
              role="alert"
              data-testid="unreadable-head-lock"
              data-head={unreadableHead.head}
              data-latest={unreadableHead.latest}
            >
              <span className="dot red" aria-hidden="true" />
              <span className="strip-sentence">{unreadableHead.message}</span>
              <button type="button" className="chip-act" onClick={retryUnreadableHead} disabled={drawing.refreshing}>
                Retry loading
              </button>
            </div>
          )}
          {/* W4c-V1: the drafting ribbon — the drawing window's command
              strip, in the cockpit grammar. Studio-only (rail OFF renders
              nothing); tools are the ACTIVE SURFACE's fold, wired through
              the same run-decision path as the rail (source 'ribbon'). */}
          {studioGround && drafting && (
            <DraftingRibbon clusters={ribbonClusters} tab={ribbonTab}>
              {/* The engine's own panels (File, Draw, Modify) read the ONE
                  session through context; ENV_CAD_EDIT first so a flag-off
                  build folds them away with the provider. Always mounted so
                  the quick-access Open/Save and the operand line exist on
                  every tab; the tab picks which panels the band shows. */}
              {ENV_CAD_EDIT && (
                <EngineRibbonClusters
                  importOpen={importOpen}
                  onToggleImport={() => setImportOpen((o) => !o)}
                  panels={ribbonTab === 'insert' ? ['file'] : ribbonTab === 'draw' ? ['draw', 'modify'] : []}
                />
              )}
              {/* W4f slice B: the command line's typed words (LINE, C, MOVE ...)
                  reach the engine through this consumer; renders nothing. */}
              {ENV_CAD_EDIT && <CommandLineArmer />}
              {/* W4f slice A0: while a DXF is open in the engine, the canvas
                  shows the ENGINE document through the viewer's own
                  applyVersion seam (the console drawing returns on close);
                  the card carries data-engine-document for the pins. */}
              {ENV_CAD_EDIT && (
                <EngineDocumentView
                  viewerRef={viewerRef}
                  onShown={(intake) => {
                    const el = workspaceCardRef.current
                    if (!el) return
                    if (intake) el.dataset.engineDocument = intake.documentId
                    else delete el.dataset.engineDocument
                  }}
                />
              )}
              {/* W4g-1b: the console's OWN drawing opens in the engine at
                  mount (GET .../dxf), so Draw/Modify are live without an
                  import; a moved head (a tool run, undo/redo, restore)
                  re-opens a clean engine copy. The studio's drafting surfaces
                  only; the rail-OFF shell stays byte-identical (no fetch, no
                  engine view, no card stamp outside the cockpit). W4g-1c:
                  the public demo (mock) has no server head, so its head is
                  the static /sample.dxf, the synthesis of the very intake it
                  draws, at version 1; the Draw tools go live there too, and
                  Save stays honestly off (no target). */}
              {ENV_CAD_EDIT && (
                <EngineHeadOpener
                  drawingId={REQUESTED_DRAWING_ID}
                  enabled={!!studioGround && !!drafting && !!intake}
                  headKey={drawingState?.head ?? (mock ? 1 : null)}
                  fetchDxf={mock ? fetchSampleDxf : fetchDrawingDxf}
                />
              )}
              {/* W4f slice A1: a click on the drawing answers the armed
                  prompt's point steps; while a point command is live the
                  card carries data-cockpit-picking and the console's
                  click-to-select stands aside (the Viewer callback below). */}
              {ENV_CAD_EDIT && (
                <CanvasPointPicker
                  viewerRef={viewerRef}
                  ground={studioGround}
                  onPicking={(live) => {
                    const el = workspaceCardRef.current
                    if (!el) return
                    if (live) el.dataset.cockpitPicking = '1'
                    else delete el.dataset.cockpitPicking
                  }}
                />
              )}
            </DraftingRibbon>
          )}
          <div className="viewer-toolbar">
            {/* W4e: the toolbar is the reference's document-tab band. Start
                is the project board (the Browser tab, a real surface switch);
                the drawing is the active tab; + opens a DXF in the engine.

                ONE DOCUMENT, and that is on purpose, stated so nobody has to
                re-derive it: this shell has no multi-document state anywhere
                — no open-drawings collection, no per-document tab list, and
                DrawingIdentityProvider exposes a single `drawingId`. The band
                is therefore a SINGLE-document band wearing the reference's
                tab shape, not a tab strip with one tab in it. Real
                multi-document is a lane of its own (per-drawing viewer,
                checkout lease, job and converse scope, all of which are
                singular today), explicitly OUT of the P1 studio-shell pass.
                What P1 owed the reader was this sentence instead of a shape
                that implies capability the app does not have; every control
                below already names what it actually does, and none of them
                claims role="tab" or aria-selected. */}
            {studioGround && drafting && (
              <button type="button" className="doc-tab-start" onClick={() => onSelectSurface('browser')}>Start</button>
            )}
            <div className="viewer-title">
              {/* One loading voice per pane — the pulse-dot line in the viewer
                  announces loading; the title placeholder stays a muted dash. */}
              {shown ? `${projectName}.dwg` : <span className="dim">—</span>}
              {shown && (
                <span className="dim">
                  {' · '}{shown.polylines.length} polylines
                  {shown.inserts?.length ? ` · ${shown.inserts.length} inserts` : ''}
                  {shown.faces3d?.length ? ` · ${shown.faces3d.length} faces` : ''}
                  {' · '}{shown.layers.length} layers
                </span>
              )}
            </div>
            {studioGround && drafting && shown && (
              <button
                type="button"
                className="doc-tab-close"
                aria-label="Close the drawing view and return to Start"
                title="Close (back to Start)"
                onClick={() => onSelectSurface('browser')}
              >
                ×
              </button>
            )}
            {ENV_CAD_EDIT && studioGround && drafting && (
              <button
                type="button"
                className="doc-tab-add"
                aria-label="Open a DXF in the browser engine"
                title="Open a DXF"
                aria-expanded={importOpen}
                aria-controls="cockpit-import-pane"
                onClick={() => setImportOpen((o) => !o)}
              >
                +
              </button>
            )}
            <div className="viewer-actions">
              {/* Slice 11a: the running-count badge. Renders nothing at zero
                  and where the contract declares no build card, so the idle
                  band is byte-identical; one click expands the job spine. */}
              <SurfaceFrame.Builds />
              {/* Version-completed events surface as NT2 toasts; only the genuine
                  read-only-preview advisory keeps an amber note here. */}
              {!mock && previewing && (
                <span className="version-note readonly">viewing v{previewing.version} · read-only</span>
              )}
              {!mock && (
                <CheckoutControls
                  lockedByOther={otherHeldCheckout}
                  staleByOther={lock.staleByOther}
                  legacyByOther={lock.legacyByOther}
                  canTake={lock.canTake}
                  heldByUs={heldByUs}
                  unknown={lock.unknown}
                  readFailed={checkout.readFailed}
                  busy={checkout.busy}
                  onTake={checkout.actions.take}
                  onRelease={checkout.actions.release}
                  onRetry={checkout.actions.refresh}
                />
              )}
              {drawingState && (
                <>
                  <button
                    className="btn ghost"
                    data-cockpit="ribbon"
                    onClick={onUndo}
                    disabled={versionBusy || running || !!previewing || drawingMutationsBlocked || !canUndo}
                  >
                    Undo
                  </button>
                  <button
                    className="btn ghost"
                    data-cockpit="ribbon"
                    onClick={onRedo}
                    disabled={versionBusy || running || !!previewing || drawingMutationsBlocked || !canRedo}
                  >
                    Redo
                  </button>
                  {versionsMounted && <div className="vh-anchor">
                    <button
                      className="btn ghost"
                      onClick={onToggleHistoryTracked}
                      aria-expanded={historyOpen}
                      disabled={versionBusy}
                    >
                      History{previewing ? ` · v${previewing.version}` : ''}
                    </button>
                    {historyExit.shown && (
                      <VersionHistory
                        data={history}
                        error={historyErr}
                        loading={historyLoading}
                        previewingVersion={previewing?.version ?? null}
                        onPreview={onPreviewVersionTracked}
                        onBackToHead={onBackToHead}
                        onClose={closeHistory}
                        onRetry={loadHistory}
                        retryKey={rTarget === 'history'}
                        exiting={historyExit.exiting}
                        mock={mock}
                        capability={checkout.actions.getCapability()}
                        onRestored={onRestoreCommitted}
                        headWarning={unreadableHead}
                        mutationBlocked={drawingMutationsBlocked}
                      />
                    )}
                  </div>}
                </>
              )}
              {isEditFixture && (
                <>
                  <button
                    className="btn ghost"
                    onClick={() => setPendingEdit((p) => (p ? null : pendingEditDemo))}
                    disabled={applied}
                  >
                    {pendingEdit ? 'Hide pending edit' : 'Preview pending edit'}
                  </button>
                  <button className="btn ghost" onClick={applyVersion} disabled={applied}>
                    {applied ? 'Version applied' : 'Apply version'}
                  </button>
                </>
              )}
              <button className="btn ghost" data-cockpit="ribbon" onClick={() => viewerRef.current?.fit()}>Fit to bounds</button>
            </div>
          </div>
          {/* X1: a failed post-write viewer refresh — red row + Retry + honest
              fallback note (the completion itself already toasted plainly). */}
          {refreshFail && (
            <div className="inline-error" style={{ margin: '0 0 8px' }}>
              Couldn’t refresh the viewer — showing the previous version
              <button type="button" className="chip-act" onClick={onRetryViewerRefresh}>Retry</button>
              {rTarget === 'refresh' && <span className="key" aria-hidden="true">R</span>}
            </div>
          )}
          {/* cad_edit surface: ENV_CAD_EDIT is deliberately the FIRST operand
              so a flag-off build folds the whole cadedit module away (the
              module's own documented call-site contract). The engine fence
              stands: the only /cad/ contact stays engineWorker, inside
              cadedit/, never from here. */}
          {ENV_CAD_EDIT && (
            <CadEditSurface
              // F-4: the engine attribution NOTICE arrives from the tenant
              // capability contract at runtime (web source may not name the
              // engine — license fence). The session itself (and the save
              // target) come from the EngineSessionProvider wrapping this
              // workspace (W4d Slice A).
              notice={catalog?.cad_engine?.notice || ''}
            />
          )}
          <div className="viewer-wrap">
            {/* X3 whole-pane takeover: red dot + what failed + quiet reason + Retry. */}
            {loadErr && !signedOut && (
              <div className="pane-fail" role="alert" style={{ position: 'absolute', inset: 0 }}>
                <span className="pane-fail-title"><span className="dot red" aria-hidden="true" />Couldn’t load drawing</span>
                <span className="pane-fail-reason">{loadErr}</span>
                <button className="chip-act" onClick={() => setIntakeRetryKey((k) => k + 1)}>Retry</button>
                {/* There is always a way home: in live mode a dead backend makes
                    Retry unwinnable, so offer the same demo escape the
                    SignedOutGate already gives. */}
                {!mock && (
                  <button className="chip-act" onClick={() => setMock(true)}>Back to the demo</button>
                )}
              </div>
            )}
            {signedOut && <div className="overlay-msg">Sign in or explore the demo to load a drawing.</div>}
            {!intake && !loadErr && !signedOut && (
              // Indeterminate load: content-shaped pulse dot + verb, top-left —
              // the centered takeover position is reserved for failures (X3).
              <div className="loading-line dim" style={{ position: 'absolute', top: 14, left: 14 }}>
                <span className="dot live pulse" aria-hidden="true" /> Loading drawing
              </div>
            )}
            {intake && (() => {
              // W3 one-shell: the console OWNS this element — every prop, the
              // ref, the version/undo/redo imperative path — in BOTH shells.
              // Under the studio shell the element PORTALS into the ground
              // layer (z0, under the floating console) instead of rendering
              // inline; a null ground (rail off, old shell) renders inline
              // exactly as before, which is the rollback contract. The ground
              // viewer goes transparent so the studio void reads through.
              const viewerEl = (
                <Suspense fallback={<ViewerSkeleton />}>
                <Viewer
                  ref={viewerRef}
                  intake={intake}
                  colorForLayer={surfaceColorForLayer}
                  stringRoutes={solarStringRoutes}
                  visibleLayers={visibleLayers}
                  highlightHandles={overlay?.highlight_handles}
                  markers={overlay?.markers}
                  overlayPolylines={overlay?.polylines}
                  selectedHandle={selectedHandle}
                  // W4f slice A1: a click that answers an armed point prompt
                  // is not a selection (the picker stamps the card while a
                  // point command is live).
                  onSelectEntity={(handle) => {
                    if (workspaceCardRef.current?.dataset.cockpitPicking === '1') return
                    setSelectedHandle(handle)
                  }}
                  pendingEdit={pendingEdit || writeGhost}
                  background={studioGround ? 'transparent' : undefined}
                />
                </Suspense>
              )
              // W4a: the drawing is the ground for CAD and Solar CAD only;
              // on Browser/iOS it stays mounted (WebGL, lock, job state
              // survive) but hidden while that surface's own ground shows.
              return studioGround
                ? createPortal(<div className="studio-ground-viewer" hidden={!groundShowsDrawing(activeSurface)}>{viewerEl}</div>, studioGround)
                : viewerEl
            })()}
            {/* W4c-V2: under the studio the Legend and the readout live in
                the right palette (the SAME elements - one source of truth
                for every field); rail OFF renders them inline byte-for-byte.
                Geometry is client-derived from the intake entity in place. */}
            {(() => {
              const legendEl = shown ? (
                <Legend
                  layers={shown.layers}
                  counts={layerCounts}
                  colorForLayer={surfaceColorForLayer}
                  visibleLayers={visibleLayers}
                  onToggle={toggleLayer}
                />
              ) : null
              const readoutEl = intake ? (
                <SelectionReadout selection={selection} onDeselect={() => setSelectedHandle(null)} />
              ) : null
              // wideViewport: <=980px stacks the console; the dock's
              // floating placement has no home there, so the inline arm
              // (today's placement) renders instead of hiding the tools.
              // The Plan section is useful before a drawing or intake exists.
              // Keep the dock mounted on every wide drafting surface so the
              // entitlement controls never disappear in that honest-empty state.
              // Slice 2: `dockSections` (contract.rails.dock) replaces
              // `drafting` here: a surface declares its dock, or declares
              // none. paneOpen below is the SECOND gate, unchanged.
              if (studioGround && dockSections && wideViewport) {
                // Closed by its own control: nothing renders here until the View
                // tab's Properties tool reopens it (the condition above stays
                // literal for the App wiring pin).
                return paneOpen ? (
                  <PropertiesDock
                    layers={legendEl}
                    selection={readoutEl}
                    geometry={selectedEntityGeometry}
                    drawing={paneDrawingFacts}
                    onClose={() => setPaneOpen(false)}
                    plan={<SurfaceFrame.Entitlement at="docked" />}
                  />
                ) : null
              }
              return <>{legendEl}{readoutEl}</>
            })()}
            {/* W4b cockpit: view snaps on the drawing (studio only). */}
            {studioGround && intake && groundShowsDrawing(activeSurface) && (
              <ViewCluster viewerRef={viewerRef} />
            )}
          </div>
        </div>,
        )}

        <div className="result-block enter" style={{ '--rank': 2 }} ref={resultBlockRef}>
          <ResultPanel
            running={running}
            runStatus={runStatus}
            runProgress={runProgress}
            runElapsedMs={runElapsedMs}
            error={runErr}
            result={result}
            tool={selectedTool}
            onRetry={onRetry}
            notices={
              /* NR banners dock UNDER the header of the affected pane (the
                 result) — rendered right after its <h3>; 2+ conditions collapse
                 to one line with a count instead of stacking. */
              advisories.length >= 2 ? (
                <div className="banner">
                  <span>{advisories.length} advisories — {advisories.join(' · ')}</span>
                </div>
              ) : (
                <>
                  {quotaShown && <QuotaCard message={quotaShown.message} remaining={usage?.cap?.remaining} onAction={openSessionDetails} />}
                  {runQuotaShown && (
                    <QuotaCard
                      kind="daily_runs"
                      message={runQuotaShown.error?.message}
                      tier={runQuotaShown.tier}
                      limit={runQuotaShown.limit}
                      used={runQuotaShown.used}
                      onAction={openSessionDetails}
                    />
                  )}
                  {entitlementError && (
                    <EntitlementNotice
                      required={entitlementError.required}
                      tier={entitlementError.tier || entTier}
                      message={entitlementError.error?.message}
                    />
                  )}
                  {degraded && <DegradedBanner />}
                </>
              )
            }
          />
          {/* Quiet Details -> the run's DT2 provenance drawer (receipt area). */}
          {result && !running && (
            <div className="result-details-row">
              <button type="button" className="chip-act" onClick={openRunDetails}>Details</button>
            </div>
          )}
        </div>

        {/* Agent tier (wire §11): the conversational surface for this drawing's
            session. LIVE only — never rendered in mock. 'race' keeps the chip
            primary; taking the chip unmounts this (onRun) without cancelling
            the server-side turn. */}
        {/* T1 operator decision card. Rendered whenever this session has a
            pending overlay, independent of agentMode: a proposal outlives the
            panel that produced it, and an operator who dismissed the panel
            must still be able to decide. */}
        {!mock && themeOverlay.pendingProposalId && (
          <OverlayDecisionCard
            proposal={{ proposal_id: themeOverlay.pendingProposalId,
                        tokens: themeOverlay.tokens }}
            documentVersion={themeOverlay.documentVersion}
            onDecide={(proposalId, opts) => themeOverlay.decide(proposalId, opts)}
          />
        )}

        {annotationEnabled && annotations.annotation && (
          <AnnotationDecisionCard
            annotation={annotations.annotation}
            busy={annotations.busy}
            error={annotations.error}
            confirmation={annotations.confirmation}
            onPreview={annotations.preview}
            onAccept={annotations.accept}
            onReject={annotations.reject}
            onRetry={annotations.retry}
            onUndo={annotations.undo}
          />
        )}

        {!mock && agentMode && agentSessionId && (
          <ConversePanel
            sessionId={agentSessionId}
            userTurns={agentTurns}
            onDismiss={clearAgentMode}
            onLinkClaude={() => setClaudeOpen(true)}
            onAttachJob={onAttachAgentJob}
            onJobLinked={refreshJobs}
          />
        )}

        {/* Studio drafting surfaces host it in the dock (see the dock's Plan
            section); everywhere else it renders here, unchanged. Slice 4a: the
            choice is the frame's `entitlement.placement`, declared once. */}
        <SurfaceFrame.Entitlement at="inline" />
        </main>

        <div className="bar-dock">
          {/* W4e slice H: the engine's command prompt ("LINE  Specify first
              point:") portals here, the line above the command input, on the
              studio's drafting surfaces (empty otherwise). */}
          {studioGround && drafting && <div id="cockpit-prompt-slot" className="cockpit-prompt-slot" />}
          {/* SB3 state strips ride above the well: running / failed. Decisions
              (the route) attach as resolver rows / decision strips below. */}
          {running && (
            <div className="strip-running enter">
              <span className="dot live pulse" aria-hidden="true" />
              <span className="verb">
                {(runProgress || runStatus || 'running')}
                {selectedTool?.name ? ` — ${selectedTool.name}` : ''}
                {runElapsedMs != null ? ` · ${fmtElapsed(runElapsedMs)}` : ''}
              </span>
              <span className="key hot">Esc</span>
              <span className="dim">interrupt</span>
            </div>
          )}
          {!running && (runErr || routeErr) && (
            <div className="strip-failed enter">
              <span className="dot red" aria-hidden="true" />
              <span className="strip-sentence">
                {routeErr
                  ? `Couldn’t route the prompt — ${routeErr}`
                  : `Couldn’t run ${selectedTool?.name || 'the tool'} — ${runErr}`}
                <span className="dim"> · your last good result is unchanged</span>
              </span>
              <button
                type="button"
                className="chip-act"
                onClick={routeErr ? onDispatch : onRetry}
              >
                Retry
              </button>
              <span className="key">R</span>
            </div>
          )}
          {/* Calm agent-tier advisory (degraded fallback / quota / grant):
              amber square dot + sentence — the deterministic result above it
              rendered exactly as today; this only says why there's no chat. */}
          {agentBanner && !running && (
            <div className="strip-decision enter" role="status">
              <span className="dot square" aria-hidden="true" />
              <span className="strip-sentence">{agentBanner.message}</span>
              {agentBanner.kind === 'grant' && (
                <button type="button" className="chip-act" onClick={() => setClaudeOpen(true)}>
                  Link account
                </button>
              )}
              <button type="button" className="chip-neutral" onClick={clearAgentBanner}>
                Dismiss
              </button>
            </div>
          )}
          <RoutePanel
            route={route}
            tools={tools}
            running={running || !!previewing}
            writeLocked={writeLocked}
            writeEntitled={canRunWrite}
            onConfirmIntent={onConfirmCatalogRun}
            onPickAlternative={onPickAlternative}
            onOpenAuthor={onOpenAuthor}
            onDismiss={dismissRoute}
          />
          {/* Slice 4a: the command well is the frame's `commandBar` render
              prop (declared at the SurfaceFrame call above), so slice 5 has one
              seat to unify. The element and its props are unchanged. Slice 8a
              round 3 wires secretRefusal at that same declaration (the bar has
              no guard of its own; the transport raises the refusal), not here. */}
          <SurfaceFrame.CommandBar />
        </div>

        {/* The golden path's payoff (result numbers) and the running strip are
            both silent to a screen reader. One PERMANENTLY-mounted polite region
            announces the mutation; styles inline because no .sr-only utility
            exists in the sheet. */}
        <div
          role="status"
          aria-live="polite"
          style={{
            position: 'absolute', width: 1, height: 1, padding: 0, margin: -1,
            overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0,
          }}
        >
          {running
            ? `Running ${selectedTool?.name || 'tool'}`
            : result?.ok
              ? `${result.tool} complete${result.result?.total != null ? ` — total ${Number(result.result.total).toLocaleString()}` : ''}`
              : ''}
        </div>

        <SurfaceFrame.Toast />
      </div>

      {/* W4d Slice D seating: on drafting surfaces under the studio the job
          monitor boots as a 44px spine on the right (the reference gives that
          edge to the viewport and the viewcube); its live count stays visible
          and one click expands it. Rail OFF: no spine, the rail renders
          exactly as before. Slice 4a: that derivation (contract.rails.right
          === 'job-spine', ANDed with the studio and viewport terms) is the
          frame's, from the posture passed above. */}
      <SurfaceFrame.JobRail />

      <footer className="foot-bar" data-checkout-instance={checkout.instanceId} data-controller-instance={workspaceInstanceId}>
        {/* P1: on the studio's drafting surfaces the bar is three REGIONS —
            documents, our honesty band, the drawing instruments — each a real
            element, so the boundary between a coordinate readout and a build
            hash is a region edge and not one more cell in a strip. `footRegions`
            is the ONE gate; off, every FootRegion is a fragment and this
            footer's DOM is byte-identical to what the old shell renders. */}
        <FootRegion on={footRegions} name="docs">
        {/* W4e: on the studio's drafting surfaces the status bar opens with
            the reference's Model tab, the drawing's name, and + (the project
            board). Rail OFF and every other surface: nothing here. */}
        {studioGround && drafting && (
          <StatusTabs name={shown ? `${projectName}.dwg` : ''} onStart={() => onSelectSurface('browser')} />
        )}
        </FootRegion>
        <FootRegion on={footRegions} name="system">
        {/* Traversal left: a named "← Parent" link while a project is open. */}
        {!mock && openProjectId && (
          <button type="button" className="chip-act" onClick={onCloseProject}>← All projects</button>
        )}
        {/* Real statuses get the 6px dot + tinted sentence-case word; counts and
            spend are muted metadata (green is reserved for genuine states). */}
        {mock ? (
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">mock (no cloud)</span></span>
        ) : authRequired ? (
          /* the ONLY unauthenticated signal is the public /api/health ping — a
             rosy "cloud live · N tools" over a 401-walled app would be a lie */
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">sign-in required</span></span>
        ) : health ? (
          health.aps_live
            ? <span className="foot-stat"><span className="dot" aria-hidden="true" />backend · <span className="ok-txt">cloud live</span></span>
            : <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">local only</span></span>
        ) : !authConfigured ? (
          /* Gating window (a VITE_MOCK=0 build with Auth0 unconfigured, before the
             401 auto-fallback flips to mock): never claim a green "live" state we
             haven't confirmed. Neutral until the fallback lands. */
          <span className="foot-stat"><span className="dot square" aria-hidden="true" />backend · <span className="warn-txt">connecting…</span></span>
        ) : (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />backend · <span className="ok-txt">live</span></span>
        )}
        {mock ? (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />local solver · <span className="ok-txt">ready</span></span>
        ) : authRequired ? null : health ? (
          <span className="foot-stat">
            <span className={health.da_client_present ? 'dot' : 'dot square'} aria-hidden="true" />
            data agent · <span className={health.da_client_present ? 'ok-txt' : 'warn-txt'}>
              {health.da_client_present ? 'ready' : 'absent'}
            </span> · <span className="dim">{plural(health.n_tools, 'tool')}</span>
          </span>
        ) : (
          <span className="foot-stat"><span className="dot" aria-hidden="true" />local solver · <span className="ok-txt">ready</span></span>
        )}
        <span className="dim">{plural(capCount, 'cap')} · {catalog.families.length} famil{catalog.families.length === 1 ? 'y' : 'ies'} · tier {gateTier}</span>
        {!mock && usage && (
          <span className="dim">${Number(usage.today?.usd_est || 0).toFixed(3)} today</span>
        )}
        {mock && !tourOn && tourAvailable.current && studioGround && drafting && (
          <button
            type="button"
            className="chip-neutral"
            onClick={() => {
              tourStartedRef.current = true
              tourStepRef.current = 0
              setTourStep(TOUR_STEPS[0]?.id)
              track('tour.started', { entry: 'button' })
              track('tour.step_reached', { step_id: TOUR_STEPS[0]?.id })
              setTourLanded(true); setTourOn(true)
            }}
          >
            Restart guided tour
          </button>
        )}
        <span style={{ marginLeft: 'auto' }}>build <span style={{ fontFamily: 'var(--font-mono)' }}>{__BUILD_HASH__}</span> · {mock ? 'sample data' : 'live'}</span>
        </FootRegion>
        {/* The drawing instruments, anchored at the right edge like the
            reference's. They moved BELOW the build hash in the DOM so the
            regions read left-to-right without a single `order:` — and that
            move is a no-op off the studio, where both render null (the
            CockpitStatus gate below, and SurfaceFrame.Cockpit's own
            posture.studio gate). */}
        <FootRegion on={footRegions} name="instruments">
        {/* W4b cockpit: live cursor coordinates, scale, counts, selection
            (studio only; DOM-written at rAF rate, never React state). */}
        {studioGround && groundShowsDrawing(activeSurface) && (
          <CockpitStatus ground={studioGround} viewerRef={viewerRef} shown={shown} selectedHandle={selectedHandle} />
        )}
        {/* W4e: the reference's drafting toggles (honestly off) and fullscreen. */}
        <SurfaceFrame.Cockpit />
        </FootRegion>
      </footer>

      {opsExit.shown && <OpsDrawer onDismiss={() => setOpsDismissed(true)} exiting={opsExit.exiting} />}

      {signedIn && customizeExit.shown && <CustomizePanel tenant={tenant} onDismiss={() => setCustomizeOpen(false)} exiting={customizeExit.exiting} />}

      {/* DT2 drawer: fixed over the events rail (row 2, col 3) — the rail
          behind never re-flows. Esc (global ladder) or the header cap closes. */}
      <DetailsDrawer data={drawer} onClose={() => setDrawer(null)} />

      {/* M5: the ?demo=tour walkthrough. Mock-only — the tour drives real mock
          handlers, so it must never point at a live/paid backend. */}
      {mock && tourOn && (
        <DemoTour
          // Slice 4b: the console's declared anchors for this surface (step id
          // -> data-tour id). TOUR_STEPS is untouched; the contract carries it.
          anchors={surfaceSlots.tourAnchors?.console ?? null}
          onCannedPrompt={onCannedPrompt}
          onIndexChange={onTourStepChange}
          onExit={onTourExit}
          landed={tourLanded && !running && !routing}
          busy={running || routing}
        />
      )}
      {/* Lane E operator console entry: renders nothing unless a single
          probe of GET /api/operator/sessions says the operator surface is
          mounted (LEAF_OPERATOR_ENABLED=1) AND this caller holds a grant.
          Tenant deployments never see it and never re-probe. */}
      <OperatorEntry />
    </div>
    </SurfaceFrame>
  )
}
