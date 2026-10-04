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
import SolarEdgeImportPanel, { SOLAREDGE_PANEL_REASONS } from './SolarEdgeImportPanel.jsx'
import { createSolarImportClient } from './solarImportClient.js'
import { FLOW_PANELS } from './solarWorkspacePanels.js'
import './solarFlow.css'

export const SOLAREDGE_WORKSPACE_REASONS = Object.freeze({
  run_in_progress: 'A run is in progress, so wait for it to finish',
  checkout_required: 'Take the drawing checkout before importing or accepting SolarEdge tracking.',
  refreshing: 'The drawing is refreshing, so wait before changing SolarEdge tracking.',
  refresh_failed: 'The drawing did not finish refreshing, so refresh it before changing SolarEdge tracking.',
  tracker_pending: 'Tracker rows are being published, so wait for the request to finish.',
  solaredge_pending: 'A SolarEdge upload or report is in progress, so wait before publishing tracker rows.',
  staging_unavailable: 'Tracking acceptance is not connected in this workspace. You can upload the PDF and build its report.',
  revision_loading: 'Reading the current design revision before accepting tracking labels.',
  revision_unavailable: 'The current design revision could not be read. Retry the revision read before accepting.',
  revision_needed: 'Build a report to read the current design revision before accepting.',
  report_stale: 'The drawing changed after this report was built. Build the report again before accepting.',
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
  onStageSolarEdgeAccept,
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
  const importClient = useMemo(() => createSolarImportClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
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
      trackerRowsClient={trackerRowsClient}
      importClient={importClient}
      onStageSolarEdgeAccept={onStageSolarEdgeAccept}
      readIntake={readIntake}
      drawingVersion={drawingVersion}
      onDrawingVersionChanged={onDrawingVersionChanged}
      onRunPlacement={onRunPlacement}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function ToolsForScope({ drawingId, projectId, drawingVersion, flow, checkoutHeld, busy, client,
  combinerClient, terrainClient, trackerRowsClient, importClient, onStageSolarEdgeAccept, readIntake, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement }) {
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
  const solarEdgeTrigger = useRef(null)
  const solarEdgeReason = useRef(null)
  const solarEdgePanel = useRef(null)
  const revisionGeneration = useRef(0)
  const solarLifetime = useRef(0)
  const stagingToken = useRef(null)
  const [stagingPending, setStagingPending] = useState(false)
  const [revision, setRevision] = useState({ scope: drawingId, version: drawingVersion, phase: 'needed', graphRev: null })
  const revisionRef = useRef(revision)
  const reportBinding = useRef(null)
  const solarCurrent = useRef(null)
  const previousSolar = solarCurrent.current
  if (previousSolar && (previousSolar.version !== drawingVersion || previousSolar.flow !== flow
    || previousSolar.open !== openPanel || previousSolar.read !== readIntake)) {
    solarLifetime.current += 1
    revisionGeneration.current += 1
    stagingToken.current = null
    revisionRef.current = null
  }
  solarCurrent.current = { version: drawingVersion, flow, open: openPanel, read: readIntake, busy, checkoutHeld, phase,
    onStageSolarEdgeAccept }
  const renderSolarLifetime = solarLifetime.current
  const hasLandxml = !!drawingId && FLOW_PANELS[flow]?.includes('landxml') === true
  const hasTerrain = !!drawingId && FLOW_PANELS[flow]?.includes('terrain') === true
  const hasCombiner = !!drawingId && projectId === null && FLOW_PANELS[flow]?.includes('combiner-intake') === true
  const hasTrackerRows = !!drawingId && FLOW_PANELS[flow]?.includes('tracker-rows') === true
  const hasSolarEdge = !!drawingId && FLOW_PANELS[flow]?.includes('solaredge-import') === true

  function solarGate() {
    const current = solarCurrent.current
    if (writerTokens.current.tracker) return SOLAREDGE_WORKSPACE_REASONS.tracker_pending
    if (current.busy) return SOLAREDGE_WORKSPACE_REASONS.run_in_progress
    if (current.checkoutHeld !== true) return SOLAREDGE_WORKSPACE_REASONS.checkout_required
    if (current.phase === 'refreshing') return SOLAREDGE_WORKSPACE_REASONS.refreshing
    if (current.phase === 'failed') return SOLAREDGE_WORKSPACE_REASONS.refresh_failed
    if (stagingToken.current) return SOLAREDGE_PANEL_REASONS.staging_pending
    return null
  }

  function readRevision() {
    const captured = solarCurrent.current
    if (!activeScope.current || captured.open !== 'solaredge-import' || captured.flow !== 'solaredge-import') return
    const readToken = ++revisionGeneration.current
    const isCurrent = () => activeScope.current && revisionGeneration.current === readToken
      && solarCurrent.current.version === captured.version && solarCurrent.current.read === captured.read
      && solarCurrent.current.open === 'solaredge-import' && solarCurrent.current.flow === captured.flow
    const storeRevision = (next) => { revisionRef.current = next; setRevision(next) }
    const base = { scope: drawingId, version: captured.version, graphRev: null }
    const failed = () => { if (isCurrent()) storeRevision({ ...base, phase: 'unavailable' }) }
    storeRevision({ ...base, phase: 'loading' })
    if (!Number.isInteger(captured.version) || captured.version < 0) { failed(); return }
    let result
    try { result = captured.read(drawingId, captured.version) } catch { failed(); return }
    Promise.resolve(result).then((value) => {
      if (!isCurrent()) return
      try {
        const proto = value && Object.getPrototypeOf(value)
        const rev = value?.intake?.solar_design_graph?.rev
        if (!value || (proto !== Object.prototype && proto !== null) || value.version !== captured.version
          || !Number.isInteger(rev) || rev < 0 || rev > 2147483647) { failed(); return }
        storeRevision({ ...base, phase: 'ready', graphRev: rev })
      } catch { failed() }
    }, failed)
  }

  useLayoutEffect(() => {
    setStagingPending(false)
    if (openPanel === 'solaredge-import' && hasSolarEdge && previousSolar
      && previousSolar.version !== drawingVersion) readRevision()
    else if (!revisionRef.current) {
      const next = { scope: drawingId, version: drawingVersion, phase: 'needed', graphRev: null }
      revisionRef.current = next
      setRevision(next)
    }
  }, [drawingVersion, readIntake, openPanel, hasSolarEdge])

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
    if (kind !== 'tracker' && kind !== 'solaredge') request.signal?.addEventListener('abort', release, { once: true })
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
      revisionGeneration.current += 1
      stagingToken.current = null
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
    if (openPanel === 'solaredge-import' && hasSolarEdge) solarEdgePanel.current?.querySelector('input[type="file"]')?.focus()
  }, [openPanel, hasLandxml, hasCombiner, hasTerrain, hasSolarEdge])

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
    : writers.combiner ? 'combiner_pending' : writers.solaredge ? 'solaredge_pending'
      : phase === 'refreshing' || phase === 'failed' ? 'drawing_refresh' : null

  const scopeImportClient = useMemo(() => {
    const version = drawingVersion
    const lifetime = solarLifetime.current
    const send = (request, method) => {
      const current = solarCurrent.current
      if (!activeScope.current || current.open !== 'solaredge-import' || current.flow !== 'solaredge-import'
        || solarLifetime.current !== lifetime
        || current.version !== version || request.drawingId !== drawingId || request.projectId !== projectId
        || request.signal?.aborted || solarGate()) {
        return Promise.resolve({ ok: false, code: 'SOLAREDGE_CLIENT_REQUEST_INVALID', retryable: false, status: null })
      }
      if (reportBinding.current) reportBinding.current = { ...reportBinding.current, eligible: false }
      revisionGeneration.current += 1
      const next = { scope: drawingId, version, phase: 'needed', graphRev: null }
      revisionRef.current = next
      setRevision(next)
      return guardedWrite('solaredge', request, method, 'SOLAREDGE_CLIENT_REQUEST_INVALID')
    }
    return { uploadPdf: (request) => send(request, importClient.uploadPdf),
      requestReport: (request) => send(request, importClient.requestReport) }
  }, [importClient, drawingVersion, drawingId, projectId, openPanel, flow, readIntake])

  function reportReady(value) {
    if (!activeScope.current || solarLifetime.current !== renderSolarLifetime) return
    const reportVersion = value?.source_version
    const known = Number.isInteger(drawingVersion) && drawingVersion >= 0
    reportBinding.current = { value, version: known ? reportVersion : drawingVersion, eligible: known && reportVersion === drawingVersion }
    if (!known || reportVersion === drawingVersion) readRevision()
    else setRevision((current) => ({ ...current }))
  }

  function stageSolarEdge(params) {
    const current = solarCurrent.current
    const rev = revisionRef.current
    const binding = reportBinding.current
    if (!activeScope.current || current.open !== 'solaredge-import' || current.flow !== 'solaredge-import'
      || solarLifetime.current !== renderSolarLifetime
      || solarGate() || !current.onStageSolarEdgeAccept || binding?.version !== current.version
      || binding?.eligible !== true
      || rev?.version !== current.version || rev.phase !== 'ready' || params.expected_rev !== rev.graphRev
      || params.report_artifact_id !== binding.value.report?.artifact_id) return false
    const token = {}
    stagingToken.current = token
    let result
    try { result = current.onStageSolarEdgeAccept(params) } catch (error) {
      if (stagingToken.current === token) stagingToken.current = null
      throw error
    }
    if (!result || typeof result.then !== 'function') {
      if (stagingToken.current === token) stagingToken.current = null
      return result
    }
    setStagingPending(true)
    const release = () => {
      if (activeScope.current && stagingToken.current === token) { stagingToken.current = null; setStagingPending(false) }
    }
    return Promise.resolve(result).then((answer) => { release(); return answer }, (error) => { release(); throw error })
  }

  const solarReason = solarGate()
  const currentRevision = revisionRef.current?.version === drawingVersion ? revisionRef.current : null
  const acceptSolarReason = !onStageSolarEdgeAccept ? SOLAREDGE_WORKSPACE_REASONS.staging_unavailable
    : reportBinding.current && reportBinding.current.version !== drawingVersion ? SOLAREDGE_WORKSPACE_REASONS.report_stale
      : currentRevision?.phase === 'loading' ? SOLAREDGE_WORKSPACE_REASONS.revision_loading
        : currentRevision?.phase === 'unavailable' ? SOLAREDGE_WORKSPACE_REASONS.revision_unavailable
          : currentRevision?.phase !== 'ready' ? SOLAREDGE_WORKSPACE_REASONS.revision_needed : null

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

  if (!hasLandxml && !hasCombiner && !hasTerrain && !hasSolarEdge && !trackerMounted) return null
  return (
    <section className="solar-workspace-tools" aria-label="Solar workspace tools"
      hidden={!hasLandxml && !hasCombiner && !hasTerrain && !hasTrackerRows && !hasSolarEdge}
      data-physical-head-index={physicalHead?.index ?? undefined}>
      {hasSolarEdge && <button type="button" ref={solarEdgeTrigger} disabled={!!solarReason}
        aria-expanded={openPanel === 'solaredge-import'} onClick={() => {
          if (solarGate()) return
          if (openPanel === 'solaredge-import') solarEdgePanel.current?.querySelector('input[type="file"]')?.focus()
          else { reportBinding.current = null; setOpenPanel('solaredge-import') }
        }}>Import SolarEdge PDF</button>}
      {hasSolarEdge && solarReason && <p ref={solarEdgeReason} tabIndex={-1}>{solarReason}</p>}
      {hasSolarEdge && openPanel === 'solaredge-import' && <div className="solar-workspace-panel" ref={solarEdgePanel}>
        <SolarEdgeImportPanel drawingId={drawingId} projectId={projectId} drawingVersion={drawingVersion}
          graphRev={currentRevision?.phase === 'ready' ? currentRevision.graphRev : undefined}
          client={scopeImportClient} onReportReady={reportReady} onAccept={stageSolarEdge}
          disabled={!!solarReason || stagingPending} acceptDisabledReason={acceptSolarReason} />
        {acceptSolarReason && <p>{acceptSolarReason}</p>}
        {currentRevision?.phase === 'unavailable' && <button type="button" onClick={() => {
          if (revisionRef.current?.phase !== 'loading') readRevision()
        }}>Retry design revision</button>}
        <button type="button" onClick={() => {
          revisionGeneration.current += 1
          revisionRef.current = null
          stagingToken.current = null
          setStagingPending(false)
          setRevision({ scope: drawingId, version: drawingVersion, phase: 'needed', graphRev: null })
          setOpenPanel(null)
          if (solarReason) solarEdgeReason.current?.focus()
          else solarEdgeTrigger.current?.focus()
        }}>Close</button>
      </div>}
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
      {hasTrackerRows && trackerBlockedReason && <p data-testid="solar-tracker-workspace-reason">{trackerBlockedReason === 'solaredge_pending'
        ? SOLAREDGE_WORKSPACE_REASONS.solaredge_pending : TRACKER_ROWS_PANEL_REASONS[trackerBlockedReason]}</p>}
      {writers.tracker && <p data-testid="solar-tracker-pending-reason">{TRACKER_ROWS_PANEL_REASONS.tracker_pending}</p>}
      {trackerMounted && <div className="solar-workspace-panel" data-flow-stage="layout"
        hidden={openPanel !== 'tracker-rows' || !hasTrackerRows || !!writers.solaredge}>
        <SolarTrackerRowsPanel drawingId={drawingId} projectId={projectId} drawingVersion={drawingVersion}
          client={scopeTrackerRowsClient} terrainClient={scopeTerrainClient} readIntake={readIntake}
          headSignal={trackerHeadSignal} active={openPanel === 'tracker-rows' && hasTrackerRows && !writers.solaredge}
          checkoutHeld={checkoutHeld} busy={busy} blockedReason={trackerBlockedReason === 'solaredge_pending' ? null : trackerBlockedReason}
          onPhysicalHeadChanged={onTrackerRowsChanged}
          onClose={() => { setOpenPanel(null); trackerTrigger.current?.focus() }} />
      </div>}
      <p className="solar-workspace-announce" aria-live="polite" aria-atomic="true">{announcement}</p>
    </section>
  )
}
