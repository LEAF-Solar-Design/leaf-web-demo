import { useEffect, useMemo, useRef, useState } from 'react'
import SolarEdgeImportPanel from './SolarEdgeImportPanel.jsx'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const TOOL = 'solar-solaredge-accept'
const HEX = /^[0-9a-f]{64}$/
const DIGEST = /^sha256:[0-9a-f]{64}$/
const choiceKey = (choices) => JSON.stringify(Object.entries(choices).sort(([a], [b]) => a.localeCompare(b)))
const identity = (data, binding) => ['organization_id', 'project_id', 'drawing_id', 'input_version_id']
  .every((key) => data?.[key] === binding?.[key])

function admittedPreview(outcome, digest) {
  if (outcome?.ok !== true || !Array.isArray(outcome.data?.families)) return false
  const rows = outcome.data.families.flatMap((family) => Array.isArray(family?.capabilities) ? family.capabilities : [])
    .filter((row) => row?.name === TOOL)
  return rows.length > 0 && rows.every((row) => row.catalog_digest === digest &&
    row.availability?.admission_checked === true && row.availability.admission?.status_code === 200 &&
    row.availability.admission.error === null &&
    ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].every((key) => row.availability[key] === true) &&
    Array.isArray(row.availability.refusal_reasons) && row.availability.refusal_reasons.length === 0)
}

// Read-only review of the same immutable artifact used for download and acceptance.
// Older matched rows omit status; unresolved rows from the review producer name it.
function groupMatches(bytes, report, binding) {
  const data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  if (data?.schema !== 'leaf.solar-project-solaredge-report.v1' ||
      !['organization_id', 'project_id', 'drawing_id'].every((key) => data[key] === binding[key]) ||
      data.source_version_id !== binding.input_version_id || data.graph_sha256 !== report.graph_sha256 ||
      data.source_intake_sha256 !== report.source_intake_sha256 ||
      data.source?.artifact_id !== report.source_artifact_id || !Array.isArray(data.matches) ||
      data.matches.length > 10_000) throw new Error('Invalid report review')
  const refs = new Set()
  return data.matches.map((match) => {
    if (typeof match?.frame_ref !== 'string' || !match.frame_ref || match.frame_ref.length > 200 ||
        refs.has(match.frame_ref) || !Array.isArray(match.candidates) || match.candidates.length > 16 ||
        match.candidate_count !== match.candidates.length ||
        match.candidates.some((value) => !Number.isSafeInteger(value) || value < 0) ||
        new Set(match.candidates).size !== match.candidates.length) throw new Error('Invalid group match')
    refs.add(match.frame_ref)
    const status = match.status ?? 'matched'
    if ((status === 'matched' && (!Number.isSafeInteger(match.pdf_grid) || match.pdf_grid < 0 ||
          !match.candidates.includes(match.pdf_grid))) ||
        (status === 'unmatched' && (match.pdf_grid !== null || match.candidates.length !== 0)) ||
        (status === 'ambiguous' && (match.pdf_grid !== null || match.candidates.length < 1)) ||
        !['matched', 'unmatched', 'ambiguous'].includes(status)) throw new Error('Invalid match status')
    return { ref: match.frame_ref, status, grid: match.pdf_grid, candidates: match.candidates }
  })
}

