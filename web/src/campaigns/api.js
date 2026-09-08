import { authHeaders, config } from '../api.js'

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const messages = {
  401: 'Your session is not signed in for this project any more.',
  403: 'You do not have permission to do that in this project.',
  404: 'That project is no longer available to you.',
  409: 'That change collided with another update. Reload and try again.',
  400: 'Some of that input was not accepted.',
  422: 'Some of that input was not accepted.',
  503: 'Campaigns are unavailable right now; retry in a moment.',
}
const conflicts = {
  answer_conflict: 'This question already has a different recorded answer. Reload to see it.',
  question_conflict: 'That question already exists with different text. Reload to see it.',
  idempotency_conflict: 'That submission key was already used for another campaign. Change the draft and try again.',
}

function invalid(field, message) {
  throw Object.assign(new Error(message), { status: 0, invalidField: field })
}

function uuid(value, field) {
  const id = String(value ?? '').trim()
  if (!UUID_SHAPE.test(id)) invalid(field, `That ${field} is not a valid id, so nothing was sent.`)
  return id
}

function bounded(value, field, max) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    invalid(field, `${field[0].toUpperCase() + field.slice(1)} must contain 1 to ${max} characters.`)
  }
  return value
}

async function request(path, options = {}, binary = false) {
  const identity = authHeaders()
  if (!identity.Authorization) throw Object.assign(new Error('Sign in to submit a campaign.'), { status: 0 })
  const headers = { ...options.headers, ...identity, 'X-Tenant-Id': config.tenant }
  try {
    const org = localStorage.getItem('leaf.org_id')
    if (org) headers['X-Org-Id'] = org
  } catch { /* Storage can be disabled. Bearer authority remains required. */ }
  let response
  try {
    response = await fetch(`${config.apiBase}${path}`, { ...options, headers })
  } catch (cause) {
    throw Object.assign(new Error('Campaigns could not be reached. Try again.'), { status: 0, retryable: true, cause })
  }
  if (!response.ok) {
    let body = null
    try { body = await response.json() } catch { /* Use the status message. */ }
    const code = body?.error?.error_code
    const detail = body?.error?.message
    const plain = typeof detail === 'string' && detail.trim() && detail.length <= 240
      && !/\/api\/|->\s*\d{3}\b|[{}<>]/.test(detail)
    const message = (response.status === 409 && conflicts[code])
      || (plain && detail.trim()) || messages[response.status] || 'That campaign action did not go through.'
    throw Object.assign(new Error(message), {
      status: response.status, body, code, retryable: body?.error?.retryable === true,
    })
  }
  return binary ? response : response.json()
}

function post(body, headers = {}) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) }
}

const inputMediaTypes = { json: 'application/json', dxf: 'application/dxf', csv: 'text/csv', txt: 'text/plain', md: 'text/markdown' }
const maxInputBytes = 1048576

