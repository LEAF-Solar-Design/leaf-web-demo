import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { scanSource } from '../../scripts/check_user_facing_copy.mjs'
import { SOLAR_REFUSAL_REASONS } from '../lib/ribbonClusters.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'
import { PROJECT_SOLAR_RUN_REASONS, projectSolarRunFailure, readProjectSolarCompletion,
  readProjectSolarWorkerRefusal, projectSolarWorkerRefusal } from './solarProjectCompletion.js'
import { humanizeError } from '../errorHumanize.js'
import workerUnavailable from './__fixtures__/workerUnavailable.json'

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const J = '55555555-5555-5555-5555-555555555555'
const W = 'abcdefab-6666-0000-0000-666666666666'
const X = '77777777-7777-7777-7777-777777777777'
const T = 'solar-settings'
const B = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const job = (changes = {}) => ({ job_id: J, org_id: O, project_id: P, input_version_id: V,
  tool_name: T, execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: D },
  status: 'complete', output_version_id: W, error: null,
  result: { output_version_id: W, graph_commit: { output_version_id: W } }, ...changes })
const failed = (changes = {}) => job({ status: 'failed', result: null, output_version_id: null,
  error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'EQUIPMENT_ASSIGNMENT_REQUIRED', retryable: false }, ...changes })
const read = (value, changes = {}) => readProjectSolarCompletion({ job: value, binding: B, jobId: J, tool: T, ...changes })
const success = { state: 'succeeded', jobId: J, outputVersionId: W, reasonCode: null, message: null }
const unreadable = { state: 'unreadable', jobId: J, outputVersionId: null, reasonCode: null,
  message: PROJECT_SOLAR_RUN_REASONS.unreadable_output }
const fallback = { state: 'failed', jobId: J, outputVersionId: null, reasonCode: null,
  message: 'The server refused this solar tool' }

