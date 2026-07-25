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
} from '../api.js'
import ConversePanel from '../components/ConversePanel.jsx'
import AuthorPanel from '../components/AuthorPanel.jsx'
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
import Toast from '../components/Toast.jsx'
import SessionGate from '../components/SessionGate.jsx'
import OpsDrawer from '../components/OpsDrawer.jsx'
import ToolsPanel from '../components/ToolsPanel.jsx'
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

const CAT_REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'
const DRAWING_ID = 'cat-panels'
const catalogServices = { getTools, getCapabilities, routePrompt: nlPrompt }
const workspaceServices = { createOrg, listProjects, createProject, openProject }

function defaultsOf(schema) {
  const defaults = {}
  for (const [key, property] of Object.entries(schema?.properties || {})) {
    if (property.default !== undefined) defaults[key] = property.default
  }
  return defaults
}

function phaseLabel(phase) {
  if (phase === 'starting') return 'Starting request'
  if (phase === 'proposal') return 'Waiting for approval'
  if (phase === 'running') return 'Rearranging panels'
  if (phase === 'complete') return 'Cat version ready'
  if (phase === 'tool-complete') return 'Tool run complete'
  if (phase === 'undone') return 'Original restored'
  if (phase === 'failed') return 'Request failed'
  return 'Drawing ready'
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
  onVisibleLayersChange,
  selectedHandle,
  onSelectedHandleChange,
  onResultOverlayChange,
}) {
  const [prompt, setPrompt] = useState(CAT_REQUEST)
  const { converse, drawing, drawingEvent, drawingError, instanceId } = useWorkspaceControllers()
  const { sessionId, turns, startTurn, clear: clearConverse, resetCached } = converse
  const platformSession = useSessionController()
  const sessionAuthRequired = platformSession.status === 'required'
  const requireAuth = platformSession.actions.requireAuth
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState(null)
  const [linkedJobId, setLinkedJobId] = useState(null)
  const [tenantId, setTenantId] = useState('try-surface')
  const [sessionTier, setSessionTier] = useState(null)
  const [sessionOrg, setSessionOrg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [leftView, setLeftView] = useState('operator')
  const [rightView, setRightView] = useState('execution')
  const [selectedCatalogTool, setSelectedCatalogTool] = useState(null)
  const [claudeOpen, setClaudeOpen] = useState(false)
  const [toast, setToast] = useState(null)
  const [drawer, setDrawer] = useState(null)
  const [uploadDragActive, setUploadDragActive] = useState(false)
  const [opsOpen, setOpsOpen] = useState(() => new URLSearchParams(window.location.search).get('ops') === '1')
  const toastSeqRef = useRef(0)
  const catalogDecisionRef = useRef(null)
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
  const catalogAdapters = useMemo(() => ({
    previewRoute: matchPrompt,
    commitDecision: (decision) => catalogDecisionRef.current?.(decision),
    dismissDecision: () => {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    },
    onAuthRequired: () => requireAuth('catalog'),
  }), [requireAuth])

  useEffect(() => {
    if (!drawingEvent) return
    setPhase(drawingEvent.event === 'undo' ? 'undone' : 'complete')
  }, [drawingEvent])

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
    getSession(false, 'cat')
      .then((data) => {
        if (!live) return
        seatIntake(data.intake, {
          drawingId: DRAWING_ID,
          drawingState: { drawing_id: DRAWING_ID, version: 1, head: 1, latest: 1 },
          apply: true,
        })
        setTenantId(data.tenant || 'try-surface')
        setSessionTier(data.tier || null)
        setSessionOrg(data.org || null)
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

  const onCompleteVersion = useCallback(async (newVersion) => {
    const drawingId = newVersion?.drawing_id || 'cat-panels'
    try {
      const view = await getDrawingIntake(false, drawingId, 'head')
      seatVersion(view, { drawingId, source: 'job', event: 'complete' })
    } catch {
      setError('The panel run completed, but its drawing version could not be loaded.')
      setPhase('failed')
    }
  }, [seatVersion])

  const showToast = useCallback((next) => {
    toastSeqRef.current += 1
    setToast({ id: toastSeqRef.current, ...next })
  }, [])

  const onJobNotice = useCallback(({ text }) => {
    showToast({ text, action: { label: 'View', onClick: () => setRightView('execution') } })
  }, [showToast])

  const onUploadReady = useCallback(async ({ receipt, view }) => {
    seatVersion(view, { drawingId: receipt.drawing_id, source: 'upload', event: 'upload' })
    setTenantId(receipt.tenant_id || tenantId)
    setPhase('ready')
    setError(null)
    showToast({ text: `Drawing ready, ${view?.intake?.dwg || receipt.drawing_id}`, action: { label: 'View', onClick: () => setRightView('view') } })
  }, [seatVersion, showToast, tenantId])

  const drawingUpload = useDrawingUploadController({ onReady: onUploadReady })

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
    adoptEnvelope,
  } = useJobController({
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
    mock: false,
    onAuthRequired: (required, sources) => {
      if (required) requireAuth(`platform:${(sources || []).join(',') || 'unknown'}`)
    },
  })
  const writeEntitled = platform.isEntitled('run_write')
  const checkout = useCheckoutController({
    mock: false,
    drawingId: drawing.drawingState?.drawing_id || DRAWING_ID,
    holder: tenantId || 'try-surface',
  })
  const workspace = useWorkspaceController({ mock: false, services: workspaceServices })
  const currentProjectName = selectCurrentProjectName(workspace)
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
      mock: false,
      entitlements: null,
      running: busy || jobRunning,
      agentDisabled: true,
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
    drawingUpload.actions.cancel()
    clearConverse()
    resetCached()
  }, [catalog.actions, clearConverse, drawingUpload.actions, resetCached, sessionAuthRequired])
  const {
    tools,
    toolsError,
    route,
  } = catalog.state
  const armCatalogDecision = useCallback((decision) => {
    if (decision?.lane !== 'run') return decision
    const tool = tools.find((candidate) => candidate.name === decision.tool)
    if (!tool) return decision
    if ((tool.capabilities || []).includes('drawing.write') && checkout.writeLocked) {
      setError(`Editing is locked by ${checkout.lockedByOther.holder}. Read tools still run.`)
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
  }, [catalog.actions])

  const runCatalogTool = useCallback(async (intent, tool, params) => {
    if (!tool || busy || jobRunning) return
    if ((tool.capabilities || []).includes('drawing.write') && checkout.writeLocked) {
      setError(`Editing is locked by ${checkout.lockedByOther.holder}. The write did not run.`)
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
      execute: ({ onSubmit, onStatus }) => runToolAsync(
        tool,
          confirmed.execution.params,
          confirmed.execution.context.drawingId,
          {
          orgId: confirmed.execution.context.orgId || undefined,
          projectId: confirmed.execution.context.projectId || undefined,
          idempotencyKey: confirmed.execution.intentId,
          catalogDigest: confirmed.execution.toolSnapshot.catalogDigest || undefined,
          dwgVersion: confirmed.execution.context.drawingVersion ?? undefined,
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
    if (workspace.openProjectId) workspace.rehydrate()
    checkout.actions.refresh()
  }, [busy, catalog.actions, catalogRunContext, checkout.actions, checkout.lockedByOther, checkout.writeLocked, jobRunning, runTrackedJob, workspace])

  const retryCatalogRun = useCallback(() => {
    const last = lastConfirmedRunRef.current
    if (!last || busy || jobRunning) return
    requestCatalogRun(last.tool, { ...last.params })
  }, [busy, jobRunning, requestCatalogRun])

  const openWorkspaceProject = useCallback(async (projectId) => {
    const opened = await workspace.openProject(projectId)
    const canonical = opened?.drawing_versions?.[0]?.version_id || null
    workspace.selectCanonicalVersion(canonical)
    setLeftView('workspace')
  }, [workspace])

  const createWorkspaceOrg = useCallback(async (name) => {
    if (name != null) await workspace.createOrg(name)
  }, [workspace])

  const createWorkspaceProject = useCallback(async (name) => {
    if (name == null || !name.trim()) return
    const project = await workspace.createProject(name)
    if (project) setLeftView('workspace')
  }, [workspace])

  const authorTool = useCallback((description) => stageAuthorTool(false, description), [])

  const publishAuthoredTool = useCallback(async (staged) => {
    const published = await publishStagedAuthor(false, staged)
    const tool = published.tool || staged.tool
    catalog.actions.upsertTool(tool)
    await catalog.actions.loadCatalog()
    const message = `Tool published, ${tool.name}`
    showToast({ text: message, action: { label: 'View', onClick: () => setLeftView('author') } })
    return { ...published, tool }
  }, [catalog.actions, showToast])

  const useAuthoredTool = useCallback((tool) => {
    if (!tool) return
    setSelectedCatalogTool(tool)
    catalog.actions.commitDecision({
      lane: 'run',
      tool: tool.name,
      params: {},
      confidence: 0.99,
      rationale: `Authored just now. Confirm to run ${tool.name}.`,
      alternatives: [],
    })
  }, [catalog.actions])

  const onJobLinked = useCallback((nextJobId) => {
    if (!nextJobId) return
    setLinkedJobId(nextJobId)
    setPhase('running')
    setError(null)
  }, [])

  const attachJob = useCallback(async (nextJobId, toolName) => {
    if (!nextJobId) return
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
  }, [attachTrackedJob, checkout.actions, onJobLinked, workspace])

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
    if (!job?.job_id) return
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
  }, [adoptEnvelope])

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

  const runRequest = useCallback(async () => {
    const text = prompt.trim()
    if (!text || busy || jobRunning) return
    if (text.startsWith('/')) {
      setLeftView('catalog')
      await catalog.actions.dispatch(text)
      return
    }
    setBusy(true)
    setError(null)
    setPhase('starting')
    try {
      await startTurn(text, { lane: 'build', tool: null, confidence: 0.42 })
      setPhase('proposal')
    } catch {
      resetCached()
      setError('The assistant could not start this request. The drawing is unchanged.')
      setPhase('failed')
    } finally {
      setBusy(false)
    }
  }, [busy, catalog.actions, jobRunning, prompt, resetCached, startTurn])

  const undo = useCallback(async () => {
    if (busy || jobRunning || !canUndo) return
    setError(null)
    await undoDrawingVersion()
  }, [busy, canUndo, jobRunning, undoDrawingVersion])

  const redo = useCallback(async () => {
    if (busy || jobRunning || !canRedo) return
    setError(null)
    await redoDrawingVersion()
  }, [busy, canRedo, jobRunning, redoDrawingVersion])

  const runOnEnter = (event) => {
    if (event.key === 'Enter') runRequest()
  }

  const statusClass = phase === 'failed' ? 'red' : (phase === 'proposal' ? 'hollow' : 'live')

  return (
    <>
      <div className="tc-topcluster" data-cast="tool" style={{ '--rank': 3 }}>
        <ProjectSwitcher
          mock={false}
          projectName="cat-panels"
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
        <button type="button" className="tc-back" onClick={() => navigate('/')}>Back to the site</button>
        <span className="key">Esc</span>
      </div>

      <aside className="tc-rail tc-rail-l tc-operator-rail" data-cast="tool" data-controller-instance={instanceId} style={{ '--rank': 0 }} data-testid="operator-surface">
        <div className="tc-rail-head">
          <span className="tc-rail-title">Workspace</span>
          <span className="tc-rail-sub">request and tools</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Workspace panels" onKeyDown={moveTab}>
          <button type="button" role="tab" tabIndex={leftView === 'operator' ? 0 : -1} aria-selected={leftView === 'operator'} onClick={() => setLeftView('operator')}>Operator</button>
          <button type="button" role="tab" tabIndex={leftView === 'catalog' ? 0 : -1} aria-selected={leftView === 'catalog'} onClick={() => setLeftView('catalog')}>Catalog <span>{tools.length}</span></button>
          <button type="button" role="tab" tabIndex={leftView === 'author' ? 0 : -1} aria-selected={leftView === 'author'} onClick={() => setLeftView('author')}>Author</button>
          <button type="button" role="tab" tabIndex={leftView === 'workspace' ? 0 : -1} aria-selected={leftView === 'workspace'} onClick={() => setLeftView('workspace')}>Project</button>
        </div>
        <div className="tc-rail-body">
          {leftView === 'operator' && (sessionAuthRequired ? (
            <SessionGate
              configured={authConfigured}
              onSignIn={login}
              onDemo={() => { window.location.href = '/app?demo=1' }}
            />
          ) : sessionId ? (
            <ConversePanel
              sessionId={sessionId}
              userTurns={turns}
              onDismiss={() => {}}
              onLinkClaude={() => {}}
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
            <ToolsPanel
              tools={tools}
              error={toolsError}
              running={busy || jobRunning}
              selectedTool={selectedCatalogTool}
              onRequestRun={requestCatalogRun}
              onOpenTool={setSelectedCatalogTool}
              onRetry={catalog.actions.retryTools}
              writeEntitled={writeEntitled}
              writeLocked={checkout.writeLocked}
              subtitle="Choose a registered capability, inspect its parameters, then review before it runs."
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
          <span className="tc-link muted">{leftView === 'operator' ? 'Claude plans, tools act' : leftView === 'catalog' ? `${tools.length} tools` : leftView === 'author' ? 'Stage, review, publish' : currentProjectName || 'No project open'}</span>
        </div>
      </aside>

      <aside className="tc-rail tc-rail-r" data-cast="tool" style={{ '--rank': 1 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Operations</span>
          <span className="tc-rail-sub">controller state</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Operation panels" onKeyDown={moveTab}>
          <button type="button" role="tab" tabIndex={rightView === 'execution' ? 0 : -1} aria-selected={rightView === 'execution'} onClick={() => setRightView('execution')}>Execution</button>
          <button type="button" role="tab" tabIndex={rightView === 'jobs' ? 0 : -1} aria-selected={rightView === 'jobs'} onClick={() => setRightView('jobs')}>Jobs <span>{visibleJobCount}</span></button>
          <button type="button" role="tab" tabIndex={rightView === 'versions' ? 0 : -1} aria-selected={rightView === 'versions'} onClick={() => { setRightView('versions'); drawing.actions.loadHistory() }}>Versions <span>{drawing.latest || 1}</span></button>
          <button type="button" role="tab" tabIndex={rightView === 'trust' ? 0 : -1} aria-selected={rightView === 'trust'} onClick={() => { setRightView('trust'); platform.actions.refreshAll() }}>Trust</button>
          <button type="button" role="tab" tabIndex={rightView === 'view' ? 0 : -1} aria-selected={rightView === 'view'} onClick={() => setRightView('view')}>View</button>
        </div>
        <div className="tc-rail-body">
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
            heldByUs={checkout.heldByUs}
            busy={checkout.busy}
            onTake={checkout.actions.take}
            onRelease={checkout.actions.release}
          />
        </div>
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
            mock={false}
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
            <div className="tc-trust-row"><span>Backend</span><b>{platform.healthStatus.status}</b></div>
            <div className="tc-trust-row"><span>Claude account</span><b>{platform.grant?.linked ? `linked${platform.grant.kind ? ` · ${platform.grant.kind}` : ''}` : 'not linked'}</b></div>
            <div className="tc-trust-row"><span>Runs today</span><b>{platform.usage?.today?.runs ?? 'unknown'}</b></div>
            <div className="tc-trust-row"><span>Spend remaining</span><b>{typeof platform.usage?.cap?.remaining === 'number' ? `$${platform.usage.cap.remaining.toFixed(2)}` : 'unknown'}</b></div>
            <EntitlementGate
              tier={platform.entitlements?.tier}
              entitlements={platform.entitlements}
              loading={platform.entLoading}
              mock={false}
            />
            <ClaudeAccountPanel
              mock={false}
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
            <Legend
              layers={drawing.shown?.layers || []}
              counts={layerCounts}
              colorForLayer={() => '#96a0ac'}
              visibleLayers={drawing.visibleLayers}
              onToggle={toggleLayer}
            />
            <SelectionReadout selection={selection} onDeselect={() => onSelectedHandleChange?.(null)} />
          </div>
        )}
        </div>
        <div className="tc-rail-foot"><span className="tc-link muted">{phase === 'complete' || phase === 'undone' ? 'Cat oracle, sitting-v1' : 'Contract proof, no APS claim'}</span></div>
      </aside>

      <div className="tc-bar-wrap" data-cast="tool" style={{ '--rank': 2 }}>
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
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={runOnEnter}
              aria-label="Command bar"
            />
            <button type="button" className="tc-run" onClick={runRequest} disabled={sessionAuthRequired || busy || jobRunning || phase === 'loading'}>Run</button>
          </div>
          <div className="tc-bar-controls">
            <span className="tc-bar-chip">Scope · this drawing</span>
            <span className="tc-bar-scopes">plan · approve · execute · version</span>
            <span className="tc-bar-proj">cat-panels</span>
            <span className="key tc-bar-key">⌘K</span>
          </div>
        </div>
      </div>

      <div className="tc-caption" data-cast="tool">
        Deterministic browser proof. This surface does not claim a live Claude or APS run.
      </div>

      <div className="sr-only" aria-live="polite" aria-atomic="true">
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
    </>
  )
}