// Keep file contents inside the existing authenticated project transport.
export async function uploadProjectInput(projectId, file) {
  const project = uuid(projectId, 'project')
  const name = typeof file?.name === 'string' ? file.name.split(/[\\/]/).pop() : ''
  const extension = name.match(/\.([^.]+)$/)?.[1].toLowerCase()
  const mediaType = Object.hasOwn(inputMediaTypes, extension) ? inputMediaTypes[extension] : null
  if (!mediaType) invalid('input file', 'Choose a JSON, ASCII DXF, CSV, TXT or MD file.')
  if (!Number.isSafeInteger(file?.size) || file.size < 1 || file.size > maxInputBytes) {
    invalid('input file', 'Choose a file from 1 byte through 1 MiB.')
  }
  let bytes
  try {
    bytes = new Uint8Array(typeof file.arrayBuffer === 'function' ? await file.arrayBuffer() : await new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(new Error('File read failed'))
      reader.onabort = () => reject(new Error('File read cancelled'))
      reader.readAsArrayBuffer(file)
    }))
  } catch { invalid('input file', 'The file could not be read. Select it again.') }
  if (bytes.byteLength !== file.size || bytes.byteLength < 1 || bytes.byteLength > maxInputBytes) {
    invalid('input file', 'Choose a file from 1 byte through 1 MiB.')
  }
  let content
  try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) }
  catch { invalid('input file', 'The file must contain valid UTF-8 text.') }
  if (content.includes('\u0000') || (extension === 'dxf' && /[^\x01-\x7f]/.test(content))) {
    invalid('input file', extension === 'dxf' ? 'Choose an ASCII DXF file, not a binary drawing.' : 'The file must contain text without null bytes.')
  }
  if (!globalThis.crypto?.subtle?.digest) invalid('input file', 'This browser cannot prepare the file safely. Try a secure browser session.')
  const hash = async value => Array.from(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', value)),
    byte => byte.toString(16).padStart(2, '0')).join('')
  const digest = await hash(bytes)
  const stem = name.slice(0, -(extension.length + 1)).replace(/[^A-Za-z0-9._-]/g, '_')
    .replace(/\.{2,}/g, '_').replace(/^[._-]+|[._-]+$/g, '').slice(0, 96) || 'input'
  const path = `inputs/${digest}/${stem}.${extension}`
  const idempotencyKey = `finish-input-${await hash(new TextEncoder().encode(`${project}\n${path}\n${digest}`))}`
  const snapshot = await request(`/api/projects/${project}/lifecycle`, { redirect: 'error' })
  if (!Array.isArray(snapshot?.files)) throw new Error('Project material could not be checked. Try adding the file again.')
  const existing = snapshot.files.find(item => item.path === path)
  if (existing && (existing.content_sha256 !== digest || existing.media_type !== mediaType || existing.content !== content)) {
    throw new Error('Different project material already uses this input path. Keep that material and select a different file name.')
  }
  const result = await request(`/api/projects/${project}/files`, {
    ...post({ path, media_type: mediaType, content }, { 'Idempotency-Key': idempotencyKey }), method: 'PUT', redirect: 'error',
  })
  if (result?.file?.path !== path || result.file.content_sha256 !== digest || result.file.media_type !== mediaType) {
    throw new Error('The server did not confirm this input. Try adding the file again.')
  }
  return { path, name, sha256: digest }
}

function finishFields(finish) {
  if (!finish || typeof finish !== 'object' || Array.isArray(finish)) invalid('finish', 'Choose a delivery profile.')
  const profile = bounded(finish.delivery_profile, 'delivery profile', 64)
  if (!/^[a-z][a-z0-9_]*$/.test(profile)) invalid('delivery profile', 'Choose a supported delivery profile.')
  const refs = finish.artifact_refs ?? []
  if (!Array.isArray(refs) || refs.length > 32) invalid('artifact references', 'Choose at most 32 project artifacts.')
  let deadline
  if (finish.deadline_at != null && finish.deadline_at !== '') {
    if (typeof finish.deadline_at !== 'string' || !Number.isFinite(Date.parse(finish.deadline_at))) invalid('deadline', 'Choose a valid release deadline.')
    deadline = new Date(finish.deadline_at).toISOString()
  }
  return { delivery_profile: profile, intended_user: bounded(finish.intended_user, 'intended user', 2000),
    workflow: bounded(finish.workflow, 'workflow', 2000), artifact_refs: refs.map(ref => bounded(ref, 'artifact reference', 512)),
    ...(deadline ? { deadline_at: deadline } : {}) }
}

export async function submitCampaign({ projectId, title, prompt, idempotencyKey, mode, finish }) {
  const body = { project_id: uuid(projectId, 'project'), title: bounded(title, 'title', 200), prompt: bounded(prompt, 'prompt', 32768) }
  if (mode !== undefined && mode !== 'finish') invalid('mode', 'Choose a supported campaign mode.')
  if (mode === 'finish') Object.assign(body, { mode, finish: finishFields(finish) })
  bounded(idempotencyKey, 'submission key', 128)
  return request('/api/campaigns', post(body, { 'Idempotency-Key': idempotencyKey }))
}

