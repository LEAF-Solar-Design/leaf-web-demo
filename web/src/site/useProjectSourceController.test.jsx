import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import useProjectSourceController from './useProjectSourceController.js'
import { createCatalogController } from '../controllers/catalog/createCatalogController.js'
import { readActiveScope, notifyActiveScopeChanged } from '../auth/activeScope.js'
import { listProjectSourceRequests, stageProjectSource, validateProjectSource, exportProjectSourceBundle } from '../api.js'

const PROJECT = '11111111-1111-4111-8111-111111111111'
const OTHER = '22222222-2222-4222-8222-222222222222'
const ORG = '33333333-3333-4333-8333-333333333333'
const REQUEST = '44444444-4444-4444-8444-444444444444'
const AUTHORITY = { session_id: 'actual-session', turn_id: 'actual-turn' }
const brief = '  Create an accessible project page.\nKeep my original wording.  '
const row = (overrides = {}) => ({ schema: 'leaf.browser-project-source.v1', project_id: PROJECT,
  source_request_id: REQUEST, state: 'awaiting_confirmation', version: 1, receipt_digest: 'a'.repeat(64),
  base_commit: 'b'.repeat(40), source_commit: 'c'.repeat(40), source_tree: 'd'.repeat(40),
  changed_paths: ['app/page.jsx'], created_at: '2026-11-01T12:00:00Z', updated_at: '2026-11-01T12:00:00Z', ...overrides })
const material = (overrides = {}) => ({ material_id: OTHER, org_id: ORG, project_id: PROJECT,
  actor_binding_id: ORG, name: 'Original 6.9 MB.txt', media_type: 'text/plain', byte_size: 6900000,
  sha256: 'e'.repeat(64), created_at: '2026-11-01T12:00:00Z', ...overrides })
const reply = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
function signIn(subject = 'person') {
  localStorage.setItem('leaf.jwt', `test.${btoa(JSON.stringify({ sub: subject, iss: 'test' }))}.test`)
  localStorage.setItem('leaf.org_id', ORG)
}
function setup({ materials = [], saved = [], mint, props: extra = {}, fetcher } = {}) {
  const calls = [], events = []
  let durable = saved
  const fetch = vi.fn(async (url, init = {}) => {
    calls.push({ url: String(url), ...init }); events.push(init.method === 'POST' ? 'post' : 'read')
    if (fetcher) {
      const value = await fetcher(String(url), init)
      if (value !== undefined) return value
    }
    if (String(url).includes('/reference-files')) return reply({ materials, next_cursor: null })
    if (init.method === 'POST') {
      const body = JSON.parse(init.body)
      const previous = durable.find((item) => item.source_request_id === body.source_request_id) || durable[0]
      const next = row({ source_request_id: body.source_request_id || previous?.source_request_id,
        ...(String(url).includes('/native/source/') ? { schema: 'leaf.native-project-source.v1', changed_paths: ['Sources/ContentView.swift'] } : {}),
        ...(String(url).endsWith('/confirm') || String(url).endsWith('/recover') ? { state: 'published', version: 2 } : {}),
        ...(String(url).endsWith('/reject') ? { state: 'rejected', version: 2 } : {}) })
      durable = [next]; return reply(next)
    }
    const id = String(url).split('/requests/')[1]
    return id ? reply(durable.find((item) => item.source_request_id === id) || {}, durable.some((item) => item.source_request_id === id) ? 200 : 404) : reply(durable)
  })
  vi.stubGlobal('fetch', fetch)
  const startAgentTurn = vi.fn(async (...args) => { events.push('mint'); return mint ? mint(...args) : AUTHORITY })
  const props = { projectId: PROJECT, orgId: ORG, signedIn: true, mock: false, startAgentTurn, ...extra }
  const hook = renderHook((value) => useProjectSourceController(value), { initialProps: props })
  return { ...hook, props, calls, events, fetch, startAgentTurn, setDurable: (value) => { durable = value } }
}
async function open(hook, text = brief) {
  act(() => { hook.result.current.open(text) })
  await waitFor(() => expect(hook.result.current.loading || hook.result.current.materialsLoading).toBe(false))
}
beforeEach(() => { localStorage.clear(); signIn() })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear() })

const bundleBytes = new TextEncoder().encode(`# v2 git bundle\n${'c'.repeat(40)} refs/heads/main\n\nPACK\u0000\u0000\u0000\u0002`)
const bundleHash = createHash('sha256').update(bundleBytes).digest('hex')
function bundleReply({ headers = {}, body = bundleBytes, status = 200 } = {}) {
  return new Response(body, { status, headers: {
    'Content-Type': 'application/octet-stream', 'Content-Length': String(bundleBytes.byteLength),
    'Content-Disposition': `attachment; filename="project-source-${'c'.repeat(40)}.bundle"`,
    'X-Leaf-Source-Commit': 'c'.repeat(40), 'X-Leaf-Source-Tree': 'd'.repeat(40),
    'X-Leaf-Bundle-Sha256': bundleHash, 'X-Leaf-Source-Receipt-Digest': 'a'.repeat(64),
    'X-Leaf-Source-Version': '1', 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', ...headers,
  } })
}
function downloadEnvironment() {
  vi.stubGlobal('crypto', webcrypto)
  const NativeURL = URL
  const create = vi.fn(() => 'blob:verified-source')
  const revoke = vi.fn()
  vi.stubGlobal('URL', class extends NativeURL {
    static createObjectURL = create
    static revokeObjectURL = revoke
  })
  return { create, revoke }
}

