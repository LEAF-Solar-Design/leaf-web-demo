import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getCapabilities,
  getDrawingIntake,
  getDrawingVersions,
  getSession,
  getTools,
  nlPrompt,
  redoDrawing,
  runToolAsync,
  undoDrawing,
} from '../api.js'
import ConversePanel from '../components/ConversePanel.jsx'
import EntitlementGate from '../components/EntitlementGate.jsx'
import JobRail from '../components/JobRail.jsx'
import RoutePanel from '../components/RoutePanel.jsx'
import ToolsPanel from '../components/ToolsPanel.jsx'
import { useWorkspaceControllers } from '../controllers/WorkspaceControllerProvider.jsx'
import useCatalogController from '../controllers/catalog/useCatalogController.js'
import useDrawingVersionController from '../controllers/useDrawingVersionController.js'
import useJobController from '../controllers/useJobController.js'
import usePlatformTrustController from '../controllers/platform/usePlatformTrustController.js'
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

const CAT_REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'
const DRAWING_ID = 'cat-panels'
const loadHead = (drawingId) => getDrawingIntake(false, drawingId, 'head')
const loadVersion = (drawingId, version) => getDrawingIntake(false, drawingId, version)
const loadVersions = (drawingId) => getDrawingVersions(false, drawingId)
const undoVersion = (drawingId) => undoDrawing(false, drawingId)
const redoVersion = (drawingId) => redoDrawing(false, drawingId)
const catalogServices = { getTools, getCapabilities, routePrompt: nlPrompt }

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

