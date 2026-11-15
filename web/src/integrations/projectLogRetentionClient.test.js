import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createProjectLogRetentionMethods } from './projectLogRetentionClient.js'

const project = '22222222-2222-4222-8222-222222222222'
const id = 'retention-execution:' + 'a'.repeat(64)
const authority = 'retention-execution:' + 'e'.repeat(64)
const table = 'retention-table:' + 'b'.repeat(64)
const review = 'retention-review:' + 'c'.repeat(64)
const digest = 'd'.repeat(64)
const at = '2026-11-14T09:45:00.000Z', deadline = '2026-11-14T09:49:00.000Z'
const settings = { batchSize: 100, maxBatchesPerClass: 1 }
const definition = { kind: 'project_log_retention_definition_v1', external_activation: false, default_mode: 'dry_run', title: 'Logs', settings }
const def = { resourceId: 'retention-definition:logs:v1', revision: 1, definition }
const sched = { resourceId: 'retention-schedule:daily:v1', revision: 1, schedule: {
  kind: 'project_log_retention_schedule_v1', external_activation: false, daily_utc: '09:45',
  definition_id: def.resourceId, definition_revision: 1, prepared_table_id: table } }
const input = { schedule: { resourceId: sched.resourceId, revision: 1 }, definition: { resourceId: def.resourceId, revision: 1 }, preparedTableId: table, dueAt: at }
const audit = { resourceId: id, revision: 1, intent: { schedule: { ...input.schedule, digest }, definition: { ...input.definition, digest },
  prepared: { resourceId: table, revision: 1, digest } }, checkpoint: { phase: 'admitted', batches: 0 }, admittedAt: at, parentDeadlineAt: deadline,
  expired: false, counters: { input: 2, active: 2, archived: 0, deletedBuild: 0, deletedOther: 0, restored: 0, batchesBuild: 0, batchesOther: 0 }, archiveDigest: digest }
const cls = { retentionCutoff: at, archiveCutoff: at, eligible_count: 1, archive_count: 1, batch_count: 1, remaining_eligible_count: 0 }
const plan = { mode: 'dry_run', expired: false, active_count: 2, retained_count: 0, retentionClock: at, settings, build: cls, other: cls }
const receipt = { invocation_id: digest, target_id: id, previous_revision: 1, revision: 2, review_id: review,
  changed_count: 1, archive_count: 1, archive_digest: digest, completed_at: at }
const definitionExport = { schema_version: 1, kind: 'project_resource', operation_ref: { id: 'project-log-retention', version: 1,
  digest: '1fad76246c0dd99fd5ada1a384a37bc87fc12dd6e5aa4b09c60216e3e3108d30' }, definition: { id: 'logs', version: 1, payload: definition } }
const prepare = { datasetRef: { id: 'logs', version: 1, digest }, expectedDatasetRevision: 1, tableId: 'source-logs',
  fields: { id: 'id', source: 'source', level: 'level', timestamp: 'timestamp' }, retentionClock: at }
