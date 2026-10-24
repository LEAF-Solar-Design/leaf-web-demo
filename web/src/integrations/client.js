import { z } from 'zod'
import { authHeaders, noteUnauthorized } from '../api.js'
import { parseScheduleDraftReferencePage } from '../../../vendor/assistant-react/src/actions/workflow-schedule-draft-picker-state'
import { ConnectionV1, DatasetElementV1, DatasetQueryV1, DatasetReadV1, DatasetFieldV1, IntegrationRefV1,
  ConnectorDefinitionV1, DatasetDefinitionV1, TransformMappingV1, IntegrationWorkflowScheduleDraftV1,
  IntegrationActionElementV1, IntegrationActionSubmissionV1, IntegrationActionSubmissionResultV1, IntegrationActionSnapshotV1,
  IntegrationManualWorkflowSnapshotV1, IntegrationManualWorkflowRunV1, IntegrationManualWorkflowDefinitionV1, IntegrationActionCatalogV1,
  IntegrationManualWorkflowSubmissionV1, IntegrationManualWorkflowSubmissionResultV1 } from '../../../vendor/assistant-contract/src/integration'

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/i
const slug = z.string().regex(/^[a-z0-9]+(?:[-_][a-z0-9]+)*(?![\s\S])/).min(1).max(128)
const revision = z.number().int().positive().max(Number.MAX_SAFE_INTEGER)
const scopeSchema = z.object({
  tenantId: z.string().uuid(), projectId: z.string().uuid(), platformUserId: z.string().uuid(),
  bindingId: z.string().uuid(), projectRole: z.enum(['owner', 'editor', 'reviewer', 'read_only']),
  externalAuthority: z.literal('auth0'), externalSubject: z.string().min(1).max(500),
})
const recipeSchema = z.union([DatasetElementV1, IntegrationActionElementV1])
const savedSchema = z.object({ element: recipeSchema, revision })
// The API returns a safe summary, deliberately without credential references.
const connectionSchema = z.object({
  id: ConnectionV1.shape.id, state: ConnectionV1.shape.state, connector_ref: ConnectionV1.shape.connector_ref,
  operation_ids: ConnectionV1.shape.operation_ids.refine((ids) => new Set(ids).size === ids.length),
  grant_revision: ConnectionV1.shape.grant_revision.max(Number.MAX_SAFE_INTEGER),
})
const sameRef = (left, right) => ['id', 'version', 'digest'].every((key) => left[key] === right[key])
export const actionRunMatches = (run, binding) => run.connection_id === binding.connection_id &&
  sameRef(run.connector_ref, binding.connector_ref) && sameRef(run.operation_ref, binding.operation_ref) &&
  run.grant_revision === binding.expected_grant_revision
const page = (record) => z.object({ records: z.array(record).max(100), nextCursor: z.string().max(2048).nullable() })
const workflowCursor = z.string().regex(/^[A-Za-z0-9_-]{1,1024}(?![\s\S])/)
export const ManualWorkflowHistoryPage = z.strictObject({
  items: z.array(IntegrationManualWorkflowRunV1).max(50)
    .refine((items) => new Set(items.map((run) => run.run_id.toLowerCase())).size === items.length),
  next_cursor: workflowCursor.nullable(),
})
const scheduleDate = z.string().regex(/^20\d{2}-\d{2}-\d{2}(?![\s\S])/).refine((value) => {
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
})
const scheduleInstant = z.string().regex(/^20\d{2}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,3})?Z(?![\s\S])/)
  .refine((value) => scheduleDate.safeParse(value.slice(0, 10)).success && Number.isFinite(Date.parse(value)))
function canonicalScheduleTimezone(value) {
  try { return new Intl.DateTimeFormat('en-US', { timeZone: value }).resolvedOptions().timeZone } catch { return null }
}
const scheduleTimezone = z.string().min(1).max(100).regex(/^[A-Za-z][A-Za-z0-9._+-]*(?:\/[A-Za-z0-9._+-]+)*(?![\s\S])/).refine((value) =>
  value === value.trim() && canonicalScheduleTimezone(value) !== null)
