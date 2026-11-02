import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readActiveScope } from './auth/activeScope.js'
import { listProjectSourceRequests, readProjectSourceRequest, stageProjectSource, confirmProjectSource,
  recoverProjectSource, rejectProjectSource, exportProjectSourceBundle } from './api.js'

const PROJECT = '11111111-1111-4111-8111-111111111111'
const REQUEST = '22222222-2222-4222-8222-222222222222'
const MATERIAL = '33333333-3333-4333-8333-333333333333'
const ORG = '44444444-4444-4444-8444-444444444444'
const authority = { session_id: 'shared-session', turn_id: 'shared-turn' }
const row = (overrides = {}) => ({ schema: 'leaf.native-project-source.v1', project_id: PROJECT,
  source_request_id: REQUEST, state: 'awaiting_confirmation', version: 1, receipt_digest: 'a'.repeat(64),
  base_commit: 'b'.repeat(40), source_commit: 'c'.repeat(40), source_tree: 'd'.repeat(40),
  changed_paths: ['Sources/ContentView.swift'], created_at: '2026-11-01T12:00:00Z', updated_at: '2026-11-01T12:00:00Z', ...overrides })
const body = () => ({ source_request_id: REQUEST, requirements: 'Create an iOS reading app from the transcript.',
  source_operation: 'create', material_refs: [{ material_id: MATERIAL, sha256: 'e'.repeat(64) }] })
const metadata = (overrides = {}) => ({ material_id: MATERIAL, project_id: PROJECT, org_id: ORG,
  actor_binding_id: ORG, name: 'Authentic transcript.txt', media_type: 'text/plain', byte_size: 1048576,
  sha256: 'e'.repeat(64), ...overrides })
