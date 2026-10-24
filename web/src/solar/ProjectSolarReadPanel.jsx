import { useLayoutEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import SolarReadResult from './SolarReadResult.jsx'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

export const PROJECT_SOLAR_WORKSPACE_REASONS = Object.freeze({
  binding_invalid: 'Open a project drawing version to read Solar output.',
  scope_stale: 'The drawing or session changed. Read the output again.',
  client_unavailable: 'Project Solar reads are not available in this view.',
  catalog_unavailable: 'Refresh the Solar tools before reading this output.',
  catalog_refreshing: 'Refreshing the Solar tools. Your inputs are kept.',
  tool_not_admitted: 'This Solar tool is no longer available for this project. Close it to continue.',
  params_schema_changed: 'This Solar tool changed. Close and reopen it to use the new inputs.',
  unsupported: 'This tool is not available in the project Solar workspace.',
  host_unavailable: 'This editor is not available in the project Solar workspace.',
  busy: 'Wait for the current project action to finish.',
  parameters_invalid: 'Check the read parameters before trying again.',
  idle: 'Read Solar output for the selected drawing version.',
  reading: 'Reading Solar output for the selected drawing version.',
  read_refused: 'This Solar read could not finish. Your inputs are kept.',
  run_submitted: 'Solar run submitted.',
  run_published: 'Solar run published a drawing version.',
  proposal_ready: 'Proposal ready for review.',
  run_outcome_unknown: 'The run outcome is unknown. Check its job before submitting again.',
  run_refused: 'The server refused this Solar request.',
  proposal_unreadable: 'This Solar proposal could not be read.',
  proposal_required: 'Run a solve and review its proposal before committing.',
  proposal_unpublished: 'This proposal has not published a drawing version.',
  proposal_more_strings: 'More proposed strings not shown: {count}.',
})

export const PROJECT_SOLAR_WORKSPACE_LABELS = Object.freeze({
  read_action: 'Read project output',
  solve_action: 'Solve',
  correct_strings_action: 'Correct strings',
  run_solve_action: 'Run solve',
  close_action: 'Close',
  proposal_review_label: 'Solve proposal review',
  proposal_review_title: 'Review solve proposal',
  frame_label: 'Frame',
  phase_label: 'Phase',
  string_count_label: 'String count',
  proposed_strings_label: 'Proposed strings',
  string_label: 'String',
  panels_label: 'Panels',
  start_label: 'Start',
  end_label: 'End',
  commit_proposal_action: 'Commit proposal',
  discard_action: 'Discard',
  solve_again_action: 'Run solve again',
})

export function projectSolarWorkspaceReason(code) {
  switch (code) {
    case 'binding_invalid': return PROJECT_SOLAR_WORKSPACE_REASONS.binding_invalid
    case 'scope_stale': return PROJECT_SOLAR_WORKSPACE_REASONS.scope_stale
    case 'client_unavailable': return PROJECT_SOLAR_WORKSPACE_REASONS.client_unavailable
    case 'catalog_unavailable': return PROJECT_SOLAR_WORKSPACE_REASONS.catalog_unavailable
    case 'catalog_refreshing': return PROJECT_SOLAR_WORKSPACE_REASONS.catalog_refreshing
    case 'tool_not_admitted': return PROJECT_SOLAR_WORKSPACE_REASONS.tool_not_admitted
    case 'params_schema_changed': return PROJECT_SOLAR_WORKSPACE_REASONS.params_schema_changed
    case 'unsupported': return PROJECT_SOLAR_WORKSPACE_REASONS.unsupported
    case 'host_unavailable': return PROJECT_SOLAR_WORKSPACE_REASONS.host_unavailable
    case 'busy': return PROJECT_SOLAR_WORKSPACE_REASONS.busy
    case 'parameters_invalid': return PROJECT_SOLAR_WORKSPACE_REASONS.parameters_invalid
    case 'idle': return PROJECT_SOLAR_WORKSPACE_REASONS.idle
    case 'reading': return PROJECT_SOLAR_WORKSPACE_REASONS.reading
    case 'read_refused': return PROJECT_SOLAR_WORKSPACE_REASONS.read_refused
    case 'run_submitted': return PROJECT_SOLAR_WORKSPACE_REASONS.run_submitted
    case 'run_published': return PROJECT_SOLAR_WORKSPACE_REASONS.run_published
    case 'proposal_ready': return PROJECT_SOLAR_WORKSPACE_REASONS.proposal_ready
    case 'run_outcome_unknown': return PROJECT_SOLAR_WORKSPACE_REASONS.run_outcome_unknown
    case 'run_refused': return PROJECT_SOLAR_WORKSPACE_REASONS.run_refused
    case 'proposal_unreadable': return PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unreadable
    case 'proposal_required': return PROJECT_SOLAR_WORKSPACE_REASONS.proposal_required
    case 'proposal_unpublished': return PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unpublished
    case 'proposal_more_strings': return PROJECT_SOLAR_WORKSPACE_REASONS.proposal_more_strings
    default: return null
  }
}

const READ_TOOLS = ['solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']
const DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const REASON_CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const plain = (value) => value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
// SchemaForm renders a property's title and description as text, so either one must be a string when present.
const textField = (value) => value === undefined || typeof value === 'string'
const current = (predicate) => {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}

// Copy the declared form and admitted parameters, so edits cannot mutate a pending request.
function copyJson(value, depth = 0) {
  if (depth > 64) throw new TypeError('Invalid read parameters')
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (Array.isArray(value)) return Object.freeze(value.map((item) => copyJson(item, depth + 1)))
  if (!plain(value)) throw new TypeError('Invalid read parameters')
  const result = {}
  for (const key of Object.keys(value).sort()) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (!descriptor || !Object.hasOwn(descriptor, 'value')) throw new TypeError('Invalid read parameters')
    Object.defineProperty(result, key, { value: copyJson(descriptor.value, depth + 1), enumerable: true })
  }
  return Object.freeze(result)
}

