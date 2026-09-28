import { useEffect, useMemo, useRef, useState } from 'react'
import { solarRailReason } from '../lib/ribbonClusters.js'
import { solarFlowReadyMap, solarFlowState, solarFlowStepId, solarFlowSteps } from './solarFlowModel.js'
import './solarFlow.css'

const STATUS_TEXT = Object.freeze({
  pending: 'Running',
  failed: 'Failed',
  ready: 'Ready',
  blocked: 'Blocked',
})

function runAnnouncement(item, run) {
  if (run.ok === true) return `${item.label} finished`
  return typeof run.code === 'string' && run.code.length > 0 ? `${item.label} failed: ${run.code}` : `${item.label} failed`
}

// The W1 Solar steps as one ordered rail. Every blocked step says why, with the
// same sentence the ribbon uses; the step to resume carries aria-current. The
// previous readiness map lives here, per drawing, so an invalidated step is a
// comparison against what this drawing showed before, never a stored "done".
// familiesDrawingId names the drawing the families were loaded for; while it
// differs from drawingId (a switch in flight) the families are another
// drawing's, so nothing is recorded and nothing is compared.
export default function SolarFlowRail({
  families, familiesDrawingId, drawingId = null, pendingTool = null, runs, openName = null, openSettingsForm, onOpenStep,
}) {
  const steps = useMemo(() => solarFlowSteps(families), [families])
  const memory = useRef({ drawingId, everReady: Object.create(null) })
  if (memory.current.drawingId !== drawingId) memory.current = { drawingId, everReady: Object.create(null) }
  const current = drawingId !== null && familiesDrawingId === drawingId
  const { items, resumeIndex } = solarFlowState({
    steps, previousReady: current ? memory.current.everReady : null, pendingTool, runs, openSettingsForm,
  })

  useEffect(() => {
    if (!current || memory.current.drawingId !== drawingId) return
    const now = solarFlowReadyMap(steps)
    const everReady = memory.current.everReady
    for (const name of Object.keys(now)) if (now[name] === true) everReady[name] = true
  }, [steps, drawingId, current])

  const [announcement, setAnnouncement] = useState('')
  useEffect(() => { setAnnouncement('') }, [drawingId])
  const lastRuns = useRef(runs)
  useEffect(() => {
    const before = lastRuns.current
    lastRuns.current = runs
    if (!runs || typeof runs !== 'object' || runs === before) return
    let text = ''
    for (const item of items) {
      const run = Object.prototype.hasOwnProperty.call(runs, item.name) ? runs[item.name] : undefined
      const previous = before && Object.prototype.hasOwnProperty.call(before, item.name) ? before[item.name] : undefined
      if (run && typeof run === 'object' && run !== previous) text = runAnnouncement(item, run)
    }
    if (text) setAnnouncement(text)
  }, [runs, items])

  return (
    <nav aria-label="Solar design steps" data-testid="solar-flow-rail" className="solar-flow-rail">
      <ol className="solar-flow-steps">
        {items.map((item, index) => {
          const id = solarFlowStepId(item.name)
          const reason = item.enabled ? '' : solarRailReason(item.row.availability)
          const reasonId = `${id}-reason`
          const rerunId = `${id}-rerun`
          const describedBy = reason ? (item.invalidated ? `${rerunId} ${reasonId}` : reasonId) : undefined
          const resume = index === resumeIndex
          return (
            <li
              key={item.name}
              className="solar-flow-step"
              data-status={item.status}
              data-skip={item.skip ? 'true' : undefined}
              data-invalidated={item.invalidated ? 'true' : undefined}
            >
              <button
                type="button"
                id={id}
                className="solar-flow-button"
                aria-current={resume ? 'step' : undefined}
                aria-expanded={openName === item.name}
                aria-describedby={describedBy}
                disabled={!item.enabled}
                onClick={() => { if (item.enabled && typeof onOpenStep === 'function') onOpenStep(item.row) }}
              >
                <span className="solar-flow-label">{item.label}</span>
                <span className="solar-flow-status">{STATUS_TEXT[item.status]}</span>
                {resume && <span className="solar-flow-resume">Resume here</span>}
              </button>
              {item.skip && <p className="solar-flow-note">An earlier step is not complete yet.</p>}
              {reason && (
                <p className="solar-flow-reason">
                  {item.invalidated && <span id={rerunId}>Needs rerun: </span>}
                  <span id={reasonId}>{reason}</span>
                </p>
              )}
            </li>
          )
        })}
      </ol>
      <p role="status" className="solar-flow-announce" data-testid="solar-flow-announce">{announcement}</p>
    </nav>
  )
}
