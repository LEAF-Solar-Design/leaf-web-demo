import { writeFileSync } from 'node:fs'
import { isAbsolute } from 'node:path'
import { CHECKPOINTS, CHECKPOINT_OWNERS, REQUIRED_TEST_IDS, SIZING_SCENARIOS, SIZING_OWNERS, publicId, validateMetadata } from './projectIdentity.mjs'

// The Python adapter reads this same closed wire contract.
export const BROWSER_REPORT_SCHEMA = 'leaf.project-browser.v1'
export const MAX_RECEIPT_BYTES = 4 * 1024 * 1024
export const EVENT_NAME = 'pp3b.checkpoint'
export const CANONICAL_NAME = 'pp3b.canonical'
export const SIZING_NAME = 'pp3b.sizing'
export const CANONICAL_FIELDS = Object.freeze([
  'organization_id', 'project_id', 'drawing_id', 'input_version_id', 'output_version_id',
  'job_id', 'binding_id', 'worker_id', 'checkout_fence', 'before_settings_sha256',
  'settings_sha256', 'reopened_settings_sha256',
])
const owners = CHECKPOINT_OWNERS
const statuses = new Set(['passed', 'failed', 'timedOut', 'skipped', 'interrupted'])
const annotationTypes = new Set(['skip', 'fixme', 'fail', 'slow', 'issue', 'tag'])
const fixedError = () => ({ message: 'PP3B_TEST_FAILURE', stack: 'PP3B_TEST_FAILURE' })
const completeIds = ids => ids.length === REQUIRED_TEST_IDS.length && REQUIRED_TEST_IDS.every((id, i) => ids[i] === id)

const sizingFields = Object.freeze({
  'capture-recovery': ['scenario', 'organization_id', 'project_id', 'drawing_id', 'input_version_id', 'job_id',
    'output_version_id', 'exchange_job_id', 'evidence_sha256', 'panels_in_sequence'],
  'read-custody': ['scenario', 'organization_id', 'project_id', 'drawing_id', 'source_version_id', 'groups_job_id',
    'input_version_id', 'frame_count', 'tool', 'request_sha256', 'output_sha256'],
  'browser-response-discard': ['scenario', 'organization_id', 'project_id', 'drawing_id', 'input_version_id', 'job_id',
    'output_version_id', 'exchange_job_id', 'evidence_sha256', 'panels_in_sequence', 'submissions', 'same_submission'],
  'service-response-drop': ['scenario', 'organization_id', 'project_id', 'drawing_id', 'input_version_id', 'job_id',
    'status', 'reason_code', 'retryable'],
})

export function sizingRecord(value, scenario, metadata) {
  const refuse = () => { throw new Error('PP3B_SIZING') }
  const fields = typeof scenario === 'string' && Object.hasOwn(sizingFields, scenario) ? sizingFields[scenario] : null
  if (!fields || !value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== fields.length || !fields.every(key => Object.hasOwn(value, key)) ||
      value.scenario !== scenario || value.organization_id !== metadata.organization_id) refuse()
  const record = Object.fromEntries(fields.map(key => [key, value[key]]))
  for (const key of fields) {
    if (key.endsWith('_id') && !publicId(record[key])) refuse()
    if (key.endsWith('_sha256') && (typeof record[key] !== 'string' || !/^[a-f0-9]{64}$/.test(record[key]))) refuse()
    if (['frame_count', 'panels_in_sequence'].includes(key) && (!Number.isSafeInteger(record[key]) || record[key] < 1)) refuse()
  }
  if (Object.hasOwn(record, 'exchange_job_id') &&
      (record.exchange_job_id !== record.job_id || record.input_version_id === record.output_version_id)) refuse()
  if (scenario === 'read-custody' &&
      (record.tool !== 'solar-string-data' || record.source_version_id === record.input_version_id)) refuse()
  if (scenario === 'browser-response-discard' && (record.submissions !== 2 || record.same_submission !== true)) refuse()
  if (scenario === 'service-response-drop' &&
      (record.status !== 'failed' || record.reason_code !== 'SIZING_OUTCOME_UNKNOWN' || record.retryable !== false)) refuse()
  return record
}

