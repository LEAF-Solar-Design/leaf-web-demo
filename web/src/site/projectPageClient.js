import { authHeaders, config, getStoredOrgId, noteUnauthorized } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import { validatePortfolioDocument } from './portfolioDocument.js'

const FIELDS = ['alignment', 'description', 'headline', 'label', 'status', 'theme']
const LIMITS = { label: 120, headline: 240, description: 1200, status: 80 }

export function validatePageDocument(document) {
  if (document && Object.hasOwn(document, 'schema')) return validatePortfolioDocument(document)
  if (!document || typeof document !== 'object' || Array.isArray(document)
    || Object.keys(document).sort().join(',') !== FIELDS.join(',')) throw new Error('The page has an unsupported format.')
  for (const [field, limit] of Object.entries(LIMITS)) {
    const value = document[field]
    if (typeof value !== 'string' || Array.from(value).length > limit || /[<>]/.test(value)
      || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(value.replaceAll('\n', ''))
      || /(?:[a-z][a-z0-9+.-]*:\/\/|\b(?:https?|ftp|file|data|javascript):|\bwww\.)/i.test(value)
      || (['label', 'headline'].includes(field) && !value.trim())) {
      throw new Error(`Check the ${field}. Use plain text within ${limit} characters.`)
    }
  }
  if (!['cream', 'light', 'dark'].includes(document.theme) || !['center', 'left'].includes(document.alignment)) {
    throw new Error('Choose one of the available page layouts.')
  }
  return document
}

function pagePath(projectId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(projectId || '')) {
    throw new Error('Choose a project before opening a page.')
  }
  return `/api/projects/${projectId}/page`
}

async function request(path, { signal, native = false, ...options } = {}, consume = response => response) {
  const headers = { ...authHeaders(), ...options.headers }
  const org = getStoredOrgId()
  const actor = getStoredActorBindingId()
  if (org) headers['X-Org-Id'] = org
  if (actor) headers['X-Actor-Binding-Id'] = actor
  const controller = new AbortController()
  const cancel = () => controller.abort()
  let timedOut = false
  if (signal?.aborted) cancel()
  signal?.addEventListener('abort', cancel, { once: true })
  const timeout = setTimeout(() => { timedOut = true; cancel() }, 30000)
  try {
    let response
    try { response = await fetch(`${config.apiBase}${path}`, { ...options, headers, signal: controller.signal, cache: 'no-store' }) }
    catch (error) {
      if (error.name === 'AbortError') throw error
      throw new Error('Could not reach the page service. Your draft is still here. Try again.')
    }
    if (response.status === 401) noteUnauthorized(response, path, headers.Authorization)
    if (!response.ok) {
      const messages = {
        401: 'Sign in again to access this page.',
        403: 'You do not have permission to access or change this page.',
        404: 'This project is no longer available to you.',
        409: 'The saved page changed elsewhere. Your draft is still here. Reload the saved page to continue.',
        413: 'The page document is too large. Reduce its content and try again.',
        422: 'Some page fields were not accepted. Check your draft and try again.',
      }
      let message = messages[response.status]
      if (response.status === 422) {
        try {
          const { detail } = await response.json()
          if (typeof detail === 'string' && /^Portfolio (?:previews exceed 64 MiB|(?:source|archive) exceeds 80 MiB)/.test(detail)) message = 'This portfolio is too large to export. Reduce the selected photos, save, and try again.'
          if (typeof detail === 'string' && detail.startsWith('A portfolio photo is missing from this project')) message = 'A selected photo is missing or changed. Re-select it from this project, save, and try again.'
        } catch { /* retain the bounded default message */ }
      }
      const error = new Error(message || 'The page action failed. Your draft is still here. Try again.')
      error.status = response.status
      throw error
    }
    return await consume(response)
  } catch (error) {
    if (native) {
      if (error.name === 'AbortError' && !timedOut) throw error
      const failure = new Error(timedOut ? 'The iOS preview service took too long. Try again.' : 'Could not access the iOS preview. Try again.')
      failure.status = error.status
      throw failure
    }
    if (timedOut) throw new Error('The page service took too long. Your draft is still here. Try again.')
    throw error
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', cancel)
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const DIGEST = /^[0-9a-f]{64}$/
const validUuid = value => typeof value === 'string' && value.length === 36 && UUID.test(value)
const validDigest = value => typeof value === 'string' && value.length === 64 && DIGEST.test(value)
const MAX_SCREENSHOT = 16 * 1024 * 1024
const nativeInvalid = () => { throw new Error('The iOS preview response could not be verified. Try again.') }

function nativePath(projectId, jobId) {
  if (!validUuid(projectId) || (jobId !== undefined && !validUuid(jobId))) nativeInvalid()
  return `/api/projects/${projectId}/native-previews${jobId === undefined ? '' : `/${jobId}`}`
}

export function validateNativePreview(value, expected = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !validUuid(value.job_id)
    || !Number.isSafeInteger(value.revision) || value.revision < 1
    || !validDigest(value.document_sha256) || !validDigest(value.archive_sha256)
    || !['queued', 'running', 'complete', 'failed', 'cancelled'].includes(value.status)) nativeInvalid()
  for (const field of ['job_id', 'revision', 'document_sha256', 'archive_sha256']) {
    if (expected[field] !== undefined && value[field] !== expected[field]) nativeInvalid()
  }
  if (value.status === 'complete') {
    const receipt = value.receipt
    if (value.evidence_basis !== 'authenticated-worker-report' || !receipt || Array.isArray(receipt)
      || Object.keys(receipt).sort().join(',') !== 'kind,receipt_id,sha256'
      || receipt.kind !== 'simulator' || !validUuid(receipt.receipt_id) || !validDigest(receipt.sha256)) nativeInvalid()
  }
  return { job_id: value.job_id, status: value.status, revision: value.revision,
    document_sha256: value.document_sha256, archive_sha256: value.archive_sha256,
    ...(value.status === 'complete' ? { receipt: { ...value.receipt }, evidence_basis: value.evidence_basis } : {}) }
}

export async function submitProjectNativePreview(projectId, revision, idempotencyKey, { signal } = {}) {
  if (!Number.isSafeInteger(revision) || revision < 1 || typeof idempotencyKey !== 'string'
    || idempotencyKey.length < 1 || idempotencyKey.length > 200 || /[^\x21-\x7e]/.test(idempotencyKey)) nativeInvalid()
  return request(nativePath(projectId), { native: true, signal, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ revision }),
  }, async response => validateNativePreview(await response.json(), { revision }))
}

