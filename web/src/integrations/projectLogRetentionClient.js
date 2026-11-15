import { z } from 'zod'
import { IntegrationRefV1 } from '../../../vendor/assistant-contract/src/integration'

const uuid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/i)
const slug = z.string().min(1).max(64).regex(/^[a-z0-9]+(?:-[a-z0-9]+)*(?![\s\S])/)
const revision = z.number().int().min(1).max(2147483647)
const count = z.number().int().min(0).max(2147483647)
const digest = z.string().regex(/^[a-f0-9]{64}(?![\s\S])/)
const eid = z.string().regex(/^retention-execution:[a-f0-9]{64}(?![\s\S])/)
const table = z.string().regex(/^retention-table:[a-f0-9]{64}(?![\s\S])/)
const review = z.string().regex(/^retention-review:[a-f0-9]{64}(?![\s\S])/)
const identity = z.string().min(1).max(128).regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*(?![\s\S])/)
const definitionId = z.string().max(128).regex(/^retention-definition:[a-z0-9]+(?:-[a-z0-9]+)*:v[1-9][0-9]*(?![\s\S])/)
const scheduleId = z.string().max(128).regex(/^retention-schedule:[a-z0-9]+(?:-[a-z0-9]+)*:v[1-9][0-9]*(?![\s\S])/)
const utc = z.string().max(24).regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z(?![\s\S])/)
  .refine(value => Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value)
const options = z.strictObject({ batchSize: z.number().int().safe().optional(), maxBatchesPerClass: z.number().int().safe().optional() })
const settings = z.strictObject({ batchSize: z.number().int().min(100).max(20000), maxBatchesPerClass: z.number().int().min(1).max(2000) })
const field = z.string().min(1).max(128).regex(/^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])/).refine(value => !['__proto__', 'prototype', 'constructor'].includes(value))
const fields = z.strictObject({ id: field, source: field, level: field, timestamp: field }).refine(value => new Set(Object.values(value)).size === 4)
const prepareInput = z.strictObject({ datasetRef: IntegrationRefV1, expectedDatasetRevision: revision, tableId: identity, fields, retentionClock: utc })
const definitionInput = z.strictObject({ id: slug, version: revision, title: z.string().trim().min(1).max(256), settings: options.optional() })
const definitionRef = z.strictObject({ resourceId: definitionId, revision })
const scheduleInput = z.strictObject({ id: slug, version: revision, definition: definitionRef, preparedTableId: table })
const admitInput = z.strictObject({ schedule: z.strictObject({ resourceId: scheduleId, revision }), definition: definitionRef,
  preparedTableId: table, dueAt: utc.refine(value => value.endsWith('T09:45:00.000Z')) })
const definition = z.strictObject({ kind: z.literal('project_log_retention_definition_v1'), external_activation: z.literal(false),
  default_mode: z.literal('dry_run'), title: z.string().min(1).max(256).optional(), settings: settings.optional() })
const definitionResult = z.strictObject({ resourceId: definitionId, revision: z.literal(1), definition })
const schedule = z.strictObject({ kind: z.literal('project_log_retention_schedule_v1'), external_activation: z.literal(false), daily_utc: z.literal('09:45'),
  definition_id: definitionId, definition_revision: revision, prepared_table_id: table })
const scheduleResult = z.strictObject({ resourceId: scheduleId, revision: z.literal(1), schedule })
const exported = z.strictObject({ schema_version: z.literal(1), kind: z.literal('project_resource'),
  operation_ref: z.strictObject({ id: z.literal('project-log-retention'), version: z.literal(1), digest: z.literal('1fad76246c0dd99fd5ada1a384a37bc87fc12dd6e5aa4b09c60216e3e3108d30') }),
  definition: z.strictObject({ id: slug, version: revision, payload: definition }) })
const batch = z.strictObject({ id: digest, mode: z.enum(['apply', 'restore']), class: z.enum(['build', 'other', 'restore']),
  source_digest: digest, selected_digest: digest, count: count.refine(value => value > 0), review_id: review })
const checkpoint = z.strictObject({ phase: z.enum(['admitted', 'dispatched', 'complete']), batches: count,
  stop_reason: z.enum(['complete', 'max_batches', 'deadline', 'conflict', 'paused']).optional(), last_batch_id: digest.optional(), batch: batch.optional() })
  .refine(value => (value.phase === 'dispatched') === (value.batch !== undefined))
