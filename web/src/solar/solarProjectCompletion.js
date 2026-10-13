import { SOLAR_REFUSAL_REASONS } from '../lib/ribbonClusters.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'

export const PROJECT_SOLAR_RUN_REASONS = Object.freeze({
  unreadable_output: 'This Solar run finished, but its output could not be read.',
  outcome_unknown: 'The Solar run outcome is unknown. Check its job before submitting again.',
  run_in_progress: 'A Solar run is already in progress for this drawing version.',
  request_invalid: 'This Solar run request could not be prepared.',
})

const UUID = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
const CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const TOOL = new RegExp('^[a-z0-9][a-z0-9-]{0,127}$')
const uuid = (value) => typeof value === 'string' && UUID.test(value)
const assert = (value) => { if (!value) throw new TypeError('Invalid job record') }
function field(record, key, required = true) {
  assert(record !== null && typeof record === 'object' &&
    [Object.prototype, null].includes(Object.getPrototypeOf(record)))
  const descriptor = Object.getOwnPropertyDescriptor(record, key)
  if (!descriptor) { assert(!required); return undefined }
  assert(Object.hasOwn(descriptor, 'value'))
  return descriptor.value
}
function record(value) {
  assert(value !== null && typeof value === 'object' &&
    [Object.prototype, null].includes(Object.getPrototypeOf(value)))
  return value
}
const present = (value, key) => !!Object.getOwnPropertyDescriptor(value, key)
const completion = (state, jobId, outputVersionId = null, reasonCode = null, message = null) =>
  Object.freeze({ state, jobId, outputVersionId, reasonCode, message })

export function projectSolarRunFailure(code) {
  if (typeof code !== 'string' || !CODE.test(code)) {
    return Object.freeze({ reasonCode: null, message: SOLAR_REFUSAL_REASONS.unlisted })
  }
  const key = code.toLowerCase()
  switch (key) {
    case 'cloud_auth_missing': return Object.freeze({ reasonCode: key, message: SOLAR_SIZING_RUN_REASONS.grant_missing })
    case 'cloud_tenant_unauthorized': return Object.freeze({ reasonCode: key, message: SOLAR_SIZING_RUN_REASONS.grant_scope })
    case 'cloud_upstream_failure': return Object.freeze({ reasonCode: key, message: SOLAR_SIZING_RUN_REASONS.cloud_unavailable })
    case 'cloud_response_invalid': return Object.freeze({ reasonCode: key, message: SOLAR_SIZING_RUN_REASONS.cloud_response_invalid })
    case 'cloud_request_invalid': return Object.freeze({ reasonCode: key, message: SOLAR_SIZING_RUN_REASONS.request_invalid })
    case 'invalid_pvcase_convert_request': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_pvcase_convert_request })
    case 'invalid_pvcase_solve_request': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_pvcase_solve_request })
    case 'invalid_pvcase_export_request': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_pvcase_export_request })
    case 'pvcase_empty_target_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_empty_target_required })
    case 'pvcase_conversion_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_conversion_required })
    case 'pvcase_solve_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_solve_required })
    case 'pvcase_target_in_use': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_target_in_use })
    case 'pvcase_source_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_source_required })
    case 'pvcase_source_unavailable': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvcase_source_unavailable })
    case 'pvg_invalid_json': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_invalid_json })
    case 'pvg_input_bytes_exceeded': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_input_bytes_exceeded })
    case 'pvg_envelope_fields': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_envelope_fields })
    case 'pvg_envelope_schema': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_envelope_schema })
    case 'pvg_invalid_intake': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_invalid_intake })
    case 'pvg_list_limit': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_list_limit })
    case 'pvg_depth_limit': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_depth_limit })
    case 'pvg_node_limit': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_node_limit })
    case 'pvg_byte_limit': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_byte_limit })
    case 'pvg_matrix_limit': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_matrix_limit })
    case 'pvg_geometry_range': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_geometry_range })
    case 'pvg_invalid_source': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_invalid_source })
    case 'pvg_invalid_target': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_invalid_target })
    case 'pvg_target_context': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_target_context })
    case 'pvg_target_not_empty': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_target_not_empty })
    case 'pvg_no_panel_groups': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_no_panel_groups })
    case 'pvg_no_usable_panels': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_no_usable_panels })
    case 'pvg_invalid_result': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.pvg_invalid_result })
    case 'frame_membership_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.frame_membership_mismatch })
    case 'matrix_cell_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.matrix_cell_mismatch })
    case 'frame_sequence_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.frame_sequence_mismatch })
    case 'matrix_input_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.matrix_input_mismatch })
    case 'entitlement_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.entitlement_required })
    case 'entitlement_policy_unavailable': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.entitlement_policy_unavailable })
    case 'broker_adapter_unavailable': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.broker_adapter_unavailable })
    case 'drawing_context_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.drawing_context_required })
    case 'invalid_drawing_context': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_drawing_context })
    case 'persisted_graph_unavailable': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.persisted_graph_unavailable })
    case 'graph_seed_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.graph_seed_required })
    case 'not_current_head': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.not_current_head })
    case 'seed_project_scope_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.seed_project_scope_unsupported })
    case 'invalid_seed_request': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_seed_request })
    case 'graph_already_embedded': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.graph_already_embedded })
    case 'unresolved_units': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.unresolved_units })
    case 'strings_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.strings_required })
    case 'valid_settings_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.valid_settings_required })
    case 'project_name_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_name_required })
    case 'project_zip_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_zip_required })
    case 'invalid_project_zip': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_project_zip })
    case 'invalid_project_coordinates': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_project_coordinates })
    case 'project_units_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_units_required })
    case 'sizing_confirmation_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.sizing_confirmation_required })
    case 'sized_panel_groups_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.sized_panel_groups_required })
    case 'valid_strings_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.valid_strings_required })
    case 'equipment_assignment_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.equipment_assignment_required })
    case 'complete_routing_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.complete_routing_required })
    case 'licensed_graph_commit_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.licensed_graph_commit_required })
    case 'module_power_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.module_power_required })
    case 'roof_installation_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.roof_installation_required })
    case 'routing_topology_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.routing_topology_required })
    case 'inverter_assignment_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.inverter_assignment_mismatch })
    case 'degenerate_route': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.degenerate_route })
    case 'invalid_route_point': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.invalid_route_point })
    case 'proposal_job_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.proposal_job_required })
    case 'frames_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.frames_required })
    case 'panels_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.panels_required })
    case 'electrical_zones_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.electrical_zones_required })
    case 'panels_already_present': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.panels_already_present })
    case 'panel_layer_filter_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.panel_layer_filter_required })
    case 'rooftop_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.rooftop_required })
    case 'drawing_units_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.drawing_units_unsupported })
    case 'guardrails_sizing_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.guardrails_sizing_required })
    case 'guardrails_sizing_ambiguous': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.guardrails_sizing_ambiguous })
    case 'guardrails_project_coordinates_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.guardrails_project_coordinates_required })
    case 'guardrails_mixed_inverters': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.guardrails_mixed_inverters })
    case 'guardrails_input_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.guardrails_input_unsupported })
    case 'electrical_zones_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.electrical_zones_unsupported })
    case 'unassigned_strings_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.unassigned_strings_required })
    case 'string_collectors_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.string_collectors_required })
    case 'schedules_input_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.schedules_input_unsupported })
    case 'schedules_kernel_refused': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.schedules_kernel_refused })
    case 'schedules_mapping_failed': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.schedules_mapping_failed })
    case 'schedules_optimizers_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.schedules_optimizers_unsupported })
    case 'schedules_zone_sizing_unsupported': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.schedules_zone_sizing_unsupported })
    case 'solar_output_not_current': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.solar_output_not_current })
    case 'ground_installation_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_installation_required })
    case 'ground_conversion_in_use': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_conversion_in_use })
    case 'ground_physical_state_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_physical_state_required })
    case 'ground_tracker_rows_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_tracker_rows_required })
    case 'ground_units_mismatch': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_units_mismatch })
    case 'ground_layout_invalid': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_layout_invalid })
    case 'ground_layout_too_large': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.ground_layout_too_large })
    case 'project_execution_disabled': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_execution_disabled })
    case 'project_adapter_unavailable': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_adapter_unavailable })
    case 'project_request_required': return Object.freeze({ reasonCode: key, message: SOLAR_REFUSAL_REASONS.project_request_required })
    default: return Object.freeze({ reasonCode: null, message: SOLAR_REFUSAL_REASONS.unlisted })
  }
}

