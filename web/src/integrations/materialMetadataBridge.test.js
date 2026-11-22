import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { createIntegrationsClient } from './client.js'
import { authHeaders } from '../api.js'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))

// Actual wire contract: Ops producer458a6791, application-service.ts:1356-1361.
// Times below are synthetic fixture values for existing manifest clocks.
const project = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'fixture-material', version: 1, digest: 'a'.repeat(64) }
const lineage = { kind: 'project_material', material_id: project, sha256: 'b'.repeat(64), byte_length: 100 }
const root = { reference, revision: 3, title: 'Fixture material',
  fields: [{ id: 'key', label: 'Key', type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' }],
  material_lineage: lineage, lastAttempt: { outcome: 'success', row_count: 6 },
  freshness: { fetched_at: '2026-11-12T12:00:00Z', synced_at: null, source_updated_at: null } }
const context = { schema: 'leaf.project-material-context.v1', source: lineage,
  selection: { records_pointer: '/rows' }, row_count: 6,
  envelope: { title: 'Original context', rows: { schema: 'leaf.selected-records-reference.v1', records_pointer: '/rows', row_count: 6 } } }
const envelope = { ...root, material_context: context }
const copy = value => JSON.parse(JSON.stringify(value))

function setup(value, operation) {
  const fetchImpl = vi.fn(async () => {
    const bytes = new TextEncoder().encode(JSON.stringify(operation === 'list' ? { records: [value], nextCursor: null } : value))
    return { ok: true, status: 200, headers: { get: name => name === 'content-type' ? 'application/json' : null },
      body: new ReadableStream({ start(c) { c.enqueue(bytes); c.close() } }) }
  })
  return { fetchImpl, client: createIntegrationsClient({ origin: 'https://integrations.example', fetchImpl }),
    options: { signal: new AbortController().signal } }
}
async function read(client, operation, options) {
  if (operation === 'detail') return client.getDataset(project, reference, { ...options, revision: 3 })
  if (operation === 'version') return client.getDatasetVersion(project, reference.id, 1, options)
  return (await client.listDatasets(project, options)).records[0]
}
beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  authHeaders.mockReturnValue({ Authorization: 'Bearer fixture.invalid.signature' })
})
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

for (const operation of ['detail', 'version', 'list']) {
  it.each([['root array', root], ['selected envelope', envelope]])(`preserves actual material metadata on ${operation}: %s`, async (_, value) => {
    const { client, fetchImpl, options } = setup(value, operation)
    const result = await read(client, operation, options)
    expect(result).toEqual(value)
    expect(result.reference.digest).toBe(reference.digest)
    expect(result.freshness.synced_at).toBeNull()
    expect(result.freshness.source_updated_at).toBeNull()
    if (value === root) expect(result).not.toHaveProperty('material_context')
    expect(fetchImpl).toHaveBeenCalledOnce()
  })
}

const malformed = [
  ['null tag', value => { value.material_lineage = null }],
  ['wrong kind', value => { value.material_lineage.kind = 'connector' }],
  ['invalid material id', value => { value.material_lineage.material_id = 'foreign' }],
  ['invalid digest', value => { value.material_lineage.sha256 = 'G'.repeat(64) }],
  ['zero bytes', value => { value.material_lineage.byte_length = 0 }],
  ['too many bytes', value => { value.material_lineage.byte_length = 8388609 }],
  ['extra lineage key', value => { value.material_lineage.connector_ref = reference }],
  ['missing summary', value => { delete value.lastAttempt }],
  ['failed summary', value => { value.lastAttempt.outcome = 'failure' }],
  ['negative count', value => { value.lastAttempt.row_count = -1 }],
  ['too many rows', value => { value.lastAttempt.row_count = 100001 }],
  ['invented receipt field', value => { value.lastAttempt.dataset_ref = reference }],
  ['missing freshness', value => { delete value.freshness }],
  ['invented fetched clock', value => { value.freshness.fetched_at = 'not a clock' }],
  ['mixed derived evidence', value => { value.derivation = { kind: 'derived' } }],
  ['mixed derived clock', value => { value.computed_at = '2026-11-12T12:00:00Z' }],
  ['unknown field', value => { value.extra_evidence = true }],
]
for (const operation of ['detail', 'version', 'list']) {
  it.each(malformed)(`refuses malformed tagged material without raw fallback on ${operation}: %s`, async (_, mutate) => {
    const value = copy(root); mutate(value)
    const { client, fetchImpl, options } = setup(value, operation)
    await expect(read(client, operation, options)).rejects.toMatchObject({ status: 0 })
    expect(fetchImpl).toHaveBeenCalledOnce()
  })
  it.each([
    ['source binding', value => { value.material_context.source.sha256 = 'c'.repeat(64) }],
    ['material id', value => { value.material_context.source.material_id = '22222222-2222-4222-8222-222222222222' }],
    ['byte length', value => { value.material_context.source.byte_length = 101 }],
    ['row count', value => { value.lastAttempt.row_count = 5 }],
    ['marker count', value => { value.material_context.envelope.rows.row_count = 5 }],
  ])(`refuses inconsistent selected context on ${operation}: %s`, async (_, mutate) => {
    const value = copy(envelope); mutate(value)
    const { client, options } = setup(value, operation)
    await expect(read(client, operation, options)).rejects.toMatchObject({ status: 0 })
  })
}

for (const operation of ['detail', 'version']) {
  it.each(['id', 'version', 'digest'])(`retains outer reference checks on ${operation}: %s`, async key => {
    // Version lookup has no requested digest, but the exact-reference lookup does.
    const value = copy(root)
    value.reference[key] = key === 'version' ? 2 : key === 'id' ? 'foreign' : 'd'.repeat(64)
    const { client, options } = setup(value, operation)
    if (operation === 'version' && key === 'digest') {
      expect((await read(client, operation, options)).reference.digest).toBe('d'.repeat(64))
    } else await expect(read(client, operation, options)).rejects.toMatchObject({ status: 0 })
  })
  it('rejects an untagged material summary as connector detail on ' + operation, async () => {
    const value = copy(root); delete value.material_lineage
    const { client, options } = setup(value, operation)
    await expect(read(client, operation, options)).rejects.toMatchObject({ status: 0 })
  })
}
it('retains the exact requested resource revision for material detail', async () => {
  const { client, options } = setup({ ...root, revision: 4 }, 'detail')
  await expect(read(client, 'detail', options)).rejects.toMatchObject({ status: 0 })
})
