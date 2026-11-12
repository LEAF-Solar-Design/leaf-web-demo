import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { createIntegrationsClient } from './client.js'
import { authHeaders } from '../api.js'
import { serviceHeaders, serviceSession } from '../lib/serviceAuthSession.js'
import { ProjectMaterialDatasetDefinitionV1 } from '../../../vendor/assistant-contract/src/dataset-derivation'
import { TransformMappingV1 } from '../../../vendor/assistant-contract/src/integration'

vi.mock('../api.js', () => ({ authHeaders: vi.fn(), noteUnauthorized: vi.fn() }))

const local = 'https://platform-staging.leafdesign.ai'
const upstream = 'https://integrations.example'
const project = '11111111-1111-4111-8111-111111111111'
const selector = '22222222-2222-4222-8222-222222222222'
const reference = { id: 'fixture-accounts', version: 1, digest: 'a'.repeat(64) }
const keyField = { id: 'key', label: 'Key', type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' }
const material = { definition: { schema_version: 1, id: reference.id, title: 'Fixture accounts', version: 1,
  fields: [keyField], key_fields: ['key'], reporting_time_zone: 'UTC', retention_days: 30,
  lineage: { kind: 'project_material', material_id: project, sha256: 'b'.repeat(64), byte_length: 100 } },
  mapping: { schema_version: 1, fields: [{ field_id: 'key', pointer: '/id', conversion: 'identity' }] } }
const element = { schema_version: 1, id: 'fixture-account-view', title: 'Fixture accounts', dataset_ref: reference,
  query: { fields: ['key'], limit: 100 }, kind: 'dataset.table', props: { columns: ['key'], row_limit: 100 } }

function reply(value, status = 200) {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  return { status, ok: status >= 200 && status < 300,
    headers: { get: name => name === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(c) { c.enqueue(bytes); c.close() } }),
  }
}

function setup(operation) {
  const result = operation === 'import' ? { reference, revision: 1, rowCount: 6, status: 'complete' }
    : { element, revision: operation === 'update' ? 8 : 1 }
  const fetchImpl = vi.fn(async () => reply(result))
  return { fetchImpl, result, client: createIntegrationsClient({ origin: upstream, fetchImpl }),
    options: { signal: new AbortController().signal } }
}

function write(client, operation, options) {
  return operation === 'import' ? client.importProjectMaterial(project, material, options)
    : client.saveElement(project, element, operation === 'update' ? { ...options, expectedRevision: 7 } : options)
}

beforeEach(async () => {
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('location', { origin: local })
  const values = new Map()
  vi.stubGlobal('sessionStorage', { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) })
  const session = serviceSession()
  session.select(selector)
  expect(await session.probe({ origin: local, fetchImpl: async () => ({ ok: true,
    json: async () => ({ authenticated: true, binding_id: selector,
      expires_at: new Date(Date.now() + 60000).toISOString(),
      identity: { tenant_id: project, subject: 'fixture@clients', sub: 'fixture@clients',
        iss: 'https://issuer.example', principal_type: 'service', org_id: null } }),
  }) })).toBe(true)
  authHeaders.mockReturnValue(serviceHeaders())
})

afterEach(() => { serviceSession().human(); vi.clearAllMocks(); vi.unstubAllGlobals() })

it.each(['import', 'create', 'update'])('keeps %s body, authority and revision on the same-origin transport', async operation => {
  const { client, fetchImpl, options, result } = setup(operation)
  expect(await write(client, operation, options)).toEqual(result)
  expect(fetchImpl).toHaveBeenCalledOnce()
  const [target, init] = fetchImpl.mock.calls[0]
  const path = operation === 'import' ? 'dataset-imports'
    : 'elements' + (operation === 'update' ? '/' + element.id : '')
  expect(target).toBe(`/api/v1/projects/${project}/integrations/${path}`)
  expect(new URL(target, local).origin).toBe(local)
  expect(init).toMatchObject({ method: operation === 'update' ? 'PUT' : 'POST',
    credentials: 'same-origin', redirect: 'error', cache: 'no-store',
    headers: { Authorization: `LeafServiceSession ${selector}`, 'X-Tenant-Id': project,
      'X-Leaf-Integration-Origin': upstream, 'Content-Type': 'application/json' } })
  expect(JSON.parse(init.body)).toEqual(operation === 'import' ? material : element)
  expect(init.headers).not.toHaveProperty('Cookie')
  if (operation === 'update') expect(init.headers['If-Match']).toBe('7')
  else expect(init.headers).not.toHaveProperty('If-Match')
})

it('rejects a schema-valid material mapping over the byte bound before any write', async () => {
  const fields = Array.from({ length: 100 }, (_, i) => ({ ...keyField, id: i ? `field_${i}` : 'key' }))
  const input = { definition: { ...material.definition, fields }, mapping: { schema_version: 1,
    fields: fields.map(field => ({ field_id: field.id, pointer: '/' + 'é'.repeat(900), conversion: 'identity' })) } }
  expect(ProjectMaterialDatasetDefinitionV1.safeParse(input.definition).success).toBe(true)
  expect(TransformMappingV1.safeParse(input.mapping).success).toBe(true)
  expect(new TextEncoder().encode(JSON.stringify(input)).byteLength).toBeGreaterThan(163840)
  const { client, fetchImpl, options } = setup('import')
  await expect(client.importProjectMaterial(project, input, options)).rejects.toMatchObject({ status: 0, dispatched: false })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('rejects an invalid revision before dispatch', async () => {
  const { client, fetchImpl, options } = setup('update')
  await expect(client.saveElement(project, element, { ...options, expectedRevision: 0 }))
    .rejects.toMatchObject({ status: 0, dispatched: false })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it('rejects authority rotation between the verified snapshot and write headers', async () => {
  authHeaders.mockReturnValue({ Authorization: `LeafServiceSession ${project}` })
  const { client, fetchImpl, options } = setup('update')
  await expect(write(client, 'update', options)).rejects.toMatchObject({ status: 401, dispatched: false })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it.each(['create', 'update'])('does not retry a %s conflict', async operation => {
  const { client, fetchImpl, options } = setup(operation)
  fetchImpl.mockResolvedValue(reply({}, 409))
  await expect(write(client, operation, options)).rejects.toMatchObject({ status: 409, dispatched: true })
  expect(fetchImpl).toHaveBeenCalledOnce()
})

it.each(['import', 'create', 'update'])('does not retry an uncertain %s transport failure', async operation => {
  const { client, fetchImpl, options } = setup(operation)
  fetchImpl.mockRejectedValue(new TypeError('synthetic connection failure'))
  await expect(write(client, operation, options)).rejects.toMatchObject({ status: 0, dispatched: true })
  expect(fetchImpl).toHaveBeenCalledOnce()
})

it('does not submit an already-aborted update', async () => {
  const { client, fetchImpl } = setup('update')
  const controller = new AbortController()
  controller.abort()
  await expect(write(client, 'update', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(fetchImpl).not.toHaveBeenCalled()
})

it.each(['create', 'update'])('rejects a mismatched %s readback without automatically replaying the write', async operation => {
  const { client, fetchImpl, options } = setup(operation)
  fetchImpl.mockResolvedValue(reply({ element: { ...element, id: 'different-view' }, revision: 8 }))
  await expect(write(client, operation, options)).rejects.toMatchObject({ status: 0 })
  expect(fetchImpl).toHaveBeenCalledOnce()
})
