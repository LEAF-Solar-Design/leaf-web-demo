import { listProjectPhotos, readProjectPhotoPreview } from '../projects/api.js'
import { loadProjectPage } from './projectPageClient.js'
import { validatePortfolioDocument } from './portfolioDocument.js'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const HEX = /^[0-9a-f]{64}$/
const validUuid = value => typeof value === 'string' && value.length === 36 && UUID.test(value)
const validDigest = value => typeof value === 'string' && value.length === 64 && HEX.test(value)
const BUDGET = 1024 * 1024
const snapshots = new WeakSet()
const copy = value => JSON.parse(JSON.stringify(value))
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}
function fail(code, details) {
  const error = new Error(`Photo inspection refused: ${code}.`)
  error.name = 'ProjectPhotoInspectionError'
  error.code = code
  if (details) error.details = freeze(details)
  throw error
}
function guard(isCurrent, signal) {
  if (signal?.aborted) fail('ABORTED')
  if (typeof isCurrent !== 'function') fail('INVALID_INPUT')
  let current = false
  try { current = isCurrent() === true } catch { /* a broken guard fails closed */ }
  if (!current) fail('STALE')
}
// All asynchronous boundaries, including digest and encoding seams, are guarded.
async function step(action, isCurrent, signal) {
  guard(isCurrent, signal)
  let cancel
  try {
    const pending = action()
    const value = await (signal ? Promise.race([pending, new Promise((_, reject) => {
      cancel = () => reject(new Error('Cancelled'))
      signal.addEventListener('abort', cancel, { once: true })
      if (signal.aborted) cancel()
    })]) : pending)
    guard(isCurrent, signal)
    return value
  } catch (error) {
    guard(isCurrent, signal)
    // Do not expose transport errors, paths, response bodies, or credentials.
    if (error?.name === 'AbortError') fail('ABORTED')
    if (error?.name === 'ProjectPhotoInspectionError') throw error
    fail('READ_FAILED')
  } finally {
    if (cancel) signal.removeEventListener('abort', cancel)
  }
}
function validSource(projectId, source) {
  if (!validUuid(projectId) || source?.project_id !== projectId
    || !Number.isSafeInteger(source.revision) || source.revision < 1
    || !validDigest(source.digest)) fail('INVALID_SOURCE')
}
async function checkSource(projectId, source, isCurrent, signal, deps) {
  const saved = await step(() => (deps.loadProjectPage || loadProjectPage)(projectId, { signal }), isCurrent, signal)
  guard(isCurrent, signal)
  if (!saved || saved.revision !== source.revision || saved.digest !== source.digest
    || (saved.project_id !== undefined && saved.project_id !== projectId)) fail('STALE_SOURCE')
}
const metadataFields = ['material_id', 'name', 'media_type', 'byte_size', 'sha256', 'preview_sha256', 'width', 'height', 'created_at']
function metadata(value, projectId) {
  if (!value || !validUuid(value.material_id)
    || (value.project_id !== undefined && value.project_id !== projectId)
    || typeof value.name !== 'string' || !value.name
    || !['image/jpeg', 'image/png', 'image/heif'].includes(value.media_type)
    || !['byte_size', 'width', 'height'].every(key => Number.isSafeInteger(value[key]) && value[key] > 0)
    || !['sha256', 'preview_sha256'].every(key => validDigest(value[key]))
    || typeof value.created_at !== 'string' || !Number.isFinite(Date.parse(value.created_at))
    || (value.preview_media_type !== undefined && value.preview_media_type !== 'image/jpeg')) fail('INVALID_CATALOG')
  return Object.fromEntries(metadataFields.map(key => [key, value[key]]))
}