export function readProjectSolarCompletion(options = {}) {
  let job, binding, jobId, tool
  let retainedId = null
  const unreadable = () => completion('unreadable', retainedId, null, null,
    PROJECT_SOLAR_RUN_REASONS.unreadable_output)
  try {
    jobId = field(options, 'jobId')
    retainedId = uuid(jobId) ? jobId : null
    job = field(options, 'job')
    binding = field(options, 'binding')
    tool = field(options, 'tool')
    assert(retainedId && typeof tool === 'string' && TOOL.test(tool))
    assert(field(binding, 'kind') === 'project')
    for (const [jobKey, bindingKey] of [
      ['org_id', 'organization_id'], ['project_id', 'project_id'],
      ['input_version_id', 'input_version_id'],
    ]) {
      const expected = field(binding, bindingKey)
      assert(uuid(expected) && field(job, jobKey) === expected)
    }
    assert(field(job, 'job_id') === jobId && field(job, 'tool_name') === tool)
    const context = field(job, 'execution_context')
    const drawing = field(binding, 'drawing_id')
    assert(uuid(drawing) && field(context, 'drawing_id') === drawing &&
      field(context, 'schema') === 'leaf.solar-project-graph-job.v1')
    const status = field(job, 'status')
    if (status === 'submitted' || status === 'running') return completion('pending', jobId)
    if (status === 'complete') {
      const result = record(field(job, 'result'))
      const output = field(result, 'output_version_id')
      assert(uuid(output))
      const top = field(job, 'output_version_id', false)
      assert(!present(job, 'output_version_id') || (uuid(top) && top === output))
      const commit = field(result, 'graph_commit', false)
      if (present(result, 'graph_commit')) {
        record(commit)
        const receiptOutput = field(commit, 'output_version_id', false)
        assert(!present(commit, 'output_version_id') || (uuid(receiptOutput) && receiptOutput === output))
      }
      const error = field(job, 'error', false)
      assert(!present(job, 'error') || error === null)
      return completion('succeeded', jobId, output)
    }
    assert(status === 'failed' || status === 'cancelled')
    const error = record(field(job, 'error'))
    const code = field(error, 'error_code')
    const reason = field(error, 'reason_code', false)
    assert(typeof code === 'string' && CODE.test(code))
    assert(!present(error, 'reason_code') || (typeof reason === 'string' && CODE.test(reason)))
    assert(typeof field(error, 'retryable') === 'boolean')
    const result = field(job, 'result', false)
    const output = field(job, 'output_version_id', false)
    assert((!present(job, 'result') || result === null) && (!present(job, 'output_version_id') || output === null))
    if ((reason === undefined ? code : reason).toLowerCase() === 'sizing_outcome_unknown') {
      return completion('outcome_unknown', jobId, null, null, PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
    }
    const failure = projectSolarRunFailure(reason === undefined ? code : reason)
    return completion('failed', jobId, null, failure.reasonCode, failure.message)
  } catch { return unreadable() }
}
