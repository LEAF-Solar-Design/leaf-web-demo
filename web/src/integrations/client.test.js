import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextEncoder, TextDecoder } from 'node:util'
import { createIntegrationsClient, integrationOrigin, validateDatasetRegistration } from './client.js'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))
import { authHeaders, noteUnauthorized } from '../api.js'
const projectId = '11111111-1111-4111-8111-111111111111'
const scope = { tenantId: projectId, projectId, platformUserId: projectId, bindingId: projectId,
  projectRole: 'owner', externalAuthority: 'auth0', externalSubject: 'auth0|user' }
const ref = { id: 'orders', version: 1, digest: 'a'.repeat(64) }
const element = { schema_version: 1, id: 'orders-table', title: 'Orders', dataset_ref: ref,
  query: { fields: ['count'], limit: 100 }, kind: 'dataset.table', props: { columns: ['count'], row_limit: 100 } }
const action = { schema_version: 1, id: 'send-order', title: 'Send order', kind: 'integration.action',
  connection_id: 'orders-connection', connector_ref: ref, operation_ref: { ...ref, id: 'send' },
  form: { fields: [], submit_label: 'Send' } }
const submission = { schema_version: 1, connection_id: action.connection_id, connector_ref: ref,
  operation_ref: action.operation_ref, expected_grant_revision: 7, arguments: { count: 0, enabled: false }, idempotency_key: projectId }
const run = { schema_version: 1, run_id: projectId, revision: 1, connection_id: action.connection_id,
  connector_ref: ref, operation_ref: action.operation_ref, grant_revision: 7, submission_digest: 'b'.repeat(64),
  authority_binding_digest: 'c'.repeat(64), created_at: '2026-10-20T00:00:00Z', updated_at: '2026-10-20T00:00:00Z',
  state: 'accepted', effect_outcome: 'not_dispatched' }
const connection = { id: action.connection_id, state: 'active', connector_ref: ref, operation_ids: ['send'], grant_revision: 7 }
function response(value, { status = 200, type = 'application/json', raw, length } = {}) {
  const bytes = new TextEncoder().encode(raw ?? JSON.stringify(value))
  return { status, ok: status >= 200 && status < 300,
    headers: { get: (name) => name === 'content-type' ? type : name === 'content-length' ? length ?? null : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close() } }) }
}
beforeEach(() => {
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('TextEncoder', TextEncoder)
  authHeaders.mockReturnValue({ Authorization: 'Bearer first' })
})

const operation = { schema_version: 1, id: 'get-normalized-orders', title: 'Read orders', method: 'GET', path: '/orders', effect: 'read',
  pagination: { kind: 'none', records_pointer: '' }, limits: { timeout_ms: 1000, max_response_bytes: 131072, max_records: 250, max_pages: 1 }, idempotency: { kind: 'read' } }
const connectorDefinition = { schema_version: 1, id: 'orders', title: 'Orders', version: 1, state: 'draft', protocol: 'https-rest',
  origins: ['https://source.example'], auth: { kind: 'bearer' }, operations: [operation] }
const connector = { id: 'orders', title: 'Orders', state: 'draft', reference: ref, revision: 1, auth_kind: 'bearer', operation_count: 1, draft_definition: connectorDefinition }
const operationRef = { ...ref, id: operation.id }
const ownerSummary = { ...connection, title: 'Orders connection', revision: 1, origin: 'https://source.example', auth_kind: 'bearer',
  credential_purposes: [], health: { checked_at: null, code: 'unknown' }, cleanup: 'complete', owner_id: projectId, provider_account_id: null, identity_verified: false,
  operation_ids: [operation.id] }
