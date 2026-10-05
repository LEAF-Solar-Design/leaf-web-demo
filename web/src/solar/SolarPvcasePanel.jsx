import React, { useEffect, useId, useRef, useState } from 'react'
import { createCatalogToolSnapshot } from '../runIntent.js'
import {
  PVCASE_REASONS, pvcaseReason, validatePvcaseSource, validatePvcaseCommit, validatePvcaseExport,
  sourceSummary, commitSummary, solveSummary,
} from './solarPvcaseModel.js'

function saveArtifact(bytes, mediaType, filename) {
  const url = URL.createObjectURL(new Blob([bytes], { type: mediaType }))
  const link = document.createElement('a')
  try {
    link.href = url; link.download = filename
    document.body.appendChild(link); link.click()
  } finally { link.remove(); URL.revokeObjectURL(url) }
}
function contextOf(props) {
  const c = props.context ?? props.workspaceContext ?? props
  return {
    tenantId: c.tenantId, orgId: c.orgId ?? null, projectId: c.projectId ?? null,
    drawingId: c.drawingId, drawingVersion: c.drawingVersion, graphRev: c.graphRev,
    checkoutCapability: c.checkoutCapability ?? props.checkoutCapability,
  }
}
export default function SolarPvcasePanel(props) {
  const context = contextOf(props)
  const key = JSON.stringify([context.tenantId, context.orgId, context.projectId, context.drawingId, context.drawingVersion, context.graphRev])
  return <PvcaseScope key={key} {...props} context={context} />
}
function envelope(tool, receipt) {
  return { ok: true, tool, version: '1.0.0', result: receipt, overlay: null, timing_ms: 0, cost: 0, error: null, degraded_mode: false }
}
function publicContext(context) {
  const { tenantId, orgId, projectId, drawingId, drawingVersion, graphRev } = context
  return { tenantId, orgId, projectId, drawingId, drawingVersion, graphRev }
}
function PvcaseScope({ context, client, catalogRows = [], heldCheckout = false, disabled = false, download, save = saveArtifact, onResult, onCommitted, onCounts }) {
  const id = useId()
  const [file, setFile] = useState(null)
  const [source, setSource] = useState(null)
  const [committed, setCommitted] = useState(null)
  const [snapshot, setSnapshot] = useState(null)
  const [exported, setExported] = useState(null)
  const [phase, setPhase] = useState(context.drawingId ? 'selected' : 'no-drawing')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [uncertain, setUncertain] = useState(null)
  const alive = useRef(false)
  const generation = useRef(0)
  const lock = useRef(null)
  const controller = useRef(null)
  const resultFocus = useRef(null)
  const focusWanted = useRef(false)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; generation.current++; controller.current?.abort(); lock.current = null }
  }, [])
  useEffect(() => {
    if (focusWanted.current && !busy && alive.current) {
      focusWanted.current = false
      if (document.activeElement === document.body || resultFocus.current?.contains(document.activeElement)) resultFocus.current?.focus()
    }
  }, [busy, phase])
  const currentContext = committed ? { ...context, drawingVersion: committed.receipt.new_version.version, graphRev: committed.receipt.after_rev } : context
  function rowFor(operation) {
    try {
      const name = `solar-pvcase-${operation}`
      const row = catalogRows.find((r) => r?.name === name)
      const s = createCatalogToolSnapshot(row)
      return s.name === name && typeof s.catalogDigest === 'string' && /^[0-9a-f]{64}$/.test(s.catalogDigest) ? row : null
    } catch { return null }
  }
  function announce(sentence) { setMessage(sentence); setAnnouncement(sentence) }
  function select(event) {
    generation.current++; controller.current?.abort(); lock.current = null
    setBusy(false); setFile(event.target.files?.[0] ?? null); setSource(null); setCommitted(null); setSnapshot(null); setExported(null); setUncertain(null)
    setPhase('selected'); setMessage(''); setAnnouncement(''); focusWanted.current = false
  }
  function prerequisite(action) {
    if (!context.drawingId) return PVCASE_REASONS.drawing
    if (busy || disabled) return PVCASE_REASONS.pending
    if (uncertain && action !== 'observe') return PVCASE_REASONS.unknown
    if (action === 'upload') return file ? '' : PVCASE_REASONS.fileRequired
    if (!source) return PVCASE_REASONS.sourceRequired
    if (action === 'download-source') return typeof (download ?? client?.download) === 'function' ? '' : PVCASE_REASONS.artifactAgain
    if (action === 'retry') return committed && !snapshot ? '' : PVCASE_REASONS.conversionRequired
    if (action === 'observe') return uncertain?.jobId ? '' : PVCASE_REASONS.unknown
    if (action === 'download-assignments') return exported && typeof (download ?? client?.download) === 'function' ? '' : PVCASE_REASONS.solveRequired
    if (!rowFor(action)) return PVCASE_REASONS.catalog
    if (action !== 'export' && !heldCheckout) return PVCASE_REASONS.checkout
    if (committed && !snapshot && action !== 'convert') return PVCASE_REASONS.countsUnavailable
    if (action === 'solve' && (!snapshot || committed?.operation !== 'convert')) return PVCASE_REASONS.conversionRequired
    if (action === 'export' && (!snapshot || committed?.operation !== 'solve')) return PVCASE_REASONS.solveRequired
    return ''
  }
  async function locked(action, work) {
    if (lock.current || prerequisite(action)) return
    const token = { generation: generation.current }
    lock.current = token
    const activeController = new AbortController()
    controller.current = activeController
    const current = () => alive.current && generation.current === token.generation && lock.current === token
    setBusy(true); announce(PVCASE_REASONS.pending)
    try { await work(current, activeController.signal) }
    catch { if (current()) { setPhase('refused'); announce(PVCASE_REASONS.response) } }
    finally { if (current()) { lock.current = null; setBusy(false); focusWanted.current = true } }
  }
  function notify(kind, receipt, counts, current) {
    if (!current()) return
    try {
      const value = { kind, receipt, ...(counts ? { counts } : {}) }
      Promise.resolve(onResult?.(value)).catch(() => {})
      if (!current()) return
      Promise.resolve(kind === 'committed' ? onCommitted?.(receipt) : onCounts?.(receipt, counts)).catch(() => {})
    } catch { /* Callback failure does not undo an accepted receipt. */ }
  }
  async function readCounts(accepted, current, signal) {
    if (!current()) return
    setSnapshot(null); setPhase(accepted.operation === 'convert' ? 'conversion-reading' : 'solve-reading')
    let read
    try { read = await client.readCommittedGraph({ context: accepted.context, receipt: accepted.receipt, signal }) } catch { read = null }
    if (!current()) return
    const summary = read?.ok ? (accepted.operation === 'convert' ? commitSummary : solveSummary)(accepted.receipt, read.value) : null
    if (!summary?.ok) {
      setSnapshot(null); setPhase('counts-unavailable'); announce(PVCASE_REASONS.countsUnavailable)
      return
    }
    setSnapshot(summary.value); setPhase(accepted.operation === 'convert' ? 'converted' : 'solved')
    announce(accepted.operation === 'convert' ? PVCASE_REASONS.converted : PVCASE_REASONS.solved)
    notify('counts', accepted.receipt, summary.value, current)
  }
  function failed(result, operation, captured) {
    const unknown = ['timeout', 'network', 'aborted', 'response'].includes(result?.code)
    if (unknown) {
      setUncertain({ operation, context: publicContext(captured), jobId: result?.jobId ?? null })
      setPhase('outcome-unknown'); announce(PVCASE_REASONS.unknown)
    } else { setPhase('refused'); announce(pvcaseReason(result?.code, result?.status)) }
  }
  async function acceptRun(result, operation, captured, current, signal) {
    if (!current()) return
    if (!result?.ok) { failed(result, operation, captured); return }
    const tool = `solar-pvcase-${operation}`
    const binding = { context: captured, source, jobId: result.value?.job_id, tool }
    const valid = operation === 'export' ? validatePvcaseExport(envelope(tool, result.value), binding) : validatePvcaseCommit(envelope(tool, result.value), binding)
    if (!valid.ok) { failed(valid, operation, captured); return }
    setUncertain(null)
    if (operation === 'export') { setExported(valid.value); setPhase('export-ready'); announce(PVCASE_REASONS.exported); return }
    const accepted = { receipt: valid.value, operation, context: publicContext(captured) }
    setCommitted(accepted); setSnapshot(null); setExported(null)
    announce(operation === 'convert' ? PVCASE_REASONS.converted : PVCASE_REASONS.solved)
    notify('committed', valid.value, null, current)
    if (current()) await readCounts(accepted, current, signal)
  }
  const upload = () => locked('upload', async (current, signal) => {
    setPhase('uploading')
    const result = await client.uploadSource({ drawingId: context.drawingId, projectId: context.projectId, file, signal })
    if (!current()) return
    const valid = result?.ok ? validatePvcaseSource(result.value, context) : result
    if (!valid?.ok) { setPhase('refused'); announce(pvcaseReason(valid?.code, valid?.status)); return }
    if (source?.source.artifact_id !== valid.value.source.artifact_id || source?.source.content_sha256 !== valid.value.source.content_sha256) {
      setCommitted(null); setSnapshot(null); setExported(null); setUncertain(null)
    }
    setSource(valid.value); setPhase('source-ready'); announce(PVCASE_REASONS.admitted)
  })
  const run = (operation) => locked(operation, async (current, signal) => {
    const captured = { ...currentContext }
    setPhase(operation === 'convert' ? 'converting' : operation === 'solve' ? 'solving' : 'exporting')
    let result
    try { result = await client[operation === 'export' ? 'exportAssignments' : operation]({ context: captured, source, catalogRow: rowFor(operation), signal }) }
    catch { result = { ok: false, code: 'network', status: null } }
    if (current()) await acceptRun(result, operation, captured, current, signal)
  })
  const observe = () => locked('observe', async (current, signal) => {
    const retained = uncertain
    const row = rowFor(retained.operation)
    if (!row) { announce(PVCASE_REASONS.catalog); return }
    const result = await client.observeJob({ context: retained.context, source, catalogRow: row, operation: retained.operation, jobId: retained.jobId, signal })
    if (current()) await acceptRun(result, retained.operation, retained.context, current, signal)
  })
  const retry = () => locked('retry', (current, signal) => readCounts(committed, current, signal))
  const downloadFile = (assignment) => locked(assignment ? 'download-assignments' : 'download-source', async (current, signal) => {
    const ref = assignment ? exported.output.artifact : source.source
    const priorPhase = phase
    setPhase('downloading')
    const result = await (download ?? client.download)({ drawingId: context.drawingId, ref, signal, current: false })
    if (!current()) return
    const v = result?.value
    if (!result?.ok) { setPhase('refused'); announce(pvcaseReason(result?.code, result?.status)); return }
    if (!v || v.artifactId !== ref.artifact_id || v.mediaType !== ref.media_type || v.filename !== ref.filename || v.byteLength !== ref.byte_length
      || Object.prototype.toString.call(v.bytes) !== '[object Uint8Array]' || v.bytes.byteLength !== v.byteLength) {
      setPhase('refused'); announce(PVCASE_REASONS.artifactInvalid); return
    }
    try {
      if (!current()) return
      await save(v.bytes, v.mediaType, v.filename)
      if (current()) { setPhase(priorPhase); announce(PVCASE_REASONS.downloaded) }
    } catch { if (current()) { setPhase('refused'); announce(PVCASE_REASONS.save) } }
  })
  const admitted = source ? sourceSummary(source).value : null
  function button(action, label, handler) {
    const reason = prerequisite(action)
    const reasonId = `${id}-${action}`
    return <div><button type="button" disabled={!!reason} aria-describedby={reason ? reasonId : undefined} onClick={handler}>{label}</button>{reason && <p id={reasonId}>{reason}</p>}</div>
  }
  return <section aria-label="PVcase G33 import" data-state={phase}>
    <h3>Import a PVcase G33 source</h3>
    <label htmlFor={`${id}-file`}>G33 JSON file</label>
    <input id={`${id}-file`} type="file" accept="application/json,.json" disabled={busy || disabled || !context.drawingId || !!uncertain} onChange={select} />
    {(busy || disabled || !context.drawingId || uncertain) && <p>{busy || disabled ? PVCASE_REASONS.pending : uncertain ? PVCASE_REASONS.unknown : PVCASE_REASONS.drawing}</p>}
    {file && <p>{file.name}</p>}
    {button('upload', 'Upload G33 source', upload)}
    {admitted && <dl aria-label="Source provenance">
      <dt>Source bytes</dt><dd>{admitted.bytes.toLocaleString('en-US')}</dd>
      <dt>Source schema</dt><dd>{admitted.schema}</dd>
      <dt>Source artifact</dt><dd>{admitted.artifactId}</dd>
      <dt>Source SHA-256</dt><dd>{admitted.sha256}</dd>
      <dt>Source upload version</dt><dd>{admitted.version}</dd>
      <dt>Source graph revision</dt><dd>{admitted.revision}</dd>
      <dt>Source graph SHA-256</dt><dd>{admitted.graphSha256}</dd>
    </dl>}
    {button('convert', 'Convert G33 source', () => run('convert'))}
    {button('solve', 'Run parity solve', () => run('solve'))}
    <p>{PVCASE_REASONS.electrical}</p>
    {button('export', 'Make assignment output', () => run('export'))}
    {button('download-source', 'Download G33 source', () => downloadFile(false))}
    {button('download-assignments', 'Download assignment output', () => downloadFile(true))}
    {uncertain?.jobId && <><p>Job ID: {uncertain.jobId}</p>{button('observe', 'Check retained job', observe)}</>}
    {committed && !snapshot && button('retry', 'Retry reading counts', retry)}
    <div ref={resultFocus} tabIndex={-1} aria-label="G33 result">
      {committed && <p>Committed version {committed.receipt.new_version.version}, graph revision {committed.receipt.after_rev}</p>}
      {snapshot && committed.operation === 'convert' && <p>{snapshot.frames.toLocaleString('en-US')} panel groups, {snapshot.panels.toLocaleString('en-US')} panels, {snapshot.strings.toLocaleString('en-US')} strings, {snapshot.inverters.toLocaleString('en-US')} inverters</p>}
      {snapshot && committed.operation === 'solve' && <p>{snapshot.strings.toLocaleString('en-US')} strings, {snapshot.assignedPanels.toLocaleString('en-US')} panels assigned to a string, {snapshot.assignedInverters.toLocaleString('en-US')} inverters with assignments</p>}
      {exported && <p>Assignment output version {exported.source_version}; artifact {exported.output.artifact.artifact_id}; SHA-256 {exported.output.artifact.content_sha256}</p>}
      {message && <p role={phase === 'refused' || phase === 'counts-unavailable' || phase === 'outcome-unknown' ? 'alert' : undefined}>{message}</p>}
      {phase === 'refused' && file && <p>{PVCASE_REASONS.retained}</p>}
    </div>
    <p role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
  </section>
}