export async function loadProjectNativePreview(projectId, jobId, { signal } = {}) {
  return request(nativePath(projectId, jobId), { native: true, signal },
    async response => validateNativePreview(await response.json(), { job_id: jobId }))
}

export async function downloadProjectNativePreviewScreenshot(projectId, jobId, { signal } = {}) {
  return request(`${nativePath(projectId, jobId)}/screenshot`, { native: true, signal }, async response => {
    if ((response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase() !== 'image/png') nativeInvalid()
    const length = response.headers.get('content-length')
    if (length && (!/^\d+$/.test(length) || Number(length) > MAX_SCREENSHOT)) nativeInvalid()
    const reader = response.body?.getReader()
    let blob
    if (reader) {
      const chunks = []
      let size = 0
      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          size += value.byteLength
          if (size > MAX_SCREENSHOT) nativeInvalid()
          chunks.push(value)
        }
        blob = new Blob(chunks, { type: 'image/png' })
      } finally {
        try { await reader.cancel() } finally { reader.releaseLock() }
      }
    } else blob = await response.blob()
    if (blob.size < 33 || blob.size > MAX_SCREENSHOT) nativeInvalid()
    const signature = new Uint8Array(await blob.slice(0, 16).arrayBuffer())
    if (![137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82].every((byte, index) => signature[index] === byte)) nativeInvalid()
    return blob
  })
}

async function readPage(response, allowAbsent) {
  const body = await response.json()
  if (allowAbsent && body?.page === null) return null
  const page = body?.page
  if (!page || !Number.isSafeInteger(page.revision) || page.revision < 1 || !/^[0-9a-f]{64}$/.test(page.digest || '')) {
    throw new Error('The page service returned an invalid saved page. Try opening it again.')
  }
  validatePageDocument(page.document)
  return { revision: page.revision, digest: page.digest, document: page.document }
}

export async function loadProjectPage(projectId, { signal } = {}) {
  return request(pagePath(projectId), { signal }, response => readPage(response, true))
}

export async function saveProjectPage(projectId, document, expectedRevision, { signal } = {}) {
  validatePageDocument(document)
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) throw new Error('Open the page again before saving.')
  return request(pagePath(projectId), {
    method: 'PUT', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_revision: expectedRevision, document }),
  }, response => readPage(response, false))
}

export async function downloadProjectPageSource(projectId, revision, { signal, target = 'web' } = {}) {
  if (!Number.isSafeInteger(revision) || revision < 1) throw new Error('Save your page before downloading its source.')
  if (!['web', 'ios'].includes(target)) throw new Error('Choose a supported source format.')
  const suffix = target === 'ios' ? '&target=ios' : ''
  return request(`${pagePath(projectId)}/source?revision=${revision}${suffix}`, { signal }, async response => {
    if (!(response.headers.get('content-type') || '').toLowerCase().startsWith('application/zip')) {
      throw new Error('The page service did not return a source archive. Try again.')
    }
    const blob = await response.blob()
    if (!blob.size || blob.size > 80 * 1024 * 1024) throw new Error('The source archive could not be downloaded. Try again.')
    return blob
  })
}