const boundRef = schema => z.strictObject({ resourceId: schema, revision, digest })
const audit = z.strictObject({ resourceId: eid, revision,
  intent: z.strictObject({ schedule: boundRef(scheduleId), definition: boundRef(definitionId), prepared: boundRef(table) }),
  checkpoint, admittedAt: utc, parentDeadlineAt: utc, expired: z.boolean(),
  counters: z.strictObject({ input: count, active: count, archived: count, deletedBuild: count, deletedOther: count, restored: count, batchesBuild: count, batchesOther: count }), archiveDigest: digest })
  .refine(value => Date.parse(value.parentDeadlineAt) - Date.parse(value.admittedAt) === 240000)
const page = z.strictObject({ resourceId: eid, revision, view: z.enum(['current', 'archive']), total: count,
  items: z.array(z.record(z.string(), z.unknown())).max(100), nextCursor: z.string().min(1).max(1024).nullable() })
  .refine(value => value.items.length <= value.total)
const classSummary = z.strictObject({ retentionCutoff: utc, archiveCutoff: utc, eligible_count: count, archive_count: count, batch_count: count, remaining_eligible_count: count })
const plan = z.strictObject({ mode: z.literal('dry_run'), expired: z.boolean(), active_count: count, retained_count: count,
  retentionClock: utc, settings, build: classSummary, other: classSummary })
  .refine(value => value.retained_count + value.build.eligible_count + value.other.eligible_count === value.active_count)
const reviewInput = z.strictObject({ mode: z.enum(['apply', 'restore']), ...options.shape })
const reviewResult = z.strictObject({ reviewId: review, mode: z.enum(['apply', 'restore']), archive_count: count, archive_digest: digest })
const applyInput = z.strictObject({ reviewId: review, class: z.enum(['build', 'other']) })
const summary = z.strictObject({ changed_count: count, revision: revision.optional(), stop_reason: z.enum(['complete', 'max_batches', 'deadline', 'conflict', 'paused']) })
const restoreAdmissionInput = z.strictObject({ reviewId: review, invocationId: digest })
const restoreAdmission = z.strictObject({ authorityId: eid, revision, targetId: eid, invocationId: digest, admittedAt: utc, parentDeadlineAt: utc })
  .refine(value => Date.parse(value.parentDeadlineAt) - Date.parse(value.admittedAt) === 240000)
const restoreInput = z.strictObject({ authorityId: eid })
const restoreReceipt = z.strictObject({ invocation_id: digest, target_id: eid, previous_revision: revision.max(2147483646), revision,
  review_id: review, changed_count: count, archive_count: count, archive_digest: digest, completed_at: utc })
  .refine(value => value.revision === value.previous_revision + 1 && value.changed_count <= value.archive_count)
const restoreResult = z.union([restoreReceipt, z.strictObject({ changed_count: z.literal(0), revision, stop_reason: z.literal('complete') })])
// Opaque keyset positions use canonical, unpadded base64url. They never grant access.
const historyCursor = z.string().min(1).max(1024).regex(/^[A-Za-z0-9_-]+(?![\s\S])/).refine(value => {
  const remainder = value.length % 4
  const tail = ['ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz', '0123456789-_'].join('').indexOf(value.at(-1))
  return remainder !== 1 && (remainder !== 2 || (tail & 15) === 0) && (remainder !== 3 || (tail & 3) === 0)
})
const historyCheckpoint = z.strictObject({ phase: z.enum(['admitted', 'dispatched', 'complete']), batches: count,
  stop_reason: z.enum(['complete', 'max_batches', 'deadline', 'conflict', 'paused']).optional(), last_batch_id: digest.optional() })
const historyItem = z.strictObject({ resourceId: eid, revision, admittedAt: utc, parentDeadlineAt: utc, expired: z.boolean(),
  dueAt: utc.refine(value => value.endsWith('T09:45:00.000Z')), schedule: boundRef(identity), definition: boundRef(identity), prepared: boundRef(table),
  checkpoint: historyCheckpoint, counters: audit.shape.counters.nullable(), archiveDigest: digest.nullable() })
  .refine(value => Date.parse(value.parentDeadlineAt) - Date.parse(value.admittedAt) === 240000 &&
    (value.counters === null) === (value.archiveDigest === null))
