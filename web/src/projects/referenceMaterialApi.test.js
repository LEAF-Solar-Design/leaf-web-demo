import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gzipSync } from 'node:zlib'
import { listProjectReferences, readProjectReferenceOriginal, uploadProjectReference } from './api.js'

const auth = vi.hoisted(() => ({ scope: 'synthetic-account-a' }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => auth.scope }))
vi.mock('../api.js', () => ({ config: { apiBase: 'https://reference.test' }, noteUnauthorized: vi.fn() }))
vi.mock('../controllers/workspace/createWorkspaceController.js', () => ({ WORKSPACE_ORG_KEY: 'leaf.org_id' }))

const project = '11111111-2222-4333-8444-555555555555'
const material = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
const cursor = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff'
const base = `https://reference.test/api/projects/${project}/reference-files`
const file = (name = 'notes.txt', type = 'text/plain') => new File(['original bytes'], name, { type })
const jsonResponse = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  auth.scope = 'synthetic-account-a'
  // Replace the entire storage surface; no real browser credentials are read.
  const values = { 'leaf.jwt': 'synthetic-reference-token', 'leaf.org_id': 'synthetic-org', 'leaf.actor_binding_id': 'synthetic-binding' }
  vi.stubGlobal('localStorage', { getItem: vi.fn(key => values[key] ?? null) })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ materials: [] })))
})
afterEach(() => vi.unstubAllGlobals())

