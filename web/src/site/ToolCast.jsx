import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  getCapabilities,
  createOrg,
  createProject,
  getDrawingIntake,
  getJob,
  getSession,
  isWorkspaceBootstrapRequired,
  getTools,
  listProjects,
  nlPrompt,
  openProject,
  runToolAsync,
  publishStagedAuthor,
  recordToEnvelope,
  restoreDrawingVersion,
  runTool,
} from '../api.js'
import ConversePanel from '../components/ConversePanel.jsx'
import AuthorPanel from '../components/AuthorPanel.jsx'
import CapabilityCatalog from '../components/CapabilityCatalog.jsx'
import ClaudeAccountPanel from '../components/ClaudeAccountPanel.jsx'
import CheckoutControls from '../components/CheckoutControls.jsx'
import EntitlementGate from '../components/EntitlementGate.jsx'
import DetailsDrawer from '../components/DetailsDrawer.jsx'
import DrawingUploadControl from '../components/DrawingUploadControl.jsx'
import JobRail from '../components/JobRail.jsx'
import Legend from '../components/Legend.jsx'
import ProjectSwitcher from '../components/ProjectSwitcher.jsx'
import ProductSurfaceTabs, { ProductSurfaceFrame } from '../components/ProductSurfaceTabs.jsx'
import SelectionReadout from '../components/SelectionReadout.jsx'
import RoutePanel from '../components/RoutePanel.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import QuotaCard from '../components/QuotaCard.jsx'
import DegradedBanner from '../components/DegradedBanner.jsx'
import Toast from '../components/Toast.jsx'
import SessionGate from '../components/SessionGate.jsx'
import OpsDrawer from '../components/OpsDrawer.jsx'
import WorkspaceSummary from '../components/WorkspaceSummary.jsx'
import WorkspaceBootstrapGate from '../components/WorkspaceBootstrapGate.jsx'
import ProjectLifecyclePanel from '../projects/ProjectLifecyclePanel.jsx'
import { ENV_LIFECYCLE_UI } from '../projects/flag.js'
import CadEditSurface from '../cadedit/CadEditSurface.jsx'
import { ENV_CAD_EDIT } from '../cadedit/flag.js'
import IosSurface from '../ios/IosSurface.jsx'
import { ENV_IOS_SURFACE } from '../ios/flag.js'
import useIosSurface from '../ios/useIosSurface.js'
import { useWorkspaceControllers } from '../controllers/WorkspaceControllerProvider.jsx'
import useCatalogController from '../controllers/catalog/useCatalogController.js'
import { resolvePublishedCatalogTool } from './publishedCatalogTool.js'
import { track, setTourStep } from '../telemetry.js'
import useJobController from '../controllers/useJobController.js'
import useAuthorStageController from '../controllers/useAuthorStageController.js'
import usePlatformTrustController from '../controllers/platform/usePlatformTrustController.js'
import useWorkspaceController from '../controllers/workspace/useWorkspaceController.js'
import useSessionOrgAdoption from '../controllers/workspace/useSessionOrgAdoption.js'
import useCheckoutController from '../controllers/checkout/useCheckoutController.js'
import useDrawingUploadController from '../controllers/upload/useDrawingUploadController.js'
import useSessionController from '../controllers/session/useSessionController.js'
import { selectCurrentProjectName } from '../controllers/workspace/createWorkspaceController.js'
import { useDrawingScopeReset } from '../drawing/DrawingIdentityProvider.jsx'
import { matchPrompt } from '../mock/mockNlPrompt.js'
import {
  confirmRunIntent,
  createCatalogRunContext,
  createCatalogToolSnapshot,
  createRunIntentState,
  dismissRunIntent,
  mintCorrelationId,
  prepareCatalogRunParams,
  stageRunIntent,
} from '../runIntent.js'
import { navigate } from './router.js'
import { productSurfaceFromSearch, productSurfaceStates, searchForProductSurface } from './productSurfaces.js'
import {
  emptyIosShipReadiness,
  fetchIosShipReadiness,
  getIosShipExecution,
  getIosShipReceipt,
  iosShipLaunchAffordance,
  makeIosShipLaunchKey,
  requestIosShipLaunch,
} from './iosShipReadiness.js'
import { authConfigured, isSignedIn, login } from '../auth.js'
import { agentBannerFor as agentBannerForKind, OPERATOR_AGENT_BANNER_COPY } from '../lib/agentBanner.js'
import { claimHolderId, getSessionHolderId } from '../checkoutIdentity.js'
import DemoTour from '../demo/DemoTour.jsx'
import DemoConversationPanel, { demoReplyFor } from '../demo/DemoConversationPanel.jsx'
import FirstRunCoach from '../demo/FirstRunCoach.jsx'
import { shouldStartTour } from '../demo/tourEntry.js'
import * as mockVersions from '../mock/mockVersions.js'

const CAT_REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'
const PROOF_MODE =
  import.meta.env.VITE_CAT_PROOF === '1' ||
  new URLSearchParams(window.location.search).get('proof') === '1'
const DEMO_VALUE = new URLSearchParams(window.location.search).get('demo')
// A signed-in user gets the live session and mounted-account path on the same
// CAD surface. Only an anonymous demo remains fully local.
const PUBLIC_DEMO = DEMO_VALUE === '1' && !isSignedIn()
const LIVE_TOUR_REQUESTED = DEMO_VALUE === 'tour'
const LIVE_DEMO = LIVE_TOUR_REQUESTED || (DEMO_VALUE === '1' && isSignedIn())
const MODE_DRAWING_ID = PROOF_MODE
  ? 'cat-panels'
  : PUBLIC_DEMO
    ? 'demo'
    : LIVE_DEMO
      ? 'rooftop_demo'
      : null
const catalogServices = { getTools, getCapabilities, routePrompt: nlPrompt }
// Creation stays on POST /api/projects. The lifecycle factory
// (POST /api/projects/blank) goes through _get_lifecycle_actor, which REQUIRES
// X-Actor-Binding-Id whenever LEAF_AUTH_LIVE is off (the default, platform/
// deps.py auth_live). Nothing in the browser can produce that id, so routing
// creation there breaks project creation in every auth-off deployment, flag on
// or off. POST /api/projects now mints the creator's project membership from
// the VERIFIED subject (platform/store.py _grant_creator_project_membership),
// so a project created here under live auth is fully manageable by the person
// who created it. With auth off no identity is proven, no membership is
// invented, and the lifecycle panel below still reports the server's own 403 -
// a truthful read of the demo seam, not a client-side failure to try.
const workspaceServices = { createOrg, listProjects, createProject, openProject }
const UNIFIED_TOUR_STEPS = [
  {
    id: 'welcome',
    title: 'One operator scene',
    body: 'The drawing, request, approval, job, result, version history, and trust state stay together here.',
    target: '.stage-root',
  },
  {
    id: 'viewer',
    title: 'The resident drawing',
    body: 'This canvas stays mounted while the operator moves between tools and results. Pan, zoom, select, and Fit all act on this drawing.',
    target: '.stage-viewer',
  },
  {
    id: 'request',
    title: 'Ask Claude for the cat edit',
    body: 'The real command bar classifies this request, sends the same text and route hint to Claude, and waits for a proposal.',
    target: '.tc-bar',
    prompt: CAT_REQUEST,
    action: 'version',
  },
  {
    id: 'approval',
    title: 'Approval owns the write boundary',
    body: 'Review the proposed drawing.write action, then select Approve. The tour will wait for the new version.',
    target: '.converse-confirm, .tc-operator-rail',
    action: 'version',
  },
  {
    id: 'versions',
    title: 'The cat is a new version',
    body: 'The result, parent version, preview, Undo, and Redo remain available without leaving the scene.',
    target: '.tc-rail-r',
  },
  {
    id: 'trust',
    title: 'Operational trust stays visible',
    body: 'Backend health, usage, plan gates, platform identity, and the Claude grant stay separate and refreshable.',
    target: '.tc-rail-r',
  },
  {
    id: 'exit',
    title: 'Continue in the same scene',
    body: 'Exit the walkthrough and keep working with the cat version, job receipt, and complete operator surface still in place.',
    target: null,
    action: 'exit',
  },
]

function phaseLabel(phase) {
  if (phase === 'empty') return 'No drawing yet'
  if (phase === 'loading') return PROOF_MODE ? 'Loading drawing' : 'Connecting backend'
  if (phase === 'ready') return PROOF_MODE ? 'Drawing ready' : 'Backend ready'
  if (phase === 'starting') return 'Starting request'
  if (phase === 'proposal') return PROOF_MODE ? 'Waiting for approval' : 'Assistant response'
  if (phase === 'running') return 'Rearranging panels'
  if (phase === 'complete') return PROOF_MODE ? 'Cat version ready' : '3D cat ready'
  if (phase === 'tool-complete') return 'Tool run complete'
  if (phase === 'undone') return 'Original restored'
  if (phase === 'failed') return 'Request failed'
  return 'Workspace state unavailable'
}

function selectedEntity(intake, handle) {
  if (!handle) return null
  const polyline = intake?.polylines?.find((entity) => entity.handle === handle)
  if (polyline) return { handle, kind: 'polyline', layer: polyline.layer }
  const insert = intake?.inserts?.find((entity) => entity.handle === handle)
  if (insert) return { handle, kind: 'insert', layer: insert.layer, name: insert.name }
  const face = intake?.faces3d?.find((entity) => entity.handle === handle)
  if (face) return { handle, kind: '3dface', layer: face.layer }
  return null
}

function moveTab(event) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return
  const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')]
  if (!tabs.length) return
  const current = tabs.indexOf(document.activeElement)
  let next = current < 0 ? 0 : current
  if (event.key === 'ArrowRight') next = (next + 1) % tabs.length
  if (event.key === 'ArrowLeft') next = (next - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  event.preventDefault()
  tabs[next].focus()
  tabs[next].click()
}

// Whether this SPA page load ever saw an ACTIVE platform session. Lives at
// module scope so it survives ToolCast unmounts (route to /sheets or /app and
// back); see the first-run coach gate below.
let sessionWasActiveThisPageLoad = false

