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

async function request(path, options = {}) {
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
  return response.json()
}

function post(body, headers = {}) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) }
}

function finishFields(finish) {
  if (!finish || typeof finish !== 'object' || Array.isArray(finish)) invalid('finish', 'Choose a delivery profile.')
  const profile = bounded(finish.delivery_profile, 'delivery profile', 64)
  if (!/^[a-z][a-z0-9_]*$/.test(profile)) invalid('delivery profile', 'Choose a supported delivery profile.')
  const refs = finish.artifact_refs ?? []
  if (!Array.isArray(refs) || refs.length > 100) invalid('artifact references', 'Choose at most 100 project artifacts.')
  return { delivery_profile: profile, intended_user: bounded(finish.intended_user, 'intended user', 4096),
    workflow: bounded(finish.workflow, 'workflow', 32768), artifact_refs: refs.map(ref => bounded(ref, 'artifact reference', 2048)) }
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

export async function listReleases(projectId, id) {
  return request(`${releasePath(id)}?project_id=${encodeURIComponent(uuid(projectId, 'project'))}`)
}

export async function transitionRelease(projectId, id, releaseId, action) {
  if (!['pause', 'resume', 'cancel'].includes(action)) invalid('action', 'Choose pause, resume or cancel.')
  return request(`${releasePath(id, releaseId)}/${action}`, post({ project_id: uuid(projectId, 'project') }))
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
