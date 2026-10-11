import { useEffect, useRef, useState } from 'react'
import { solarSizingRunSentence } from './solarSizingRunReasons.js'
import { buildSizingParams, MODULE_PARAMETER_KEYS, sizingGraph, sizingTargets, SOLAR_SIZING_REASONS,
  deriveProjectSizingState, buildProjectSizingRequest, exactProjectSizingConductorPreview,
  projectSizingConductorReason, createProjectSizingConductorBinding } from './solarSizingModel.js'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const projectReasonCode = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}(?![\\s\\S])')
const projectJobId = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\\s\\S])')

export function projectSizingConductorFeedback(outcome, admission = null, phase = 'submission') {
  const code = [admission?.error?.reason_code, outcome?.envelope?.error?.reason_code, outcome?.reason]
    .find((value) => typeof value === 'string' && projectReasonCode.test(value)) ?? null
  let reason = 'preview_refused'
  if (code === 'project_adapter_unavailable' || code === 'SIP_R3_TOOL_UNSUPPORTED') reason = 'project_adapter_unavailable'
  else if (code === 'SIP_R3_SERVICE_EVIDENCE_REQUIRED') reason = 'manual_only'
  else if (['SIP_R1_CHECKOUT_REQUIRED', 'SIP_R1_CHECKOUT_EXPIRED', 'SIP_R1_CHECKOUT_STALE',
    'SIP_R3_CHECKOUT_REQUIRED', 'SIP_R3_CHECKOUT_EXPIRED', 'SIP_R3_CHECKOUT_STALE',
    'CHECKOUT_REQUIRED', 'CHECKOUT_EXPIRED', 'CHECKOUT_STALE'].includes(code)) reason = 'checkout_required'
  else if (outcome?.status === 401 || outcome?.status === 403) reason = 'access_denied'
  else if (code === 'SIP_R7_SCOPE_STALE') reason = 'scope_stale'
  else if (code === 'SIP_R7_TIMEOUT') reason = 'request_timeout'
  else if (code === 'SIP_R7_ABORTED') reason = 'request_cancelled'
  else if ((phase === 'transport' && !outcome) || outcome?.status >= 500 || code === 'SIP_R7_TRANSPORT_FAILED') reason = 'service_unavailable'
  else if (['SIP_R7_REQUEST_INVALID', 'SIP_R7_RESPONSE_INVALID'].includes(code)
    || typeof outcome !== 'object' || outcome === null || Array.isArray(outcome) || typeof outcome.ok !== 'boolean'
    || (phase === 'submission' && outcome.ok && (outcome.status !== 202 || outcome.data?.status !== 'submitted'
      || typeof outcome.data.job_id !== 'string' || !projectJobId.test(outcome.data.job_id))) ) reason = 'request_invalid'
  return { outcome, admission, reason, text: projectSizingConductorReason(reason), code }
}

