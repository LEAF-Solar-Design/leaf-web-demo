import { useEffect, useMemo, useRef, useState } from 'react'
import { solarRailReason } from '../lib/ribbonClusters.js'
import {
  DEFAULT_SOLAR_FLOW, SOLAR_FLOW_MATURITY_NOTES, solarFlowId, solarFlowOptionLabel,
  solarFlowOptions, solarFlowReadyMap, solarFlowSelect, solarFlowState, solarFlowStepId,
  deriveProjectSolarFlowState, projectSolarFlowScope, projectSolarFlowRunsFor, projectSolarFlowReason,
} from './solarFlowModel.js'
import { sameSolarDrawingBinding } from './solarDrawingBinding.js'
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
export default function SolarFlowRail(props) {
  // Project mode is the forms' rule: an own solarBinding prop, or a non-null projectId without one.
  if (Object.prototype.hasOwnProperty.call(props, 'solarBinding') || props.projectId != null) return <ProjectSolarFlowRail {...props} />
  return <StandaloneSolarFlowRail {...props} />
}

function ProjectSolarFlowRail({ solarBinding, authorityKey, projectClient, projectCatalog, projectRunMemory, projectPending,
  openName = null, openSettingsForm, onOpenStep, onFlowChange, workspacePanelsByFlow = {} }) {
  const [flowId, setFlowId] = useState(DEFAULT_SOLAR_FLOW)
  const [, refresh] = useState(0)
  useEffect(() => {
    if (typeof projectClient?.subscribe !== 'function') return undefined
    return projectClient.subscribe(() => refresh((value) => value + 1))
  }, [projectClient])
  const scope = projectSolarFlowScope(solarBinding, authorityKey)
  function catalogCurrent() {
    try {
      const snapshot = projectClient?.getSnapshot?.()
      return !!scope && projectCatalog?.client === projectClient && projectCatalog?.authorityKey === authorityKey
        && sameSolarDrawingBinding(projectCatalog?.binding, solarBinding)
        && sameSolarDrawingBinding(snapshot?.binding, solarBinding) && snapshot?.authorityKey === authorityKey
        && Array.isArray(projectCatalog?.families) && typeof projectCatalog?.isCurrent === 'function'
        && projectCatalog.isCurrent() === true
    } catch { return false }
  }
  const current = catalogCurrent()
  const families = current ? projectCatalog.families : []
  const options = solarFlowOptions(families, undefined, workspacePanelsByFlow)
  const selection = solarFlowSelect(families, flowId, undefined, workspacePanelsByFlow)
  // Keep contradictory duplicates for project admission even though the legacy selector picks the first row.
  const selectedNames = new Set(selection.steps.map((row) => row.name))
  const duplicates = families.flatMap((family) => Array.isArray(family?.capabilities) ? family.capabilities : [])
    .filter((row) => selectedNames.has(row?.name) && !selection.steps.includes(row))
  const runs = projectSolarFlowRunsFor(projectRunMemory, solarBinding, authorityKey)
  const memory = useRef(null)
  if (!memory.current || memory.current.artifactKey !== scope?.artifactKey) {
    memory.current = { artifactKey: scope?.artifactKey, everReady: Object.create(null), runs, announcement: '' }
  }
  const saved = memory.current
  const { items, resumeIndex } = deriveProjectSolarFlowState({ steps: [...selection.steps, ...duplicates], current,
    previousReady: saved.everReady, pending: projectPending, runs, binding: solarBinding, authorityKey, openSettingsForm })
  const [, announce] = useState(0)
  useEffect(() => {
    if (memory.current !== saved || !catalogCurrent()) return
    for (const item of items) if (item.ready) saved.everReady[item.name] = true
  }, [projectCatalog, solarBinding, authorityKey, projectClient, selection.flow, current])
  useEffect(() => {
    if (memory.current !== saved || saved.runs === runs) return
    let text = ''
    for (const item of items) {
      const run = runs[item.name]
      if (run && run !== saved.runs?.[item.name]) text = runAnnouncement(item, run)
    }
    saved.runs = runs
    if (text) { saved.announcement = text; announce((value) => value + 1) }
  }, [runs, saved, items])
  return (
    <nav aria-label="Solar design steps" data-testid="solar-flow-rail" className="solar-flow-rail" data-flow={selection.flow}>
      <label className="solar-flow-picker" htmlFor="solar-flow-select">Solar flow</label>
      <select id="solar-flow-select" data-testid="solar-flow-select" value={selection.flow} onChange={(event) => {
        const next = solarFlowId(event.target.value)
        if (next !== flowId) {
          setFlowId(next)
          saved.announcement = ''
          onFlowChange?.(next)
        }
      }}>
        {options.map((option) => <option key={option.id} value={option.id}>{solarFlowOptionLabel(option)}</option>)}
      </select>
      {!scope ? <p role="status" data-testid="project-solar-flow-reason">{projectSolarFlowReason('binding_invalid')}</p>
        : !current ? <p role="status" data-testid="project-solar-flow-reason">{projectSolarFlowReason('catalog_pending')}</p>
        : <>
          {selection.maturity !== 'production' && <p className="solar-flow-maturity" data-testid="solar-flow-maturity">{SOLAR_FLOW_MATURITY_NOTES[selection.maturity]}</p>}
          {selection.available ? <ol className="solar-flow-steps">
            {items.map((item, index) => {
              const id = solarFlowStepId(item.name)
              const reason = projectSolarFlowReason(item.status === 'review' ? 'review_required' : !item.enabled ? 'preview_refused' : null)
              return <li key={item.name} className="solar-flow-step" data-status={item.status}
                data-skip={item.skip ? 'true' : undefined} data-invalidated={item.invalidated ? 'true' : undefined}>
                <button type="button" id={id} className="solar-flow-button" disabled={!item.enabled}
                  aria-current={index === resumeIndex ? 'step' : undefined} aria-expanded={openName === item.name}
                  aria-describedby={reason ? `${id}-reason` : undefined}
                  onClick={() => { if (item.enabled && catalogCurrent() && typeof onOpenStep === 'function') onOpenStep(item.row) }}>
                  <span className="solar-flow-label">{item.label}</span>
                  <span className="solar-flow-status">{item.status === 'review' ? 'Review inputs' : STATUS_TEXT[item.status]}</span>
                  {index === resumeIndex && <span className="solar-flow-resume">Resume here</span>}
                </button>
                {item.skip && <p className="solar-flow-note">An earlier step is not complete yet.</p>}
                {reason && <p id={`${id}-reason`} className="solar-flow-reason">
                  {item.invalidated && <span>Needs rerun: </span>}{reason}{item.codes[0] ? ` (${item.codes[0]})` : ''}
                </p>}
              </li>
            })}
          </ol> : <div className="solar-flow-unavailable" data-testid="solar-flow-unavailable">
            <p id="solar-flow-unavailable-reason">{selection.reason}</p>
            {selection.missing.length > 0 && <ul aria-label="Unavailable stages">{selection.missing.map((label) => <li key={label}>{label}</li>)}</ul>}
          </div>}
        </>}
      <p role="status" className="solar-flow-announce" data-testid="solar-flow-announce">{saved.announcement}</p>
    </nav>
  )
}