function declaredSchema(row) {
  if (!plain(row)) throw new TypeError('Invalid read tool')
  const wire = row.params_schema
  const normalized = row.params
  const schema = copyJson(wire === undefined ? normalized : wire)
  if (!plain(schema) || schema.type !== 'object' || !plain(schema.properties) ||
      Object.values(schema.properties).some((property) => !plain(property) ||
        !textField(property.title) || !textField(property.description)) ||
      (schema.required !== undefined && (!Array.isArray(schema.required) || schema.required.some((key) => typeof key !== 'string')))) {
    throw new TypeError('Invalid read schema')
  }
  if (wire !== undefined && normalized !== undefined && JSON.stringify(schema) !== JSON.stringify(copyJson(normalized))) {
    throw new TypeError('Conflicting read schemas')
  }
  return schema
}

function describeOwner(props) {
  const { solarBinding, authorityKey, projectClient, projectCatalog, tool, isCurrent } = props
  let binding = null, bindingKey = null, schema = null, schemaKey = null, digest = null, name = null, receiptCurrent
  try { binding = createSolarDrawingBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding) } catch { /* refused below */ }
  let reason = binding ? null : 'binding_invalid'
  if (!reason && !current(isCurrent)) reason = 'scope_stale'
  let available = false
  try {
    available = typeof authorityKey === 'string' && !!authorityKey.trim() && new TextEncoder().encode(authorityKey).length <= 256 &&
      !!projectClient && ['readSolar', 'getSnapshot', 'subscribe', 'captureCatalogScope', 'downloadArtifact']
        .every((key) => typeof projectClient[key] === 'function')
  } catch { /* unavailable */ }
  if (!reason && !available) reason = 'client_unavailable'
  if (!reason) {
    try {
      const snapshot = projectClient.getSnapshot()
      if (!sameSolarDrawingBinding(snapshot.binding, binding) || snapshot.authorityKey !== authorityKey) reason = 'scope_stale'
    } catch { reason = 'scope_stale' }
  }
  try {
    name = tool?.name
    digest = tool?.catalog_digest
    schema = declaredSchema(tool)
    schemaKey = JSON.stringify(schema)
    // Read the receipt's predicate once, inside the guard: a throwing accessor is a stale catalog, never a crash.
    receiptCurrent = projectCatalog?.isCurrent
    if (!projectCatalog || !sameSolarDrawingBinding(projectCatalog.binding, binding) ||
        projectCatalog.authorityKey !== authorityKey || projectCatalog.client !== projectClient ||
        !current(receiptCurrent) || !Array.isArray(projectCatalog.families)) throw new TypeError('Stale read catalog')
    const rows = projectCatalog.families.flatMap((family) => {
      if (!Array.isArray(family?.capabilities)) throw new TypeError('Invalid read catalog')
      return family.capabilities
    })
    if (!rows.includes(tool) || typeof digest !== 'string' || !DIGEST.test(digest) ||
        rows.some((row) => row?.name === name && (row.catalog_digest !== digest ||
          JSON.stringify(declaredSchema(row)) !== schemaKey))) throw new TypeError('Detached read tool')
  } catch { if (!reason) reason = 'catalog_unavailable' }
  if (!reason && !READ_TOOLS.includes(name)) reason = 'unsupported'
  const properties = { ...schema?.properties }
  delete properties.drawing_id
  const editableSchema = { ...schema, properties, required: schema?.required?.filter((key) => key !== 'drawing_id') }
  return { binding, solarBinding: binding, bindingKey, authorityKey, projectClient, projectCatalog, tool, name, digest,
    schemaKey, editableSchema, isCurrent, receiptCurrent, reason }
}