// The two project forms share the exact-intent lifecycle; standalone components never enter it.
export function useProjectSizingConductor(props, { toolName, deriveState, buildRequest, declarationIdentity = '',
  declarationValid = true, declarationCurrent = () => true }) {
  const { solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey, onProjectSubmit } = props
  let binding = null, bindingKey = null
  try { binding = createProjectSizingConductorBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding) } catch { /* local refusal */ }
  // SolarStepEditor marks project mode with projectId true when its caller passes only the canonical
  // project props; any other supplied projectId must be exactly the binding's own project id.
  if (props.projectId != null && props.projectId !== true && props.projectId !== binding?.project_id) {
    binding = null
    bindingKey = null
  }
  const latest = useRef(props)
  latest.current = props
  const owner = useRef(null)
  const sequence = useRef(0)
  if (!owner.current || owner.current.bindingKey !== bindingKey || owner.current.authorityKey !== authorityKey
      || owner.current.client !== projectClient) {
    if (owner.current) {
      owner.current.alive = false
      owner.current.cancelTouch?.()
      for (const controller of owner.current.controllers) controller.abort()
    }
    owner.current = { id: ++sequence.current, bindingKey, binding, authorityKey, client: projectClient,
      alive: true, generation: 0, controllers: new Set(), candidateControllers: new Set(), predicates: new Set(),
      candidate: null, armed: null, retry: null, lock: null, digest: catalogDigest, declarationIdentity }
  }
  const scope = owner.current
  function invalidateCandidate() {
    scope.cancelTouch?.()
    scope.generation += 1
    scope.armed = null
    scope.candidate = null
    scope.retry = null
    for (const controller of scope.candidateControllers) controller.abort()
  }
  if (scope.digest !== catalogDigest || scope.declarationIdentity !== declarationIdentity) {
    invalidateCandidate()
    scope.digest = catalogDigest
    scope.declarationIdentity = declarationIdentity
  }
  const [loaded, setLoaded] = useState(null)
  const [publication, setPublication] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [refresh, setRefresh] = useState(0)
  const [submitting, setSubmitting] = useState(false)
  const dependencies = binding && typeof authorityKey === 'string' && authorityKey.trim().length > 0
    && new TextEncoder().encode(authorityKey).byteLength <= 256
    && projectClient && ['readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'subscribe', 'getSnapshot']
      .every((key) => typeof projectClient[key] === 'function')
  const available = Boolean(dependencies && typeof newProjectIntentKey === 'function' && typeof onProjectSubmit === 'function')
  const isScopeCurrent = () => {
    const current = latest.current
    const snapshot = projectClient?.getSnapshot?.()
    return scope.alive && owner.current === scope && current.projectClient === scope.client
      && current.authorityKey === scope.authorityKey && sameSolarDrawingBinding(current.solarBinding, scope.binding)
      && (current.projectId == null || current.projectId === true || current.projectId === scope.binding?.project_id)
      && sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot?.authorityKey === scope.authorityKey
  }
  const operation = (candidate = true) => {
    const generation = scope.generation
    const digest = catalogDigest
    const predicate = projectClient.captureCatalogScope()
    const controller = new AbortController()
    scope.controllers.add(controller)
    if (candidate) scope.candidateControllers.add(controller)
    scope.predicates.add(predicate)
    return { signal: controller.signal, checkoutCurrent: predicate,
      isCurrent: () => isScopeCurrent() && !controller.signal.aborted && predicate()
        && (!candidate || (scope.generation === generation && latest.current.catalogDigest === digest
          && latest.current.row?.name === toolName && declarationCurrent())),
      abort: () => controller.abort(),
      finish: () => {
        scope.controllers.delete(controller)
        scope.candidateControllers.delete(controller)
        scope.predicates.delete(predicate)
      } }
  }
  useEffect(() => {
    scope.alive = true
    setLoaded(null)
    setPublication(null)
    setFeedback(null)
    setSubmitting(false)
    if (!available) return () => { scope.alive = false }
    if (!isScopeCurrent()) {
      setLoaded({ scope, state: { mode: 'refused', reason: 'scope_stale' } })
      return () => { scope.alive = false }
    }
    let read = null
    let contextSettled = false
    const startRead = () => {
      const current = operation(false)
      read = current
      ;(async () => {
        try {
          const outcome = await projectClient.readContext({ signal: current.signal })
          if (!current.isCurrent()) return
          contextSettled = true
          setLoaded({ scope, state: outcome.ok ? deriveState({ binding, contextView: outcome.data })
            : { mode: 'refused', reason: 'context_unreadable' } })
          if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...projectSizingConductorFeedback(outcome) })
        } catch {
          if (current.isCurrent()) {
            contextSettled = true
            setLoaded({ scope, state: { mode: 'refused', reason: 'context_unreadable' } })
          }
        } finally { current.finish() }
      })()
    }
    const unsubscribe = projectClient.subscribe(() => {
      if (!scope.alive || owner.current !== scope) return
      if (!isScopeCurrent() || (scope.previewCurrent && !scope.previewCurrent())
          || (scope.armed && !scope.armed.isCurrent()) || [...scope.predicates].some((predicate) => !predicate())) {
        scope.cancelTouch?.()
        scope.armed = null
        scope.retry = null
        scope.generation += 1
        for (const controller of scope.controllers) controller.abort()
        scope.predicates.clear()
        scope.previewCurrent = null
        setPublication(null)
        setRefresh((value) => value + 1)
      }
      if (!contextSettled && isScopeCurrent() && (!read || read.signal.aborted)) startRead()
    })
    startRead()
    return () => {
      scope.cancelTouch?.()
      scope.alive = false
      scope.generation += 1
      scope.armed = null
      read?.abort()
      for (const controller of scope.controllers) controller.abort()
      unsubscribe()
    }
  }, [scope, available])

  const state = !binding ? { mode: 'refused', reason: 'binding_invalid' }
    : !available ? { mode: 'refused', reason: 'submit_unavailable' }
      : loaded?.scope === scope ? loaded.state : null
  const editable = state?.mode === 'edit'
  const result = editable ? (props.row?.name !== toolName || !declarationValid
    ? { ok: false, reason: 'request_invalid' }
    : buildRequest({ binding, state, catalogDigest, idempotencyKey: 'candidate-validation', scope })) : null
  const fingerprint = result?.ok ? JSON.stringify([result.request, declarationIdentity]) : null
  if (scope.fingerprint !== fingerprint) {
    invalidateCandidate()
    scope.fingerprint = fingerprint
  }
  useEffect(() => {
    scope.armed = null
    scope.retry = null
    setPublication(null)
    if (!fingerprint || !available || !isScopeCurrent()) return undefined
    const preview = operation()
    if (!preview.isCurrent()) { preview.finish(); return undefined }
    let candidate = scope.candidate
    try {
      if (!candidate || candidate.fingerprint !== fingerprint) {
        const key = latest.current.newProjectIntentKey()
        const built = buildRequest({ binding, state, catalogDigest, idempotencyKey: key, scope })
        if (!built.ok) {
          if (preview.isCurrent()) setPublication({ scope, generation: scope.generation, reason: built.reason })
          preview.finish()
          return undefined
        }
        const intent = projectClient.prepareRun(built.request)
        if (!preview.isCurrent()) { preview.finish(); return undefined }
        candidate = { fingerprint, intent }
        scope.candidate = candidate
      }
    } catch {
      if (preview.isCurrent()) setPublication({ scope, generation: scope.generation, reason: 'request_invalid' })
      preview.finish()
      return undefined
    }
    if (!preview.isCurrent()) { preview.finish(); return undefined }
    scope.previewCurrent = preview.checkoutCurrent
    ;(async () => {
      try {
        const outcome = await projectClient.catalog({ intents: [candidate.intent], signal: preview.signal })
        if (!preview.isCurrent()) return
        const exact = exactProjectSizingConductorPreview(outcome, toolName, catalogDigest)
        if (exact.accepted) {
          scope.armed = { candidate, generation: scope.generation, isCurrent: preview.isCurrent }
          setPublication({ scope, generation: scope.generation, reason: null })
          setFeedback(scope.retry?.candidate === candidate
            ? { scope, generation: scope.generation, ...scope.retry.feedback } : null)
        } else {
          const message = projectSizingConductorFeedback(outcome, exact.admission, 'preview')
          setPublication({ scope, generation: scope.generation, reason: message.reason })
          setFeedback({ scope, generation: scope.generation, ...message })
        }
      } catch {
        if (preview.isCurrent()) {
          const message = projectSizingConductorFeedback(null, null, 'transport')
          setPublication({ scope, generation: scope.generation, reason: message.reason })
          setFeedback({ scope, generation: scope.generation, ...message })
        }
      } finally { preview.finish() }
    })()
    return () => preview.abort()
  }, [scope, fingerprint, available, refresh])

  const armed = scope.armed && scope.armed.generation === scope.generation && scope.armed.isCurrent()
    && scope.armed.candidate.fingerprint === fingerprint
  const reason = state?.mode === 'refused' ? state.reason : !available ? 'submit_unavailable'
    : !isScopeCurrent() ? 'scope_stale' : result && !result.ok ? result.reason
      : !armed ? (publication?.scope === scope && publication.generation === scope.generation
        ? publication.reason || 'preview_required' : 'preview_required') : null
  const runMessage = feedback?.scope === scope && feedback.generation === scope.generation ? feedback : null
  async function submit(event) {
    event?.preventDefault()
    if (reason || !armed || scope.lock || !isScopeCurrent() || !scope.armed?.isCurrent()
        || scope.armed.generation !== scope.generation || scope.armed.candidate.fingerprint !== fingerprint) return
    const accepted = scope.armed
    const pending = operation()
    const lock = {}
    scope.lock = lock
    setSubmitting(true)
    try {
      const outcome = await projectClient.readContext({ signal: pending.signal })
      if (!pending.isCurrent() || scope.armed !== accepted) return
      const checked = outcome.ok ? deriveState({ binding, contextView: outcome.data })
        : { mode: 'refused', reason: 'context_unreadable' }
      if (checked.mode !== 'edit' || checked.mode !== state.mode || checked.intakeSha256 !== state.intakeSha256
          || checked.graphSha256 !== state.graphSha256 || checked.rev !== state.rev
          || !sameSolarDrawingBinding(checked.binding, state.binding)) {
        scope.armed = null
        if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...projectSizingConductorFeedback(outcome) })
        setPublication({ scope, generation: scope.generation,
          reason: checked.reason === 'not_current_head' ? 'not_current_head' : 'scope_stale' })
        return
      }
      if (!pending.isCurrent() || typeof latest.current.onProjectSubmit !== 'function') return
      const result = await latest.current.onProjectSubmit(accepted.candidate.intent,
        { binding, authorityKey, signal: pending.signal, isCurrent: pending.isCurrent })
      if (!pending.isCurrent()) return
      if (result?.ok === true && result.status === 202 && result.data?.status === 'submitted'
          && typeof result.data.job_id === 'string' && projectJobId.test(result.data.job_id)) {
        scope.retry = null
        setFeedback({ scope, generation: scope.generation, outcome: result, text: 'Request submitted.', code: null })
      } else {
        const message = projectSizingConductorFeedback(result)
        scope.retry = { candidate: accepted.candidate, feedback: message }
        setFeedback({ scope, generation: scope.generation, ...message })
      }
    } catch {
      if (pending.isCurrent()) {
        const message = projectSizingConductorFeedback(null, null, 'transport')
        scope.retry = { candidate: accepted.candidate, feedback: message }
        setFeedback({ scope, generation: scope.generation, ...message })
      }
    } finally {
      if (scope.lock === lock) {
        scope.lock = null
        if (isScopeCurrent()) setSubmitting(false)
      }
      pending.finish()
    }
  }
  const retryAvailable = Boolean(scope.retry?.candidate && scope.retry.candidate === scope.candidate && isScopeCurrent())
  return { scope, state, editable, reason, armed, submitting, runMessage, retryAvailable, submit, isScopeCurrent,
    invalidateCandidate: () => {
      invalidateCandidate()
      setRefresh((value) => value + 1)
    }, refreshPreview: () => {
      scope.cancelTouch?.()
      scope.generation += 1
      scope.armed = null
      for (const controller of scope.candidateControllers) controller.abort()
      setPublication(null)
      setRefresh((value) => value + 1)
    } }
}

