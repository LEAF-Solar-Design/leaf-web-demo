import './structural.css'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, Suspense } from 'react'
import { track, setTourStep } from './telemetry.js'
// The 3D viewer drags in `three`; loading it lazily (mirroring the auth.js
// dynamic-import pattern) keeps first paint off the critical path.
const Viewer = React.lazy(() => import('./components/Viewer.jsx'))
import Legend from './components/Legend.jsx'
import ToolsPanel from './components/ToolsPanel.jsx'
import ResultPanel from './components/ResultPanel.jsx'
import AuthorPanel from './components/AuthorPanel.jsx'
import SelectionReadout from './components/SelectionReadout.jsx'
import PromptBox from './components/PromptBox.jsx'
import RoutePanel from './components/RoutePanel.jsx'
import JobRail from './components/JobRail.jsx'
import DegradedBanner from './components/DegradedBanner.jsx'
import OperatorEntry from './components/operator/OperatorEntry.jsx'
import EntitlementGate, { EntitlementNotice } from './components/EntitlementGate.jsx'
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
import ProductSurfaceTabs, { ProductSurfaceFrame, WorkspaceProjectSlot } from './components/ProductSurfaceTabs.jsx'
import { deriveWorkspaceProjectState } from './site/workspaceProjectState.js'
import IosSurface from './ios/IosSurface.jsx'
import { ENV_IOS_SURFACE } from './ios/flag.js'
import CadEditSurface from './cadedit/CadEditSurface.jsx'
import { markInstant } from './lib/instant.js'
import { agentBannerFor } from './lib/agentBanner.js'
import { selectEntity } from './lib/selectEntity.js'
import { countEntitiesByLayer } from './lib/layerCounts.js'
import { ENV_CAD_EDIT } from './cadedit/flag.js'
import {
  productSurfaceStates,
  productSurfaceFromSearch,
  searchForProductSurface,
} from './site/productSurfaces.js'
import { fetchIosSurfaceStatus } from './ios/iosSurfaceStatus.js'
// `logout` is no longer imported here: the session controller owns ending a
// session (useSessionController defaults endSession to auth.js logout).
import { authConfigured, login, isSignedIn, handleRedirectCallback, isAuthRedirectCallback } from './auth.js'
import { shouldAutoDemo } from './demoState.js'
import { humanizeError } from './errorHumanize.js'
import { cadTimingRows } from './cadTimingPresentation.js'
import {
  getSessionHolderId, claimHolderId, lockState,
  stageCheckoutReloadHandoff, bootstrapCheckoutReloadHandoff,
  holdCheckoutReloadAuthority, remintSessionHolderId,
} from './checkoutIdentity.js'
import {
  confirmRunIntent, createCatalogRunContext, createCatalogToolSnapshot, createRunIntentState,
  dismissRunIntent, drawingVersionForRun, mintCorrelationId, prepareCatalogRunParams, stageRunIntent,
} from './runIntent.js'
import useExit from './useExit.js'
import Toast from './components/Toast.jsx'
import DetailsDrawer from './components/DetailsDrawer.jsx'
import {
  config, getSession, getTools, getCapabilities, runTool, runToolAsync,
  getJob, recordToEnvelope, publishStagedAuthor, getDrawingIntake,
  getDrawingVersions, undoDrawing, redoDrawing, takeCheckout, releaseCheckout, nlPrompt,
  createOrg, listProjects, createProject, openProject, subscribeUnauthorized,
  saveEditedDrawingVersion,
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
import useJobController from './controllers/useJobController.js'
import useAuthorStageController from './controllers/useAuthorStageController.js'
import useDrawingVersionController from './controllers/useDrawingVersionController.js'
import usePlatformTrustController from './controllers/platform/usePlatformTrustController.js'
import useWorkspaceController from './controllers/workspace/useWorkspaceController.js'
import useCatalogController from './controllers/catalog/useCatalogController.js'
import useSessionController from './controllers/session/useSessionController.js'
import { consoleAuthRequired, consoleSignedOut } from './controllers/session/consoleGate.js'
import { resolveCheckoutDrawingId } from './controllers/checkout/createCheckoutController.js'
import { useDrawingIdentity } from './drawing/DrawingIdentityProvider.jsx'

// Calm layer palette, re-derived at higher lightness for the DARK CADViewport
// canvas (--cv-bg #0f0f11) — same hue spacing as the retired light-paper set so
// the legend swatches stay distinguishable.
const PALETTE = ['#6b9fd4', '#8fbf9c', '#b49bd1', '#d4af6e', '#cf8fa6', '#79bcc7']

// Suspense fallback while the lazy viewer chunk arrives — L1 indeterminate:
// pulse dot + verb, top-left (the centered position is reserved for X3 failures).
function ViewerSkeleton() {
  return (
    <div aria-hidden="true" style={{ position: 'absolute', inset: 0, background: '#0f0f11' }}>
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

// Collapsible left-rail section (keeps the classic catalog reachable but
// secondary to the prompt box — the primary path).
function Section({ title, count, open, onToggle, children, innerRef, className = '' }) {
  return (
    <div className={`section ${className} ${open ? '' : 'collapsed'}`.replace(/\s+/g, ' ').trim()} ref={innerRef}>
      <button className="section-head" onClick={onToggle} aria-expanded={open}>
        <span>{title}{count != null ? <span className="n"> · {count}</span> : null}</span>
        <span className="chev">{open ? 'hide' : 'show'}</span>
      </button>
      {open && <div className="section-body">{children}</div>}
    </div>
  )
}

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

  // --- projects / orgs workspace (UI wave 2, item 1) ---

  // --- single-writer checkout lock (item 3) ---
  const [checkout, setCheckout] = useState(null)         // {holder, acquired, expires} | null (from /versions)
  const [checkoutBusy, setCheckoutBusy] = useState(false) // take/release request in flight (3B)
  const [checkoutUnknown, setCheckoutUnknown] = useState(true)
  const [checkoutReadFailed, setCheckoutReadFailed] = useState(false)
  const initialHolderRef = useRef(null)
  if (!initialHolderRef.current) initialHolderRef.current = getSessionHolderId()
  const reloadHandoffRef = useRef(undefined)
  if (reloadHandoffRef.current === undefined) {
    reloadHandoffRef.current = bootstrapCheckoutReloadHandoff({
      holder: initialHolderRef.current,
      drawingId: REQUESTED_DRAWING_ID,
      deferForAuthCallback: isAuthRedirectCallback(),
    })
  }
  // The server returns this bearer proof once when the session takes the lock.
  // Keep it outside rendered state. The only storage edge is the one-use,
  // short-lived beforeunload handoff consumed above.
  const capabilityRef = useRef(null)
  const holderClaimRef = useRef(null)
  const reloadAuthorityRef = useRef(null)
  const [, bumpCheckoutAuthority] = useState(0)

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
  const { converse } = useWorkspaceControllers()
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
    undoVersion: (drawingId) => undoDrawing(mock, drawingId, capabilityRef.current),
    redoVersion: (drawingId) => redoDrawing(mock, drawingId, capabilityRef.current),
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
  }), [sessionActions])
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
    hintLane,
    runnableTools: slashTools,
    capabilityCount: capCount,
  } = catalogState
  const {
    retryTools,
    loadCatalog,
    upsertTool,
    toggleFamily,
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
  const slashCommandActions = useMemo(() => ({
    help: () => {
      // The menu IS the help: reopen it on a bare slash and put the caret back
      // in the well so the next keystroke filters.
      onPromptChange('/')
      barInputRef.current?.focus()
    },
  }), [onPromptChange])

  // --- single-writer checkout (item 3) ---
  // The holder is a per-session display identity, never the tenant. A copied tab
  // initially inherits sessionStorage, so the claim channel remints the duplicate.
  const [ownHolder, setOwnHolder] = useState(initialHolderRef.current)
  useEffect(() => {
    // A reload handoff is only provisional authority. The Web Lock below
    // arbitrates any cloned copies before this runtime joins the holder channel.
    if (reloadHandoffRef.current) return undefined
    const claim = claimHolderId({ id: ownHolder, onRemint: setOwnHolder })
    holderClaimRef.current = claim
    return () => claim.stop()
    // Claim once per runtime. A remint must not restart the claim loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const rawCheckout = demoLocked
    ? { holder: 'another-session', acquired: new Date().toISOString(), expires: new Date(Date.now() + 3600e3).toISOString() }
    : checkout
  const baseLock = lockState({ mock, checkout: rawCheckout, unknown: checkoutUnknown, ownHolder })
  const unprovenOwnLock = baseLock.heldByUs && !capabilityRef.current ? rawCheckout : null
  const lock = {
    ...baseLock,
    heldByUs: baseLock.heldByUs && !!capabilityRef.current,
    otherHeld: baseLock.otherHeld || unprovenOwnLock,
    writeLocked: baseLock.writeLocked || !!unprovenOwnLock,
    canTake: baseLock.canTake || !!unprovenOwnLock,
  }
  const otherHeldCheckout = lock.otherHeld || unprovenOwnLock
  const writeLocked = lock.writeLocked || drawingMutationsBlocked
  const heldByUs = lock.heldByUs
  const staleHeldCheckout = lock.stale ? lock.otherHeld : null
  const legacyHeldCheckout = lock.legacy ? lock.otherHeld : null

  useEffect(() => {
    const stageReloadHandoff = () => {
      const provisional = reloadHandoffRef.current
      const capability = capabilityRef.current || provisional?.capability
      const drawingId = resolveCheckoutDrawingId({
        drawingState,
        requestedDrawingId: REQUESTED_DRAWING_ID,
      })
      if (capability && drawingId) {
        holderClaimRef.current?.stop()
        stageCheckoutReloadHandoff({
          capability,
          holder: provisional?.holder || ownHolder,
          drawingId: provisional?.drawingId || drawingId,
        })
      }
    }
    window.addEventListener('beforeunload', stageReloadHandoff)
    return () => window.removeEventListener('beforeunload', stageReloadHandoff)
  }, [drawingState, ownHolder])

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
        // A 200 from /api/session IS the platform session — the same proof
        // ToolCast activates on. Live only: a mock 200 proves nothing.
        if (!mock) sessionActions.activate({ tenant: t, tier: ti, org: o })
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
  // Checkout lock (item 3): read the current drawing's version manifest and pick
  // up its `checkout` (sibling contract adds it to /versions). Unknown and failed
  // reads stay fail-closed, and sequence fencing prevents a stale response from
  // re-enabling writes after the drawing changes.
  const checkoutSeqRef = useRef(0)
  const loadCheckout = useCallback(async () => {
    if (mock) {
      setCheckout(null)
      setCheckoutUnknown(false)
      setCheckoutReadFailed(false)
      return
    }
    const did = resolveCheckoutDrawingId({
      drawingState,
      requestedDrawingId: REQUESTED_DRAWING_ID,
    })
    const seq = ++checkoutSeqRef.current
    setCheckoutUnknown(true)
    try {
      const v = await getDrawingVersions(mock, did)
      if (seq !== checkoutSeqRef.current) return
      setCheckout(v?.checkout || null)
      setCheckoutUnknown(false)
      setCheckoutReadFailed(false)
    } catch {
      if (seq !== checkoutSeqRef.current) return
      setCheckout(null)
      setCheckoutUnknown(true)
      setCheckoutReadFailed(true)
    }
  }, [mock, drawingState])

  useEffect(() => { loadCheckout() }, [loadCheckout])

  // Install a reload handoff only while this runtime owns the origin-wide,
  // exclusive authority lock. Reload releases the old runtime's lock and wakes
  // this one. Duplicated tabs queue, so they cannot write concurrently.
  useEffect(() => {
    const handoff = reloadHandoffRef.current
    if (mock || !handoff) return undefined
    const authority = holdCheckoutReloadAuthority({
      handoff,
      onAcquired: (owned) => {
        capabilityRef.current = owned.capability
        reloadHandoffRef.current = null
        holderClaimRef.current = claimHolderId({
          id: owned.holder,
          onRemint: setOwnHolder,
          now: () => 0,
        })
        bumpCheckoutAuthority((version) => version + 1)
        loadCheckout()
      },
      onError: () => {
        capabilityRef.current = null
        reloadHandoffRef.current = null
        setOwnHolder(remintSessionHolderId())
        bumpCheckoutAuthority((version) => version + 1)
        loadCheckout()
      },
    })
    reloadAuthorityRef.current = authority
    if (!authority.active) {
      capabilityRef.current = null
      reloadHandoffRef.current = null
      setOwnHolder(remintSessionHolderId())
      bumpCheckoutAuthority((version) => version + 1)
    }
    // The module runtime owns the lock. React StrictMode cleanup must not release
    // checkout authority between its setup probes; explicit Release calls stop it.
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Take / Release the single-writer checkout (3B). Both refetch /versions after
  // the call so the chip reflects the real lock (source of truth), and both stay
  // calm on failure — a 409 (someone else took it) / 403 (not the holder) just
  // means the refetched manifest shows the truth. Live only.
  const onTakeCheckout = useCallback(async () => {
    if (mock) return
    const did = resolveCheckoutDrawingId({
      drawingState,
      requestedDrawingId: REQUESTED_DRAWING_ID,
    })
    setCheckoutBusy(true)
    try {
      const result = await takeCheckout(did, ownHolder, undefined, capabilityRef.current)
      if (result?.acquired && result.checkout_capability) {
        reloadAuthorityRef.current?.stop()
        const authority = holdCheckoutReloadAuthority({
          handoff: {
            capability: result.checkout_capability,
            holder: ownHolder,
            drawingId: did,
          },
          onAcquired: (owned) => {
            capabilityRef.current = owned.capability
            bumpCheckoutAuthority((version) => version + 1)
          },
        })
        reloadAuthorityRef.current = authority
        if (!authority.active || !(await authority.acquired)) {
          capabilityRef.current = null
          try { await releaseCheckout(did, result.checkout_capability) } catch { /* fail closed */ }
        }
      } else if (!result?.acquired) {
        capabilityRef.current = null
        reloadAuthorityRef.current?.stop()
        reloadAuthorityRef.current = null
      }
    } catch (error) {
      if (error?.status === 403 || error?.status === 409) {
        capabilityRef.current = null
        reloadAuthorityRef.current?.stop()
        reloadAuthorityRef.current = null
      }
    }
    finally {
      await loadCheckout()
      setCheckoutBusy(false)
    }
  }, [mock, drawingState, ownHolder, loadCheckout])

  const onReleaseCheckout = useCallback(async () => {
    if (mock) return
    const did = resolveCheckoutDrawingId({
      drawingState,
      requestedDrawingId: REQUESTED_DRAWING_ID,
    })
    setCheckoutBusy(true)
    try {
      await releaseCheckout(did, capabilityRef.current)
      capabilityRef.current = null
      reloadAuthorityRef.current?.stop()
      reloadAuthorityRef.current = null
    } catch (error) {
      if (error?.status === 403 || error?.status === 409) capabilityRef.current = null
    }
    finally {
      await loadCheckout()
      setCheckoutBusy(false)
    }
  }, [mock, drawingState, loadCheckout])

  // Prefer ending checkout authority before leaving this origin for Auth0. If
  // the release cannot complete, login still proceeds and the marked, one-use
  // auth-return handoff above preserves the capability behind the Web Lock.
  const onLogin = useCallback(async () => {
    if (!mock && capabilityRef.current) {
      const did = resolveCheckoutDrawingId({
        drawingState,
        requestedDrawingId: REQUESTED_DRAWING_ID,
      })
      try {
        await releaseCheckout(did, capabilityRef.current)
        capabilityRef.current = null
        reloadAuthorityRef.current?.stop()
        reloadAuthorityRef.current = null
        // The server lease is already gone. Converge the rendered lock state
        // before Auth0 navigation so a rejected redirect cannot leave a stale
        // "You hold the edit lock" control with no bearer capability.
        setCheckout(null)
        setCheckoutUnknown(false)
        setCheckoutReadFailed(false)
        bumpCheckoutAuthority((version) => version + 1)
      } catch { /* the auth-return handoff remains the fail-closed fallback */ }
    }
    await login()
  }, [mock, drawingState])

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
  const authorAuthorityProvider = useCallback(async (description) => {
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
      const response = await startAgentTurn(description, { source: 'author_panel', purpose: 'stage_authority' })
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
    const catalogTool = tools.find((candidate) => candidate.name === decision.tool)
    if (!catalogTool) {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      return decision
    }
    if (!mock && !tenant) return
    if (!catalogRunContextRef.current) {
      setRunErr('This workspace has no canonical drawing version to run. Import a drawing first.')
      return
    }
    const isWrite = (catalogTool.capabilities || []).includes('drawing.write')
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
    const prepared = prepareRunParams(catalogTool, decision.params)
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `${runIntentSessionRef.current}:${++runIntentSeqRef.current}`,
      toolName: catalogTool.name,
      params: prepared,
      context: catalogRunContextRef.current,
      toolSnapshot: createCatalogToolSnapshot(catalogTool),
    })
    runIntentStateRef.current = staged.state
    const armed = {
      ...decision,
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
  }, [tools, mock, tenant, prepareRunParams, running, previewing, writeLocked, canRunWrite, catalogRunContext])
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
    if (writeLocked && (tool.capabilities || []).includes('drawing.write')) return null
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
          checkoutCapability: capabilityRef.current || undefined,
          onSubmit,
          onStatus,
        })),
    })

    if (!mock) {
      loadUsage(); loadCheckout()
      if (openProjectId) rehydrate()
    }
    return envelope
  }, [agentMode, catalogRunContext, clearAgentMode, dismissRoute, loadCheckout, loadUsage, mock,
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
  const onAuthor = useCallback(async (description, targetToolName = null) => {
    // R5 only stages bytes. It must not place a tool in the runnable catalog.
    return authorStage.stage(description, targetToolName)
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
  const onUseAuthored = useCallback((tool) => {
    if (!tool) return
    commitCatalogDecision({
      lane: 'run', tool: tool.name, params: {}, confidence: 0.99,
      rationale: `Authored just now — confirm to run “${tool.name}”.`,
      alternatives: [],
    })
    setTimeout(() => document.querySelector('main')?.scrollIntoView({ block: 'start', behavior: 'smooth' }), 0)
  }, [commitCatalogDecision])

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

  const onDispatch = useCallback((override) => {
    if (running) return undefined
    return catalogActions.dispatch(override)
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

  // Global key ladder: ⌘K summons the bar; Esc closes the topmost surface
  // (drawer > history > route/failed strip > running run > selection > open
  // project); R retries the highest-priority visible error (outside text
  // inputs); any OTHER bare printable keystroke falls into the prompt bar
  // (type-to-fall-through).
  useEffect(() => {
    const onKey = (e) => {
      const tag = ((e.target && e.target.tagName) || '').toLowerCase()
      const typing = tag === 'input' || tag === 'textarea'
      // Hotkey-driven changes land frame-of-keypress (data-instant, W0#7).
      // Stamp only a branch that will handle the key. An ordinary r/R typed
      // into a field, or an inactive retry rung, must keep normal motion.
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        markInstant()
        e.preventDefault()
        barInputRef.current?.focus()
        return
      }
      if (e.key === 'Escape') {
        if (drawer) { markInstant(); setDrawer(null); return }
        if (historyOpen) { markInstant(); closeHistory(); return }
        if (route) { markInstant(); dismissRoute(); return }
        if (routeErr || runErr) { markInstant(); clearRouteError(); clearRunErr(); return }
        if (running) {
          markInstant()
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
          return
        }
        if (selectedHandle) { markInstant(); setSelectedHandle(null); return }
        // Bottom rung: the WorkspaceSummary Esc cap — close the open project
        // only once every higher surface has already yielded.
        if (openProjectId) { markInstant(); onCloseProject() }
        return
      }
      // R: fire the ladder's active rung. rTarget === 'result' means
      // ResultPanel's own listener owns the keypress (duplicating it here
      // double-fired the retry: two POST /api/run from one keypress).
      if (!typing && (e.key === 'r' || e.key === 'R') &&
          !e.metaKey && !e.ctrlKey && !e.altKey &&
          rTarget && rTarget !== 'result') {
        markInstant()
        e.preventDefault()
        if (rTarget === 'route') onDispatch()
        else if (rTarget === 'history') loadHistory()
        else if (rTarget === 'tools') retryTools()
        else if (rTarget === 'catalog') loadCatalog()
        else if (rTarget === 'refresh') onRetryViewerRefresh()
        return
      }
      // Type-to-fall-through (operator rule): a bare printable keystroke on the
      // surface always falls into the prompt bar. Focus BEFORE the default
      // action so the character itself lands in the input; visible mnemonic
      // rungs above (⌘K, Esc, R-on-failed-strip) keep priority. Never steals
      // from an editable element.
      const editable = typing || tag === 'select' || (e.target && e.target.isContentEditable)
      // A focused interactive control keeps its keys (Space must ACTIVATE a
      // button, not yank focus); Space never falls through; overlays (drawer,
      // history) keep typing local to themselves.
      const interactive = e.target instanceof Element &&
        e.target.closest('button, a, summary, [role="button"], [role="option"], [role="menuitem"]')
      if (!editable && !interactive && !drawer && !historyOpen &&
          !e.metaKey && !e.ctrlKey && !e.altKey && e.key.length === 1 && e.key !== ' ') {
        barInputRef.current?.focus()
      }
    }
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

  return (
    <div className="app">
      <header className="top">
        <div className="mark"><span className="diamond" aria-hidden="true" /> Leaf — build CAD tools with AI</div>
        <div className="proj">
          <ProjectSwitcher
            mock={mock}
            projectName={projectName}
            orgId={orgId}
            projects={projects}
            openProjectId={openProjectId}
            currentName={currentProjectName}
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

      <aside className="nav">
        <div className="fam-title">
          Catalog · {catalog.families.length} famil{catalog.families.length === 1 ? 'y' : 'ies'} · {capCount} caps
          {catalog.source === 'flat-fallback' ? ' · flat' : ''}
        </div>
        {catalogErr && !signedOut && (
          <>
            <div className="inline-error" style={{ margin: '0 4px 4px' }}>
              Couldn’t load families: {catalogErr}
              <button type="button" className="chip-act" onClick={loadCatalog}>Retry</button>
              {rTarget === 'catalog' && <span className="key" aria-hidden="true">R</span>}
            </div>
            <div className="dim" style={{ margin: '0 4px 8px', fontSize: 11.5 }}>Showing the flat tool list instead.</div>
            <Section title="Tools" count={tools.length} open={toolsOpen} onToggle={() => setToolsOpen((o) => !o)}>
              <ToolsPanel
                tools={tools}
                writeLocked={writeLocked}
                writeEntitled={canRunWrite}
                error={toolsErr}
                onRetry={retryTools}
                retryKey={rTarget === 'tools'}
                running={running || !!previewing}
                selectedTool={selectedTool}
                onRequestRun={onRequestCatalogRun}
                onOpenTool={setOpenTool}
              />
            </Section>
          </>
        )}
        {!catalogErr && catalog.families.length === 0 && (
          // Loading = static content-shaped skeleton rows (no spinner, no text note).
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}
        {catalog.families.map((fam) => (
          <Section
            key={fam.family_id}
            title={fam.label}
            count={fam.capabilities.length}
            open={!!openFamilies[fam.family_id]}
            onToggle={() => toggleFamily(fam.family_id)}
          >
            <ToolsPanel
              tools={fam.capabilities}
              writeLocked={writeLocked}
              writeEntitled={canRunWrite}
              subtitle={fam.description}
              error={toolsErr}
              onRetry={retryTools}
              retryKey={rTarget === 'tools'}
              running={running || !!previewing}
              selectedTool={selectedTool}
              onRequestRun={onRequestCatalogRun}
              onOpenTool={setOpenTool}
              onReviseTool={fam.family_id === 'custom-authored' || fam.family_id === 'custom'
                ? onReviseAuthoredTool
                : undefined}
            />
          </Section>
        ))}
        <Section
          title="Author a tool"
          className="author-section"
          open={authorOpen}
          onToggle={() => setAuthorOpen((o) => !o)}
          innerRef={authorSectionRef}
        >
          <AuthorPanel
            onAuthor={onAuthor}
            onPublish={onPublishAuthor}
            onUseAuthored={onUseAuthored}
            seed={authorSeed}
            seedSignal={authorSignal}
            seedAutoSubmit={tourOn}
            targetToolName={authorTargetTool}
            onCancelRevision={onCancelAuthorRevision}
            stageActivity={authorStage}
            onResumeAuthor={authorStage.resume}
            notLinked={claudeNotLinked}
            onLinkClaude={() => setClaudeOpen(true)}
            buildEntitled={canBuild}
          />
        </Section>
      </aside>

      <div className="center-col">
        <main className="center-scroll">
        {/* the tour carries its own persistent banner — don't stack two */}
        {mock && !tourOn && <DemoBanner />}
        {/* There is a way back IN: leaving the tour (Skip / Exit) used to be
            one-way, with a hard reload the only re-entry — forbidden on stage. */}
        {mock && !tourOn && tourAvailable.current && (
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

        <ProductSurfaceTabs
          activeSurface={activeSurface}
          states={surfaceStates}
          onSelect={onSelectSurface}
          workspaceProject={workspaceProjectState}
          catalog={catalog}
        />
        {activeSurface !== 'cad' && (
          <ProductSurfaceFrame
            activeSurface={activeSurface}
            states={surfaceStates}
            catalog={catalog}
            catalogError={catalogErr}
            projectSlot={activeSurface === 'ios'
              ? <IosSurface enabled={ENV_IOS_SURFACE} contract={iosContract} />
              : <WorkspaceProjectSlot state={workspaceProjectState} onCreateProject={onCreateProject} />}
          />
        )}
        {/* The CAD workspace hides (not unmounts) on other tabs so live
            drawing, lock, and job state survive tab switches untouched.
            Solar shows it too: that tab IS the CAD workspace on the solar
            tool set, opened inline by the tab itself (no "Open ..." button;
            operator directive 2026-09-01). */}
        <div className="workspace-card enter" style={{ '--rank': 1, display: activeSurface === 'cad' || activeSurface === 'solar' ? undefined : 'none' }} ref={workspaceCardRef}>
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
          <div className="viewer-toolbar">
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
            <div className="viewer-actions">
              {/* Version-completed events surface as NT2 toasts; only the genuine
                  read-only-preview advisory keeps an amber note here. */}
              {!mock && previewing && (
                <span className="version-note readonly">viewing v{previewing.version} · read-only</span>
              )}
              {!mock && (
                <CheckoutControls
                  lockedByOther={otherHeldCheckout}
                  staleByOther={!!staleHeldCheckout}
                  legacyByOther={!!legacyHeldCheckout}
                  canTake={lock.canTake}
                  heldByUs={heldByUs}
                  unknown={checkoutUnknown}
                  readFailed={checkoutReadFailed}
                  busy={checkoutBusy}
                  onTake={onTakeCheckout}
                  onRelease={onReleaseCheckout}
                  onRetry={loadCheckout}
                />
              )}
              {drawingState && (
                <>
                  <button
                    className="btn ghost"
                    onClick={onUndo}
                    disabled={versionBusy || running || !!previewing || drawingMutationsBlocked || !canUndo}
                  >
                    Undo
                  </button>
                  <button
                    className="btn ghost"
                    onClick={onRedo}
                    disabled={versionBusy || running || !!previewing || drawingMutationsBlocked || !canRedo}
                  >
                    Redo
                  </button>
                  <div className="vh-anchor">
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
                        capability={capabilityRef.current}
                        onRestored={onRestoreCommitted}
                        headWarning={unreadableHead}
                        mutationBlocked={drawingMutationsBlocked}
                      />
                    )}
                  </div>
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
              <button className="btn ghost" onClick={() => viewerRef.current?.fit()}>Fit to bounds</button>
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
              // engine — license fence).
              notice={catalog?.cad_engine?.notice || ''}
              // F-3 persistence leg: live (non-mock) sessions can save
              // edited bytes as a NEW VERSION of the open drawing. The
              // parent is fetched FRESH at save time; the server's
              // compare-and-set still guards the race. Mock/demo stays
              // download-only, honestly.
              saveTarget={!mock && intake ? {
                drawingId: REQUESTED_DRAWING_ID,
                headVersion: null,
                save: async (bytes, _parent, digest) => {
                  const chain = await getDrawingVersions(false, REQUESTED_DRAWING_ID)
                  // The store publishes ONLY under a live single-writer
                  // checkout (the postgres authority fails closed without
                  // one — staging's exact first-save refusal). Use the
                  // session's held capability when there is one; otherwise
                  // take a checkout for exactly this save and release it
                  // after, the same acquire→save→release discipline the
                  // acceptance prover runs. A refused acquire surfaces the
                  // real holder instead of the store's opaque 400.
                  const held = capabilityRef.current
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
                    return await saveEditedDrawingVersion(
                      REQUESTED_DRAWING_ID, bytes, chain.head, digest, cap)
                  } finally {
                    if (acquired) {
                      releaseCheckout(REQUESTED_DRAWING_ID, cap).catch(() => {})
                    }
                  }
                },
              } : null}
              onSaved={(receipt) => {
                if (receipt?.new_version) {
                  completedVersionRef.current?.(receipt.new_version, { result: {} })
                }
              }}
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
            {intake && (
              <Suspense fallback={<ViewerSkeleton />}>
              <Viewer
                ref={viewerRef}
                intake={intake}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                highlightHandles={overlay?.highlight_handles}
                markers={overlay?.markers}
                overlayPolylines={overlay?.polylines}
                selectedHandle={selectedHandle}
                onSelectEntity={setSelectedHandle}
                pendingEdit={pendingEdit || writeGhost}
              />
              </Suspense>
            )}
            {shown && (
              <Legend
                layers={shown.layers}
                counts={layerCounts}
                colorForLayer={colorForLayer}
                visibleLayers={visibleLayers}
                onToggle={toggleLayer}
              />
            )}
            {intake && (
              <SelectionReadout selection={selection} onDeselect={() => setSelectedHandle(null)} />
            )}
          </div>
        </div>

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

        <EntitlementGate
          tier={entTier}
          entitlements={entitlements}
          loading={entLoading}
          mock={mock}
        />
        </main>

        <div className="bar-dock">
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
            // The registry supersedes the tools-only list when it loaded (its
            // entries carry `kind`, which is what groups the picker); a failed
            // fetch falls back to exactly today's list.
            tools={registryEntries.length ? registryEntries : slashTools}
            skills={catalogSkills}
            commandActions={slashCommandActions}
            sessionId={agentSessionId}
            imageAttachmentsEnabled={false}
          />
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

        <Toast toast={toast} onDone={onToastDone} />
      </div>

      <JobRail
        mock={mock}
        jobs={jobs}
        currentJob={currentJob}
        inflight={inflightPtr}
        reattaching={reattaching}
        onSelectJob={onSelectJob}
      />

      <footer className="foot-bar">
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
        <span style={{ marginLeft: 'auto' }}>build <span style={{ fontFamily: 'var(--font-mono)' }}>{__BUILD_HASH__}</span> · {mock ? 'sample data' : 'live'}</span>
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
  )
}