export function sizingComplete(records) {
  if (Object.keys(records).length !== SIZING_SCENARIOS.length || !SIZING_SCENARIOS.every(key => Object.hasOwn(records, key))) return false
  const recovery = records['capture-recovery'], custody = records['read-custody']
  const jobs = ['capture-recovery', 'browser-response-discard', 'service-response-drop'].map(key => records[key])
  return new Set(jobs.map(r => r.project_id)).size === 3 && new Set(jobs.map(r => r.job_id)).size === 3 &&
    custody.project_id === recovery.project_id && custody.drawing_id === recovery.drawing_id &&
    custody.source_version_id === recovery.output_version_id
}

export function canonicalProjection(value, metadata) {
  if (!value || Object.keys(value).length !== CANONICAL_FIELDS.length) throw new Error('PP3B_CANONICAL')
  const projected = Object.fromEntries(CANONICAL_FIELDS.map(key => [key, value[key]]))
  for (const key of CANONICAL_FIELDS.slice(0, 8)) if (!publicId(projected[key])) throw new Error('PP3B_CANONICAL')
  for (const key of CANONICAL_FIELDS.slice(9)) if (!/^[a-f0-9]{64}$/.test(projected[key])) throw new Error('PP3B_CANONICAL')
  if (!Number.isSafeInteger(projected.checkout_fence) || projected.checkout_fence < 1 ||
      projected.organization_id !== metadata.organization_id || projected.binding_id !== metadata.binding_id ||
      projected.worker_id !== metadata.worker_id || projected.input_version_id === projected.output_version_id ||
      projected.before_settings_sha256 === projected.settings_sha256 ||
      projected.settings_sha256 !== projected.reopened_settings_sha256) throw new Error('PP3B_CANONICAL')
  return projected
}

export default class ProjectProofReporter {
  constructor({ metadata, outputFile, write = writeFileSync, _mode = 'test' } = {}) {
    this.metadata = validateMetadata(metadata)
    // Playwright's reporter command mode distinguishes discovery from execution.
    // A listing may succeed, but must never publish browser execution evidence.
    this.listOnly = _mode === 'list'
    if (!isAbsolute(outputFile || '')) throw new Error('PP3B_OUTPUT')
    this.outputFile = outputFile
    this.write = write
    this.invalid = false
    this.result = {
      schema: BROWSER_REPORT_SCHEMA,
      run_id: this.metadata.run_id,
      source_binding: this.metadata.source_binding,
      public_key_thumbprint: this.metadata.public_key_thumbprint,
      exit_code: 1, selected_test_ids: [], started_test_ids: [], completed_test_ids: [],
      outcomes: [], annotation_types: [], checkpoints: {}, canonical: null, sizing: {},
    }
  }

  id(test) {
    if (!REQUIRED_TEST_IDS.includes(test?.title)) { this.invalid = true; return 'unknown' }
    return test.title
  }

  record(list, test) {
    const id = this.id(test)
    if (list.length >= REQUIRED_TEST_IDS.length || list.includes(id)) { this.invalid = true; return id }
    list.push(id)
    return id
  }

  annotations(test) {
    if (test?.annotations?.length) {
      this.result.annotation_types = [...new Set([...this.result.annotation_types,
        ...test.annotations.slice(0, 32).map(a => annotationTypes.has(a.type) ? a.type : 'unknown_annotation')])]
      this.invalid = true
    }
  }

  onBegin(config, suite) {
    if (config.shard || config.workers !== 1 || config.forbidOnly !== true ||
        config.projects?.length !== 1 || config.projects[0].name !== 'project' || config.projects[0].retries !== 0) this.invalid = true
    for (const test of suite.allTests()) {
      this.record(this.result.selected_test_ids, test)
      this.annotations(test)
    }
  }

  onTestBegin(test) { this.record(this.result.started_test_ids, test) }

