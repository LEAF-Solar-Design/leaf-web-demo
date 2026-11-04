import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import { loadProjectPage, portfolioTextChanges, proposeProjectPage, validateProjectPageProposal, validateProjectPhotoAssignmentProposal } from './projectPageClient.js'
import { validatePageDocument, validatePageAction, NO_PAGE_DIGEST, validateFirstPageProposal, firstPageChanges } from './projectPageClient.js'

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

const firstDocument = () => ({ label: 'Bakery', headline: 'Fresh bread soon', description: 'Made locally', status: 'Opening soon', theme: 'cream', alignment: 'center',
  action: { label: 'Contact us', href: 'mailto:hello@example.com' } })
const firstResult = () => ({ source: { project_id: PROJECT, revision: 0, digest: NO_PAGE_DIGEST }, proposed_document: firstDocument(),
  proposed_digest: 'd'.repeat(64), edits: [], model: 'fixture', usage: {} })

describe('first page proposals and contact actions', () => {
  it.each(['https://example.com/contact?from=page#email', 'mailto:hello+page@example.com'])('accepts contact action %s', href => {
    const value = { ...firstDocument(), action: { label: 'Contact us', href } }
    expect(validatePageDocument(value)).toBe(value)
  })

  it.each(['http://example.com', 'javascript:alert(1)', 'data:text/html,x', '/relative', 'https://user@example.com',
    'https://exa mple.com', 'mailto:a@b.co?subject=x', 'https://example.com\n', 'https://example.com/%zz'])('rejects contact href %s', href => {
    expect(() => validatePageDocument({ ...firstDocument(), action: { label: 'Contact us', href } })).toThrow('Check the contact link.')
  })

  it.each([
    { label: 'Contact', href: 'https://example.com', extra: true }, { label: 'Contact' },
    { label: '', href: 'https://example.com' }, { label: '   ', href: 'https://example.com' },
    { label: 'x'.repeat(61), href: 'https://example.com' }, { label: 'Visit https://example.com', href: 'https://example.com' },
    { label: 'Contact\nnow', href: 'https://example.com' }, { label: 'Contact\u200b', href: 'https://example.com' },
  ])('rejects malformed contact action %#', action => {
    expect(() => validatePageDocument({ ...firstDocument(), action })).toThrow()
    expect(() => validatePageAction(action)).toThrow()
  })

  it('keeps the no-action document unchanged', () => {
    const value = firstDocument(); delete value.action
    const encoded = JSON.stringify(value)
    expect(validatePageDocument(value)).toBe(value)
    expect(JSON.stringify(value)).toBe(encoded)
  })

  it('posts the first revision sentinel and validates the response', async () => {
    const value = firstResult(), fetch = vi.fn().mockResolvedValue(reply(value))
    vi.stubGlobal('fetch', fetch)
    expect(await proposeProjectPage(PROJECT, null, 'brief')).toEqual(value)
    expect(fetch.mock.calls[0][0]).toBe(`https://api.example.test/api/projects/${PROJECT}/page/proposals`)
    expect(fetch.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ expected_revision: 0, expected_digest: NO_PAGE_DIGEST, instruction: 'brief' })
  })

  it.each([
    value => { value.source.revision = 1 }, value => { value.source.digest = 'a'.repeat(64) },
    value => { value.source.project_id = OTHER }, value => { value.proposed_document.schema = 'leaf.portfolio.v1' },
    value => { value.extra = true }, value => { value.proposed_document.action.href = 'javascript:alert(1)' },
    value => { value.edits = [{ id: 'a', value: 'ok', extra: true }] }, value => { value.usage.extra = 1 },
    value => { value.model = 'x'.repeat(1024 * 1024) },
  ])('rejects invalid first-page result %#', async mutate => {
    const value = firstResult(); mutate(value)
    expect(() => validateFirstPageProposal(PROJECT, value)).toThrow(INVALID)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(value)))
    await expect(proposeProjectPage(PROJECT, null, 'brief')).rejects.toThrow(INVALID)
  })

  it.each([undefined, { revision: 0 }, {}, { revision: 1, digest: 'invalid' }])('rejects non-null invalid saved input %# before HTTP', async source => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    await expect(proposeProjectPage(PROJECT, source, 'brief')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('orders first-page changes and skips empty fields', () => {
    expect(firstPageChanges(firstDocument())).toEqual([
      ['Page label', 'Bakery'], ['Headline', 'Fresh bread soon'], ['Description', 'Made locally'], ['Status text', 'Opening soon'],
      ['Contact button label', 'Contact us'], ['Contact link', 'mailto:hello@example.com'],
    ].map(([label, after]) => ({ label, before: '', after })))
    const value = firstDocument(); delete value.action; value.description = ''; value.status = ''
    expect(firstPageChanges(value).map(change => change.label)).toEqual(['Page label', 'Headline'])
  })
})

