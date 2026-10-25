import { createSolarDrawingBinding, sameSolarDrawingBinding } from './solarDrawingBinding.js'
import { deriveProjectSettingsState } from './solarSettingsModel.js'

export const PROJECT_SOLAR_GRAPH_TOOLS = Object.freeze([
  'solar-panels-from-drawing', 'solar-panel-groups', 'solar-homeruns', 'solar-feeders', 'solar-schedule',
])

export const PROJECT_SOLAR_GRAPH_REASONS = Object.freeze({
  binding_invalid: 'Select a valid project drawing version to use Solar.',
  context_unreadable: 'This project drawing version could not be read. Reload it and try again.',
  graph_unreadable: 'The Solar design stored with this drawing could not be read.',
  graphless: 'Start a Solar design in settings before using this tool.',
  not_current_head: 'Open the newest drawing version before using this tool.',
  version_digest_unavailable: 'This drawing version has no recorded content digest. Reload it before continuing.',
  scope_stale: 'The drawing or session changed. Refresh before continuing.',
  stale_graph_revision: 'The Solar graph revision changed. Refresh before continuing.',
  catalog_digest_required: 'Refresh the Solar tools before preparing this request.',
  idempotency_key_required: 'This Solar request could not be identified. Review it again.',
  request_invalid: 'This Solar request could not be prepared. Review the inputs.',
  insertion_invalid: 'Enter two or three comma-separated coordinates, each between -1000000000000 and 1000000000000.',
  no_panel_groups: 'No panel angles have at least two ungrouped panels.',
  too_many_groups: 'There are more than 128 proposed groups. No groups have been omitted.',
  group_too_large: 'A proposed group has more than 4096 panels. No panels have been omitted.',
  panel_groups_stale: 'The proposed panel groups changed. Refresh the preview before continuing.',
  group_name_invalid: 'Enter a group name of 1 to 255 characters without [.',
  group_name_collision: 'Group names must be unique, including existing panel groups.',
  group_numbers_invalid: 'Enter all three dimensions and tolerances as finite numbers greater than 0 and at most 1000000.',
  tool_unsupported: 'This tool has no project form in this view.',
  submit_unavailable: 'Project Solar submission is unavailable in this view.',
  run_in_progress: 'A Solar run is in progress. Wait for it to finish.',
  preview_required: 'Check this request before submitting it.',
  preview_refused: 'This Solar request could not be checked. Refresh before continuing.',
  request_refused: 'The server refused this Solar request. Review the reported code.',
  equipment_assignment_required: 'This tool requires valid equipment assignments. Review the electrical reasons on the inverter.',
  homeruns_equipment_required: 'Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.',
  equipment_voltage_unknown: 'The inverter has no verified string voltage.',
  equipment_power_unknown: 'The inverter has no verified module power.',
  valid_settings_required: 'Feeders require valid L2 collector settings.',
  string_collectors_required: 'Feeders require string collectors.',
  routing_topology_required: 'This tool requires valid routing topology.',
  schedule_topology_required: 'The schedule requires routing topology.',
  complete_routing_required: 'The schedule requires complete current homeruns.',
  solar_output_not_current: 'Electrical output is unavailable because this Solar design is not current.',
  project_unavailable: 'Project Solar runs are unavailable in this view.',
  cancelled: 'The Solar request was cancelled without publishing a drawing version.',
  context_loading: 'Reading the selected project drawing version.',
  submitted: 'The Solar request was submitted.',
  pending: 'The Solar run is still in progress.',
  succeeded: 'The Solar run published a drawing version.',
  failed: 'The Solar run did not publish a drawing version.',
  unreadable: 'This Solar run finished, but its output could not be read.',
  outcome_unknown: 'The Solar run outcome is unknown. Check its job before submitting again.',
  aborted: 'Observation stopped. The server job may still be running.',
})

const CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const HEX = new RegExp('^[0-9a-f]{64}$')
const UUID = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
const DECIMAL = new RegExp('^[+-]?(?:[0-9]+(?:\\.[0-9]+)?|\\.[0-9]+)$')
const CAPABILITY = new RegExp('lco1\\.[0-9a-f]{64}')
const boundedCode = (value) => typeof value === 'string' && CODE.test(value) ? value : null
const validUuid = (value) => typeof value === 'string' && UUID.test(value)