function ProjectSolarSizingForm(props) {
  const form = useProjectSizingConductor(props, { toolName: 'solar-size-strings',
    deriveState: deriveProjectSizingState, buildRequest: buildProjectSizingRequest })
  const { state, editable, reason, submitting, runMessage, retryAvailable, submit } = form
  const sentence = projectSizingConductorReason(reason)
    || (reason === 'sizing_panels_required' ? SOLAR_SIZING_REASONS.sizing_panels_required
      : reason === 'sizing_manual_length_required' ? SOLAR_SIZING_REASONS.sizing_manual_length_required : null)
  return <div data-testid="project-solar-sizing-form" data-mode={state?.mode ?? 'loading'}>
    {state === null && <p role="status">Reading the drawing.</p>}
    {editable && <>
      <label>Scope<select value="manual-global" disabled>
        <option value="manual-global">Saved global length</option>
      </select></label>
      {Number.isInteger(state.graph.savedLength) && state.graph.savedLength >= 1 && state.graph.savedLength <= 4096
        && <p>Saved global string length: {state.graph.savedLength}</p>}
      <p>Change the length in Solar settings.</p>
      <p>Manual confirmation does not calculate voltage limits or module power.</p>
    </>}
    {sentence && <p role="status">{sentence}</p>}
    {runMessage && <p role="status" data-testid="project-solar-sizing-feedback">
      {runMessage.text}{runMessage.code ? ` (${runMessage.code})` : ''}
    </p>}
    <button type="button" disabled={Boolean(reason) || submitting || !editable} onClick={submit}>Review &amp; run</button>
    {retryAvailable && <button type="button"
      disabled={Boolean(reason) || submitting || !editable} onClick={submit}>Retry</button>}
    {editable && <button type="button" disabled={submitting} onClick={form.refreshPreview}>Refresh preview</button>}
    {props.onClose && <button type="button" onClick={props.onClose}>Cancel</button>}
  </div>
}

