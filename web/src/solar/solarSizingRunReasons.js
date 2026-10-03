export const SOLAR_SIZING_RUN_REASONS = Object.freeze({
  grant_missing: 'String sizing needs a valid cloud sizing grant, which is unavailable for this workspace. Your inputs are kept.',
  grant_scope: 'The cloud sizing grant does not authorize string sizing for this workspace. Your inputs are kept.',
  request_invalid: 'Some sizing inputs are invalid or incomplete. Review the form before retrying. Your inputs are kept.',
  coverage_invalid: 'The sizing requests must cover every target, with each panel in exactly one zone when using zones. Your inputs are kept.',
  models_mismatch: 'The sizing equipment does not match the saved zone equipment. Review the module and inverter. Your inputs are kept.',
  project_mismatch: 'The sizing location does not match the saved project ZIP. Review the project location. Your inputs are kept.',
  cloud_unavailable: 'The cloud sizing service could not complete this request. Try again later. Your inputs are kept.',
  cloud_response_invalid: 'The cloud sizing service returned a result that could not be validated. Your inputs are kept.',
  cold_voltage: 'The proposed string length exceeds the inverter voltage limit in cold conditions. Your inputs are kept.',
  stale: 'String sizing needs the current drawing revision. Review the drawing before retrying. Your inputs are kept.',
  settings: 'Complete valid Solar settings before sizing strings. Your inputs are kept.',
  units: 'Resolve the drawing units before sizing strings. Your inputs are kept.',
  panels: 'The drawing needs panels before strings can be sized. Your inputs are kept.',
  confirmation: 'The sizing result could not be confirmed against this drawing. Your inputs are kept.',
  graph_unavailable: 'This saved drawing is not available for string sizing. Review its version and format. Your inputs are kept.',
  graph_invalid: 'The saved design contains data that string sizing cannot validate. Review the drawing. Your inputs are kept.',
  checkout: 'String sizing needs your active checkout of this drawing. Your inputs are kept.',
  storage: 'Drawing storage is unavailable, so the sizing result could not be confirmed. Your inputs are kept.',
  commit_unconfirmed: 'The saved sizing result could not be verified. Check the current drawing before retrying. Your inputs are kept.',
  commit_refused: 'String sizing cannot update this drawing with the current run request. Your inputs are kept.',
  cancelled: 'The string sizing request was cancelled. Your inputs are kept.',
  broker_unavailable: 'The sizing worker could not be reached. Check the current run before retrying. Your inputs are kept.',
  timeout: 'The sizing run did not finish within its time limit. Check the current run before retrying. Your inputs are kept.',
  access: 'This workspace or session is not permitted to run string sizing. Your inputs are kept.',
  quota: 'A workspace run or spending limit prevented string sizing. Your inputs are kept.',
  unknown: 'String sizing did not return a confirmed result. Your inputs are kept.',
})