export function projectSolarGraphReason(code, tool) {
  if (code == null) return null
  if (!boundedCode(code)) return PROJECT_SOLAR_GRAPH_REASONS.request_refused
  switch (code.toLowerCase()) {
    case 'no_panel_groups': return PROJECT_SOLAR_GRAPH_REASONS.no_panel_groups
    case 'too_many_groups': return PROJECT_SOLAR_GRAPH_REASONS.too_many_groups
    case 'group_too_large': return PROJECT_SOLAR_GRAPH_REASONS.group_too_large
    case 'panel_groups_stale': return PROJECT_SOLAR_GRAPH_REASONS.panel_groups_stale
    case 'group_name_invalid': return PROJECT_SOLAR_GRAPH_REASONS.group_name_invalid
    case 'group_name_collision': return PROJECT_SOLAR_GRAPH_REASONS.group_name_collision
    case 'group_numbers_invalid': return PROJECT_SOLAR_GRAPH_REASONS.group_numbers_invalid
    case 'sip_r7_binding_invalid':
    case 'binding_invalid': return PROJECT_SOLAR_GRAPH_REASONS.binding_invalid
    case 'context_unreadable': return PROJECT_SOLAR_GRAPH_REASONS.context_unreadable
    case 'graph_unreadable': return PROJECT_SOLAR_GRAPH_REASONS.graph_unreadable
    case 'graphless': return PROJECT_SOLAR_GRAPH_REASONS.graphless
    case 'not_current_head': return PROJECT_SOLAR_GRAPH_REASONS.not_current_head
    case 'version_digest_unavailable': return PROJECT_SOLAR_GRAPH_REASONS.version_digest_unavailable
    case 'sip_r7_scope_stale':
    case 'scope_stale': return PROJECT_SOLAR_GRAPH_REASONS.scope_stale
    case 'stale_graph_revision': return PROJECT_SOLAR_GRAPH_REASONS.stale_graph_revision
    case 'catalog_digest_required': return PROJECT_SOLAR_GRAPH_REASONS.catalog_digest_required
    case 'idempotency_key_required': return PROJECT_SOLAR_GRAPH_REASONS.idempotency_key_required
    case 'sip_r7_request_invalid':
    case 'request_invalid': return PROJECT_SOLAR_GRAPH_REASONS.request_invalid
    case 'insertion_invalid': return PROJECT_SOLAR_GRAPH_REASONS.insertion_invalid
    case 'tool_unsupported': return PROJECT_SOLAR_GRAPH_REASONS.tool_unsupported
    case 'submit_unavailable': return PROJECT_SOLAR_GRAPH_REASONS.submit_unavailable
    case 'sip_r13b_run_in_progress':
    case 'run_in_progress': return PROJECT_SOLAR_GRAPH_REASONS.run_in_progress
    case 'preview_required': return PROJECT_SOLAR_GRAPH_REASONS.preview_required
    case 'preview_refused': return PROJECT_SOLAR_GRAPH_REASONS.preview_refused
    case 'request_refused': return PROJECT_SOLAR_GRAPH_REASONS.request_refused
    case 'equipment_assignment_required':
      if (tool === 'solar-homeruns') return PROJECT_SOLAR_GRAPH_REASONS.homeruns_equipment_required
      return PROJECT_SOLAR_GRAPH_REASONS.equipment_assignment_required
    case 'homeruns_equipment_required': return PROJECT_SOLAR_GRAPH_REASONS.homeruns_equipment_required
    case 'equipment_voltage_unknown': return PROJECT_SOLAR_GRAPH_REASONS.equipment_voltage_unknown
    case 'equipment_power_unknown': return PROJECT_SOLAR_GRAPH_REASONS.equipment_power_unknown
    case 'valid_settings_required': return PROJECT_SOLAR_GRAPH_REASONS.valid_settings_required
    case 'string_collectors_required': return PROJECT_SOLAR_GRAPH_REASONS.string_collectors_required
    case 'routing_topology_required':
      if (tool === 'solar-schedule') return PROJECT_SOLAR_GRAPH_REASONS.schedule_topology_required
      return PROJECT_SOLAR_GRAPH_REASONS.routing_topology_required
    case 'schedule_topology_required': return PROJECT_SOLAR_GRAPH_REASONS.schedule_topology_required
    case 'complete_routing_required': return PROJECT_SOLAR_GRAPH_REASONS.complete_routing_required
    case 'solar_output_not_current': return PROJECT_SOLAR_GRAPH_REASONS.solar_output_not_current
    case 'project_execution_disabled':
    case 'project_adapter_unavailable':
    case 'project_unavailable': return PROJECT_SOLAR_GRAPH_REASONS.project_unavailable
    case 'graph_commit_cancelled':
    case 'cancelled': return PROJECT_SOLAR_GRAPH_REASONS.cancelled
    case 'context_loading': return PROJECT_SOLAR_GRAPH_REASONS.context_loading
    case 'submitted': return PROJECT_SOLAR_GRAPH_REASONS.submitted
    case 'pending': return PROJECT_SOLAR_GRAPH_REASONS.pending
    case 'succeeded': return PROJECT_SOLAR_GRAPH_REASONS.succeeded
    case 'failed': return PROJECT_SOLAR_GRAPH_REASONS.failed
    case 'unreadable': return PROJECT_SOLAR_GRAPH_REASONS.unreadable
    case 'sip_r13b_outcome_unknown':
    case 'outcome_unknown': return PROJECT_SOLAR_GRAPH_REASONS.outcome_unknown
    case 'sip_r7_aborted':
    case 'aborted': return PROJECT_SOLAR_GRAPH_REASONS.aborted
    default: return PROJECT_SOLAR_GRAPH_REASONS.request_refused
  }
}