const historyPage = z.strictObject({ items: z.array(historyItem).max(25), nextCursor: historyCursor.nullable(), asOf: utc })
  .refine(value => value.items.every((item, index) => item.admittedAt <= value.asOf && (index === 0 ||
    value.items[index - 1].admittedAt > item.admittedAt || (value.items[index - 1].admittedAt === item.admittedAt &&
      value.items[index - 1].resourceId > item.resourceId))))
const safeKeys = new Set(['__proto__', 'prototype', 'constructor'])
function jsonBytes(value) {
  let nodes = 0
  const visit = (item, depth) => {
    if (++nodes > 50000 || depth > 32) throw new Error('bounded JSON required')
    if (item === null || typeof item === 'string' || typeof item === 'boolean') return
    if (typeof item === 'number' && Number.isFinite(item) && !Object.is(item, -0)) return
    if (!item || typeof item !== 'object' || (Array.isArray(item) ? Object.getPrototypeOf(item) !== Array.prototype :
      Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null)) throw new Error('plain JSON required')
    if (Object.getOwnPropertySymbols(item).length) throw new Error('plain JSON required')
    const descriptors = Object.getOwnPropertyDescriptors(item)
    if (Array.isArray(item) && (Object.keys(item).length !== item.length || Object.keys(item).some((key, index) => key !== String(index)))) throw new Error('dense array required')
    for (const key of Object.keys(item)) {
      if (safeKeys.has(key) || !descriptors[key] || !Object.hasOwn(descriptors[key], 'value')) throw new Error('plain JSON required')
      visit(descriptors[key].value, depth + 1)
    }
  }
  visit(value, 0)
  return new TextEncoder().encode(JSON.stringify(value)).byteLength
}

