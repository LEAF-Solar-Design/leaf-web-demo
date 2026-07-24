import { useCallback, useEffect, useState } from 'react'
import { getDrawingIntake, getSession, redoDrawing, undoDrawing } from '../api.js'
import ConversePanel from '../components/ConversePanel.jsx'
import useConverseSessionController from '../controllers/useConverseSessionController.js'
import useJobController from '../controllers/useJobController.js'
import { navigate } from './router.js'

const CAT_REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'

function phaseLabel(phase) {
  if (phase === 'starting') return 'Starting request'
  if (phase === 'proposal') return 'Waiting for approval'
  if (phase === 'running') return 'Rearranging panels'
  if (phase === 'complete') return 'Cat version ready'
  if (phase === 'undone') return 'Original restored'
  if (phase === 'failed') return 'Request failed'
  return 'Backend ready'
}

export default function ToolCast({ active, onIntakeChange }) {
  const [prompt, setPrompt] = useState(CAT_REQUEST)
  const { sessionId, turns, startTurn, resetCached } = useConverseSessionController({
    drawingId: 'cat-panels',
  })
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState(null)
  const [linkedJobId, setLinkedJobId] = useState(null)
  const [version, setVersion] = useState(1)
  const [panelCount, setPanelCount] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!active) return undefined
    let live = true
    setPhase('loading')
    getSession(false, 'cat')
      .then((data) => {
        if (!live) return
        onIntakeChange(data.intake)
        setPanelCount(data.intake?.polylines?.length || null)
        setPhase('ready')
      })
      .catch(() => {
        if (!live) return
        setError('The drawing backend is unavailable. Start the proof API and reload this surface.')
        setPhase('failed')
      })
    return () => { live = false }
  }, [active, onIntakeChange])

  const seatView = useCallback((view, nextPhase) => {
    if (view?.intake) {
      onIntakeChange(view.intake)
      setPanelCount(view.intake.polylines?.length || null)
    }
    if (Number.isFinite(Number(view?.head))) setVersion(Number(view.head))
    setPhase(nextPhase)
  }, [onIntakeChange])

  const onCompleteVersion = useCallback(async (newVersion) => {
    const drawingId = newVersion?.drawing_id || 'cat-panels'
    try {
      const view = await getDrawingIntake(false, drawingId, 'head')
      seatView(view, 'complete')
    } catch {
      setError('The panel run completed, but its drawing version could not be loaded.')
      setPhase('failed')
    }
  }, [seatView])

  const {
    currentJobId,
    running: jobRunning,
    error: jobError,
    attachJob: attachTrackedJob,
  } = useJobController({
    onCompleteVersion,
    formatError: () => 'The panel run did not produce a readable result.',
  })

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
  }, [busy, jobRunning, prompt, resetCached, startTurn])

  const undo = useCallback(async () => {
    if (busy || jobRunning || version !== 2) return
    setBusy(true)
    setError(null)
    try {
      seatView(await undoDrawing(false, 'cat-panels'), 'undone')
    } catch {
      setError('Undo failed. The current drawing version is unchanged.')
    } finally {
      setBusy(false)
    }
  }, [busy, jobRunning, seatView, version])

  const redo = useCallback(async () => {
    if (busy || jobRunning || version !== 1 || phase !== 'undone') return
    setBusy(true)
    setError(null)
    try {
      seatView(await redoDrawing(false, 'cat-panels'), 'complete')
    } catch {
      setError('Redo failed. The current drawing version is unchanged.')
    } finally {
      setBusy(false)
    }
  }, [busy, jobRunning, phase, seatView, version])

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
        <span className="tc-version" data-testid="version-head">Version {version}</span>
        <button type="button" className="tc-back" onClick={() => navigate('/')}>Back to the site</button>
        <span className="key">Esc</span>
      </div>

      <aside className="tc-rail tc-rail-l tc-operator-rail" data-cast="tool" style={{ '--rank': 0 }} data-testid="operator-surface">
        <div className="tc-rail-head">
          <span className="tc-rail-title">Operator request</span>
          <span className="tc-rail-sub">Claude plans, tools act</span>
        </div>
        {sessionId ? (
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
        )}
        {(error || jobError) && <div className="tc-operator-error"><span className="dot red" />{error || jobError}</div>}
        <div className="tc-rail-foot">
          <span className="tc-link">Drawing operator</span>
          <span className="tc-link muted">Deterministic proof</span>
        </div>
      </aside>

      <aside className="tc-rail tc-rail-r" data-cast="tool" style={{ '--rank': 1 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Execution</span>
          <span className="tc-rail-sub">live</span>
        </div>
        <div className="tc-events">
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
            <span className={version === 2 ? 'dot' : 'dot hollow'} />
            <span className="tc-event-text">Version head</span>
            <span className="tc-event-time">v{version}</span>
          </div>
        </div>
        <div className="tc-version-actions">
          <button type="button" className="tc-bar-chip" onClick={undo} disabled={busy || jobRunning || version !== 2}>Undo</button>
          <button type="button" className="tc-bar-chip" onClick={redo} disabled={busy || jobRunning || phase !== 'undone'}>Redo</button>
        </div>
        <div className="tc-rail-note">
          <span>The request, approval, job, drawing, and version history remain in this scene.</span>
        </div>
        <div className="tc-rail-foot"><span className="tc-link muted">Cat oracle, sitting-v1</span></div>
      </aside>

      <div className="tc-bar-wrap" data-cast="tool" style={{ '--rank': 2 }}>
        <div className="tc-bar">
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