const scheduleHour = z.number().int().min(0).max(23)
const scheduleMinute = z.number().int().min(0).max(59)
const schedulePreviewInput = z.strictObject({ schema_version: z.literal(1),
  schedule: z.strictObject({ kind: z.literal('daily'), timezone: scheduleTimezone, hour: scheduleHour, minute: scheduleMinute }),
  after: scheduleInstant,
})
const schedulePreviewResponse = z.strictObject({ schema_version: z.literal(1), preview_only: z.literal(true),
  occurrence: z.strictObject({ scheduledLocalDate: scheduleDate, instant: scheduleInstant, timezone: scheduleTimezone,
    hour: scheduleHour, minute: scheduleMinute, occurrenceKey: z.string().max(256) }),
})
export function validateWorkflowSchedulePreviewInput(value) {
  const body = parse(schedulePreviewInput, value)
  boundedIntegrationBody(body, 2048)
  return body
}
export function validateWorkflowSchedulePreview(value, input) {
  const body = validateWorkflowSchedulePreviewInput(input)
  const result = parse(schedulePreviewResponse, value)
  const occurrence = result.occurrence
  if (occurrence.timezone !== canonicalScheduleTimezone(body.schedule.timezone) ||
      canonicalScheduleTimezone(occurrence.timezone) !== occurrence.timezone ||
      occurrence.hour !== body.schedule.hour || occurrence.minute !== body.schedule.minute ||
      Date.parse(occurrence.instant) <= Date.parse(body.after) ||
      occurrence.occurrenceKey !== JSON.stringify(['daily', occurrence.timezone, occurrence.hour, occurrence.minute, occurrence.scheduledLocalDate])) throw invalid()
  return result
}
const metadata = z.object({
  reference: IntegrationRefV1, revision, title: z.string().min(1).max(120),
  fields: z.array(DatasetFieldV1).min(1).max(100).refine((fields) => new Set(fields.map((field) => field.id)).size === fields.length),
  lastAttempt: z.unknown(), freshness: z.unknown(),
})

const title = z.string().min(1).max(120)
const authKind = z.enum(['none', 'bearer', 'api_key'])
const refResult = z.object({ reference: IntegrationRefV1, revision })
const importResult = z.object({ reference: IntegrationRefV1.nullable(), revision: revision.nullable(),
  issues: z.array(z.object({ code: z.string().min(1).max(128), summary: z.string().max(1000) })).max(100),
}).refine((value) => (value.reference === null) === (value.revision === null))
const connectorMetadata = z.object({ id: slug, title, state: z.enum(['draft', 'published', 'retired']),
  reference: IntegrationRefV1, revision, auth_kind: authKind, operation_count: z.number().int().min(1).max(100),
  draft_definition: ConnectorDefinitionV1.optional(),
})
const publishedMetadata = connectorMetadata.omit({ draft_definition: true }).extend({
  definition: ConnectorDefinitionV1,
  operationReferences: z.array(IntegrationRefV1).min(1).max(100)
    .refine((refs) => new Set(refs.map((ref) => ref.id)).size === refs.length),
})
// Owner HTTP summaries are public projections, never the private ConnectionV1 storage object.
const ownerConnection = z.object({ id: slug, title, revision, state: ConnectionV1.shape.state,
  health: ConnectionV1.shape.health, cleanup: z.enum(['complete', 'needs-attention']),
  connector_ref: IntegrationRefV1, origin: ConnectionV1.shape.origin, auth_kind: authKind,
  credential_purposes: z.array(z.enum(['access_token', 'api_key'])).max(2).refine((values) => new Set(values).size === values.length),
  operation_ids: z.array(slug).min(1).max(100).refine((ids) => new Set(ids).size === ids.length),
  grant_revision: revision, owner_id: z.string().uuid(), provider_account_id: z.string().min(1).max(256).nullable(),
  identity_verified: z.boolean(),
}).refine((value) => (!value.identity_verified || value.provider_account_id !== null) &&
  (value.state !== 'revoked' || value.health.code === 'revoked') && value.credential_purposes.every((purpose) =>
    (value.auth_kind === 'bearer' && purpose === 'access_token') || (value.auth_kind === 'api_key' && purpose === 'api_key')))
