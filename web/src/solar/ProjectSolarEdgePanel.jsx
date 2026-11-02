import { useEffect, useMemo, useRef, useState } from 'react'
import SolarEdgeImportPanel from './SolarEdgeImportPanel.jsx'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const TOOL = 'solar-solaredge-accept'
const HEX = /^[0-9a-f]{64}$/
const DIGEST = /^sha256:[0-9a-f]{64}$/
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
  const scope = useMemo(() => ({ key, binding, authorityKey, projectClient, catalogDigest, isCurrent, submissionSignal,
    alive: true, controllers: new Set(), report: null, candidate: null }),
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
      const outcome = await projectClient.uploadSolarEdgePdf({ file, signal })
      if (!current() || signal?.aborted) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      return outcome.ok && identity(outcome.data, binding) && outcome.data.graph_sha256 === saved.graph && outcome.data.source_intake_sha256 === saved.intake
        ? { ok: true, value: outcome.data } : { ok: false, code: outcome.reason || 'SIP_R7_RESPONSE_INVALID' }
    },
    async requestReport({ drawingId, projectId, sourceArtifactId, alignmentTolerance, selectionOrder, signal }) {
      if (!saved || !current() || drawingId !== binding.drawing_id || projectId !== binding.project_id) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      const outcome = await projectClient.reportSolarEdgePdf({ sourceArtifactId, alignmentTolerance, selectionOrder, signal })
      if (!current() || signal?.aborted) return { ok: false, code: 'SIP_R7_SCOPE_STALE' }
      return outcome.ok && identity(outcome.data, binding) && outcome.data.graph_sha256 === saved.graph && outcome.data.source_intake_sha256 === saved.intake
        ? { ok: true, value: outcome.data } : { ok: false, code: outcome.reason || 'SIP_R7_RESPONSE_INVALID' }
    },
  }
  async function accept(params) {
    if (!saved?.isHead || !current() || latest.current.busy || typeof newProjectIntentKey !== 'function' ||
        typeof latest.current.onProjectSubmit !== 'function' || params.expected_rev !== saved.rev ||
        params.report_artifact_id !== scope.report?.report?.artifact_id) return false
    const controller = new AbortController()
    scope.controllers.add(controller)
    const captured = projectClient.captureCatalogScope()
    const active = () => current() && !controller.signal.aborted && captured() === true
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
    finally { scope.controllers.delete(controller) }
  }
  return <section aria-label="Project SolarEdge import">
    {saved && !saved.isHead && <p role="status">Open the newest drawing version before accepting tracking labels.</p>}
    {!available || !current() ? <p role="status">The selected project drawing changed. Reopen SolarEdge import.</p>
      : !context || context.scope !== scope ? <p role="status">Reading the selected project drawing version.</p>
        : !saved ? <><p role="status">The saved Solar design could not be read. Create panel groups before matching the PDF.</p>
          <button type="button" onClick={() => setRefresh((value) => value + 1)}>Read design again</button></>
          : <SolarEdgeImportPanel key={key} drawingId={binding.drawing_id} projectId={binding.project_id}
            drawingVersion={binding.input_version_id} graphRev={saved.rev} client={facade} disabled={busy}
            onReportReady={(report) => { if (current()) scope.report = report }} onAccept={accept}
            acceptDisabledReason={!saved.isHead ? 'Open the newest drawing version before accepting tracking labels.' : null} />}
    <button type="button" onClick={onClose}>Close</button>
  </section>
}