function StandaloneSolarFlowRail({
  families, familiesDrawingId, drawingId = null, pendingTool = null, runs, openName = null, openSettingsForm, onOpenStep, onFlowChange,
  workspacePanelsByFlow = {},
}) {
  const [flowId, setFlowId] = useState(DEFAULT_SOLAR_FLOW)
  const options = useMemo(() => solarFlowOptions(families, undefined, workspacePanelsByFlow), [families, workspacePanelsByFlow])
  const selection = useMemo(() => solarFlowSelect(families, flowId, undefined, workspacePanelsByFlow), [families, flowId, workspacePanelsByFlow])
  const steps = selection.steps
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
    <nav aria-label="Solar design steps" data-testid="solar-flow-rail" className="solar-flow-rail" data-flow={selection.flow}>
      <label className="solar-flow-picker" htmlFor="solar-flow-select">Solar flow</label>
      <select id="solar-flow-select" data-testid="solar-flow-select" value={selection.flow}
        aria-describedby={selection.available ? undefined : 'solar-flow-unavailable-reason'}
        onChange={(event) => {
          const next = solarFlowId(event.target.value)
          if (next !== flowId) {
            setFlowId(next)
            setAnnouncement('')
            if (typeof onFlowChange === 'function') onFlowChange(next)
          }
        }}>
        {options.map((option) => <option key={option.id} value={option.id}>{solarFlowOptionLabel(option)}</option>)}
      </select>
      {selection.maturity !== 'production' && (
        <p className="solar-flow-maturity" data-testid="solar-flow-maturity">{SOLAR_FLOW_MATURITY_NOTES[selection.maturity]}</p>
      )}
      {selection.available ? <ol className="solar-flow-steps">
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
      </ol> : (
        <div className="solar-flow-unavailable" data-testid="solar-flow-unavailable">
          <p id="solar-flow-unavailable-reason" className="solar-flow-reason">{selection.reason}</p>
          {selection.missing.length > 0 && (
            <ul className="solar-flow-missing" aria-label="Unavailable stages">
              {selection.missing.map((label) => <li key={label}>{label}</li>)}
            </ul>
          )}
        </div>
      )}
      <p role="status" className="solar-flow-announce" data-testid="solar-flow-announce">{announcement}</p>
    </nav>
  )
}
