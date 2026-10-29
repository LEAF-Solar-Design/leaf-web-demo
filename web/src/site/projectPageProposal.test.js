import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { portfolioTextChanges, proposeProjectPage } from './projectPageClient.js'

const unauthorized = vi.hoisted(() => vi.fn())
vi.mock('../api.js', () => ({ config: { apiBase: 'https://api.example.test' },
  authHeaders: () => ({ Authorization: 'Bearer fixture' }), getStoredOrgId: () => 'org-a', noteUnauthorized: unauthorized }))
vi.mock('../projects/api.js', () => ({ getStoredActorBindingId: () => 'binding-a', listProjectPhotos: vi.fn() }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const OTHER = '22222222-2222-4222-8222-222222222222'
const document = () => ({ schema: 'leaf.portfolio.v1', name: 'Portfolio', headline: 'Selected work', introduction: '', about: '',
  contact: { email: '', location: 'Portland' }, theme: 'light',
  resume: [{ heading: 'Experience', entries: [{ title: 'Baker', subtitle: '', dates: '', description: '' }] }],
  categories: [{ slug: 'cakes', title: 'Cakes', description: '', hero: 0, images: [PROJECT, OTHER].map(material_id => ({
    material_id, sha256: 'b'.repeat(64), preview_sha256: 'c'.repeat(64), title: 'Cake', alt: 'Cake on a plate',
  })) }] })
const saved = () => ({ revision: 7, digest: 'a'.repeat(64), document: document() })
const result = () => ({ source: { project_id: PROJECT, revision: 7, digest: 'a'.repeat(64) },
  proposed_document: { ...document(), headline: 'Selected baking' }, proposed_digest: 'd'.repeat(64),
  edits: [{ id: 'opaque-field', value: 'Selected baking' }], model: 'fixture-model', usage: { input_tokens: 12, output_tokens: 3 } })
const reply = value => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
const propose = (source = saved(), instruction = 'Shorten the headline', options) => proposeProjectPage(PROJECT, source, instruction, options)
const INVALID = 'The text proposal could not be verified. Try again.'

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); unauthorized.mockClear() })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('source-bound portfolio proposal transport', () => {
  it('posts only instruction and saved source with existing authenticated no-store headers', async () => {
    const source = saved(), original = JSON.stringify(source), output = result()
    fetch.mockResolvedValueOnce(reply(output))
    expect(await propose(source)).toEqual(output)
    expect(JSON.stringify(source)).toBe(original)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(fetch.mock.lastCall[0]).toBe(`https://api.example.test/api/projects/${PROJECT}/page/proposals`)
    expect(fetch.mock.lastCall[1]).toMatchObject({ method: 'POST', cache: 'no-store',
      headers: { Authorization: 'Bearer fixture', 'X-Org-Id': 'org-a', 'X-Actor-Binding-Id': 'binding-a', 'Content-Type': 'application/json' } })
    expect(JSON.parse(fetch.mock.lastCall[1].body)).toEqual({ expected_revision: 7, expected_digest: source.digest, instruction: 'Shorten the headline' })
  })

  it('rejects invalid sources and blank or oversized instructions before HTTP', async () => {
    for (const instruction of ['', ' \n ', 'x'.repeat(4001), null]) await expect(propose(saved(), instruction)).rejects.toThrow()
    for (const patch of [{ revision: 0 }, { revision: true }, { revision: 1.5 }, { digest: 'A'.repeat(64) }, { digest: 'a'.repeat(64) + '\n' }, { document: { headline: 'Legacy page' } }]) {
      await expect(propose({ ...saved(), ...patch })).rejects.toThrow()
    }
    await expect(proposeProjectPage('../project', saved(), 'Change text')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([
    value => { value.extra = 'secret' },
    value => { value.source.project_id = OTHER },
    value => { value.source.revision = 8 },
    value => { value.source.digest = 'b'.repeat(64) },
    value => { value.source.extra = true },
    value => { value.proposed_digest = 'D'.repeat(64) },
    value => { value.proposed_digest += '\n' },
    value => { value.model = 'x'.repeat(129) },
    value => { value.model = '' },
    value => { value.edits[0].extra = true },
    value => { value.edits.push(value.edits[0]) },
    value => { value.edits = Array.from({ length: 33 }, (_, i) => ({ id: `${i}`, value: 'x' })) },
    value => { value.edits[0].value = 'x'.repeat(8001) },
    value => { value.edits[0].id = 'x'.repeat(129) },
    value => { value.edits = [] },
    value => { value.usage = { secret: 1 } },
    value => { value.usage.input_tokens = -1 },
    value => { value.usage.input_tokens = 1.5 },
    value => { value.usage.input_tokens = Number.MAX_SAFE_INTEGER + 1 },
    value => { value.proposed_document.headline = '<script>secret</script>' },
  ])('rejects malformed, unbound or unbounded output with fixed text (%#)', async mutate => {
    const value = result(); mutate(value)
    fetch.mockResolvedValueOnce(reply(value))
    await expect(propose()).rejects.toThrow(INVALID)
  })

  it('accepts no-op output and valid unknown usage without fabricating changes', async () => {
    const value = { ...result(), proposed_document: document(), edits: [], usage: {} }
    fetch.mockResolvedValueOnce(reply(value))
    expect(await propose()).toEqual(value)
    expect(portfolioTextChanges(document(), value.proposed_document)).toEqual([])
  })

  it('rejects oversized advertised and streamed bodies and cancels readers', async () => {
    const cancel = vi.fn().mockResolvedValue(), releaseLock = vi.fn()
    const read = vi.fn().mockResolvedValue({ done: false, value: new Uint8Array(1024 * 1024 + 1) })
    const reader = { read, cancel, releaseLock }
    fetch.mockResolvedValueOnce({ ok: true, headers: new Headers({ 'content-type': 'application/json', 'content-length': String(1024 * 1024 + 1) }), body: { cancel } })
    await expect(propose()).rejects.toThrow(INVALID)
    expect(cancel).toHaveBeenCalled()
    fetch.mockResolvedValueOnce({ ok: true, headers: new Headers({ 'content-type': 'application/json' }), body: { getReader: () => reader } })
    await expect(propose()).rejects.toThrow(INVALID)
    expect(read).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalledTimes(2)
    expect(releaseLock).toHaveBeenCalled()
  })

  it('bounds non-stream responses and never reflects provider or parser errors', async () => {
    for (const response of [new Response('secret', { headers: { 'content-type': 'text/html' } }),
      new Response('secret', { headers: { 'content-type': 'application/json' } }),
      { ok: true, headers: new Headers({ 'content-type': 'application/json' }), text: async () => 'x'.repeat(1024 * 1024 + 1) },
      { ok: true, headers: new Headers({ 'content-type': 'application/json' }), text: async () => { throw new Error('secret') } }]) {
      fetch.mockResolvedValueOnce(response)
      await expect(propose()).rejects.toThrow(INVALID)
    }
    for (const status of [401, 409, 422, 500]) {
      const json = vi.fn().mockRejectedValue(new Error('secret'))
      fetch.mockResolvedValueOnce({ ok: false, status, json })
      await expect(propose()).rejects.toMatchObject({ status, message: expect.not.stringContaining('secret') })
      expect(json).not.toHaveBeenCalled()
    }
    expect(unauthorized).toHaveBeenCalledTimes(1)
  })

  it('allows 60 seconds only for proposals and aborts on timeout or caller cancellation', async () => {
    vi.useFakeTimers()
    fetch.mockImplementation(() => new Promise(() => {}))
    let finished = false
    const pending = propose().finally(() => { finished = true })
    const timed = expect(pending).rejects.toThrow('took too long')
    await vi.advanceTimersByTimeAsync(35000)
    expect(finished).toBe(false)
    await vi.advanceTimersByTimeAsync(25000); await timed
    expect(fetch.mock.lastCall[1].signal.aborted).toBe(true)
    const controller = new AbortController()
    const cancelled = expect(propose(saved(), 'Change text', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    controller.abort(); await cancelled
    expect(fetch.mock.lastCall[1].signal.aborted).toBe(true)
    const already = new AbortController(); already.abort()
    const count = fetch.mock.calls.length
    await expect(propose(saved(), 'Change text', { signal: already.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetch).toHaveBeenCalledTimes(count)
  })

  it('cancels a blocked body read and ignores caller mutation while pending', async () => {
    const cancel = vi.fn().mockResolvedValue(), releaseLock = vi.fn(), controller = new AbortController()
    const read = vi.fn(() => new Promise(() => {}))
    fetch.mockResolvedValueOnce({ ok: true, headers: new Headers({ 'content-type': 'application/json' }), body: { getReader: () => ({ read, cancel, releaseLock }) } })
    const pending = expect(propose(saved(), 'Change text', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    await vi.waitFor(() => expect(read).toHaveBeenCalled())
    controller.abort(); await pending
    expect(cancel).toHaveBeenCalled(); expect(releaseLock).toHaveBeenCalled()
    const source = saved(), output = result()
    fetch.mockImplementationOnce(async () => { source.revision = 99; source.digest = 'f'.repeat(64); source.document.categories[0].hero = 1; return reply(output) })
    expect(await propose(source)).toEqual(output)
  })
})

describe('text-only portfolio invariants', () => {
  it.each([
    value => { value.schema = 'other' },
    value => { value.theme = 'dark' },
    value => { value.categories[0].slug = 'bread' },
    value => { value.categories[0].hero = 1 },
    value => { value.categories[0].images.reverse() },
    value => { value.categories[0].images[0].material_id = '33333333-3333-4333-8333-333333333333' },
    value => { value.categories[0].images[0].sha256 = 'd'.repeat(64) },
    value => { value.categories[0].images[0].preview_sha256 = 'd'.repeat(64) },
    value => { value.categories[0].images.pop() },
    value => { value.categories.push({ ...value.categories[0], slug: 'bread' }) },
    value => { value.resume = [] },
    value => { value.resume[0].entries.push({ ...value.resume[0].entries[0] }) },
    value => { value.extra = true },
  ])('rejects structural or material changes (%#)', async mutate => {
    const output = result(); mutate(output.proposed_document)
    fetch.mockResolvedValueOnce(reply(output))
    await expect(propose()).rejects.toThrow(INVALID)
  })

  it('labels changes by saved gallery and resume context, including repeated field names', () => {
    const original = document(), next = document()
    next.resume[0].heading = 'Work'; next.resume[0].entries[0].title = 'Pastry chef'
    next.categories[0].title = 'Celebrations'; next.categories[0].images[1].title = 'Bread'
    next.contact.location = 'Oregon'
    const changes = portfolioTextChanges(original, next)
    expect(changes).toEqual(expect.arrayContaining([
      { label: 'Resume section 1: Experience — heading', before: 'Experience', after: 'Work' },
      { label: 'Resume section 1: Experience, entry 1 — title', before: 'Baker', after: 'Pastry chef' },
      { label: 'Gallery 1: Cakes — title', before: 'Cakes', after: 'Celebrations' },
      { label: 'Gallery 1: Cakes, photo 2 — caption', before: 'Cake', after: 'Bread' },
    ]))
    expect(original.categories[0].title).toBe('Cakes')
  })
})
