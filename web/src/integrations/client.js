import { z } from 'zod'
import { authHeaders, noteUnauthorized } from '../api.js'
import { ConnectionV1, DatasetElementV1, DatasetQueryV1, DatasetReadV1, DatasetFieldV1, IntegrationRefV1,
  IntegrationActionElementV1, IntegrationActionSubmissionV1, IntegrationActionSubmissionResultV1, IntegrationActionSnapshotV1 } from '../../../vendor/assistant-contract/src/integration'

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\s\S])/i
const slug = z.string().regex(/^[a-z0-9]+(?:[-_][a-z0-9]+)*(?![\s\S])/).min(1).max(128)
const revision = z.number().int().positive()
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
const metadata = z.object({
  reference: IntegrationRefV1, revision, title: z.string().min(1).max(120),
  fields: z.array(DatasetFieldV1).min(1).max(100).refine((fields) => new Set(fields.map((field) => field.id)).size === fields.length),
  lastAttempt: z.unknown(), freshness: z.unknown(),
})

export class IntegrationError extends Error {
  constructor(status = 0, message = 'Integrations are unavailable. Please retry.') {
    super(message)
    this.status = status
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
  async function request(projectId, path, { signal, method = 'GET', body, match } = {}) {
    if (!destination) throw new IntegrationError(0, 'Integration setup is unavailable. An API destination must be configured.')
    if (!UUID.test(projectId) || !signal) throw invalid()
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const auth = authHeaders()
    if (!/^Bearer \S+(?![\s\S])/.test(auth.Authorization || '')) throw new IntegrationError(401, 'Sign in to view integrations.')
    try {
      const response = await fetchImpl(`${destination}/api/v1/projects/${projectId}${path}`, {
        method, signal, credentials: 'omit', redirect: 'error', cache: 'no-store',
        headers: { ...auth, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}), ...(match !== undefined ? { 'If-Match': String(match) } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
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
      if (error instanceof IntegrationError || error?.name === 'AbortError') throw error
      throw new IntegrationError()
    }
  }
  const list = async (projectId, resource, schema, { signal, after, limit = 25 } = {}) => {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (after !== undefined && (typeof after !== 'string' || after.length > 2048))) throw invalid()
    const query = new URLSearchParams({ limit: String(limit) })
    if (after !== undefined) query.set('after', after)
    return parse(page(schema), await request(projectId, `/integrations/${resource}?${query}`, { signal }))
  }
  return {
    available: Boolean(destination),
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
