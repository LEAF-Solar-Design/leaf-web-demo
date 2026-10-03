import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, getDrawingIntake, noteUnauthorized } from '../api.js'
import { createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import { createSolarCombinerIntakeClient } from './solarCombinerIntakeClient.js'
import SolarTerrainPanel from './SolarTerrainPanel.jsx'
import { createSolarTerrainClient } from './solarTerrainClient.js'
import SolarTrackerRowsPanel, { TRACKER_ROWS_PANEL_REASONS } from './SolarTrackerRowsPanel.jsx'
import { createSolarTrackerRowsClient } from './solarTrackerRowsClient.js'
import './solarFlow.css'

const FLOW_PANELS = Object.freeze({
  rooftop: Object.freeze(['combiner-intake']),
  'ground-electrical': Object.freeze([]),
  'ground-physical': Object.freeze(['landxml', 'terrain', 'tracker-rows']),
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

function withCurrentCheckoutCapability(value, getCapability) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value
  const proto = Object.getPrototypeOf(value)
  if (proto !== Object.prototype && proto !== null) return value
  const copy = {}
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key === 'string' && key.toLowerCase() === 'x-checkout-capability') continue
    if (typeof key !== 'string' || typeof value[key] !== 'string') return value
    Object.defineProperty(copy, key, { value: value[key], enumerable: true, configurable: true, writable: true })
  }
  const cap = getCapability?.()
  if (typeof cap === 'string' && cap.length > 0) copy['X-Checkout-Capability'] = cap
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
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])
  const terrainClient = useMemo(() => createSolarTerrainClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
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
  const trackerRowsClient = useMemo(() => createSolarTrackerRowsClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])
  const readIntake = useMemo(() => transport?.readIntake ?? ((id, version) => getDrawingIntake(false, id, version)), [transport])

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
      trackerRowsClient={trackerRowsClient}
      readIntake={readIntake}
      drawingVersion={drawingVersion}
      onDrawingVersionChanged={onDrawingVersionChanged}
      onRunPlacement={onRunPlacement}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function ToolsForScope({ drawingId, projectId, drawingVersion, flow, checkoutHeld, busy, client,
  combinerClient, terrainClient, trackerRowsClient, readIntake, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement }) {
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
  const [writers, setWriters] = useState({})
  const writerTokens = useRef({})
  const [trackerMounted, setTrackerMounted] = useState(false)
  const [trackerHeadSignal, setTrackerHeadSignal] = useState(null)
  const trackerTrigger = useRef(null)
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
  const hasTrackerRows = !!drawingId && FLOW_PANELS[flow]?.includes('tracker-rows') === true

  // Each kind holds the SET of its in-flight requests (the key exists only while the set is non-empty), so a
  // second request of one kind can never release the interlock while the first is still unresolved.
  function guardedWrite(kind, request, send, code) {
    const held = writerTokens.current
    const inFlight = (name) => (held[name]?.size ?? 0) > 0
    if (!activeScope.current || (kind === 'tracker'
      ? Object.keys(held).some(inFlight) || phase === 'refreshing' || phase === 'failed'
      : inFlight('tracker'))) return Promise.resolve({ ok: false, code, retryable: false, status: null })
    const token = {}
    ;(held[kind] ??= new Set()).add(token)
    setWriters((current) => ({ ...current, [kind]: true }))
    let released = false
    const release = () => {
      if (released) return
      released = true
      request.signal?.removeEventListener('abort', release)
      const tokens = writerTokens.current[kind]
      if (!tokens?.delete(token) || tokens.size > 0) return
      delete writerTokens.current[kind]
      if (activeScope.current) setWriters((current) => ({ ...current, [kind]: false }))
    }
    if (kind !== 'tracker') request.signal?.addEventListener('abort', release, { once: true })
    let result
    try { result = send(request) } catch (error) { release(); throw error }
    return Promise.resolve(result).then((answer) => { release(); return answer }, (error) => { release(); throw error })
  }

  useLayoutEffect(() => {
    activeScope.current = true
    return () => {
      activeScope.current = false
      pendingImport.current = null
      landxmlRequest.current = null
      intakeRef.current = null
      writerTokens.current = {}
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
    if (!activeScope.current || writerTokens.current.tracker || !intake || phase !== 'idle' || drawingVersion !== intake.version
      || params.expected_rev !== intake.graphRev || checkoutHeld !== true || busy) return
    if (onRunPlacement?.(params) === false) setNotStaged(true)
  }

  const disabledReason = writers.tracker ? 'tracker_pending' : busy ? 'run_in_progress' : checkoutHeld !== true ? 'checkout_required'
    : phase === 'refreshing' ? 'refreshing' : phase === 'failed' ? 'refresh_failed' : null
  const reason = disabledReason ?? (notStaged ? 'not_staged' : null)
  const terrainGate = writers.tracker ? 'tracker_pending' : busy ? 'run_in_progress' : landxmlPending ? 'landxml_importing' : checkoutHeld !== true ? 'checkout_required' : null
  const trackerBlockedReason = writers.landxml ? 'landxml_pending' : writers.terrain ? 'terrain_pending'
    : writers.combiner ? 'combiner_pending' : phase === 'refreshing' || phase === 'failed' ? 'drawing_refresh' : null

  const upload = useMemo(() => (request) => {
    if (writerTokens.current.tracker) return Promise.resolve({ ok: false, code: 'LANDXML_CLIENT_REQUEST_INVALID', retryable: false })
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
      promise = guardedWrite('landxml', request, client.uploadLandxml, 'LANDXML_CLIENT_REQUEST_INVALID')
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
      return guardedWrite('combiner', request, combinerClient.importCombinerIntake, 'COMBINER_CLIENT_REQUEST_INVALID')
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
    runTerrainOperation: (request) => guardedWrite('terrain', request, terrainClient.runTerrainOperation, 'TERRAIN_CLIENT_REQUEST_INVALID'),
  }), [terrainClient, drawingId])
  const scopeTrackerRowsClient = useMemo(() => ({
    createTrackerRows: (request) => guardedWrite('tracker', request, trackerRowsClient.createTrackerRows, 'TRACKER_ROWS_CLIENT_REQUEST_INVALID'),
  }), [trackerRowsClient, phase])

  function onImported(value) {
    const started = pendingImport.current
    if (!activeScope.current || !started || started.drawingId !== drawingId
      || started.projectId !== projectId || value?.drawing_id !== drawingId) return
    pendingImport.current = null
    setPhysicalHead(value.head)
    if (typeof value.head?.state?.artifact_id === 'string') setTerrainHeadSignal(value.head.state.artifact_id)
    if (typeof value.head?.state?.artifact_id === 'string') setTrackerHeadSignal(value.head.state.artifact_id)
    setAnnouncement(value.created
      ? 'Terrain imported for this drawing.'
      : 'This terrain was already imported, so nothing changed.')
    onPhysicalHeadChanged?.(value)
  }

  function onTerrainChanged(result) {
    if (!activeScope.current || (result?.drawing_id !== undefined && result.drawing_id !== drawingId)) return
    setPhysicalHead(result.head)
    if (typeof result.head?.state?.artifact_id === 'string') setTrackerHeadSignal(result.head.state.artifact_id)
    onPhysicalHeadChanged?.(result)
  }

  function onTrackerRowsChanged(result) {
    if (!activeScope.current || result?.drawing_id !== drawingId) return
    setPhysicalHead((current) => Number.isInteger(current?.index) && current.index > result.head.index ? current : result.head)
    setTerrainHeadSignal(result.head.state.artifact_id)
    onPhysicalHeadChanged?.(result)
  }

  if (!hasLandxml && !hasCombiner && !hasTerrain && !trackerMounted) return null
  return (
    <section className="solar-workspace-tools" aria-label="Solar workspace tools"
      hidden={!hasLandxml && !hasCombiner && !hasTerrain && !hasTrackerRows}
      data-physical-head-index={physicalHead?.index ?? undefined}>
      {hasLandxml && <button type="button" ref={trigger} disabled={!!writers.tracker} aria-expanded={openPanel === 'landxml'} onClick={() => setOpenPanel('landxml')}>
        Import LandXML terrain
      </button>}
      {hasLandxml && openPanel === 'landxml' && (
        <div className="solar-workspace-panel" ref={panel}>
          <SolarLandxmlUpload drawingId={drawingId} projectId={projectId} upload={upload}
            checkoutHeld={checkoutHeld} busy={busy || !!writers.tracker} onImported={onImported}
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
        disabled={landxmlPending || !!writers.tracker} onClick={() => setOpenPanel('terrain')}>Terrain preview</button>}
      {hasTerrain && landxmlPending && <p data-testid="solar-terrain-reason">{TERRAIN_WORKSPACE_REASONS.landxml_importing}</p>}
      {hasTerrain && openPanel === 'terrain' && (
        <div className="solar-workspace-panel" ref={terrainPanel}>
          <h3 tabIndex={-1} ref={terrainHeading}>Terrain preview</h3>
          <SolarTerrainPanel drawingId={drawingId} projectId={projectId} client={scopeTerrainClient}
            headSignal={terrainHeadSignal} disabled={terrainGate !== null}
            disabledReason={terrainGate === null ? undefined : terrainGate === 'tracker_pending' ? TRACKER_ROWS_PANEL_REASONS.tracker_pending : TERRAIN_WORKSPACE_REASONS[terrainGate]}
            onPhysicalHeadChanged={onTerrainChanged} />
          <button type="button" onClick={() => {
            setOpenPanel(null)
            terrainTrigger.current?.focus()
          }}>Close</button>
        </div>
      )}
      {hasCombiner && <button type="button" ref={combinerTrigger} aria-expanded={openPanel === 'combiner-intake'}
        disabled={!!writers.tracker} onClick={() => setOpenPanel('combiner-intake')}>Import combiner intake</button>}
      {hasCombiner && openPanel === 'combiner-intake' && (
        <div className="solar-workspace-panel" ref={combinerPanel}>
          <SolarCombinerIntake key={generation} drawingId={drawingId} projectId={projectId}
            client={scopeCombinerClient} onStored={onStored} onRunPlacement={runPlacement} disabled={!!disabledReason} />
          {reason && <p data-testid="solar-combiner-reason">{reason === 'tracker_pending' ? TRACKER_ROWS_PANEL_REASONS.tracker_pending : COMBINER_WORKSPACE_REASONS[reason]}</p>}
          {phase === 'failed' && <button type="button" onClick={() => refreshDrawing(refreshValue.current)}>Refresh drawing</button>}
          <button type="button" onClick={() => {
            setOpenPanel(null)
            combinerTrigger.current?.focus()
          }}>Close</button>
        </div>
      )}
      {hasTrackerRows && <button type="button" ref={trackerTrigger} aria-expanded={openPanel === 'tracker-rows'}
        disabled={trackerBlockedReason !== null} onClick={() => { setTrackerMounted(true); setOpenPanel('tracker-rows') }}>
        Create tracker rows
      </button>}
      {hasTrackerRows && trackerBlockedReason && <p data-testid="solar-tracker-workspace-reason">{TRACKER_ROWS_PANEL_REASONS[trackerBlockedReason]}</p>}
      {writers.tracker && <p data-testid="solar-tracker-pending-reason">{TRACKER_ROWS_PANEL_REASONS.tracker_pending}</p>}
      {trackerMounted && <div className="solar-workspace-panel" data-flow-stage="layout"
        hidden={openPanel !== 'tracker-rows' || !hasTrackerRows}>
        <SolarTrackerRowsPanel drawingId={drawingId} projectId={projectId} drawingVersion={drawingVersion}
          client={scopeTrackerRowsClient} terrainClient={scopeTerrainClient} readIntake={readIntake}
          headSignal={trackerHeadSignal} active={openPanel === 'tracker-rows' && hasTrackerRows}
          checkoutHeld={checkoutHeld} busy={busy} blockedReason={trackerBlockedReason}
          onPhysicalHeadChanged={onTrackerRowsChanged}
          onClose={() => { setOpenPanel(null); trackerTrigger.current?.focus() }} />
      </div>}
      <p className="solar-workspace-announce" aria-live="polite" aria-atomic="true">{announcement}</p>
    </section>
  )
}
