import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { createIntegrationsClient, validateMaterialImport } from './client.js'
import { authHeaders } from '../api.js'
import { ProjectMaterialDatasetDefinitionV1 } from '../../../vendor/assistant-contract/src/dataset-derivation'
import { TransformMappingV1 } from '../../../vendor/assistant-contract/src/integration'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))
const project = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'fixture-import', version: 1, digest: 'a'.repeat(64) }
const field = id => ({ id, label: id, type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' })
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).byteLength
function fixture(target, multibyte = false) {
  const fields = Array.from({ length: target ? 100 : 1 }, (_, i) => field(i ? `field_${i}` : 'key'))
  const input = { definition: { schema_version: 1, id: reference.id, title: 'Fixture import', version: 1,
    fields, key_fields: ['key'], reporting_time_zone: 'UTC', retention_days: 30,
    lineage: { kind: 'project_material', material_id: project, sha256: 'b'.repeat(64), byte_length: 100 } },
    mapping: { schema_version: 1, fields: fields.map(f => ({ field_id: f.id,
      pointer: multibyte ? '/é' : '/id', conversion: 'identity' })) } }
  if (target) {
    let remaining = target - bytes(input)
    expect(remaining).toBeGreaterThan(0)
    for (const mapping of input.mapping.fields) {
      const add = Math.min(remaining, 900 - mapping.pointer.length)
      mapping.pointer += 'x'.repeat(add)
      remaining -= add
    }
    expect(remaining).toBe(0)
    expect(bytes(input)).toBe(target)
  }
  expect(ProjectMaterialDatasetDefinitionV1.safeParse(input.definition).success).toBe(true)
  expect(TransformMappingV1.safeParse(input.mapping).success).toBe(true)
  return input
}
function setup() {
  const fetchImpl = vi.fn(async () => {
    const encoded = new TextEncoder().encode(JSON.stringify({ reference, revision: 1, rowCount: 6, status: 'complete' }))
    return { ok: true, status: 200, headers: { get: name => name === 'content-type' ? 'application/json' : null },
      body: new ReadableStream({ start(c) { c.enqueue(encoded); c.close() } }) }
  })
  return { fetchImpl, client: createIntegrationsClient({ origin: 'https://integrations.example', fetchImpl }),
    options: { signal: new AbortController().signal } }
}
beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder); vi.stubGlobal('TextDecoder', TextDecoder)
  authHeaders.mockReturnValue({ Authorization: 'Bearer fixture.invalid.signature' })
})
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

it.each([undefined, 65536])('validates and dispatches one supported import at bytes=%s', async size => {
  const input = fixture(size)
  expect(validateMaterialImport(input)).toEqual(input)
  const { client, fetchImpl, options } = setup()
  expect(await client.importProjectMaterial(project, input, options)).toEqual({ reference, revision: 1, rowCount: 6, status: 'complete' })
  expect(fetchImpl).toHaveBeenCalledOnce()
  const [url, init] = fetchImpl.mock.calls[0]
  expect(url).toBe(`https://integrations.example/api/v1/projects/${project}/integrations/dataset-imports`)
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body)).toEqual(input)
  expect(new TextEncoder().encode(init.body).byteLength).toBe(bytes(input))
})
it.each([false, true])('rejects schema-valid 65537-byte import before dispatch, multibyte=%s', async multibyte => {
  const input = fixture(65537, multibyte)
  if (multibyte) expect(JSON.stringify(input).length).toBeLessThan(65536)
  expect(() => validateMaterialImport(input)).toThrow()
  const { client, fetchImpl, options } = setup()
  await expect(client.importProjectMaterial(project, input, options)).rejects.toMatchObject({ status: 0, dispatched: false })
  expect(fetchImpl).not.toHaveBeenCalled()
})