// No dataset sync receipt is exported by the vendored contract. Project the host's public evidence.
const instant = ConnectionV1.shape.health.shape.checked_at.unwrap()
const syncReceipt = z.object({ id: z.string().uuid(), started_at: instant, finished_at: instant,
  actor_id: z.string().uuid(), binding_id: z.string().uuid(), connector_ref: IntegrationRefV1,
  operation_ref: IntegrationRefV1, dataset_ref: IntegrationRefV1, transform_ref: IntegrationRefV1,
  source_outcome: z.enum(['success', 'failure']), outcome: z.enum(['success', 'failure']),
  row_count: z.number().int().min(0).max(250), failure_code: z.enum(['destination_refused', 'timeout', 'response_too_large',
    'invalid_response', 'auth', 'quota', 'transient', 'cancelled', 'invalid_records', 'materialization_too_large']).nullable(),
  retryAfterMs: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER).optional(),
}).refine((receipt) => {
  const [start, startFraction = ''] = receipt.started_at.slice(0, -1).split('.')
  const [end, endFraction = ''] = receipt.finished_at.slice(0, -1).split('.')
  const width = Math.max(startFraction.length, endFraction.length)
  return (start < end || (start === end && startFraction.padEnd(width, '0') <= endFraction.padEnd(width, '0'))) &&
    (receipt.outcome === 'success' ? receipt.source_outcome === 'success' && receipt.failure_code === null :
      receipt.row_count === 0 && receipt.failure_code !== null)
})
const syncResult = refResult.extend({ receipt: syncReceipt, status: z.enum(['ready', 'empty', 'stale', 'unavailable']) })
  .refine((value) => sameRef(value.reference, value.receipt.dataset_ref) &&
    (value.receipt.outcome === 'success' ? value.status === (value.receipt.row_count ? 'ready' : 'empty') :
      ['stale', 'unavailable'].includes(value.status)))
const datasetMetadata = metadata.extend({ lastAttempt: syncReceipt.nullable(), freshness: DatasetReadV1.shape.freshness.nullable() })
const registrationSchema = z.strictObject({ definition: DatasetDefinitionV1, mapping: TransformMappingV1 })
  .refine(({ definition, mapping }) => definition.fields.length === mapping.fields.length &&
    definition.fields.every((field) => mapping.fields.some((entry) => entry.field_id === field.id)) &&
    definition.lineage.transform_ref === undefined)

export function boundedIntegrationBody(body, limit = 262144) {
  let encoded
  try { encoded = JSON.stringify(body) } catch { throw invalid() }
  if (typeof encoded !== 'string' || new TextEncoder().encode(encoded).byteLength > limit) throw invalid()
  return encoded
}
export function validateDatasetRegistration(value) {
  const body = parse(registrationSchema, value)
  boundedIntegrationBody(body, 163840)
  return body
}

export class IntegrationError extends Error {
  constructor(status = 0, message = 'Integrations are unavailable. Please retry.') {
    super(message)
    this.status = status
    this.dispatched = false
  }
}
const invalid = () => new IntegrationError(0, 'The integration response or request was invalid.')
function parse(schema, value) {
  const result = schema.safeParse(value)
  if (!result.success) throw invalid()
  return result.data
}
export function integrationOrigin(value) {
  if (typeof value !== 'string' || !/^https:\/\/[^/?#@\\\s]+$/.test(value)) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password && url.origin === value ? value : null
  } catch { return null }
}
async function boundedJson(response, signal) {
  if (!response.body?.getReader) throw invalid()
  const reader = response.body.getReader()
  const abort = () => { reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', abort, { once: true })
  const chunks = []
  let size = 0
  try {
    if (!/^application\/(?:json|[a-z0-9.+-]+\+json)(?:\s*;|$)/i.test(response.headers.get('content-type') || '')) throw invalid()
    if (Number(response.headers.get('content-length')) > 262144) throw invalid()
    while (true) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 262144) throw invalid()
      chunks.push(value)
    }
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const bytes = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  } catch (error) {
    await reader.cancel().catch(() => {})
    if (error?.name === 'AbortError') throw error
    throw invalid()
  } finally { signal.removeEventListener('abort', abort); reader.releaseLock() }
}