describe('exact project Solar completion', () => {
  it('G17 fixed copy names the submitted tool and survives humanization', () => {
    const graphLabels = {
      'solar-panels-from-drawing': 'Import panels from drawing', 'solar-panel-groups': 'Panel groups',
      'solar-homeruns': 'Homeruns', 'solar-feeders': 'Feeders', 'solar-schedule': 'Schedule',
    }
    const otherLabels = {
      'solar-settings': 'Solar settings', 'solar-size-strings': 'String sizing', 'solar-combiners': 'Combiners',
      'solar-string-add': 'Add string', 'solar-assign-equipment': 'Assign equipment',
      'solar-string-multi-add': 'Add multiple strings', 'solar-string-conductors': 'String conductors',
      'solar-correct-string': 'Correct string', 'solar-solve-proposal': 'Solve proposal',
      'solar-commit-solve': 'Apply solve proposal',
    }
    for (const [tool, label] of Object.entries({ ...graphLabels, ...otherLabels })) {
      const message = `No worker was available to start ${label}. Try again later${Object.hasOwn(graphLabels, tool)
        ? ': Refresh preview, then Review & run.' : '.'}`
      expect(projectSolarWorkerRefusal(tool)).toEqual({ reasonCode: 'sip_r4_worker_unavailable', message })
      expect(message.length).toBeLessThanOrEqual(160)
      expect(humanizeError(message)).toBe(message)
      expect(scanSource('const text = ' + JSON.stringify(message), 'copy.js', { strict: true })).toEqual([])
    }
    for (const tool of ['private-tool-sentinel', '__proto__', 'constructor', null, { toString() { throw new Error() } }]) {
      expect(projectSolarWorkerRefusal(tool)).toEqual(projectSolarRunFailure(null))
    }
    const source = readFileSync(resolve(process.cwd(), 'src/solar/solarProjectCompletion.js'), 'utf8')
    expect(scanSource(source, 'src/solar/solarProjectCompletion.js', { strict: true })).toEqual([])
  })
  it('G17 only an unambiguous initial worker 503 is definite', () => {
    const reason = 'SIP_R4_WORKER_UNAVAILABLE'
    const wire = () => ({ ok: false, status: 503, reason, envelope: workerUnavailable })
    const local = () => ({ ok: false, status: 503, reason: reason.toLowerCase(), envelope: null })
    expect(readProjectSolarWorkerRefusal(wire())).toBe(reason.toLowerCase())
    expect(readProjectSolarWorkerRefusal(local())).toBe(reason.toLowerCase())
    const invalid = [
      ...[500, 502, 409, 202, '503', undefined, null, NaN, 503.1].map(status => ({ ...wire(), status })),
      ...['SIP_R1_STORE_UNAVAILABLE', reason + '_EXTRA', '', null, 503].map(value => ({ ...wire(), reason: value })),
      { ok: false, status: 503, envelope: null }, { ...wire(), ok: true },
      { ...wire(), envelope: { ...workerUnavailable, ok: true } },
      { ...wire(), envelope: { ...workerUnavailable, reason_code: 'SIP_R1_STORE_UNAVAILABLE' } },
      { ...wire(), error: { reason_code: 'OTHER' } },
      { ...wire(), envelope: { ...workerUnavailable, status: 'submitted' } },
      { ...wire(), state: 'submitted' }, { ...wire(), status_code: 202 },
      ...['job_id', 'jobId', 'output_version_id', 'outputVersionId', 'data', 'result', 'job'].flatMap(key => [
        { ...wire(), [key]: key === 'data' ? { status: 'submitted', job_id: J } : {} },
        { ...wire(), envelope: { ...workerUnavailable, [key]: J } },
      ]),
    ]
    for (const value of invalid) expect(readProjectSolarWorkerRefusal(value)).toBeNull()
    let reads = 0
    const getter = () => { reads += 1; return reason }
    for (const key of ['ok', 'status', 'status_code', 'state', 'reason', 'envelope', 'data', 'result', 'job_id', 'outputVersionId']) {
      expect(readProjectSolarWorkerRefusal(Object.defineProperty(wire(), key, { get: getter }))).toBeNull()
    }
    expect(readProjectSolarWorkerRefusal({ ...wire(), envelope: { ...workerUnavailable,
      error: Object.defineProperty({}, 'reason_code', { get: getter }) } })).toBeNull()
    expect(readProjectSolarWorkerRefusal({ ...wire(), envelope: Object.defineProperty({ ...workerUnavailable },
      'error', { get: getter }) })).toBeNull()
    expect(reads).toBe(0)
    // Unrelated free-text accessors are never read or used to format this refusal.
    expect(readProjectSolarWorkerRefusal({ ...wire(), envelope: { ...workerUnavailable,
      error: Object.defineProperty({ reason_code: reason }, 'message', { get: getter }) } })).toBe(reason.toLowerCase())
    expect(reads).toBe(0)
    expect(projectSolarRunFailure(reason)).toEqual(projectSolarRunFailure(null))
    expect(read(failed({ error: { error_code: 'INTERNAL', reason_code: reason, retryable: true } }))).toEqual(fallback)
  })
  it('W1C-34c maps cold voltage failures and tolerates their arithmetic detail', () => {
    expect(projectSolarRunFailure('COLD_VOLTAGE_FAILED')).toEqual({ reasonCode: 'cold_voltage_failed',
      message: SOLAR_SIZING_RUN_REASONS.cold_voltage })
    expect(read(failed({ error: { error_code: 'BAD_PARAMS', reason_code: 'COLD_VOLTAGE_FAILED', retryable: false,
      cold_voltage: { standard_string_length: 32, suggested_string_length: 31,
        per_module: 48.3, string_voltage: 1545.6, max_dc_voltage: 1500 } } }))).toEqual({
      state: 'failed', jobId: J, outputVersionId: null, reasonCode: 'cold_voltage_failed',
      message: SOLAR_SIZING_RUN_REASONS.cold_voltage,
    })
  })
  it('W1C5 recognizes a scoped nonpublishing proposal and freezes only the review projection', () => {
    const result = { schema: 'leaf.solar-solve-proposal.v1', candidate_sha256: 'a'.repeat(64),
      frame_id: 'frame-1', phase: 'initial', candidate_summary: [{ panel_count: 12, endpoints: ['panel-1', 'panel-12'] }],
      request_sha256: 'b'.repeat(64), response_sha256: 'c'.repeat(64),
      execution_provenance: { schema: 'leaf.solar-solve-proposal.v1' }, private_payload: 'not retained' }
    const proposal = () => read(job({ tool_name: 'solar-solve-proposal', result, output_version_id: null }), { tool: 'solar-solve-proposal' })
    expect(proposal()).toEqual({ state: 'proposal', jobId: J, outputVersionId: null, reasonCode: null, message: null,
      candidateSha256: 'a'.repeat(64), frameId: 'frame-1', phase: 'initial', summary: result.candidate_summary })
    expect(Object.isFrozen(proposal().summary[0].endpoints)).toBe(true)
    expect(JSON.stringify(proposal())).not.toContain('not retained')
    for (const key of ['schema', 'candidate_sha256', 'frame_id', 'phase', 'candidate_summary',
      'request_sha256', 'response_sha256', 'execution_provenance']) {
      const saved = result[key]; delete result[key]
      expect(proposal()).toEqual(unreadable)
      result[key] = saved
    }
    result.candidate_summary[0].panel_count = 0
    expect(proposal()).toEqual(unreadable)
    result.candidate_summary[0].panel_count = 12
    expect(read(job({ result, output_version_id: null }))).toEqual(unreadable)
    expect(read(job({ tool_name: 'solar-solve-proposal', result, output_version_id: W }), { tool: 'solar-solve-proposal' })).toEqual(unreadable)
    expect(read(job({ tool_name: 'solar-solve-proposal', org_id: X, result, output_version_id: null }), { tool: 'solar-solve-proposal' })).toEqual(unreadable)
  })
  it('W1C5 preserves commit refusal codes and keeps uncertain solve results locked', () => {
    for (const code of ['SOLVE_PROPOSAL_STALE', 'SOLVE_PROPOSAL_NOT_FOUND', 'SOLVE_PROPOSAL_NOT_SUCCEEDED',
      'SOLVE_PROPOSAL_MANIFEST_MISMATCH', 'SOLVE_PROPOSAL_EVIDENCE_INVALID']) {
      expect(projectSolarRunFailure(code)).toMatchObject({ reasonCode: code.toLowerCase() })
    }
    expect(read(failed({ tool_name: 'solar-solve-proposal',
      error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'SOLVE_OUTCOME_UNKNOWN', retryable: false } }),
    { tool: 'solar-solve-proposal' })).toMatchObject({ state: 'outcome_unknown', jobId: J })
  })
  it('R14D-08 test_cloud_completion_preserves_envelope_guards', () => {
    const cases = [
      ['CLOUD_AUTH_MISSING', SOLAR_SIZING_RUN_REASONS.grant_missing],
      ['CLOUD_TENANT_UNAUTHORIZED', SOLAR_SIZING_RUN_REASONS.grant_scope],
      ['CLOUD_UPSTREAM_FAILURE', SOLAR_SIZING_RUN_REASONS.cloud_unavailable],
      ['CLOUD_RESPONSE_INVALID', SOLAR_SIZING_RUN_REASONS.cloud_response_invalid],
      ['CLOUD_REQUEST_INVALID', SOLAR_SIZING_RUN_REASONS.request_invalid],
      ['SIZING_OUTCOME_UNKNOWN', PROJECT_SOLAR_RUN_REASONS.outcome_unknown],
    ]
    for (const [code, sentence] of cases) {
      for (const reason of [code, code.toLowerCase()]) {
        const error = { error_code: 'GRAPH_JOB_FAILED', reason_code: reason, retryable: false,
          message: 'hostile server prose' }
        const value = failed({ error })
        const expected = { state: code === 'SIZING_OUTCOME_UNKNOWN' ? 'outcome_unknown' : 'failed',
          jobId: J, outputVersionId: null, reasonCode: code === 'SIZING_OUTCOME_UNKNOWN' ? null : code.toLowerCase(), message: sentence }
        expect(read(value)).toEqual(expected)
        expect(read(failed({ status: 'cancelled', error }))).toEqual(expected)
        expect(read(failed({ error: { error_code: reason, retryable: false } }))).toEqual(expected)
        if (code !== 'SIZING_OUTCOME_UNKNOWN') {
          expect(projectSolarRunFailure(reason)).toEqual({ reasonCode: code.toLowerCase(), message: sentence })
        }
        for (const changes of [{ job_id: X }, { org_id: X }, { project_id: X }, { input_version_id: X },
          { tool_name: 'solar-other' }, { execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: X } },
          { output_version_id: W }, { result: {} }, { error: { ...error, retryable: 'false' } },
          { error: { ...error, error_code: null } }, { error: { ...error, reason_code: 'unsafe.code' } }]) {
          expect(read({ ...value, ...changes })).toEqual(unreadable)
        }
        let getters = 0
        for (const key of ['error_code', 'reason_code', 'retryable']) {
          const accessor = { ...error }
          Object.defineProperty(accessor, key, { get() { getters += 1; return reason }, enumerable: true })
          expect(read(failed({ error: accessor }))).toEqual(unreadable)
        }
        const accessorJob = { ...value }
        Object.defineProperty(accessorJob, 'error', { get() { getters += 1; return error }, enumerable: true })
        expect(read(accessorJob)).toEqual(unreadable)
        expect(getters).toBe(0)
      }
    }
    expect(read(failed()).state).toBe('failed')
    expect(read(failed({ error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'CLOUD_SOMETHING_ELSE', retryable: false } }))).toEqual(fallback)
  })
  it('R13B-01 selects the exact successful output', () => {
    expect(read(job())).toEqual(success)
  })
  it('R13B-02 refuses success without result output', () => {
    expect(read(job({ result: { graph_commit: { output_version_id: W } } }))).toEqual(unreadable)
  })
  it('R13B-03 refuses head-shaped output values', () => {
    for (const output of ['head', 'HEAD', 'head:' + W, '/versions/' + W, { version_id: W }]) {
      expect(read(job({ result: { output_version_id: output } }))).toEqual(unreadable)
    }
  })
  it('R13B-45 refuses head in every output position at once', () => {
    for (const value of ['head', 'HEAD', 'Head']) {
      expect(read(job({ output_version_id: value,
        result: { output_version_id: value, graph_commit: { output_version_id: value } } }))).toEqual(unreadable)
    }
  })
  it('R13B-04 enforces canonical UUID formatting', () => {
    for (const output of [W.toUpperCase(), ' ' + W + ' ', W.replaceAll('-', ''), 1, null]) {
      expect(read(job({ result: { output_version_id: output } }))).toEqual(unreadable)
    }
    expect(read(job())).toEqual(success)
    expect(read(job(), { jobId: ' ' + J })).toEqual({ ...unreadable, jobId: null })
  })
  it('R13B-05 refuses conflicting output identities', () => {
    for (const value of [job({ output_version_id: X }), job({ result: { output_version_id: W,
      graph_commit: { output_version_id: X } } }), job({ output_version_id: null }),
    job({ output_version_id: undefined }), job({ result: { output_version_id: W, graph_commit: null } })]) {
      expect(read(value)).toEqual(unreadable)
    }
  })
  it('R13B-06 ignores unrelated version-like fields', () => {
    const value = job({ head_version_id: X, version_id: X, graph_revision: 900 })
    Object.defineProperty(value, 'unrelated', { get() { throw new Error('Unrelated field evaluated') } })
    expect(read(value)).toEqual(success)
    const minimal = job({ result: { output_version_id: W } })
    delete minimal.output_version_id
    delete minimal.error
    expect(read(minimal)).toEqual(success)
  })
  it('R13B-07 maps a known terminal refusal', () => {
    expect(read(failed())).toEqual({ state: 'failed', jobId: J, outputVersionId: null,
      reasonCode: 'equipment_assignment_required', message: 'Assign inverter equipment first' })
  })
  it('R13B-08 maps an unknown code to fixed copy', () => {
    expect(read(failed({ error: { error_code: 'GRAPH_JOB_FAILED', reason_code: 'FUTURE_SERVER_CODE', retryable: true } }))).toEqual(fallback)
  })
  it('R13B-09 accepts the server failure without reason code', () => {
    expect(read(failed({ error: { error_code: 'ATTEMPTS_EXHAUSTED', retryable: false } }))).toEqual(fallback)
  })
  it('R13B-10 refuses malformed terminal envelopes', () => {
    const missingResult = job(); delete missingResult.result
    const missingError = failed(); delete missingError.error
    const inherited = Object.create(job())
    for (const value of [null, [], missingResult, job({ result: 'result' }), missingError,
      failed({ error: { error_code: 1, retryable: false } }), failed({ error: { error_code: 'ERROR' } }),
      job({ error: { error_code: 'ERROR' } }), failed({ output_version_id: W }), failed({ result: {} }),
      failed({ error: { error_code: 'ERROR', reason_code: undefined, retryable: false } }), inherited]) {
      expect(read(value)).toEqual(unreadable)
    }
  })
  it('R13B-11 validates every job association field', () => {
    for (const changes of [{ job_id: X }, { org_id: X }, { project_id: X }, { input_version_id: X },
      { tool_name: 'solar-other' }, { execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: X } },
      { execution_context: { schema: 'different', drawing_id: D } }]) {
      expect(read(job(changes))).toEqual(unreadable)
    }
    expect(read(job(), { binding: { ...B, drawing_id: 'head' } })).toEqual(unreadable)
  })
  it('R13B-12 keeps retries pending', () => {
    for (const status of ['submitted', 'running']) {
      expect(read(failed({ status }))).toEqual({ state: 'pending', jobId: J,
        outputVersionId: null, reasonCode: null, message: null })
    }
  })
  it('R13B-13 recognizes route statuses only', () => {
    expect(read(failed({ status: 'cancelled' })).state).toBe('failed')
    for (const status of ['succeeded', 'completed', 'error', 'canceled', 'future']) {
      expect(read(job({ status }))).toEqual(unreadable)
    }
  })
  it('R13B-14 never evaluates accessors or exposes exceptions', () => {
    let getters = 0
    const result = {}
    Object.defineProperty(result, 'output_version_id', { get() { getters += 1; throw new Error('secret exception') } })
    expect(read(job({ result }))).toEqual(unreadable)
    expect(getters).toBe(0)
    const proxy = new Proxy({}, { getPrototypeOf() { throw new Error('secret exception') } })
    expect(read(proxy)).toEqual(unreadable)
    const consumed = job()
    Object.defineProperty(consumed, 'job_id', { get() { getters += 1; return J } })
    expect(read(consumed)).toEqual(unreadable)
    expect(getters).toBe(0)
    expect(JSON.stringify(read(proxy))).not.toContain('secret exception')
    expect(readProjectSolarCompletion(proxy)).toEqual({ ...unreadable, jobId: null })
    const options = { binding: B, jobId: J, tool: T }
    Object.defineProperty(options, 'job', { get() { getters += 1; return job() } })
    expect(readProjectSolarCompletion(options)).toEqual(unreadable)
    expect(getters).toBe(0)
  })
  it('R13B-15 discards raw server text', () => {
    const sentinel = 'private-server-sentinel'
    const value = failed({ progress: sentinel, provenance: { text: sentinel }, unrelated: sentinel })
    value.error.message = sentinel
    const result = read(value)
    expect(result.message).toBe('Assign inverter equipment first')
    expect(JSON.stringify(result)).not.toContain(sentinel)
    Object.defineProperty(value.error, 'message', { get() { throw new Error(sentinel) } })
    expect(read(value)).toEqual(result)
  })
  it('R13B-16 covers the frozen existing refusal vocabulary', () => {
    const excluded = ['capability_availability_unavailable', 'capability_not_ready', 'unlisted']
    for (const [key, message] of Object.entries(SOLAR_REFUSAL_REASONS)) {
      for (const code of [key, key.toUpperCase()]) {
        expect(projectSolarRunFailure(code)).toEqual(excluded.includes(key)
          ? { reasonCode: null, message: SOLAR_REFUSAL_REASONS.unlisted } : { reasonCode: key, message })
      }
    }
    for (const code of [null, 4, '', 'bad code', 'A'.repeat(129), { code: 'ERROR' }]) {
      expect(projectSolarRunFailure(code)).toEqual({ reasonCode: null, message: SOLAR_REFUSAL_REASONS.unlisted })
    }
  })
  it('R13B-40 freezes copy and public outcome fields', () => {
    expect(Object.isFrozen(PROJECT_SOLAR_RUN_REASONS)).toBe(true)
    for (const sentence of Object.values(PROJECT_SOLAR_RUN_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(scanSource('const text = ' + JSON.stringify(sentence), 'copy.js', { strict: true })).toEqual([])
    }
    const keys = ['jobId', 'message', 'outputVersionId', 'reasonCode', 'state']
    for (const result of [read(job()), read(failed()), read(null), read(job({ status: 'submitted' }))]) {
      expect(Object.keys(result).sort()).toEqual(keys)
      expect(Object.isFrozen(result)).toBe(true)
    }
    expect(Object.keys(projectSolarRunFailure('ERROR')).sort()).toEqual(['message', 'reasonCode'])
    expect(Object.isFrozen(projectSolarRunFailure('ERROR'))).toBe(true)
    const source = readFileSync(resolve(process.cwd(), 'src/solar/solarProjectCompletion.js'), 'utf8')
    expect(source).toContain('switch (key)')
    expect(source).not.toContain('SOLAR_REFUSAL_REASONS[')
    expect(source).not.toContain('solarRailReason')
    expect(scanSource(source, 'src/solar/solarProjectCompletion.js', { strict: true })).toEqual([])
  })
})
