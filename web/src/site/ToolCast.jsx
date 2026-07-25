import { useCallback, useEffect, useState } from 'react'
import { getDrawingIntake, getJob, getSession, redoDrawing, undoDrawing } from '../api.js'
import ConversePanel from '../components/ConversePanel.jsx'
import { ensureSession, postMessage, resetSession } from '../converse.js'
import { navigate } from './router.js'

const CAT_REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function phaseLabel(phase) {
  if (phase === 'starting') return 'Starting request'
  if (phase === 'proposal') return 'Waiting for approval'
  if (phase === 'running') return 'Rearranging panels'
  if (phase === 'complete') return '3D cat ready'
  if (phase === 'undone') return 'Original restored'
  if (phase === 'failed') return 'Request failed'
  return 'Backend ready'
}

export default function ToolCast({ active, onIntakeChange, onViewModeChange }) {
  const [prompt, setPrompt] = useState(CAT_REQUEST)
  const [sessionId, setSessionId] = useState(null)
  const [turns, setTurns] = useState([])
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState(null)
  const [jobId, setJobId] = useState(null)
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
        onViewModeChange('flat')
        setPanelCount(data.intake?.polylines?.length || null)
        setPhase('ready')
      })
      .catch(() => {
        if (!live) return
        setError('The drawing backend is unavailable. Start the proof API and reload this surface.')
        setPhase('failed')
      })
    return () => { live = false }
  }, [active, onIntakeChange, onViewModeChange])

  const seatView = useCallback((view, nextPhase) => {
    if (view?.intake) {
      onIntakeChange(view.intake)
      setPanelCount(view.intake.polylines?.length || null)
    }
    if (Number.isFinite(Number(view?.head))) setVersion(Number(view.head))
    onViewModeChange(nextPhase === 'complete' ? 'panel-sculpture' : 'flat')
    setPhase(nextPhase)
  }, [onIntakeChange, onViewModeChange])

  const attachJob = useCallback(async (linkedJobId) => {
    if (!linkedJobId) return
    setJobId(linkedJobId)
    setPhase('running')
    setError(null)
    try {
      let job = null
      for (let attempt = 0; attempt < 30; attempt += 1) {
        job = await getJob(linkedJobId)
        if (job?.status === 'complete' || job?.status === 'failed') break
        await wait(400)
      }
      if (job?.status !== 'complete' || !job?.result?.ok) throw new Error('job did not complete')
      const drawingId = job.result?.result?.new_version?.drawing_id || 'cat-panels'
      const view = await getDrawingIntake(false, drawingId, 'head')
      seatView(view, 'complete')
    } catch {
      setError('The panel run did not produce a readable drawing version.')
      setPhase('failed')
    }
  }, [seatView])

  const runRequest = useCallback(async () => {
    const text = prompt.trim()
    if (!text || busy) return
    setBusy(true)
    setError(null)
    setPhase('starting')
    try {
      const drawingId = 'cat-panels'
      const session = await ensureSession(drawingId)
      setSessionId(session.session_id)
      const turn = await postMessage(session.session_id, {
        text,
        classifier_hint: { lane: 'build', tool: null, confidence: 0.42 },
      })
      setTurns((current) => [...current, { turnId: turn.turn_id, text }])
      setPhase('proposal')
    } catch {
      resetSession('cat-panels')
      setError('The assistant could not start this request. The drawing is unchanged.')
      setPhase('failed')
    } finally {
      setBusy(false)
    }
  }, [busy, prompt])

  const undo = useCallback(async () => {
    if (busy || version !== 2) return
    setBusy(true)
    setError(null)
    try {
      seatView(await undoDrawing(false, 'cat-panels'), 'undone')
    } catch {
      setError('Undo failed. The current drawing version is unchanged.')
    } finally {
      setBusy(false)
    }
  }, [busy, seatView, version])

  const redo = useCallback(async () => {
    if (busy || version !== 1 || phase !== 'undone') return
    setBusy(true)
    setError(null)
    try {
      seatView(await redoDrawing(false, 'cat-panels'), 'complete')
    } catch {
      setError('Redo failed. The current drawing version is unchanged.')
    } finally {
      setBusy(false)
    }
  }, [busy, phase, seatView, version])

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
        {error && <div className="tc-operator-error"><span className="dot red" />{error}</div>}
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
            <span className={jobId ? 'dot' : 'dot hollow'} />
            <span className="tc-event-text">Tool job</span>
            <span className="tc-event-time">{jobId ? jobId.slice(0, 8) : 'pending'}</span>
          </div>
          <div className="tc-event">
            <span className={version === 2 ? 'dot' : 'dot hollow'} />
            <span className="tc-event-text">Version head</span>
            <span className="tc-event-time">v{version}</span>
          </div>
        </div>
        <div className="tc-version-actions">
          <button type="button" className="tc-bar-chip" onClick={undo} disabled={busy || version !== 2}>Undo</button>
          <button type="button" className="tc-bar-chip" onClick={redo} disabled={busy || phase !== 'undone'}>Redo</button>
        </div>
        {phase === 'complete' && (
          <div className="tc-camera-controls" data-testid="camera-controls">
            Drag to orbit · right-drag to pan · scroll to zoom
          </div>
        )}
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
            <button type="button" className="tc-run" onClick={runRequest} disabled={busy || phase === 'loading'}>Run</button>
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