export async function enumerateProjectPhotoInspection({ projectId, expectedScope, source, document, isCurrent, signal }, deps = {}) {
  guard(isCurrent, signal)
  validSource(projectId, source)
  if (typeof expectedScope !== 'string' || !expectedScope) fail('INVALID_SCOPE')
  const capturedSource = freeze({ project_id: projectId, revision: source.revision, digest: source.digest })
  const scope = freeze(copy(expectedScope))
  let draft
  try { draft = freeze(validatePortfolioDocument(copy(document))) } catch { fail('INVALID_DOCUMENT') }
  await checkSource(projectId, capturedSource, isCurrent, signal, deps)
  guard(isCurrent, signal)
  const catalog = new Map(), cursors = new Set()
  let cursor
  do {
    const page = await step(() => (deps.listProjectPhotos || listProjectPhotos)(projectId, { cursor, signal, expectedScope: scope }), isCurrent, signal)
    guard(isCurrent, signal)
    if (!page || !Array.isArray(page.materials)) fail('INVALID_CATALOG')
    for (const raw of page.materials) {
      const photo = metadata(raw, projectId)
      if (catalog.has(photo.material_id)) {
        fail(JSON.stringify(catalog.get(photo.material_id)) === JSON.stringify(photo) ? 'DUPLICATE_ID' : 'CONFLICTING_METADATA')
      }
      catalog.set(photo.material_id, photo)
      if (catalog.size > 200) fail('CATALOG_LIMIT')
    }
    cursor = page.next_cursor
    if (cursor !== undefined && cursor !== null && (typeof cursor !== 'string' || !cursor.trim())) fail('INVALID_CURSOR')
    if (cursor != null) {
      if (cursors.has(cursor)) fail('CURSOR_LOOP')
      cursors.add(cursor)
    }
  } while (cursor != null)
  const photos = [...catalog.values()].sort((a, b) => a.material_id < b.material_id ? -1 : a.material_id > b.material_id ? 1 : 0)
  for (const photo of photos) photo.assignments = []
  for (const gallery of draft.categories) gallery.images.forEach((image, index) => {
    const photo = catalog.get(image.material_id)
    if (!photo) fail('MISSING_REFERENCE')
    if (image.sha256 !== photo.sha256 || image.preview_sha256 !== photo.preview_sha256) fail('REFERENCE_DIGEST_MISMATCH')
    photo.assignments.push({ gallery_slug: gallery.slug, gallery_title: gallery.title, index,
      hero: gallery.hero === index, caption: image.title, alt: image.alt })
  })
  guard(isCurrent, signal)
  const snapshot = freeze({ source: capturedSource, expectedScope: scope, document: draft, photos,
    coverage: { listed_ids: photos.map(photo => photo.material_id), listed_count: photos.length, complete: true } })
  snapshots.add(snapshot)
  return snapshot
}

async function sha256(bytes) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}
function encodeBase64(bytes) {
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192))
  return btoa(binary)
}
function splitPlan(prepared) {
  const batches = [], oversized_ids = []
  let batch = [], size = 0
  for (const { photo, bytes } of prepared) {
    if (bytes.length > BUDGET) {
      if (batch.length) batches.push(batch)
      batch = []; size = 0
      oversized_ids.push(photo.material_id)
      continue
    }
    if (size + bytes.length > BUDGET || batch.length === 3) {
      batches.push(batch); batch = []; size = 0
    }
    batch.push(photo.material_id); size += bytes.length
  }
  if (batch.length) batches.push(batch)
  return { split_plan: batches, oversized_ids }
}

export async function prepareProjectPhotoInspectionBatch({ snapshot, materialIds, isCurrent, signal }, deps = {}) {
  guard(isCurrent, signal)
  // Only immutable, authorized enumerations from this helper may supply pixels.
  if (!snapshots.has(snapshot)) fail('INVALID_SNAPSHOT')
  if (!Array.isArray(materialIds) || materialIds.length < 1 || materialIds.length > 3
    || new Set(materialIds).size !== materialIds.length) fail('INVALID_BATCH')
  const ids = freeze([...materialIds])
  const selected = snapshot.photos.filter(photo => ids.includes(photo.material_id))
  if (selected.length !== ids.length) fail('UNKNOWN_MATERIAL')
  const { source, expectedScope } = snapshot
  await checkSource(source.project_id, source, isCurrent, signal, deps)
  guard(isCurrent, signal)
  const prepared = []
  for (const photo of selected) {
    const blob = await step(() => (deps.readProjectPhotoPreview || readProjectPhotoPreview)(source.project_id, photo.material_id, { signal, expectedScope }), isCurrent, signal)
    guard(isCurrent, signal)
    if (!blob || blob.type !== 'image/jpeg' || typeof blob.arrayBuffer !== 'function') fail('INVALID_JPEG')
    const buffer = await step(() => blob.arrayBuffer(), isCurrent, signal)
    guard(isCurrent, signal)
    const bytes = new Uint8Array(buffer)
    if (bytes.length < 3 || bytes[0] !== 0xff || bytes[1] !== 0xd8 || bytes[2] !== 0xff) fail('INVALID_JPEG')
    const digest = await step(() => (deps.sha256 || sha256)(bytes), isCurrent, signal)
    guard(isCurrent, signal)
    if (digest !== photo.preview_sha256) fail('PREVIEW_DIGEST_MISMATCH')
    prepared.push({ photo, bytes })
  }
  if (prepared.reduce((sum, entry) => sum + entry.bytes.length, 0) > BUDGET) fail('IMAGE_BUDGET', splitPlan(prepared))
  const images = [], manifest = []
  for (const { photo, bytes } of prepared) {
    const data = await step(() => (deps.encodeBase64 || encodeBase64)(bytes), isCurrent, signal)
    guard(isCurrent, signal)
    images.push({ media_type: 'image/jpeg', data })
    manifest.push({ attachment_index: manifest.length, material_id: photo.material_id,
      original_sha256: photo.sha256, preview_sha256: photo.preview_sha256, assignments: photo.assignments })
  }
  await checkSource(source.project_id, source, isCurrent, signal, deps)
  guard(isCurrent, signal)
  return freeze({ source, images, manifest })
}