export function parseProjectSolarInsertionPoint(text) {
  const fail = () => ({ ok: false, reason: 'insertion_invalid' })
  if (typeof text !== 'string' || text.length > 160) return fail()
  const tokens = text.split(',').map((token) => token.trim())
  if (tokens.length !== 2 && tokens.length !== 3) return fail()
  const point = []
  for (const token of tokens) {
    if (token.length > 48 || !DECIMAL.test(token)) return fail()
    const value = Number(token)
    if (!Number.isFinite(value) || Math.abs(value) > 1e12) return fail()
    point.push(value === 0 ? 0 : value)
  }
  if (point.length === 2) point.push(0)
  return { ok: true, point }
}

export function deriveProjectSolarGraphFormState({ binding, contextView }) {
  let state
  try { state = deriveProjectSettingsState({ binding, contextView }) }
  catch { return { mode: 'refused', reason: 'context_unreadable' } }
  if (state.mode === 'refused') return state
  if (state.mode === 'initialize') return { mode: 'refused', reason: 'graphless' }
  if (!Number.isSafeInteger(state.rev) || state.rev < 0) return { mode: 'refused', reason: 'graph_unreadable' }
  return { mode: 'edit', binding: state.binding, intakeSha256: state.intakeSha256, rev: state.rev }
}

export function panelGroupAngleKey(angleDegrees) {
  if (!Number.isFinite(angleDegrees)) return null
  // Match angle_key(math.radians(panel.angle)), including r_to_d's strict snaps.
  let degrees = (angleDegrees * (Math.PI / 180)) * (180 / Math.PI)
  for (const snap of [360, 270, 180, 90, 0]) {
    if (degrees < snap + 0.001 && degrees > snap - 0.001) { degrees = snap; break }
  }
  degrees %= 360
  if (degrees < 0) degrees += 360
  if (degrees >= 180) degrees -= 180
  // toFixed rounds the exact positive binary value, as Decimal(float) HALF_UP does.
  return degrees.toFixed(1)
}

export function proposePanelGroups(graph) {
  const buckets = new Map()
  let ungrouped = 0
  for (const panel of graph?.panels ?? []) {
    if (panel?.frame_ref !== null || typeof panel.id !== 'string'
        || typeof panel.provenance?.source_handle !== 'string') continue
    const key = panelGroupAngleKey(panel.angle)
    if (key === null) continue
    ungrouped += 1
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key).push(panel.id)
  }
  let singletons = 0
  const proposals = []
  for (const [key, refs] of [...buckets].sort(([a], [b]) => Number(a) - Number(b))) {
    if (refs.length === 1) { singletons += 1; continue }
    proposals.push({ key, angle_key: key, panel_refs: refs.slice().sort(), count: refs.length })
  }
  const refused = proposals.length > 128 ? 'too_many_groups'
    : proposals.some((proposal) => proposal.count > 4096) ? 'group_too_large' : null
  return { proposals, singletons, ungrouped, refused }
}

