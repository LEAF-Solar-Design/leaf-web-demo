import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import { createSolarCombinerIntakeClient } from './solarCombinerIntakeClient.js'
import './solarFlow.css'

const FLOW_PANELS = Object.freeze({
  rooftop: Object.freeze(['combiner-intake']),
  'ground-electrical': Object.freeze([]),
  'ground-physical': Object.freeze(['landxml']),
  'solaredge-import': Object.freeze([]),
  'pvcase-tutorial': Object.freeze([]),
})

export const COMBINER_WORKSPACE_REASONS = Object.freeze({
  run_in_progress: 'A run is in progress, so wait for it to finish',
  checkout_required: 'Take the drawing checkout before importing the combiner intake',
  refreshing: 'The drawing is refreshing to the imported version',
  refresh_failed: 'The drawing did not refresh to the imported version',
  not_staged: 'Combiner placement was not staged',
})

export default function SolarWorkspaceTools({
  drawingId, projectId = null, drawingVersion, flow, checkoutHeld, busy,
  getCheckoutCapability, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement, transport,
}) {
  const capability = useRef(getCheckoutCapability)
  capability.current = getCheckoutCapability
  const client = useMemo(() => createSolarLandxmlClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: transport?.headers ?? (() => ({ 'X-Tenant-Id': config.tenant, ...authHeaders() })),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])
  const combinerClient = useMemo(() => createSolarCombinerIntakeClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => {
      const base = transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }
      const cap = capability.current?.()
      return { ...base, ...(typeof cap === 'string' && cap.length > 0 ? { 'X-Checkout-Capability': cap } : {}) }
    },
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])

  return (
    <ToolsForScope
      key={JSON.stringify([drawingId ?? null, projectId ?? null])}
      drawingId={drawingId}
      projectId={projectId}
      flow={flow}
      checkoutHeld={checkoutHeld}
      busy={busy}
      client={client}
      combinerClient={combinerClient}
      drawingVersion={drawingVersion}
      onDrawingVersionChanged={onDrawingVersionChanged}
      onRunPlacement={onRunPlacement}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function ToolsForScope({ drawingId, projectId, drawingVersion, flow, checkoutHeld, busy, client,
  combinerClient, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement }) {
  const [openPanel, setOpenPanel] = useState(null)
  const [intake, setIntake] = useState(null)
  const intakeRef = useRef(null)
  const refreshValue = useRef(null)
  const [phase, setPhase] = useState('idle')
  const [generation, setGeneration] = useState(0)
  const [notStaged, setNotStaged] = useState(false)
  const [physicalHead, setPhysicalHead] = useState(null)
  const [announcement, setAnnouncement] = useState('')
  const trigger = useRef(null)
  const panel = useRef(null)
  const combinerTrigger = useRef(null)
  const combinerPanel = useRef(null)
  const activeScope = useRef(false)
  const pendingImport = useRef(null)
  const hasLandxml = !!drawingId && FLOW_PANELS[flow]?.includes('landxml') === true
  const hasCombiner = !!drawingId && projectId === null && FLOW_PANELS[flow]?.includes('combiner-intake') === true

  useLayoutEffect(() => {
    activeScope.current = true
    return () => {
      activeScope.current = false
      pendingImport.current = null
      intakeRef.current = null
    }
  }, [])

  useLayoutEffect(() => {
    setOpenPanel(null)
  }, [flow])

  useLayoutEffect(() => {
    if (openPanel === 'landxml' && hasLandxml) panel.current?.querySelector('input[type="file"]')?.focus()
    if (openPanel === 'combiner-intake' && hasCombiner) combinerPanel.current?.querySelector('input[type="file"]')?.focus()
  }, [openPanel, hasLandxml, hasCombiner])

  useLayoutEffect(() => {
    if (!intake) return
    if (drawingVersion === intake.version) {
      setPhase('idle')
    } else if (phase === 'idle' || drawingVersion > intake.version) {
      intakeRef.current = null
      refreshValue.current = null
      setIntake(null)
      setPhase('idle')
      setNotStaged(false)
      setGeneration((previous) => previous + 1)
    }
  }, [drawingVersion, intake, phase])

  function refreshDrawing(value) {
    const current = intakeRef.current
    const isCurrent = () => activeScope.current && intakeRef.current === current
    setPhase('refreshing')
    let result
    try {
      result = onDrawingVersionChanged?.({ drawing_id: value.drawing_id, version: value.version,
        parent: value.parent_version }, undefined, { isCurrent, announce: value.created })
    } catch {
      if (isCurrent()) setPhase('failed')
      return
    }
    Promise.resolve(result).then((accepted) => {
      if (isCurrent() && accepted === false) setPhase('failed')
    }, () => { if (isCurrent()) setPhase('failed') })
  }

  function onStored(value) {
    if (!activeScope.current || value.drawing_id !== drawingId) return
    const next = { version: value.version, graphRev: value.graph_rev }
    intakeRef.current = next
    refreshValue.current = value
    setIntake(next)
    setNotStaged(false)
    if (value.version === drawingVersion) setPhase('idle')
    else refreshDrawing(value)
  }

  function runPlacement(params) {
    setNotStaged(false)
    if (!activeScope.current || !intake || phase !== 'idle' || drawingVersion !== intake.version
      || params.expected_rev !== intake.graphRev || checkoutHeld !== true || busy) return
    if (onRunPlacement?.(params) === false) setNotStaged(true)
  }

  const disabledReason = busy ? 'run_in_progress' : checkoutHeld !== true ? 'checkout_required'
    : phase === 'refreshing' ? 'refreshing' : phase === 'failed' ? 'refresh_failed' : null
  const reason = disabledReason ?? (notStaged ? 'not_staged' : null)

  const upload = useMemo(() => (request) => {
    const started = { drawingId, projectId }
    pendingImport.current = started
    return client.uploadLandxml(request)
  }, [client, drawingId, projectId])

  const scopeCombinerClient = useMemo(() => ({
    importCombinerIntake: (request) => {
      setNotStaged(false)
      return combinerClient.importCombinerIntake(request)
    },
  }), [combinerClient])

  function onImported(value) {
    const started = pendingImport.current
    if (!activeScope.current || !started || started.drawingId !== drawingId
      || started.projectId !== projectId || value?.drawing_id !== drawingId) return
    pendingImport.current = null
    setPhysicalHead(value.head)
    setAnnouncement(value.created
      ? 'Terrain imported for this drawing.'
      : 'This terrain was already imported, so nothing changed.')
    onPhysicalHeadChanged?.(value)
  }

  if (!hasLandxml && !hasCombiner) return null
  return (
    <section className="solar-workspace-tools" aria-label="Solar workspace tools"
      data-physical-head-index={physicalHead?.index ?? undefined}>
      {hasLandxml && <button type="button" ref={trigger} aria-expanded={openPanel === 'landxml'} onClick={() => setOpenPanel('landxml')}>
        Import LandXML terrain
      </button>}
      {hasLandxml && openPanel === 'landxml' && (
        <div className="solar-workspace-panel" ref={panel}>
          <SolarLandxmlUpload drawingId={drawingId} projectId={projectId} upload={upload}
            checkoutHeld={checkoutHeld} busy={busy} onImported={onImported}
            onClose={() => {
              pendingImport.current = null
              setOpenPanel(null)
              trigger.current?.focus()
            }} />
        </div>
      )}
      {hasCombiner && <button type="button" ref={combinerTrigger} aria-expanded={openPanel === 'combiner-intake'}
        onClick={() => setOpenPanel('combiner-intake')}>Import combiner intake</button>}
      {hasCombiner && openPanel === 'combiner-intake' && (
        <div className="solar-workspace-panel" ref={combinerPanel}>
          <SolarCombinerIntake key={generation} drawingId={drawingId} projectId={projectId}
            client={scopeCombinerClient} onStored={onStored} onRunPlacement={runPlacement} disabled={!!disabledReason} />
          {reason && <p data-testid="solar-combiner-reason">{COMBINER_WORKSPACE_REASONS[reason]}</p>}
          {phase === 'failed' && <button type="button" onClick={() => refreshDrawing(refreshValue.current)}>Refresh drawing</button>}
          <button type="button" onClick={() => {
            setOpenPanel(null)
            combinerTrigger.current?.focus()
          }}>Close</button>
        </div>
      )}
      <p className="solar-workspace-announce" aria-live="polite" aria-atomic="true">{announcement}</p>
    </section>
  )
}
