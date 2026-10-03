import { describe, expect, it } from 'vitest'
import { SOLAR_SIZING_RUN_REASONS, solarSizingRunSentence } from './solarSizingRunReasons.js'
import { solarFlowRunOutcome } from './solarFlowModel.js'

const sentences = {
  "grant_missing": "String sizing needs a valid cloud sizing grant, which is unavailable for this workspace. Your inputs are kept.",
  "grant_scope": "The cloud sizing grant does not authorize string sizing for this workspace. Your inputs are kept.",
  "request_invalid": "Some sizing inputs are invalid or incomplete. Review the form before retrying. Your inputs are kept.",
  "coverage_invalid": "The sizing requests must cover every target, with each panel in exactly one zone when using zones. Your inputs are kept.",
  "models_mismatch": "The sizing equipment does not match the saved zone equipment. Review the module and inverter. Your inputs are kept.",
  "project_mismatch": "The sizing location does not match the saved project ZIP. Review the project location. Your inputs are kept.",
  "cloud_unavailable": "The cloud sizing service could not complete this request. Try again later. Your inputs are kept.",
  "cloud_response_invalid": "The cloud sizing service returned a result that could not be validated. Your inputs are kept.",
  "cold_voltage": "The proposed string length exceeds the inverter voltage limit in cold conditions. Your inputs are kept.",
  "stale": "String sizing needs the current drawing revision. Review the drawing before retrying. Your inputs are kept.",
  "settings": "Complete valid Solar settings before sizing strings. Your inputs are kept.",
  "units": "Resolve the drawing units before sizing strings. Your inputs are kept.",
  "panels": "The drawing needs panels before strings can be sized. Your inputs are kept.",
  "confirmation": "The sizing result could not be confirmed against this drawing. Your inputs are kept.",
  "graph_unavailable": "This saved drawing is not available for string sizing. Review its version and format. Your inputs are kept.",
  "graph_invalid": "The saved design contains data that string sizing cannot validate. Review the drawing. Your inputs are kept.",
  "checkout": "String sizing needs your active checkout of this drawing. Your inputs are kept.",
  "storage": "Drawing storage is unavailable, so the sizing result could not be confirmed. Your inputs are kept.",
  "commit_unconfirmed": "The saved sizing result could not be verified. Check the current drawing before retrying. Your inputs are kept.",
  "commit_refused": "String sizing cannot update this drawing with the current run request. Your inputs are kept.",
  "cancelled": "The string sizing request was cancelled. Your inputs are kept.",
  "broker_unavailable": "The sizing worker could not be reached. Check the current run before retrying. Your inputs are kept.",
  "timeout": "The sizing run did not finish within its time limit. Check the current run before retrying. Your inputs are kept.",
  "access": "This workspace or session is not permitted to run string sizing. Your inputs are kept.",
  "quota": "A workspace run or spending limit prevented string sizing. Your inputs are kept.",
  "unknown": "String sizing did not return a confirmed result. Your inputs are kept."
}
const aliases = [
  ["CLOUD_AUTH_MISSING","grant_missing","FORBIDDEN"],
  ["CLOUD_TENANT_UNAUTHORIZED","grant_scope","FORBIDDEN"],
  ["CLOUD_REQUEST_INVALID","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_REQUEST","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_MODE","request_invalid","BAD_PARAMS"],
  ["invalid_seed_request","request_invalid","BAD_PARAMS"],
  ["INVALID_COMMIT_REQUEST","request_invalid","BAD_PARAMS"],
  ["INVALID_NUMERIC_PARAM","request_invalid","BAD_PARAMS"],
  ["INVALID_PARENT_VERSION","request_invalid","BAD_PARAMS"],
  ["DRAWING_ID_CONFLICT","request_invalid","BAD_PARAMS"],
  ["INVALID_SEED_REQUEST","request_invalid","BAD_PARAMS"],
  ["JOB_IDENTITY_MISSING","request_invalid","BAD_PARAMS"],
  ["local_graph_commit_invalid","request_invalid","BAD_PARAMS"],
  ["invalid_solve_binding","request_invalid","BAD_PARAMS"],
  ["file_only_request_invalid","request_invalid","BAD_PARAMS"],
  ["GRAPH_LIMIT_EXCEEDED","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_OBJECT","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_STRING","request_invalid","BAD_PARAMS"],
  ["NONFINITE_NUMBER","request_invalid","BAD_PARAMS"],
  ["NUMBER_LIMIT_EXCEEDED","request_invalid","BAD_PARAMS"],
  ["INVALID_JSON_VALUE","request_invalid","BAD_PARAMS"],
  ["INVALID_SIZING_COVERAGE","coverage_invalid","BAD_PARAMS"],
  ["INVALID_ZONE_COVERAGE","coverage_invalid","BAD_PARAMS"],
  ["SIZING_MODEL_MISMATCH","models_mismatch","BAD_PARAMS"],
  ["SIZING_PROJECT_MISMATCH","project_mismatch","BAD_PARAMS"],
  ["CLOUD_UPSTREAM_FAILURE","cloud_unavailable","WORKITEM_FAILED"],
  ["CLOUD_RESPONSE_INVALID","cloud_response_invalid","WORKITEM_FAILED"],
  ["COLD_VOLTAGE_FAILED","cold_voltage","BAD_PARAMS"],
  ["STALE_GRAPH_REVISION","stale","BAD_PARAMS"],
  ["not_current_head","stale","BAD_PARAMS"],
  ["valid_settings_required","settings","BAD_PARAMS"],
  ["UNRESOLVED_UNITS","units","BAD_PARAMS"],
  ["MISSING_PANEL","panels","BAD_PARAMS"],
  ["SIZING_CONFIRMATION_REQUIRED","confirmation","BAD_PARAMS"],
  ["MODULE_POWER_REQUIRED","confirmation","BAD_PARAMS"],
  ["drawing_context_required","graph_unavailable","BAD_PARAMS"],
  ["invalid_drawing_context","graph_unavailable","BAD_PARAMS"],
  ["persisted_graph_unavailable","graph_unavailable","BAD_PARAMS"],
  ["licensed_graph_commit_required","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_CONTEXT_UNAVAILABLE","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_NOT_EMBEDDED","graph_unavailable","BAD_PARAMS"],
  ["LICENSED_GRAPH_COMMIT_REQUIRED","graph_unavailable","BAD_PARAMS"],
  ["GRAPH_DIGEST_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["PROJECT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["INVALID_GROUND_SLOTS","graph_invalid","BAD_PARAMS"],
  ["INVALID_GRAPH","graph_invalid","BAD_PARAMS"],
  ["UNSUPPORTED_SCHEMA_VERSION","graph_invalid","BAD_PARAMS"],
  ["UNKNOWN_UNITS","graph_invalid","BAD_PARAMS"],
  ["INVALID_GRAPH_SCHEMA","graph_invalid","BAD_PARAMS"],
  ["INVALID_REVISION_CHAIN","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_APPLICATION_ID","graph_invalid","BAD_PARAMS"],
  ["ID_KIND_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FUTURE_ENTITY_REVISION","graph_invalid","BAD_PARAMS"],
  ["FUTURE_SCHEDULE_REVISION","graph_invalid","BAD_PARAMS"],
  ["INVALID_SCHEDULE_COLUMNS","graph_invalid","BAD_PARAMS"],
  ["INSTALLATION_DESIGN_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["TRACKER_SLOT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DEGENERATE_TRACKER_AXIS","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_TRACKER_SOURCE","graph_invalid","BAD_PARAMS"],
  ["STRING_COUNT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_PANEL_MEMBERSHIP","graph_invalid","BAD_PARAMS"],
  ["PANEL_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_MEMBERSHIP_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["MISSING_ELECTRICAL_ZONE","graph_invalid","BAD_PARAMS"],
  ["INVALID_MATRIX_DIMENSIONS","graph_invalid","BAD_PARAMS"],
  ["INVALID_MATRIX_PANEL","graph_invalid","BAD_PARAMS"],
  ["MATRIX_CELL_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["MATRIX_SEQUENCE_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["FRAME_SEQUENCE_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["INVERTER_CAPACITY_EXCEEDED","graph_invalid","BAD_PARAMS"],
  ["INVERTER_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_INVERTER_INPUT","graph_invalid","BAD_PARAMS"],
  ["EQUIPMENT_TYPE_REQUIRED","graph_invalid","BAD_PARAMS"],
  ["L2_MODE_REQUIRED","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_EQUIPMENT_NUMBER","graph_invalid","BAD_PARAMS"],
  ["L2_MIXED_INPUTS","graph_invalid","BAD_PARAMS"],
  ["L2_CAPACITY_EXCEEDED","graph_invalid","BAD_PARAMS"],
  ["L2_ASSIGNMENT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_L2_INPUT","graph_invalid","BAD_PARAMS"],
  ["MATRIX_INPUT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["ROUTE_ENDPOINT_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["DUPLICATE_FEEDER","graph_invalid","BAD_PARAMS"],
  ["ROUTE_PATHWAY_MISMATCH","graph_invalid","BAD_PARAMS"],
  ["CHECKOUT_REQUIRED","checkout","FORBIDDEN"],
  ["CHECKOUT_DENIED","checkout","FORBIDDEN"],
  ["GRAPH_STORE_UNAVAILABLE","storage","INTERNAL"],
  ["GRAPH_COMMIT_READBACK_FAILED","commit_unconfirmed","INTERNAL"],
  ["INVALID_JOB_BINDING","commit_refused","BAD_PARAMS"],
  ["JOB_BINDING_REUSED","commit_refused","BAD_PARAMS"],
  ["STALE_GRAPH_COMPANION","commit_refused","BAD_PARAMS"],
  ["DRAWING_MUTATION_REFUSED","commit_refused","BAD_PARAMS"],
  ["GRAPH_COMMIT_REFUSED","commit_refused","BAD_PARAMS"],
  ["UNKNOWN_LOCAL_GRAPH_TOOL","commit_refused","BAD_PARAMS"],
  ["drawing_mutations_env_disabled","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_closed","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_unreadable","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_fence_lock_unavailable","commit_refused","APS_UNAVAILABLE"],
  ["drawing_mutations_refused_unattributed","commit_refused","APS_UNAVAILABLE"],
  ["GRAPH_COMMIT_CANCELLED","cancelled","BAD_PARAMS"],
  ["BROKER_UNREACHABLE","broker_unavailable","BROKER_UNREACHABLE"],
  ["TIMEOUT","timeout","TIMEOUT"],
  ["FORBIDDEN","access","FORBIDDEN"],
  ["UNAUTHENTICATED","access","UNAUTHENTICATED"],
  ["TENANT_DISABLED","access","TENANT_DISABLED"],
  ["ENTITLEMENT_REQUIRED","access","ENTITLEMENT_REQUIRED"],
  ["entitlement_required","access","FORBIDDEN"],
  ["quota_exceeded","quota","quota_exceeded"],
  ["INTERNAL","unknown","INTERNAL"],
  ["BAD_PARAMS","unknown","BAD_PARAMS"],
  ["UNKNOWN_TOOL","unknown","UNKNOWN_TOOL"],
  ["WORKITEM_FAILED","unknown","WORKITEM_FAILED"],
  ["APS_UNAVAILABLE","unknown","APS_UNAVAILABLE"],
]

describe('Sizing run sentences', () => {
  it.each(aliases)('G1A-1 %s selects %s for reason shapes and case variants', (code, klass, error_code) => {
    for (const reason of new Set([code, code.toUpperCase()])) {
      expect(solarSizingRunSentence(reason)).toBe(sentences[klass])
      for (const envelope of [
        { ok: false, reason_code: reason, error: { error_code } },
        { ok: false, error: { reason_code: reason, error_code } },
      ]) {
        expect(solarFlowRunOutcome(envelope)).toEqual({ ok: false, code: reason })
        expect(solarSizingRunSentence(solarFlowRunOutcome(envelope).code)).toBe(sentences[klass])
      }
    }
  })

  it('G1A-2 freezes exactly 26 exact sentences with no code interpolation', () => {
    expect(Object.isFrozen(SOLAR_SIZING_RUN_REASONS)).toBe(true)
    expect(Object.keys(SOLAR_SIZING_RUN_REASONS)).toHaveLength(26)
    expect(SOLAR_SIZING_RUN_REASONS).toEqual(sentences)
    for (const sentence of Object.values(SOLAR_SIZING_RUN_REASONS)) {
      expect(sentence.length).toBeGreaterThanOrEqual(12)
    expect(sentence.includes('${')).toBe(false)
    expect(sentence).not.toMatch(/failureCode|reason_code|error_code/)
    }
  })

  // The empty, length and trim checks are implied by the ASCII grammar and the exact cases, so no
  // output can tell them apart; they bound the work before toUpperCase. The grammar check is the one
  // that decides: these non-ASCII spellings uppercase onto real server codes.
  it.each([['mıssıng_panel', 'MISSING_PANEL'], ['ſtale_graph_reviſion', 'STALE_GRAPH_REVISION'],
    ['MIßING_PANEL', 'MISSING_PANEL'], ['tımeout', 'TIMEOUT']])(
    'G1A-13 a non-ASCII spelling %s that uppercases onto %s stays unknown', (code, server) => {
      expect(code.toUpperCase()).toBe(server)
      expect(solarSizingRunSentence(server)).not.toBe(sentences.unknown)
      expect(solarSizingRunSentence(code)).toBe(sentences.unknown)
    })

  it.each([null, undefined, 7, false, {}, [], new String('TIMEOUT'), '', 'A'.repeat(65),
    'bad code', 'bad-code', '9_LEAD', '__proto__', 'constructor', 'toString', 'UNLISTED_REASON',
    '<script>hostile</script>', 'TIMEOUT\n'])('G1A-3 rejects unknown or malformed value %s', (code) => {
    expect(solarSizingRunSentence(code)).toBe(sentences.unknown)
  })

  it('G1A-4 distinguishes category-only access from a missing valid cloud grant', () => {
    expect(solarSizingRunSentence('FORBIDDEN')).toBe(sentences.access)
    expect(solarSizingRunSentence('FORBIDDEN')).not.toBe(sentences.grant_missing)
    expect(solarSizingRunSentence('CLOUD_AUTH_MISSING')).toBe(sentences.grant_missing)
    for (const [code, klass] of aliases) {
      expect(solarSizingRunSentence(solarFlowRunOutcome({ ok: false, error: { error_code: code } }).code))
        .toBe(sentences[klass])
    }
    expect(solarSizingRunSentence(solarFlowRunOutcome(null).code)).toBe(sentences.unknown)
  })
})
