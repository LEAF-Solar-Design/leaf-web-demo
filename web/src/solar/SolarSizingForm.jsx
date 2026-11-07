import { useEffect, useId, useRef, useState } from 'react'
import { readProjectSolarWorkerRefusal, projectSolarWorkerRefusal } from './solarProjectCompletion.js'
import { humanizeError } from '../errorHumanize.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { solarSizingRunSentence, SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'
import { buildSizingParams, MODULE_PARAMETER_KEYS, sizingGraph, sizingTargets, SOLAR_SIZING_REASONS,
  deriveProjectSizingState, buildProjectSizingRequest, exactProjectSizingConductorPreview,
  projectSizingConductorReason, projectSizingCloudReason, createProjectSizingConductorBinding } from './solarSizingModel.js'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const projectReasonCode = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}(?![\\s\\S])')
const projectJobId = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\\s\\S])')
const previewTimeoutSentence = 'The catalog preview did not finish in time. Select Refresh preview to try again.'

export const MODULE_PARAMETER_LABELS = Object.freeze({
  V_oc_ref: 'Open circuit voltage at reference conditions (V)',
  I_sc_ref: 'Short circuit current at reference conditions (A)',
  V_mp_ref: 'Maximum power voltage at reference conditions (V)',
  I_mp_ref: 'Maximum power current at reference conditions (A)',
  alpha_sc: 'Short circuit current temperature coefficient (A/°C)',
  beta_oc: 'Open circuit voltage temperature coefficient (V/°C)',
  STC: 'Power at standard test conditions (W)',
  gamma_r: 'Power temperature coefficient (%/°C)',
  T_NOCT: 'Nominal operating cell temperature (°C)',
  N_s: 'Cells in series',
})