export function validatePanelGroups(graph, groups, numbers) {
  const fail = (reason) => ({ ok: false, reason })
  const { proposals, refused } = proposePanelGroups(graph)
  if (refused) return fail(refused)
  if (!proposals.length) return fail('no_panel_groups')
  if (!Array.isArray(groups) || groups.length !== proposals.length
      || proposals.some((proposal, index) => groups[index]?.key !== proposal.key)) return fail('panel_groups_stale')
  const names = new Set((graph?.frames ?? []).map((frame) => foldGroupName(frame.name ?? '')))
  const named = []
  for (const group of groups) {
    const name = typeof group.name === 'string' ? group.name.trim() : ''
    if (!name || [...name].length > 255 || name.includes('[')) return fail('group_name_invalid')
    const folded = foldGroupName(name)
    if (names.has(folded)) return fail('group_name_collision')
    names.add(folded)
    named.push(name)
  }
  const parsed = {}
  for (const key of ['module_width_along_row', 'module_height_across_row', 'alignment_tolerance']) {
    const text = numbers?.[key]
    const value = typeof text === 'string' && text.trim() ? Number(text) : NaN
    if (!Number.isFinite(value) || value <= 0 || value > 1e6) return fail('group_numbers_invalid')
    parsed[key] = value
  }
  return { ok: true, value: proposals.map((proposal, index) => ({ name: named[index],
    panel_refs: proposal.panel_refs, ...parsed })) }
}

function foldGroupName(name) {
  // Expand Unicode case variants (including sharp s, ligatures and final sigma).
  // Dotless i has no casefold mapping and must remain distinct from ASCII i.
  return name.replace(/[^\u0131]+/gu, (text) => text.toUpperCase().toLowerCase()).replace(/ß/g, 'ss')
}

export function buildProjectSolarGraphRequest({ tool, binding, state, currentState, insertionPointText, groups,
  catalogDigest, idempotencyKey }) {
  const fail = (reason) => ({ ok: false, reason })
  try {
    state = state == null ? state : { mode: state.mode, reason: state.reason, binding: state.binding,
      rev: state.rev, intakeSha256: state.intakeSha256 }
    currentState = currentState == null ? currentState : { mode: currentState.mode, reason: currentState.reason,
      binding: currentState.binding, rev: currentState.rev, intakeSha256: currentState.intakeSha256 }
  } catch { return fail('request_invalid') }
  let exact
  try { exact = createSolarDrawingBinding(binding) } catch { return fail('binding_invalid') }
  if (!PROJECT_SOLAR_GRAPH_TOOLS.includes(tool)) return fail('tool_unsupported')
  for (const value of [state, currentState]) {
    if (value?.mode === 'refused') return fail(value.reason)
    if (!sameSolarDrawingBinding(exact, value?.binding)) return fail('scope_stale')
    if (value.mode !== 'edit' || !Number.isSafeInteger(value.rev) || value.rev < 0
        || typeof value.intakeSha256 !== 'string' || !HEX.test(value.intakeSha256)) return fail('request_invalid')
  }
  if (state.intakeSha256 !== currentState.intakeSha256) return fail('scope_stale')
  if (state.rev !== currentState.rev) return fail('stale_graph_revision')
  if (typeof catalogDigest !== 'string' || !DIGEST.test(catalogDigest)) return fail('catalog_digest_required')
  if (typeof idempotencyKey !== 'string' || !idempotencyKey.trim()
      || new TextEncoder().encode(idempotencyKey).byteLength > 256 || CAPABILITY.test(idempotencyKey)) {
    return fail('idempotency_key_required')
  }
  const params = { drawing_id: exact.drawing_id, expected_rev: state.rev }
  if (tool === 'solar-panel-groups') {
    if (!Array.isArray(groups) || !groups.length || groups.length > 128) return fail('request_invalid')
    params.groups = groups
  }
  if (tool === 'solar-schedule') {
    const parsed = parseProjectSolarInsertionPoint(insertionPointText)
    if (!parsed.ok) return parsed
    params.insertion_point = parsed.point
  } else if (insertionPointText !== undefined && insertionPointText !== '') return fail('request_invalid')
  return { ok: true, request: { tool, params, catalogDigest, idempotencyKey } }
}