export default function SolarSizingForm(props) {
  if (Object.prototype.hasOwnProperty.call(props, 'solarBinding') || props.projectId != null) {
    return <ProjectSolarSizingForm {...props} />
  }
  return <StandaloneSolarSizingForm {...props} />
}

function StandaloneSolarSizingForm({
  row, drawingId, drawingVersion, projectId = null, readIntake, status = null,
  failureCode = null, onSubmit, onClose,
}) {
  const [loaded, setLoaded] = useState(null)
  const [mode, setMode] = useState('manual-global')
  const [draft, setDraft] = useState({})
  const [refresh, setRefresh] = useState(0)
  const previousStatus = useRef(status)
  const reader = useRef(readIntake)
  reader.current = readIntake
  const lock = useRef(false)
  const scoped = projectId === null && typeof drawingId === 'string' && drawingId.length > 0

  useEffect(() => {
    let current = true
    setLoaded(null)
    if (!scoped) return undefined
    Promise.resolve().then(() => reader.current(drawingId, drawingVersion))
      .then((view) => {
        if (current) setLoaded({ drawingId, drawingVersion, graph: sizingGraph(view, drawingVersion),
          savedZip: view?.intake?.solar_design_graph?.project?.zip_code })
      })
      .catch(() => { if (current) setLoaded(null) })
    return () => { current = false }
  }, [drawingId, drawingVersion, scoped, refresh])

  useEffect(() => {
    if (status === 'finished' && previousStatus.current !== 'finished') setRefresh((value) => value + 1)
    previousStatus.current = status
  }, [status])

  useEffect(() => { lock.current = false })

  const graph = loaded?.drawingId === drawingId && loaded?.drawingVersion === drawingVersion ? loaded.graph : null
  const built = !scoped ? { ok: false, reason: 'sizing_project_scope' }
    : buildSizingParams({ graph, mode, draft })
  const targets = scoped && graph?.ok ? sizingTargets(graph, mode) : null
  const busy = status === 'pending'
  const invalid = new Set(built.invalid || [])
  const change = (key, value) => setDraft((previous) => ({ ...previous, [key]: value }))
  function input(key, label) {
    return <label key={key}>{label}<input value={draft[key] ?? ''} disabled={busy}
      aria-invalid={invalid.has(key) || undefined} onChange={(event) => change(key, event.target.value)} /></label>
  }
  function boolean(key, label) {
    return <label>{label}<select value={draft[key] === undefined ? '' : String(draft[key])} disabled={busy}
      aria-invalid={invalid.has(key) || undefined}
      onChange={(event) => change(key, event.target.value === '' ? undefined : event.target.value === 'true')}>
      <option value="">Choose</option><option value="true">Yes</option><option value="false">No</option>
    </select></label>
  }
  function submit() {
    if (lock.current || busy || !built.ok) return
    lock.current = true
    onSubmit(row, built.params)
  }

  return <div>
    <label>Scope<select value={mode} disabled={busy} onChange={(event) => setMode(event.target.value)}>
      <option value="manual-global">Saved global length</option>
      <option value="global">Global</option><option value="zones">Zones</option>
    </select></label>
    {scoped && graph?.ok && <p>Saved project ZIP: {loaded.savedZip}</p>}
    {mode === 'manual-global' && <>
      {graph?.ok && <p>Saved global string length: {graph.savedLength}</p>}
      <p>Change the length in Solar settings.</p>
      <p>Manual confirmation does not calculate voltage limits or module power.</p>
    </>}
    {mode !== 'manual-global' && <>
    {targets?.ok && targets.targets.map((id, index) => <div key={id}>
      <p>{id}</p>
      {mode === 'zones' && <p>Module: {graph.zones[index].module_model}; Inverter: {graph.zones[index].inverter_model_a}</p>}
      <p>{Object.hasOwn(graph.power, id) && graph.power[id] != null ? `Confirmed module power: ${graph.power[id]} W` : 'Confirmed module power is unavailable. Re-size strings.'}</p>
    </div>)}
    {mode === 'global' && <>{input('module_name', 'Module')}{input('full_inverter_name', 'Inverter')}</>}
    {boolean('bifacial', 'Bifacial')}
    {input('bifacial_coefficient', 'Bifacial coefficient')}
    <label>Racking<select value={draft.racking_type ?? ''} disabled={busy}
      aria-invalid={invalid.has('racking_type') || undefined} onChange={(event) => change('racking_type', event.target.value)}>
      <option value="">Choose</option><option value="fixed_tilt">Fixed tilt</option><option value="single_axis">Single axis</option>
    </select></label>
    {input('surface_tilt', 'Surface tilt')}{input('surface_azimuth', 'Surface azimuth')}{input('albedo', 'Albedo')}
    {draft.racking_type === 'single_axis' && <>
      {input('axis_tilt', 'Axis tilt')}{input('axis_azimuth', 'Axis azimuth')}
      {input('max_angle', 'Max angle')}{input('gcr', 'Ground coverage ratio')}{boolean('backtrack', 'Backtrack')}
    </>}
    {input('max_voltage', 'Maximum voltage')}{input('thermal_model_type', 'Thermal model')}
    {boolean('open_circuit_rise', 'Open circuit rise')}{input('grant_ref', 'Grant reference')}
    <label><input type="checkbox" checked={draft.use_module_parameters === true} disabled={busy}
      onChange={(event) => change('use_module_parameters', event.target.checked)} />Use module parameters</label>
    {draft.use_module_parameters && MODULE_PARAMETER_KEYS.map((key) => input(key, key))}
    </>}
    {!built.ok && <p role="status">{SOLAR_SIZING_REASONS[built.reason]}</p>}
    {busy && <p role="status">This step is running. Confirm or wait for it to finish.</p>}
    {status === 'failed' && <p role="alert">{solarSizingRunSentence(failureCode)}</p>}
    {status === 'finished' && <p role="status">String sizing applied.</p>}
    <button type="button" disabled={busy || !built.ok} onClick={submit}>Review &amp; run</button>
    {status === 'failed' && <button type="button" disabled={busy || !built.ok} onClick={submit}>Retry</button>}
    {onClose && <button type="button" onClick={onClose}>Cancel</button>}
  </div>
}