export function projectSizingConductorFeedback(outcome, admission = null, phase = 'submission') {
  const code = [admission?.error?.reason_code, outcome?.envelope?.error?.reason_code, outcome?.reason]
    .find((value) => typeof value === 'string' && projectReasonCode.test(value)) ?? null
  switch (code) {
    case 'CLOUD_AUTH_MISSING':
    case 'cloud_auth_missing': return { outcome, admission, reason: 'cloud_auth_missing',
      text: SOLAR_SIZING_RUN_REASONS.grant_missing, code }
    case 'CLOUD_TENANT_UNAUTHORIZED':
    case 'cloud_tenant_unauthorized': return { outcome, admission, reason: 'cloud_tenant_unauthorized',
      text: SOLAR_SIZING_RUN_REASONS.grant_scope, code }
    case 'CLOUD_UPSTREAM_FAILURE':
    case 'cloud_upstream_failure': return { outcome, admission, reason: 'cloud_upstream_failure',
      text: SOLAR_SIZING_RUN_REASONS.cloud_unavailable, code }
    case 'CLOUD_RESPONSE_INVALID':
    case 'cloud_response_invalid': return { outcome, admission, reason: 'cloud_response_invalid',
      text: SOLAR_SIZING_RUN_REASONS.cloud_response_invalid, code }
    case 'CLOUD_REQUEST_INVALID':
    case 'cloud_request_invalid': return { outcome, admission, reason: 'cloud_request_invalid',
      text: SOLAR_SIZING_RUN_REASONS.request_invalid, code }
    default: break
  }
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
  return { outcome, admission, reason, phase,
    text: reason === 'request_timeout' && phase === 'preview' ? previewTimeoutSentence
      : reason === 'request_timeout' && phase === 'context' ? 'The drawing check did not finish in time.'
        : projectSizingConductorReason(reason), code }
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
      owner.current.cancelJob?.()
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
    if (scope.declarationIdentity !== declarationIdentity) scope.cancelJob?.()
    invalidateCandidate()
    scope.digest = catalogDigest
    scope.declarationIdentity = declarationIdentity
  }
  const [loaded, setLoaded] = useState(null)
  const [publication, setPublication] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [refresh, setRefresh] = useState(0)
  const [submitting, setSubmitting] = useState(false)
  const [terminalJob, setTerminalJob] = useState(null)
  if (terminalJob && (terminalJob.scope !== scope || terminalJob.declarationIdentity !== declarationIdentity)) {
    setTerminalJob(null)
  }
  const dependencies = binding && typeof authorityKey === 'string' && authorityKey.trim().length > 0
    && new TextEncoder().encode(authorityKey).byteLength <= 256
    && projectClient && ['readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'subscribe', 'getSnapshot']
      .every((key) => typeof projectClient[key] === 'function')
  const available = Boolean(dependencies && typeof newProjectIntentKey === 'function' && typeof onProjectSubmit === 'function')
  // Keep sizing loaded when the workspace temporarily withholds submission callbacks.
  const isSizing = toolName === 'solar-size-strings'
  const readable = isSizing ? Boolean(dependencies) : available
  const busy = isSizing && props.busy
  const previewAvailable = available && !busy
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
    if (!readable) return () => { scope.alive = false }
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
          if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...projectSizingConductorFeedback(outcome, null, 'context') })
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
        // Catalog invalidation must not interrupt observation of an already submitted job.
        read?.abort()
        for (const controller of scope.candidateControllers) controller.abort()
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
      scope.cancelJob?.()
      scope.alive = false
      scope.generation += 1
      scope.armed = null
      read?.abort()
      for (const controller of scope.controllers) controller.abort()
      unsubscribe()
    }
  }, [scope, readable])

  const state = !binding ? { mode: 'refused', reason: 'binding_invalid' }
    : !readable ? { mode: 'refused', reason: 'submit_unavailable' }
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
    if (!fingerprint || !previewAvailable || !isScopeCurrent()) return undefined
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
  }, [scope, fingerprint, previewAvailable, refresh])

  const armed = scope.armed && scope.armed.generation === scope.generation && scope.armed.isCurrent()
    && scope.armed.candidate.fingerprint === fingerprint
  const reason = state?.mode === 'refused' ? state.reason : isSizing && (busy || submitting) ? 'run_in_progress'
    : !available ? 'submit_unavailable'
    : !isScopeCurrent() ? 'scope_stale' : result && !result.ok ? result.reason
      : !armed ? (publication?.scope === scope && publication.generation === scope.generation
        ? publication.reason || 'preview_required' : 'preview_required') : null
  const runMessage = feedback?.scope === scope && feedback.generation === scope.generation ? feedback : null
  async function submit(event) {
    event?.preventDefault()
    if (reason || (isSizing && latest.current.busy) || !armed || scope.lock || !isScopeCurrent() || !scope.armed?.isCurrent()
        || scope.armed.generation !== scope.generation || scope.armed.candidate.fingerprint !== fingerprint) return
    const accepted = scope.armed
    scope.cancelJob?.()
    setTerminalJob(null)
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
        if (!outcome.ok) setFeedback({ scope, generation: scope.generation, ...projectSizingConductorFeedback(outcome, null, 'context') })
        setPublication({ scope, generation: scope.generation,
          reason: checked.reason === 'not_current_head' ? 'not_current_head' : 'scope_stale' })
        return
      }
      if (!pending.isCurrent() || (isSizing && latest.current.busy)
          || typeof latest.current.onProjectSubmit !== 'function') return
      const result = await latest.current.onProjectSubmit(accepted.candidate.intent,
        { binding, authorityKey, signal: pending.signal, isCurrent: pending.isCurrent })
      if (!pending.isCurrent()) return
      if (readProjectSolarWorkerRefusal(result)) {
        const refusal = projectSolarWorkerRefusal(toolName)
        scope.retry = null
        scope.candidate = null
        scope.armed = null
        setFeedback({ scope, generation: scope.generation, outcome: result,
          reason: refusal.reasonCode, text: refusal.message, code: refusal.reasonCode })
      } else if (result?.ok === true && result.status === 202 && result.data?.status === 'submitted'
          && typeof result.data.job_id === 'string' && projectJobId.test(result.data.job_id)) {
        scope.retry = null
        setFeedback({ scope, generation: scope.generation, outcome: result, text: 'Request submitted.', code: null })
        if (isSizing && typeof projectClient.observeJob === 'function') {
          const observer = operation(false)
          scope.cancelJob = observer.abort
          ;(async () => {
            try {
              const outcome = await projectClient.observeJob(result.data.job_id,
                { tool: toolName, signal: observer.signal })
              if (owner.current === scope && scope.alive && !observer.signal.aborted
                  && scope.declarationIdentity === declarationIdentity && outcome?.ok) {
                setTerminalJob({ scope, declarationIdentity, job: outcome.data?.job || outcome.data })
              }
            } catch { /* An unreadable job adds no suggestion. */ }
            finally {
              observer.finish()
              if (scope.cancelJob === observer.abort) scope.cancelJob = null
            }
          })()
        }
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
    terminalJob: terminalJob?.scope === owner.current && terminalJob.declarationIdentity === declarationIdentity
      ? terminalJob.job : null,
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
  const [mode, setMode] = useState('manual-global')
  const [storedDraft, setDraft] = useState({})
  let draftBindingKey = null
  try { draftBindingKey = solarDrawingBindingKey(createProjectSizingConductorBinding(props.solarBinding)) } catch { /* local refusal */ }
  const draftScopeKey = JSON.stringify([draftBindingKey, props.authorityKey, props.projectId])
  const draftScope = useRef({ key: draftScopeKey, client: props.projectClient })
  let draft = storedDraft
  if (draftScope.current.key !== draftScopeKey || draftScope.current.client !== props.projectClient) {
    draftScope.current = { key: draftScopeKey, client: props.projectClient }
    const { accepted_string_length, ...rest } = draft
    draft = rest
    setDraft(rest)
  }
  const declarationIdentity = JSON.stringify([mode, ...[
    'module_name', 'module_confirmed', 'full_inverter_name', 'bifacial', 'bifacial_coefficient',
    'racking_type', 'surface_tilt', 'surface_azimuth', 'albedo',
    'axis_tilt', 'axis_azimuth', 'max_angle', 'gcr', 'backtrack',
    'max_voltage', 'thermal_model_type', 'open_circuit_rise', 'grant_ref',
    'use_module_parameters', 'accepted_string_length', ...MODULE_PARAMETER_KEYS,
  ].map((key) => draft[key] ?? null)])
  const form = useProjectSizingConductor(props, { toolName: 'solar-size-strings',
    deriveState: deriveProjectSizingState, declarationIdentity,
    buildRequest: (args) => buildProjectSizingRequest({ ...args, mode, draft }) })
  const { state, editable, reason, submitting, runMessage, retryAvailable, submit } = form
  const built = editable ? buildSizingParams({ graph: state.graph, mode, draft }) : null
  const invalid = new Set(built?.invalid || [])
  const change = (key, value) => {
    setDraft(({ accepted_string_length, ...previous }) => ({ ...previous, [key]: value,
      ...(key === 'module_name' ? { module_confirmed: undefined } : {}) }))
  }
  const cold = form.terminalJob?.status === 'failed'
    && form.terminalJob.error?.reason_code === 'COLD_VOLTAGE_FAILED' ? form.terminalJob.error.cold_voltage : null
  const suggestion = mode === 'global' && cold && typeof cold === 'object' && !Array.isArray(cold)
    && ['standard_string_length', 'suggested_string_length', 'per_module', 'string_voltage', 'max_dc_voltage']
      .every((key) => Number.isFinite(cold[key]))
    && Number.isInteger(cold.standard_string_length) && Number.isInteger(cold.suggested_string_length)
    && cold.suggested_string_length >= 1 && cold.suggested_string_length < cold.standard_string_length ? cold : null
  const sentence = (reason === 'request_timeout' && runMessage?.phase === 'preview' ? previewTimeoutSentence : projectSizingConductorReason(reason))
    || projectSizingCloudReason(reason)
    || (reason === 'sizing_module_unconfirmed' ? SOLAR_SIZING_REASONS.sizing_module_unconfirmed : null)
    || (reason === 'sizing_zip_required' ? SOLAR_SIZING_REASONS.sizing_zip_required
      : reason === 'sizing_fields_invalid' ? SOLAR_SIZING_REASONS.sizing_fields_invalid : null)
    || (reason === 'sizing_panels_required' ? SOLAR_SIZING_REASONS.sizing_panels_required
      : reason === 'sizing_manual_length_required' ? SOLAR_SIZING_REASONS.sizing_manual_length_required : null)
  return <div data-testid="project-solar-sizing-form" data-mode={state?.mode ?? 'loading'}>
    {state === null && <p role="status">Reading the drawing.</p>}
    {editable && <>
      <label>Scope<select value={mode} disabled={submitting} onChange={(event) => {
        form.invalidateCandidate()
        setDraft(({ accepted_string_length, ...previous }) => previous)
        setMode(event.target.value)
      }}>
        <option value="manual-global">Saved global length</option>
        <option value="global">Global cloud sizing</option>
      </select></label>
      <p>Saved project ZIP: {state.savedZip}</p>
      {Number.isInteger(state.graph.savedLength) && state.graph.savedLength >= 1 && state.graph.savedLength <= 4096
        && <p>Saved global string length: {state.graph.savedLength}</p>}
      <p>Change the length in Solar settings.</p>
      {mode === 'manual-global' && <p>Manual confirmation does not calculate voltage limits or module power.</p>}
      {mode === 'global' && <SizingCloudFields mode={mode} draft={draft} change={change}
        projectClient={props.projectClient} searchScope={draftScopeKey}
        chooseModule={(key) => setDraft(({ accepted_string_length, ...previous }) => ({ ...previous,
          module_name: key, module_confirmed: key }))}
        busy={submitting} invalid={invalid} />}
      {mode === 'global' && draft.accepted_string_length !== undefined && <p>
        Accepted string length: {draft.accepted_string_length}{' '}
        <button type="button" disabled={submitting} onClick={() => {
          form.invalidateCandidate()
          setDraft(({ accepted_string_length, ...previous }) => previous)
        }}>Clear</button>
      </p>}
    </>}
    {sentence && <p role="status">{sentence}</p>}
    {runMessage && <p role="status" data-testid="project-solar-sizing-feedback">
      {humanizeError(runMessage.reason === 'request_timeout' && runMessage.phase === 'submission'
        ? 'The submission was not confirmed. Retry the same request to check its status.' : runMessage.text)}
      {runMessage.code === 'sip_r4_worker_unavailable' && ` (${runMessage.code})`}
    </p>}
    {suggestion && <div role="status">
      <p>At {suggestion.standard_string_length} modules the cold-weather open-circuit voltage is {suggestion.string_voltage.toFixed(1)} V, above the {suggestion.max_dc_voltage.toFixed(1)} V limit. {suggestion.suggested_string_length} modules stay within it.</p>
      <button type="button" disabled={submitting} onClick={() => change('accepted_string_length', suggestion.suggested_string_length)}>
        Use {suggestion.suggested_string_length} modules
      </button>
    </div>}
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

function ModuleCatalogField({ draft, change, chooseModule, projectClient, searchScope, busy, invalid }) {
  const listId = useId()
  const fieldRef = useRef(null)
  const [storedResult, setResult] = useState(null)
  const [active, setActive] = useState(0)
  const [open, setOpen] = useState(false)
  const query = (draft.module_name ?? '').trim()
  const grantValid = typeof draft.grant_ref === 'string' && /^[a-zA-Z0-9_-]{1,64}(?![\s\S])/.test(draft.grant_ref)
  const resultKey = JSON.stringify([searchScope, query, draft.module_name, draft.module_confirmed, draft.grant_ref, busy])
  const result = storedResult?.key === resultKey && storedResult.client === projectClient ? storedResult.data : null
  useEffect(() => {
    const controller = new AbortController()
    setResult(null)
    setOpen(false)
    setActive(0)
    if (busy || !grantValid || query.length < 2 || draft.module_confirmed === draft.module_name) return undefined
    const timer = setTimeout(async () => {
      try {
        const outcome = await projectClient?.searchSizingModules({ q: query, limit: 20,
          grantRef: draft.grant_ref, signal: controller.signal })
        if (controller.signal.aborted) return
        setResult({ key: resultKey, client: projectClient,
          data: outcome?.ok ? { ...outcome.data, modules: outcome.data.modules.slice(0, 20) } : { failed: true } })
        setOpen(true)
      } catch {
        if (!controller.signal.aborted) {
          setResult({ key: resultKey, client: projectClient, data: { failed: true } }); setOpen(true)
        }
      }
    }, 300)
    return () => { clearTimeout(timer); controller.abort() }
  }, [query, draft.module_name, draft.module_confirmed, draft.grant_ref, grantValid, busy, projectClient, searchScope])
  const modules = result?.modules ?? []
  const expanded = open && modules.length > 0
  useEscapeOwner('solar-module-options', expanded, () => setOpen(false), { layer: 'menu', scope: fieldRef, scoped: true })
  function choose(key) { chooseModule(key); setOpen(false); setResult(null) }
  return <div ref={fieldRef}>
    <label>Module<input role="combobox" value={draft.module_name ?? ''} disabled={busy}
      autoComplete="off" aria-autocomplete="list" aria-expanded={expanded} aria-controls={expanded ? listId : undefined}
      aria-activedescendant={expanded ? `${listId}-${active}` : undefined}
      aria-invalid={invalid.has('module_name') || undefined}
      onChange={(event) => { setResult(null); setOpen(false); change('module_name', event.target.value) }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return
        if (modules.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
          event.preventDefault()
          setOpen(true)
          setActive((index) => (index + (event.key === 'ArrowDown' ? 1 : modules.length - 1)) % modules.length)
        } else if (expanded && event.key === 'Enter') { event.preventDefault(); choose(modules[active].key) }
      }} /></label>
    {expanded && <ul id={listId} role="listbox" aria-label="Sizer modules">
      {modules.map((module, index) => {
        const parts = [module.stc_w === null ? null : `${module.stc_w} W`, module.n_s === null ? null : `${module.n_s} cells`].filter(Boolean)
        return <li key={module.key} id={`${listId}-${index}`} role="option" aria-selected={index === active}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(module.key)}>
          {module.key}{parts.length ? ` (${parts.join(', ')})` : ''}
        </li>
      })}
    </ul>}
    {!grantValid && <p role="status">Enter the grant reference to search the module catalog.</p>}
    {result?.failed && <p role="status">The module catalog could not be searched.</p>}
    {result?.modules?.length === 0 && <p role="status">No modules match.</p>}
    {result?.truncated && <p role="status">More modules match; keep typing.</p>}
  </div>
}