export function exactProjectSolarGraphPreview(input) {
  try {
    const { tool, catalogDigest, outcome } = input
    const fail = (reasonCode = null) => ({ accepted: false, reasonCode })
    if (outcome?.ok !== true) return fail(boundedCode(outcome?.envelope?.error?.reason_code) ?? boundedCode(outcome?.reason))
    const families = outcome.data?.families
    if (!PROJECT_SOLAR_GRAPH_TOOLS.includes(tool) || typeof catalogDigest !== 'string' || !DIGEST.test(catalogDigest)
        || !Array.isArray(families) || families.length > 64) return fail()
    const rows = []
    let total = 0
    for (const family of families) {
      if (!Array.isArray(family?.capabilities)) return fail()
      total += family.capabilities.length
      if (total > 1024) return fail()
      for (const row of family.capabilities) if (row?.name === tool) rows.push(row)
    }
    if (!rows.length) return fail()
    let accepted = true
    const codes = new Set()
    for (const row of rows) {
      const view = row.availability
      const ready = row.catalog_digest === catalogDigest && view?.admission_checked === true
        && view.admission?.status_code === 200 && view.admission.error === null
        && ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].every((key) => view[key] === true)
        && Array.isArray(view.refusal_reasons) && view.refusal_reasons.length === 0
      if (!ready) accepted = false
      const admissionCode = boundedCode(view?.admission?.error?.reason_code)
      if (admissionCode) codes.add(admissionCode)
      else if (Array.isArray(view?.refusal_reasons)) {
        if (view.refusal_reasons.length > 1024) return fail()
        for (const value of view.refusal_reasons) {
          const code = boundedCode(value)
          if (code) codes.add(code)
        }
      }
    }
    if (accepted) return { accepted: true, reasonCode: null }
    if (!codes.size) {
      const code = boundedCode(outcome.reason)
      if (code) codes.add(code)
    }
    return fail(codes.size === 1 ? [...codes][0] : null)
  } catch { return { accepted: false, reasonCode: null } }
}

function refusalText(code, tool) {
  switch (code?.toLowerCase()) {
    case 'submitted':
    case 'pending':
    case 'succeeded':
    case 'context_loading': return PROJECT_SOLAR_GRAPH_REASONS.request_refused
    default: return projectSolarGraphReason(code ?? 'request_refused', tool)
  }
}

export function projectSolarGraphFeedback(input) {
  try {
    const { tool, submission, completion } = input
    const result = (state, reasonCode = null, jobId = null, outputVersionId = null) => ({ state, reasonCode,
      text: state === 'refused' || (state === 'failed' && reasonCode !== null) ? refusalText(reasonCode, tool)
        : projectSolarGraphReason(state === 'stale' ? 'scope_stale' : state, tool), jobId, outputVersionId })
    if ((submission !== undefined) === (completion !== undefined)) return result('outcome_unknown')
    if (submission !== undefined) {
      if (submission?.ok === true && submission.status === 202 && submission.data?.status === 'submitted'
          && validUuid(submission.data.job_id) && submission.reason == null && submission.error == null
          && submission.envelope?.error == null && submission.data.error == null) {
        return result('submitted', null, submission.data.job_id)
      }
      if (submission?.ok === false) {
        const code = boundedCode(submission.envelope?.error?.reason_code) ?? boundedCode(submission.reason)
        const lower = code?.toLowerCase()
        if (lower === 'sip_r13b_run_in_progress') return result('refused', code)
        if (lower === 'sip_r13b_outcome_unknown' || lower === 'outcome_unknown'
            || lower === 'sip_r7_transport_failed' || lower === 'sip_r7_timeout'
            || lower === 'sip_r7_response_invalid' || (!code && submission.status == null)
            || (submission.status != null && (!Number.isInteger(submission.status) || submission.status >= 500))) {
          return result('outcome_unknown', lower === 'sip_r13b_outcome_unknown' ? code : null)
        }
        if ((submission.status >= 400 && submission.status < 500) || (submission.status == null && code)) {
          return result('refused', code)
        }
      }
      return result('outcome_unknown')
    }
    if (!completion || !validUuid(completion.jobId)) return result('outcome_unknown')
    const { state, jobId } = completion
    const code = boundedCode(completion.reasonCode)
    if (state === 'succeeded') {
      if (!validUuid(completion.outputVersionId) || completion.reasonCode != null) return result('outcome_unknown', null, jobId)
      return result('succeeded', null, jobId, completion.outputVersionId)
    }
    if (completion.outputVersionId != null) return result('outcome_unknown', null, jobId)
    switch (state) {
      case 'pending': return code ? result('outcome_unknown', null, jobId) : result('pending', null, jobId)
      case 'refused': return result('refused', code, jobId)
      case 'failed': return result('failed', code, jobId)
      case 'unreadable': return result('unreadable', null, jobId)
      case 'outcome_unknown': return result('outcome_unknown', null, jobId)
      case 'aborted': return result('aborted', null, jobId)
      case 'stale': return result('stale', null, jobId)
      default: return result('outcome_unknown', null, jobId)
    }
  } catch {
    return { state: 'outcome_unknown', reasonCode: null, text: PROJECT_SOLAR_GRAPH_REASONS.outcome_unknown,
      jobId: null, outputVersionId: null }
  }
}