const registration = { definition: { schema_version: 1, id: ref.id, title: 'Orders', version: 1,
  fields: [{ id: 'key', label: 'Key', type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' }],
  key_fields: ['key'], reporting_time_zone: 'UTC', retention_days: 30, lineage: { connection_id: connection.id, operation_ref: operationRef } },
  mapping: { schema_version: 1, fields: [{ field_id: 'key', pointer: '/id', conversion: 'identity' }] } }
const receipt = { id: projectId, actor_id: projectId, binding_id: projectId,
  started_at: '2026-10-20T00:00:00Z', finished_at: '2026-10-20T00:00:01Z',
  connector_ref: ref, operation_ref: operationRef, dataset_ref: ref, transform_ref: { ...ref, id: 'orders-transform' },
  source_outcome: 'success', outcome: 'success', row_count: 2, failure_code: null }
describe('first organization setup HTTP contract', () => {
  it('uses normalized IDs, exact refs, revision bodies and safe owner projections through the complete flow', async () => {
    const { client, fetchImpl, options } = setup({ reference: ref, revision: 1, issues: [] })
    const imported = { document: { openapi: '3.1.0', paths: {} }, options: { id: 'orders', title: 'Orders' } }
    await client.importConnector(projectId, imported, options)
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual(imported)
    fetchImpl.mockResolvedValueOnce(response(connector))
    expect((await client.getConnector(projectId, 'orders', options)).draft_definition.operations[0].id).toBe(operation.id)
    await client.publishConnector(projectId, 'orders', { version: 1, expectedRevision: 1, readOperationIds: [operation.id] }, options)
    expect(JSON.parse(fetchImpl.mock.calls[2][1].body)).toEqual({ version: 1, expectedRevision: 1, readOperationIds: [operation.id] })
    fetchImpl.mockResolvedValueOnce(response({ ...connector, state: 'published', definition: { ...connectorDefinition, state: 'published' }, operationReferences: [operationRef] }))
    const published = await client.getConnectorVersion(projectId, 'orders', 1, options)
    expect(published.draft_definition).toBeUndefined()
    fetchImpl.mockResolvedValueOnce(response({ ...ownerSummary, secret: 'must be dropped', credential_refs: ['private'] }))
    const created = await client.createConnection(projectId, { id: connection.id, title: ownerSummary.title, connectorRef: ref, origin: ownerSummary.origin, operationIds: [operation.id] }, options)
    expect(created).toEqual(ownerSummary)
    fetchImpl.mockResolvedValueOnce(response({ ...ownerSummary, revision: 2, credential_purposes: ['access_token'] }))
    await client.setConnectionCredential(projectId, connection.id, { purpose: 'access_token', value: 'transient', expectedRevision: 1 }, options)
    expect(fetchImpl.mock.calls[5][1]).toMatchObject({ method: 'PUT', signal: options.signal })
    fetchImpl.mockResolvedValueOnce(response(ownerSummary))
    await client.getConnectionSummary(projectId, connection.id, options)
    await client.registerDataset(projectId, registration, options)
    expect(JSON.parse(fetchImpl.mock.calls[7][1].body)).toEqual(registration)
    fetchImpl.mockResolvedValueOnce(response({ reference: ref, revision: 2, receipt, status: 'ready' }))
    expect((await client.syncDataset(projectId, ref, options)).receipt).toEqual(receipt)
    expect(fetchImpl.mock.calls[8][1]).toMatchObject({ method: 'POST', body: '{}' })
    fetchImpl.mockResolvedValueOnce(response({ reference: ref, revision: 2, title: 'Orders', fields: registration.definition.fields, lastAttempt: receipt,
      freshness: { source_updated_at: null, synced_at: null, fetched_at: '2026-10-20T00:00:00Z' } }))
    await client.getDataset(projectId, ref, { ...options, revision: 2 })
    expect(fetchImpl.mock.calls[9][0]).toContain('/datasets/orders/versions/1?revision=2')
    expect(fetchImpl.mock.calls.every(([, request]) => request.credentials === 'omit' && request.cache === 'no-store' && request.redirect === 'error')).toBe(true)
  })
  it('rejects invalid and UTF-8 oversized public, credential and mapping inputs before fetch', async () => {
    const { client, fetchImpl, options } = setup()
    await expect(client.importConnector(projectId, { document: { openapi: '3.0.3', description: 'é'.repeat(140000) }, options: { id: 'orders', title: 'Orders' } }, options)).rejects.toThrow('invalid')
    await expect(client.importConnector(projectId, { document: { openapi: '2.0' }, options: { id: 'orders', title: 'Orders' } }, options)).rejects.toThrow('invalid')
    await expect(client.setConnectionCredential(projectId, connection.id, { purpose: 'access_token', value: 'é'.repeat(8193), expectedRevision: 1 }, options)).rejects.toThrow('invalid')
    await expect(client.setConnectionCredential(projectId, connection.id, { purpose: 'refresh_token', value: 'private', expectedRevision: 1 }, options)).rejects.toThrow('invalid')
    await expect(client.registerDataset(projectId, { ...registration, mapping: { schema_version: 1, fields: [{ field_id: 'other', pointer: '/id', conversion: 'identity' }] } }, options)).rejects.toThrow('invalid')
    const fields = Array.from({ length: 100 }, (_, index) => ({ ...registration.definition.fields[0], id: `f${index}` }))
    await expect(client.registerDataset(projectId, { definition: { ...registration.definition, fields, key_fields: ['f0'] },
      mapping: { schema_version: 1, fields: fields.map((field) => ({ field_id: field.id, pointer: `/${'é'.repeat(900)}`, conversion: 'identity' })) } }, options)).rejects.toThrow('invalid')
    expect(() => validateDatasetRegistration({ ...registration, definition: { ...registration.definition, key_fields: [] } })).toThrow('invalid')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('rejects unbound resources and malformed evidence after a mutation as unknown dispatch', async () => {
    const { client, fetchImpl, options } = setup({ reference: { ...ref, id: 'wrong' }, revision: 1 })
    await expect(client.registerDataset(projectId, registration, options)).rejects.toMatchObject({ dispatched: true })
    fetchImpl.mockResolvedValueOnce(response({ ...ownerSummary, connector_ref: { ...ref, digest: 'b'.repeat(64) } }))
    await expect(client.createConnection(projectId, { id: connection.id, title: ownerSummary.title, connectorRef: ref, origin: ownerSummary.origin, operationIds: [operation.id] }, options)).rejects.toMatchObject({ dispatched: true })
    fetchImpl.mockResolvedValueOnce(response({ reference: ref, revision: 1, status: 'ready', receipt: { ...receipt, row_count: 251 } }))
    await expect(client.syncDataset(projectId, ref, options)).rejects.toMatchObject({ dispatched: true })
    fetchImpl.mockRejectedValueOnce(new Error('provider credential private'))
    await expect(client.setConnectionCredential(projectId, connection.id, { purpose: 'access_token', value: 'private', expectedRevision: 1 }, options)).rejects.toMatchObject({ dispatched: true, message: 'Integrations are unavailable. Please retry.' })
    expect(fetchImpl).toHaveBeenCalledTimes(4)
  })
  it.each([
    { dataset_ref: { ...ref, digest: 'b'.repeat(64) } },
    { actor_id: 'invalid' }, { binding_id: 'invalid' }, { operation_ref: null },
    { finished_at: '2026-10-19T00:00:00Z' }, { source_outcome: 'ok' },
    { failure_code: 'timeout' }, { outcome: 'failure', failure_code: 'timeout', row_count: 0 },
  ])('rejects malformed or inconsistent sync evidence %j', async (patch) => {
    const { client, options } = setup({ reference: ref, revision: 2, receipt: { ...receipt, ...patch }, status: 'ready' })
    await expect(client.syncDataset(projectId, ref, options)).rejects.toMatchObject({ dispatched: true })
  })
  it('accepts exact host operation reference arrays and rejects missing, duplicate and wrong-version references', async () => {
    for (const refs of [[], [operationRef, operationRef], [{ ...operationRef, version: 2 }], { [operation.id]: operationRef }]) {
      const { client, options } = setup({ ...connector, state: 'published',
        definition: { ...connectorDefinition, state: 'published' }, operationReferences: refs })
      await expect(client.getConnectorVersion(projectId, ref.id, 1, options)).rejects.toThrow('invalid')
    }
  })
  it('reads an unsynchronized dataset with null freshness and rejects another dataset receipt', async () => {
    const { client, fetchImpl, options } = setup({ reference: ref, revision: 1, title: 'Orders',
      fields: registration.definition.fields, lastAttempt: null, freshness: null })
    expect(await client.getDataset(projectId, ref, options)).toMatchObject({ lastAttempt: null, freshness: null })
    fetchImpl.mockResolvedValueOnce(response({ reference: ref, revision: 2, title: 'Orders', fields: registration.definition.fields,
      lastAttempt: { ...receipt, dataset_ref: { ...ref, id: 'other' } }, freshness: null }))
    await expect(client.getDatasetVersion(projectId, ref.id, ref.version, options)).rejects.toThrow('invalid')
  })
})
afterEach(() => vi.unstubAllGlobals())
function setup(value = scope) {
  const fetchImpl = vi.fn().mockImplementation(async () => response(value))
  return { client: createIntegrationsClient({ origin: 'https://ops.example', fetchImpl }), fetchImpl,
    options: { signal: new AbortController().signal } }
}
describe('fixed integration destination and bounded authenticated transport', () => {
  it.each([undefined, '', 'http://ops.example', 'https://ops.example/', 'https://user@ops.example', 'https://ops.example/path', 'https://ops.example?q=1', 'https://ops.example#x', 'https://ops.example\\evil'])('rejects origin %s', async (origin) => {
    expect(integrationOrigin(origin)).toBeNull()
    const fetchImpl = vi.fn()
    const client = createIntegrationsClient({ origin: origin ?? '', fetchImpl })
    await expect(client.getScope(projectId, { signal: new AbortController().signal })).rejects.toThrow('setup')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('uses only the deployment origin, caller signal, and fresh bearer on each request', async () => {
    const { client, fetchImpl, options } = setup()
    await client.getScope(projectId, options)
    authHeaders.mockReturnValue({ Authorization: 'Bearer rotated' })
    await client.getScope(projectId, options)
    expect(fetchImpl.mock.calls[0][0]).toBe(`https://ops.example/api/v1/projects/${projectId}/scope`)
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ signal: options.signal, credentials: 'omit', redirect: 'error', cache: 'no-store', headers: { Authorization: 'Bearer first' } })
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe('Bearer rotated')
    authHeaders.mockReturnValue({})
    await expect(client.getScope(projectId, options)).rejects.toMatchObject({ status: 401 })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
  it.each([{ ...scope, projectId: '22222222-2222-4222-8222-222222222222' }, { ...scope, projectRole: 'admin' }, { ...scope, bindingId: null }])('rejects unverified scope fields', async (value) => {
    const { client, options } = setup(value)
    await expect(client.getScope(projectId, options)).rejects.toThrow('invalid')
  })
  it.each([{ type: 'text/html' }, { raw: 'secret not json' }, { raw: 'x'.repeat(262145) }, { length: '262145' }])('bounds and validates JSON without reflecting bodies', async (settings) => {
    const { client, fetchImpl, options } = setup()
    fetchImpl.mockResolvedValue(response(scope, settings))
    await expect(client.getScope(projectId, options)).rejects.toThrow('invalid')
  })
  it('keeps service 401 nonfatal and errors sanitized', async () => {
    const { client, fetchImpl, options } = setup()
    const unauthorized = response(null, { status: 401, raw: 'bearer-secret' })
    fetchImpl.mockResolvedValue(unauthorized)
    await expect(client.getScope(projectId, options)).rejects.toMatchObject({ status: 401 })
    expect(noteUnauthorized).toHaveBeenCalledWith(unauthorized, 'integrations', 'Bearer first', { fatal: false })
    fetchImpl.mockRejectedValue(new Error('https://secret.example/token'))
    await expect(client.getScope(projectId, options)).rejects.toThrow('Integrations are unavailable. Please retry.')
  })
  it('uses finite dataset, pagination, create, and revision-aware update routes', async () => {
    const { client, fetchImpl, options } = setup({ records: [], nextCursor: null })
    await client.listDatasets(projectId, { ...options, after: 'opaque&cursor' })
    expect(fetchImpl.mock.calls[0][0]).toContain('/integrations/datasets?limit=25&after=opaque%26cursor')
    await client.listElements(projectId, options)
    expect(fetchImpl.mock.calls[1][0]).toContain('/integrations/elements?limit=25')
    fetchImpl.mockImplementation(async () => response({ element, revision: 2 }))
    await client.saveElement(projectId, element, options)
    expect(fetchImpl.mock.calls[2][1].method).toBe('POST')
    expect(JSON.parse(fetchImpl.mock.calls[2][1].body)).toEqual(element)
    await client.saveElement(projectId, element, { ...options, expectedRevision: 1 })
    expect(fetchImpl.mock.calls[3][1]).toMatchObject({ method: 'PUT', headers: { 'If-Match': '1' } })
    expect(fetchImpl.mock.calls[3][0]).toContain('/elements/orders-table')
    fetchImpl.mockImplementation(async () => response({ element, revision: 1 }))
    await client.getElement(projectId, element.id, { ...options, revision: 1 })
    expect(fetchImpl.mock.calls[4][0]).toContain('/elements/orders-table?revision=1')
    await expect(client.getElement(projectId, '../escape', options)).rejects.toThrow('invalid')
    await expect(client.listDatasets(projectId, { ...options, limit: 101 })).rejects.toThrow('invalid')
  })
  it('validates query and exact returned reference', async () => {
    const read = { schema_version: 1, dataset_ref: ref, fields: [{ id: 'count', label: 'Count', type: { kind: 'count' }, nullable: false, sensitivity: 'public' }],
      rows: [], next_cursor: null, status: 'empty', freshness: { source_updated_at: null, synced_at: null, fetched_at: '2026-10-20T00:00:00Z' } }
    const { client, fetchImpl, options } = setup(read)
    await client.queryDataset(projectId, ref, element.query, options)
    expect(fetchImpl.mock.calls[0][0]).toContain('/datasets/orders/versions/1/query')
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual(element.query)
    fetchImpl.mockImplementation(async () => response({ ...read, dataset_ref: { ...ref, digest: 'b'.repeat(64) } }))
    await expect(client.queryDataset(projectId, ref, element.query, options)).rejects.toThrow('invalid')
    await expect(client.queryDataset(projectId, ref, { ...element.query, execute: true }, options)).rejects.toThrow('invalid')
  })
  it('uses the same strict saved recipe service for actions and datasets', async () => {
    const { client, fetchImpl, options } = setup({ records: [{ element, revision: 1 }, { element: action, revision: 2 }], nextCursor: null })
    expect((await client.listElements(projectId, options)).records).toHaveLength(2)
    fetchImpl.mockImplementation(async () => response({ element: action, revision: 2 }))
    await client.saveElement(projectId, action, options)
    await client.saveElement(projectId, action, { ...options, expectedRevision: 1 })
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual(action)
    await client.getElement(projectId, action.id, { ...options, revision: 2 })
    await expect(client.getElement(projectId, action.id, { ...options, revision: 1 })).rejects.toThrow('invalid')
    await expect(client.getElement(projectId, element.id, options)).rejects.toThrow('invalid')
    await expect(client.saveElement(projectId, { ...action, execute: true }, options)).rejects.toThrow('invalid')
  })
  it('parses only safe connection controls, binds IDs, and preserves the signal', async () => {
    const { client, fetchImpl, options } = setup({ ...connection, title: 'Orders', cleanup: null, credential_purposes: ['api_key'] })
    expect(await client.getConnection(projectId, connection.id, options)).toEqual(connection)
    expect(fetchImpl.mock.calls[0][0]).toContain('/integrations/connections/orders-connection')
    expect(fetchImpl.mock.calls[0][1].signal).toBe(options.signal)
    fetchImpl.mockImplementation(async () => response({ ...connection, id: 'another' }))
    await expect(client.getConnection(projectId, connection.id, options)).rejects.toThrow('invalid')
    await expect(client.getConnection(projectId, '../escape', options)).rejects.toThrow('invalid')
  })
  it('serializes the identical key and arguments only on explicit submission calls; status is GET', async () => {
    const { client, fetchImpl, options } = setup({ status: 'admitted', run, replayed: false })
    await client.submitAction(projectId, Object.freeze(submission), options)
    await client.submitAction(projectId, submission, options)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl.mock.calls[0][0]).toContain('/integrations/actions')
    expect(fetchImpl.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual(submission)
    expect(fetchImpl.mock.calls[1][1].body).toBe(fetchImpl.mock.calls[0][1].body)
    fetchImpl.mockImplementation(async () => response({ run, receipts: [] }))
    await client.getAction(projectId, run.run_id, options)
    expect(fetchImpl.mock.calls[2][0]).toContain(`/integrations/actions/${run.run_id}`)
    expect(fetchImpl.mock.calls[2][1]).toMatchObject({ method: 'GET', signal: options.signal })
    expect(fetchImpl.mock.calls[2][1].body).toBeUndefined()
    await expect(client.getAction(projectId, '22222222-2222-4222-8222-222222222222', options)).rejects.toThrow('invalid')
  })
  it.each([
    { ...run, connection_id: 'another' }, { ...run, connector_ref: { ...ref, version: 2 } },
    { ...run, operation_ref: ref }, { ...run, grant_revision: 8 }, { ...run, secret: 'do not display' },
    { ...run, state: 'succeeded' },
  ])('rejects malformed or unbound action results without leaking the payload', async (value) => {
    const { client, fetchImpl, options } = setup({ status: 'admitted', run: value, replayed: false })
    await expect(client.submitAction(projectId, submission, options)).rejects.toThrow('The integration response or request was invalid.')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('does not retry failed action writes or dispatch an aborted caller', async () => {
    const { client, fetchImpl, options } = setup()
    fetchImpl.mockRejectedValue(new Error('private provider payload'))
    await expect(client.submitAction(projectId, submission, options)).rejects.toThrow('Integrations are unavailable. Please retry.')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const controller = new AbortController()
    controller.abort()
    await expect(client.submitAction(projectId, submission, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it.each([
    { run: { ...run, extra: 'private' }, receipts: [] },
    { run, receipts: [], extra: 'private' },
    { run: { ...run, state: 'succeeded', effect_outcome: 'applied' }, receipts: [] },
  ])('rejects noncanonical status evidence %j', async (snapshot) => {
    const { client, options } = setup(snapshot)
    await expect(client.getAction(projectId, run.run_id, options)).rejects.toThrow('The integration response or request was invalid.')
  })
})