function sameOwner(left, right) {
  return !!left && ['bindingKey', 'authorityKey', 'projectClient', 'projectCatalog', 'tool', 'name', 'digest',
    'schemaKey', 'isCurrent', 'receiptCurrent', 'reason'].every((key) => left[key] === right[key])
}

export default function ProjectSolarReadPanel({ tool, solarBinding, authorityKey, projectClient,
  projectCatalog, isCurrent, busy = false, save }) {
  const owner = describeOwner({ tool, solarBinding, authorityKey, projectClient, projectCatalog, isCurrent })
  const latest = useRef(null)
  const generation = useRef(0)
  const operation = useRef(null)
  const presentedScope = useRef(null)
  const mounted = useRef(false)
  const formEpoch = useRef(0)
  const parameterRevision = useRef(0)
  // SchemaForm's latest validity report, written in the handler itself: the report can arrive (from its effect) after
  // the render a submit closes over, and a stale valid form must never send the previous values.
  const reportedValid = useRef(true)
  const [state, setState] = useState({ phase: 'idle', generation: 0 })
  const [, refresh] = useState(0)
  const invalidate = () => {
    generation.current += 1
    operation.current?.controller.abort()
    operation.current = null
    presentedScope.current = null
  }
  const previous = latest.current
  if (!sameOwner(previous, owner)) {
    invalidate()
    if (!previous || previous.bindingKey !== owner.bindingKey || previous.authorityKey !== authorityKey ||
        previous.tool !== tool || previous.name !== owner.name || previous.schemaKey !== owner.schemaKey) formEpoch.current += 1
  }
  if (presentedScope.current && !current(presentedScope.current)) {
    invalidate()
    setState({ phase: 'stale', generation: generation.current })
  }
  latest.current = owner
  const [form, setForm] = useState(() => ({ epoch: formEpoch.current, values: defaultsOf(owner.editableSchema), valid: true }))
  const activeForm = form.epoch === formEpoch.current ? form :
    { epoch: formEpoch.current, values: defaultsOf(owner.editableSchema), valid: true }
  if (form !== activeForm) setForm(activeForm)
  const renderedParameters = parameterRevision.current
  const visible = state.generation === generation.current ? state : { phase: 'idle' }
  const observe = useRef(null)
  observe.current = () => {
    const next = describeOwner(latest.current)
    if (!sameOwner(latest.current, next) || (presentedScope.current && !current(presentedScope.current))) {
      invalidate()
      setState({ phase: 'stale', generation: generation.current })
    } else if (operation.current) operation.current.check()
    refresh((value) => value + 1)
  }
  useLayoutEffect(() => {
    mounted.current = true
    let unsubscribe
    try { unsubscribe = projectClient?.subscribe(() => observe.current()) } catch { /* refused admission */ }
    return () => {
      mounted.current = false
      invalidate()
      try { unsubscribe?.() } catch { /* observer isolation */ }
    }
  }, [projectClient])

  const edit = () => {
    parameterRevision.current += 1
    invalidate()
    setState({ phase: 'idle', generation: generation.current })
  }
  const read = async (event) => {
    event.preventDefault()
    const admitted = describeOwner(latest.current)
    if (admitted.reason || !sameOwner(latest.current, admitted)) {
      invalidate()
      if (mounted.current) setState({ phase: 'stale', generation: generation.current })
      return
    }
    if (operation.current) { operation.current.check(); return }
    if (busy || !activeForm.valid || !reportedValid.current || parameterRevision.current !== renderedParameters ||
        !mounted.current) return
    let params
    try { params = copyJson({ ...activeForm.values, drawing_id: admitted.binding.drawing_id }) }
    catch { setForm((value) => ({ ...value, valid: false })); return }
    const controller = new AbortController()
    const token = generation.current
    const active = { controller, token, check: null }
    operation.current = active
    let capturedScope
    const scopeCurrent = () => {
      try {
        const snapshot = admitted.projectClient.getSnapshot()
        return sameOwner(latest.current, admitted) && current(admitted.isCurrent) && current(admitted.receiptCurrent) &&
          current(capturedScope) && sameSolarDrawingBinding(snapshot.binding, admitted.binding) && snapshot.authorityKey === admitted.authorityKey
      } catch { return false }
    }
    const check = () => {
      const valid = mounted.current && generation.current === token && operation.current === active &&
        !controller.signal.aborted && scopeCurrent()
      if (!valid && operation.current === active) {
        invalidate()
        if (mounted.current) setState({ phase: 'stale', generation: generation.current })
      }
      return valid
    }
    active.check = check
    try {
      capturedScope = admitted.projectClient.captureCatalogScope()
      if (!check()) return
      setState({ phase: 'reading', generation: token })
      const outcome = await admitted.projectClient.readSolar({ tool: admitted.name, params,
        catalogDigest: admitted.digest, current: true, signal: controller.signal })
      if (!check()) return
      const ok = outcome?.ok === true
      const data = ok ? outcome.data?.result : null
      const reason = ok ? null : outcome?.reason
      const code = typeof reason === 'string' && REASON_CODE.test(reason) ? reason : 'HTTP_ERROR'
      if (!check()) return
      presentedScope.current = scopeCurrent
      if (ok) {
        setState({ phase: 'presented', generation: token, data })
      } else setState({ phase: 'refused', generation: token, code })
    } catch {
      if (check()) {
        presentedScope.current = scopeCurrent
        setState({ phase: 'refused', generation: token, code: 'SIP_R7_TRANSPORT_FAILED' })
      }
    } finally {
      if (operation.current === active) operation.current = null
    }
  }
  const reason = owner.reason || (visible.phase === 'reading' ? 'reading' : busy ? 'busy' :
    !activeForm.valid ? 'parameters_invalid' : visible.phase === 'refused' ? 'read_refused' :
      visible.phase === 'stale' ? 'scope_stale' : visible.phase === 'presented' ? null : 'idle')
  return <section className="project-solar-read-panel">
    <form onSubmit={read} onInputCapture={edit} onChangeCapture={edit}>
      <SchemaForm key={formEpoch.current} schema={owner.editableSchema} values={activeForm.values}
        onChange={(values) => { edit(); setForm((value) => ({ ...value, values })) }}
        onValidityChange={(valid) => {
          reportedValid.current = valid
          setForm((value) => value.valid === valid ? value : { ...value, valid })
        }} />
      <p role="status" aria-live="polite">{projectSolarWorkspaceReason(reason)}
        {reason === 'read_refused' && <> <code>{visible.code}</code></>}
      </p>
      <button type="submit" disabled={!!owner.reason || visible.phase === 'reading' || busy || !activeForm.valid}>
        {PROJECT_SOLAR_WORKSPACE_LABELS.read_action}
      </button>
    </form>
    {!owner.reason && visible.phase === 'presented' && <SolarReadResult data={visible.data}
      solarBinding={solarBinding} authorityKey={authorityKey} projectClient={projectClient}
      catalogDigest={owner.digest} projectCurrent={true} save={save} />}
  </section>
}
