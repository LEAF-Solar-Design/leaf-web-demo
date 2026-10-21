import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TextDecoder, TextEncoder } from 'node:util'
import { ReadableStream } from 'node:stream/web'
import { IntegrationSetupPanel, datasetRegistration } from './IntegrationSetupPanel.jsx'
import { StrictMode } from 'react'
import { createIntegrationsClient } from './client.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer current' }), noteUnauthorized: vi.fn() }))
const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'sample-api', version: 1, digest: 'a'.repeat(64) }
const operationRef = { ...reference, id: 'normalized-read' }
const datasetRef = { ...reference, id: 'sample-data' }
const operation = { schema_version: 1, id: operationRef.id, title: 'Read normalized rows', method: 'GET', path: '/rows', effect: 'read',
  pagination: { kind: 'none', records_pointer: '' }, limits: { timeout_ms: 1000, max_response_bytes: 131072, max_records: 250, max_pages: 1 }, idempotency: { kind: 'read' } }
const definition = { schema_version: 1, id: reference.id, version: 1, title: 'Sample API', state: 'draft', protocol: 'https-rest',
  origins: ['https://source.example'], auth: { kind: 'bearer' }, operations: [operation] }
const connector = { id: reference.id, title: 'Sample API', state: 'draft', reference, revision: 1, auth_kind: 'bearer', operation_count: 1, draft_definition: definition }
const field = { id: 'key', label: 'Key', type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' }
const receipt = { id: projectId, actor_id: projectId, binding_id: projectId,
  started_at: '2026-10-20T00:00:00Z', finished_at: '2026-10-20T00:00:01Z',
  connector_ref: reference, operation_ref: operationRef, dataset_ref: datasetRef, transform_ref: { ...reference, id: 'sample-transform' },
  source_outcome: 'success', outcome: 'success', row_count: 1, failure_code: null }
const metadata = { reference: datasetRef, revision: 1, title: 'Sample data', fields: [field], lastAttempt: receipt,
  freshness: { source_updated_at: null, synced_at: null, fetched_at: '2026-10-20T00:00:00Z' } }
const summary = { id: 'sample-connection', title: 'Sample connection', revision: 1, state: 'setup', connector_ref: reference,
  origin: 'https://source.example', auth_kind: 'bearer', credential_purposes: [], operation_ids: [operation.id], grant_revision: 1,
  owner_id: projectId, provider_account_id: null, identity_verified: false, health: { checked_at: null, code: 'unknown' }, cleanup: 'complete' }
function response(value, status = 200) {
  return { ok: status === 200, status, headers: { get: (key) => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close() } }) }
}
function setup({ auth = 'bearer', syncStatus = 'ready' } = {}) {
  const controller = new AbortController()
  const draft = { ...connector, auth_kind: auth, draft_definition: { ...definition, auth: auth === 'api_key' ? { kind: auth, header: 'X-API-Key' } : { kind: auth } } }
  let connection = { ...summary, auth_kind: auth }
  const fetchImpl = vi.fn(async (url, request) => {
    if (url.endsWith('/connectors/import')) return response({ reference, revision: 1, issues: [] })
    if (url.endsWith('/connectors/sample-api')) return response(draft)
    if (url.endsWith('/publish')) return response({ reference, revision: 2 })
    if (url.endsWith('/connectors/sample-api/versions/1')) return response({ ...draft, state: 'published', revision: 2,
      definition: { ...draft.draft_definition, state: 'published' }, operationReferences: [operationRef] })
    if (url.endsWith('/connections')) return response(connection)
    if (url.endsWith('/credential')) { connection = { ...connection, revision: 2, credential_purposes: [auth === 'api_key' ? 'api_key' : 'access_token'] }; return response(connection) }
    if (url.endsWith('/connections/sample-connection')) return response(connection)
    if (url.endsWith('/datasets')) return response({ reference: datasetRef, revision: 1 })
    if (url.endsWith('/sync')) return response({ reference: datasetRef, revision: 2, status: syncStatus,
      receipt: syncStatus === 'ready' || syncStatus === 'empty' ? { ...receipt, row_count: syncStatus === 'empty' ? 0 : 1 } : { ...receipt, outcome: 'failure', source_outcome: 'failure', failure_code: 'timeout', row_count: 0 } })
    if (url.endsWith('/datasets/sample-data/versions/1')) return response(metadata)
    throw new Error('Unexpected request')
  })
  const props = { client: createIntegrationsClient({ origin: 'https://ops.example', fetchImpl }), projectId,
    scope: { projectRole: 'owner', platformUserId: projectId, bindingId: projectId }, signal: controller.signal,
    guard: vi.fn(async () => {}), onForbidden: vi.fn(), onDatasetReady: vi.fn(async () => {}) }
  return { props, fetchImpl, controller }
}
function change(label, value) { fireEvent.change(screen.getByLabelText(label), { target: { value } }) }
async function importAndPublish() {
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API')
  change('OpenAPI JSON', JSON.stringify({ openapi: '3.1.0', paths: { '/rows': { get: { operationId: 'documentIdMustNotBeUsed' } } } }))
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText(/Imported connector:/)
  change('Supported read operation', operation.id)
  fireEvent.click(screen.getByRole('button', { name: 'Publish selected read' }))
  await screen.findByRole('form', { name: 'Create connection' })
}
async function createConnection(auth = 'bearer') {
  await importAndPublish()
  change('Connection stable ID', summary.id); change('Connection title', summary.title)
  fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
  if (auth === 'none') return screen.findByRole('form', { name: 'Define typed dataset' })
  return screen.findByLabelText(auth === 'api_key' ? 'Header API key' : 'Bearer token')
}
async function configureCredential(auth = 'bearer') {
  const input = await createConnection(auth)
  fireEvent.change(input, { target: { value: 'private-test-value' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save private credential' }))
  await screen.findByRole('form', { name: 'Define typed dataset' })
  return input
}
function fillDataset() {
  change('Dataset stable ID', 'sample-data'); change('Dataset title', 'Sample data')
  change('Field 1 ID', 'key'); change('Field 1 label', 'Key'); change('Field 1 type', 'identifier'); change('Field 1 JSON pointer', '/id')
  fireEvent.click(screen.getByLabelText('Field 1 stable key'))
}
beforeEach(() => { vi.stubGlobal('TextDecoder', TextDecoder); vi.stubGlobal('TextEncoder', TextEncoder) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it.each(['bearer', 'api_key', 'none'])('walks a blank %s integration using normalized IDs and explicit provider sync', async (auth) => {
  const { props, fetchImpl } = setup({ auth })
  render(<IntegrationSetupPanel {...props} />)
  const secret = auth === 'none' ? null : await configureCredential(auth)
  if (auth === 'none') await createConnection(auth)
  if (secret) expect(secret.value).toBe('')
  fillDataset()
  expect(screen.getByLabelText('Dataset version')).toHaveValue(1)
  expect(screen.getByLabelText('Reporting timezone')).toHaveValue('UTC')
  expect(screen.getByLabelText('Retention days')).toHaveValue(30)
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/sync'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  await screen.findByRole('button', { name: 'Synchronize dataset' })
  const body = JSON.parse(fetchImpl.mock.calls.find(([url]) => url.endsWith('/datasets'))[1].body)
  expect(body.definition.lineage).toEqual({ connection_id: summary.id, operation_ref: operationRef })
  expect(body.mapping.fields).toEqual([{ field_id: 'key', pointer: '/id', conversion: 'identity' }])
  fireEvent.click(screen.getByRole('button', { name: 'Synchronize dataset' }))
  await screen.findByText(/Dataset status: ready/)
  expect(props.onDatasetReady).toHaveBeenCalledWith(datasetRef, expect.any(AbortSignal))
  expect(JSON.parse(fetchImpl.mock.calls.find(([url]) => url.endsWith('/publish'))[1].body).readOperationIds).toEqual([operation.id])
  expect(document.body.textContent).not.toContain('private-test-value')
})
it('keeps setup usable after the Studio StrictMode effect cleanup and replay', async () => {
  const { props } = setup()
  render(<StrictMode><IntegrationSetupPanel {...props} /></StrictMode>)
  await importAndPublish()
  expect(screen.getByRole('form', { name: 'Create connection' })).toBeInTheDocument()
})
it.each(['empty', 'stale', 'unavailable'])('renders %s synchronization without claiming fresh ready data', async (syncStatus) => {
  const { props } = setup({ auth: 'none', syncStatus })
  render(<IntegrationSetupPanel {...props} />)
  await createConnection('none'); fillDataset()
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Synchronize dataset' }))
  await screen.findByText(new RegExp(`Dataset status: ${syncStatus}`))
  expect(screen.queryByText(/Data is ready for the native/)).toBeNull()
  if (syncStatus !== 'empty') expect(screen.getByText(/Outcome: failure/)).toBeInTheDocument()
})
it('deduplicates immediately and keeps an unknown import blocked until readback is explicitly accepted', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  let reject
  fetchImpl.mockImplementation((url, request) => url.endsWith('/connectors/import') ? new Promise((_, fail) => { reject = fail }) : original(url, request))
  render(<IntegrationSetupPanel {...props} />)
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API'); change('OpenAPI JSON', '{"openapi":"3.0.3"}')
  const form = screen.getByRole('form', { name: 'Import connector JSON' })
  fireEvent.submit(form); fireEvent.submit(form)
  await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
  await act(async () => reject(new Error('private transport failure')))
  await screen.findByText(/Outcome unknown/)
  expect(screen.getByRole('button', { name: 'Import JSON' })).toBeDisabled()
  expect(screen.getByLabelText('Connector title')).toHaveValue('Sample API')
  fireEvent.click(screen.getByRole('button', { name: 'Read existing resource status' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use displayed existing resource' }))
  await screen.findByText(/Imported connector:/)
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/connectors/import'))).toHaveLength(1)
  expect(document.body.textContent).not.toContain('private transport failure')
})
it('clears private entry on unknown custody and requires readback plus explicit acceptance without reupload', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation(async (url, request) => {
    const result = await original(url, request)
    if (url.endsWith('/credential')) throw new Error('private-test-value')
    return result
  })
  render(<IntegrationSetupPanel {...props} />)
  const secret = await createConnection()
  fireEvent.change(secret, { target: { value: 'private-test-value' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save private credential' }))
  await screen.findByText(/Outcome unknown/)
  expect(secret).toHaveValue('')
  fireEvent.click(screen.getByRole('button', { name: 'Read existing resource status' }))
  await screen.findByText(/Configured custody cannot prove the submitted credential value/)
  fireEvent.click(screen.getByRole('button', { name: 'Use displayed existing resource' }))
  await screen.findByRole('form', { name: 'Define typed dataset' })
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/credential'))).toHaveLength(1)
  expect(document.body.textContent).not.toContain('private-test-value')
})
it('keeps a confirmed import locked for read-only recovery if its follow-up read fails', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, request) => url.endsWith('/connectors/sample-api') ?
    Promise.reject(new Error('read failed')) : original(url, request))
  render(<IntegrationSetupPanel {...props} />)
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API'); change('OpenAPI JSON', '{"openapi":"3.1.0"}')
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText(/Outcome unknown/)
  expect(screen.getByRole('button', { name: 'Import JSON' })).toBeDisabled()
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/connectors/import'))).toHaveLength(1)
})
it('requires recovery to match the exact reference returned by publication', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation(async (url, request) => {
    if (url.endsWith('/connectors/sample-api/versions/1')) return response({ ...connector, state: 'published', revision: 2,
      reference: { ...reference, digest: 'b'.repeat(64) }, definition: { ...definition, state: 'published' }, operationReferences: [operationRef] })
    return original(url, request)
  })
  render(<IntegrationSetupPanel {...props} />)
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API'); change('OpenAPI JSON', '{"openapi":"3.1.0"}')
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText(/Imported connector:/)
  change('Supported read operation', operation.id)
  fireEvent.click(screen.getByRole('button', { name: 'Publish selected read' }))
  await screen.findByText(/Outcome unknown/)
  fireEvent.click(screen.getByRole('button', { name: 'Read existing resource status' }))
  await screen.findByText(/The existing resource does not match this setup/)
  expect(screen.queryByRole('button', { name: 'Use displayed existing resource' })).toBeNull()
  expect(screen.queryByRole('form', { name: 'Create connection' })).toBeNull()
})
it('discloses mapping uncertainty and requires explicit adoption after unknown registration', async () => {
  const { props, fetchImpl } = setup({ auth: 'none' })
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, request) => url.endsWith('/datasets') ? Promise.reject(new Error('lost')) : original(url, request))
  render(<IntegrationSetupPanel {...props} />)
  await createConnection('none'); fillDataset()
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  await screen.findByText(/Outcome unknown/)
  fireEvent.click(screen.getByRole('button', { name: 'Read existing resource status' }))
  await screen.findByText(/Metadata cannot prove the submitted mapping/)
  expect(screen.queryByRole('button', { name: 'Synchronize dataset' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Use displayed existing resource' }))
  await screen.findByRole('button', { name: 'Synchronize dataset' })
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/datasets'))).toHaveLength(1)
})
it('cancels and clears a password immediately on context abort and ignores a late private response', async () => {
  const { props, fetchImpl, controller } = setup()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, request) => url.endsWith('/credential') ? new Promise((resolve) => { finish = resolve }) : original(url, request))
  const view = render(<IntegrationSetupPanel {...props} />)
  const secret = await createConnection()
  fireEvent.change(secret, { target: { value: 'private-test-value' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save private credential' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  act(() => controller.abort())
  expect(secret).toHaveValue('')
  expect(fetchImpl.mock.calls.find(([url]) => url.endsWith('/credential'))[1].signal.aborted).toBe(true)
  view.unmount()
  await act(async () => finish(response({ ...summary, revision: 2, credential_purposes: ['access_token'] })))
  expect(screen.queryByRole('form', { name: 'Define typed dataset' })).toBeNull()
})
it('clears private entry during a delayed authority preflight and never uploads after disposal', async () => {
  const { props, fetchImpl, controller } = setup()
  const view = render(<IntegrationSetupPanel {...props} />)
  const secret = await createConnection()
  let finish
  props.guard.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  fireEvent.change(secret, { target: { value: 'private-test-value' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Private credential' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  act(() => controller.abort())
  expect(secret).toHaveValue('')
  view.unmount()
  await act(async () => finish())
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/credential'))).toHaveLength(0)
})
it('clears private entry after refused preflight while retaining the configured nonsecret connection', async () => {
  const { props, fetchImpl } = setup()
  render(<IntegrationSetupPanel {...props} />)
  const secret = await createConnection()
  props.guard.mockRejectedValueOnce(new Error('private-test-value'))
  fireEvent.change(secret, { target: { value: 'private-test-value' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Private credential' }))
  await screen.findByText(/Setup could not continue/)
  expect(secret).toHaveValue('')
  expect(screen.getByText(/Connection: Sample connection/)).toBeInTheDocument()
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/credential'))).toHaveLength(0)
  expect(document.body.textContent).not.toContain('private-test-value')
})
it.each(['actor_id', 'binding_id', 'connector_ref', 'operation_ref'])('refuses a successful sync receipt with different %s attribution', async (key) => {
  const { props, fetchImpl } = setup({ auth: 'none' })
  const original = fetchImpl.getMockImplementation()
  const changed = key.endsWith('_ref') ? { ...reference, id: 'different' } : '22222222-2222-4222-8222-222222222222'
  fetchImpl.mockImplementation((url, request) => url.endsWith('/sync') ? Promise.resolve(response({ reference: datasetRef, revision: 2,
    status: 'ready', receipt: { ...receipt, [key]: changed } })) : original(url, request))
  render(<IntegrationSetupPanel {...props} />)
  await createConnection('none'); fillDataset()
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Synchronize dataset' }))
  await screen.findByText(/Outcome unknown/)
  expect(screen.queryByText(/Dataset status: ready/)).toBeNull()
  expect(props.onDatasetReady).not.toHaveBeenCalled()
})
it('recovers an unknown sync using only reads and explicitly adopts the latest recorded attempt', async () => {
  const { props, fetchImpl } = setup({ auth: 'none' })
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, request) => url.endsWith('/sync') ? Promise.reject(new Error('lost')) : original(url, request))
  render(<IntegrationSetupPanel {...props} />)
  await createConnection('none'); fillDataset()
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Synchronize dataset' }))
  await screen.findByText(/Outcome unknown/)
  fireEvent.click(screen.getByRole('button', { name: 'Read existing resource status' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use displayed existing resource' }))
  await screen.findByText(/Dataset status: readback/)
  expect(screen.getByText(/Latest recorded attempt; this does not prove/)).toBeInTheDocument()
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/sync'))).toHaveLength(1)
  expect(props.onDatasetReady).toHaveBeenCalledOnce()
})
it('rejects invalid or oversized input locally and uses fixed import diagnostics', async () => {
  const { props, fetchImpl } = setup()
  fetchImpl.mockResolvedValue(response({ reference: null, revision: null, issues: [{ code: 'unsupported_auth', summary: '<script>private</script>' }] }))
  render(<IntegrationSetupPanel {...props} />)
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API'); change('OpenAPI JSON', '{}')
  fireEvent.submit(screen.getByRole('form', { name: 'Import connector JSON' }))
  expect(fetchImpl).not.toHaveBeenCalled()
  change('OpenAPI JSON', JSON.stringify({ openapi: '3.0.3', description: 'é'.repeat(140000) }))
  fireEvent.submit(screen.getByRole('form', { name: 'Import connector JSON' }))
  expect(fetchImpl).not.toHaveBeenCalled()
  change('OpenAPI JSON', '{"openapi":"3.0.3"}')
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText('This authentication scheme is unsupported.')
  expect(document.body.textContent).not.toContain('<script>')
})
it.each(['editor', 'reviewer', 'read_only'])('does not expose setup to %s', (projectRole) => {
  const { props, fetchImpl } = setup()
  render(<IntegrationSetupPanel {...props} scope={{ projectRole }} />)
  expect(screen.queryByRole('heading', { name: 'Set up an HTTP dataset' })).toBeNull()
  expect(fetchImpl).not.toHaveBeenCalled()
})
it('rechecks authority before each mutation and sends nothing if the guard fails', async () => {
  const { props, fetchImpl } = setup()
  props.guard.mockRejectedValueOnce(new Error('private scope'))
  render(<IntegrationSetupPanel {...props} />)
  change('Connector stable ID', 'sample-api'); change('Connector title', 'Sample API'); change('OpenAPI JSON', '{"openapi":"3.0.3"}')
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText(/Setup could not continue/)
  expect(fetchImpl).not.toHaveBeenCalled()
  expect(screen.queryByText(/Outcome unknown/)).toBeNull()
})
it('validates every semantic type without inferring units, currency, time, stable keys or conversions', () => {
  const types = [{ kind: 'identifier' }, { kind: 'text' }, { kind: 'boolean' }, { kind: 'count' }, { kind: 'instant' }, { kind: 'date' },
    { kind: 'number', unit: 'kWh', precision: 2 }, { kind: 'ratio', scale: 'percent' }, { kind: 'money', currency: 'EUR', scale: 'minor' }, { kind: 'duration', unit: 'hours' }]
  const fields = types.map((type, i) => ({ id: `f${i}`, label: `Field ${i}`, ...type, nullable: false, sensitivity: 'internal',
    key: i === 0, pointer: `/f${i}`, conversion: i === 6 ? 'string_number' : i === 4 ? 'iso_instant' : 'identity', precision: type.precision ?? '' }))
  const form = { id: 'typed-data', title: 'Typed data', version: '1', timezone: 'UTC', retention: '30', timeField: 'f4', fields }
  expect(datasetRegistration(form, summary, operationRef).definition.fields.map((field) => field.type)).toEqual(types)
  expect(() => datasetRegistration({ ...form, fields: fields.map((field) => ({ ...field, key: false })) }, summary, operationRef)).toThrow()
  expect(() => datasetRegistration({ ...form, fields: fields.map((field) => field.kind === 'money' ? { ...field, currency: '' } : field) }, summary, operationRef)).toThrow()
  expect(() => datasetRegistration({ ...form, fields: fields.map((field) => ({ ...field, pointer: '/x'.repeat(33) })) }, summary, operationRef)).toThrow()
})