const photoDigest = document => createHash('sha256').update(JSON.stringify(document, (_, value) => value && typeof value === 'object' && !Array.isArray(value)
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value)).digest('hex')
const photoSaved = () => ({ ...saved(), digest: photoDigest(document()) })
const photoProposal = () => {
  const proposed = document()
  proposed.categories[0].images.push({ material_id: '33333333-3333-4333-8333-333333333333', sha256: 'e'.repeat(64), preview_sha256: 'f'.repeat(64), title: 'Crème cake', alt: 'A fresh cake' })
  return { schema: 'leaf.project-photo-assignment-proposal.v1', source: { ...result().source, digest: photoSaved().digest }, proposed_document: proposed, proposed_digest: photoDigest(proposed),
    changes: [{ op: 'assign', material_id: proposed.categories[0].images[2].material_id, material_name: 'Cake original.jpg', gallery_slug: 'cakes', gallery_title: 'Cakes', index: 2, title: 'Crème cake', alt: 'A fresh cake' }] }
}

describe('closed original-photo proposal validation', () => {
  it('hashes Python canonical UTF8, detaches before await and deeply freezes the append-only result', async () => {
    vi.stubGlobal('crypto', webcrypto)
    const input = photoProposal(), source = photoSaved(), expected = JSON.parse(JSON.stringify(input))
    const pending = validateProjectPhotoAssignmentProposal(PROJECT, source, input)
    input.proposed_document.categories[0].images[0].title = 'Injected'
    input.changes[0].material_name = 'Injected'
    const validated = await pending
    expect(validated).toEqual(expected)
    for (const value of [validated, validated.source, validated.changes, validated.changes[0], validated.proposed_document, validated.proposed_document.categories[0].images[2]]) expect(Object.isFrozen(value)).toBe(true)
    expect(source).toEqual(photoSaved())
  })

  it.each([
    ['schema', value => { value.extra = true }],
    ['project', value => { value.source.project_id = OTHER }],
    ['revision', value => { value.source.revision++ }],
    ['source digest', value => { value.source.digest = 'b'.repeat(64) }],
    ['digest', value => { value.proposed_digest = '0'.repeat(64) }],
    ['image hash', value => { value.proposed_document.categories[0].images[2].sha256 = '0'.repeat(64) }],
    ['change caption', value => { value.changes[0].title = 'Different' }],
    ['duplicate assignment', value => { value.changes.push({ ...value.changes[0] }) }],
    ['already assigned', value => { value.changes[0].material_id = PROJECT; value.proposed_document.categories[0].images[2].material_id = PROJECT }],
    ['reorder', value => { value.proposed_document.categories[0].images.reverse() }],
    ['removal', value => { value.proposed_document.categories[0].images.splice(0, 1) }],
    ['old caption', value => { value.proposed_document.categories[0].images[0].title = 'Rewritten' }],
    ['design', value => { value.proposed_document.galleryLayout = 'single' }],
    ['hero', value => { value.proposed_document.categories[0].hero = 1 }],
    ['gallery', value => { value.changes[0].gallery_title = 'Invented' }],
    ['index', value => { value.changes[0].index = 1 }],
    ['unknown change field', value => { value.changes[0].sha256 = 'e'.repeat(64) }],
    ['unsafe caption', value => { value.proposed_document.categories[0].images[2].title = value.changes[0].title = '<script>' }],
    ['empty alt', value => { value.proposed_document.categories[0].images[2].alt = value.changes[0].alt = '' }],
    ['oversized filename', value => { value.changes[0].material_name = 'x'.repeat(1025) }],
    ['oversized envelope', value => { value.padding = 'x'.repeat(1024 * 1024) }],
    ['too many changes', value => { value.changes = Array.from({ length: 201 }, () => ({ ...value.changes[0] })) }],
    ['empty changes', value => { value.changes = [] }],
  ])('rejects %s with a fixed safe error', async (label, corrupt) => {
    vi.stubGlobal('crypto', webcrypto)
    const value = photoProposal(); corrupt(value)
    // Structural tampering must fail even with a matching document digest.
    if (!['digest', 'image hash'].includes(label)) value.proposed_digest = photoDigest(value.proposed_document)
    await expect(validateProjectPhotoAssignmentProposal(PROJECT, photoSaved(), value)).rejects.toThrow('The photo assignment proposal could not be verified. Try again.')
  })

  it('appends operations in order across existing galleries while retaining original design and captions', async () => {
    vi.stubGlobal('crypto', webcrypto)
    const source = photoSaved()
    source.document.textStyles = { headline: { size: 48, lineHeight: 1.5 } }
    source.document.categories[0].images[0].display = { fit: 'cover', focal: 'top-left' }
    source.document.categories.push({ ...source.document.categories[0], slug: 'bread', title: 'Bread', images: [source.document.categories[0].images[1]] })
    source.digest = photoDigest(source.document)
    const value = photoProposal()
    value.source.digest = source.digest
    value.proposed_document = JSON.parse(JSON.stringify(source.document))
    const first = { ...photoProposal().proposed_document.categories[0].images[2] }
    const second = { ...first, material_id: '44444444-4444-4444-8444-444444444444', title: 'Second original' }
    value.proposed_document.categories[1].images.push(first)
    value.proposed_document.categories[0].images.push(second)
    value.changes = [{ ...value.changes[0], gallery_slug: 'bread', gallery_title: 'Bread', index: 1 },
      { ...value.changes[0], material_id: second.material_id, title: second.title }]
    value.proposed_digest = photoDigest(value.proposed_document)
    const validated = await validateProjectPhotoAssignmentProposal(PROJECT, source, value)
    expect(validated.proposed_document.categories[0].images.slice(0, 2)).toEqual(source.document.categories[0].images)
    expect(validated.proposed_document.textStyles).toEqual(source.document.textStyles)
    expect(validated.changes.map(change => change.gallery_slug)).toEqual(['bread', 'cakes'])
  })

  it('keeps the closed text schema and text-only structure boundary', () => {
    expect(validateProjectPageProposal(PROJECT, saved(), result())).toEqual(result())
    expect(() => validateProjectPageProposal(PROJECT, saved(), photoProposal())).toThrow(INVALID)
    const value = result(); value.proposed_document = photoProposal().proposed_document
    expect(() => validateProjectPageProposal(PROJECT, saved(), value)).toThrow(INVALID)
  })
})