const releasePath = (id, releaseId) => `/api/campaigns/${uuid(id, 'campaign')}/releases${releaseId === undefined ? '' : `/${uuid(releaseId, 'release')}`}`

export async function createRelease(projectId, id, { finish, idempotencyKey }) {
  return request(releasePath(id), post({ project_id: uuid(projectId, 'project'), finish: finishFields(finish) },
    { 'Idempotency-Key': bounded(idempotencyKey, 'submission key', 128) }))
}

export async function getRelease(projectId, id, releaseId) {
  return request(`${releasePath(id, releaseId)}?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

// Returns verified bytes only: { bytes: ArrayBuffer, mediaType: string, name: string }.
// Evidence URLs are deliberately never consulted or followed with app credentials.
export async function downloadReleaseArtifact(projectId, id, releaseId, artifact) {
  const name = artifact?.name
  if (typeof name !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$/.test(name)
      || name.includes('..') || /[. ]$/.test(name)) {
    invalid('output', 'This output has an invalid file name. Reload the release.')
  }
  if (artifact.valid !== true || artifact.retrieved !== true
      || !Number.isSafeInteger(artifact.byte_count) || artifact.byte_count <= 0 || artifact.byte_count > 1048576
      || typeof artifact.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(artifact.sha256)) {
    invalid('output', 'Verified output details are unavailable. Reload the release.')
  }
  if (!globalThis.crypto?.subtle?.digest) throw new Error('This browser cannot verify downloads. Use a browser with secure download verification.')
  const path = `${releasePath(id, releaseId)}/artifacts/${encodeURIComponent(name)}?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`
  const response = await request(path, { redirect: 'error' }, true)
  const length = response.headers?.get('Content-Length')
  if (length && (!/^\d+$/.test(length) || Number(length) !== artifact.byte_count)) {
    throw new Error('The output size changed. Reload the release before downloading.')
  }
  let bytes
  try { bytes = await response.arrayBuffer() }
  catch { throw new Error('The output download was interrupted. Try downloading again.') }
  if (bytes.byteLength !== artifact.byte_count || bytes.byteLength > 1048576) {
    throw new Error('The downloaded output has the wrong size. Reload the release.')
  }
  let digest
  try {
    digest = Array.from(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes)),
      value => value.toString(16).padStart(2, '0')).join('')
  } catch { throw new Error('This browser could not verify the output. Try downloading again.') }
  if (digest !== artifact.sha256) throw new Error('The downloaded output did not match the verified release. Reload the release.')
  const mediaType = (response.headers?.get('Content-Type') || 'application/octet-stream').split(';')[0].trim().toLowerCase()
  return { bytes, mediaType, name }
}

export async function listReleases(projectId, id) {
  return request(`${releasePath(id)}?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function transitionRelease(projectId, id, releaseId, action, authority) {
  if (!['pause', 'resume', 'cancel', 'advance'].includes(action)) invalid('action', 'Choose pause, resume, cancel or advance.')
  const headers = {}
  if (['resume', 'advance'].includes(action) && authority !== undefined) {
    for (const [field, header] of [['sessionId', 'X-Authority-Session-Id'], ['turnId', 'X-Authority-Turn-Id']]) {
      const value = authority?.[field]
      if (typeof value !== 'string' || !UUID_SHAPE.test(value)) invalid('authority', 'The project conversation did not provide valid continuation authority.')
      headers[header] = value
    }
  }
  return request(`${releasePath(id, releaseId)}/${action}`, post({ project_id: uuid(projectId, 'project') }, headers))
}

export async function reviseRelease(projectId, id, releaseId, { workflow, reason, idempotencyKey }) {
  return request(`${releasePath(id, releaseId)}/revise`, post({ project_id: uuid(projectId, 'project'),
    workflow: bounded(workflow, 'workflow', 16384), reason: bounded(reason, 'reason', 4096) },
  { 'Idempotency-Key': bounded(idempotencyKey, 'submission key', 128) }))
}

export async function retryReleaseStage(projectId, id, releaseId, stage) {
  if (!['implementation', 'publication', 'deployment', 'user_verification', 'delivery'].includes(stage)) invalid('stage', 'Choose a release stage.')
  return request(`${releasePath(id, releaseId)}/retry`, post({ project_id: uuid(projectId, 'project'), stage }))
}

export async function listCampaigns(projectId, limit = 50) {
  const project = uuid(projectId, 'project')
  const count = Number.isFinite(Number(limit)) ? Math.max(1, Math.min(200, Math.trunc(Number(limit)))) : 50
  return request(`/api/campaigns?project_id=${encodeURIComponent(project)}&limit=${count}`)
}

export async function getCampaign(projectId, id) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function getExecution(projectId, id, limit = 50) {
  const project = uuid(projectId, 'project')
  const count = Number.isFinite(Number(limit)) ? Math.max(1, Math.min(200, Math.trunc(Number(limit)))) : 50
  return request(`/api/campaigns/${uuid(id, 'campaign')}/execution?project_id=${encodeURIComponent(project)}&limit=${count}`)
}

export async function askQuestion(projectId, id, { questionKey, prompt }) {
  bounded(questionKey, 'question key', 128)
  if (!/^[A-Za-z0-9._-]+$/.test(questionKey)) invalid('question key', 'The question key is not valid, so nothing was sent.')
  return request(`/api/campaigns/${uuid(id, 'campaign')}/questions`, post({
    project_id: uuid(projectId, 'project'), question_key: questionKey, prompt: bounded(prompt, 'question', 4096),
  }))
}

export async function listQuestions(projectId, id) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/questions?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function listEnrollments(projectId, id) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function listCapabilities(projectId, id) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/capabilities?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function bindPublication(projectId, id, enrollmentId, changeSetId) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments/${uuid(enrollmentId, 'enrollment')}/publication`, post({
    project_id: uuid(projectId, 'project'), change_set_id: bounded(changeSetId, 'published tool', 200),
  }))
}

export async function invokeCapability(projectId, id, enrollmentId, { effectiveCatalogDigest, idempotencyKey }) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments/${uuid(enrollmentId, 'enrollment')}/invoke`, post({
    project_id: uuid(projectId, 'project'), effective_catalog_digest: bounded(effectiveCatalogDigest, 'catalog digest', 200),
  }, { 'Idempotency-Key': bounded(idempotencyKey, 'submission key', 128) }))
}

export async function requestEnrollment(projectId, id, machineId, capability) {
  if (capability !== undefined && !['campaign.host-enrollment', 'campaign.native-release'].includes(capability)) {
    invalid('capability', 'Choose a supported registration capability.')
  }
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments`, post({
    project_id: uuid(projectId, 'project'), machine_id: bounded(machineId, 'machine', 200),
    ...(capability === undefined ? {} : { capability }),
  }))
}

export async function enableEnrollment(projectId, id, enrollmentId) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments/${uuid(enrollmentId, 'enrollment')}/enable`, post({ project_id: uuid(projectId, 'project') }))
}

export async function revokeEnrollment(projectId, id, enrollmentId) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/enrollments/${uuid(enrollmentId, 'enrollment')}/revoke`, post({ project_id: uuid(projectId, 'project') }))
}

export async function answerQuestion(projectId, id, qid, answer) {
  return request(`/api/campaigns/${uuid(id, 'campaign')}/questions/${uuid(qid, 'question')}/answer`, post({
    project_id: uuid(projectId, 'project'), answer: bounded(answer, 'answer', 8192),
  }))
}