describe('Verified source bundle download through the real API', () => {
  it('hashes actual streamed bytes and sends only frozen published witnesses using normal auth', async () => {
    downloadEnvironment()
    const fetch = vi.fn(async () => bundleReply())
    vi.stubGlobal('fetch', fetch)
    const value = await exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })
    expect(value.sha256).toBe(bundleHash)
    expect(value.blob.size).toBe(bundleBytes.byteLength)
    expect(value.filename).toBe(`project-source-${'c'.repeat(40)}.bundle`)
    const [url, init] = fetch.mock.calls[0]
    const parsed = new URL(url, 'https://app.example')
    expect(parsed.pathname).toBe(`/api/projects/${PROJECT}/browser/source/requests/${REQUEST}/export`)
    expect(Object.fromEntries(parsed.searchParams)).toEqual({ source_commit: 'c'.repeat(40), source_tree: 'd'.repeat(40), receipt_digest: 'a'.repeat(64), version: '1' })
    expect(init).toMatchObject({ method: 'GET', cache: 'no-store', redirect: 'error', headers: { 'Authorization': expect.stringMatching(/^Bearer /) } })
    expect(init.body).toBeUndefined()
  })

  it.each([
    { 'Content-Length': '67108865' }, { 'Content-Length': '0' }, { 'Content-Length': '01' },
    { 'Content-Length': 'garbage' }, { 'Content-Length': '2' }, { 'Content-Type': 'application/zip' },
    { 'Content-Disposition': 'attachment; filename="bad.zip"' }, { 'X-Leaf-Bundle-Sha256': '0'.repeat(64) },
    { 'X-Leaf-Source-Commit': 'e'.repeat(40) }, { 'X-Leaf-Source-Tree': 'f'.repeat(40) },
    { 'X-Leaf-Source-Version': '2' }, { 'X-Leaf-Source-Receipt-Digest': 'b'.repeat(64) },
    { 'Content-Encoding': 'gzip' },
  ])('rejects malformed, oversized or mismatched binary metadata: %j', async (headers) => {
    downloadEnvironment()
    vi.stubGlobal('fetch', vi.fn(async () => bundleReply({ headers })))
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })).rejects.toThrow()
  })

  it('rejects altered actual bytes despite valid-looking headers', async () => {
    downloadEnvironment()
    const altered = bundleBytes.slice(); altered[altered.length - 1] ^= 1
    vi.stubGlobal('fetch', vi.fn(async () => bundleReply({ body: altered })))
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })).rejects.toThrow()
  })

  it('caps streamed bytes and cancels before trusting a misleading length', async () => {
    downloadEnvironment()
    const cancel = vi.fn()
    let reads = 0
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, headers: bundleReply({ headers: { 'Content-Length': '1' } }).headers,
      body: { getReader: () => ({ read: async () => { reads += 1; return { done: false, value: bundleBytes } }, cancel, releaseLock: vi.fn() }) } })))
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })).rejects.toThrow()
    expect(reads).toBe(1)
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('rejects invalid declared length before opening the body reader', async () => {
    downloadEnvironment()
    const getReader = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true,
      headers: bundleReply({ headers: { 'Content-Length': '67108865' } }).headers, body: { getReader } })))
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })).rejects.toThrow()
    expect(getReader).not.toHaveBeenCalled()
  })

  it.each(['scope', 'abort'])('rechecks %s after body bytes arrive', async (change) => {
    downloadEnvironment()
    const entered = deferred(), late = deferred(), cancel = vi.fn()
    const abort = new AbortController()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, headers: bundleReply().headers,
      body: { getReader: () => ({ read: () => { entered.resolve(); return late.promise }, cancel, releaseLock: vi.fn() }) } })))
    const result = exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope(), signal: abort.signal })
    const rejected = expect(result).rejects.toThrow()
    await entered.promise
    if (change === 'scope') signIn('other')
    else abort.abort()
    late.resolve({ done: false, value: bundleBytes })
    await rejected
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it.each(['scope', 'abort'])('rechecks %s after the actual digest completes', async (change) => {
    downloadEnvironment()
    const entered = deferred(), late = deferred()
    const abort = new AbortController()
    vi.stubGlobal('crypto', { subtle: { digest: async (...args) => {
      entered.resolve(); await late.promise; return webcrypto.subtle.digest(...args)
    } } })
    vi.stubGlobal('fetch', vi.fn(async () => bundleReply()))
    const result = exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope(), signal: abort.signal })
    const rejected = expect(result).rejects.toThrow()
    await entered.promise
    if (change === 'scope') signIn('other')
    else abort.abort()
    late.resolve()
    await rejected
  })

  it('accepts a bounded inert arrayBuffer response and verifies its actual digest', async () => {
    downloadEnvironment()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, headers: bundleReply().headers,
      arrayBuffer: async () => bundleBytes.slice().buffer })))
    const result = await exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })
    expect(result.sha256).toBe(bundleHash)
  })

  it.each([403, 409, 500, 503])('uses fixed human errors for HTTP %s without reading provider diagnostics', async (status) => {
    downloadEnvironment()
    vi.stubGlobal('fetch', vi.fn(async () => bundleReply({ status, body: 'PRIVATE_PROVIDER_LEASE' })))
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope() })).rejects.toThrow(/Project source/)
  })

  it('refuses unpublished candidates and already-aborted requests before any GET', async () => {
    downloadEnvironment()
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(exportProjectSourceBundle(PROJECT, row(), { expectedScope: readActiveScope() })).rejects.toThrow()
    const abort = new AbortController(); abort.abort()
    await expect(exportProjectSourceBundle(PROJECT, row({ state: 'published' }), { expectedScope: readActiveScope(), signal: abort.signal })).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('fences duplicate clicks, never mints or posts, and revokes on replacement and close', async () => {
    const { create, revoke } = downloadEnvironment()
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?') ? bundleReply() : undefined })
    await open(h)
    const witness = h.result.current.candidate
    await act(async () => { await Promise.all([h.result.current.prepareDownload(witness), h.result.current.prepareDownload(witness)]) })
    expect(h.calls.filter((call) => call.url.includes('/export?'))).toHaveLength(1)
    expect(create).toHaveBeenCalledTimes(1)
    expect(h.result.current.download.sha256).toBe(bundleHash)
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
    await act(async () => { await h.result.current.prepareDownload(witness) })
    expect(revoke).toHaveBeenCalledWith('blob:verified-source')
    act(() => h.result.current.close())
    expect(h.result.current.download).toBeNull()
    expect(revoke).toHaveBeenCalledTimes(2)
  })

  it('preserves the original candidate and intent when a download is rejected', async () => {
    const { create, revoke } = downloadEnvironment()
    let tamper = false
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?')
      ? bundleReply({ headers: tamper ? { 'X-Leaf-Bundle-Sha256': '0'.repeat(64) } : {} }) : undefined })
    await open(h)
    const selected = h.result.current.candidate
    const intent = h.result.current.intent
    await act(async () => { await h.result.current.prepareDownload(selected) })
    tamper = true
    await act(async () => { await h.result.current.prepareDownload(selected) })
    expect(h.result.current.candidate).toEqual(selected)
    expect(h.result.current.intent).toBe(intent)
    expect(h.result.current.download).toBeNull()
    expect(h.result.current.error).toMatch(/unavailable/)
    expect(create).toHaveBeenCalledTimes(1)
    expect(revoke).toHaveBeenCalledWith('blob:verified-source')
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    const before = h.calls.length
    await act(async () => { await h.result.current.prepareDownload({ ...selected, version: 9 }) })
    expect(h.calls).toHaveLength(before)
  })

  it.each(['account', 'project', 'close', 'candidate'])('discards late bytes after %s changes', async (change) => {
    const { create } = downloadEnvironment()
    const late = deferred()
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?') ? late.promise : undefined })
    await open(h)
    let action
    act(() => { action = h.result.current.prepareDownload(h.result.current.candidate) })
    if (change === 'account') act(() => { signIn('other'); notifyActiveScopeChanged() })
    if (change === 'project') h.rerender({ ...h.props, projectId: OTHER })
    if (change === 'close') act(() => h.result.current.close())
    if (change === 'candidate') {
      h.setDurable([row({ state: 'published', version: 2 })])
      await act(async () => { await h.result.current.refresh() })
    }
    await act(async () => { late.resolve(bundleReply()); await action })
    expect(create).not.toHaveBeenCalled()
    expect(h.result.current.download).toBeNull()
  })

  it.each(['account', 'project', 'unmount'])('revokes ready URLs on %s changes', async (change) => {
    const { revoke } = downloadEnvironment()
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?') ? bundleReply() : undefined })
    await open(h)
    await act(async () => { await h.result.current.prepareDownload(h.result.current.candidate) })
    if (change === 'account') act(() => { signIn('other'); notifyActiveScopeChanged() })
    if (change === 'project') h.rerender({ ...h.props, projectId: OTHER })
    if (change === 'unmount') h.unmount()
    expect(revoke).toHaveBeenCalledWith('blob:verified-source')
  })
})