export default function ToolCast({
  active,
  drawingId,
  onDrawingReady,
  onFitDrawing,
  onViewModeChange,
  onVisibleLayersChange,
  selectedHandle,
  onSelectedHandleChange,
  onResultOverlayChange,
}) {
  const [prompt, setPrompt] = useState(PROOF_MODE ? CAT_REQUEST : '')
  const [activeSurface, setActiveSurface] = useState(() => productSurfaceFromSearch(window.location.search))
  const {
    converse,
    bindConverseProject,
    drawing,
    drawingEvent,
    drawingError,
    instanceId,
  } = useWorkspaceControllers()
  const {
    sessionId,
    turns,
    activeRequests,
    requestStatus,
    startTurn,
    clear: clearConverse,
    resetCached,
  } = converse
  const startTurnRef = useRef(startTurn)
  startTurnRef.current = startTurn
  const platformSession = useSessionController()
  const sessionAuthRequired = platformSession.status === 'required'
  const sessionReady = PUBLIC_DEMO || platformSession.status === 'active'
  const transportMock = PUBLIC_DEMO || !sessionReady
  const sessionReadyRef = useRef(sessionReady)
  sessionReadyRef.current = sessionReady
  // The first-run coach is only for visitors who were never signed in during
  // this page view. An active session that later expires flips status back
  // to 'required' (any subscribed 401 does it), and a first-run hint
  // surfacing mid-session for a returning user is noise, not coaching.
  // Module-scoped (not a ref): navigating through /sheets or /app unmounts
  // ToolCast, and a remount within the same SPA page load must not forget
  // that the visitor was signed in.
  useEffect(() => {
    if (platformSession.status === 'active') sessionWasActiveThisPageLoad = true
  }, [platformSession.status])
  useEffect(() => {
    const restoreSurface = () => setActiveSurface(productSurfaceFromSearch(window.location.search))
    window.addEventListener('popstate', restoreSurface)
    return () => window.removeEventListener('popstate', restoreSurface)
  }, [])
  const requireAuth = platformSession.actions.requireAuth
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState(null)
  const [linkedJobId, setLinkedJobId] = useState(null)
  const [tenantId, setTenantId] = useState('try-surface')
  const [checkoutHolder, setCheckoutHolder] = useState(() => getSessionHolderId())
  const [guestDrawing, setGuestDrawing] = useState(null)
  const [sessionTier, setSessionTier] = useState(null)
  const [sessionOrg, setSessionOrg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [leftView, setLeftView] = useState('operator')
  const [rightView, setRightView] = useState('execution')
  const [selectedCatalogTool, setSelectedCatalogTool] = useState(null)
  const [authorSeed, setAuthorSeed] = useState('')
  const [authorSeedSignal, setAuthorSeedSignal] = useState(0)
  const [authorTargetTool, setAuthorTargetTool] = useState(null)
  const [claudeOpen, setClaudeOpen] = useState(false)
  const [workspaceBootstrapRequired, setWorkspaceBootstrapRequired] = useState(false)
  const [sessionRetry, setSessionRetry] = useState(0)
  const [toast, setToast] = useState(null)
  const [drawer, setDrawer] = useState(null)
  const [uploadDragActive, setUploadDragActive] = useState(false)
  const [confirmingRecovery, setConfirmingRecovery] = useState(null)
  const [recoveringVersion, setRecoveringVersion] = useState(null)
  const [recoveryError, setRecoveryError] = useState(null)
  const [opsOpen, setOpsOpen] = useState(() => !PUBLIC_DEMO && new URLSearchParams(window.location.search).get('ops') === '1')
  const [quotaAt, setQuotaAt] = useState(0)
  const [tourOn, setTourOn] = useState(false)
  const [tourIndex, setTourIndex] = useState(0)
  const [demoTurns, setDemoTurns] = useState([])
  const [focusView, setFocusView] = useState(false)
  const toastSeqRef = useRef(0)
  const accountSessionObservedRef = useRef(false)
  const tourSeqRef = useRef(0)
  const demoTurnSeqRef = useRef(0)
  const catalogDecisionRef = useRef(null)
  const openedCatalogKeyRef = useRef('')
  const authorPendingRef = useRef(false)
  const scopedDrawingIdRef = useRef(null)
  const activeDrawingIdRef = useRef(null)
  const catalogRunScopeRef = useRef(0)
  const resetJobRef = useRef(() => {})
  const runIntentSessionRef = useRef(null)
  if (!runIntentSessionRef.current) {
    runIntentSessionRef.current = `try-${mintCorrelationId()}`
  }
  const runIntentStateRef = useRef(null)
  const lastConfirmedRunRef = useRef(null)
  if (!runIntentStateRef.current) {
    runIntentStateRef.current = createRunIntentState(runIntentSessionRef.current)
  }
  useEffect(() => {
    const claim = claimHolderId({ id: checkoutHolder, onRemint: setCheckoutHolder })
    return () => claim.stop()
    // Claim once per runtime. A remint must not restart the claim loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const catalogAdapters = useMemo(() => ({
    previewRoute: matchPrompt,
    commitDecision: (decision) => catalogDecisionRef.current?.(decision),
    dismissDecision: () => {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    },
    onAuthRequired: () => requireAuth('catalog'),
    openAuthor: (text) => {
      if (!authorPendingRef.current) setAuthorTargetTool(null)
      setAuthorSeed(text || '')
      setAuthorSeedSignal((current) => current + 1)
      setLeftView('author')
    },
    startAgentTurn: async (text, hint) => {
      if (!sessionReadyRef.current) return undefined
      const response = await startTurnRef.current(text, hint)
      setLeftView('operator')
      setPhase('proposal')
      return response
    },
    agentBannerFor: (error) => agentBannerForKind(error, { copy: OPERATOR_AGENT_BANNER_COPY }),
  }), [requireAuth])

  useEffect(() => {
    if (!drawingEvent) return
    setPhase(drawingEvent.event === 'undo' ? 'undone' : 'complete')
  }, [drawingEvent])

  // P2 wave C-2: the /try?demo=tour walkthrough is its OWN tour surface
  // (review #428 round-1 blocker 3) and reports the same tour funnel as the
  // app tour: started once per activation, step 0 as reached, tour_step
  // context on every organic event while active.
  const tourStartedRef = useRef(false)
  useEffect(() => {
    if (LIVE_TOUR_REQUESTED && platformSession.status === 'active') {
      if (!tourStartedRef.current) {
        tourStartedRef.current = true
        setTourStep(UNIFIED_TOUR_STEPS[0]?.id)
        track('tour.started', { entry: 'deeplink' })
        track('tour.step_reached', { step_id: UNIFIED_TOUR_STEPS[0]?.id })
      }
      setTourOn(true)
    }
  }, [platformSession.status])

  useEffect(() => {
    if (!drawingError) return
    const { error: cause, context } = drawingError
    if (context?.operation === 'undo') setError('Undo failed. The current drawing version is unchanged.')
    else if (context?.operation === 'redo') setError('Redo failed. The current drawing version is unchanged.')
    else setError(String(cause?.message || cause))
  }, [drawingError])
  const {
    seatIntake,
    seatVersion,
    undo: undoDrawingVersion,
    redo: redoDrawingVersion,
  } = drawing.actions
  const version = drawing.head
  const hasDrawing = Boolean(drawing.shown && drawing.drawingState?.drawing_id)
  const canOperate = sessionReady && hasDrawing
  const { canUndo, canRedo } = drawing
  const panelCount = drawing.shown?.polylines?.length || null
  const selection = useMemo(() => selectedEntity(drawing.shown, selectedHandle), [drawing.shown, selectedHandle])
  const sculpture = Number(drawing.previewing?.version ?? version) > 1 && phase !== 'undone'
  const layerCounts = useMemo(() => {
    const counts = {}
    for (const layer of drawing.shown?.layers || []) counts[layer] = 0
    for (const entity of [...(drawing.shown?.polylines || []), ...(drawing.shown?.inserts || []), ...(drawing.shown?.faces3d || [])]) {
      counts[entity.layer] = (counts[entity.layer] || 0) + 1
    }
    return counts
  }, [drawing.shown])

  useEffect(() => {
    onVisibleLayersChange?.(drawing.visibleLayers)
  }, [drawing.visibleLayers, onVisibleLayersChange])

  useEffect(() => {
    onViewModeChange?.(sculpture ? 'panel-sculpture' : 'flat')
    if (!sculpture) setFocusView(false)
  }, [onViewModeChange, sculpture])

  const toggleLayer = useCallback((layer) => {
    const next = { ...drawing.visibleLayers, [layer]: drawing.visibleLayers[layer] === false }
    drawing.actions.setVisibleLayers(next)
    if (selection?.layer === layer && next[layer] === false) onSelectedHandleChange?.(null)
  }, [drawing.actions, drawing.visibleLayers, onSelectedHandleChange, selection])

  useEffect(() => {
    if (!active) return undefined
    const requestedDrawingId = MODE_DRAWING_ID || drawingId
    const drawingSource = PROOF_MODE ? 'cat' : PUBLIC_DEMO ? 'rooftop_demo' : requestedDrawingId
    if (drawing.shown && drawing.drawingState?.drawing_id === requestedDrawingId) {
      return undefined
    }
    if (!drawingSource) {
      setWorkspaceBootstrapRequired(false)
      setError(null)
      setPhase('empty')
      if (!isSignedIn()) {
        requireAuth('/api/session')
        return undefined
      }
      // A signed-in user with no drawing yet still needs to learn whether they
      // own a tenant workspace: without this probe the bootstrap-required 403
      // (only surfaced by /api/session) never fires on the empty landing, the
      // "Create your Leaf workspace" gate never renders, and their first upload
      // 403s with no path forward (found by the 2026-08-17 staging walk). The
      // probe is authority-only: it seats no intake and leaves phase 'empty'.
      let probeLive = true
      getSession(false, 'rooftop_demo')
        .then((data) => {
          if (!probeLive) return
          setWorkspaceBootstrapRequired(false)
          // A 200 here IS the platform session: the probe proves the verified
          // subject owns a tenant workspace. Without activating, a signed-in
          // visitor who has not uploaded a drawing yet sat at status 'checking'
          // forever, so sessionReady stayed false and the Trust/Jobs tabs
          // rendered permanently disabled against a working API.
          accountSessionObservedRef.current = true
          setSessionTier(data?.tier || null)
          setSessionOrg(data?.org || null)
          platformSession.actions.activate(data)
        })
        .catch((cause) => {
          if (!probeLive) return
          if (cause?.status === 401) { requireAuth('/api/session'); return }
          if (isWorkspaceBootstrapRequired(cause)) setWorkspaceBootstrapRequired(true)
        })
      return () => { probeLive = false }
    }
    let live = true
    platformSession.actions.checking()
    setWorkspaceBootstrapRequired(false)
    setPhase('loading')
    getSession(PUBLIC_DEMO, drawingSource)
      .then((data) => {
        if (!live) return
        if (PUBLIC_DEMO) mockVersions.seedBase(data.intake)
        seatIntake(data.intake, {
          drawingId: requestedDrawingId,
          drawingState: { drawing_id: requestedDrawingId, version: 1, head: 1, latest: 1 },
          apply: true,
        })
        setTenantId(data.tenant || 'try-surface')
        setGuestDrawing(null)
        setSessionTier(data.tier || null)
        setSessionOrg(data.org || null)
        accountSessionObservedRef.current = true
        platformSession.actions.activate(data)
        setPhase('ready')
      })
      .catch((cause) => {
        if (!live) return
        if (cause?.status === 401) {
          requireAuth('/api/session')
          setError(null)
          setPhase('failed')
          return
        }
        if (isWorkspaceBootstrapRequired(cause)) {
          setWorkspaceBootstrapRequired(true)
          setError(null)
          setPhase('failed')
          return
        }
        setError('The drawing backend is unavailable. Start the proof API and reload this surface.')
        setPhase('failed')
      })
    return () => { live = false }
    // `platformSession.recoveries` is the bounded re-entry: the controller
    // re-opens `checking` at most MAX_TOKEN_RECOVERIES times, and only when a
    // token appears that is not the one the refusal latched on. That bump is
    // what re-runs getSession after a post-callback 401 — nothing else in this
    // dep list changes when localStorage gains a token.
  }, [active, drawing.drawingState?.drawing_id, drawing.head, drawing.shown, drawingId, platformSession.actions, platformSession.recoveries, requireAuth, seatIntake, sessionRetry])

  const showToast = useCallback((next) => {
    toastSeqRef.current += 1
    setToast({ id: toastSeqRef.current, ...next })
  }, [])

  const onCompleteVersion = useCallback(async (newVersion, envelope) => {
    const scopeAtStart = activeDrawingIdRef.current
    const drawingId = newVersion?.drawing_id || scopeAtStart || 'cat-panels'
    if (scopeAtStart && drawingId !== scopeAtStart) return
    try {
      if (PUBLIC_DEMO) {
        if (!mockVersions.isSeeded() && drawing.shown) mockVersions.seedBase(drawing.shown)
        mockVersions.applyDelete(envelope?.result?.removed)
      }
      if (envelope?.result?.new_version_readable === false) {
        if (activeDrawingIdRef.current !== scopeAtStart) return
        drawing.actions.recordCommittedUnreadableHead(newVersion)
        showToast({ text: `Version ${newVersion?.version || 'created'} created` })
        return
      }
      const view = await getDrawingIntake(PUBLIC_DEMO, drawingId, 'head')
      if (activeDrawingIdRef.current !== scopeAtStart) return
      seatVersion(view, { drawingId, source: 'job', event: 'complete' })
    } catch {
      if (activeDrawingIdRef.current !== scopeAtStart) return
      drawing.actions.markRefreshFailure({ drawing_id: drawingId, version: newVersion?.version })
      showToast({ text: `Version ${newVersion?.version || 'created'} created` })
    }
  }, [drawing.actions, drawing.shown, seatVersion, showToast])

  const onJobNotice = useCallback(({ text }) => {
    showToast({ text, action: { label: 'View', onClick: () => setRightView('execution') } })
  }, [showToast])

  const onUploadReady = useCallback(async ({ receipt, view }) => {
    const previousDrawingId = activeDrawingIdRef.current
    if (previousDrawingId && previousDrawingId !== receipt.drawing_id) {
      activeDrawingIdRef.current = receipt.drawing_id
      catalogRunScopeRef.current += 1
      setPrompt('')
      setSelectedCatalogTool(null)
      setLinkedJobId(null)
      setBusy(false)
      resetJobRef.current({ clearPointer: true })
      lastConfirmedRunRef.current = null
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
      clearConverse()
      resetCached()
    }
    seatVersion(view, { drawingId: receipt.drawing_id, source: 'upload', event: 'upload' })
    onDrawingReady?.(receipt)
    setTenantId(receipt.tenant_id || tenantId)
    setGuestDrawing(receipt.tenant_kind === 'guest' ? {
      drawingId: receipt.drawing_id,
      retentionExpiresAt: receipt.retention_expires_at || null,
    } : null)
    setPhase('ready')
    setError(null)
    if (receipt.tenant_kind === 'account') platformSession.actions.activate(receipt)
    showToast({ text: `Drawing ready, ${view?.intake?.dwg || receipt.drawing_id}`, action: { label: 'View', onClick: () => setRightView('view') } })
  }, [clearConverse, onDrawingReady, platformSession.actions, resetCached, seatVersion, showToast, tenantId])

  const drawingUpload = useDrawingUploadController({ onReady: onUploadReady })

  useEffect(() => {
    // Preserve the public policy request during the initial signed-out boot,
    // but invalidate an account upload if an active session expires mid-flight.
    if (!sessionAuthRequired || !accountSessionObservedRef.current) return
    accountSessionObservedRef.current = false
    drawingUpload.actions.cancel()
  }, [drawingUpload.actions, sessionAuthRequired])

  const {
    jobs,
    currentJob,
    currentJobId,
    inflight,
    reattaching,
    running: jobRunning,
    result: jobResult,
    error: jobError,
    status: jobStatus,
    progress: jobProgress,
    elapsedMs: jobElapsedMs,
    runJob: runTrackedJob,
    attachJob: attachTrackedJob,
    detachJob,
    reset: resetJob,
    adoptEnvelope,
  } = useJobController({
    mock: transportMock,
    onCompleteVersion,
    onNotice: onJobNotice,
    onAuthRequired: (required) => { if (required) requireAuth('jobs') },
    formatError: () => 'The panel run did not produce a readable result.',
  })
  resetJobRef.current = resetJob

  // Turn-authority provider for the author panel's stage POST: the server
  // fail-closes (409 stage_authority_invalid) without X-Authority-Session-Id/
  // -Turn-Id naming an ACTIVE turn owned by this subject, so mint one from the
  // converse session right before staging and reuse it briefly. The response's
  // own session_id is authoritative; the state-fed sessionId can lag a render.
  // A failed mint returns null and the server still answers with its own
  // refusal — never a client-side one.
  // Wide margin under the server's TURN_MAX_S default of 300s.
  const AUTHOR_AUTHORITY_TTL_MS = 120_000
  const authorAuthorityRef = useRef(null) // { sessionId, turnId, mintedAt }
  const authorAuthorityProvider = useCallback(async (description) => {
    const cached = authorAuthorityRef.current
    if (cached && Date.now() - cached.mintedAt < AUTHOR_AUTHORITY_TTL_MS) {
      return { sessionId: cached.sessionId, turnId: cached.turnId }
    }
    try {
      const response = await startTurnRef.current(description, { source: 'author_panel', purpose: 'stage_authority' })
      const mintedSession = response?.session_id
      if (!mintedSession || !response?.turn_id) return null
      authorAuthorityRef.current = { sessionId: mintedSession, turnId: response.turn_id, mintedAt: Date.now() }
      return { sessionId: mintedSession, turnId: response.turn_id }
    } catch {
      return null
    }
  }, [])

  const authorStage = useAuthorStageController({
    mock: PUBLIC_DEMO,
    enabled: sessionReady,
    authorityProvider: authorAuthorityProvider,
  })
  authorPendingRef.current = !!authorStage.pointer

  useEffect(() => {
    const pending = authorStage.pointer
    if (!pending || !sessionReady) return
    setAuthorTargetTool(pending.target_tool_name || null)
    setAuthorSeed(pending.description || '')
    setAuthorSeedSignal((current) => current + 1)
    setLeftView('author')
  }, [authorStage.pointer?.idempotency_key, sessionReady])

  useEffect(() => {
    onResultOverlayChange?.(drawing.overlayStale ? null : (jobResult?.overlay || null))
  }, [drawing.overlayStale, jobResult, onResultOverlayChange])
  const visibleJobCount = useMemo(() => {
    if (!currentJob) return jobs.length
    return jobs.some((job) => job.job_id === currentJob.job_id) ? jobs.length : jobs.length + 1
  }, [currentJob, jobs])
  const platform = usePlatformTrustController({
    mock: transportMock,
    quotaResult: jobResult,
    quotaAt,
    onAuthRequired: (required, sources) => {
      if (required) requireAuth(`platform:${(sources || []).join(',') || 'unknown'}`)
    },
  })
  useEffect(() => {
    const quota = jobResult?.ok === false && (
      jobResult.quota_kind === 'daily_runs' || jobResult.error?.error_code === 'quota_exceeded'
    )
    setQuotaAt(quota ? Date.now() : 0)
  }, [jobResult])
  const writeEntitled = platform.isEntitled('run_write')
  const checkout = useCheckoutController({
    mock: transportMock,
    drawingId: drawing.drawingState?.drawing_id || null,
    holder: checkoutHolder,
  })
  // Previewing an older version is a READ-ONLY view: the stage and the version
  // panel show v{n} while the head sits elsewhere, so a write tool would commit
  // against a drawing the operator is not looking at. /app's VersionHistory has
  // always locked writes during preview; /try's port carried the preview NOTE
  // and the active-row highlight but not the lock, so the acceptance driver had
  // to drop its read-only assertion (PR #409).
  // `previewing` is non-null only for a NON-head version — previewVersion()
  // clears it on its `isHead` branch (useDrawingVersionController.js) — so
  // "Back to head" is what releases this lock.
  const previewLocked = drawing.previewing != null
  const writeLocked = checkout.writeLocked || drawing.mutationsBlocked || previewLocked
  // Why writes are paused, in the surface's own voice. Ordered most- to
  // least-specific; preview sits last because it is the one the operator can
  // clear themselves, and an unreadable head or another session's checkout is
  // the more actionable thing to say when both are true.
  const writeLockNote = !writeLocked
    ? null
    : drawing.mutationsBlocked
      ? 'the committed drawing head is unreadable; editing is locked until it is readable or you recover a historical version.'
      : checkout.lockedByOther?.holder
        ? `editing is locked by ${checkout.lockedByOther.holder}; this write tool is paused.`
        : previewLocked
          ? `you are viewing v${drawing.previewing.version} read-only — choose “Back to head” in Version history to edit again.`
          : 'editing is paused while Leaf checks the drawing lock.'
  const takeCheckout = useCallback((...args) => {
    if (!sessionReady) return undefined
    return checkout.actions.take(...args)
  }, [checkout.actions, sessionReady])
  const releaseCheckout = useCallback((...args) => {
    if (!sessionReady) return undefined
    return checkout.actions.release(...args)
  }, [checkout.actions, sessionReady])
  const signInWithCheckoutRelease = useCallback(async () => {
    if (checkout.actions.getCapability()) await checkout.actions.release()
    await login()
  }, [checkout.actions])
  const workspace = useWorkspaceController({ mock: transportMock, services: workspaceServices })
  // Scope-reset contract (docs/convergence/ACCEPTANCE.md, binding): document
  // identity persists across interactions, so it must NOT survive a project
  // switch or close — no drawing from the previous project's scope may still
  // be addressed under the next one. Opening the FIRST project of a session
  // is not a switch and keeps the boot seed.
  useDrawingScopeReset(workspace.openProjectId)
  // Live sessions echo the verified subject's org — persist it (leaf.org_id)
  // so a fresh browser lists its projects instead of the create-org bootstrap.
  useSessionOrgAdoption(sessionOrg, workspace.adoptOrgId)
  useEffect(() => {
    bindConverseProject(workspace.openProjectId || null)
  }, [bindConverseProject, workspace.openProjectId])
  const currentProjectName = selectCurrentProjectName(workspace)
  const activeDrawingId = drawing.drawingState?.drawing_id || null
  const catalogRunContext = useMemo(() => createCatalogRunContext({
    tenantId,
    orgId: workspace.orgId,
    projectId: workspace.openProjectId || null,
    workspace: workspace.workspace,
    selectedVersionId: workspace.canonicalVersionId,
    drawingState: drawing.drawingState,
    fallbackDrawingId: activeDrawingId,
  }), [activeDrawingId, drawing.drawingState, tenantId, workspace.canonicalVersionId, workspace.openProjectId, workspace.orgId, workspace.workspace])

  const catalog = useCatalogController({
    services: catalogServices,
    adapters: catalogAdapters,
    context: {
      mock: transportMock,
      entitlements: null,
      running: busy || jobRunning,
      agentDisabled: transportMock || !platform.isEntitled('converse'),
    },
  })

  useLayoutEffect(() => {
    if (!activeDrawingId) return
    const previousDrawingId = scopedDrawingIdRef.current
    scopedDrawingIdRef.current = activeDrawingId
    activeDrawingIdRef.current = activeDrawingId
    if (!previousDrawingId || previousDrawingId === activeDrawingId) return

    catalogRunScopeRef.current += 1
    setPrompt('')
    setSelectedCatalogTool(null)
    setLinkedJobId(null)
    setBusy(false)
    resetJob({ clearPointer: true })
    lastConfirmedRunRef.current = null
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    catalog.actions.dismissRoute()
    clearConverse()
    resetCached()
  }, [activeDrawingId, catalog.actions, clearConverse, resetCached, resetJob])

  useEffect(() => {
    if (!sessionAuthRequired) return
    setLeftView('operator')
    setRightView('execution')
    setError(null)
    setPhase('failed')
    runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    catalog.actions.dismissRoute()
    clearConverse()
    resetCached()
  }, [catalog.actions, clearConverse, resetCached, sessionAuthRequired])
  const {
    tools,
    toolsError,
    catalog: capabilityCatalog,
    catalogError,
    openFamilies,
    route,
    routing,
    routeError,
    agentBanner,
  } = catalog.state
  useEffect(() => {
    if (capabilityCatalog.families.length === 0) return
    const catalogKey = `${capabilityCatalog.source || 'unknown'}:${capabilityCatalog.families.map((family) => family.family_id).join(',')}`
    if (openedCatalogKeyRef.current === catalogKey) return
    openedCatalogKeyRef.current = catalogKey
    for (const family of capabilityCatalog.families) {
      catalog.actions.setFamilyOpen(family.family_id, true)
    }
  }, [capabilityCatalog.families, catalog.actions])
  const armCatalogDecision = useCallback((decision) => {
    if (decision?.lane !== 'run') return decision
    const refreshedTool = decision.refreshedTool?.name === decision.tool
      ? decision.refreshedTool
      : null
    const tool = refreshedTool || tools.find((candidate) => candidate.name === decision.tool)
    if (!tool) return decision
    if ((tool.capabilities || []).includes('drawing.write') && writeLocked) {
      setError(drawing.mutationsBlocked
        ? 'Editing is locked until the committed drawing head becomes readable or you recover a historical version.'
        : checkout.lockedByOther?.holder
        ? `Editing is locked by ${checkout.lockedByOther.holder}. Read tools still run.`
        : previewLocked
        ? `Editing is paused while you view v${drawing.previewing.version} read-only. Choose “Back to head” in Version history to edit again. Read tools still run.`
        : 'Editing is paused while Leaf checks the drawing lock. Read tools still run.')
      return undefined
    }
    const context = catalogRunContext
    if (!context) {
      setError('This project needs a canonical drawing version before a tool can run.')
      return undefined
    }
    const id = mintCorrelationId()
    const effectiveParams = prepareCatalogRunParams(tool, decision.params, context)
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `try-intent-${id}`,
      toolName: tool.name,
      params: effectiveParams,
      context,
      toolSnapshot: createCatalogToolSnapshot(tool),
    })
    runIntentStateRef.current = staged.state
    const { refreshedTool: _refreshedTool, ...publicDecision } = decision
    return { ...publicDecision, params: staged.intent.params, runIntent: staged.intent }
  }, [catalogRunContext, checkout.lockedByOther, drawing.mutationsBlocked, drawing.previewing, previewLocked, tools, writeLocked])
  catalogDecisionRef.current = armCatalogDecision

  const requestCatalogRun = useCallback((tool, params) => {
    if (!canOperate) return
    setSelectedCatalogTool(tool)
    setLeftView('catalog')
    catalog.actions.commitDecision({
      lane: 'run',
      tool: tool.name,
      params,
      confidence: 1,
      rationale: 'Catalog selection. Confirm the exact tool and parameters before it runs.',
      alternatives: [],
      // P2: provenance excludes this arm from route.outcome (catalog is not
      // the prompt funnel) and attributes run.confirm_shown honestly.
      source: 'catalog',
    })
  }, [canOperate, catalog.actions])

  const runCatalogTool = useCallback(async (intent, tool, params) => {
    if (!canOperate || !tool || busy || jobRunning) {
      catalog.actions.dismissRoute()
      return
    }
    if ((tool.capabilities || []).includes('drawing.write') && writeLocked) {
      setError(drawing.mutationsBlocked
        ? 'Editing is locked until the committed drawing head becomes readable or you recover a historical version. The write did not run.'
        : checkout.lockedByOther?.holder
        ? `Editing is locked by ${checkout.lockedByOther.holder}. The write did not run.`
        : previewLocked
        ? `Editing is paused while you view v${drawing.previewing.version} read-only. Choose “Back to head” in Version history to edit again. The write did not run.`
        : 'Editing is paused while Leaf checks the drawing lock. The write did not run.')
      catalog.actions.dismissRoute()
      return
    }
    const confirmed = confirmRunIntent(runIntentStateRef.current, {
      intentId: intent?.intentId,
      sessionId: intent?.sessionId,
      toolName: tool.name,
      params,
      context: catalogRunContext,
      toolSnapshot: createCatalogToolSnapshot(tool),
    })
    runIntentStateRef.current = confirmed.state
    if (!confirmed.ok) {
      setError('That run confirmation is no longer valid. Review the tool again.')
      catalog.actions.dismissRoute()
      return
    }
    lastConfirmedRunRef.current = {
      tool,
      params: { ...confirmed.execution.params },
    }
    setSelectedCatalogTool(tool)
    const runScope = ++catalogRunScopeRef.current
    const runDrawingId = confirmed.execution.context.drawingArtifactId
      || confirmed.execution.context.drawingId
    setBusy(true)
    setError(null)
    setPhase('running')
    setRightView('jobs')
    const envelope = await runTrackedJob({
      toolName: tool.name,
      execute: ({ onSubmit, onStatus }) => PUBLIC_DEMO
        ? runTool(true, tool, confirmed.execution.params, drawing.shown, confirmed.execution.context.drawingId)
        : runToolAsync(
          tool,
          confirmed.execution.params,
          confirmed.execution.context.drawingId,
          {
          orgId: confirmed.execution.context.orgId || undefined,
          projectId: confirmed.execution.context.projectId || undefined,
          idempotencyKey: confirmed.execution.intentId,
          catalogDigest: confirmed.execution.toolSnapshot.catalogDigest || undefined,
          dwgVersion: confirmed.execution.context.drawingVersion ?? undefined,
          checkoutCapability: checkout.actions.getCapability() || undefined,
          onSubmit,
          onStatus,
          },
        ),
    })
    if (
      catalogRunScopeRef.current !== runScope ||
      activeDrawingIdRef.current !== runDrawingId
    ) return
    if (!PUBLIC_DEMO) await workspace.rehydrate()
    if (envelope?.ok) setPhase(envelope.result?.new_version ? 'complete' : 'tool-complete')
    else if (envelope) {
      setPhase('failed')
      setError(null)
    } else {
      setPhase('failed')
      setError('The catalog run did not produce a readable result.')
    }
    setBusy(false)
    catalog.actions.dismissRoute()
    if (!PUBLIC_DEMO) checkout.actions.refresh()
  }, [busy, canOperate, catalog.actions, catalogRunContext, checkout.actions, checkout.lockedByOther, drawing.mutationsBlocked, drawing.previewing, drawing.shown, jobRunning, previewLocked, runTrackedJob, workspace, writeLocked])

  const recoverHistoricalVersion = useCallback(async (versionToRecover) => {
    const drawingId = drawing.drawingState?.drawing_id
    const currentHead = Number(drawing.head)
    const target = Number(versionToRecover)
    if (
      !sessionReady || drawingId == null || !drawing.unreadableHead ||
      drawing.unreadableHead.pending || recoveringVersion != null ||
      !Number.isFinite(target) || target === currentHead
    ) return
    setRecoveringVersion(target)
    setRecoveryError(null)
    try {
      const result = await restoreDrawingVersion(
        PUBLIC_DEMO,
        drawingId,
        target,
        checkout.actions.getCapability() || undefined,
      )
      setConfirmingRecovery(null)
      await drawing.actions.recordRestore(result)
      await drawing.actions.loadHistory()
    } catch (cause) {
      setRecoveryError(cause?.message || `Could not recover from v${target}.`)
    } finally {
      setRecoveringVersion(null)
    }
  }, [checkout.actions, drawing.actions, drawing.drawingState?.drawing_id, drawing.head, drawing.unreadableHead, recoveringVersion, sessionReady])

  const retryCatalogRun = useCallback(() => {
    const last = lastConfirmedRunRef.current
    if (!last || busy || jobRunning) return
    requestCatalogRun(last.tool, { ...last.params })
  }, [busy, jobRunning, requestCatalogRun])

  const openWorkspaceProject = useCallback(async (projectId) => {
    if (!sessionReady) return
    const opened = await workspace.openProject(projectId)
    const canonical = opened?.drawing_versions?.[0]?.version_id || null
    workspace.selectCanonicalVersion(canonical)
    setLeftView('workspace')
  }, [sessionReady, workspace])

  const createWorkspaceOrg = useCallback(async (name) => {
    if ((!sessionReady && !workspaceBootstrapRequired) || name == null) return
    const org = await workspace.createOrg(name)
    if (org && workspaceBootstrapRequired) {
      setWorkspaceBootstrapRequired(false)
      setSessionRetry((current) => current + 1)
    }
    return org
  }, [sessionReady, workspace, workspaceBootstrapRequired])

  const createWorkspaceProject = useCallback(async (name) => {
    if (!sessionReady) return
    if (name == null || !name.trim()) return
    const project = await workspace.createProject(name)
    if (project) setLeftView('workspace')
  }, [sessionReady, workspace])

  // A deleted project cannot be rehydrated: drop the open workspace and leave
  // the Project tab rather than let the rail render a project the server no
  // longer has. The delete receipt outlives the panel here, because the panel
  // it would have been rendered in is unmounting with the project.
  const forgetDeletedProject = useCallback((projectId, receiptId) => {
    bindConverseProject(null)
    workspace.closeProject()
    setLeftView('operator')
    showToast({ text: receiptId ? `Project deleted. Receipt ${receiptId}` : 'Project deleted.' })
  }, [bindConverseProject, showToast, workspace])

  const authorTool = useCallback((description, targetToolName = null) => {
    if (!sessionReady) return undefined
    return authorStage.stage(description, targetToolName)
  }, [authorStage.stage, sessionReady])

  const reviseAuthoredTool = useCallback((tool) => {
    if (!tool?.name || authorStage.pointer) return
    setAuthorTargetTool(tool.name)
    setAuthorSeed('')
    setAuthorSeedSignal((current) => current + 1)
    setLeftView('author')
  }, [authorStage.pointer])

  const cancelAuthorRevision = useCallback(() => {
    if (authorStage.pointer) return
    setAuthorTargetTool(null)
    setAuthorSeed('')
    setAuthorSeedSignal((current) => current + 1)
  }, [authorStage.pointer])

  const publishAuthoredTool = useCallback(async (staged) => {
    if (!sessionReady) return undefined
    const published = await publishStagedAuthor(PUBLIC_DEMO, staged)
    const tool = published.tool || staged.tool
    if (published.published) {
      catalog.actions.upsertTool(tool)
      await catalog.actions.loadCatalog()
      const message = `Tool published, ${tool.name}`
      showToast({ text: message, action: { label: 'View', onClick: () => setLeftView('author') } })
      authorStage.completePublication()
    }
    return { ...published, tool }
  }, [authorStage.completePublication, catalog.actions, sessionReady, showToast])

  const useAuthoredTool = useCallback(async (tool) => {
    if (!sessionReady || !tool) return
    const refreshedTools = await catalog.actions.loadTools()
    let runnableTool
    try {
      runnableTool = resolvePublishedCatalogTool(tool, refreshedTools)
    } catch (cause) {
      setError(cause?.message || 'The published tool is not ready to run yet.')
      return
    }
    setSelectedCatalogTool(runnableTool)
    catalog.actions.commitDecision({
      lane: 'run',
      tool: runnableTool.name,
      params: {},
      confidence: 0.99,
      rationale: `Authored just now. Confirm to run ${runnableTool.name}.`,
      alternatives: [],
      refreshedTool: runnableTool,
      // P2: provenance (see requestCatalogRun above); 'authored' is in the
      // run.confirm_shown source vocabulary already.
      source: 'authored',
    })
  }, [catalog.actions, sessionReady])

  const onJobLinked = useCallback((nextJobId) => {
    if (!nextJobId) return
    setLinkedJobId(nextJobId)
    setPhase('running')
    setError(null)
  }, [])

  const attachJob = useCallback(async (nextJobId, toolName) => {
    if (!sessionReady || !nextJobId) return
    onJobLinked(nextJobId)
    const envelope = await attachTrackedJob(nextJobId, {
      toolName: toolName || 'arrange-panels-as-cat',
      persist: true,
    })
    if (envelope && !envelope.ok) {
      setPhase('failed')
      setError(null)
    } else if (!envelope) {
      setPhase('failed')
      setError('The panel run did not produce a readable drawing version.')
    }
    await workspace.rehydrate()
    checkout.actions.refresh()
  }, [attachTrackedJob, checkout.actions, onJobLinked, sessionReady, workspace])

  const openResultDetails = useCallback((envelope = jobResult, jobId = currentJobId) => {
    if (!envelope) return
    const cost = envelope.cost?.usd_est
    setDrawer({
      title: 'Run details',
      rows: [
        `job ${jobId || 'not recorded'}`,
        `tool ${envelope.tool || 'unknown'}`,
        `version ${envelope.version || 'unknown'}`,
        `timing ${envelope.timing_ms ?? 'unknown'} ms`,
        `cost ${Number(cost) > 0 ? `$${Number(cost).toFixed(4)}` : 'no cloud cost'}`,
        `degraded ${envelope.degraded_mode ? 'yes, local fallback' : 'no'}`,
        ...(envelope.error ? [
          `error code ${envelope.error.error_code || 'unknown'}`,
          `error message ${envelope.error.message || 'failed'}`,
          `retryable ${envelope.error.retryable ? 'yes' : 'no'}`,
        ] : []),
      ],
      foot: 'This receipt belongs to one immutable tool run.',
    })
  }, [currentJobId, jobResult])

  const inspectHistoricalJob = useCallback(async (job) => {
    if (!sessionReady || !job?.job_id) return
    try {
      const record = await getJob(job.job_id)
      const envelope = recordToEnvelope(record)
      setDrawer({
        title: 'Job details',
        rows: [
          `job ${job.job_id}`,
          `tool ${envelope.tool || job.tool || 'unknown'}`,
          `status ${record.status || 'unknown'}`,
          `timing ${envelope.timing_ms ?? 'unknown'} ms`,
        ],
        action: {
          label: 'Show in result pane',
          onClick: () => {
            adoptEnvelope(envelope, { jobId: job.job_id, toolName: envelope.tool || job.tool })
            setRightView('execution')
            setDrawer(null)
          },
        },
        foot: 'Selecting details does not rerun the tool.',
      })
    } catch {
      setError('The stored job details could not be loaded.')
    }
  }, [adoptEnvelope, sessionReady])

  useEffect(() => {
    if (!drawer) return undefined
    const closeOnEscape = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setDrawer(null)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [drawer])

  useEffect(() => {
    if (!opsOpen) return undefined
    const closeOpsOnEscape = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpsOpen(false)
    }
    window.addEventListener('keydown', closeOpsOnEscape)
    return () => window.removeEventListener('keydown', closeOpsOnEscape)
  }, [opsOpen])

  useEffect(() => {
    if (!route) return undefined
    const dismissProposalOnEscape = (event) => {
      if (event.key !== 'Escape') return
      if (document.querySelector('.drawer-layer .drawer, .claude-pop, .proj-menu')) return
      event.preventDefault()
      event.stopImmediatePropagation()
      catalog.actions.dismissRoute()
      requestAnimationFrame(() => document.querySelector('.tc-bar-input')?.focus())
    }
    window.addEventListener('keydown', dismissProposalOnEscape, true)
    return () => window.removeEventListener('keydown', dismissProposalOnEscape, true)
  }, [catalog.actions, route])

  useEffect(() => {
    if (!jobRunning) return undefined
    const detachOnEscape = (event) => {
      if (event.key !== 'Escape') return
      if (route || drawer || opsOpen || document.querySelector('.claude-pop, .proj-menu')) return
      event.preventDefault()
      event.stopImmediatePropagation()
      const toolName = currentJob?.tool || selectedCatalogTool?.name || 'job'
      detachJob()
      showToast({ text: `Detached from ${toolName}. The job keeps running in Jobs.` })
    }
    window.addEventListener('keydown', detachOnEscape)
    return () => window.removeEventListener('keydown', detachOnEscape)
  }, [currentJob?.tool, detachJob, drawer, jobRunning, opsOpen, route, selectedCatalogTool?.name, showToast])

  const changePrompt = useCallback((value) => {
    setPrompt(value)
    catalog.actions.setPrompt(value)
  }, [catalog.actions])

  const dispatchRequest = useCallback(async (override) => {
    const text = (typeof override === 'string' ? override : prompt).trim()
    if (!text || platformSession.status !== 'active' || !hasDrawing || busy || jobRunning || routing) return
    setError(null)
    setPhase('starting')
    if (text.startsWith('/')) setLeftView('catalog')
    const decision = await catalog.actions.dispatch(text)
    if (PUBLIC_DEMO) {
      const id = `demo-turn-${++demoTurnSeqRef.current}`
      setDemoTurns((current) => [...current, { id, text, reply: demoReplyFor(text, decision) }])
      setPrompt('')
      const actionable = decision?.lane !== 'run' || (decision?.tool && Number(decision.confidence) >= 0.7)
      if (!actionable) {
        catalog.actions.dismissRoute()
        setLeftView('operator')
        setPhase('ready')
        return decision
      }
      if (decision?.lane === 'run' || decision?.lane === 'solve') setLeftView('operator')
    }
    if (decision) setPhase('proposal')
    else setPhase('failed')
    return decision
  }, [busy, catalog.actions, hasDrawing, jobRunning, platformSession.status, prompt, routing])

  const runRequest = useCallback(() => dispatchRequest(), [dispatchRequest])

  const runTourPrompt = useCallback(async (text) => {
    const seq = ++tourSeqRef.current
    changePrompt('')
    for (let index = 16; index < text.length + 16; index += 16) {
      if (tourSeqRef.current !== seq) return
      changePrompt(text.slice(0, Math.min(index, text.length)))
      await new Promise((resolve) => setTimeout(resolve, 55))
    }
    if (tourSeqRef.current === seq) await dispatchRequest(text)
  }, [changePrompt, dispatchRequest])

  const moveTour = useCallback((next) => {
    setTourIndex(next)
    const step = UNIFIED_TOUR_STEPS[next]
    setTourStep(step?.id)
    track('tour.step_reached', { step_id: step?.id })
    if (step?.id === 'approval') setLeftView('operator')
    if (step?.id === 'versions') {
      setRightView('versions')
      drawing.actions.loadHistory()
    }
    if (step?.id === 'trust') {
      setRightView('trust')
      platform.actions.refreshAll()
    }
  }, [drawing.actions, platform.actions])

  const exitTour = useCallback(() => {
    track('tour.exited', {
      at_step: UNIFIED_TOUR_STEPS[tourIndex]?.id,
      completed: tourIndex >= UNIFIED_TOUR_STEPS.length - 1,
    })
    setTourStep(null)
    tourSeqRef.current += 1
    setTourOn(false)
  }, [tourIndex])

  const undo = useCallback(async () => {
    if (!sessionReady || busy || jobRunning || !canUndo) return
    setError(null)
    await undoDrawingVersion(checkout.actions.getCapability())
  }, [busy, canUndo, checkout.actions, jobRunning, sessionReady, undoDrawingVersion])

  const redo = useCallback(async () => {
    if (!sessionReady || busy || jobRunning || !canRedo) return
    setError(null)
    await redoDrawingVersion(checkout.actions.getCapability())
  }, [busy, canRedo, checkout.actions, jobRunning, redoDrawingVersion, sessionReady])

  const runOnEnter = (event) => {
    if (event.key === 'Enter') runRequest()
  }

  // Wave D one-shot iOS ship lane. The surface consumes REAL readiness: the
  // projection module fails closed to `launchable === false` for missing,
  // invalid, stale, cross-tenant, unhealthy, or secret-shaped records, so a
  // launch affordance can never be derived from anything the browser invents.
  // Only the signed-in project context asks for readiness; otherwise the lane
  // resets to the frozen empty shape.
  const [iosShip, setIosShip] = useState(() => emptyIosShipReadiness())
  const [iosShipBusy, setIosShipBusy] = useState(false)
  const [iosShipError, setIosShipError] = useState(null)
  const [iosShipExecution, setIosShipExecution] = useState(null)
  const [iosShipReceipt, setIosShipReceipt] = useState(null)
  useEffect(() => {
    const projectId = workspace.openProjectId
    const revision = workspace.canonicalVersionId
    const signedIn = platformSession.status === 'active'
    setIosShip(emptyIosShipReadiness('loading', null, projectId || null))
    setIosShipExecution(null)
    setIosShipReceipt(null)
    if (activeSurface !== 'ios' || !signedIn || !projectId || !revision) {
      setIosShip(emptyIosShipReadiness('no_approved_project_revision', null, projectId || null))
      return undefined
    }
    let live = true
    setIosShipError(null)
    fetchIosShipReadiness({ projectId, revision }).then((next) => {
      if (!live) return
      setIosShip(next)
      if (!next.launchable) setIosShipError(next.setupAction || next.reason || null)
    })
    return () => { live = false }
  }, [activeSurface, platformSession.status, workspace.canonicalVersionId, workspace.openProjectId])
  // Consume-only iOS readiness (ios_surface, cards D-1..D-4): one live read of
  // GET /api/ios-surface/status for the open project/revision, distinct from the
  // ios_ship LAUNCH lane above (this one only ever reads). Inert unless the
  // build flag is on AND an operator workspace with an open project is showing;
  // the backend refuses (404) while LEAF_IOS_SURFACE_ENABLED is off regardless.
  const iosSurface = useIosSurface(
    workspace.openProjectId,
    workspace.canonicalVersionId,
    {
      enabled: ENV_IOS_SURFACE && leftView === 'workspace'
        && !PUBLIC_DEMO && !transportMock && canOperate,
    },
  )
  const launchIosShip = useCallback(async () => {
    const projectId = workspace.openProjectId
    const revision = workspace.canonicalVersionId
    if (!iosShipLaunchAffordance(iosShip, {
      projectId, revision, sessionActive: platformSession.status === 'active',
    }) || iosShipBusy) return
    setIosShipBusy(true)
    setIosShipError(null)
    try {
      // One reviewed idempotent launch: identifiers only. The backend owns
      // the approved-revision gate; this browser never fabricates an approval
      // and never sends credential material.
      const response = await requestIosShipLaunch({
        projectId,
        approvedLaunch: iosShip.approvedLaunch,
        idempotencyKey: makeIosShipLaunchKey(projectId, iosShip.approvedLaunch),
      })
      setIosShipExecution(response.execution)
      showToast({ text: 'iOS ship launch accepted. Track it in the lane.', action: { label: 'View', onClick: () => setRightView('execution') } })
    } catch (cause) {
      setIosShipError(cause?.envelope?.message || cause?.message || 'The iOS ship launch was refused.')
    } finally {
      setIosShipBusy(false)
    }
  }, [iosShip, iosShipBusy, platformSession.status, setRightView, showToast, workspace.canonicalVersionId, workspace.openProjectId])

  useEffect(() => {
    const projectId = workspace.openProjectId
    const executionId = iosShipExecution?.execution_id
    if (activeSurface !== 'ios' || !projectId || !executionId) return undefined
    let live = true
    let timer = null
    const poll = async () => {
      try {
        const response = await getIosShipExecution({ projectId, executionId })
        if (!live) return
        const next = response.execution
        setIosShipExecution(next)
        if (next?.receipt_id) {
          const receiptResponse = await getIosShipReceipt({ projectId, receiptId: next.receipt_id })
          if (live) setIosShipReceipt(receiptResponse.receipt)
          return
        }
        if (!['succeeded', 'failed'].includes(next?.status)) timer = setTimeout(poll, 2000)
      } catch (cause) {
        if (live) setIosShipError(cause?.message || 'The iOS ship status is unavailable.')
      }
    }
    if (iosShipExecution.receipt_id) {
      poll()
    } else if (!['succeeded', 'failed'].includes(iosShipExecution.status)) {
      timer = setTimeout(poll, 2000)
    }
    return () => { live = false; if (timer) clearTimeout(timer) }
  }, [activeSurface, iosShipExecution?.execution_id, iosShipExecution?.receipt_id,
    iosShipExecution?.status, workspace.openProjectId])

  const statusClass = phase === 'failed' ? 'red' : (phase === 'proposal' || phase === 'empty' ? 'hollow' : 'live')
  const productStates = productSurfaceStates({
    sessionActive: platformSession.status === 'active',
    hasDrawing,
    apsLive: platform.health?.aps_live,
    iosReady: iosShip.launchable === true,
  })
  const selectProductSurface = useCallback((surfaceId) => {
    const search = searchForProductSurface(window.location.search, surfaceId)
    window.history.pushState({}, '', `${window.location.pathname}${search}${window.location.hash}`)
    setActiveSurface(surfaceId)
  }, [])
  const projectSlot = (
    <ProjectSwitcher
      mock={transportMock}
      projectName={activeDrawingId}
      orgId={workspace.orgId}
      projects={workspace.projects}
      openProjectId={workspace.openProjectId}
      currentName={currentProjectName}
      unavailable={workspace.projectsError}
      loading={workspace.projectsLoading}
      orgBusy={workspace.orgBusy}
      projectBusy={workspace.projectBusy}
      onCreateOrg={createWorkspaceOrg}
      onCreateProject={createWorkspaceProject}
      onOpenProject={openWorkspaceProject}
    />
  )

  return (
    <>
      <ProductSurfaceTabs activeSurface={activeSurface} states={productStates} onSelect={selectProductSurface} catalog={capabilityCatalog} />
      {activeSurface === 'cad' ? (
      <>
      <div className="tc-topcluster tc-topcluster-product" data-cast="tool" style={{ '--rank': 3 }}>
        {projectSlot}
        <span className="tc-solve" data-testid="operator-phase">
          <span className={`dot ${statusClass}${phase === 'running' ? ' pulse' : ''}`} />
          {phaseLabel(phase)}
        </span>
        <span className="tc-version" data-testid="version-head">{version == null ? 'No version' : `Version ${version}`}</span>
        {sculpture && (
          <button
            type="button"
            className="tc-back"
            data-testid="focus-3d"
            onClick={() => setFocusView((current) => !current)}
          >
            {focusView ? 'Show controls' : 'Focus 3D'}
          </button>
        )}
        <button type="button" className="tc-back" onClick={() => navigate('/')}>Back to the site</button>
        {LIVE_TOUR_REQUESTED && sessionReady && !tourOn && shouldStartTour(window.location.search) && (
          <button
            type="button"
            className="tc-back"
            onClick={() => {
              setTourStep(UNIFIED_TOUR_STEPS[0]?.id)
              track('tour.started', { entry: 'button' })
              track('tour.step_reached', { step_id: UNIFIED_TOUR_STEPS[0]?.id })
              setTourIndex(0); setTourOn(true)
            }}
          >Restart walk</button>
        )}
        <span className="key">Esc</span>
      </div>

      <aside className={`tc-rail tc-rail-l tc-operator-rail${focusView ? ' tc-focus-hidden' : ''}`} aria-label="Workspace controls" data-cast="tool" data-controller-instance={instanceId} style={{ '--rank': 0 }} data-testid="operator-surface">
        <div className="tc-rail-head">
          <span className="tc-rail-title">Workspace</span>
          <span className="tc-rail-sub">request and tools</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Workspace panels" onKeyDown={moveTab}>
          <button id="workspace-tab-operator" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'operator' ? 0 : -1} aria-selected={leftView === 'operator'} onClick={() => setLeftView('operator')}>Operator</button>
          <button id="workspace-tab-catalog" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'catalog' ? 0 : -1} aria-selected={leftView === 'catalog'} disabled={!canOperate} onClick={() => setLeftView('catalog')}>Catalog <span>{tools.length}</span></button>
          <button id="workspace-tab-author" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'author' ? 0 : -1} aria-selected={leftView === 'author'} disabled={!canOperate} onClick={() => setLeftView('author')}>Author</button>
          <button id="workspace-tab-workspace" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'workspace' ? 0 : -1} aria-selected={leftView === 'workspace'} disabled={!canOperate} onClick={() => setLeftView('workspace')}>Project</button>
        </div>
        <div id="workspace-tabpanel" className="tc-rail-body" role="tabpanel" aria-labelledby={`workspace-tab-${leftView}`} tabIndex={0}>
          {leftView === 'operator' && (PUBLIC_DEMO ? (
            <DemoConversationPanel turns={demoTurns} onSuggestion={dispatchRequest} canSignIn={authConfigured} onSignIn={signInWithCheckoutRelease} />
          ) : workspaceBootstrapRequired ? (
            <WorkspaceBootstrapGate
              busy={workspace.orgBusy}
              error={workspace.projectsError}
              onCreate={createWorkspaceOrg}
            />
          ) : sessionAuthRequired ? (
            <>
              {guestDrawing && (
                <div className="tc-panel-note" role="status" data-testid="guest-view-only">
                  <strong>Guest drawing ready.</strong> Inspect this drawing here. Sign in to load tools or run actions. Guest uploads are view-only.
                </div>
              )}
              <SessionGate
                configured={authConfigured}
                onSignIn={signInWithCheckoutRelease}
                onDemo={() => { window.location.href = '/try?demo=1' }}
              />
            </>
          ) : sessionId ? (
            <ConversePanel
              sessionId={sessionId}
              userTurns={turns}
              onDismiss={() => {}}
              onLinkClaude={() => { setRightView('trust'); setClaudeOpen(true) }}
              onAttachJob={attachJob}
              onJobLinked={attachJob}
              writeLocked={writeLocked}
            />
          ) : (
            <div className="tc-operator-empty">
              <span className={`dot ${phase === 'loading' ? 'live pulse' : 'hollow'}`} />
              <span>{phase === 'loading'
                ? 'Loading the drawing backend'
                : phase === 'empty'
                  ? 'No drawing yet. Upload a DWG or DXF to begin.'
                  : 'Type the request in the command bar. The proposal will stay in this rail.'}</span>
            </div>
          ))}
          {leftView === 'catalog' && (
            <CapabilityCatalog
              catalog={capabilityCatalog}
              catalogError={catalogError}
              openFamilies={openFamilies}
              onToggleFamily={catalog.actions.toggleFamily}
              onRetryCatalog={catalog.actions.loadCatalog}
              tools={tools}
              toolsError={toolsError}
              running={busy || jobRunning}
              selectedTool={selectedCatalogTool}
              onRequestRun={requestCatalogRun}
              onOpenTool={setSelectedCatalogTool}
              onReviseTool={reviseAuthoredTool}
              onRetryTools={catalog.actions.retryTools}
              writeEntitled={writeEntitled}
              writeLocked={writeLocked}
              writeLockNote={writeLockNote}
            />
          )}
          {leftView === 'workspace' && (
            workspace.workspace ? (
              <>
                <div className="tc-panel-note" data-testid="project-conversation-activity">
                  Conversation work: {activeRequests.queued} queued, {activeRequests.executing} running
                  {requestStatus ? ` (${requestStatus})` : ''}.
                </div>
                <WorkspaceSummary
                  workspace={workspace.workspace}
                  loading={workspace.workspaceLoading}
                  selectedVersionId={workspace.canonicalVersionId}
                  onSelectVersion={workspace.selectCanonicalVersion}
                  onClose={() => {
                    bindConverseProject(null)
                    workspace.closeProject()
                    setLeftView('operator')
                  }}
                />
              </>
            ) : (
              <div className="tc-panel-note">Choose a project from the header to load its drawing versions, jobs, and built tools.</div>
            )
          )}
          {/* Project lifecycle (cards B-U2..B-U6). ENV_LIFECYCLE_UI is the FIRST
              operand on purpose: it is a build-time literal, so with the flag off
              Rollup folds this whole expression away and the panel never reaches
              the bundle (web/src/projects/bundleFence.test.js is the oracle).
              !PUBLIC_DEMO is stated even though !transportMock already implies it
              — the public /try?demo=1 page must stay fenced off even if the
              transportMock definition ever changes. canOperate is the same gate
              the Project tab button itself uses for operator affordances. */}
          {ENV_LIFECYCLE_UI && leftView === 'workspace'
            && !PUBLIC_DEMO && !transportMock && canOperate && workspace.openProjectId && (
            <ProjectLifecyclePanel
              projectId={workspace.openProjectId}
              projectName={currentProjectName}
              onProjectDeleted={forgetDeletedProject}
            />
          )}
          {/* Browser CAD editing surface, behind cad_edit. Same shape as the
              lifecycle fence above and for the same reason: ENV_CAD_EDIT is
              the FIRST operand, so a flag-off build folds the whole
              expression away and neither the surface nor the DXF engine it
              pulls in reaches the bundle (web/src/cadedit/bundleFence.test.js
              is the oracle). !PUBLIC_DEMO and !transportMock keep it off the
              public /try demo and off any mock-transport session; canOperate
              and an open project are the same operator gates the lifecycle
              panel uses. */}
          {ENV_CAD_EDIT && leftView === 'workspace'
            && !PUBLIC_DEMO && !transportMock && canOperate && workspace.openProjectId && (
            <CadEditSurface />
          )}
          {/* Consume-only iOS readiness (cards D-1..D-4). Rendered on the
              workspace context (NOT flag-first, unlike the lifecycle fence
              above) so that with the flag off it shows the DORMANT placeholder
              the envelope's negative control requires; enabled={ENV_IOS_SURFACE}
              drives dormant-vs-live, and the backend route refuses (404) while
              off. Passive read only — the ios_ship LAUNCH lane is separate. */}
          {leftView === 'workspace'
            && !PUBLIC_DEMO && !transportMock && canOperate && workspace.openProjectId && (
            <IosSurface enabled={ENV_IOS_SURFACE} contract={iosSurface.contract} />
          )}
          {leftView === 'author' && (
            <AuthorPanel
              onAuthor={authorTool}
              onPublish={publishAuthoredTool}
              onUseAuthored={useAuthoredTool}
              notLinked={platform.grant?.linked === false}
              onLinkClaude={() => setRightView('trust')}
              buildEntitled={platform.isEntitled('build')}
              seed={authorSeed}
              seedSignal={authorSeedSignal}
              targetToolName={authorTargetTool}
              onCancelRevision={cancelAuthorRevision}
              stageActivity={authorStage}
              onResumeAuthor={authorStage.resume}
            />
          )}
          {(error || jobError) && <div className="tc-operator-error" role="alert"><span className="dot red" />{error || jobError}</div>}
          {leftView === 'operator' && (
            <DrawingUploadControl
              policy={drawingUpload.policy}
              policyLoading={drawingUpload.policyLoading}
              busy={drawingUpload.busy}
              phase={drawingUpload.phase}
              error={drawingUpload.error}
              engine={drawingUpload.engine}
              onEngineChange={drawingUpload.actions.setEngine}
              onUpload={drawingUpload.actions.upload}
              onCancel={drawingUpload.actions.cancel}
            />
          )}
        </div>
        <div className="tc-rail-foot">
          <span className="tc-link">{leftView === 'operator' ? 'Drawing operator' : leftView === 'catalog' ? 'Registered catalog' : leftView === 'author' ? 'Tool authoring' : 'Project workspace'}</span>
          <span className="tc-link muted">{leftView === 'operator' ? (PUBLIC_DEMO ? 'Interactive demo' : PROOF_MODE ? 'Deterministic proof' : 'Live services') : leftView === 'catalog' ? `${tools.length} tools` : leftView === 'author' ? 'Stage, review, publish' : currentProjectName || 'No project open'}</span>
        </div>
      </aside>

      <aside className={`tc-rail tc-rail-r${focusView ? ' tc-focus-hidden' : ''}`} aria-label="Operations controls" data-cast="tool" style={{ '--rank': 1 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Operations</span>
          <span className="tc-rail-sub">controller state</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Operation panels" onKeyDown={moveTab}>
          <button id="operations-tab-execution" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'execution' ? 0 : -1} aria-selected={rightView === 'execution'} onClick={() => setRightView('execution')}>Execution</button>
          <button id="operations-tab-jobs" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'jobs' ? 0 : -1} aria-selected={rightView === 'jobs'} disabled={!sessionReady} onClick={() => setRightView('jobs')}>Jobs <span>{visibleJobCount}</span></button>
          <button id="operations-tab-versions" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'versions' ? 0 : -1} aria-selected={rightView === 'versions'} disabled={!canOperate} onClick={() => { setRightView('versions'); drawing.actions.loadHistory() }}>Versions <span>{drawing.latest ?? 0}</span></button>
          <button id="operations-tab-trust" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'trust' ? 0 : -1} aria-selected={rightView === 'trust'} disabled={!sessionReady} onClick={() => { setRightView('trust'); platform.actions.refreshAll() }}>Trust</button>
          <button id="operations-tab-view" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'view' ? 0 : -1} aria-selected={rightView === 'view'} disabled={!hasDrawing} onClick={() => setRightView('view')}>View</button>
        </div>
        <div id="operations-tabpanel" className="tc-rail-body" role="tabpanel" aria-labelledby={`operations-tab-${rightView}`} tabIndex={0}>
        {rightView === 'execution' && <><div className="tc-events">
          <div className="tc-event current">
            <span className={`dot ${statusClass}${phase === 'running' ? ' pulse' : ''}`} />
            <span className="tc-event-text hot">{phaseLabel(phase)}</span>
            <span className="tc-event-time">now</span>
          </div>
          <div className="tc-event">
            <span className="dot" />
            <span className="tc-event-text">Panels preserved</span>
            <span className="tc-event-time">{panelCount?.toLocaleString('en-US') || 'pending'}</span>
          </div>
          <div className="tc-event">
            <span className={(currentJobId || linkedJobId) ? 'dot' : 'dot hollow'} />
            <span className="tc-event-text">Tool job</span>
            <span className="tc-event-time">{(currentJobId || linkedJobId) ? (currentJobId || linkedJobId).slice(0, 8) : 'pending'}</span>
          </div>
          <div className="tc-event">
            <span className={canUndo ? 'dot' : 'dot hollow'} />
            <span className="tc-event-text">Version head</span>
            <span className="tc-event-time">v{version}</span>
          </div>
        </div>
        <div className="tc-version-actions">
          <button type="button" className="tc-bar-chip" onClick={undo} disabled={busy || jobRunning || drawing.versionBusy || !canUndo}>Undo</button>
          <button type="button" className="tc-bar-chip" onClick={redo} disabled={busy || jobRunning || drawing.versionBusy || !canRedo}>Redo</button>
          <CheckoutControls
            lockedByOther={checkout.lockedByOther}
            staleByOther={checkout.staleByOther}
            legacyByOther={checkout.legacyByOther}
            canTake={checkout.canTake}
            heldByUs={checkout.heldByUs}
            unknown={checkout.unknown}
            readFailed={checkout.readFailed}
            busy={checkout.busy}
            disabled={!sessionReady || !activeDrawingId}
            onTake={takeCheckout}
            onRelease={releaseCheckout}
            onRetry={checkout.actions.refresh}
          />
        </div>
        {drawing.unreadableHead && (
          <div
            className="inline-error"
            role="alert"
            data-testid="unreadable-head-lock"
            data-head={drawing.unreadableHead.head}
            data-latest={drawing.unreadableHead.latest}
          >
            {drawing.unreadableHead.message}
            {!drawing.unreadableHead.pending ? (
              <button type="button" className="chip-act" onClick={drawing.actions.retryUnreadableHead} disabled={drawing.refreshing}>
                {drawing.refreshing ? 'Reading the version chain…' : 'Retry loading'}
              </button>
            ) : null}
          </div>
        )}
        {drawing.refreshFailure && (
          <div className="inline-error tc-refresh-failure" role="alert">
            Couldn’t refresh the viewer. The previous version is still shown.
            <button type="button" className="chip-act" onClick={drawing.actions.retryRefresh} disabled={drawing.refreshing}>
              {drawing.refreshing ? 'Refreshing…' : 'Retry'}
            </button>
          </div>
        )}
        <div className="tc-rail-note">
          <span>The request, approval, job, drawing, and version history remain in this scene.</span>
        </div>
        {jobRunning && (
          <div className="tc-running" role="status">
            <span className="dot live pulse" />
            <span>{jobProgress || jobStatus || 'Running tool'}</span>
            {jobElapsedMs != null && <b>{(jobElapsedMs / 1000).toFixed(1)}s</b>}
          </div>
        )}
        <div data-testid="catalog-run-result">
          {platform.quota.visible && (
            <QuotaCard
              kind={platform.quota.visible.kind}
              message={platform.quota.visible.error?.message}
              remaining={platform.usage?.cap?.remaining}
              tier={jobResult?.tier || platform.entitlements?.tier}
              limit={platform.quota.visible.limit}
              used={platform.quota.visible.used}
              onAction={() => setRightView('trust')}
            />
          )}
          {jobResult?.degraded_mode && (
            <DegradedBanner source="toolcast" reason={jobResult.degraded_reason || jobResult.result?.degraded_reason || jobResult.result?.reason} />
          )}
          <ResultPanel
            running={jobRunning}
            error={jobError}
            result={jobResult}
            tool={selectedCatalogTool}
            onRetry={lastConfirmedRunRef.current ? retryCatalogRun : undefined}
          />
          {jobResult && (
            <div className="tc-result-details">
              <button type="button" className="chip-act" onClick={() => openResultDetails()}>Details</button>
            </div>
          )}
        </div></>}
        {rightView === 'jobs' && (
          <JobRail
            mock={transportMock}
            jobs={jobs}
            currentJob={currentJob}
            inflight={inflight}
            reattaching={reattaching}
            onSelectJob={inspectHistoricalJob}
          />
        )}
        {rightView === 'versions' && (
          <div className="tc-version-panel" role="region" aria-label="Version history">
            <div className="tc-panel-heading">
              <span>Version history</span>
              <button type="button" onClick={drawing.actions.loadHistory}>Refresh</button>
            </div>
            {drawing.previewing && (
              <>
                <div className="tc-preview-note">
                  Viewing v{drawing.previewing.version} read-only
                  <button type="button" onClick={drawing.actions.backToHead}>Back to head</button>
                </div>
                {/* The lock is real (writeLocked, :532) — say so here, where the
                    operator already is and where the control that lifts it sits.
                    A SIBLING, not a child: .tc-preview-note is a two-item
                    space-between flex row, and a third child would spread across
                    it. Keeping the note's DOM intact also keeps the acceptance
                    driver's `getByText(/Viewing v1 read-only/)` a single match. */}
                <div className="tc-preview-lock" role="status" data-testid="try-preview-write-lock">
                  Editing is paused until you return to head.
                </div>
              </>
            )}
            {drawing.historyLoading && <div className="tc-panel-note">Loading versions</div>}
            {drawing.historyError && <div className="tc-panel-error">{drawing.historyError}</div>}
            {recoveryError && <div className="tc-panel-error" role="alert">{recoveryError}</div>}
            <div className="tc-version-list">
              {[...(drawing.history?.versions || [])].reverse().map((item) => {
                const isCurrentHead = Number(item.v) === Number(drawing.head)
                const recoveryMode = Boolean(drawing.unreadableHead && !isCurrentHead)
                const recoveryDisabled = Boolean(drawing.unreadableHead?.pending || recoveringVersion != null)
                const confirming = confirmingRecovery === item.v
                return (
                  <div className="tc-version-row" key={item.v} data-testid={`try-version-v${item.v}`}>
                    <button
                      type="button"
                      className={drawing.previewing?.version === item.v ? 'active' : ''}
                      onClick={() => drawing.actions.previewVersion(item.v)}
                    >
                      <span>v{item.v}</span>
                      <span>{item.tool || 'drawing'}</span>
                      {isCurrentHead ? <b>head</b> : null}
                    </button>
                    {recoveryMode ? (
                      confirming ? (
                        <span className="tc-version-recovery">
                          <button
                            type="button"
                            className="chip-act"
                            disabled={recoveryDisabled}
                            onClick={() => recoverHistoricalVersion(item.v)}
                          >
                            {recoveringVersion === item.v ? 'Recovering…' : `Recover from v${item.v}`}
                          </button>
                          <button type="button" className="chip-neutral" disabled={recoveringVersion != null} onClick={() => setConfirmingRecovery(null)}>Cancel</button>
                        </span>
                      ) : (
                        <button
                          type="button"
                          className="chip-act"
                          disabled={recoveryDisabled}
                          onClick={() => { setRecoveryError(null); setConfirmingRecovery(item.v) }}
                        >
                          Recover
                        </button>
                      )
                    ) : null}
                  </div>
                )
              })}
            </div>
          </div>
        )}
        {rightView === 'trust' && (
          <div className="tc-trust-panel">
            <div className="tc-panel-heading">
              <span>Service trust</span>
              <button type="button" onClick={platform.actions.refreshAll} disabled={platform.healthLoading || platform.usageLoading || platform.entLoading || platform.grantLoading}>
                {platform.healthLoading ? 'Refreshing' : 'Refresh'}
              </button>
            </div>
            {platform.healthStatus.degraded && (
              <div className="banner" role="status">
                <b>Backend degraded</b>
                <span className="banner-rest"> {platform.health?.aps_live === false ? 'Cloud execution is unavailable.' : 'The backend health check reports degraded operation.'} Read the result notice before relying on fallback output.</span>
                <span className="banner-since">clears after a healthy refresh</span>
              </div>
            )}
            {!platform.healthLoading && !platform.health && (
              <div className="tc-panel-note" role="status">Health details are unavailable. Existing results remain visible.</div>
            )}
            <div className="tc-trust-row"><span>Backend</span><b>{platform.healthStatus.status}</b></div>
            <div className="tc-trust-row"><span>Claude account</span><b>{platform.grant?.linked ? `linked${platform.grant.kind ? ` · ${platform.grant.kind}` : ''}` : 'not linked'}</b></div>
            {/* The usage LEDGER (showcase #2): /api/usage has always returned
                today/total/cap - the UI rendered one number and dropped the
                rest. Every row below is a field the endpoint already ships;
                absent fields render 'unknown', never a fabricated zero. The
                cap bar is a REAL percentage (house rule: width = real % only).
                The agent-turn sub-ledger lands with the W3 agent-trace work. */}
            {platform.usage ? (
              <div data-testid="usage-ledger">
                <div className="tc-trust-row"><span>Runs today</span><b>{platform.usage.today?.runs ?? 'unknown'}</b></div>
                <div className="tc-trust-row"><span>Spend today</span><b>{typeof platform.usage.today?.usd_est === 'number' ? `$${platform.usage.today.usd_est.toFixed(3)}` : 'unknown'}</b></div>
                <div className="tc-trust-row"><span>Runs total</span><b>{typeof platform.usage.total?.runs === 'number' ? platform.usage.total.runs.toLocaleString() : 'unknown'}</b></div>
                <div className="tc-trust-row"><span>Spend total</span><b>{typeof platform.usage.total?.usd_est === 'number' ? `$${platform.usage.total.usd_est.toFixed(2)}` : 'unknown'}</b></div>
                {platform.usage.cap?.enabled && typeof platform.usage.cap.remaining === 'number' && typeof platform.usage.cap.usd_cap === 'number' && platform.usage.cap.usd_cap > 0 ? (
                  <div className="tc-trust-row tc-usage-cap">
                    <span>Cap ${platform.usage.cap.usd_cap.toFixed(2)}</span>
                    <b>
                      ${platform.usage.cap.remaining.toFixed(2)} left
                      <span className="strip-bar tc-usage-bar" aria-hidden="true">
                        <i style={{ width: `${Math.min(100, Math.max(0, (1 - platform.usage.cap.remaining / platform.usage.cap.usd_cap) * 100))}%` }} />
                      </span>
                    </b>
                  </div>
                ) : (
                  <div className="tc-trust-row"><span>Cap</span><b>{platform.usage.cap?.enabled === false ? 'no cap configured' : (typeof platform.usage.cap?.remaining === 'number' ? `$${platform.usage.cap.remaining.toFixed(2)} left` : 'unknown')}</b></div>
                )}
              </div>
            ) : (
              <>
                <div className="tc-trust-row"><span>Runs today</span><b>unknown</b></div>
                <div className="tc-trust-row"><span>Spend remaining</span><b>unknown</b></div>
              </>
            )}
            <EntitlementGate
              tier={platform.entitlements?.tier}
              entitlements={platform.entitlements}
              loading={platform.entLoading}
              mock={transportMock}
            />
            <ClaudeAccountPanel
              mock={transportMock}
              grant={platform.grant}
              loading={platform.grantLoading}
              busy={platform.grantBusy}
              error={platform.grantErr}
              open={claudeOpen}
              onToggle={setClaudeOpen}
              onLink={platform.actions.linkClaude}
              onUnlink={platform.actions.unlinkClaude}
            />
            <button
              type="button"
              className="chip-act tc-account-details"
              onClick={() => setDrawer({
                title: 'Account details',
                rows: [
                  `tenant ${tenantId}`,
                  `organization ${sessionOrg || workspace.orgId || 'unknown'}`,
                  `tier ${sessionTier || platform.entitlements?.tier || 'unknown'}`,
                  `authentication ${sessionAuthRequired ? 'sign in required' : 'active'}`,
                ],
                action: isSignedIn() ? { label: 'Sign out', onClick: platformSession.actions.signOut } : null,
                foot: 'Platform identity and Claude account credit are separate.',
              })}
            >
              Account details
            </button>
          </div>
        )}
        {rightView === 'view' && (
          <div className="tc-view-panel">
            <div className="tc-panel-heading">
              <span>Drawing view</span>
              <button type="button" onClick={onFitDrawing}>Fit</button>
            </div>
            <Legend
              layers={drawing.shown?.layers || []}
              counts={layerCounts}
              colorForLayer={() => '#96a0ac'}
              visibleLayers={drawing.visibleLayers}
              onToggle={toggleLayer}
            />
            <SelectionReadout selection={selection} onDeselect={() => onSelectedHandleChange?.(null)} />
            {sculpture && (
              <div className="tc-camera-controls" data-testid="camera-controls">
                Drag to orbit · right-drag to pan · scroll to zoom
              </div>
            )}
          </div>
        )}
        </div>
        <div className="tc-rail-foot"><span className="tc-link muted">{PROOF_MODE ? (phase === 'complete' || phase === 'undone' ? 'Cat oracle, sitting-v1' : 'Contract proof, no APS claim') : 'Tool results appear here'}</span></div>
      </aside>

      <div className={`tc-bar-wrap${focusView ? ' tc-focus-hidden' : ''}`} data-cast="tool" style={{ '--rank': 2 }}>
        <div
          className={`tc-bar ${uploadDragActive ? 'upload-drag-active' : ''}`}
          onDragEnter={(event) => { event.preventDefault(); setUploadDragActive(true) }}
          onDragOver={(event) => { event.preventDefault(); setUploadDragActive(true) }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setUploadDragActive(false)
          }}
          onDrop={(event) => {
            event.preventDefault()
            setUploadDragActive(false)
            const file = event.dataTransfer?.files?.[0]
            if (file) drawingUpload.actions.upload(file)
          }}
        >
          {uploadDragActive && <div className="tc-upload-drop" role="status">Drop a DWG or DXF to open it here</div>}
          {routeError && (
            <div className="strip-decision enter error" role="alert">
              <span className="dot red" aria-hidden="true" />
              <span className="strip-sentence">Could not route the request. {routeError}<span className="dim"> The drawing is unchanged.</span></span>
              <button type="button" className="chip-act" onClick={runRequest}>Retry</button>
              <span className="key">R</span>
            </div>
          )}
          {agentBanner && (
            <div className="strip-decision enter" role="status">
              <span className="dot square" aria-hidden="true" />
              <span className="strip-sentence">{agentBanner.message}</span>
              {agentBanner.kind === 'grant' && <button type="button" className="chip-act" onClick={() => { setRightView('trust'); setClaudeOpen(true) }}>Link account</button>}
              <button type="button" className="chip-neutral" onClick={catalog.actions.clearAgentBanner}>Dismiss</button>
            </div>
          )}
          <RoutePanel
            route={route}
            tools={tools}
            running={busy || jobRunning}
            writeEntitled={writeEntitled}
            writeLocked={writeLocked}
            writeLockNote={writeLockNote}
            onConfirmIntent={runCatalogTool}
            onPickAlternative={catalog.actions.pickAlternative}
            onOpenAuthor={() => setLeftView('author')}
            onDismiss={catalog.actions.dismissRoute}
          />
          <div className="tc-bar-input-row">
            <span className="tc-bar-caret">›</span>
            <input
              type="text"
              className="tc-bar-input"
              value={prompt}
              onChange={(event) => changePrompt(event.target.value)}
              onKeyDown={runOnEnter}
              aria-label="Command bar"
              data-testid="command-bar"
              placeholder={PUBLIC_DEMO
                ? 'Message the demo or describe a CAD task.'
                : PROOF_MODE
                ? `Try: ${CAT_REQUEST}`
                : 'Describe a change to this drawing. Nothing runs until you submit it.'}
            />
            <button type="button" className="tc-run" onClick={runRequest} disabled={platformSession.status !== 'active' || !hasDrawing || busy || jobRunning || routing || phase === 'loading'}>{routing ? 'Routing' : PUBLIC_DEMO ? 'Send' : 'Run'}</button>
          </div>
          <div className="tc-bar-controls">
            <span className="tc-bar-chip">Scope · this drawing</span>
            <span className="tc-bar-scopes">{PUBLIC_DEMO ? 'message · review · run · version' : 'plan · approve · execute · version'}</span>
            <span className="tc-bar-proj">{activeDrawingId || 'No drawing'}</span>
            <span className="key tc-bar-key">⌘K</span>
          </div>
        </div>
      </div>

      <div className={`tc-caption${focusView ? ' tc-focus-hidden' : ''}`} data-cast="tool">
        {PUBLIC_DEMO
          ? 'Interactive local demo: sample drawing and client-side tools. No cloud data changes.'
          : PROOF_MODE
            ? 'Deterministic browser proof. This surface does not claim a live Claude or APS run.'
          : 'Live service chain: web → app → harness → broker. Requests are not preloaded or simulated.'}
      </div>

      <div className="sr-only" role="status" aria-label="Run status announcements" aria-live="polite" aria-atomic="true">
        {jobRunning
          ? `Running ${selectedCatalogTool?.name || currentJob?.tool || 'tool'}`
          : jobResult?.ok
            ? `${jobResult.tool || 'Tool'} complete`
            : jobResult?.error?.message || ''}
      </div>
      <Toast toast={toast} onDone={(id) => setToast((current) => current?.id === id ? null : current)} />
      <DetailsDrawer data={drawer} onClose={() => setDrawer(null)} />
      {opsOpen && (
        <div className="drawer-layer tc-ops-layer">
          <OpsDrawer onDismiss={() => setOpsOpen(false)} />
        </div>
      )}
      {tourOn && sessionReady && (
        <DemoTour
          steps={UNIFIED_TOUR_STEPS}
          index={tourIndex}
          onIndexChange={moveTour}
          onCannedPrompt={runTourPrompt}
          onExit={exitTour}
          landed={UNIFIED_TOUR_STEPS[tourIndex]?.id === 'request'
            ? phase === 'proposal'
            : UNIFIED_TOUR_STEPS[tourIndex]?.id === 'approval'
              ? phase === 'complete'
              : true}
          busy={routing || busy || jobRunning}
          bannerTitle="Leaf operator walkthrough"
          bannerSubtitle="One scene for request, approval, job, drawing, version, and trust."
        />
      )}
      {!tourOn && sessionAuthRequired && !sessionWasActiveThisPageLoad && (
        // Mounted independent of the scene so the data-cast choreography can
        // fade it with the other tool panes (a mount gated on `active` pops
        // in over the fading landing scene and unmounts without the exit
        // fade). The scene CSS + SiteRoot's inert sweep hide it off-scene;
        // sceneActive only gates its document listeners.
        <FirstRunCoach
          signedIn={platformSession.status === 'active'}
          active={!focusView}
          sceneActive={active}
        />
      )}
      </>
      ) : activeSurface === 'ios' ? (
      <>
      <div className="tc-topcluster tc-topcluster-product" data-cast="tool" style={{ '--rank': 3 }}>
        {projectSlot}
        <span className="tc-solve" data-testid="ios-ship-status">
          <span className={`dot ${iosShip.launchable ? 'live' : 'hollow'}`} />
          {iosShip.launchable ? 'Ship lane ready' : 'Ship lane setup required'}
        </span>
        <button type="button" className="tc-back" onClick={() => navigate('/')}>Back to the site</button>
        <span className="key">Esc</span>
      </div>
      <aside className="tc-rail tc-rail-l tc-operator-rail" aria-label="iOS ship lane" data-cast="tool" data-testid="ios-ship-lane" style={{ '--rank': 0 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">iOS ship lane</span>
          <span className="tc-rail-sub">readiness · launch · receipt</span>
        </div>
        <div className="tc-rail-body">
          <p className="tc-rail-note">
            Turn an approved project revision into a TestFlight build through the mounted Apple
            ship lane. Apple passwords, two-factor codes, keys, certificates, and profiles never
            enter this browser.
          </p>
          {!iosShip.launchable && (
            <p className="tc-rail-note" data-testid="ios-ship-setup">
              {iosShip.setupAction
                ? `Setup action: ${iosShip.setupAction}`
                : 'The ship lane is not ready. No launch control is available.'}
            </p>
          )}
          {iosShipLaunchAffordance(iosShip, {
            projectId: workspace.openProjectId,
            revision: workspace.canonicalVersionId,
            sessionActive: platformSession.status === 'active',
          }) && (
            <button
              type="button"
              className="tc-run"
              data-testid="ios-ship-launch"
              disabled={iosShipBusy}
              onClick={launchIosShip}
            >
              {iosShipBusy ? 'Launching' : 'Launch TestFlight build'}
            </button>
          )}
          {iosShipExecution && (
            <p className="tc-rail-note" data-testid="ios-ship-execution">
              Build {iosShipExecution.build_number} · {iosShipExecution.status}
              {iosShipExecution.failed_stage ? ` at ${iosShipExecution.failed_stage}` : ''}
            </p>
          )}
          {iosShipReceipt && (
            <div className="tc-rail-note" data-testid="ios-ship-receipt">
              <strong>TestFlight receipt</strong>
              <div>{iosShipReceipt.bundle_identifier} · {iosShipReceipt.marketing_version} ({iosShipReceipt.build_number})</div>
              <div>{iosShipReceipt.app_store_connect_result?.status} · {iosShipReceipt.app_store_connect_result?.build_id}</div>
            </div>
          )}
          {iosShipError && <p className="tc-rail-note" data-testid="ios-ship-error">{iosShipError}</p>}
        </div>
      </aside>
      </>
      ) : (
        <ProductSurfaceFrame
          activeSurface={activeSurface}
          states={productStates}
          catalog={capabilityCatalog}
          catalogError={catalogError}
          projectSlot={projectSlot}
        />
      )}
    </>
  )
}