export default function ToolCast({ active, onIntakeChange }) {
  const [prompt, setPrompt] = useState(CAT_REQUEST)
  const { converse } = useWorkspaceControllers()
  const { sessionId, turns, startTurn, resetCached } = converse
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState(null)
  const [linkedJobId, setLinkedJobId] = useState(null)
  const [panelCount, setPanelCount] = useState(null)
  const [busy, setBusy] = useState(false)
  const [leftView, setLeftView] = useState('operator')
  const [rightView, setRightView] = useState('execution')
  const [selectedCatalogTool, setSelectedCatalogTool] = useState(null)
  const catalogDecisionRef = useRef(null)
  const runIntentSessionRef = useRef(null)
  if (!runIntentSessionRef.current) {
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    runIntentSessionRef.current = `try-${id}`
  }
  const runIntentStateRef = useRef(null)
  if (!runIntentStateRef.current) {
    runIntentStateRef.current = createRunIntentState(runIntentSessionRef.current)
  }
  const catalogAdapters = useMemo(() => ({
    previewRoute: matchPrompt,
    commitDecision: (decision) => catalogDecisionRef.current?.(decision),
    dismissDecision: () => {
      runIntentStateRef.current = dismissRunIntent(runIntentStateRef.current)
    },
  }), [])

  const applyIntake = useCallback((nextIntake) => {
    onIntakeChange(nextIntake)
    setPanelCount(nextIntake?.polylines?.length || null)
  }, [onIntakeChange])

  const onVersionEvent = useCallback(({ event }) => {
    setPhase(event === 'undo' ? 'undone' : 'complete')
  }, [])

  const onDrawingError = useCallback((cause, { operation }) => {
    if (operation === 'undo') setError('Undo failed. The current drawing version is unchanged.')
    else if (operation === 'redo') setError('Redo failed. The current drawing version is unchanged.')
    else setError(String(cause?.message || cause))
  }, [])

  const drawing = useDrawingVersionController({
    loadHead,
    loadVersion,
    loadVersions,
    undoVersion,
    redoVersion,
    onApplyIntake: applyIntake,
    onVersionEvent,
    onError: onDrawingError,
    initialDrawingState: { drawing_id: DRAWING_ID, version: 1, head: 1, latest: 1 },
  })
  const {
    seatIntake,
    seatVersion,
    undo: undoDrawingVersion,
    redo: redoDrawingVersion,
  } = drawing.actions
  const version = drawing.head
  const { canUndo, canRedo } = drawing

  useEffect(() => {
    if (!active) return undefined
    let live = true
    setPhase('loading')
    getSession(false, 'cat')
      .then((data) => {
        if (!live) return
        seatIntake(data.intake, {
          drawingId: DRAWING_ID,
          drawingState: { drawing_id: DRAWING_ID, version: 1, head: 1, latest: 1 },
          apply: true,
        })
        setPhase('ready')
      })
      .catch(() => {
        if (!live) return
        setError('The drawing backend is unavailable. Start the proof API and reload this surface.')
        setPhase('failed')
      })
    return () => { live = false }
  }, [active, seatIntake])

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

  const {
    jobs,
    currentJob,
    currentJobId,
    inflight,
    reattaching,
    running: jobRunning,
    result: jobResult,
    error: jobError,
    runJob: runTrackedJob,
    attachJob: attachTrackedJob,
  } = useJobController({
    onCompleteVersion,
    formatError: () => 'The panel run did not produce a readable result.',
  })
  const visibleJobCount = useMemo(() => {
    if (!currentJob) return jobs.length
    return jobs.some((job) => job.job_id === currentJob.job_id) ? jobs.length : jobs.length + 1
  }, [currentJob, jobs])
  const platform = usePlatformTrustController({ mock: false })
  const writeEntitled = platform.isEntitled('run_write')

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
  const {
    tools,
    toolsError,
    route,
  } = catalog.state
  const armCatalogDecision = useCallback((decision) => {
    if (decision?.lane !== 'run') return decision
    const tool = tools.find((candidate) => candidate.name === decision.tool)
    if (!tool) return decision
    const context = createCatalogRunContext({
      tenantId: 'try-surface',
      drawingState: drawing.drawingState,
      fallbackDrawingId: DRAWING_ID,
    })
    const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    const staged = stageRunIntent(runIntentStateRef.current, {
      intentId: `try-intent-${id}`,
      toolName: tool.name,
      params: decision.params || {},
      context,
      toolSnapshot: createCatalogToolSnapshot(tool),
    })
    runIntentStateRef.current = staged.state
    return { ...decision, params: staged.intent.params, runIntent: staged.intent }
  }, [drawing.drawingState, tools])
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
    const confirmed = confirmRunIntent(runIntentStateRef.current, {
      intentId: intent?.intentId,
      sessionId: intent?.sessionId,
      toolName: tool.name,
      params,
      context: createCatalogRunContext({
        tenantId: 'try-surface',
        drawingState: drawing.drawingState,
        fallbackDrawingId: DRAWING_ID,
      }),
      toolSnapshot: createCatalogToolSnapshot(tool),
    })
    runIntentStateRef.current = confirmed.state
    if (!confirmed.ok) {
      setError('That run confirmation is no longer valid. Review the tool again.')
      catalog.actions.dismissRoute()
      return
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
          idempotencyKey: confirmed.execution.intentId,
          catalogDigest: confirmed.execution.toolSnapshot.catalogDigest || undefined,
          dwgVersion: confirmed.execution.context.drawingVersion ?? undefined,
          onSubmit,
          onStatus,
        },
      ),
    })
    if (envelope?.ok) setPhase(envelope.result?.new_version ? 'complete' : 'tool-complete')
    else {
      setPhase('failed')
      setError('The catalog run did not produce a readable result.')
    }
    setBusy(false)
    catalog.actions.dismissRoute()
  }, [busy, catalog.actions, drawing.drawingState, jobRunning, runTrackedJob])

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
    if (!envelope?.ok) {
      setPhase('failed')
      setError('The panel run did not produce a readable drawing version.')
    }
  }, [attachTrackedJob, onJobLinked])

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
        <span className="tc-solve" data-testid="operator-phase">
          <span className={`dot ${statusClass}${phase === 'running' ? ' pulse' : ''}`} />
          {phaseLabel(phase)}
        </span>
        <span className="tc-version" data-testid="version-head">Version {version ?? 'pending'}</span>
        <button type="button" className="tc-back" onClick={() => navigate('/')}>Back to the site</button>
        <span className="key">Esc</span>
      </div>

      <aside className="tc-rail tc-rail-l tc-operator-rail" data-cast="tool" style={{ '--rank': 0 }} data-testid="operator-surface">
        <div className="tc-rail-head">
          <span className="tc-rail-title">Workspace</span>
          <span className="tc-rail-sub">request and tools</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Workspace panels">
          <button type="button" role="tab" aria-selected={leftView === 'operator'} onClick={() => setLeftView('operator')}>Operator</button>
          <button type="button" role="tab" aria-selected={leftView === 'catalog'} onClick={() => setLeftView('catalog')}>Catalog <span>{tools.length}</span></button>
        </div>
        <div className="tc-rail-body">
          {leftView === 'operator' && (sessionId ? (
            <ConversePanel
              sessionId={sessionId}
              userTurns={turns}
              onDismiss={() => {}}
              onLinkClaude={() => {}}
              onAttachJob={attachJob}
              onJobLinked={attachJob}
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
              subtitle="Choose a registered capability, inspect its parameters, then review before it runs."
            />
          )}
          {(error || jobError) && <div className="tc-operator-error"><span className="dot red" />{error || jobError}</div>}
        </div>
        <div className="tc-rail-foot">
          <span className="tc-link">{leftView === 'operator' ? 'Drawing operator' : 'Registered catalog'}</span>
          <span className="tc-link muted">{leftView === 'operator' ? 'Claude plans, tools act' : `${tools.length} tools`}</span>
        </div>
      </aside>

      <aside className="tc-rail tc-rail-r" data-cast="tool" style={{ '--rank': 1 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Operations</span>
          <span className="tc-rail-sub">controller state</span>
        </div>
        <div className="tc-rail-tabs" role="tablist" aria-label="Operation panels">
          <button type="button" role="tab" aria-selected={rightView === 'execution'} onClick={() => setRightView('execution')}>Execution</button>
          <button type="button" role="tab" aria-selected={rightView === 'jobs'} onClick={() => setRightView('jobs')}>Jobs <span>{visibleJobCount}</span></button>
          <button type="button" role="tab" aria-selected={rightView === 'versions'} onClick={() => { setRightView('versions'); drawing.actions.loadHistory() }}>Versions <span>{drawing.latest || 1}</span></button>
          <button type="button" role="tab" aria-selected={rightView === 'trust'} onClick={() => { setRightView('trust'); platform.actions.refreshAll() }}>Trust</button>
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
        </div>
        <div className="tc-rail-note">
          <span>The request, approval, job, drawing, and version history remain in this scene.</span>
        </div>
        {jobResult && (
          <div className="tc-result-summary" data-testid="catalog-run-result">
            <span className="dot" />
            <span>{jobResult.tool || selectedCatalogTool?.name || 'Tool'} completed</span>
          </div>
        )}</>}
        {rightView === 'jobs' && (
          <JobRail
            mock={false}
            jobs={jobs}
            currentJob={currentJob}
            inflight={inflight}
            reattaching={reattaching}
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
          </div>
        )}
        </div>
        <div className="tc-rail-foot"><span className="tc-link muted">{phase === 'complete' || phase === 'undone' ? 'Cat oracle, sitting-v1' : 'Contract proof, no APS claim'}</span></div>
      </aside>

      <div className="tc-bar-wrap" data-cast="tool" style={{ '--rank': 2 }}>
        <div className="tc-bar">
          <RoutePanel
            route={route}
            tools={tools}
            running={busy || jobRunning}
            writeEntitled={writeEntitled}
            onConfirmIntent={runCatalogTool}
            onPickAlternative={catalog.actions.pickAlternative}
            onOpenAuthor={() => setError('Open the Author panel to build this capability.')}
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
            <button type="button" className="tc-run" onClick={runRequest} disabled={busy || jobRunning || phase === 'loading'}>Run</button>
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
    </>
  )
}