describe('Native source through the real API', () => {
  const nativeRow = (overrides = {}) => row({ schema: 'leaf.native-project-source.v1', changed_paths: ['Sources/ContentView.swift'], ...overrides })
  const transcript = (overrides = {}) => material({ byte_size: 1048576, name: 'Authentic transcript.txt', ...overrides })
  const nativeSetup = (options = {}) => setup({ ...options, props: { sourceContext: 'native', ...options.props } })

  it('creates from the selected transcript and confirms once with exact Native shared-turn hints', async () => {
    const h = nativeSetup({ materials: [transcript(), material({ material_id: REQUEST, name: 'Teacher.mov', media_type: 'video/quicktime', byte_size: 55000000 })] })
    await open(h)
    expect(h.result.current.selected).toEqual([OTHER])
    expect(h.result.current.materials).toHaveLength(2)
    await act(async () => { await Promise.all([h.result.current.stage(), h.result.current.stage()]) })
    const post = h.calls.find((call) => call.method === 'POST')
    const body = JSON.parse(post.body)
    expect(post.url).toBe(`/api/projects/${PROJECT}/native/source/requests`)
    expect(body).toEqual({ source_request_id: expect.any(String), requirements: brief, source_operation: 'create',
      material_refs: [{ material_id: OTHER, sha256: 'e'.repeat(64) }] })
    expect(h.startAgentTurn.mock.calls[0]).toEqual([brief, { source: 'author_panel', purpose: 'stage_authority',
      browser_action: 'create', source_request_id: body.source_request_id, material_refs: body.material_refs,
      source_profile: 'ios-swiftui-xcodegen-v1' }, { allowSecretOnce: false, requireImmediateTurn: true }])
    const witness = h.result.current.candidate
    await act(async () => { await Promise.all([h.result.current.confirm(witness), h.result.current.confirm(witness)]) })
    const confirmation = JSON.parse(h.calls.find((call) => call.url.endsWith('/confirm')).body)
    expect(confirmation).toEqual({ receipt_digest: witness.receipt_digest, expected_version: witness.version, confirmation_id: expect.any(String) })
    expect(h.startAgentTurn.mock.calls[1][1]).toEqual({ source: 'author_panel', purpose: 'stage_authority', browser_action: 'confirm',
      source_request_id: witness.source_request_id, ...confirmation, source_profile: 'ios-swiftui-xcodegen-v1' })
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(2)
    expect(h.calls.filter((call) => call.url.includes('/source/')).every((call) => call.url.includes('/native/source/'))).toBe(true)
    expect(h.result.current.candidate.state).toBe('published')
    await act(async () => { await h.result.current.confirm(witness); await h.result.current.prepareDownload(h.result.current.candidate) })
    expect(h.startAgentTurn).toHaveBeenCalledTimes(2)
    expect(h.calls.some((call) => call.url.includes('/export'))).toBe(false)
  })

  it.each([['recover', 'publishing', 'published'], ['reject', 'awaiting_confirmation', 'rejected']])('uses the exact Native %s hint and durable readback', async (action, state, finalState) => {
    const h = nativeSetup({ saved: [nativeRow({ state })] })
    await open(h)
    const witness = h.result.current.candidate
    await act(async () => { await h.result.current[action](witness) })
    const post = h.calls.find((call) => call.method === 'POST')
    expect(post.url).toBe(`/api/projects/${PROJECT}/native/source/requests/${REQUEST}/${action}`)
    expect(JSON.parse(post.body)).toEqual({ receipt_digest: witness.receipt_digest, expected_version: witness.version })
    expect(h.startAgentTurn.mock.calls[0][1]).toEqual({ source: 'author_panel', purpose: 'stage_authority', browser_action: action,
      source_request_id: REQUEST, receipt_digest: witness.receipt_digest, expected_version: witness.version, source_profile: 'ios-swiftui-xcodegen-v1' })
    expect(h.result.current.candidate.state).toBe(finalState)
  })

  it.each([{ materials: [] }, { materials: [transcript({ byte_size: 1048577 })] }, { materials: [transcript({ byte_size: 0 })] },
    { materials: [transcript({ sha256: 'bad' })] }, { materials: [transcript({ media_type: 'video/quicktime', byte_size: 55000000 })] }])('requires a nonempty supported selection and complete metadata: %j', async ({ materials }) => {
    const h = nativeSetup({ materials })
    await open(h)
    act(() => h.result.current.reviewSelection())
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
    expect(h.result.current.error).toMatch(/Choose|Review/)
  })

  it('rejects aggregate overflow and nine references, then accepts exactly 4 MiB', async () => {
    const materials = Array.from({ length: 9 }, (_, index) => transcript({ material_id: `55555555-5555-4555-8555-${String(index + 1).padStart(12, '0')}` }))
    const h = nativeSetup({ materials })
    await open(h)
    expect(h.result.current.selected).toEqual([])
    for (const item of materials) act(() => h.result.current.toggleMaterial(item.material_id))
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/eight/)
    for (const item of materials.slice(5)) act(() => h.result.current.toggleMaterial(item.material_id))
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/4 MiB/)
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    act(() => h.result.current.toggleMaterial(materials[4].material_id))
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).toHaveBeenCalledOnce()
    expect(JSON.parse(h.calls.find((call) => call.method === 'POST').body).material_refs).toHaveLength(4)
  })

  it.each([false, true])('native_edit_ui stages from published Native exact base once (new originals: %s)', async (withOriginal) => {
    const h = nativeSetup({ saved: [nativeRow({ state: 'published' })], materials: [transcript()] })
    await open(h)
    if (!withOriginal) act(() => h.result.current.toggleMaterial(OTHER))
    expect(h.result.current.sourceOperation).toBe('edit')
    expect(h.result.current.selectionProblem).toBeNull()
    await act(async () => { await Promise.all([h.result.current.stage(), h.result.current.stage()]) })
    const posts = h.calls.filter((call) => call.method === 'POST')
    expect(posts).toHaveLength(1)
    const body = JSON.parse(posts[0].body)
    expect(body).toEqual({ source_request_id: expect.any(String), requirements: brief, source_operation: 'edit',
      expected_base_commit: 'c'.repeat(40), material_refs: withOriginal ? [{ material_id: OTHER, sha256: 'e'.repeat(64) }] : [] })
    expect(posts[0].url).toBe(`/api/projects/${PROJECT}/native/source/requests`)
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
    expect(h.startAgentTurn).toHaveBeenCalledWith(brief, { source: 'author_panel', purpose: 'stage_authority',
      browser_action: 'edit', source_request_id: body.source_request_id, material_refs: body.material_refs,
      expected_base_commit: body.expected_base_commit, source_profile: 'ios-swiftui-xcodegen-v1' },
    { allowSecretOnce: false, requireImmediateTurn: true })
    const witness = h.result.current.candidate
    await act(async () => { await h.result.current.confirm({ ...witness, version: witness.version + 1 }) })
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
    await act(async () => { await h.result.current.confirm(witness) })
    expect(JSON.parse(h.calls.find((call) => call.url.endsWith('/confirm')).body)).toEqual({
      receipt_digest: witness.receipt_digest, expected_version: witness.version, confirmation_id: expect.any(String) })
  })

  it.each([false, true])('native_edit_ui requires explicit empty selection review and permits it only for edits (published: %s)', async (published) => {
    const h = nativeSetup({ saved: published ? [nativeRow({ state: 'published' })] : [] })
    await open(h)
    expect(h.result.current.selectionReviewed).toBe(false)
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    act(() => h.result.current.reviewSelection())
    expect(h.result.current.selectionProblem).toEqual(published ? null : 'Choose one through eight TXT or JSON originals.')
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).toHaveBeenCalledTimes(published ? 1 : 0)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(published ? 1 : 0)
  })

  it('native_edit_ui preserves the exact stale Native base and originals after 409 with no redispatch', async () => {
    let id
    const h = nativeSetup({ saved: [nativeRow({ state: 'published' })], materials: [transcript()], fetcher: (url, init) => {
      // Main may have advanced through another profile; only the own-profile source is an edit base.
      if (init.method === 'POST') { id = JSON.parse(init.body).source_request_id; return reply({ error: 'stale main' }, 409) }
      if (url.includes('/requests/')) return reply({}, 404)
      return undefined
    } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.unknown).toBe(true)
    expect(h.result.current.error).toMatch(/Refresh the original request/)
    expect(h.result.current.intent).toMatchObject({ source_request_id: id, source_operation: 'edit', expected_base_commit: 'c'.repeat(40),
      material_refs: [{ material_id: OTHER, sha256: 'e'.repeat(64) }], referenceNames: ['Authentic transcript.txt'] })
    expect(h.result.current.selected).toEqual([OTHER])
    expect(h.result.current.materials).toEqual([transcript()])
    await act(async () => { await h.result.current.stage(); await h.result.current.refresh(); await h.result.current.stage() })
    expect(h.result.current.unknown).toBe(true)
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(h.calls.filter((call) => call.url.endsWith(`/native/source/requests/${id}`)).length).toBeGreaterThanOrEqual(2)
    expect(h.calls.every((call) => call.url.includes('/native/source/') || call.url.includes('/reference-files'))).toBe(true)
  })

  it.each(['account', 'project', 'profile'])('native_edit_ui fences late edit POST after %s replacement', async (change) => {
    const gate = deferred()
    let id
    const h = nativeSetup({ saved: [nativeRow({ state: 'published' })], materials: [transcript()], fetcher: (url, init) => {
      if (init.method === 'POST') { id = JSON.parse(init.body).source_request_id; return gate.promise }
      return undefined
    } })
    await open(h)
    let staging
    act(() => { staging = h.result.current.stage() })
    await waitFor(() => expect(id).toBeTruthy())
    if (change === 'account') act(() => { signIn('replacement'); notifyActiveScopeChanged() })
    if (change === 'project') h.rerender({ ...h.props, projectId: OTHER })
    if (change === 'profile') h.rerender({ ...h.props, sourceContext: 'browser' })
    await act(async () => { gate.resolve(reply(nativeRow({ source_request_id: id }))); await staging })
    expect(h.result.current.candidate).toBeNull()
    expect(h.result.current.intent).toBeNull()
    expect(h.result.current.isOpen).toBe(false)
    expect(h.calls.some((call) => call.url.includes('/requests/'))).toBe(false)
  })

  it.each(['account', 'project', 'profile'])('native_edit_ui does not dispatch an edit after %s changes during authority minting', async (change) => {
    const gate = deferred()
    const h = nativeSetup({ saved: [nativeRow({ state: 'published' })], materials: [transcript()], mint: () => gate.promise })
    await open(h)
    let staging
    act(() => { staging = h.result.current.stage() })
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
    if (change === 'account') act(() => { signIn('replacement'); notifyActiveScopeChanged() })
    if (change === 'project') h.rerender({ ...h.props, projectId: OTHER })
    if (change === 'profile') h.rerender({ ...h.props, sourceContext: 'browser' })
    await act(async () => { gate.resolve(AUTHORITY); await staging })
    expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
    expect(h.result.current.intent).toBeNull()
  })

  it.each([{ schema: 'leaf.browser-project-source.v1' }, { project_id: OTHER }])(
    'native_edit_ui refuses foreign published readback as an edit base: %j', async (overrides) => {
      const h = nativeSetup({ saved: [nativeRow({ state: 'published', ...overrides })], materials: [transcript()] })
      await open(h)
      await act(async () => { await h.result.current.stage() })
      expect(h.result.current.sourceLoaded).toBe(false)
      expect(h.result.current.candidates).toEqual([])
      expect(h.startAgentTurn).not.toHaveBeenCalled()
      expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('reads the original Native ID after an unknown outcome without another model turn', async () => {
    let id
    const h = nativeSetup({ materials: [transcript()], fetcher: (url, init) => {
      if (init.method === 'POST') { id = JSON.parse(init.body).source_request_id; throw new TypeError('timeout') }
      if (url.includes('/requests/')) return reply({}, 404)
      return undefined
    } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.unknown).toBe(true)
    expect(h.result.current.intent.source_request_id).toBe(id)
    await act(async () => { await h.result.current.stage(); await h.result.current.refresh() })
    expect(h.startAgentTurn).toHaveBeenCalledOnce()
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(h.calls.filter((call) => call.url.endsWith(`/native/source/requests/${id}`)).length).toBeGreaterThanOrEqual(2)
  })

  it('aborts and fences Browser -> Native -> Browser late reads and re-reads original durable candidates', async () => {
    const browserGate = deferred(), nativeGate = deferred()
    let hold = true
    const h = setup({ saved: [row()], fetcher: (url) => {
      if (!hold || !url.endsWith('/requests')) return undefined
      return url.includes('/native/') ? nativeGate.promise : browserGate.promise
    } })
    act(() => h.result.current.open(brief))
    const browserSignal = h.calls.find((call) => call.url.endsWith('/requests')).signal
    h.rerender({ ...h.props, sourceContext: 'native' })
    expect(browserSignal.aborted).toBe(true)
    act(() => h.result.current.open(brief))
    const nativeSignal = h.calls.find((call) => call.url.includes('/native/')).signal
    h.rerender(h.props)
    expect(nativeSignal.aborted).toBe(true)
    await act(async () => { browserGate.resolve(reply([row()])); nativeGate.resolve(reply([nativeRow()])) })
    expect(h.result.current.candidate).toBeNull()
    expect(h.result.current.materials).toEqual([])
    hold = false
    await open(h)
    expect(h.result.current.candidate).toEqual(row())
    expect(h.result.current.sourceContext).toBe('browser')
  })

  it('revokes a Browser download on Native switch and never carries it back', async () => {
    const { revoke } = downloadEnvironment()
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?') ? bundleReply() : undefined })
    await open(h)
    await act(async () => { await h.result.current.prepareDownload(h.result.current.candidate) })
    expect(h.result.current.download.url).toBe('blob:verified-source')
    h.rerender({ ...h.props, sourceContext: 'native' })
    expect(revoke).toHaveBeenCalledWith('blob:verified-source')
    expect(h.result.current.download).toBeNull()
    expect(h.result.current.candidate).toBeNull()
    h.rerender(h.props)
    expect(h.result.current.download).toBeNull()
  })

  it('fences a Browser export still in flight when Native opens', async () => {
    const { create } = downloadEnvironment(), gate = deferred()
    const h = setup({ saved: [row({ state: 'published' })], fetcher: (url) => url.includes('/export?') ? gate.promise : undefined })
    await open(h)
    let preparing
    act(() => { preparing = h.result.current.prepareDownload(h.result.current.candidate) })
    h.rerender({ ...h.props, sourceContext: 'native' })
    await act(async () => { gate.resolve(bundleReply()); await preparing })
    expect(create).not.toHaveBeenCalled()
    expect(h.result.current.download).toBeNull()
  })

  it('does not POST if the context switches during shared-turn minting', async () => {
    const gate = deferred(), h = nativeSetup({ materials: [transcript()], mint: () => gate.promise })
    await open(h)
    let staging
    act(() => { staging = h.result.current.stage() })
    h.rerender({ ...h.props, sourceContext: 'browser' })
    await act(async () => { gate.resolve(AUTHORITY); await staging })
    expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
    expect(h.result.current.intent).toBeNull()
  })

  it('rejects stale Native confirmation witnesses without minting authority', async () => {
    const h = nativeSetup({ saved: [nativeRow()] })
    await open(h)
    const witness = h.result.current.candidate
    await act(async () => { await h.result.current.confirm({ ...witness, version: 2 }); await h.result.current.reject({ ...witness, receipt_digest: 'f'.repeat(64) }) })
    expect(h.result.current.error).toMatch(/displayed source changed/)
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    expect(h.calls.some((call) => call.method === 'POST')).toBe(false)
  })

  it('rejects unknown controller contexts without network or authority', async () => {
    const h = setup({ props: { sourceContext: 'ios' } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/context is unavailable/)
    expect(h.fetch).not.toHaveBeenCalled()
    expect(h.startAgentTurn).not.toHaveBeenCalled()
  })

  it('discards a late Native mutation after returning through Browser to Native', async () => {
    const gate = deferred()
    let id
    const h = nativeSetup({ materials: [transcript()], fetcher: (url, init) => {
      if (init.method === 'POST') { id = JSON.parse(init.body).source_request_id; return gate.promise }
      return undefined
    } })
    await open(h)
    let staging
    act(() => { staging = h.result.current.stage() })
    await waitFor(() => expect(id).toBeTruthy())
    h.rerender({ ...h.props, sourceContext: 'browser' })
    h.rerender(h.props)
    await act(async () => { gate.resolve(reply(nativeRow({ source_request_id: id }))); await staging })
    expect(h.result.current.candidate).toBeNull()
    expect(h.result.current.intent).toBeNull()
    expect(h.result.current.isOpen).toBe(false)
    expect(h.calls.some((call) => call.url.includes('/requests/'))).toBe(false)
    h.setDurable([nativeRow({ source_request_id: id })])
    await open(h)
    expect(h.result.current.candidate.source_request_id).toBe(id)
  })
})

describe('Browser source through the real API', () => {
  it('mints shared intent before the exact metadata-only POST and explicitly confirms once', async () => {
    const h = setup({ materials: [material()] })
    await open(h)
    await act(async () => { await Promise.all([h.result.current.stage(), h.result.current.stage()]) })
    const posts = h.calls.filter((call) => call.method === 'POST')
    expect(posts).toHaveLength(1)
    const body = JSON.parse(posts[0].body)
    expect(body).toEqual({ source_request_id: expect.any(String), requirements: brief, source_operation: 'create',
      material_refs: [{ material_id: OTHER, sha256: 'e'.repeat(64) }] })
    expect(h.startAgentTurn).toHaveBeenCalledWith(brief, { source: 'author_panel', purpose: 'stage_authority',
      browser_action: 'create', source_request_id: body.source_request_id, material_refs: body.material_refs },
    { allowSecretOnce: false, requireImmediateTurn: true })
    expect(h.events.indexOf('mint')).toBeLessThan(h.events.indexOf('post'))
    expect(posts[0].headers).toEqual({ 'Authorization': expect.stringMatching(/^Bearer /), 'Content-Type': 'application/json',
      'X-Authority-Session-Id': AUTHORITY.session_id, 'X-Authority-Turn-Id': AUTHORITY.turn_id })
    expect(h.calls.every((call) => call.cache === 'no-store')).toBe(true)
    expect(h.calls.every((call) => !call.url.includes('/content'))).toBe(true)
    const displayed = h.result.current.candidate
    await act(async () => { await Promise.all([h.result.current.confirm(displayed), h.result.current.confirm(displayed)]) })
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(2)
    const confirmation = JSON.parse(h.calls.find((call) => call.url.endsWith('/confirm')).body)
    expect(confirmation).toEqual({ receipt_digest: displayed.receipt_digest, expected_version: displayed.version, confirmation_id: expect.any(String) })
    expect(h.startAgentTurn.mock.calls[1][1]).toEqual({ source: 'author_panel', purpose: 'stage_authority', browser_action: 'confirm',
      source_request_id: displayed.source_request_id, ...confirmation })
    expect(h.result.current.candidate.state).toBe('published')
  })

  it('supports plain briefs and derives edit bases only from published readback', async () => {
    const h = setup({ saved: [row({ state: 'published' })] })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    const body = JSON.parse(h.calls.find((call) => call.method === 'POST').body)
    expect(body.material_refs).toEqual([])
    expect(body.source_operation).toBe('edit')
    expect(body.expected_base_commit).toBe('c'.repeat(40))
    expect(h.startAgentTurn.mock.calls[0][1].expected_base_commit).toBe(body.expected_base_commit)
  })

  it.each([{ projectId: null }, { mock: true }, { signedIn: false }])('refuses unavailable authority without mint or network: %j', async (props) => {
    const h = setup({ props })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    expect(h.fetch).not.toHaveBeenCalled()
    expect(h.result.current.error).toMatch(/project controls|signed-in/)
  })

  it('blocks empty/oversized requirements before mint and preserves transport credential refusal', async () => {
    const h = setup()
    await open(h, ' ')
    await act(async () => { await h.result.current.stage() })
    act(() => h.result.current.setRequirements('é'.repeat(8193)))
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    act(() => h.result.current.setRequirements(`api_key: ${'x'.repeat(24)}`))
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.secretRefusal?.overridable).toBe(true)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    await act(async () => { await h.result.current.stage({ allowSecretOnce: true }) })
    expect(h.startAgentTurn.mock.calls.at(-1)[2].allowSecretOnce).toBe(true)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })

  it('exposes unsupported/invalid/oversized inputs and requires an explicit bounded selection', async () => {
    const materials = [material(), material({ material_id: REQUEST, media_type: 'video/quicktime', name: 'Retained.mov' }),
      material({ material_id: PROJECT, byte_size: 9 * 1024 * 1024, name: 'Large.txt' }),
      material({ material_id: ORG, sha256: 'bad', name: 'Invalid.txt' })]
    const h = setup({ materials })
    await open(h)
    expect(h.result.current.materials).toHaveLength(4)
    expect(h.result.current.selected).toEqual([])
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    act(() => h.result.current.toggleMaterial(OTHER))
    await act(async () => { await h.result.current.stage() })
    expect(JSON.parse(h.calls.find((call) => call.method === 'POST').body).material_refs).toHaveLength(1)
  })

  it('does not silently truncate nine supported originals or a selected total over 8 MiB', async () => {
    const materials = Array.from({ length: 9 }, (_, index) => material({ material_id: `55555555-5555-4555-8555-${String(index + 1).padStart(12, '0')}`, byte_size: 1024 * 1024 + 1 }))
    const h = setup({ materials })
    await open(h)
    expect(h.result.current.selected).toEqual([])
    for (const item of materials) act(() => h.result.current.toggleMaterial(item.material_id))
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/eight/)
    act(() => h.result.current.toggleMaterial(materials[0].material_id))
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/8 MiB/)
    expect(h.startAgentTurn).not.toHaveBeenCalled()
  })

  it('follows cursor pages and fails closed on cycles without claiming a complete list', async () => {
    const h = setup({ fetcher: (url) => url.includes('/reference-files') ? reply({ materials: [], next_cursor: OTHER }) : undefined })
    await open(h)
    expect(h.calls.filter((call) => call.url.includes('/reference-files'))).toHaveLength(2)
    expect(h.result.current.materialsComplete).toBe(false)
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
  })

  it.each(['project', 'account'])('sends no POST if %s changes during mint', async (change) => {
    const gate = deferred(), h = setup({ mint: () => gate.promise })
    await open(h)
    let submission
    act(() => { submission = h.result.current.stage() })
    if (change === 'project') h.rerender({ ...h.props, projectId: OTHER })
    else act(() => { signIn('different-person'); notifyActiveScopeChanged() })
    expect(h.result.current.isOpen).toBe(false)
    expect(h.result.current.materials).toEqual([])
    await act(async () => { gate.resolve(AUTHORITY); await submission })
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('suppresses late private reads after project change and retains review across profile-only rerenders', async () => {
    const gate = deferred()
    const h = setup({ fetcher: (url) => url.endsWith('/requests') ? gate.promise : undefined })
    act(() => h.result.current.open(brief))
    h.rerender({ ...h.props, projectId: OTHER })
    await act(async () => { gate.resolve(reply([row()])) })
    expect(h.result.current.candidate).toBeNull()
    h.unmount()
    const restored = setup({ saved: [row()] })
    await open(restored)
    restored.rerender({ ...restored.props, profile: 'solar' })
    expect(restored.result.current.candidate).toEqual(row())
    expect(restored.result.current.isOpen).toBe(true)
    act(() => restored.result.current.close())
    await open(restored)
    expect(restored.result.current.candidate).toEqual(row())
  })

  it('reads back the original ID after an uncertain POST and never repeats an editor or nonce', async () => {
    let id
    const h = setup({ fetcher: (url, init) => {
      if (init.method === 'POST') { id = JSON.parse(init.body).source_request_id; throw new TypeError('timeout') }
      if (url.includes('/requests/')) return reply({}, 404)
      return undefined
    } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.unknown).toBe(true)
    expect(h.result.current.intent.source_request_id).toBe(id)
    await act(async () => { await h.result.current.stage(); await h.result.current.refresh() })
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(h.calls.filter((call) => call.url.endsWith(`/requests/${id}`)).length).toBeGreaterThanOrEqual(2)
  })

  it('restores the durable candidate when a timed-out POST actually saved source', async () => {
    let committed
    const h = setup({ fetcher: (url, init) => {
      if (init.method === 'POST') {
        committed = row({ source_request_id: JSON.parse(init.body).source_request_id })
        throw new DOMException('aborted response', 'AbortError')
      }
      if (url.includes('/requests/')) return reply(committed)
      return undefined
    } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.unknown).toBe(false)
    expect(h.result.current.candidate).toEqual(committed)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    expect(h.startAgentTurn).toHaveBeenCalledTimes(1)
  })

  it('does not accept a read older than the confirmation response or mint another confirmation', async () => {
    let caughtUp = false
    const h = setup({ saved: [row()], fetcher: (url, init) => {
      if (init.method === 'POST') return reply(row({ state: 'published', version: 3 }))
      if (url.includes('/requests/')) return reply(caughtUp ? row({ state: 'published', version: 3 }) : row())
      return undefined
    } })
    await open(h)
    const displayed = h.result.current.candidate
    await act(async () => { await h.result.current.confirm(displayed) })
    expect(h.result.current.unknown).toBe(true)
    expect(h.result.current.candidate).toBeNull()
    await act(async () => { await h.result.current.confirm(displayed) })
    expect(h.startAgentTurn).toHaveBeenCalledOnce()
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(1)
    h.setDurable([row({ state: 'published', version: 3 })])
    // Until exact readback catches up, the published mutation witness is not UI success.
    await act(async () => { await h.result.current.refresh() })
    expect(h.result.current.unknown).toBe(true)
    expect(h.result.current.candidate).toBeNull()
    caughtUp = true
    await act(async () => { await h.result.current.refresh() })
    expect(h.result.current.candidate.state).toBe('published')
    expect(h.result.current.unknown).toBe(false)
    expect(h.startAgentTurn).toHaveBeenCalledOnce()
  })

  it('preserves an explicit reference exclusion when metadata is refreshed', async () => {
    const h = setup({ materials: [material()] })
    await open(h)
    act(() => h.result.current.toggleMaterial(OTHER))
    expect(h.result.current.selected).toEqual([])
    act(() => h.result.current.refreshMaterials())
    await waitFor(() => expect(h.result.current.materialsLoading).toBe(false))
    expect(h.result.current.selected).toEqual([])
    await act(async () => { await h.result.current.stage() })
    expect(JSON.parse(h.calls.find((call) => call.method === 'POST').body).material_refs).toEqual([])
  })

  it.each([null, { session_id: 'incomplete' }])('does not fall back to another author after an incomplete/busy mint: %j', async (authority) => {
    const h = setup({ mint: () => authority })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.startAgentTurn).toHaveBeenCalledOnce()
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
    expect(h.result.current.error).toMatch(/conversation turn/)
  })

  it('keeps provider diagnostics out of the source flow after a failed shared turn', async () => {
    const h = setup({ mint: () => { throw new Error('private provider lease diagnostic') } })
    await open(h)
    await act(async () => { await h.result.current.stage() })
    expect(h.result.current.error).toMatch(/conversation turn/)
    expect(h.result.current.error).not.toMatch(/provider|lease|diagnostic/)
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it('discards a mutation response from an account that has changed', async () => {
    const gate = deferred()
    let sourceId
    const h = setup({ fetcher: (url, init) => {
      if (init.method === 'POST') { sourceId = JSON.parse(init.body).source_request_id; return gate.promise }
      return undefined
    } })
    await open(h)
    let submission
    act(() => { submission = h.result.current.stage() })
    await waitFor(() => expect(sourceId).toBeTruthy())
    act(() => { signIn('replacement'); notifyActiveScopeChanged() })
    await act(async () => { gate.resolve(reply(row({ source_request_id: sourceId }))); await submission })
    expect(h.result.current.candidate).toBeNull()
    expect(h.result.current.intent).toBeNull()
    expect(h.result.current.isOpen).toBe(false)
    expect(h.calls.filter((call) => call.url.includes('/requests/'))).toHaveLength(0)
  })

  it('loads a complete paginated metadata list without downloading originals', async () => {
    const h = setup({ fetcher: (url) => {
      if (!url.includes('/reference-files')) return undefined
      return reply(url.includes('?cursor=') ? { materials: [material({ material_id: REQUEST, byte_size: 1000 })], next_cursor: null }
        : { materials: [material()], next_cursor: OTHER })
    } })
    await open(h)
    expect(h.result.current.materialsComplete).toBe(true)
    expect(h.result.current.selected).toEqual([OTHER, REQUEST])
    expect(h.calls.filter((call) => call.url.includes('/reference-files'))).toHaveLength(2)
    expect(h.calls.every((call) => call.signal instanceof AbortSignal)).toBe(true)
  })

  it('keeps unsupported composer image names visible without sending image bytes or claiming consumption', async () => {
    const h = setup()
    act(() => h.result.current.open(brief, { images: [{ file: { name: 'Retained.png', bytes: 'must-stay-local' } }] }))
    await waitFor(() => expect(h.result.current.loading || h.result.current.materialsLoading).toBe(false))
    expect(h.result.current.unsupportedInputs).toEqual(['Retained.png'])
    await act(async () => { await h.result.current.stage() })
    const post = h.calls.find((call) => call.method === 'POST')
    expect(post.body).not.toMatch(/Retained|bytes|must-stay-local/)
    expect(h.startAgentTurn.mock.calls[0][1].material_refs).toEqual([])
  })

  it('invalidates an old mint even if account scope changes away and back before React renders', async () => {
    const gate = deferred(), h = setup({ mint: () => gate.promise })
    await open(h)
    let submission
    act(() => { submission = h.result.current.stage() })
    act(() => { signIn('temporary-account'); notifyActiveScopeChanged(); signIn(); notifyActiveScopeChanged() })
    await act(async () => { gate.resolve(AUTHORITY); await submission })
    expect(h.result.current.intent).toBeNull()
    expect(h.calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })

  it.each(['recover', 'reject'])('uses frozen server witnesses for %s and disallows invalid states', async (action) => {
    const h = setup({ saved: [row({ state: action === 'recover' ? 'publishing' : 'staged' })] })
    await open(h)
    await act(async () => { await h.result.current[action]({ ...h.result.current.candidate, version: 99 }) })
    expect(h.startAgentTurn).not.toHaveBeenCalled()
    await act(async () => { await h.result.current[action](h.result.current.candidate) })
    const call = h.calls.find((item) => item.method === 'POST')
    expect(call.url.endsWith(`/${action}`)).toBe(true)
    expect(JSON.parse(call.body)).toEqual({ receipt_digest: 'a'.repeat(64), expected_version: 1 })
    expect(h.startAgentTurn.mock.calls[0][1]).toEqual({ source: 'author_panel', purpose: 'stage_authority', browser_action: action,
      source_request_id: REQUEST, receipt_digest: 'a'.repeat(64), expected_version: 1 })
  })

  it('rejects malformed, extra, mismatched and stale server records and fences direct API calls', async () => {
    for (const invalid of [row({ extra: true }), row({ project_id: OTHER }), row({ state: 'built' }), row({ version: 0 }), row({ source_tree: 'bad' })]) {
      expect(() => validateProjectSource(invalid, PROJECT)).toThrow()
    }
    const h = setup({ saved: [row({ version: 2 })] })
    await open(h)
    h.setDurable([row({ version: 1 })])
    await act(async () => { await h.result.current.refresh() })
    expect(h.result.current.candidate).toBeNull()
    const count = h.calls.length
    await expect(listProjectSourceRequests(PROJECT, { expectedScope: 'old-account' })).rejects.toThrow()
    await expect(stageProjectSource(PROJECT, { source_request_id: REQUEST, requirements: 'plain', source_operation: 'create', material_refs: [], repo: 'forbidden' },
      { expectedScope: readActiveScope(), authority: AUTHORITY })).rejects.toThrow()
    expect(h.calls).toHaveLength(count)
  })

  it('rejects over-limit server lists and does not promote backend error details into the UI', async () => {
    const h = setup({ fetcher: (url) => url.endsWith('/requests') ? reply(Array.from({ length: 21 }, () => row())) : undefined })
    await expect(listProjectSourceRequests(PROJECT, { expectedScope: readActiveScope() })).rejects.toThrow(/unavailable/)
    h.fetch.mockImplementation(async () => reply({ error: { message: 'private provider/repository diagnostic' } }, 503))
    await expect(listProjectSourceRequests(PROJECT, { expectedScope: readActiveScope() })).rejects.toThrow('Project source is unavailable. Refresh to review its current state.')
  })
})

describe('real catalog optional source adapter', () => {
  function catalog(decision, openProjectSource) {
    const startAgentTurn = vi.fn(async () => AUTHORITY), openAuthor = vi.fn()
    const controller = createCatalogController({ context: { mock: false, agentDisabled: false },
      services: { routePrompt: vi.fn(async () => decision), getTools: async () => [], getCapabilities: async () => ({ families: [] }) },
      adapters: { startAgentTurn, openProjectSource, openAuthor, dismissDecision: vi.fn(), commitDecision: vi.fn() } })
    return { controller, startAgentTurn, openAuthor }
  }
  it.each(['browser', 'native'])('hands classified %s builds to the hook and leaves source mutations to explicit review', async (sourceContext) => {
    const h = setup({ props: { sourceContext } })
    const adapter = vi.fn((text) => h.result.current.open(text))
    const c = catalog({ lane: 'build', confidence: 0.9 }, adapter)
    await act(async () => { await c.controller.actions.dispatch(brief, { allowSecretOnce: true }) })
    expect(adapter).toHaveBeenCalledWith(brief, { allowSecretOnce: true })
    expect(h.result.current.isOpen).toBe(true)
    expect(h.result.current.requirements).toBe(brief)
    expect(c.startAgentTurn).not.toHaveBeenCalled()
    expect(c.openAuthor).not.toHaveBeenCalled()
    c.controller.destroy()
  })
  it.each(['chat', 'solve', 'run'])('does not redirect ordinary %s intent to source', async (lane) => {
    const adapter = vi.fn(async () => true), c = catalog({ lane, tool: null, confidence: 0.4 }, adapter)
    await c.controller.actions.dispatch('What does this project do?')
    expect(adapter).not.toHaveBeenCalled()
    expect(c.startAgentTurn).toHaveBeenCalledOnce()
    c.controller.destroy()
  })
  it('retains unhandled build and slash behavior', async () => {
    const adapter = vi.fn(async () => false), c = catalog({ lane: 'build', confidence: 0.4 }, adapter)
    await c.controller.actions.dispatch('Build a CAD tool')
    expect(c.startAgentTurn).toHaveBeenCalledOnce()
    adapter.mockClear()
    await c.controller.actions.dispatch('/unknown-tool')
    expect(adapter).not.toHaveBeenCalled()
    c.controller.destroy()
  })
})
