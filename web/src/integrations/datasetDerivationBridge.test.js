import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { createIntegrationsClient } from './client.js'
import { authHeaders } from '../api.js'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))

const project = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'fixture-counts', version: 1, digest: 'b'.repeat(64) }
const primary = { id: 'fixture-input', version: 1, digest: 'a'.repeat(64) }
const plan = { kind: 'group_count', group_by: [], count: { as: 'count', label: 'Count' },
  windows: [{ id: 'all', filters: [] }] }
const request = { schema_version: 1, output: { id: reference.id, version: 1, title: 'Counts' },
  inputs: { primary }, plan }
const capabilities = { schema_version: 1, contract: 'dataset-derivation-v1',
  operations: ['group_count', 'window_count'], max_inputs: 2, max_records_per_input: 100000, max_windows: 4 }
const freshness = { fetched_at: '2026-11-12T12:00:00Z', synced_at: null, source_updated_at: null }
const field = (id, kind) => ({ id, label: id, type: { kind }, nullable: false, sensitivity: 'restricted' })
const metadata = {
  reference, revision: 1, title: 'Counts', fields: [field('aggregate_key', 'identifier'),
    field('window_id', 'identifier'), field('count', 'count')],
  lastAttempt: { outcome: 'success', row_count: 1 }, freshness,
  derivation: { kind: 'derived', contract_version: 1, inputs: [{ alias: 'primary', dataset_ref: primary,
    resource_revision: 1, original_binding: { kind: 'project_material', material_id: project,
      sha256: 'c'.repeat(64), byte_length: 100 } }], plan,
    engine_revision: 'fixture-engine-v1', derivation_digest: reference.digest },
  input_freshness: [{ alias: 'primary', semantic_digest: 'd'.repeat(64), freshness }],
  diagnostics: { excluded_domain_rows: 0, null_event_time_rows: 0 }, computed_at: '2026-11-12T12:01:00Z',
}

function setup(value) {
  const fetchImpl = vi.fn(async () => {
    const bytes = new TextEncoder().encode(JSON.stringify(value))
    return { ok: true, status: 200,
      headers: { get: name => name === 'content-type' ? 'application/json' : null },
      body: new ReadableStream({ start(c) { c.enqueue(bytes); c.close() } }),
    }
  })
  return { fetchImpl, client: createIntegrationsClient({ origin: 'https://integrations.example', fetchImpl }),
    options: { signal: new AbortController().signal } }
}
beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  authHeaders.mockReturnValue({ Authorization: 'Bearer fixture.invalid.signature' })
})
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

it('reads exact capabilities through the finite resource method', async () => {
  const { client, fetchImpl, options } = setup(capabilities)
  expect(await client.getDatasetDerivationCapabilities(project, options)).toEqual(capabilities)
  expect(fetchImpl.mock.calls[0][0]).toBe(`https://integrations.example/api/v1/projects/${project}/integrations/dataset-derivations/capabilities`)
})

it('creates the typed derivation with the original bounded body and matching output reference', async () => {
  const result = { reference, revision: 1, rowCount: 1, status: 'complete' }
  const { client, fetchImpl, options } = setup(result)
  expect(await client.createDatasetDerivation(project, request, options)).toEqual(result)
  expect(fetchImpl).toHaveBeenCalledOnce()
  const [url, init] = fetchImpl.mock.calls[0]
  expect(url).toBe(`https://integrations.example/api/v1/projects/${project}/integrations/dataset-derivations`)
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual(request)
})

it.each([
  { ...request, sql: 'forbidden' },
  { ...request, output: { ...request.output, id: '../escape' } },
  { ...request, padding: 'x'.repeat(163840) },
])('refuses an invalid or oversized request before dispatch', async input => {
  const { client, fetchImpl, options } = setup({})
  await expect(client.createDatasetDerivation(project, input, options)).rejects.toMatchObject({ status: 0 })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('refuses a creation result for a different output without retrying', async () => {
  const { client, fetchImpl, options } = setup({ reference: { ...reference, id: 'foreign' },
    revision: 1, rowCount: 1, status: 'complete' })
  await expect(client.createDatasetDerivation(project, request, options)).rejects.toMatchObject({ dispatched: true })
  expect(fetchImpl).toHaveBeenCalledOnce()
})

it.each(['detail', 'version', 'list'])('preserves all typed derived evidence on %s', async operation => {
  const { client, options } = setup(operation === 'list' ? { records: [metadata], nextCursor: null } : metadata)
  const result = operation === 'detail' ? await client.getDataset(project, reference, options)
    : operation === 'version' ? await client.getDatasetVersion(project, reference.id, 1, options)
      : (await client.listDatasets(project, options)).records[0]
  expect(result).toEqual(metadata)
  expect(result.freshness.source_updated_at).toBeNull()
  expect(result.input_freshness[0].freshness.synced_at).toBeNull()
})

it.each([
  { ...metadata, derivation: undefined },
  { ...metadata, computed_at: null },
  { ...metadata, computed_at: 'invented clock' },
  { ...metadata, input_freshness: [] },
  { ...metadata, input_freshness: [...metadata.input_freshness, ...metadata.input_freshness] },
  { ...metadata, input_freshness: [{ ...metadata.input_freshness[0], alias: 'events' }] },
  { ...metadata, diagnostics: { ...metadata.diagnostics, excluded_domain_rows: 100001 } },
  { ...metadata, lastAttempt: { outcome: 'success', row_count: 100001 } },
  { ...metadata, lastAttempt: { outcome: 'failure', row_count: 0 } },
  { ...metadata, reference: { ...reference, digest: 'e'.repeat(64) } },
  { ...metadata, arbitrary_evidence: true },
])('rejects malformed derived evidence without falling back to the raw branch', async value => {
  const { client, options } = setup(value)
  await expect(client.getDatasetVersion(project, reference.id, 1, options)).rejects.toMatchObject({ status: 0 })
  const list = setup({ records: [value], nextCursor: null })
  await expect(list.client.listDatasets(project, list.options)).rejects.toMatchObject({ status: 0 })
})

it('retains strict connector sync receipts instead of admitting a derived summary as raw detail', async () => {
  const raw = { reference, revision: 1, title: 'Raw', fields: metadata.fields,
    lastAttempt: metadata.lastAttempt, freshness }
  const { client, options } = setup(raw)
  await expect(client.getDataset(project, reference, options)).rejects.toMatchObject({ status: 0 })
})