export function solarSizingRunSentence(failureCode) {
  if (typeof failureCode !== 'string' || failureCode.length === 0 || failureCode.length > 64
    || failureCode.trim() !== failureCode
    || !/^[A-Za-z][A-Za-z0-9_]*$/.test(failureCode)) return SOLAR_SIZING_RUN_REASONS.unknown
  switch (failureCode.toUpperCase()) {
    case 'CLOUD_AUTH_MISSING':
      return SOLAR_SIZING_RUN_REASONS.grant_missing
    case 'CLOUD_TENANT_UNAUTHORIZED':
      return SOLAR_SIZING_RUN_REASONS.grant_scope
    case 'CLOUD_REQUEST_INVALID':
    case 'INVALID_SIZING_REQUEST':
    case 'INVALID_SIZING_MODE':
    case 'INVALID_SEED_REQUEST':
    case 'INVALID_COMMIT_REQUEST':
    case 'INVALID_NUMERIC_PARAM':
    case 'INVALID_PARENT_VERSION':
    case 'DRAWING_ID_CONFLICT':
    case 'JOB_IDENTITY_MISSING':
    case 'LOCAL_GRAPH_COMMIT_INVALID':
    case 'INVALID_SOLVE_BINDING':
    case 'FILE_ONLY_REQUEST_INVALID':
    case 'GRAPH_LIMIT_EXCEEDED':
    case 'INVALID_JSON_OBJECT':
    case 'INVALID_JSON_STRING':
    case 'NONFINITE_NUMBER':
    case 'NUMBER_LIMIT_EXCEEDED':
    case 'INVALID_JSON_VALUE':
      return SOLAR_SIZING_RUN_REASONS.request_invalid
    case 'INVALID_SIZING_COVERAGE':
    case 'INVALID_ZONE_COVERAGE':
      return SOLAR_SIZING_RUN_REASONS.coverage_invalid
    case 'SIZING_MODEL_MISMATCH':
      return SOLAR_SIZING_RUN_REASONS.models_mismatch
    case 'SIZING_PROJECT_MISMATCH':
      return SOLAR_SIZING_RUN_REASONS.project_mismatch
    case 'CLOUD_UPSTREAM_FAILURE':
      return SOLAR_SIZING_RUN_REASONS.cloud_unavailable
    case 'CLOUD_RESPONSE_INVALID':
      return SOLAR_SIZING_RUN_REASONS.cloud_response_invalid
    case 'COLD_VOLTAGE_FAILED':
      return SOLAR_SIZING_RUN_REASONS.cold_voltage
    case 'STALE_GRAPH_REVISION':
    case 'NOT_CURRENT_HEAD':
      return SOLAR_SIZING_RUN_REASONS.stale
    case 'VALID_SETTINGS_REQUIRED':
      return SOLAR_SIZING_RUN_REASONS.settings
    case 'UNRESOLVED_UNITS':
      return SOLAR_SIZING_RUN_REASONS.units
    case 'MISSING_PANEL':
      return SOLAR_SIZING_RUN_REASONS.panels
    case 'SIZING_CONFIRMATION_REQUIRED':
    case 'MODULE_POWER_REQUIRED':
      return SOLAR_SIZING_RUN_REASONS.confirmation
    case 'DRAWING_CONTEXT_REQUIRED':
    case 'INVALID_DRAWING_CONTEXT':
    case 'PERSISTED_GRAPH_UNAVAILABLE':
    case 'LICENSED_GRAPH_COMMIT_REQUIRED':
    case 'GRAPH_CONTEXT_UNAVAILABLE':
    case 'GRAPH_NOT_EMBEDDED':
      return SOLAR_SIZING_RUN_REASONS.graph_unavailable
    case 'GRAPH_DIGEST_MISMATCH':
    case 'PROJECT_MISMATCH':
    case 'INVALID_GROUND_SLOTS':
    case 'INVALID_GRAPH':
    case 'UNSUPPORTED_SCHEMA_VERSION':
    case 'UNKNOWN_UNITS':
    case 'INVALID_GRAPH_SCHEMA':
    case 'INVALID_REVISION_CHAIN':
    case 'DUPLICATE_APPLICATION_ID':
    case 'ID_KIND_MISMATCH':
    case 'FUTURE_ENTITY_REVISION':
    case 'FUTURE_SCHEDULE_REVISION':
    case 'INVALID_SCHEDULE_COLUMNS':
    case 'INSTALLATION_DESIGN_MISMATCH':
    case 'TRACKER_SLOT_MISMATCH':
    case 'DEGENERATE_TRACKER_AXIS':
    case 'DUPLICATE_TRACKER_SOURCE':
    case 'STRING_COUNT_MISMATCH':
    case 'DUPLICATE_PANEL_MEMBERSHIP':
    case 'PANEL_ASSIGNMENT_MISMATCH':
    case 'FRAME_MEMBERSHIP_MISMATCH':
    case 'MISSING_ELECTRICAL_ZONE':
    case 'INVALID_MATRIX_DIMENSIONS':
    case 'INVALID_MATRIX_PANEL':
    case 'MATRIX_CELL_MISMATCH':
    case 'MATRIX_SEQUENCE_MISMATCH':
    case 'FRAME_ASSIGNMENT_MISMATCH':
    case 'FRAME_SEQUENCE_MISMATCH':
    case 'INVERTER_CAPACITY_EXCEEDED':
    case 'INVERTER_ASSIGNMENT_MISMATCH':
    case 'DUPLICATE_INVERTER_INPUT':
    case 'EQUIPMENT_TYPE_REQUIRED':
    case 'L2_MODE_REQUIRED':
    case 'DUPLICATE_EQUIPMENT_NUMBER':
    case 'L2_MIXED_INPUTS':
    case 'L2_CAPACITY_EXCEEDED':
    case 'L2_ASSIGNMENT_MISMATCH':
    case 'DUPLICATE_L2_INPUT':
    case 'MATRIX_INPUT_MISMATCH':
    case 'ROUTE_ENDPOINT_MISMATCH':
    case 'DUPLICATE_FEEDER':
    case 'ROUTE_PATHWAY_MISMATCH':
      return SOLAR_SIZING_RUN_REASONS.graph_invalid
    case 'CHECKOUT_REQUIRED':
    case 'CHECKOUT_DENIED':
      return SOLAR_SIZING_RUN_REASONS.checkout
    case 'GRAPH_STORE_UNAVAILABLE':
      return SOLAR_SIZING_RUN_REASONS.storage
    case 'GRAPH_COMMIT_READBACK_FAILED':
      return SOLAR_SIZING_RUN_REASONS.commit_unconfirmed
    case 'INVALID_JOB_BINDING':
    case 'JOB_BINDING_REUSED':
    case 'STALE_GRAPH_COMPANION':
    case 'DRAWING_MUTATION_REFUSED':
    case 'GRAPH_COMMIT_REFUSED':
    case 'UNKNOWN_LOCAL_GRAPH_TOOL':
    case 'DRAWING_MUTATIONS_ENV_DISABLED':
    case 'DRAWING_MUTATIONS_FENCE_CLOSED':
    case 'DRAWING_MUTATIONS_FENCE_UNREADABLE':
    case 'DRAWING_MUTATIONS_FENCE_LOCK_UNAVAILABLE':
    case 'DRAWING_MUTATIONS_REFUSED_UNATTRIBUTED':
      return SOLAR_SIZING_RUN_REASONS.commit_refused
    case 'GRAPH_COMMIT_CANCELLED':
      return SOLAR_SIZING_RUN_REASONS.cancelled
    case 'BROKER_UNREACHABLE':
      return SOLAR_SIZING_RUN_REASONS.broker_unavailable
    case 'TIMEOUT':
      return SOLAR_SIZING_RUN_REASONS.timeout
    case 'FORBIDDEN':
    case 'UNAUTHENTICATED':
    case 'TENANT_DISABLED':
    case 'ENTITLEMENT_REQUIRED':
      return SOLAR_SIZING_RUN_REASONS.access
    case 'QUOTA_EXCEEDED':
      return SOLAR_SIZING_RUN_REASONS.quota
    case 'INTERNAL':
    case 'BAD_PARAMS':
    case 'UNKNOWN_TOOL':
    case 'WORKITEM_FAILED':
    case 'APS_UNAVAILABLE':
      return SOLAR_SIZING_RUN_REASONS.unknown
    default:
      return SOLAR_SIZING_RUN_REASONS.unknown
  }
}
