import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Blob as NodeBlob } from 'node:buffer'
import { createHash, webcrypto } from 'node:crypto'
import { enumerateProjectPhotoInspection as enumerate, prepareProjectPhotoInspectionBatch as batch } from './projectPhotoInspection.js'
import { emptyPortfolio, photoReference } from './portfolioDocument.js'

// Keep the real page/material transports and validator. Only ambient auth is a fixture.
vi.mock('../api.js', () => ({ config: { apiBase: '' }, authHeaders: () => ({}),
  getStoredOrgId: () => null, noteUnauthorized: vi.fn() }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => 'account/org' }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const id = n => `22222222-2222-4222-8222-${String(n).padStart(12, '0')}`
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const jpeg = (size = 12, fill = 7) => {
  const bytes = new Uint8Array(size).fill(fill)
  bytes.set([255, 216, 255]); bytes.set([255, 217], size - 2)
  return bytes
}
function fixture(count = 3, sizes = []) {
  const bytes = Array.from({ length: count }, (_, index) => jpeg(sizes[index] || 12, index + 1))
  const photos = bytes.map((raw, index) => ({ material_id: id(index + 1), name: `Photo ${index + 1}`,
    media_type: 'image/png', byte_size: 500, sha256: hash(new Uint8Array([index])),
    preview_sha256: hash(raw), preview_media_type: 'image/jpeg', width: 40, height: 20,
    created_at: '2026-01-01T00:00:00Z', storage_key: 'must never escape', url: 'must never escape' }))
  const document = emptyPortfolio()
  document.categories[0].images = [photoReference(photos[0])]
  document.categories[0].images[0].title = ' Exact caption '
  document.categories[0].images[0].alt = 'Exact alt'
  const source = { project_id: PROJECT, revision: 7, digest: 'a'.repeat(64) }
  const input = { projectId: PROJECT, expectedScope: 'account/org', source, document, isCurrent: () => true }
  const deps = {
    loadProjectPage: vi.fn(async () => ({ revision: 7, digest: 'a'.repeat(64), document })),
    listProjectPhotos: vi.fn(async () => ({ materials: photos, next_cursor: null })),
    readProjectPhotoPreview: vi.fn(async (_, materialId) => new NodeBlob([bytes[photos.findIndex(photo => photo.material_id === materialId)]], { type: 'image/jpeg' })),
  }
  return { bytes, photos, input, deps }
}
const prepare = (snapshot, deps, extra = {}) => batch({ snapshot, materialIds: snapshot.coverage.listed_ids.slice(0, 3), isCurrent: () => true, ...extra }, deps)
async function refusal(promise, code) {
  const error = await promise.then(() => { throw new Error('Expected refusal') }, error => error)
  expect(error).toMatchObject({ name: 'ProjectPhotoInspectionError', code })
  expect(Object.keys(error)).not.toContain('images')
  expect(JSON.stringify(error)).not.toMatch(/base64|must never escape|credentials/)
  return error
}
beforeEach(() => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network') }))
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('authorized exhaustive metadata', () => {
  it('enumerates all 94 photos across pages, sorted, with exact assignment provenance and unassigned photos', async () => {
    const { photos, input, deps } = fixture(94)
    input.document.categories.push({ slug: 'other', title: 'Other gallery', description: '', hero: 0,
      images: [{ ...photoReference(photos[0]), title: 'Other caption', alt: 'Other alt' }] })
    deps.listProjectPhotos.mockImplementation(async (_, { cursor }) => cursor === undefined
      ? { materials: photos.slice(40).reverse(), next_cursor: 'page-two' }
      : { materials: photos.slice(0, 40).reverse(), next_cursor: null })
    const snapshot = await enumerate(input, deps)
    expect(snapshot.coverage).toEqual({ listed_ids: photos.map(photo => photo.material_id), listed_count: 94, complete: true })
    expect(snapshot.photos[0].assignments).toEqual([
      { gallery_slug: 'selected-work', gallery_title: 'Selected work', index: 0, hero: true, caption: ' Exact caption ', alt: 'Exact alt' },
      { gallery_slug: 'other', gallery_title: 'Other gallery', index: 0, hero: true, caption: 'Other caption', alt: 'Other alt' },
    ])
    expect(snapshot.photos[1].assignments).toEqual([])
    expect(snapshot.photos[1]).toEqual({ ...Object.fromEntries(['material_id', 'name', 'media_type', 'byte_size', 'sha256', 'preview_sha256', 'width', 'height', 'created_at'].map(key => [key, photos[1][key]])), assignments: [] })
    expect(snapshot.document).toEqual(input.document)
    expect(snapshot.source).toEqual(input.source)
    expect(Object.isFrozen(snapshot.document.categories[0].images[0])).toBe(true)
    expect(JSON.stringify(snapshot)).not.toMatch(/storage_key|must never escape/)
    expect(deps.readProjectPhotoPreview).not.toHaveBeenCalled()
    expect(deps.listProjectPhotos.mock.calls.every(([, options]) => options.expectedScope === input.expectedScope)).toBe(true)
    expect(deps.loadProjectPage.mock.calls[0]).toEqual([PROJECT, { signal: undefined }])
  })

  it('copies source and draft before the first await and does not invalidate for style alone', async () => {
    const { input, deps } = fixture()
    const original = structuredClone(input.document)
    const pending = enumerate(input, deps)
    input.source.digest = 'b'.repeat(64)
    input.document.categories[0].images[0].title = 'Changed later'
    input.document.galleryLayout = 'single'
    const snapshot = await pending
    expect(snapshot.source.digest).toBe('a'.repeat(64))
    expect(snapshot.document).toEqual(original)
  })

  it.each([
    ['CURSOR_LOOP', [{ next_cursor: 'again' }, { next_cursor: 'again', materials: [] }]],
    ['INVALID_CURSOR', [{ next_cursor: 2 }]],
    ['INVALID_CURSOR', [{ next_cursor: '' }]],
    ['DUPLICATE_ID', [{ next_cursor: 'next' }, {}]],
    ['CONFLICTING_METADATA', [{ next_cursor: 'next' }, { changed: true }]],
  ])('refuses %s without truncation', async (code, pages) => {
    const { input, photos, deps } = fixture(1)
    deps.listProjectPhotos.mockImplementation(async () => {
      const page = pages.shift()
      return { materials: page.changed ? [{ ...photos[0], name: 'Conflict' }] : photos, next_cursor: null, ...page }
    })
    await refusal(enumerate(input, deps), code)
  })

  it('accepts 200 catalog photos and explicitly refuses 201', async () => {
    const accepted = fixture(200)
    expect((await enumerate(accepted.input, accepted.deps)).photos).toHaveLength(200)
    const excessive = fixture(201)
    await refusal(enumerate(excessive.input, excessive.deps), 'CATALOG_LIMIT')
  })

  it.each(['missing', 'original', 'preview', 'project', 'metadata', 'document'])('refuses invalid %s provenance', async kind => {
    const { input, photos, deps } = fixture()
    if (kind === 'missing') input.document.categories[0].images[0].material_id = id(99)
    if (kind === 'original') input.document.categories[0].images[0].sha256 = 'b'.repeat(64)
    if (kind === 'preview') input.document.categories[0].images[0].preview_sha256 = 'b'.repeat(64)
    if (kind === 'project') photos[0].project_id = id(99)
    if (kind === 'metadata') photos[0].width = 0
    if (kind === 'document') input.document.extra = true
    const code = { missing: 'MISSING_REFERENCE', original: 'REFERENCE_DIGEST_MISMATCH', preview: 'REFERENCE_DIGEST_MISMATCH', project: 'INVALID_CATALOG', metadata: 'INVALID_CATALOG', document: 'INVALID_DOCUMENT' }[kind]
    await refusal(enumerate(input, deps), code)
  })

  it.each(['revision', 'digest', 'project_id'])('checks saved %s before listing', async field => {
    const { input, deps } = fixture()
    deps.loadProjectPage.mockResolvedValue({ revision: 7, digest: 'a'.repeat(64), [field]: field === 'revision' ? 8 : id(99) })
    await refusal(enumerate(input, deps), 'STALE_SOURCE')
    expect(deps.listProjectPhotos).not.toHaveBeenCalled()
  })

  it('rejects mismatched input project and a missing guard before any request', async () => {
    const { input, deps } = fixture()
    await refusal(enumerate({ ...input, projectId: id(99) }, deps), 'INVALID_SOURCE')
    await refusal(enumerate({ ...input, isCurrent: undefined }, deps), 'INVALID_INPUT')
    expect(deps.loadProjectPage).not.toHaveBeenCalled()
  })

  it.each(['account', 'org', 'project', 'saved', 'draft', 'selection'])('requires the caller %s epoch to remain current across awaits', async epoch => {
    for (const boundary of ['loadProjectPage', 'listProjectPhotos']) {
      const { input, deps } = fixture()
      let currentEpoch = epoch
      input.isCurrent = () => currentEpoch === epoch
      const original = deps[boundary].getMockImplementation()
      deps[boundary].mockImplementation(async (...args) => { const value = await original(...args); currentEpoch = 'changed'; return value })
      await refusal(enumerate(input, deps), 'STALE')
    }
  })
})

describe('verified real image attachments', () => {
  it('returns canonical image order, exact bytes and manifest provenance through real browser crypto', async () => {
    const { input, deps, bytes } = fixture()
    const snapshot = await enumerate(input, deps)
    const result = await prepare(snapshot, deps, { materialIds: [id(3), id(1), id(2)] })
    expect(result.images).toEqual(bytes.map(raw => ({ media_type: 'image/jpeg', data: Buffer.from(raw).toString('base64') })))
    expect(result.manifest.map(entry => entry.material_id)).toEqual([id(1), id(2), id(3)])
    expect(result.manifest[0]).toEqual({ attachment_index: 0, material_id: id(1), original_sha256: snapshot.photos[0].sha256,
      preview_sha256: snapshot.photos[0].preview_sha256, assignments: snapshot.photos[0].assignments })
    expect(deps.readProjectPhotoPreview.mock.calls.every(([, , options]) => options.expectedScope === 'account/org')).toBe(true)
    expect(deps.loadProjectPage).toHaveBeenCalledTimes(3)
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([[], [id(1), id(1)], [id(1), id(2), id(3), id(4)], [id(99)]].map(materialIds => ({ materialIds })))('rejects invalid ID selection $materialIds before pixel reads', async ({ materialIds }) => {
    const { input, deps } = fixture()
    const snapshot = await enumerate(input, deps)
    await refusal(prepare(snapshot, deps, { materialIds }), materialIds[0] === id(99) ? 'UNKNOWN_MATERIAL' : 'INVALID_BATCH')
    expect(deps.readProjectPhotoPreview).not.toHaveBeenCalled()
  })

  it('rejects fabricated or serialized snapshots', async () => {
    const { input, deps } = fixture()
    const snapshot = await enumerate(input, deps)
    await refusal(prepare(structuredClone(snapshot), deps), 'INVALID_SNAPSHOT')
  })

  it.each(['magic', 'digest', 'type'])('refuses wrong JPEG %s with no partial images', async kind => {
    const { input, deps, bytes } = fixture()
    const snapshot = await enumerate(input, deps)
    deps.readProjectPhotoPreview.mockResolvedValueOnce(new NodeBlob([bytes[0]], { type: 'image/jpeg' }))
      .mockResolvedValueOnce(new NodeBlob([kind === 'magic' ? new Uint8Array([1, 2, 3]) : jpeg(12, 99)], { type: kind === 'type' ? 'image/png' : 'image/jpeg' }))
    await refusal(prepare(snapshot, deps), kind === 'digest' ? 'PREVIEW_DIGEST_MISMATCH' : 'INVALID_JPEG')
  })

  it('enforces the decoded aggregate boundary and gives deterministic splits without encoding partial images', async () => {
    const limit = 1024 * 1024
    const exact = fixture(3, [limit - 24, 12, 12])
    const accepted = await prepare(await enumerate(exact.input, exact.deps), exact.deps)
    expect(accepted.images.reduce((sum, image) => sum + Buffer.from(image.data, 'base64').length, 0)).toBe(limit)
    const over = fixture(3, [limit - 23, 12, 12])
    const snapshot = await enumerate(over.input, over.deps)
    over.deps.encodeBase64 = vi.fn()
    const error = await refusal(prepare(snapshot, over.deps, { materialIds: [id(3), id(1), id(2)] }), 'IMAGE_BUDGET')
    expect(error.details).toEqual({ split_plan: [[id(1), id(2)], [id(3)]], oversized_ids: [] })
    expect(over.deps.readProjectPhotoPreview).toHaveBeenCalledTimes(3)
    expect(over.deps.encodeBase64).not.toHaveBeenCalled()
    const huge = fixture(3, [12, limit + 1, 12])
    const singleton = await refusal(prepare(await enumerate(huge.input, huge.deps), huge.deps), 'IMAGE_BUDGET')
    expect(singleton.details).toEqual({ split_plan: [[id(1)], [id(3)]], oversized_ids: [id(2)] })
  })

  it.each(['loadProjectPage', 'readProjectPhotoPreview', 'arrayBuffer', 'sha256', 'encodeBase64', 'finalSource'])('refuses stale state after the %s await', async boundary => {
    const { input, deps, bytes } = fixture(1)
    const snapshot = await enumerate(input, deps)
    let current = true
    const invalidate = value => { current = false; return value }
    if (boundary === 'arrayBuffer') deps.readProjectPhotoPreview.mockResolvedValue({ type: 'image/jpeg', arrayBuffer: async () => invalidate(bytes[0].buffer) })
    else if (boundary === 'sha256') deps.sha256 = async raw => invalidate(hash(raw))
    else if (boundary === 'encodeBase64') deps.encodeBase64 = async raw => invalidate(Buffer.from(raw).toString('base64'))
    else if (boundary === 'finalSource') deps.loadProjectPage.mockResolvedValueOnce({ revision: 7, digest: 'a'.repeat(64) }).mockImplementationOnce(async () => invalidate({ revision: 7, digest: 'a'.repeat(64) }))
    else {
      const original = deps[boundary].getMockImplementation()
      deps[boundary].mockImplementation(async (...args) => invalidate(await original(...args)))
    }
    await refusal(prepare(snapshot, deps, { isCurrent: () => current }), 'STALE')
  })

  it('rechecks saved source after preparation and refuses a changed saved page', async () => {
    const { input, deps } = fixture(1)
    const snapshot = await enumerate(input, deps)
    deps.loadProjectPage.mockResolvedValueOnce({ revision: 7, digest: 'a'.repeat(64) }).mockResolvedValueOnce({ revision: 8, digest: 'b'.repeat(64) })
    await refusal(prepare(snapshot, deps), 'STALE_SOURCE')
  })

  it('checks guards before requests and sanitizes transport errors', async () => {
    const { input, deps } = fixture(1)
    await refusal(enumerate({ ...input, isCurrent: () => false }, deps), 'STALE')
    const snapshot = await enumerate(input, deps)
    await refusal(prepare(snapshot, deps, { isCurrent: () => false }), 'STALE')
    deps.readProjectPhotoPreview.mockRejectedValue(new Error('credentials must never escape'))
    await refusal(prepare(snapshot, deps), 'READ_FAILED')
  })

  it('handles pre-abort, abort during enumeration, and abort during an uncooperative preview read', async () => {
    const { input, deps } = fixture(1)
    const early = new AbortController(); early.abort()
    await refusal(enumerate({ ...input, signal: early.signal }, deps), 'ABORTED')
    expect(deps.loadProjectPage).not.toHaveBeenCalled()
    const during = new AbortController()
    deps.listProjectPhotos.mockImplementationOnce(async () => { during.abort(); return { materials: [] } })
    await refusal(enumerate({ ...input, signal: during.signal }, deps), 'ABORTED')
    const snapshot = await enumerate(input, deps)
    const controller = new AbortController()
    deps.readProjectPhotoPreview.mockImplementation(() => { controller.abort(); return new Promise(() => {}) })
    await refusal(prepare(snapshot, deps, { signal: controller.signal }), 'ABORTED')
  })

  it('uses the real authorized API boundaries by default and never calls models or writes content', async () => {
    const { input, photos, bytes } = fixture(1)
    const writes = vi.spyOn(Storage.prototype, 'setItem')
    fetch.mockImplementation(async path => {
      if (path.endsWith('/page')) return { ok: true, json: async () => ({ page: { revision: 7, digest: 'a'.repeat(64), document: input.document } }) }
      if (path.endsWith('/materials')) return { ok: true, json: async () => ({ materials: photos, next_cursor: null }) }
      if (path.endsWith('/content?variant=preview')) return { ok: true, blob: async () => new NodeBlob([bytes[0]], { type: 'image/jpeg' }) }
      throw new Error('Unexpected route')
    })
    const snapshot = await enumerate(input)
    const result = await prepare(snapshot)
    expect(result.images[0].data).toBe(Buffer.from(bytes[0]).toString('base64'))
    expect(fetch).toHaveBeenCalledTimes(5)
    expect(fetch.mock.calls.every(([path, options]) => path.startsWith(`/api/projects/${PROJECT}/`) && (!options.method || options.method === 'GET'))).toBe(true)
    expect(writes).not.toHaveBeenCalled()
  })
})