// The ordinary panel keeps its legacy contract. This host binds every operation to
// the selected canonical version and hands acceptance to the existing run rail.
export default function ProjectSolarEdgePanel({ solarBinding, authorityKey, projectClient, catalogDigest,
  newProjectIntentKey, onProjectSubmit, isCurrent, busy = false, submissionSignal, onClose }) {
  let binding = null, key = null
  try { binding = createSolarDrawingBinding(solarBinding); key = solarDrawingBindingKey(binding) } catch { /* refusal */ }
  const latest = useRef(null)
  latest.current = { key, binding, authorityKey, projectClient, catalogDigest, isCurrent, busy, submissionSignal, onProjectSubmit }
  const [context, setContext] = useState(null)
  const [refresh, setRefresh] = useState(0)
  const [inspection, setInspection] = useState(null)
  const scope = useMemo(() => ({ key, binding, authorityKey, projectClient, catalogDigest, isCurrent, submissionSignal,
    alive: true, controllers: new Set(), report: null, candidate: null,
    choices: {}, appliedChoices: {}, reviewRequest: null }),
  [key, authorityKey, projectClient, catalogDigest, isCurrent, submissionSignal])
  const available = !!binding && typeof authorityKey === 'string' && !!authorityKey.trim() && DIGEST.test(catalogDigest) &&
    ['readContext', 'uploadSolarEdgePdf', 'reportSolarEdgePdf', 'getSnapshot', 'subscribe', 'captureCatalogScope', 'prepareRun', 'catalog']
      .every((name) => typeof projectClient?.[name] === 'function')
  function current() {
    try {
      const props = latest.current, snapshot = scope.projectClient.getSnapshot()
      return scope.alive && props.key === scope.key && props.authorityKey === scope.authorityKey &&
        props.projectClient === scope.projectClient && props.catalogDigest === scope.catalogDigest &&
        props.isCurrent === scope.isCurrent && props.submissionSignal === scope.submissionSignal &&
        scope.isCurrent?.() === true && !scope.submissionSignal?.aborted &&
        sameSolarDrawingBinding(snapshot.binding, scope.binding) && snapshot.authorityKey === scope.authorityKey
    } catch { return false }
  }
  useEffect(() => {
    scope.alive = true
    scope.report = null
    scope.candidate = null
    setContext(null)
    const controller = new AbortController()
    scope.controllers.add(controller)
    if (available && current()) {
      projectClient.readContext({ signal: controller.signal }).then((outcome) => {
        if (!current() || controller.signal.aborted) return
        const data = outcome?.data, graph = data?.intake?.solar_design_graph
        const valid = outcome?.ok === true && identity(data?.context, binding) &&
          typeof data.context.is_head === 'boolean' && Number.isInteger(graph?.rev) && graph.rev >= 0 && graph.rev <= 2147483647 &&
          HEX.test(data.context.intake_sha256) && HEX.test(data.intake.solar_design_graph_sha256)
        setContext({ scope, valid, rev: valid ? graph.rev : null, isHead: valid && data.context.is_head,
          intake: valid ? data.context.intake_sha256 : null, graph: valid ? data.intake.solar_design_graph_sha256 : null })
      }).catch(() => { if (current() && !controller.signal.aborted) setContext({ scope, valid: false }) })
    }
    let unsubscribe
    try { unsubscribe = projectClient?.subscribe(() => {
      if (!current()) for (const pending of scope.controllers) pending.abort()
    }) } catch { /* refusal */ }
    return () => {
      scope.alive = false
      for (const pending of scope.controllers) pending.abort()
      scope.controllers.clear()
      try { unsubscribe?.() } catch { /* observer isolation */ }
    }
  }, [scope, available, refresh])
  const saved = context?.scope === scope && context.valid ? context : null
  const facade = {
    async uploadPdf({ drawingId, projectId, file, signal }) {
      if (!saved || !current() || drawingId !== binding.drawing_id || projectId !== binding.project_id) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      scope.report = null
      scope.choices = {}
      scope.appliedChoices = {}
      scope.reviewRequest = null
      setInspection(null)
      const outcome = await projectClient.uploadSolarEdgePdf({ file, signal })
      if (!current() || signal?.aborted) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      return outcome.ok && identity(outcome.data, binding) && outcome.data.graph_sha256 === saved.graph && outcome.data.source_intake_sha256 === saved.intake
        ? { ok: true, value: outcome.data } : { ok: false, code: outcome.reason || 'SIP_R7_RESPONSE_INVALID' }
    },
    async requestReport({ drawingId, projectId, sourceArtifactId, alignmentTolerance, selectionOrder, signal }) {
      if (!saved || !current() || drawingId !== binding.drawing_id || projectId !== binding.project_id) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      const requestKey = JSON.stringify([sourceArtifactId, alignmentTolerance, selectionOrder])
      const choices = scope.reviewRequest === requestKey ? { ...scope.choices } : {}
      scope.report = null
      setInspection(null)
      const outcome = await projectClient.reportSolarEdgePdf({ sourceArtifactId, alignmentTolerance, selectionOrder,
        ...(Object.keys(choices).length ? { associations: choices } : {}), signal })
      if (!current() || signal?.aborted) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      if (!outcome.ok || !identity(outcome.data, binding) || outcome.data.graph_sha256 !== saved.graph || outcome.data.source_intake_sha256 !== saved.intake)
        return { ok: false, code: outcome.reason || 'SIP_R7_RESPONSE_INVALID' }
      scope.choices = choices
      scope.appliedChoices = choices
      scope.reviewRequest = requestKey
      return { ok: true, value: outcome.data }
    },
  }
  async function downloadReport(report, { signal, inspect = false } = {}) {
    if (!saved || !current() || latest.current.busy || scope.report !== report ||
        typeof projectClient.downloadArtifact !== 'function') return { ok: false }
    const controller = new AbortController()
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    scope.controllers.add(controller)
    const active = () => current() && !latest.current.busy && !signal?.aborted &&
      !controller.signal.aborted && scope.report === report
    let url
    try {
      if (!active()) return { ok: false }
      const ref = report.report
      const outcome = await projectClient.downloadArtifact(ref, { signal: controller.signal })
      if (!active()) return { ok: false }
      const bytes = outcome?.data?.bytes
      if (outcome?.ok !== true || outcome.data.notModified || !(bytes instanceof Uint8Array)) return { ok: false }
      if (bytes.byteLength !== ref.byte_length) return { ok: false, code: 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH' }
      const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
      if (!active()) return { ok: false }
      const hash = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('')
      if (hash !== ref.content_sha256) return { ok: false, code: 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH' }
      if (inspect) return { ok: true, rows: groupMatches(bytes, report, binding) }
      url = URL.createObjectURL(new Blob([bytes], { type: ref.media_type }))
      const link = document.createElement('a')
      link.href = url
      link.download = ref.filename
      document.body.appendChild(link)
      try { if (!active()) return { ok: false }; link.click() }
      finally { link.remove() }
      return { ok: true }
    } catch { return { ok: false } }
    finally {
      if (url) URL.revokeObjectURL(url)
      signal?.removeEventListener('abort', abort)
      scope.controllers.delete(controller)
    }
  }
  async function inspectReport() {
    const report = scope.report
    if (!report || !current() || latest.current.busy || scope.inspectionPending) return
    scope.inspectionPending = true
    setInspection({ scope, report, pending: true })
    try {
      const outcome = await downloadReport(report, { inspect: true })
      if (current() && scope.report === report) setInspection({ scope, report, pending: false,
        rows: outcome.ok ? outcome.rows : null, error: !outcome.ok })
    } finally { scope.inspectionPending = false }
  }
  async function accept(params) {
    if (!saved?.isHead || !current() || latest.current.busy || scope.accepting || typeof newProjectIntentKey !== 'function' ||
        choiceKey(scope.choices) !== choiceKey(scope.appliedChoices) ||
        typeof latest.current.onProjectSubmit !== 'function' || params.expected_rev !== saved.rev ||
        params.report_artifact_id !== scope.report?.report?.artifact_id) return false
    const controller = new AbortController()
    scope.controllers.add(controller)
    const captured = projectClient.captureCatalogScope()
    const report = scope.report, choices = choiceKey(scope.choices)
    const active = () => current() && !controller.signal.aborted && captured() === true &&
      scope.report === report && choiceKey(scope.choices) === choices
    scope.accepting = true
    setInspection((previous) => previous ? { ...previous } : previous)
    try {
      const fingerprint = JSON.stringify([params, catalogDigest])
      if (scope.candidate?.fingerprint !== fingerprint) scope.candidate = { fingerprint,
        intent: projectClient.prepareRun({ tool: TOOL, params, catalogDigest, idempotencyKey: newProjectIntentKey() }) }
      const intent = scope.candidate.intent
      if (!active()) return false
      const preview = await projectClient.catalog({ intents: [intent], signal: controller.signal })
      if (!active() || latest.current.busy || !admittedPreview(preview, catalogDigest)) return false
      const outcome = await latest.current.onProjectSubmit(intent, { binding, authorityKey,
        signal: controller.signal, isCurrent: active })
      return active() && outcome != null && outcome !== false && outcome.ok !== false
    } catch { return false }
    finally {
      scope.controllers.delete(controller)
      scope.accepting = false
      if (current()) setInspection((previous) => previous ? { ...previous } : previous)
    }
  }
  return <section aria-label="Project SolarEdge import">
    {saved && !saved.isHead && <p role="status">Open the newest drawing version before accepting tracking labels.</p>}
    {!available || !current() ? <p role="status">The selected project drawing changed. Reopen SolarEdge import.</p>
      : !context || context.scope !== scope ? <p role="status">Reading the selected project drawing version.</p>
        : !saved ? <><p role="status">The saved Solar design could not be read. Create panel groups before matching the PDF.</p>
          <button type="button" onClick={() => setRefresh((value) => value + 1)}>Read design again</button></>
          : <SolarEdgeImportPanel key={key} drawingId={binding.drawing_id} projectId={binding.project_id}
            drawingVersion={binding.input_version_id} graphRev={saved.rev} client={facade} disabled={busy || scope.accepting}
            onReportReady={(report) => { if (current()) {
              scope.report = report
              setInspection({ scope, report, pending: false })
            } }} onAccept={accept}
            onDownloadReport={downloadReport}
            acceptDisabledReason={!saved.isHead ? 'Open the newest drawing version before accepting tracking labels.'
              : choiceKey(scope.choices) !== choiceKey(scope.appliedChoices) ? 'Build the report to apply or clear your association choices.' : null} />}
    {current() && inspection?.scope === scope && inspection.report === scope.report && <section aria-label="Group match inspection">
      <button type="button" disabled={busy || inspection.pending} onClick={inspectReport}>Inspect group matches</button>
      {inspection.pending && <p role="status">Reading and verifying the report.</p>}
      {inspection.error && <p role="status">Group matches could not be verified. Try inspecting the report again.</p>}
      {inspection.rows && <>
        <p>These are report associations, not electrical design verification. Unmatched and ambiguous groups need correction before acceptance.</p>
        <p>Choose a PDF candidate, then use Build report to review the correction. Clear choices and build again to restore automatic matching.</p>
        <button type="button" disabled={busy || scope.accepting || inspection.pending || Object.keys(scope.choices).length === 0}
          onClick={() => { if (current() && !scope.accepting) { scope.choices = {}; setInspection({ ...inspection }) } }}>Clear association choices</button>
        <table aria-label="SolarEdge group matches"><thead><tr>
          <th scope="col">Panel group</th><th scope="col">Match status</th><th scope="col">PDF association</th><th scope="col">Correction</th>
        </tr></thead><tbody>{inspection.rows.map((row) => <tr key={row.ref}>
          <th scope="row">{row.ref}</th>
          <td>{row.status === 'matched' ? 'Matched' : row.status === 'unmatched' ? 'Unmatched' : 'Ambiguous'}</td>
          <td>{row.status === 'matched' ? `PDF grid ${row.grid}` : row.candidates.length
            ? row.candidates.map((grid) => `PDF grid ${grid}`).join(', ') : 'No PDF grid matches'}</td>
          <td><select aria-label={`PDF choice for ${row.ref}`} value={scope.choices[row.ref] ?? ''}
            disabled={busy || scope.accepting || inspection.pending || row.candidates.length === 0}
            onChange={(event) => {
              if (!current() || scope.accepting || scope.report !== inspection.report || latest.current.busy) return
              const choices = { ...scope.choices }, value = event.target.value
              if (value === '') delete choices[row.ref]
              else {
                const grid = Number(value)
                if (!row.candidates.includes(grid) || Object.entries(choices).some(([ref, chosen]) => ref !== row.ref && chosen === grid)) return
                choices[row.ref] = grid
              }
              scope.choices = choices
              setInspection({ ...inspection })
            }}><option value="">Automatic review</option>{row.candidates.map((grid) =>
              <option key={grid} value={grid} disabled={Object.entries(scope.choices).some(([ref, chosen]) => ref !== row.ref && chosen === grid)}>Candidate grid {grid}</option>)}</select></td>
        </tr>)}</tbody></table>
      </>}
    </section>}
    <button type="button" onClick={onClose}>Close</button>
  </section>
}
