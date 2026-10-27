import { authHeaders, config, getStoredOrgId, noteUnauthorized } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'

const FIELDS = ['alignment', 'description', 'headline', 'label', 'status', 'theme']
const LIMITS = { label: 120, headline: 240, description: 1200, status: 80 }

export function validatePageDocument(document) {
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

async function request(path, { signal, ...options } = {}, consume = response => response) {
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
        422: 'Some page fields were not accepted. Check your draft and try again.',
      }
      const error = new Error(messages[response.status] || 'The page action failed. Your draft is still here. Try again.')
      error.status = response.status
      throw error
    }
    return await consume(response)
  } catch (error) {
    if (timedOut) throw new Error('The page service took too long. Your draft is still here. Try again.')
    throw error
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', cancel)
  }
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

export async function downloadProjectPageSource(projectId, revision, { signal } = {}) {
  if (!Number.isSafeInteger(revision) || revision < 1) throw new Error('Save your page before downloading its source.')
  return request(`${pagePath(projectId)}/source?revision=${revision}`, { signal }, async response => {
    if (!(response.headers.get('content-type') || '').toLowerCase().startsWith('application/zip')) {
      throw new Error('The page service did not return a source archive. Try again.')
    }
    const blob = await response.blob()
    if (!blob.size || blob.size > 8 * 1024 * 1024) throw new Error('The source archive could not be downloaded. Try again.')
    return blob
  })
}
