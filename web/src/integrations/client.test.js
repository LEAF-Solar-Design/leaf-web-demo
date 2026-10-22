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
const workflow = {
  definition: { schema_version: 1, id: 'orders', version: 1, title: 'Order workflow', trigger: 'manual', authority_mode: 'caller_bound',
    steps: [{ id: 'send', kind: 'action', connection_id: action.connection_id, connector_ref: ref, operation_ref: action.operation_ref, arguments: {} }] },
  run: { schema_version: 1, run_id: projectId, revision: 1, workflow_ref: ref, submission_digest: 'b'.repeat(64),
    authority_binding_digest: 'c'.repeat(64), authority_mode: 'caller_bound', created_at: run.created_at, updated_at: run.updated_at, state: 'accepted' },
  steps: [{ run_id: projectId, step_id: 'send', state: 'pending' }],
}
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
  it('revokes only the exact connection with DELETE, fresh If-Match and no body', async () => {
    const revoked = { ...ownerSummary, state: 'revoked', revision: 8, health: { checked_at: null, code: 'revoked' } }
    const { client, fetchImpl, options } = setup(revoked)
    expect(await client.revokeConnection(projectId, connection.id, 7, { ...options, body: { ignored: true }, method: 'POST' })).toEqual(revoked)
    const [url, request] = fetchImpl.mock.calls[0]
    expect(url).toMatch(new RegExp(`/api/v1/projects/${projectId}/integrations/connections/${connection.id}$`))
    expect(request).toMatchObject({ method: 'DELETE', signal: options.signal, credentials: 'omit', redirect: 'error',
      headers: { Authorization: 'Bearer first', 'If-Match': '7' } })
    expect(request).not.toHaveProperty('body')
    expect(request.headers).not.toHaveProperty('Content-Type')
    await expect(client.revokeConnection(projectId, connection.id, 0, options)).rejects.toMatchObject({ dispatched: false })
    fetchImpl.mockResolvedValueOnce(response({ ...revoked, id: 'different-connection' }))
    await expect(client.revokeConnection(projectId, connection.id, 7, options)).rejects.toMatchObject({ dispatched: true })
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
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
describe('manual workflow definition HTTP contract', () => {
  const entry = { connection_id: action.connection_id, connection_title: 'Orders', connector_ref: ref,
    operation_ref: action.operation_ref, title: 'Send', form: action.form }
  it('reads canonical bounded catalog pages only on request', async () => {
    const catalog = { actions: [entry], next_cursor: 'page_2' }
    const { client, fetchImpl, options } = setup(catalog)
    expect(await client.getActionCatalog(projectId, options)).toEqual(catalog)
    expect(fetchImpl.mock.calls[0][0]).toBe(`https://ops.example/api/v1/projects/${projectId}/integrations/action-catalog?limit=25`)
    await client.getActionCatalog(projectId, { ...options, after: 'page_2', limit: 50 })
    expect(fetchImpl.mock.calls[1][0]).toContain('?limit=50&after=page_2')
    expect(fetchImpl.mock.calls[1][1]).toMatchObject({ method: 'GET', credentials: 'omit', cache: 'no-store', signal: options.signal })
  })
  it.each([{ limit: 0 }, { limit: 51 }, { limit: 1.5 }, { after: '' }, { after: 'a=' }, { after: 'a\n' }, { after: 'a'.repeat(1025) }])('rejects catalog parameters before I/O: %j', async (query) => {
    const { client, fetchImpl, options } = setup()
    await expect(client.getActionCatalog(projectId, { ...options, ...query })).rejects.toMatchObject({ dispatched: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it.each([{ records: [], nextCursor: null }, { actions: [], next_cursor: 'bad=' }, { actions: Array(51).fill(entry), next_cursor: null },
    { actions: [{ ...entry, operation_ref: {} }], next_cursor: null }])('rejects malformed catalog: %j', async (value) => {
    const { client, options } = setup(value)
    await expect(client.getActionCatalog(projectId, options)).rejects.toMatchObject({ dispatched: false })
  })
  it('registers a plain definition and accepts only its plain immutable reference', async () => {
    const { client, fetchImpl, options } = setup(ref)
    expect(await client.registerManualWorkflow(projectId, workflow.definition, options)).toEqual(ref)
    expect(fetchImpl.mock.calls[0]).toEqual([`https://ops.example/api/v1/projects/${projectId}/integrations/manual-workflows`,
      expect.objectContaining({ method: 'POST', body: JSON.stringify(workflow.definition), signal: options.signal, credentials: 'omit', redirect: 'error' })])
  })
  it.each([{ reference: ref }, { ...ref, id: 'other' }, { ...ref, version: 2 }, { ...ref, digest: 'invalid' }])('marks invalid dispatched registration responses ambiguous: %j', async (value) => {
    const { client, options } = setup(value)
    await expect(client.registerManualWorkflow(projectId, workflow.definition, options)).rejects.toMatchObject({ dispatched: true })
  })
  it('bounds UTF-8 bytes before registration even when character counts fit', async () => {
    const { client, fetchImpl, options } = setup(ref)
    const input = { ...workflow.definition, steps: [{ ...workflow.definition.steps[0], arguments:
      Object.fromEntries(Array.from({ length: 9 }, (_, index) => [`field_${index}`, 'é'.repeat(4096)])) }] }
    expect(JSON.stringify(input).length).toBeLessThan(65536)
    expect(new TextEncoder().encode(JSON.stringify(input)).length).toBeGreaterThan(65536)
    await expect(client.registerManualWorkflow(projectId, input, options)).rejects.toMatchObject({ dispatched: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('rejects duplicate step IDs and unsafe versions before registration', async () => {
    const { client, fetchImpl, options } = setup(ref)
    for (const input of [{ ...workflow.definition, version: Number.MAX_SAFE_INTEGER + 1 },
      { ...workflow.definition, steps: [workflow.definition.steps[0], workflow.definition.steps[0]] }]) {
      await expect(client.registerManualWorkflow(projectId, input, options)).rejects.toMatchObject({ dispatched: false })
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})
describe('manual workflow start HTTP contract', () => {
  const input = { schema_version: 1, workflow_ref: ref, idempotency_key: projectId,
    step_grants: [{ step_id: 'send', expected_grant_revision: 7 }] }
  it('loads only an exact definition and submits canonical identical bodies with private transport', async () => {
    const { client, fetchImpl, options } = setup(workflow.definition)
    expect(await client.getManualWorkflowDefinition(projectId, ref, options)).toEqual(workflow.definition)
    expect(fetchImpl.mock.calls[0]).toEqual([
      `https://ops.example/api/v1/projects/${projectId}/integrations/manual-workflows/orders/versions/1?digest=${ref.digest}`,
      expect.objectContaining({ method: 'GET', signal: options.signal, cache: 'no-store', credentials: 'omit', redirect: 'error', headers: expect.objectContaining({ Authorization: 'Bearer first' }) }),
    ])
    fetchImpl.mockImplementation(async () => response({ status: 'admitted', run: workflow.run, replayed: false }))
    await client.submitManualWorkflow(projectId, input, options)
    await client.submitManualWorkflow(projectId, input, options)
    expect(fetchImpl.mock.calls[1][0]).toBe(`https://ops.example/api/v1/projects/${projectId}/integrations/manual-workflow-runs`)
    expect(fetchImpl.mock.calls[1][1]).toMatchObject({ method: 'POST', signal: options.signal, credentials: 'omit', redirect: 'error', cache: 'no-store', body: JSON.stringify(input) })
    expect(fetchImpl.mock.calls[2][1].body).toBe(fetchImpl.mock.calls[1][1].body)
  })
  it.each([{ arguments: {} }, { authority: {} }, { children: [] }, { step_grants: [{ step_id: 'send', expected_grant_revision: 0 }] }])('rejects invalid submissions before fetch: %j', async (patch) => {
    const { client, fetchImpl, options } = setup()
    await expect(client.submitManualWorkflow(projectId, { ...input, ...patch }, options)).rejects.toThrow('invalid')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it.each([{ id: '../bad' }, { version: 0 }, { digest: 'bad' }])('rejects invalid references before fetch: %j', async (patch) => {
    const { client, fetchImpl, options } = setup()
    await expect(client.getManualWorkflowDefinition(projectId, { ...ref, ...patch }, options)).rejects.toThrow('invalid')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it.each([{ id: 'another' }, { version: 2 }, { credentials: 'private' }])('binds strict definition identity: %j', async (patch) => {
    const { client, options } = setup({ ...workflow.definition, ...patch })
    await expect(client.getManualWorkflowDefinition(projectId, ref, options)).rejects.toThrow('invalid')
  })
  it('marks admitted reference mismatches as dispatched and preserves conflict privacy', async () => {
    const { client, fetchImpl, options } = setup({ status: 'admitted', run: { ...workflow.run, workflow_ref: { ...ref, digest: 'd'.repeat(64) } }, replayed: false })
    await expect(client.submitManualWorkflow(projectId, input, options)).rejects.toMatchObject({ dispatched: true })
    fetchImpl.mockImplementation(async () => response({ status: 'refused', code: 'idempotency_conflict' }))
    expect(await client.submitManualWorkflow(projectId, input, options)).toEqual({ status: 'refused', code: 'idempotency_conflict' })
    fetchImpl.mockImplementation(async () => response({ status: 'refused', code: 'idempotency_conflict', run_id: projectId }))
    await expect(client.submitManualWorkflow(projectId, input, options)).rejects.toThrow('invalid')
  })
  it('never retries lost submissions or dispatches an aborted intent', async () => {
    const { client, fetchImpl, options } = setup()
    fetchImpl.mockRejectedValue(new Error('private'))
    await expect(client.submitManualWorkflow(projectId, input, options)).rejects.toMatchObject({ dispatched: true })
    const controller = new AbortController()
    controller.abort()
    await expect(client.submitManualWorkflow(projectId, input, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchImpl).toHaveBeenCalledOnce()
  })
})

describe('existing manual workflow HTTP contract', () => {
  it('uses exact canonical GET, explicit resume and persisted-step reconcile with current bearer and empty bodies', async () => {
    const { client, fetchImpl, options } = setup(workflow)
    expect(await client.getManualWorkflowRun(projectId, projectId, options)).toEqual(workflow)
    authHeaders.mockReturnValue({ Authorization: 'Bearer second' })
    expect(await client.resumeManualWorkflowRun(projectId, projectId, {}, options)).toEqual(workflow)
    fetchImpl.mockResolvedValueOnce(response({ run, receipts: [] }))
    expect(await client.reconcileManualWorkflowStep(projectId, projectId, 'send', {}, options)).toEqual({ run, receipts: [] })
    const base = `https://ops.example/api/v1/projects/${projectId}/integrations/manual-workflow-runs/${projectId}`
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([base, `${base}/resume`, `${base}/steps/send/reconcile`])
    expect(fetchImpl.mock.calls[0][1]).toMatchObject({ method: 'GET', headers: { Authorization: 'Bearer first' } })
    expect(fetchImpl.mock.calls[0][1].body).toBeUndefined()
    for (const [, request] of fetchImpl.mock.calls.slice(1)) expect(request).toMatchObject({ method: 'POST', body: '{}', headers: { Authorization: 'Bearer second' } })
    for (const [, request] of fetchImpl.mock.calls) expect(request).toMatchObject({ credentials: 'omit', cache: 'no-store', redirect: 'error', signal: options.signal })
  })
  it('rejects invalid run/step identities and all caller authority or child fields before transport', async () => {
    const { client, fetchImpl, options } = setup()
    for (const id of ['', '../run', `${projectId}\n`]) {
      await expect(client.getManualWorkflowRun(projectId, id, options)).rejects.toThrow('invalid')
      await expect(client.resumeManualWorkflowRun(projectId, id, {}, options)).rejects.toThrow('invalid')
      await expect(client.reconcileManualWorkflowStep(projectId, id, 'send', {}, options)).rejects.toThrow('invalid')
    }
    for (const step of ['../send', '', 'send\n', 'x'.repeat(129)]) await expect(client.reconcileManualWorkflowStep(projectId, projectId, step, {}, options)).rejects.toThrow('invalid')
    for (const body of [undefined, null, { child_run_id: projectId }, { authority_binding_digest: 'c'.repeat(64) }]) {
      await expect(client.resumeManualWorkflowRun(projectId, projectId, body, options)).rejects.toThrow('invalid')
      await expect(client.reconcileManualWorkflowStep(projectId, projectId, 'send', body, options)).rejects.toThrow('invalid')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it.each(['get', 'resume', 'reconcile'])('bounds and parses every %s response without leaking response details', async (operation) => {
    const { client, fetchImpl, options } = setup()
    const call = () => operation === 'get' ? client.getManualWorkflowRun(projectId, projectId, options) : operation === 'resume'
      ? client.resumeManualWorkflowRun(projectId, projectId, {}, options) : client.reconcileManualWorkflowStep(projectId, projectId, 'send', {}, options)
    for (const result of [response({ secret: 'private' }), response(null, { raw: '{' }), response(null, { raw: 'x'.repeat(262145) }),
      response(null, { length: '262145' }), response(workflow, { type: 'text/html' })]) {
      fetchImpl.mockResolvedValueOnce(result)
      await expect(call()).rejects.toMatchObject({ message: 'The integration response or request was invalid.', dispatched: operation !== 'get' })
    }
    for (const status of [401, 403, 409, 500]) {
      fetchImpl.mockResolvedValueOnce(response({ secret: 'private' }, { status }))
      await expect(call()).rejects.toMatchObject({ status, dispatched: operation !== 'get' })
    }
  })
  it.each(['get', 'resume'])('rejects canonical but different requested run on %s', async (operation) => {
    const { client, options } = setup(workflow)
    const different = '22222222-2222-4222-8222-222222222222'
    await expect(operation === 'get' ? client.getManualWorkflowRun(projectId, different, options) :
      client.resumeManualWorkflowRun(projectId, different, {}, options)).rejects.toMatchObject({ dispatched: operation !== 'get' })
  })
})
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