function SizingCloudFields({ mode, draft, change, chooseModule, projectClient, searchScope, busy, invalid, moduleCatalog = true }) {
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
  return <>
    {mode === 'global' && <>{moduleCatalog
      ? <ModuleCatalogField {...{ draft, change, chooseModule, projectClient, searchScope, busy, invalid }} />
      : input('module_name', 'Module')}
      {input('full_inverter_name', 'Inverter')}</>}
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
    {draft.use_module_parameters && MODULE_PARAMETER_KEYS.map((key) => input(key, MODULE_PARAMETER_LABELS[key]))}
  </>
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
      .catch(() => { if (current) setLoaded({ drawingId, drawingVersion,
        error: humanizeError('Could not load the drawing. Try loading it again.') }) })
    return () => { current = false }
  }, [drawingId, drawingVersion, scoped, refresh])

  useEffect(() => {
    if (status === 'finished' && previousStatus.current !== 'finished') setRefresh((value) => value + 1)
    previousStatus.current = status
  }, [status])

  useEffect(() => { lock.current = false })

  const graph = loaded?.drawingId === drawingId && loaded?.drawingVersion === drawingVersion ? loaded.graph : null
  const currentLoad = loaded?.drawingId === drawingId && loaded?.drawingVersion === drawingVersion ? loaded : null
  const loading = scoped && currentLoad === null
  const built = !scoped ? { ok: false, reason: 'sizing_project_scope' }
    : buildSizingParams({ graph, mode, draft, moduleCatalog: false })
  const targets = scoped && graph?.ok ? sizingTargets(graph, mode) : null
  const busy = status === 'pending'
  const invalid = new Set(built.invalid || [])
  const change = (key, value) => setDraft((previous) => ({ ...previous, [key]: value,
    ...(key === 'module_name' ? { module_confirmed: undefined } : {}) }))
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
    <SizingCloudFields mode={mode} draft={draft} change={change} busy={busy} invalid={invalid} moduleCatalog={false} />
    </>}
    {loading && <p role="status">Loading the drawing.</p>}
    {currentLoad?.error && <p role="alert">{currentLoad.error}</p>}
    {!loading && !currentLoad?.error && !built.ok && <p role="status">{SOLAR_SIZING_REASONS[built.reason]}</p>}
    {currentLoad?.error && <button type="button" disabled={busy}
      onClick={() => setRefresh((value) => value + 1)}>Retry loading</button>}
    {busy && <p role="status">This step is running. Confirm or wait for it to finish.</p>}
    {status === 'failed' && <p role="alert">{solarSizingRunSentence(failureCode)}</p>}
    {status === 'finished' && <p role="status">String sizing applied.</p>}
    <button type="button" disabled={busy || !built.ok} onClick={submit}>Review &amp; run</button>
    {status === 'failed' && <button type="button" disabled={busy || !built.ok} onClick={submit}>Retry</button>}
    {onClose && <button type="button" onClick={onClose}>Cancel</button>}
  </div>
}
