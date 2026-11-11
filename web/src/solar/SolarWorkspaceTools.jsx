import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, createProjectDrawingEditorTransport, getDrawingIntake, noteUnauthorized, runToolAsync } from '../api.js'
import { createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import { createSolarCombinerIntakeClient } from './solarCombinerIntakeClient.js'
import SolarTerrainPanel from './SolarTerrainPanel.jsx'
import { createSolarTerrainClient } from './solarTerrainClient.js'
import SolarCivilPanel from './SolarCivilPanel.jsx'
import SolarPhysicalReadPanel from './SolarPhysicalReadPanel.jsx'
import { createSolarCivilClient } from './solarCivilClient.js'
import SolarTrackerRowsPanel, { TRACKER_ROWS_PANEL_REASONS } from './SolarTrackerRowsPanel.jsx'
import { createSolarTrackerRowsClient } from './solarTrackerRowsClient.js'
import SolarEdgeImportPanel, { SOLAREDGE_PANEL_REASONS } from './SolarEdgeImportPanel.jsx'
import { createSolarImportClient } from './solarImportClient.js'
import { FLOW_PANELS } from './solarWorkspacePanels.js'
import { captureSolarPublication, createSolarProjectMaterializationClient, initialSolarParameters, originalSolarParameters, solarPublicationMatches } from './solarProjectMaterializationClient.js'
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

// Each guarded call gets its own fetch closure, including fetches started asynchronously
// by the client's cancellation race. Reads keep the ordinary client transport.
function transportTrackedClient(createClient, options, methods) {
  const client = createClient(options)
  const tracked = { ...client }
  for (const method of methods) tracked[method] = (request, lifetime) => {
    if (!lifetime) return client[method](request)
    const scoped = createClient({ ...options, fetchImpl: (...args) => {
      lifetime.pending += 1
      const settled = () => { lifetime.pending -= 1; lifetime.releaseIfSettled() }
      let result
      try { result = options.fetchImpl(...args) } catch (error) { settled(); throw error }
      return Promise.resolve(result).then((answer) => { settled(); return answer }, (error) => { settled(); throw error })
    } })
    return scoped[method](request)
  }
  return tracked
}

export function ProjectLayoutPublicationTools(props) {
  return <SolarWorkspaceTools {...props} flow="project-publication" />
}

export default function SolarWorkspaceTools({
  drawingId, projectId = null, drawingVersion, flow, checkoutHeld, busy,
  getCheckoutCapability, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement, transport,
  onStageSolarEdgeAccept, catalogDigestOf,
  projectScope = null, getProjectScope, onProjectDrawingPublished,
}) {
  const publicationRequests = useRef(new Map())
  const capability = useRef(getCheckoutCapability)
  capability.current = getCheckoutCapability
  const publicationCheckout = useMemo(() => transport?.publicationCheckout ?? createProjectDrawingEditorTransport(), [transport])
  const publicationClient = useMemo(() => createSolarProjectMaterializationClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)), apiBase: config.apiBase,
    headers: scope => transport?.headers ? transport.headers(scope.drawing_id) : authHeaders(),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])
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
  const civilClient = useMemo(() => transportTrackedClient(createSolarCivilClient, {
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }, ['runCivilOperation']), [transport])
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
  const trackerRowsClient = useMemo(() => transportTrackedClient(createSolarTrackerRowsClient, {
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }, ['createTrackerRows']), [transport])
  const readIntake = useMemo(() => transport?.readIntake ?? ((id, version) => getDrawingIntake(false, id, version)), [transport])
  const importClient = useMemo(() => transportTrackedClient(createSolarImportClient, {
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: (id) => withCurrentCheckoutCapability(transport?.headers ? transport.headers(id) : { 'X-Tenant-Id': config.tenant, ...authHeaders() }, capability.current),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }, ['uploadPdf', 'requestReport']), [transport])

  return (
    <ToolsForScope
      key={JSON.stringify([drawingId ?? null, projectId ?? null])}
      drawingId={drawingId}
      projectId={projectId}
      projectScope={projectScope} getProjectScope={getProjectScope}
      publicationClient={publicationClient} publicationRequests={publicationRequests.current}
      publicationCheckout={publicationCheckout}
      onProjectDrawingPublished={onProjectDrawingPublished}
      flow={flow}
      checkoutHeld={checkoutHeld}
      busy={busy}
      client={client}
      terrainClient={terrainClient}
      civilClient={civilClient}
      runRead={transport?.runRead ?? runToolAsync}
      catalogDigestOf={catalogDigestOf}
      save={transport?.save}
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

Object.defineProperty(SolarWorkspaceTools, 'workspacePanelsByFlow', { value: FLOW_PANELS })

function ToolsForScope({ drawingId, projectId, drawingVersion, flow, checkoutHeld, busy, client,
  projectScope, getProjectScope, publicationClient, publicationRequests, publicationCheckout, onProjectDrawingPublished,
  combinerClient, terrainClient, civilClient, runRead, catalogDigestOf, save, trackerRowsClient, importClient, onStageSolarEdgeAccept, readIntake, onPhysicalHeadChanged, onDrawingVersionChanged, onRunPlacement }) {
  const [openPanel, setOpenPanel] = useState(null)
  const [intake, setIntake] = useState(null)
  const intakeRef = useRef(null)
  const refreshValue = useRef(null)
  const [phase, setPhase] = useState('idle')
  const [generation, setGeneration] = useState(0)
  const [notStaged, setNotStaged] = useState(false)
  const [physicalHead, setPhysicalHead] = useState(null)
  const [publication, setPublication] = useState({ phase: 'idle', message: '' })
  const [publicationReadback, setPublicationReadback] = useState(null)
  const [initialBoundary, setInitialBoundary] = useState('')
  const [initialPreset, setInitialPreset] = useState('')
  const [initialUnits, setInitialUnits] = useState('m')
  const [originalIntake, setOriginalIntake] = useState(null)
  const [gridLongAxis, setGridLongAxis] = useState('150')
  const initialProject = JSON.stringify([projectScope?.organization_id, projectId])
  const selectedOriginal = originalIntake?.project === initialProject ? originalIntake : null
  let initialParameters = null
  let originalSummary = null
  if (selectedOriginal) {
    initialParameters = originalSolarParameters(selectedOriginal.text, Number(gridLongAxis))
    if (initialParameters) originalSummary = JSON.parse(selectedOriginal.text.replace(/^\uFEFF/, ''))
  } else {
    try { initialParameters = initialSolarParameters(JSON.parse(initialBoundary), JSON.parse(initialPreset), initialUnits) } catch { /* incomplete input */ }
  }
  const publicationPending = useRef(false)
  const publicationCurrent = useRef(null)
  publicationCurrent.current = { scope: projectScope, head: physicalHead, readback: publicationReadback,
    parameters: initialParameters, drawingId, projectId, checkoutHeld, busy }
  const [terrainHeadSignal, setTerrainHeadSignal] = useState(null)
  const [landxmlPending, setLandxmlPending] = useState(false)
  const [writers, setWriters] = useState({})
  const writerTokens = useRef({})
  const [trackerMounted, setTrackerMounted] = useState(false)
  const [trackerHeadSignal, setTrackerHeadSignal] = useState(null)
  const [civilHeadSignal, setCivilHeadSignal] = useState(null)
  const [readHeadSignal, setReadHeadSignal] = useState(null)
  const civilTrigger = useRef(null)
  const civilHeading = useRef(null)
  const readTrigger = useRef(null)
  const readHeading = useRef(null)
  const physicalCloseFocus = useRef(null)
  const physicalReason = useRef(null)
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
  const solarEdgeCloseFocus = useRef(false)
  const revisionGeneration = useRef(0)
  const solarLifetime = useRef(0)
  const stagingToken = useRef(null)
  const [stagingPending, setStagingPending] = useState(false)
  const [revision, setRevision] = useState({ scope: drawingId, version: drawingVersion, phase: 'needed', graphRev: null })
  const revisionRef = useRef(revision)
  const reportBinding = useRef(null)
  const solarCurrent = useRef(null)
  const previousSolar = solarCurrent.current
  if (previousSolar && previousSolar.version !== drawingVersion && reportBinding.current) {
    reportBinding.current = { ...reportBinding.current, eligible: false, stale: true }
  }
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
  const hasCivil = !!drawingId && FLOW_PANELS[flow]?.includes('civil') === true
  const hasPhysicalRead = !!drawingId && FLOW_PANELS[flow]?.includes('physical-read') === true

  function solarGate() {
    const current = solarCurrent.current
    if (writerTokens.current.civil) return TRACKER_ROWS_PANEL_REASONS.terrain_pending
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
    const current = solarCurrent.current
    if (!activeScope.current || publicationPending.current || inFlight('civil') || (kind === 'civil'
      ? Object.keys(held).some(inFlight) || current.busy || current.checkoutHeld !== true
        || current.phase === 'refreshing' || current.phase === 'failed' || stagingToken.current
      : kind === 'tracker'
        ? Object.keys(held).some(inFlight) || current.phase === 'refreshing' || current.phase === 'failed'
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
    const retainsTransport = kind === 'tracker' || kind === 'solaredge' || kind === 'civil'
    const lifetime = retainsTransport ? { pending: 0, answered: false,
      releaseIfSettled: () => { if (lifetime.answered && lifetime.pending === 0) release() } } : undefined
    const answered = () => {
      if (!lifetime) { release(); return }
      lifetime.answered = true
      lifetime.releaseIfSettled()
    }
    if (!retainsTransport) request.signal?.addEventListener('abort', release, { once: true })
    let result
    try { result = retainsTransport ? send(request, lifetime) : send(request) } catch (error) { answered(); throw error }
    return Promise.resolve(result).then((answer) => { answered(); return answer }, (error) => { answered(); throw error })
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
    if (openPanel === 'civil' && hasCivil) civilHeading.current?.focus()
    if (openPanel === 'physical-read' && hasPhysicalRead) readHeading.current?.focus()
    if (openPanel === null && physicalCloseFocus.current) {
      const target = physicalCloseFocus.current
      physicalCloseFocus.current = null
      ;(target.disabled ? physicalReason.current : target)?.focus()
    }
  }, [openPanel, hasLandxml, hasCombiner, hasTerrain, hasSolarEdge, hasCivil, hasPhysicalRead])

  useLayoutEffect(() => {
    if (openPanel !== null || !solarEdgeCloseFocus.current) return
    solarEdgeCloseFocus.current = false
    ;(solarEdgeReason.current ?? solarEdgeTrigger.current)?.focus()
  }, [openPanel])

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
    if (!activeScope.current || writerTokens.current.tracker || writerTokens.current.civil || !intake || phase !== 'idle' || drawingVersion !== intake.version
      || params.expected_rev !== intake.graphRev || checkoutHeld !== true || busy) return
    if (onRunPlacement?.(params) === false) setNotStaged(true)
  }

  const disabledReason = writers.civil ? 'run_in_progress' : writers.tracker ? 'tracker_pending' : busy ? 'run_in_progress' : checkoutHeld !== true ? 'checkout_required'
    : phase === 'refreshing' ? 'refreshing' : phase === 'failed' ? 'refresh_failed' : null
  const reason = disabledReason ?? (notStaged ? 'not_staged' : null)
  const terrainGate = writers.civil ? 'run_in_progress' : writers.tracker ? 'tracker_pending' : busy ? 'run_in_progress' : landxmlPending ? 'landxml_importing' : checkoutHeld !== true ? 'checkout_required' : null
  const trackerBlockedReason = writers.civil ? 'terrain_pending' : writers.landxml ? 'landxml_pending' : writers.terrain ? 'terrain_pending'
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
    : reportBinding.current && (reportBinding.current.stale || reportBinding.current.version !== drawingVersion) ? SOLAREDGE_WORKSPACE_REASONS.report_stale
      : currentRevision?.phase === 'loading' ? SOLAREDGE_WORKSPACE_REASONS.revision_loading
        : currentRevision?.phase === 'unavailable' ? SOLAREDGE_WORKSPACE_REASONS.revision_unavailable
          : currentRevision?.phase !== 'ready' ? SOLAREDGE_WORKSPACE_REASONS.revision_needed : null

  const upload = useMemo(() => (request) => {
    if (writerTokens.current.tracker || writerTokens.current.civil) return Promise.resolve({ ok: false, code: 'LANDXML_CLIENT_REQUEST_INVALID', retryable: false })
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

  const scopeCivilClient = useMemo(() => {
    const lifetime = solarLifetime.current
    const isCurrent = (request) => activeScope.current && solarLifetime.current === lifetime
      && solarCurrent.current.open === 'civil' && solarCurrent.current.flow === 'ground-physical'
      && request.drawingId === drawingId && request.projectId === projectId && !request.signal?.aborted
    const refused = () => Promise.resolve({ ok: false, code: 'TERRAIN_CLIENT_REQUEST_INVALID', retryable: false, status: null })
    return {
      getCivil: (request) => !isCurrent(request) ? refused() : civilClient.getCivil(request).then((answer) => {
        if (isCurrent(request) && answer?.ok && answer.value?.head) observeHead(answer.value.head)
        return answer
      }),
      runCivilOperation: (request) => !isCurrent(request) ? refused()
        : guardedWrite('civil', request, civilClient.runCivilOperation, 'TERRAIN_CLIENT_REQUEST_INVALID'),
    }
  }, [civilClient, drawingId, projectId, openPanel, flow, drawingVersion])

  function observeHead(head) {
    if (!activeScope.current || head?.drawing_id !== drawingId || !Number.isInteger(head.index) || head.index < 0) return
    setPhysicalHead((current) => Number.isInteger(current?.index) && current.index > head.index ? current : head)
  }

  function publicationCapture(requestId) {
    const current = publicationCurrent.current
    const scope = getProjectScope ? getProjectScope() : current.scope
    if (!scope || !activeScope.current || current.projectId !== scope.project_id
      || (scope.drawing_id !== null && current.drawingId !== scope.drawing_id)) return null
    const captured = captureSolarPublication(scope, current.readback, requestId, current.parameters)
    if (captured?.mode === 'successor' && current.head?.project_id === captured.project_id && current.head.drawing_id === captured.drawing_id
      && (current.head.state?.artifact_id !== captured.physical_head
      || current.head.state?.content_sha256 !== captured.physical_content_sha256 || current.head.index !== captured.physical_index)) return null
    return captured
  }

  async function chooseOriginalIntake(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    const selection = { project: initialProject, name: file.name }
    setOriginalIntake(selection)
    try {
      if (!file.size || file.size > 2 * 1024 * 1024) throw new Error('invalid size')
      const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer())
      if (!originalSolarParameters(text, 150)) throw new Error('invalid intake')
      setOriginalIntake(current => current === selection ? { ...selection, text } : current)
    } catch {
      setOriginalIntake(current => current === selection
        ? { ...selection, error: 'Choose a complete UTF-8 layout intake JSON file within the 2 MiB publication limit.' } : current)
    }
  }

  async function readPublicationHead() {
    if (publicationPending.current || busy || !projectScope) return
    publicationPending.current = true
    setPublication({ phase: 'pending', message: 'Reading the current layout.' })
    const scope = projectScope
    try {
      const result = await publicationClient.read(scope)
      if (!activeScope.current) return
      if (!['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every(key => publicationCurrent.current.scope?.[key] === scope[key])) {
        setPublication({ phase: 'idle', message: 'The selected drawing changed. Read its current layout again.' }); return
      }
      if (result?.ok) setPublicationReadback(result.value)
      setPublication({ phase: 'idle', message: result?.ok
        ? result.value.writer_available === false ? 'The current layout is ready, but the licensed drawing writer is unavailable.'
          : 'Current layout read. Review it before publishing.'
        : result?.code === 'PROJECT_LAYOUT_INITIAL_UNAVAILABLE' ? 'Initial project drawing publication is not connected yet.'
          : 'The current layout is unavailable. Read it again before publishing.' })
    } catch {
      if (activeScope.current) setPublication({ phase: 'idle', message: 'The current layout could not be read. Try again.' })
    } finally { publicationPending.current = false }
  }

  async function publishLayout() {
    if (publicationPending.current || busy || phase !== 'idle'
      || Object.values(writers).some(Boolean) || stagingPending) return
    const key = JSON.stringify([projectScope?.organization_id, projectId, projectScope?.drawing_id ?? null])
    const previous = publicationRequests.get(key)
    let captured = publicationCapture(previous?.captured.request_id ?? globalThis.crypto.randomUUID())
    if (!captured || typeof onProjectDrawingPublished !== 'function') {
      setPublication({ phase: 'idle', message: 'Select a project drawing version and read its current layout before publishing.' }); return
    }
    if (previous?.unknown && !solarPublicationMatches(previous.captured, captured)) {
      setPublication({ phase: 'unknown', message: 'The previous publication outcome is unknown. Return to its drawing version and layout to reconcile that request.' }); return
    }
    if (previous && !previous.unknown && !solarPublicationMatches(previous.captured, captured))
      captured = publicationCapture(globalThis.crypto.randomUUID())
    const attempt = previous && solarPublicationMatches(previous.captured, captured) ? previous : { captured }
    publicationRequests.set(key, attempt)
    publicationPending.current = true
    setPublication({ phase: 'pending', message: 'Publishing the layout to this project drawing.' })
    let writeStarted = false
    try {
      if (!attempt.receipt && captured.mode === 'successor' && !attempt.capability) {
        const acquired = await publicationCheckout.acquireCheckout(captured)
        attempt.capability = typeof acquired === 'string' ? acquired : acquired?.capability ?? acquired?.checkout_capability
        if (typeof attempt.capability !== 'string' || !attempt.capability) throw new Error('checkout unavailable')
      }
      if (!solarPublicationMatches(attempt.captured, publicationCapture(attempt.captured.request_id))) {
        if (activeScope.current) setPublication({ phase: 'idle', message: 'The drawing or layout changed. Publication was not sent.' })
        return
      }
      writeStarted = !attempt.receipt
      const answer = attempt.receipt ? { ok: true, value: attempt.receipt }
        : await publicationClient.publish(attempt.captured, { capability: attempt.capability })
      if (!answer.ok) {
        attempt.unknown = answer.outcomeUnknown === true
        if (activeScope.current) setPublication({ phase: attempt.unknown ? 'unknown' : 'idle', message:
          answer.code === 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE'
            ? 'Editable drawing publication is unavailable because the licensed layout writer is not connected.'
            : attempt.unknown ? 'Publication outcome is unknown. Retry to reconcile the same request.'
              : `Publication is incomplete (${answer.code}). Check access and the current drawing checkout, then retry.` })
        return
      }
      attempt.unknown = false
      attempt.receipt = answer.value
      if (!solarPublicationMatches(attempt.captured, publicationCapture(attempt.captured.request_id))) {
        if (activeScope.current) setPublication({ phase: 'idle', message: 'The drawing or layout changed. Publication was not selected.' })
        return
      }
      const seated = await onProjectDrawingPublished(answer.value, attempt.captured,
        () => solarPublicationMatches(attempt.captured, publicationCapture(attempt.captured.request_id)))
      if (activeScope.current) setPublication({ phase: 'idle', message: seated
        ? 'Layout published as an editable project drawing version.'
        : 'Publication is incomplete. The project could not be refreshed to the returned drawing version.' })
    } catch {
      attempt.unknown = writeStarted && !attempt.receipt
      if (activeScope.current) setPublication({ phase: attempt.unknown ? 'unknown' : 'idle', message: attempt.unknown
        ? 'Publication outcome is unknown. Retry to reconcile the same request.'
        : attempt.receipt ? 'Publication is incomplete. Retry the project refresh for the same returned version.'
          : 'The project drawing checkout is unavailable. No publication was sent. Retry the same request.' })
    } finally {
      if (!attempt.unknown && attempt.capability) {
        try { await publicationCheckout.releaseCheckout(attempt.captured, attempt.capability); attempt.capability = null }
        catch { /* Keep the exact capability on this attempt for explicit cleanup/retry. */ }
      }
      publicationPending.current = false
    }
  }

  function signalPhysicalPeers(head, origin) {
    const signal = head?.state?.artifact_id
    if (typeof signal !== 'string') return
    if (origin !== 'civil') setCivilHeadSignal(signal)
    setReadHeadSignal(signal)
  }

  function onCivilChanged(value) {
    if (!activeScope.current || solarLifetime.current !== renderSolarLifetime || value?.drawing_id !== drawingId || !value.created) return
    observeHead(value.head)
    setTerrainHeadSignal(value.head.state.artifact_id)
    setTrackerHeadSignal(value.head.state.artifact_id)
    signalPhysicalPeers(value.head, 'civil')
    onPhysicalHeadChanged?.(value)
  }

  const readDisabledReason = phase === 'failed' ? 'The drawing could not be refreshed. Refresh the drawing before continuing.'
    : phase === 'refreshing' ? 'Wait for the drawing to finish refreshing.'
      : stagingPending ? 'Wait for the tracking labels to finish being prepared.'
        : Object.values(writers).some(Boolean) ? TRACKER_ROWS_PANEL_REASONS.terrain_pending
          : busy ? 'A run is in progress, so wait for it to finish.' : null
  const civilDisabledReason = checkoutHeld !== true ? 'Take the drawing checkout before running civil operations.' : readDisabledReason
  const civilDisabled = !!civilDisabledReason
  const physicalContention = civilDisabledReason

  function onImported(value) {
    const started = pendingImport.current
    if (!activeScope.current || !started || started.drawingId !== drawingId
      || started.projectId !== projectId || value?.drawing_id !== drawingId) return
    pendingImport.current = null
    observeHead(value.head)
    signalPhysicalPeers(value.head)
    if (typeof value.head?.state?.artifact_id === 'string') setTerrainHeadSignal(value.head.state.artifact_id)
    if (typeof value.head?.state?.artifact_id === 'string') setTrackerHeadSignal(value.head.state.artifact_id)
    setAnnouncement(value.created
      ? 'Terrain imported for this drawing.'
      : 'This terrain was already imported, so nothing changed.')
    onPhysicalHeadChanged?.(value)
  }

  function onTerrainChanged(result) {
    if (!activeScope.current || (result?.drawing_id !== undefined && result.drawing_id !== drawingId)) return
    observeHead(result.head)
    signalPhysicalPeers(result.head)
    if (typeof result.head?.state?.artifact_id === 'string') setTrackerHeadSignal(result.head.state.artifact_id)
    onPhysicalHeadChanged?.(result)
  }

  function onTrackerRowsChanged(result) {
    if (!activeScope.current || result?.drawing_id !== drawingId) return
    setPhysicalHead((current) => Number.isInteger(current?.index) && current.index > result.head.index ? current : result.head)
    setTerrainHeadSignal(result.head.state.artifact_id)
    signalPhysicalPeers(result.head)
    onPhysicalHeadChanged?.(result)
  }

  const hasProjectPublication = projectId !== null && (flow === 'ground-physical' || flow === 'project-publication')
  if (!hasProjectPublication && !hasLandxml && !hasCombiner && !hasTerrain && !hasSolarEdge && !trackerMounted) return null
  return (
    <section className="solar-workspace-tools" aria-label="Solar workspace tools"
      hidden={!hasProjectPublication && !hasLandxml && !hasCombiner && !hasTerrain && !hasTrackerRows && !hasSolarEdge}
      data-physical-head-index={physicalHead?.index ?? undefined}>
      {hasProjectPublication && <div aria-label="Project layout publication">
        {projectScope?.drawing_id === null && projectScope?.input_version_id === null && <div>
          <label>Original layout intake (JSON)<input type="file" accept=".json,application/json"
            disabled={publication.phase === 'pending'} onChange={chooseOriginalIntake} /></label>
          {selectedOriginal ? <>
            <p>{selectedOriginal.name}</p>
            {selectedOriginal.error && <p role="alert">{selectedOriginal.error}</p>}
            {originalSummary && <p>Preset {originalSummary.active_preset.Name} · {originalSummary.units === 'm' ? 'Meters' : 'Feet'}
              {' · '}Pile template {originalSummary.pile_template.Name}</p>}
            <label>Terrain grid cells on longer axis<input type="number" min={2} max={300} step={1}
              value={gridLongAxis} onChange={event => setGridLongAxis(event.target.value)} /></label>
            <p>This layout is terrain-free. The grid setting is recorded for provenance.</p>
            <button type="button" disabled={publication.phase === 'pending'}
              onClick={() => setOriginalIntake(null)}>Clear original intake</button>
          </> : <>
            <label>Site boundary (JSON)<textarea value={initialBoundary} maxLength={2000000}
              onChange={event => setInitialBoundary(event.target.value)} /></label>
            <label>Complete preset (JSON)<textarea value={initialPreset} maxLength={2000000}
              onChange={event => setInitialPreset(event.target.value)} /></label>
            <label>Drawing units for publication<select value={initialUnits} onChange={event => setInitialUnits(event.target.value)}>
              <option value="m">Metres</option><option value="ft">Feet</option>
            </select></label>
          </>}
        </div>}
        <button type="button" disabled={publication.phase === 'pending' || busy || !projectScope}
          onClick={readPublicationHead}>Read current layout for publication</button>
        <button type="button" disabled={publication.phase === 'pending' || busy
          || !publicationReadback || !projectScope || (projectScope.input_version_id === null && !initialParameters)
          || Object.values(writers).some(Boolean) || phase !== 'idle' || stagingPending}
          onClick={publishLayout}>{publication.phase === 'unknown' ? 'Reconcile layout publication'
            : projectScope?.input_version_id === null ? 'Generate editable project drawing' : 'Publish layout as editable drawing'}</button>
        {!projectScope && <p>Select a canonical project drawing version before publishing.</p>}
        <p role="status">{publication.message}</p>
      </div>}
      {hasCivil && <button type="button" ref={civilTrigger} aria-expanded={openPanel === 'civil'}
        onClick={() => { if (openPanel === 'civil') civilHeading.current?.focus(); else setOpenPanel('civil') }}>Civil operations</button>}
      {hasPhysicalRead && <button type="button" ref={readTrigger} aria-expanded={openPanel === 'physical-read'}
        onClick={() => { if (openPanel === 'physical-read') readHeading.current?.focus(); else setOpenPanel('physical-read') }}>Physical reads</button>}
      {(hasCivil || hasPhysicalRead) && physicalContention && <p ref={physicalReason} tabIndex={-1}>{physicalContention}</p>}
      {hasCivil && openPanel === 'civil' && <div className="solar-workspace-panel">
        <h3 tabIndex={-1} ref={civilHeading}>Civil operations</h3>
        <SolarCivilPanel drawingId={drawingId} projectId={projectId} client={scopeCivilClient}
          headSignal={civilHeadSignal} disabled={civilDisabled} disabledReason={civilDisabledReason} onPhysicalHeadChanged={onCivilChanged} />
        <button type="button" onClick={() => { physicalCloseFocus.current = civilTrigger.current; setOpenPanel(null) }}>Close</button>
      </div>}
      {hasPhysicalRead && openPanel === 'physical-read' && <div className="solar-workspace-panel">
        <h3 tabIndex={-1} ref={readHeading}>Physical reads</h3>
        <SolarPhysicalReadPanel drawingId={drawingId} projectId={projectId} drawingVersion={drawingVersion}
          terrainClient={scopeTerrainClient} runRead={runRead} headSignal={readHeadSignal} catalogDigestOf={catalogDigestOf}
          disabled={!!readDisabledReason} disabledReason={readDisabledReason}
          download={importClient.downloadArtifact} save={save} />
        <button type="button" onClick={() => { physicalCloseFocus.current = readTrigger.current; setOpenPanel(null) }}>Close</button>
      </div>}
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
          solarEdgeCloseFocus.current = true
          setOpenPanel(null)
        }}>Close</button>
      </div>}
      {hasLandxml && <button type="button" ref={trigger} disabled={!!writers.tracker || !!writers.civil} aria-expanded={openPanel === 'landxml'} onClick={() => setOpenPanel('landxml')}>
        Import LandXML terrain
      </button>}
      {hasLandxml && openPanel === 'landxml' && (
        <div className="solar-workspace-panel" ref={panel}>
          <SolarLandxmlUpload drawingId={drawingId} projectId={projectId} upload={upload}
            checkoutHeld={checkoutHeld} busy={busy || !!writers.tracker || !!writers.civil} onImported={onImported}
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
        disabled={landxmlPending || !!writers.tracker || !!writers.civil} onClick={() => setOpenPanel('terrain')}>Terrain preview</button>}
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
        disabled={!!writers.tracker || !!writers.civil} onClick={() => setOpenPanel('combiner-intake')}>Import combiner intake</button>}
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
      {trackerMounted && <div className="solar-workspace-panel" data-flow-stage="tracker-rows"
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