const page = { resourceId: id, revision: 1, view: 'current', total: 1, items: [{ id: 'original', text: 'retained payload', nested: { ok: true } }], nextCursor: null }
const invalid = () => Object.assign(new Error('Invalid integration input'), { code: 'INTEGRATION_INVALID_INPUT' })
const request = vi.fn()
const client = createProjectLogRetentionMethods(request, invalid)
let signal
beforeEach(() => { request.mockReset(); signal = new AbortController().signal })
const cases = [
  { name: 'retentionPrepare', args: () => [prepare, { signal }], path: 'prepare', body: prepare, result: { tableId: table, revision: 1 } },
  { name: 'retentionRegisterDefinition', args: () => [{ id: 'logs', version: 1, title: 'Logs', settings }, { signal }], path: 'definitions', body: { id: 'logs', version: 1, title: 'Logs', settings }, result: def },
  { name: 'retentionGetDefinition', args: () => ['logs', 1, { signal }], path: 'definitions/logs/versions/1', result: def },
  { name: 'retentionExportDefinition', args: () => ['logs', 1, { signal }], path: 'definitions/logs/versions/1/export', result: definitionExport },
  { name: 'retentionRegisterSchedule', args: () => [{ id: 'daily', version: 1, definition: input.definition, preparedTableId: table }, { signal }], path: 'schedules', body: { id: 'daily', version: 1, definition: input.definition, preparedTableId: table }, result: sched },
  { name: 'retentionGetSchedule', args: () => ['daily', 1, { signal }], path: 'schedules/daily/versions/1', result: sched },
  { name: 'retentionAdmit', args: () => [input, { signal }], path: 'occurrences', body: input, result: audit },
  { name: 'retentionAudit', args: () => [id, { signal }], path: 'executions/' + id, result: audit },
  { name: 'retentionRows', args: () => [id, { signal, view: 'current' }], path: 'executions/' + id + '/rows?view=current&limit=25', result: page },
  { name: 'retentionPlan', args: () => [id, {}, { signal }], path: 'executions/' + id + '/plan', body: {}, result: plan },
  { name: 'retentionReview', args: () => [id, { mode: 'apply' }, { signal }], path: 'executions/' + id + '/review', body: { mode: 'apply' }, result: { reviewId: review, mode: 'apply', archive_count: 1, archive_digest: digest } },
  { name: 'retentionApply', args: () => [id, { reviewId: review, class: 'build' }, { signal }], path: 'executions/' + id + '/apply', body: { reviewId: review, class: 'build' }, result: { changed_count: 1, revision: 2, stop_reason: 'paused' } },
  { name: 'retentionRecover', args: () => [id, { signal }], path: 'executions/' + id + '/recover', body: {}, result: { changed_count: 0, revision: 2, stop_reason: 'paused' } },
  { name: 'retentionAdmitRestore', args: () => [id, { reviewId: review, invocationId: digest }, { signal }], path: 'executions/' + id + '/restore-admissions', body: { reviewId: review, invocationId: digest }, result: { authorityId: authority, revision: 1, targetId: id, invocationId: digest, admittedAt: at, parentDeadlineAt: deadline } },
  { name: 'retentionRestore', args: () => [id, { authorityId: authority }, { signal }], path: 'executions/' + id + '/restore', body: { authorityId: authority }, result: receipt },
]
describe('O03 finite retention transport through existing request authority', () => {
  it('requires trusted request and invalid functions', () => {
    expect(() => createProjectLogRetentionMethods(null, invalid)).toThrow(TypeError)
    expect(() => createProjectLogRetentionMethods(request, null)).toThrow(TypeError)
    expect(Object.keys(client).sort()).toEqual(cases.map(value => value.name).sort())
  })
  it.each(cases)('$name uses exact fixed route and bounded request', async ({ name, args, path, body, result }) => {
    request.mockResolvedValue(result)
    expect(await client[name](project, ...args())).toEqual(result)
    expect(request).toHaveBeenCalledExactlyOnceWith(project, '/integrations/retention/' + path,
      body === undefined ? { signal } : { signal, method: 'POST', body, bodyLimit: 65536 })
    expect(request.mock.calls[0][1]).not.toContain('%3A')
  })
  it.each(cases)('$name requires an active AbortSignal before dispatch', async ({ name, args }) => {
    const absent = args(); absent[absent.length - 1] = {}
    await expect(async () => client[name](project, ...absent)).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' })
    const controller = new AbortController(); controller.abort(); const aborted = args(); aborted[aborted.length - 1] = { ...aborted.at(-1), signal: controller.signal }
    await expect(async () => client[name](project, ...aborted)).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' }); expect(request).not.toHaveBeenCalled()
  })
  it.each(['../project', project + '\n', project + '?scope=other', '', undefined])('rejects invalid project %s', async value => {
    await expect(client.retentionAudit(value, id, { signal })).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' }); expect(request).not.toHaveBeenCalled()
  })
  it.each([id + '\n', encodeURIComponent(id), id + '/recover', 'retention-execution:' + 'A'.repeat(64)])('rejects nonliteral execution identity %s', async value => {
    await expect(async () => client.retentionAudit(project, value, { signal })).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' }); expect(request).not.toHaveBeenCalled()
  })
  it.each([
    { name: 'clock', method: 'retentionAdmit', args: () => [{ ...input, admittedAt: at }, { signal }] },
    { name: 'token', method: 'retentionApply', args: () => [id, { reviewId: review, class: 'build', token: digest }, { signal }] },
    { name: 'scope', method: 'retentionReview', args: () => [id, { mode: 'apply', projectId: project }, { signal }] },
    { name: 'activation', method: 'retentionRegisterDefinition', args: () => [{ id: 'logs', version: 1, title: 'Logs', external_activation: true }, { signal }] },
    { name: 'wrong due', method: 'retentionAdmit', args: () => [{ ...input, dueAt: '2026-11-14T10:45:00.000Z' }, { signal }] },
    { name: 'duplicate fields', method: 'retentionPrepare', args: () => [{ ...prepare, fields: { ...prepare.fields, level: 'id' } }, { signal }] },
    { name: 'context scope', method: 'retentionAudit', args: () => [id, { signal, tenantId: project }] },
  ])('rejects caller $name without dispatch', async ({ method, args }) => {
    await expect(async () => client[method](project, ...args())).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' }); expect(request).not.toHaveBeenCalled()
  })
  it.each([{ view: 'current', limit: 101 }, { view: 'current', limit: 0 }, { view: 'current', after: 'x'.repeat(1025) }, { view: 'other' }, {}])('rejects page options %j', async options => {
    await expect(async () => client.retentionRows(project, id, { signal, ...options })).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT' }); expect(request).not.toHaveBeenCalled()
  })
  it('encodes cursor query only and preserves original nested row JSON', async () => {
    request.mockResolvedValue({ ...page, view: 'archive' })
    const result = await client.retentionRows(project, id, { signal, view: 'archive', limit: 1, after: 'a+b/=' })
    expect(result.items).toEqual(page.items)
    expect(request.mock.calls[0][1]).toBe('/integrations/retention/executions/' + id + '/rows?view=archive&limit=1&after=a%2Bb%2F%3D')
  })
  it.each([
    { name: 'wrong audit id', method: 'retentionAudit', args: () => [id, { signal }], result: { ...audit, resourceId: authority }, dispatched: false },
    { name: 'audit token', method: 'retentionAudit', args: () => [id, { signal }], result: { ...audit, token_hash: digest }, dispatched: false },
    { name: 'wrong definition version', method: 'retentionGetDefinition', args: () => ['logs', 1, { signal }], result: { ...def, resourceId: 'retention-definition:logs:v2' }, dispatched: false },
    { name: 'wrong page view', method: 'retentionRows', args: () => [id, { signal, view: 'current' }], result: { ...page, view: 'archive' }, dispatched: false },
    { name: 'wrong target', method: 'retentionRestore', args: () => [id, { authorityId: authority }, { signal }], result: { ...receipt, target_id: authority }, dispatched: true },
    { name: 'wrong receipt revision', method: 'retentionRestore', args: () => [id, { authorityId: authority }, { signal }], result: { ...receipt, revision: 4 }, dispatched: true },
    { name: 'mutation token', method: 'retentionApply', args: () => [id, { reviewId: review, class: 'other' }, { signal }], result: { changed_count: 1, revision: 2, stop_reason: 'paused', token_hash: digest }, dispatched: true },
    { name: 'wrong admission binding', method: 'retentionAdmit', args: () => [input, { signal }], result: { ...audit, intent: { ...audit.intent, prepared: { ...audit.intent.prepared, resourceId: 'retention-table:' + 'f'.repeat(64) } } }, dispatched: true },
  ])('rejects $name and records dispatch state', async ({ method, args, result, dispatched }) => {
    request.mockResolvedValue(result); await expect(client[method](project, ...args())).rejects.toMatchObject({ code: 'INTEGRATION_INVALID_INPUT', dispatched }); expect(request).toHaveBeenCalledTimes(1)
  })
  it.each([
    { name: 'unsafe key', row: JSON.parse('{"__proto__":{"polluted":true}}') },
    { name: 'nonfinite number', row: { id: NaN } }, { name: 'undefined', row: { id: undefined } },
    { name: 'class instance', row: { id: new Date() } }, { name: 'oversize', row: { id: 'x'.repeat(262144) } },
  ])('rejects page $name', async ({ row }) => {
    request.mockResolvedValue({ ...page, items: [row] }); await expect(client.retentionRows(project, id, { signal, view: 'current' })).rejects.toMatchObject({ dispatched: false })
  })
  it('preserves transport errors without marking or retrying', async () => {
    const error = Object.assign(new Error('transport unavailable'), { dispatched: 'uncertain' }); request.mockRejectedValue(error)
    await expect(client.retentionRecover(project, id, { signal })).rejects.toBe(error); expect(error.dispatched).toBe('uncertain'); expect(request).toHaveBeenCalledTimes(1)
  })
})