/** Adds only fixed retention routes to an existing authenticated request client. */
export function createProjectLogRetentionMethods(request, invalid) {
  if (typeof request !== 'function' || typeof invalid !== 'function') throw new TypeError('Retention request and error factory are required')
  const parse = (schema, value) => { const result = schema.safeParse(value); if (!result.success) throw invalid(); return result.data }
  const validateOptions = (value, rows = false) => {
    if (!value || typeof value !== 'object') throw invalid()
    const allowed = rows ? ['signal', 'view', 'limit', 'after'] : ['signal']
    if (Object.keys(value).some(key => !allowed.includes(key)) || !(value.signal instanceof AbortSignal) || value.signal.aborted) throw invalid()
    return value.signal
  }
  const send = async (projectId, path, signal, method, input, output, binding = () => true, maximum = 65536) => {
    parse(uuid, projectId)
    let body
    if (method !== 'GET') {
      body = input
      try { if (jsonBytes(body) > 65536) throw invalid() } catch { throw invalid() }
    }
    const value = await request(projectId, '/integrations/retention/' + path,
      method === 'GET' ? { signal } : { signal, method, body, bodyLimit: 65536 })
    try {
      if (jsonBytes(value) > maximum) throw invalid()
      const result = parse(output, value)
      if (!binding(result)) throw invalid()
      return result
    } catch {
      const error = invalid(); error.dispatched = method !== 'GET'; throw error
    }
  }
  const executionPath = value => 'executions/' + parse(eid, value)
  const versionPath = (kind, id, version) => kind + '/' + parse(slug, id) + '/versions/' + parse(revision, version)
  return {
    async retentionHistory(projectId, context) {
      if (!context || typeof context !== 'object' || Object.keys(context).some(key => !['signal', 'limit', 'after'].includes(key))) throw invalid()
      const signal = validateOptions({ signal: context.signal })
      const value = parse(z.strictObject({ limit: z.number().int().min(1).max(25), after: historyCursor.optional() }),
        { limit: context.limit === undefined ? 25 : context.limit, ...(context.after === undefined ? {} : { after: context.after }) })
      const query = new URLSearchParams({ limit: String(value.limit) }); if (value.after !== undefined) query.set('after', value.after)
      return send(projectId, 'executions?' + query, signal, 'GET', undefined, historyPage, result => result.items.length <= value.limit)
    },
    async retentionPrepare(projectId, input, context) { const signal = validateOptions(context); const body = parse(prepareInput, input)
      return send(projectId, 'prepare', signal, 'POST', body, z.strictObject({ tableId: table, revision: z.literal(1) })) },
    async retentionRegisterDefinition(projectId, input, context) { const signal = validateOptions(context); const body = parse(definitionInput, input)
      return send(projectId, 'definitions', signal, 'POST', body, definitionResult, value => value.resourceId === 'retention-definition:' + body.id + ':v' + body.version) },
    async retentionGetDefinition(projectId, id, version, context) { const signal = validateOptions(context); const path = versionPath('definitions', id, version)
      return send(projectId, path, signal, 'GET', undefined, definitionResult, value => value.resourceId === 'retention-definition:' + id + ':v' + version) },
    async retentionExportDefinition(projectId, id, version, context) { const signal = validateOptions(context); const path = versionPath('definitions', id, version)
      return send(projectId, path + '/export', signal, 'GET', undefined, exported, value => value.definition.id === id && value.definition.version === version) },
    async retentionRegisterSchedule(projectId, input, context) { const signal = validateOptions(context); const body = parse(scheduleInput, input)
      return send(projectId, 'schedules', signal, 'POST', body, scheduleResult, value => value.resourceId === 'retention-schedule:' + body.id + ':v' + body.version &&
        value.schedule.definition_id === body.definition.resourceId && value.schedule.definition_revision === body.definition.revision && value.schedule.prepared_table_id === body.preparedTableId) },
    async retentionGetSchedule(projectId, id, version, context) { const signal = validateOptions(context); const path = versionPath('schedules', id, version)
      return send(projectId, path, signal, 'GET', undefined, scheduleResult, value => value.resourceId === 'retention-schedule:' + id + ':v' + version) },
    async retentionAdmit(projectId, input, context) { const signal = validateOptions(context); const body = parse(admitInput, input)
      return send(projectId, 'occurrences', signal, 'POST', body, audit, value => value.intent.schedule.resourceId === body.schedule.resourceId &&
        value.intent.schedule.revision === body.schedule.revision && value.intent.definition.resourceId === body.definition.resourceId &&
        value.intent.definition.revision === body.definition.revision && value.intent.prepared.resourceId === body.preparedTableId) },
    async retentionAudit(projectId, id, context) { const signal = validateOptions(context)
      return send(projectId, executionPath(id), signal, 'GET', undefined, audit, value => value.resourceId === id) },
    async retentionRows(projectId, id, context) { const signal = validateOptions(context, true)
      const value = parse(z.strictObject({ view: z.enum(['current', 'archive']), limit: z.number().int().min(1).max(100), after: z.string().min(1).max(1024).optional() }),
        { view: context.view, limit: context.limit === undefined ? 25 : context.limit, ...(context.after === undefined ? {} : { after: context.after }) })
      const query = new URLSearchParams({ view: value.view, limit: String(value.limit) }); if (value.after !== undefined) query.set('after', value.after)
      return send(projectId, executionPath(id) + '/rows?' + query, signal, 'GET', undefined, page,
        result => result.resourceId === id && result.view === value.view && result.items.length <= value.limit, 262144) },
    async retentionPlan(projectId, id, input, context) { const signal = validateOptions(context)
      return send(projectId, executionPath(id) + '/plan', signal, 'POST', parse(options, input), plan) },
    async retentionReview(projectId, id, input, context) { const signal = validateOptions(context); const body = parse(reviewInput, input)
      return send(projectId, executionPath(id) + '/review', signal, 'POST', body, reviewResult, value => value.mode === body.mode) },
    async retentionApply(projectId, id, input, context) { const signal = validateOptions(context)
      return send(projectId, executionPath(id) + '/apply', signal, 'POST', parse(applyInput, input), summary) },
    async retentionRecover(projectId, id, context) { const signal = validateOptions(context)
      return send(projectId, executionPath(id) + '/recover', signal, 'POST', {}, summary.refine(value => value.changed_count === 0 && value.revision !== undefined)) },
    async retentionAdmitRestore(projectId, id, input, context) { const signal = validateOptions(context); const body = parse(restoreAdmissionInput, input)
      return send(projectId, executionPath(id) + '/restore-admissions', signal, 'POST', body, restoreAdmission,
        value => value.targetId === id && value.invocationId === body.invocationId) },
    async retentionRestore(projectId, id, input, context) { const signal = validateOptions(context)
      return send(projectId, executionPath(id) + '/restore', signal, 'POST', parse(restoreInput, input), restoreResult,
        value => !Object.hasOwn(value, 'target_id') || value.target_id === id) },
  }
}
