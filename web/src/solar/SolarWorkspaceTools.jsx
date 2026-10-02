import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import { createSolarCombinerIntakeClient } from './solarCombinerIntakeClient.js'
import SolarTerrainPanel from './SolarTerrainPanel.jsx'
import { createSolarTerrainClient } from './solarTerrainClient.js'
import './solarFlow.css'

const FLOW_PANELS = Object.freeze({
  rooftop: Object.freeze(['combiner-intake']),
  'ground-electrical': Object.freeze([]),
  'ground-physical': Object.freeze(['landxml', 'terrain']),
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

export const TERRAIN_WORKSPACE_REASONS = Object.freeze({
  run_in_progress: 'A run is in progress, so wait for it to finish',
  landxml_importing: 'The LandXML file is being imported',
  checkout_required: 'Take the drawing checkout before changing terrain previews',
})

function withoutCheckoutCapability(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value
  const proto = Object.getPrototypeOf(value)
  if (proto !== Object.prototype && proto !== null) return value
  const copy = {}
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key === 'string' && key.toLowerCase() === 'x-checkout-capability') continue
    Object.defineProperty(copy, key, { value: value[key], enumerable: true, configurable: true, writable: true })
  }
  return copy
}

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
  const terrainClient = useMemo(() => createSolarTerrainClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withoutCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }),
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
      terrainClient={terrainClient}
      combinerClient={combinerClient}
      drawingVersion={drawingVersion}
      onDrawingVersionChanged={onDrawingVersionChanged}
      onRunPlacement={onRunPlacement}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function ToolsForScope({ drawingId, projectId, drawingVersion, flow, checkoutHeld, busy, client,
  combinerClient, terrainClient, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement }) {
  const [openPanel, setOpenPanel] = useState(null)
  const [intake, setIntake] = useState(null)
  const intakeRef = useRef(null)
  const refreshValue = useRef(null)
  const [phase, setPhase] = useState('idle')
  const [generation, setGeneration] = useState(0)
  const [notStaged, setNotStaged] = useState(false)
  const [physicalHead, setPhysicalHead] = useState(null)
  const [terrainHeadSignal, setTerrainHeadSignal] = useState(null)
  const [landxmlPending, setLandxmlPending] = useState(false)
  const landxmlRequest = useRef(null)
  const [announcement, setAnnouncement] = useState('')
  const trigger = useRef(null)
  const panel = useRef(null)
  const terrainTrigger = useRef(null)
  const terrainPanel = useRef(null)
  const terrainHeading = useRef(null)
  const combinerTrigger = useRef(null)
  const combinerPanel = useRef(null)
  const activeScope = useRef(false)
  const pendingImport = useRef(null)
  const hasLandxml = !!drawingId && FLOW_PANELS[flow]?.includes('landxml') === true
  const hasTerrain = !!drawingId && FLOW_PANELS[flow]?.includes('terrain') === true
  const hasCombiner = !!drawingId && projectId === null && FLOW_PANELS[flow]?.includes('combiner-intake') === true

  useLayoutEffect(() => {
    activeScope.current = true
    return () => {
      activeScope.current = false
      pendingImport.current = null
      landxmlRequest.current = null
      intakeRef.current = null
    }
  }, [])

  useLayoutEffect(() => {
    setOpenPanel(null)
    pendingImport.current = null
    landxmlRequest.current = null
    setLandxmlPending(false)
  }, [flow])

  useLayoutEffect(() => {
    if (openPanel === 'landxml' && hasLandxml) panel.current?.querySelector('input[type="file"]')?.focus()
    if (openPanel === 'terrain' && hasTerrain) terrainHeading.current?.focus()
    if (openPanel === 'combiner-intake' && hasCombiner) combinerPanel.current?.querySelector('input[type="file"]')?.focus()
  }, [openPanel, hasLandxml, hasCombiner, hasTerrain])

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
  const terrainGate = busy ? 'run_in_progress' : landxmlPending ? 'landxml_importing' : checkoutHeld !== true ? 'checkout_required' : null

  const upload = useMemo(() => (request) => {
    const started = { drawingId, projectId }
    pendingImport.current = started
    const token = {}
    landxmlRequest.current = token
    setLandxmlPending(true)
    const settled = () => {
      if (activeScope.current && landxmlRequest.current === token) {
        landxmlRequest.current = null
        setLandxmlPending(false)
      }
    }
    let promise
    try {
      promise = client.uploadLandxml(request)
    } catch (error) {
      settled()
      throw error
    }
    promise.then(settled, settled)
    return promise
  }, [client, drawingId, projectId])

  const scopeCombinerClient = useMemo(() => ({
    importCombinerIntake: (request) => {
      setNotStaged(false)
      return combinerClient.importCombinerIntake(request)
    },
  }), [combinerClient])

  const scopeTerrainClient = useMemo(() => ({
    getTerrain: (options) => terrainClient.getTerrain(options).then((result) => {
      try {
        const head = result?.value?.head
        if (activeScope.current && options?.signal?.aborted !== true && result?.ok === true && head?.drawing_id === drawingId
          && result.value.terrain?.drawing_id === drawingId && Number.isInteger(head.index) && head.index >= 0) {
          setPhysicalHead((current) => activeScope.current
            && (!Number.isInteger(current?.index) || head.index >= current.index) ? head : current)
        }
      } catch {
        // Observing a read must never change the client's answer.
      }
      return result
    }),
    runTerrainOperation: terrainClient.runTerrainOperation,
  }), [terrainClient, drawingId])

  function onImported(value) {
    const started = pendingImport.current
    if (!activeScope.current || !started || started.drawingId !== drawingId
      || started.projectId !== projectId || value?.drawing_id !== drawingId) return
    pendingImport.current = null
    setPhysicalHead(value.head)
    if (typeof value.head?.state?.artifact_id === 'string') setTerrainHeadSignal(value.head.state.artifact_id)
    setAnnouncement(value.created
      ? 'Terrain imported for this drawing.'
      : 'This terrain was already imported, so nothing changed.')
    onPhysicalHeadChanged?.(value)
  }

  function onTerrainChanged(result) {
    if (!activeScope.current || (result?.drawing_id !== undefined && result.drawing_id !== drawingId)) return
    setPhysicalHead(result.head)
    onPhysicalHeadChanged?.(result)
  }

  if (!hasLandxml && !hasCombiner && !hasTerrain) return null
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
              landxmlRequest.current = null
              setLandxmlPending(false)
              setOpenPanel(null)
              trigger.current?.focus()
            }} />
        </div>
      )}
      {hasTerrain && <button type="button" ref={terrainTrigger} aria-expanded={openPanel === 'terrain'}
        disabled={landxmlPending} onClick={() => setOpenPanel('terrain')}>Terrain preview</button>}
      {hasTerrain && landxmlPending && <p data-testid="solar-terrain-reason">{TERRAIN_WORKSPACE_REASONS.landxml_importing}</p>}
      {hasTerrain && openPanel === 'terrain' && (
        <div className="solar-workspace-panel" ref={terrainPanel}>
          <h3 tabIndex={-1} ref={terrainHeading}>Terrain preview</h3>
          <SolarTerrainPanel drawingId={drawingId} projectId={projectId} client={scopeTerrainClient}
            headSignal={terrainHeadSignal} disabled={terrainGate !== null}
            disabledReason={terrainGate === null ? undefined : TERRAIN_WORKSPACE_REASONS[terrainGate]}
            onPhysicalHeadChanged={onTerrainChanged} />
          <button type="button" onClick={() => {
            setOpenPanel(null)
            terrainTrigger.current?.focus()
          }}>Close</button>
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
