import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getCapabilities,
  createOrg,
  createProject,
  getDrawingIntake,
  getJob,
  getSession,
  getTools,
  listProjects,
  nlPrompt,
  openProject,
  runToolAsync,
  stageAuthorTool,
  publishStagedAuthor,
  recordToEnvelope,
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
import SelectionReadout from '../components/SelectionReadout.jsx'
import RoutePanel from '../components/RoutePanel.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import QuotaCard from '../components/QuotaCard.jsx'
import DegradedBanner from '../components/DegradedBanner.jsx'
import Toast from '../components/Toast.jsx'
import SessionGate from '../components/SessionGate.jsx'
import OpsDrawer from '../components/OpsDrawer.jsx'
import WorkspaceSummary from '../components/WorkspaceSummary.jsx'
import { useWorkspaceControllers } from '../controllers/WorkspaceControllerProvider.jsx'
import useCatalogController from '../controllers/catalog/useCatalogController.js'
import useJobController from '../controllers/useJobController.js'
import usePlatformTrustController from '../controllers/platform/usePlatformTrustController.js'
import useWorkspaceController from '../controllers/workspace/useWorkspaceController.js'
import useCheckoutController from '../controllers/checkout/useCheckoutController.js'
import useDrawingUploadController from '../controllers/upload/useDrawingUploadController.js'
import useSessionController from '../controllers/session/useSessionController.js'
import { selectCurrentProjectName } from '../controllers/workspace/createWorkspaceController.js'
import { matchPrompt } from '../mock/mockNlPrompt.js'
import {
  confirmRunIntent,
  createCatalogRunContext,
  createCatalogToolSnapshot,
  createRunIntentState,
  dismissRunIntent,
  stageRunIntent,
} from '../runIntent.js'
import { navigate } from './router.js'
import { authConfigured, isSignedIn, login } from '../auth.js'
import { classifyAgentError } from '../converse.js'
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
const DRAWING_SOURCE = PROOF_MODE ? 'cat' : 'rooftop_demo'
const DEMO_REQUESTED = new URLSearchParams(window.location.search).get('demo') === '1'
// A signed-in user gets the live session and mounted-account path on the same
// CAD surface. Only an anonymous demo remains fully local.
const PUBLIC_DEMO = DEMO_REQUESTED && !isSignedIn()
const LIVE_TOUR_REQUESTED = new URLSearchParams(window.location.search).get('demo') === 'tour'
const freshDrawingId = () => {
  const randomId = globalThis.crypto?.randomUUID?.()
  if (randomId) return `cat-workbench-${randomId}`
  return `cat-workbench-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}
const liveDrawingId = () => {
  const key = 'leaf.cat.workbench.id.v1'
  try {
    const existing = globalThis.sessionStorage?.getItem(key)
    if (/^cat-workbench-[0-9a-z-]+$/.test(existing || '')) return existing
    const created = freshDrawingId()
    globalThis.sessionStorage?.setItem(key, created)
    return created
  } catch {
    return freshDrawingId()
  }
}
const DRAWING_ID = PROOF_MODE ? 'cat-panels' : liveDrawingId()
const catalogServices = { getTools, getCapabilities, routePrompt: nlPrompt }
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

function agentBannerFor(error) {
  const kind = classifyAgentError(error)
  if (kind === 'quota') return { kind, message: 'AI paused. Your built tools keep working.' }
  if (kind === 'grant') return { kind, message: 'Chat needs a linked Claude account.' }
  if (kind === 'entitlement') return { kind, message: 'Chat is not included in your plan. Your built tools keep working.' }
  if (kind === 'busy') return { kind, message: 'The assistant is mid-turn. The catalog route is still available.' }
  if (kind === 'rate_limited') return { kind, message: 'AI is rate-limited. The catalog route is still available; retry shortly.' }
  return { kind: 'unreachable', message: 'AI assistant unavailable. The catalog route is still available.' }
}

function defaultsOf(schema) {
  const defaults = {}
  for (const [key, property] of Object.entries(schema?.properties || {})) {
    if (property.default !== undefined) defaults[key] = property.default
  }
  return defaults
}

function phaseLabel(phase) {
  if (phase === 'starting') return 'Starting request'
  if (phase === 'proposal') return PROOF_MODE ? 'Waiting for approval' : 'Assistant response'
  if (phase === 'running') return 'Rearranging panels'
  if (phase === 'complete') return PROOF_MODE ? 'Cat version ready' : '3D cat ready'
  if (phase === 'tool-complete') return 'Tool run complete'
  if (phase === 'undone') return 'Original restored'
  if (phase === 'failed') return 'Request failed'
  return PROOF_MODE ? 'Drawing ready' : 'Backend ready'
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

export default function ToolCast({
  active,
  onFitDrawing,
  onViewModeChange,
  onVisibleLayersChange,
  selectedHandle,
  onSelectedHandleChange,
  onResultOverlayChange,
}) {
  const [prompt, setPrompt] = useState(PROOF_MODE ? CAT_REQUEST : '')
  const { converse, drawing, drawingEvent, drawingError, instanceId } = useWorkspaceControllers()
  const { sessionId, turns, startTurn, clear: clearConverse, resetCached } = converse
  const platformSession = useSessionController()
  const sessionAuthRequired = platformSession.status === 'required'
  const sessionReady = PUBLIC_DEMO || platformSession.status === 'active'
  const transportMock = PUBLIC_DEMO || !sessionReady
  const sessionReadyRef = useRef(sessionReady)
  sessionReadyRef.current = sessionReady
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
  const [claudeOpen, setClaudeOpen] = useState(false)
  const [toast, setToast] = useState(null)
  const [drawer, setDrawer] = useState(null)
  const [uploadDragActive, setUploadDragActive] = useState(false)
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
  const runIntentSessionRef = useRef(null)
  if (!runIntentSessionRef.current) {
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    runIntentSessionRef.current = `try-${id}`
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
      setAuthorSeed(text || '')
      setAuthorSeedSignal((current) => current + 1)
      setLeftView('author')
    },
    startAgentTurn: async (text, hint) => {
      if (!sessionReadyRef.current) return undefined
      const response = await startTurn(text, hint)
      setLeftView('operator')
      setPhase('proposal')
      return response
    },
    agentBannerFor,
  }), [requireAuth, startTurn])

  useEffect(() => {
    if (!drawingEvent) return
    setPhase(drawingEvent.event === 'undo' ? 'undone' : 'complete')
  }, [drawingEvent])

  useEffect(() => {
    if (LIVE_TOUR_REQUESTED && platformSession.status === 'active') setTourOn(true)
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
    if (platformSession.status === 'active' && drawing.shown) {
      setPhase(Number(drawing.head) > 1 ? 'complete' : 'ready')
      return undefined
    }
    let live = true
    platformSession.actions.checking()
    setPhase('loading')
    getSession(PUBLIC_DEMO, DRAWING_SOURCE)
      .then((data) => {
        if (!live) return
        if (PUBLIC_DEMO) mockVersions.seedBase(data.intake)
        seatIntake(data.intake, {
          drawingId: DRAWING_ID,
          drawingState: { drawing_id: DRAWING_ID, version: 1, head: 1, latest: 1 },
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
        setError('The drawing backend is unavailable. Start the proof API and reload this surface.')
        setPhase('failed')
      })
    return () => { live = false }
  }, [active, platformSession.actions, requireAuth, seatIntake])

  const showToast = useCallback((next) => {
    toastSeqRef.current += 1
    setToast({ id: toastSeqRef.current, ...next })
  }, [])

  const onCompleteVersion = useCallback(async (newVersion, envelope) => {
    const drawingId = newVersion?.drawing_id || 'cat-panels'
    try {
      if (PUBLIC_DEMO) {
        if (!mockVersions.isSeeded() && drawing.shown) mockVersions.seedBase(drawing.shown)
        mockVersions.applyDelete(envelope?.result?.removed)
      }
      const view = await getDrawingIntake(PUBLIC_DEMO, drawingId, 'head')
      seatVersion(view, { drawingId, source: 'job', event: 'complete' })
    } catch {
      drawing.actions.markRefreshFailure({ drawing_id: drawingId, version: newVersion?.version })
      showToast({ text: `Version ${newVersion?.version || 'created'} created` })
    }
  }, [drawing.actions, drawing.shown, seatVersion, showToast])

  const onJobNotice = useCallback(({ text }) => {
    showToast({ text, action: { label: 'View', onClick: () => setRightView('execution') } })
  }, [showToast])

  const onUploadReady = useCallback(async ({ receipt, view }) => {
    seatVersion(view, { drawingId: receipt.drawing_id, source: 'upload', event: 'upload' })
    setTenantId(receipt.tenant_id || tenantId)
    setGuestDrawing(receipt.tenant_kind === 'guest' ? {
      drawingId: receipt.drawing_id,
      retentionExpiresAt: receipt.retention_expires_at || null,
    } : null)
    setPhase('ready')
    setError(null)
    showToast({ text: `Drawing ready, ${view?.intake?.dwg || receipt.drawing_id}`, action: { label: 'View', onClick: () => setRightView('view') } })
  }, [seatVersion, showToast, tenantId])

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
    adoptEnvelope,
  } = useJobController({
    mock: transportMock,
    onCompleteVersion,
    onNotice: onJobNotice,
    onAuthRequired: (required) => { if (required) requireAuth('jobs') },
    formatError: () => 'The panel run did not produce a readable result.',
  })

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
    drawingId: drawing.drawingState?.drawing_id || DRAWING_ID,
    holder: checkoutHolder,
  })
  const takeCheckout = useCallback((...args) => {
    if (!sessionReady) return undefined
    return checkout.actions.take(...args)
  }, [checkout.actions, sessionReady])
  const releaseCheckout = useCallback((...args) => {
    if (!sessionReady) return undefined
    return checkout.actions.release(...args)
  }, [checkout.actions, sessionReady])
  const workspace = useWorkspaceController({ mock: transportMock, services: workspaceServices })
  const currentProjectName = selectCurrentProjectName(workspace)
  const activeDrawingId = drawing.drawingState?.drawing_id || DRAWING_ID
  const catalogRunContext = useMemo(() => createCatalogRunContext({
    tenantId,
    orgId: workspace.orgId,
    projectId: workspace.openProjectId || null,
    workspace: workspace.workspace,
    selectedVersionId: workspace.canonicalVersionId,
    drawingState: drawing.drawingState,
    fallbackDrawingId: DRAWING_ID,
  }), [drawing.drawingState, tenantId, workspace.canonicalVersionId, workspace.openProjectId, workspace.orgId, workspace.workspace])

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
    const tool = tools.find((candidate) => candidate.name === decision.tool)
    if (!tool) return decision
    if ((tool.capabilities || []).includes('drawing.write') && checkout.writeLocked) {
      setError(checkout.lockedByOther?.holder
        ? `Editing is locked by ${checkout.lockedByOther.holder}. Read tools still run.`
        : 'Editing is paused while Leaf checks the drawing lock. Read tools still run.')
      return undefined
    }
    const context = catalogRunContext
    if (!context) {
      setError('This project needs a canonical drawing version before a tool can run.')
      return undefined
    }
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    const effectiveParams = { ...defaultsOf(tool.params), ...(decision.params || {}) }
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `try-intent-${id}`,
      toolName: tool.name,
      params: effectiveParams,
      context,
      toolSnapshot: createCatalogToolSnapshot(tool),
    })
    runIntentStateRef.current = staged.state
    return { ...decision, params: staged.intent.params, runIntent: staged.intent }
  }, [catalogRunContext, checkout.lockedByOther, checkout.writeLocked, tools])
  catalogDecisionRef.current = armCatalogDecision

  const requestCatalogRun = useCallback((tool, params) => {
    if (!sessionReady) return
    setSelectedCatalogTool(tool)
    setLeftView('catalog')
    catalog.actions.commitDecision({
      lane: 'run',
      tool: tool.name,
      params,
      confidence: 1,
      rationale: 'Catalog selection. Confirm the exact tool and parameters before it runs.',
      alternatives: [],
    })
  }, [catalog.actions, sessionReady])

  const runCatalogTool = useCallback(async (intent, tool, params) => {
    if (!sessionReady || !tool || busy || jobRunning) {
      catalog.actions.dismissRoute()
      return
    }
    if ((tool.capabilities || []).includes('drawing.write') && checkout.writeLocked) {
      setError(checkout.lockedByOther?.holder
        ? `Editing is locked by ${checkout.lockedByOther.holder}. The write did not run.`
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
    if (!PUBLIC_DEMO && workspace.openProjectId) workspace.rehydrate()
    if (!PUBLIC_DEMO) checkout.actions.refresh()
  }, [busy, catalog.actions, catalogRunContext, checkout.actions, checkout.lockedByOther, checkout.writeLocked, drawing.shown, jobRunning, runTrackedJob, sessionReady, workspace])

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
    if (!sessionReady) return
    if (name != null) await workspace.createOrg(name)
  }, [sessionReady, workspace])

  const createWorkspaceProject = useCallback(async (name) => {
    if (!sessionReady) return
    if (name == null || !name.trim()) return
    const project = await workspace.createProject(name)
    if (project) setLeftView('workspace')
  }, [sessionReady, workspace])

  const authorTool = useCallback((description) => {
    if (!sessionReady) return undefined
    return stageAuthorTool(PUBLIC_DEMO, description)
  }, [sessionReady])

  const publishAuthoredTool = useCallback(async (staged) => {
    if (!sessionReady) return undefined
    const published = await publishStagedAuthor(PUBLIC_DEMO, staged)
    const tool = published.tool || staged.tool
    catalog.actions.upsertTool(tool)
    await catalog.actions.loadCatalog()
    const message = `Tool published, ${tool.name}`
    showToast({ text: message, action: { label: 'View', onClick: () => setLeftView('author') } })
    return { ...published, tool }
  }, [catalog.actions, sessionReady, showToast])

  const useAuthoredTool = useCallback((tool) => {
    if (!sessionReady || !tool) return
    setSelectedCatalogTool(tool)
    catalog.actions.commitDecision({
      lane: 'run',
      tool: tool.name,
      params: {},
      confidence: 0.99,
      rationale: `Authored just now. Confirm to run ${tool.name}.`,
      alternatives: [],
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
    if (workspace.openProjectId) workspace.rehydrate()
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
    if (!text || platformSession.status !== 'active' || busy || jobRunning || routing) return
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
  }, [busy, catalog.actions, jobRunning, platformSession.status, prompt, routing])

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
    tourSeqRef.current += 1
    setTourOn(false)
  }, [])

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

  const statusClass = phase === 'failed' ? 'red' : (phase === 'proposal' ? 'hollow' : 'live')

  return (
    <>
      <div className="tc-topcluster" data-cast="tool" style={{ '--rank': 3 }}>
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
        <span className="tc-solve" data-testid="operator-phase">
          <span className={`dot ${statusClass}${phase === 'running' ? ' pulse' : ''}`} />
          {phaseLabel(phase)}
        </span>
        <span className="tc-version" data-testid="version-head">Version {version ?? 'pending'}</span>
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
          <button type="button" className="tc-back" onClick={() => { setTourIndex(0); setTourOn(true) }}>Restart walk</button>
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
          <button id="workspace-tab-catalog" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'catalog' ? 0 : -1} aria-selected={leftView === 'catalog'} disabled={!sessionReady} onClick={() => setLeftView('catalog')}>Catalog <span>{tools.length}</span></button>
          <button id="workspace-tab-author" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'author' ? 0 : -1} aria-selected={leftView === 'author'} disabled={!sessionReady} onClick={() => setLeftView('author')}>Author</button>
          <button id="workspace-tab-workspace" aria-controls="workspace-tabpanel" type="button" role="tab" tabIndex={leftView === 'workspace' ? 0 : -1} aria-selected={leftView === 'workspace'} disabled={!sessionReady} onClick={() => setLeftView('workspace')}>Project</button>
        </div>
        <div id="workspace-tabpanel" className="tc-rail-body" role="tabpanel" aria-labelledby={`workspace-tab-${leftView}`} tabIndex={0}>
          {leftView === 'operator' && (PUBLIC_DEMO ? (
            <DemoConversationPanel turns={demoTurns} onSuggestion={dispatchRequest} canSignIn={authConfigured} onSignIn={() => login()} />
          ) : sessionAuthRequired ? (
            <>
              {guestDrawing && (
                <div className="tc-panel-note" role="status" data-testid="guest-view-only">
                  <strong>Guest drawing ready.</strong> Inspect this drawing here. Sign in to load tools or run actions. Guest uploads are view-only.
                </div>
              )}
              <SessionGate
                configured={authConfigured}
                onSignIn={login}
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
              writeLocked={checkout.writeLocked}
            />
          ) : (
            <div className="tc-operator-empty">
              <span className={`dot ${phase === 'loading' ? 'live pulse' : 'hollow'}`} />
              <span>{phase === 'loading' ? 'Loading the drawing backend' : 'Type the request in the command bar. The proposal will stay in this rail.'}</span>
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
              onRetryTools={catalog.actions.retryTools}
              writeEntitled={writeEntitled}
              writeLocked={checkout.writeLocked}
            />
          )}
          {leftView === 'workspace' && (
            workspace.workspace ? (
              <WorkspaceSummary
                workspace={workspace.workspace}
                loading={workspace.workspaceLoading}
                selectedVersionId={workspace.canonicalVersionId}
                onSelectVersion={workspace.selectCanonicalVersion}
                onClose={() => { workspace.closeProject(); setLeftView('operator') }}
              />
            ) : (
              <div className="tc-panel-note">Choose a project from the header to load its drawing versions, jobs, and built tools.</div>
            )
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
          <button id="operations-tab-versions" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'versions' ? 0 : -1} aria-selected={rightView === 'versions'} disabled={!sessionReady} onClick={() => { setRightView('versions'); drawing.actions.loadHistory() }}>Versions <span>{drawing.latest || 1}</span></button>
          <button id="operations-tab-trust" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'trust' ? 0 : -1} aria-selected={rightView === 'trust'} disabled={!sessionReady} onClick={() => { setRightView('trust'); platform.actions.refreshAll() }}>Trust</button>
          <button id="operations-tab-view" aria-controls="operations-tabpanel" type="button" role="tab" tabIndex={rightView === 'view' ? 0 : -1} aria-selected={rightView === 'view'} onClick={() => setRightView('view')}>View</button>
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
            onTake={takeCheckout}
            onRelease={releaseCheckout}
            onRetry={checkout.actions.refresh}
          />
        </div>
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
            <DegradedBanner reason={jobResult.degraded_reason || jobResult.result?.degraded_reason || jobResult.result?.reason} />
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
              <div className="tc-preview-note">
                Viewing v{drawing.previewing.version} read-only
                <button type="button" onClick={drawing.actions.backToHead}>Back to head</button>
              </div>
            )}
            {drawing.historyLoading && <div className="tc-panel-note">Loading versions</div>}
            {drawing.historyError && <div className="tc-panel-error">{drawing.historyError}</div>}
            <div className="tc-version-list">
              {[...(drawing.history?.versions || [])].reverse().map((item) => (
                <button
                  type="button"
                  key={item.v}
                  className={drawing.previewing?.version === item.v ? 'active' : ''}
                  onClick={() => drawing.actions.previewVersion(item.v)}
                >
                  <span>v{item.v}</span>
                  <span>{item.tool || 'drawing'}</span>
                  {item.v === drawing.head && <b>head</b>}
                </button>
              ))}
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
            <div className="tc-trust-row"><span>Runs today</span><b>{platform.usage?.today?.runs ?? 'unknown'}</b></div>
            <div className="tc-trust-row"><span>Spend remaining</span><b>{typeof platform.usage?.cap?.remaining === 'number' ? `$${platform.usage.cap.remaining.toFixed(2)}` : 'unknown'}</b></div>
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
            writeLocked={checkout.writeLocked}
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
              placeholder={PUBLIC_DEMO
                ? 'Message the demo or describe a CAD task.'
                : PROOF_MODE
                ? `Try: ${CAT_REQUEST}`
                : 'Describe a change to this drawing. Nothing runs until you submit it.'}
            />
            <button type="button" className="tc-run" onClick={runRequest} disabled={platformSession.status !== 'active' || busy || jobRunning || routing || phase === 'loading'}>{routing ? 'Routing' : PUBLIC_DEMO ? 'Send' : 'Run'}</button>
          </div>
          <div className="tc-bar-controls">
            <span className="tc-bar-chip">Scope · this drawing</span>
            <span className="tc-bar-scopes">{PUBLIC_DEMO ? 'message · review · run · version' : 'plan · approve · execute · version'}</span>
            <span className="tc-bar-proj">{activeDrawingId}</span>
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
      {opsOpen && sessionReady && (
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
      {!tourOn && sessionAuthRequired && <FirstRunCoach signedIn={platformSession.status === 'active'} active={!focusView} />}
    </>
  )
}
