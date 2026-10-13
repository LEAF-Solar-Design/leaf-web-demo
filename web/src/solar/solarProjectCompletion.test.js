import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { scanSource } from '../../scripts/check_user_facing_copy.mjs'
import { SOLAR_REFUSAL_REASONS } from '../lib/ribbonClusters.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'
import { PROJECT_SOLAR_RUN_REASONS, projectSolarRunFailure, readProjectSolarCompletion } from './solarProjectCompletion.js'

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