  onTestEnd(test, result) {
    const id = this.record(this.result.completed_test_ids, test)
    this.annotations(test)
    if (this.result.outcomes.length < REQUIRED_TEST_IDS.length) this.result.outcomes.push({ test_id: id,
      status: statuses.has(result.status) ? result.status : 'failed',
      retry: Number.isSafeInteger(result.retry) && result.retry >= 0 ? result.retry : -1 })
    for (const attachment of (result.attachments || []).slice(0, 32)) {
      if (![EVENT_NAME, CANONICAL_NAME, SIZING_NAME].includes(attachment.name)) continue
      try {
        if (attachment.contentType !== 'application/json' || !Buffer.isBuffer(attachment.body) ||
            attachment.body.length > 4096 || attachment.path !== undefined) throw new Error('PP3B_EVENT')
        const event = JSON.parse(attachment.body.toString('utf8'))
        if (attachment.name === EVENT_NAME) {
          if (Object.keys(event).length !== 2 || owners[event.key] !== id || event.value !== true ||
              Object.hasOwn(this.result.checkpoints, event.key)) throw new Error('PP3B_EVENT')
          this.result.checkpoints[event.key] = true
        } else if (attachment.name === SIZING_NAME) {
          const scenario = event?.scenario
          if (!Object.hasOwn(SIZING_OWNERS, scenario) || SIZING_OWNERS[scenario] !== id ||
              Object.hasOwn(this.result.sizing, scenario)) throw new Error('PP3B_SIZING')
          this.result.sizing[scenario] = sizingRecord(event, scenario, this.metadata)
        } else {
          if (id !== REQUIRED_TEST_IDS[2] || this.result.canonical) throw new Error('PP3B_EVENT')
          this.result.canonical = canonicalProjection(event, this.metadata)
        }
      } catch { this.invalid = true }
    }
    if ((result.attachments || []).length > 32) this.invalid = true
    // The list reporter receives this same result object. Never retain raw runner errors.
    // Its quiet mode suppresses live output, but it also buffers chunks under symbols.
    for (const key of Object.getOwnPropertySymbols(result)) delete result[key]
    result.errors = result.errors?.length ? [fixedError()] : []
    if (result.error) result.error = fixedError()
    result.stdout = []
    result.stderr = []
    result.attachments = []
    test.annotations = (test.annotations || []).slice(0, 32).map(a => ({
      type: annotationTypes.has(a.type) ? a.type : 'unknown_annotation', description: 'PP3B_ANNOTATION',
    }))
  }

  onStepEnd(test, result, step) { if (step.error) step.error = fixedError() }
  onStdOut() { /* Discard entire chunks; never reconstruct secret-bearing streams. */ }
  onStdErr() { /* Discard entire chunks, including split secret values. */ }
  onError(error) {
    this.invalid = true
    for (const key of Object.keys(error)) delete error[key]
    Object.assign(error, fixedError())
  }

  onEnd(fullResult) {
    const r = this.result
    if (this.listOnly) return { status: !this.invalid && fullResult.status === 'passed' &&
      completeIds(r.selected_test_ids) ? 'passed' : 'failed' }
    const complete = !this.invalid && fullResult.status === 'passed' &&
      [r.selected_test_ids, r.started_test_ids, r.completed_test_ids].every(completeIds) &&
      r.outcomes.length === REQUIRED_TEST_IDS.length && r.outcomes.every(o => o.status === 'passed' && o.retry === 0) &&
      r.annotation_types.length === 0 && CHECKPOINTS.every(key => r.checkpoints[key] === true) && !!r.canonical && sizingComplete(r.sizing)
    r.exit_code = complete ? 0 : 1
    const serialized = JSON.stringify(r) + '\n'
    if (Buffer.byteLength(serialized) > MAX_RECEIPT_BYTES) throw new Error('PP3B_OUTPUT_LIMIT')
    try { this.write(this.outputFile, serialized, { flag: 'wx', mode: 0o600 }) }
    catch { throw new Error('PP3B_OUTPUT_REFUSED') }
    return { status: complete ? 'passed' : 'failed' }
  }
}