describe('original reference transport', () => {
  it('uploads and downloads actual complete gzip bytes without changing the original', async () => {
    const compressed = gzipSync(Array.from({ length: 6001 }, (_, row) => JSON.stringify({ row })).join('\n'))
    const original = new File([compressed], 'BTC.jsonl.gz', { type: 'application/gzip' })
    fetch.mockResolvedValueOnce(jsonResponse({ material: { material_id: material, name: original.name } }))
    await uploadProjectReference(project, original, { expectedScope: auth.scope })
    expect(fetch.mock.calls[0][1].body).toBe(original)
    expect(fetch.mock.calls[0][1].headers['Content-Type']).toBe('application/gzip')
    fetch.mockResolvedValueOnce(new Response(compressed, { headers: { 'Content-Type': 'application/gzip' } }))
    const downloaded = await readProjectReferenceOriginal(project, material, { expectedScope: auth.scope })
    expect(new Uint8Array(await downloaded.arrayBuffer())).toEqual(new Uint8Array(compressed))
  })

  it.each([['rows.gz', 'application/gzip'], ['rows.jsonl.gz', 'application/json']])('refuses gzip suffix or MIME mismatch %s', (name, type) => {
    expect(() => uploadProjectReference(project, file(name, type))).toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([
    ['clip #1.MOV', 'video/quicktime', 'video/quicktime'],
    ['notes & plans.txt', 'text/plain;charset=utf-8', 'text/plain'],
    ['scene.json', 'application/json', 'application/json'],
    ['unknown.txt', '', 'text/plain'],
    ['binary.mov', 'application/octet-stream', 'video/quicktime'],
    ['BTC.JSONL.GZ', 'application/gzip', 'application/gzip'],
  ])('posts the raw File for %s with canonical content type', async (name, type, contentType) => {
    const original = file(name, type)
    const controller = new AbortController()
    const receipt = { material: { material_id: material, name } }
    fetch.mockResolvedValueOnce(jsonResponse(receipt))
    await expect(uploadProjectReference(project, original, { signal: controller.signal, expectedScope: auth.scope })).resolves.toEqual(receipt)
    expect(fetch).toHaveBeenCalledExactlyOnceWith(`${base}?filename=${encodeURIComponent(name)}`, {
      method: 'POST', body: original, signal: controller.signal,
      headers: {
        'Content-Type': contentType, Authorization: 'Bearer synthetic-reference-token',
        'X-Org-Id': 'synthetic-org', 'X-Actor-Binding-Id': 'synthetic-binding',
      },
    })
    expect(fetch.mock.calls[0][1].body).toBe(original)
  })

  it('uploads an Ops-sized JSON original without rewriting its File body', async () => {
    const prefix = '{"records":["', suffix = '"]}'
    const original = new File([prefix, 'x'.repeat(5224278 - prefix.length - suffix.length), suffix], 'project-input.json', { type: 'application/json' })
    expect(original.size).toBe(5224278)
    await uploadProjectReference(project, original, { expectedScope: auth.scope })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.calls[0][1].body).toBe(original)
    expect(fetch.mock.calls[0][1].headers['Content-Type']).toBe('application/json')
  })

  it('lists originals with an optional validated cursor and no browser cache', async () => {
    const page = { materials: [{ material_id: material }], next_cursor: cursor }
    const controller = new AbortController()
    fetch.mockResolvedValueOnce(jsonResponse(page))
    await expect(listProjectReferences(project, { signal: controller.signal, expectedScope: auth.scope })).resolves.toEqual(page)
    await listProjectReferences(project, { cursor, expectedScope: auth.scope })
    expect(fetch.mock.calls.map(call => call[0])).toEqual([base, `${base}?cursor=${cursor}`])
    for (const [, options] of fetch.mock.calls) {
      expect(options.method || 'GET').toBe('GET')
      expect(options.cache).toBe('no-store')
      expect(options.body).toBeUndefined()
      expect(options.expectedScope).toBeUndefined()
    }
    expect(fetch.mock.calls[0][1].signal).toBe(controller.signal)
  })

  it('reads original content as a Blob using authentication and no-store', async () => {
    const controller = new AbortController()
    fetch.mockResolvedValueOnce(new Response('original movie bytes', { headers: { 'Content-Type': 'video/quicktime' } }))
    const blob = await readProjectReferenceOriginal(project, material, { expectedScope: auth.scope, signal: controller.signal })
    expect(blob.type).toBe('video/quicktime')
    expect(await blob.text()).toBe('original movie bytes')
    expect(fetch).toHaveBeenCalledExactlyOnceWith(`${base}/${material}/content`, {
      signal: controller.signal, cache: 'no-store',
      headers: { Authorization: 'Bearer synthetic-reference-token', 'X-Org-Id': 'synthetic-org', 'X-Actor-Binding-Id': 'synthetic-binding' },
    })
    expect(fetch.mock.calls[0][0]).not.toContain('synthetic-reference-token')
  })

  it.each(['../foreign', '', null, 'not-a-uuid', `${project}/content`])('rejects malformed project %s for every operation before fetch', bad => {
    for (const invoke of [
      () => listProjectReferences(bad),
      () => uploadProjectReference(bad, file()),
      () => readProjectReferenceOriginal(bad, material),
    ]) expect(invoke).toThrow('project is not a valid id')
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['../foreign', 'not-a-uuid', `${material}?variant=original`])('rejects malformed material and cursor %s before fetch', bad => {
    expect(() => readProjectReferenceOriginal(project, bad)).toThrow('reference is not a valid id')
    expect(() => listProjectReferences(project, { cursor: bad })).toThrow('reference cursor is not a valid id')
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['', null, undefined])('rejects a missing material id %s before fetch', bad => {
    expect(() => readProjectReferenceOriginal(project, bad)).toThrow('reference is not a valid id')
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([
    ['notes.pdf', 'application/pdf', 1],
    ['notes.txt', 'application/json', 1],
    ['clip.mov', 'video/mp4', 1],
    ['scene.json', 'text/plain', 1],
    ['empty.txt', 'text/plain', 0],
    ['large.txt', 'text/plain', 8 * 1024 * 1024 + 1],
    ['large.json', 'application/json', 8 * 1024 * 1024 + 1],
    ['large.mov', 'video/quicktime', 64 * 1024 * 1024 + 1],
  ])('rejects invalid file %s (%s, %i bytes) before fetch', (name, type, size) => {
    const original = file(name, type)
    Object.defineProperty(original, 'size', { value: size })
    expect(() => uploadProjectReference(project, original)).toThrow(/Choose an original|extension and content type must agree/)
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['list', 'upload', 'read'])('fences %s before fetch when expectedScope no longer matches', async operation => {
    const expectedScope = auth.scope
    auth.scope = 'synthetic-account-b'
    const calls = {
      list: () => listProjectReferences(project, { expectedScope }),
      upload: () => uploadProjectReference(project, file(), { expectedScope }),
      read: () => readProjectReferenceOriginal(project, material, { expectedScope }),
    }
    await expect(calls[operation]()).rejects.toThrow('Your account changed. Open the project again.')
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['list', 'upload', 'read'])('preserves a real 403 permission failure for %s', async operation => {
    fetch.mockResolvedValueOnce(new Response('{}', { status: 403, headers: { 'Content-Type': 'application/json' } }))
    const calls = {
      list: () => listProjectReferences(project),
      upload: () => uploadProjectReference(project, file()),
      read: () => readProjectReferenceOriginal(project, material),
    }
    await expect(calls[operation]()).rejects.toMatchObject({ status: 403, message: 'You do not have permission to do that in this project.' })
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