export function createIntegrationsClient({ origin = import.meta.env.VITE_INTEGRATIONS_API_ORIGIN, fetchImpl = globalThis.fetch } = {}) {
  const destination = integrationOrigin(origin)
  async function request(projectId, path, { signal, method = 'GET', body, match, bodyLimit = 262144 } = {}) {
    if (!destination) throw new IntegrationError(0, 'Integration setup is unavailable. An API destination must be configured.')
    if (!UUID.test(projectId) || !signal) throw invalid()
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const encoded = body === undefined ? undefined : boundedIntegrationBody(body, bodyLimit)
    const auth = authHeaders()
    if (!/^Bearer \S+(?![\s\S])/.test(auth.Authorization || '')) throw new IntegrationError(401, 'Sign in to view integrations.')
    try {
      const response = await fetchImpl(`${destination}/api/v1/projects/${projectId}${path}`, {
        method, signal, credentials: 'omit', redirect: 'error', cache: 'no-store',
        headers: { ...auth, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}), ...(match !== undefined ? { 'If-Match': String(match) } : {}) },
        ...(body ? { body: encoded } : {}),
      })
      noteUnauthorized(response, 'integrations', auth.Authorization, { fatal: false })
      if (!response.ok) {
        response.body?.cancel?.().catch(() => {})
        throw new IntegrationError(response.status, response.status === 403 ? 'You do not have permission to view these integrations.' :
          response.status === 401 ? 'Integration authorization has expired. Please retry after signing in.' :
            response.status === 409 ? 'This view changed. Reload it before updating.' : 'The integration request failed. Please retry.')
      }
      return await boundedJson(response, signal)
    } catch (error) {
      const safe = error instanceof IntegrationError || error?.name === 'AbortError' ? error : new IntegrationError()
      safe.dispatched = method !== 'GET'
      throw safe
    }
  }
  async function setupResponse(schema, id, path, options, check = () => true) {
    const value = await request(id, `/integrations/${path}`, options)
    try {
      const result = parse(schema, value)
      if (!check(result)) throw invalid()
      return result
    } catch (error) { error.dispatched = options?.method !== undefined && options.method !== 'GET'; throw error }
  }
  const list = async (projectId, resource, schema, { signal, after, limit = 25 } = {}) => {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (after !== undefined && (typeof after !== 'string' || after.length > 2048))) throw invalid()
    const query = new URLSearchParams({ limit: String(limit) })
    if (after !== undefined) query.set('after', after)
    return parse(page(schema), await request(projectId, `/integrations/${resource}?${query}`, { signal }))
  }
  return {
    available: Boolean(destination),
    async previewWorkflowSchedule(id, input, { signal } = {}) {
      const body = validateWorkflowSchedulePreviewInput(input)
      const value = await request(id, '/integrations/workflow-schedule-preview', { signal, method: 'POST', body, bodyLimit: 2048 })
      return validateWorkflowSchedulePreview(value, body)
    },
    async listManualWorkflowRuns(id, { signal, after, limit = 25 } = {}) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 50 ||
          (after !== undefined && !workflowCursor.safeParse(after).success)) throw invalid()
      const query = new URLSearchParams({ limit: String(limit) })
      if (after !== undefined) query.set('after', after)
      return setupResponse(ManualWorkflowHistoryPage, id, `manual-workflow-runs?${query}`, { signal, method: 'GET' })
    },
    async getActionCatalog(id, { signal, after, limit = 25 } = {}) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 50 || (after !== undefined &&
          (typeof after !== 'string' || !/^[A-Za-z0-9_-]{1,1024}(?![\s\S])/.test(after)))) throw invalid()
      const query = new URLSearchParams({ limit: String(limit) })
      if (after !== undefined) query.set('after', after)
      return setupResponse(IntegrationActionCatalogV1, id, `action-catalog?${query}`, { signal, method: 'GET' })
    },
    async registerManualWorkflow(id, input, options) {
      const body = parse(IntegrationManualWorkflowDefinitionV1, input)
      return setupResponse(IntegrationRefV1, id, 'manual-workflows', { ...options, method: 'POST', body, bodyLimit: 65536 },
        (value) => value.id === body.id && value.version === body.version)
    },
    async registerWorkflowScheduleDraft(id, input, { signal } = {}) {
      const body = parse(IntegrationWorkflowScheduleDraftV1, input)
      return setupResponse(IntegrationRefV1, id, 'manual-workflow-schedule-drafts', { signal, method: 'POST', body, bodyLimit: 65536 },
        (value) => value.id === body.id && value.version === body.version)
    },
    async getWorkflowScheduleDraft(id, reference, { signal } = {}) {
      const ref = parse(IntegrationRefV1, reference)
      return setupResponse(IntegrationWorkflowScheduleDraftV1, id,
        `manual-workflow-schedule-drafts/${ref.id}/versions/${ref.version}?digest=${encodeURIComponent(ref.digest)}`,
        { signal, method: 'GET' }, (value) => value.id === ref.id && value.version === ref.version)
    },
    async listWorkflowScheduleDrafts(id, { signal, limit = 25, after } = {}) {
      if (!Number.isInteger(limit) || limit < 1 || limit > 25 || (after !== undefined &&
          (typeof after !== 'string' || after.length === 0 || after.length > 2048))) throw invalid()
      const query = new URLSearchParams({ limit: String(limit) })
      if (after !== undefined) query.set('after', after)
      const value = await request(id, `/integrations/manual-workflow-schedule-drafts?${query}`, { signal })
      const result = parseScheduleDraftReferencePage(value, after)
      if (!result || result.references.length > limit) throw invalid()
      return result
    },
    async getManualWorkflowDefinition(id, reference, options) {
      const ref = parse(IntegrationRefV1, reference)
      return setupResponse(IntegrationManualWorkflowDefinitionV1, id,
        `manual-workflows/${ref.id}/versions/${ref.version}?digest=${encodeURIComponent(ref.digest)}`,
        { ...options, method: 'GET', body: undefined },
        (value) => value.id === ref.id && value.version === ref.version)
    },
    async submitManualWorkflow(id, input, options) {
      const body = parse(IntegrationManualWorkflowSubmissionV1, input)
      return setupResponse(IntegrationManualWorkflowSubmissionResultV1, id, 'manual-workflow-runs',
        { ...options, method: 'POST', body },
        (value) => value.status !== 'admitted' || sameRef(value.run.workflow_ref, body.workflow_ref))
    },
    async getManualWorkflowRun(id, runId, options) {
      if (!UUID.test(runId)) throw invalid()
      return setupResponse(IntegrationManualWorkflowSnapshotV1, id, `manual-workflow-runs/${runId}`, { ...options, method: 'GET', body: undefined },
        (value) => value.run.run_id === runId)
    },
    async resumeManualWorkflowRun(id, runId, input, options) {
      if (!UUID.test(runId)) throw invalid()
      const body = parse(z.strictObject({}), input)
      return setupResponse(IntegrationManualWorkflowSnapshotV1, id, `manual-workflow-runs/${runId}/resume`, { ...options, method: 'POST', body },
        (value) => value.run.run_id === runId)
    },
    async reconcileManualWorkflowStep(id, runId, stepId, input, options) {
      if (!UUID.test(runId)) throw invalid()
      parse(slug, stepId)
      const body = parse(z.strictObject({}), input)
      return setupResponse(IntegrationActionSnapshotV1, id, `manual-workflow-runs/${runId}/steps/${stepId}/reconcile`, { ...options, method: 'POST', body })
    },
    async importConnector(id, input, options) {
      const body = parse(z.strictObject({ document: z.record(z.string(), z.unknown()), options: z.strictObject({ id: slug, title,
        origin: ConnectionV1.shape.origin.optional() }) }), input)
      if (!/^3\.(0|1)\.[0-9]+$/.test(body.document.openapi || '')) throw invalid()
      return setupResponse(importResult, id, 'connectors/import', { ...options, method: 'POST', body },
        (result) => result.reference === null || result.reference.id === body.options.id)
    },
    async getConnector(id, connectorId, options) {
      parse(slug, connectorId)
      return setupResponse(connectorMetadata, id, `connectors/${connectorId}`, options,
        (value) => value.id === connectorId && value.reference.id === connectorId && (!value.draft_definition ||
          (value.state === 'draft' && value.draft_definition.state === 'draft' && value.draft_definition.id === connectorId && value.draft_definition.version === value.reference.version &&
           value.draft_definition.operations.length === value.operation_count && value.draft_definition.auth.kind === value.auth_kind)))
    },
    async publishConnector(id, connectorId, input, options) {
      parse(slug, connectorId)
      const body = parse(z.strictObject({ version: revision, expectedRevision: revision, readOperationIds: z.array(slug).length(1) }), input)
      return setupResponse(refResult, id, `connectors/${connectorId}/publish`, { ...options, method: 'POST', body },
        (value) => value.reference.id === connectorId && value.reference.version === body.version)
    },
    async getConnectorVersion(id, connectorId, version, options) {
      parse(slug, connectorId); parse(revision, version)
      return setupResponse(publishedMetadata, id, `connectors/${connectorId}/versions/${version}`, options,
        (value) => value.id === connectorId && value.reference.id === connectorId && value.reference.version === version &&
          value.state === 'published' && value.definition.id === connectorId && value.definition.version === version && value.definition.state === 'published' &&
          value.definition.auth.kind === value.auth_kind && value.operation_count === value.definition.operations.length &&
          value.operationReferences.length === value.operation_count && value.definition.operations.every((operation) =>
            value.operationReferences.some((ref) => ref.id === operation.id && ref.version === version)))
    },
    async createConnection(id, input, options) {
      const body = parse(z.strictObject({ id: slug, title, connectorRef: IntegrationRefV1, origin: ConnectionV1.shape.origin,
        operationIds: z.array(slug).length(1) }), input)
      return setupResponse(ownerConnection, id, 'connections', { ...options, method: 'POST', body },
        (value) => value.id === body.id && value.title === body.title && sameRef(value.connector_ref, body.connectorRef) &&
          value.origin === body.origin && value.operation_ids.length === 1 && value.operation_ids[0] === body.operationIds[0])
    },
    async getConnectionSummary(id, connectionId, options) {
      parse(slug, connectionId)
      return setupResponse(ownerConnection, id, `connections/${connectionId}`, options, (value) => value.id === connectionId)
    },
    async revokeConnection(id, connectionId, expectedRevision, options) {
      parse(slug, connectionId); parse(revision, expectedRevision)
      return setupResponse(ownerConnection, id, `connections/${connectionId}`,
        { ...options, method: 'DELETE', match: expectedRevision, body: undefined },
        (value) => value.id === connectionId && value.state === 'revoked' && value.revision >= expectedRevision)
    },
    async setConnectionCredential(id, connectionId, input, options) {
      parse(slug, connectionId)
      const body = parse(z.strictObject({ purpose: z.enum(['access_token', 'api_key']), value: z.string().min(1), expectedRevision: revision }), input)
      if (new TextEncoder().encode(body.value).byteLength > 16384) throw invalid()
      boundedIntegrationBody(body, 131072)
      return setupResponse(ownerConnection, id, `connections/${connectionId}/credential`, { ...options, method: 'PUT', body, bodyLimit: 131072 },
        (value) => value.id === connectionId && value.revision > body.expectedRevision && value.credential_purposes.includes(body.purpose))
    },
    async registerDataset(id, input, options) {
      const body = validateDatasetRegistration(input)
      return setupResponse(refResult, id, 'datasets', { ...options, method: 'POST', body, bodyLimit: 163840 },
        (value) => value.reference.id === body.definition.id && value.reference.version === body.definition.version)
    },
    async getDataset(id, reference, { revision: expected, ...options } = {}) {
      const ref = parse(IntegrationRefV1, reference)
      if (expected !== undefined) parse(revision, expected)
      return setupResponse(datasetMetadata, id, `datasets/${ref.id}/versions/${ref.version}${expected === undefined ? '' : `?revision=${expected}`}`, options,
        (value) => sameRef(value.reference, ref) && (expected === undefined || value.revision === expected) &&
          (!value.lastAttempt || sameRef(value.lastAttempt.dataset_ref, ref)))
    },
    async getDatasetVersion(id, datasetId, version, options) {
      parse(slug, datasetId); parse(revision, version)
      return setupResponse(datasetMetadata, id, `datasets/${datasetId}/versions/${version}`, options,
        (value) => value.reference.id === datasetId && value.reference.version === version &&
          (!value.lastAttempt || sameRef(value.lastAttempt.dataset_ref, value.reference)))
    },
    async syncDataset(id, reference, options) {
      const ref = parse(IntegrationRefV1, reference)
      return setupResponse(syncResult, id, `datasets/${ref.id}/versions/${ref.version}/sync`, { ...options, method: 'POST', body: {} },
        (value) => sameRef(value.reference, ref))
    },
    async getScope(projectId, options) {
      const scope = parse(scopeSchema, await request(projectId, '/scope', options))
      if (scope.projectId.toLowerCase() !== projectId.toLowerCase()) throw invalid()
      return scope
    },
    listDatasets: (id, options) => list(id, 'datasets', metadata, options),
    listElements: (id, options) => list(id, 'elements', savedSchema, options),
    async getConnection(id, connectionId, options) {
      parse(ConnectionV1.shape.id, connectionId)
      const connection = parse(connectionSchema, await request(id, `/integrations/connections/${connectionId}`, options))
      if (connection.id !== connectionId) throw invalid()
      return connection
    },
    async submitAction(id, submission, options) {
      const body = parse(IntegrationActionSubmissionV1, submission)
      const result = parse(IntegrationActionSubmissionResultV1, await request(id, '/integrations/actions', { ...options, method: 'POST', body }))
      if (result.status === 'admitted' && !actionRunMatches(result.run, body)) throw invalid()
      return result
    },
    async getAction(id, runId, options) {
      if (!UUID.test(runId)) throw invalid()
      const snapshot = parse(IntegrationActionSnapshotV1, await request(id, `/integrations/actions/${runId}`, options))
      if (snapshot.run.run_id !== runId) throw invalid()
      return snapshot
    },
    async reconcileAction(id, runId, { signal } = {}) {
      if (!UUID.test(runId)) throw invalid()
      const snapshot = parse(IntegrationActionSnapshotV1, await request(id, `/integrations/actions/${runId}/reconcile`, { signal, method: 'POST', body: {} }))
      if (snapshot.run.run_id !== runId) throw invalid()
      return snapshot
    },
    async queryDataset(id, reference, query, options) {
      const ref = parse(IntegrationRefV1, reference)
      const read = parse(DatasetReadV1, await request(id, `/integrations/datasets/${ref.id}/versions/${ref.version}/query`, { ...options, method: 'POST', body: parse(DatasetQueryV1, query) }))
      if (['id', 'version', 'digest'].some((key) => read.dataset_ref[key] !== ref[key])) throw invalid()
      return read
    },
    async saveElement(id, element, { expectedRevision, ...options } = {}) {
      const body = parse(recipeSchema, element)
      if (expectedRevision !== undefined) parse(revision, expectedRevision)
      const saved = parse(savedSchema, await request(id, `/integrations/elements${expectedRevision === undefined ? '' : `/${body.id}`}`, {
        ...options, method: expectedRevision === undefined ? 'POST' : 'PUT', body, match: expectedRevision,
      }))
      if (saved.element.id !== body.id) throw invalid()
      return saved
    },
    async getElement(id, elementId, { revision: version, ...options } = {}) {
      parse(slug, elementId)
      if (version !== undefined) parse(revision, version)
      const saved = parse(savedSchema, await request(id, `/integrations/elements/${elementId}${version === undefined ? '' : `?revision=${version}`}`, options))
      if (saved.element.id !== elementId || (version !== undefined && saved.revision !== version)) throw invalid()
      return saved
    },
  }
}
