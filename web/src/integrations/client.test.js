import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextEncoder, TextDecoder } from 'node:util'
import { createIntegrationsClient, integrationOrigin } from './client.js'

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
  authHeaders.mockReturnValue({ Authorization: 'Bearer first' })
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