beforeEach(() => { vi.stubGlobal('fetch', vi.fn()); unauthorized.mockClear() })
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('allows text changes while preserving configuration and rejects design changes in proposals', () => {
  const source = document()
  source.appearance = { font: 'mono', palette: { text: '#aBCdEf' } }
  source.textStyles = { headline: { size: 48 } }
  source.nav = { order: ['resume', 'home'], hideAbout: true }
  source.categories[0].hidden = true
  source.categories[0].images[0].display = { fit: 'cover', focal: 'top-left' }
  source.resume[0].entries[0].layout = { padding: 24 }
  const proposed = structuredClone(source)
  proposed.headline = 'New headline'
  expect(portfolioTextChanges(source, proposed)).toEqual([{ label: 'Headline', before: source.headline, after: 'New headline' }])
  for (const change of [value => { value.appearance.font = 'serif' }, value => { delete value.categories[0].hidden },
    value => { value.categories[0].images[0].display.focal = 'bottom' }, value => { value.resume[0].entries[0].layout.padding = 0 }]) {
    const changed = structuredClone(proposed); change(changed)
    expect(() => portfolioTextChanges(source, changed)).toThrow()
  }
})

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

  it('shows fixed rate-limit copy after one request without reading errors or changing input', async () => {
    vi.useFakeTimers()
    const source = saved(), original = JSON.stringify(source), instruction = 'Shorten the headline'
    const json = vi.fn(() => { throw new Error('secret') })
    const text = vi.fn(() => { throw new Error('secret') })
    const cancel = vi.fn().mockResolvedValue()
    fetch.mockResolvedValueOnce({ ok: false, status: 429, json, text, body: { cancel } })
    await expect(propose(source, instruction)).rejects.toMatchObject({ status: 429,
      message: 'The text proposal service is temporarily rate limited. Try again later. Your saved page and draft are unchanged.' })
    expect(json).not.toHaveBeenCalled()
    expect(text).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(JSON.stringify(source)).toBe(original)
    expect(JSON.parse(fetch.mock.lastCall[1].body)).toEqual({ expected_revision: source.revision, expected_digest: source.digest, instruction })
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(60000)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(unauthorized).not.toHaveBeenCalled()
  })

  it('keeps the existing rate-limit message for other page operations', async () => {
    fetch.mockResolvedValueOnce({ ok: false, status: 429 })
    await expect(loadProjectPage(PROJECT)).rejects.toMatchObject({ status: 429,
      message: 'The page action failed. Your draft is still here. Try again.' })
    expect(fetch).toHaveBeenCalledTimes(1)
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
      { label: 'Resume section 1: Experience: heading', before: 'Experience', after: 'Work' },
      { label: 'Resume section 1: Experience, entry 1: title', before: 'Baker', after: 'Pastry chef' },
      { label: 'Gallery 1: Cakes: title', before: 'Cakes', after: 'Celebrations' },
      { label: 'Gallery 1: Cakes, photo 2: caption', before: 'Cake', after: 'Bread' },
    ]))
    expect(original.categories[0].title).toBe('Cakes')
  })
})