const reply = (value) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const options = (extra = {}) => ({ expectedScope: readActiveScope(), sourceContext: 'native', authority, ...extra })
function signIn(subject = 'native-reader') {
  localStorage.setItem('leaf.jwt', `test.${btoa(JSON.stringify({ sub: subject }))}.test`)
  localStorage.setItem('leaf.org_id', ORG)
}
beforeEach(() => { localStorage.clear(); signIn() })
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('Native project source real API transport', () => {
  it('uses only fixed Native list, item and mutation routes with normal auth and unchanged wire bodies', async () => {
    const fetch = vi.fn(async (url, init) => reply(init.method === 'GET' && String(url).endsWith('/requests') ? [row()] : row()))
    vi.stubGlobal('fetch', fetch)
    expect(await listProjectSourceRequests(PROJECT, options())).toEqual([row()])
    expect(await readProjectSourceRequest(PROJECT, REQUEST, options())).toEqual(row())
    await stageProjectSource(PROJECT, body(), options({ materialMetadata: [metadata()] }))
    const witness = { receipt_digest: 'a'.repeat(64), expected_version: 1 }
    const confirmation = { ...witness, confirmation_id: MATERIAL }
    await confirmProjectSource(PROJECT, REQUEST, confirmation, options())
    await recoverProjectSource(PROJECT, REQUEST, witness, options())
    await rejectProjectSource(PROJECT, REQUEST, witness, options())
    const root = `/api/projects/${PROJECT}/native/source/requests`
    expect(fetch.mock.calls.map(([url]) => String(url))).toEqual([root, `${root}/${REQUEST}`, root,
      `${root}/${REQUEST}/confirm`, `${root}/${REQUEST}/recover`, `${root}/${REQUEST}/reject`])
    for (const [, init] of fetch.mock.calls) {
      expect(init).toMatchObject({ cache: 'no-store', redirect: 'error', headers: { Authorization: expect.stringMatching(/^Bearer /) } })
    }
    for (const [index, expected] of [[2, body()], [3, confirmation], [4, witness], [5, witness]]) {
      const init = fetch.mock.calls[index][1]
      expect(init.method).toBe('POST')
      expect(JSON.parse(init.body)).toEqual(expected)
      expect(init.headers).toMatchObject({ 'X-Authority-Session-Id': authority.session_id, 'X-Authority-Turn-Id': authority.turn_id })
    }
  })

  it.each(['ios', 'ios-swiftui-xcodegen-v1', '', null, {}, 'https://example.com/native'])('rejects unknown selectors before network: %j', async (sourceContext) => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(listProjectSourceRequests(PROJECT, options({ sourceContext }))).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([
    { schema: 'leaf.browser-project-source.v1' }, { project_id: MATERIAL }, { source_request_id: MATERIAL },
    { actor_id: ORG }, { version: 0 }, { receipt_digest: 'bad' }, { changed_paths: ['../secret'] },
  ])('rejects cross-profile, scope and malformed representations: %j', async (overrides) => {
    vi.stubGlobal('fetch', vi.fn(async () => reply(row(overrides))))
    await expect(readProjectSourceRequest(PROJECT, REQUEST, options())).rejects.toThrow()
  })

  it('keeps Browser default schema validation and refuses Native export even with a Browser candidate', async () => {
    const fetch = vi.fn(async () => reply([row()])); vi.stubGlobal('fetch', fetch)
    await expect(listProjectSourceRequests(PROJECT, { expectedScope: readActiveScope() })).rejects.toThrow()
    expect(String(fetch.mock.calls[0][0])).toContain('/browser/source/requests')
    fetch.mockClear()
    await expect(exportProjectSourceBundle(PROJECT, row({ schema: 'leaf.browser-project-source.v1', state: 'published' }), options())).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['auth', 'scope', 'abort', 'authority'])('refuses invalid %s before POST', async (change) => {
    const opts = options(), abort = new AbortController()
    if (change === 'auth') localStorage.removeItem('leaf.jwt')
    if (change === 'scope') signIn('other')
    if (change === 'abort') { abort.abort(); opts.signal = abort.signal }
    if (change === 'authority') opts.authority = { session_id: 'missing-turn' }
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(stageProjectSource(PROJECT, body(), opts)).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['scope', 'abort'])('fences a late %s response', async (change) => {
    let resolve
    const abort = new AbortController()
    vi.stubGlobal('fetch', vi.fn(() => new Promise((done) => { resolve = done })))
    const result = readProjectSourceRequest(PROJECT, REQUEST, options({ signal: abort.signal }))
    const rejected = expect(result).rejects.toThrow()
    if (change === 'scope') signIn('other')
    else abort.abort()
    resolve(reply(row()))
    await rejected
  })

  it.each([
    { material_refs: [] }, { material_refs: Array(9).fill({ material_id: MATERIAL, sha256: 'e'.repeat(64) }) },
    { source_operation: 'edit', expected_base_commit: 'b'.repeat(40) }, { expected_base_commit: 'b'.repeat(40) },
    { actor_id: ORG }, { requirements: 'x'.repeat(16385) },
  ])('refuses invalid Native create shapes without network: %j', async (overrides) => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(stageProjectSource(PROJECT, { ...body(), ...overrides }, options())).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([{ byte_size: 1048577 }, { byte_size: 0 }, { byte_size: -1 }, { media_type: 'video/quicktime' },
    { project_id: MATERIAL }, { sha256: 'f'.repeat(64) }, { name: '' }])('checks supplied reference metadata: %j', async (overrides) => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(stageProjectSource(PROJECT, body(), options({ materialMetadata: [metadata(overrides)] }))).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('rejects supplied aggregate sizes above 4 MiB', async () => {
    const rows = Array.from({ length: 5 }, (_, index) => metadata({ material_id: `55555555-5555-4555-8555-${String(index + 1).padStart(12, '0')}` }))
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(stageProjectSource(PROJECT, { ...body(), material_refs: rows.map(({ material_id, sha256 }) => ({ material_id, sha256 })) },
      options({ materialMetadata: rows }))).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('retains credential refusal and bounds list and response shapes', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(stageProjectSource(PROJECT, { ...body(), requirements: `api_key: ${'x'.repeat(24)}` }, options())).rejects.toMatchObject({ secretRefused: true })
    expect(fetch).not.toHaveBeenCalled()
    fetch.mockResolvedValueOnce(reply([row(), row()]))
    await expect(listProjectSourceRequests(PROJECT, options())).rejects.toThrow()
    fetch.mockResolvedValueOnce(new Response('x'.repeat(262145)))
    await expect(listProjectSourceRequests(PROJECT, options())).rejects.toThrow()
  })
})
