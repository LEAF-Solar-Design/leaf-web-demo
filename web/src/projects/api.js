// Project-lifecycle transport (cards B-U1..B-U6) — the ONLY module that talks
// to platform/api.py's `/api/projects/{id}/...` lifecycle routes.
//
// IDENTITY CONTRACT (platform/api.py `_get_lifecycle_actor`, deps.py:100-160):
// every lifecycle route resolves an actor from three request headers.
//   Authorization: Bearer <jwt>   — the only thing that counts under
//                                   LEAF_AUTH_LIVE=1; the two below are ignored.
//   X-Org-Id                      — REQUIRED with auth off, else 400.
//   X-Actor-Binding-Id            — REQUIRED with auth off, else 400.
// Both dev-seam headers are therefore sent on EVERY call: harmless under live
// auth (the server discards them), and the difference between a working demo
// and a blanket 400 with auth off. The previous revision of this module sent
// the bearer alone, which is exactly why GET /api/projects 400'd and
// POST /api/projects/blank could never succeed in the demo.
//
// X-Org-Id reads the same localStorage key as web/src/api.js orgHeaders()
// (`leaf.org_id`, WORKSPACE_ORG_KEY in controllers/workspace/
// createWorkspaceController.js) so there is one org of record, not two.
//
// EVERY id that reaches a URL path is validated as a UUID here before the
// fetch, and every mutation carries a fresh Idempotency-Key. Failures surface
// as plain-English messages that survive errorHumanize.js verbatim — a 403 must
// not be laundered into "temporary problem, try again", which is a lie the
// caller will retry forever.
import { config } from '../api.js'
import { WORKSPACE_ORG_KEY } from '../controllers/workspace/createWorkspaceController.js'

const AUTH_KEY = 'leaf.jwt'
// The actor's platform identity binding. platform/api.py has NO
// binding-by-email lookup route (see web/e2e/lifecycle.spec.mjs:40-43), so the
// binding id is the only actor handle the demo seam can present.
export const ACTOR_BINDING_KEY = 'leaf.actor_binding_id'

const UUID_SHAPE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// The server's exact role vocabulary (platform/project_lifecycle.py
// PROJECT_ROLES). Note `read_only`, NOT the UI's `read-only`: the two spellings
// are translated in useProjectLifecycle.js, and this set is the wire side.
export const API_ROLES = new Set(['owner', 'editor', 'reviewer', 'read_only'])

function readStored(key) {
  try { return localStorage.getItem(key) || null } catch { return null }
}

export function getStoredActorBindingId() {
  return readStored(ACTOR_BINDING_KEY)
}

export function setStoredActorBindingId(id) {
  try {
    if (id) localStorage.setItem(ACTOR_BINDING_KEY, id)
    else localStorage.removeItem(ACTOR_BINDING_KEY)
  } catch { /* lifecycle identity persistence is best effort */ }
}

// Fails closed on a malformed id rather than letting it reach a URL path,
// where it would come back as an opaque 422 with no hint of which id was bad.
function requireUuid(value, label) {
  const id = String(value ?? '').trim()
  if (!UUID_SHAPE.test(id)) {
    const e = new Error(`That ${label} is not a valid id, so nothing was sent.`)
    e.status = 0
    e.invalidField = label
    throw e
  }
  return id
}

// Read at call time (not module load) so a sign-in mid-session is picked up.
function identityHeaders(orgId) {
  const headers = {}
  const tok = readStored(AUTH_KEY)
  if (tok) headers.Authorization = `Bearer ${tok}`
  const org = orgId || readStored(WORKSPACE_ORG_KEY)
  if (org) headers['X-Org-Id'] = org
  const binding = readStored(ACTOR_BINDING_KEY)
  if (binding) headers['X-Actor-Binding-Id'] = binding
  return headers
}

// Fresh per call — a retried submit must never collide with an id already
// used, which would replay the wrong receipt instead of acting anew.
function idempotencyKey() {
  const uuid = globalThis.crypto?.randomUUID?.()
  return uuid || `k-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

// Status -> a plain-English sentence. Deliberately free of an HTTP verb, an
// /api/ path, and a "-> NNN" arrow so errorHumanize.js's isPlainEnglish()
// passes it through instead of collapsing it to the generic service line.
// The server's own `detail` wins when it is already a plain sentence, because
// it is more specific than anything this table can say.
const STATUS_MESSAGE = {
  400: 'This workspace is missing the identity that project actions need — reopen the project and try again.',
  401: 'Your session is not signed in for this project any more.',
  403: 'You do not have permission to do that in this project.',
  404: 'That project is no longer available to you.',
  409: 'That change collided with another update — reload the project and try again.',
  422: 'Some of that input was not accepted.',
}

function detailOf(body) {
  const detail = body?.detail
  if (typeof detail !== 'string') return null
  const text = detail.trim()
  if (!text || text.length > 160) return null
  // Never promote a raw transport/path/status string into the message.
  if (/\/api\/|->\s*\d{3}\b|[{}<>]/.test(text)) return null
  return text
}

async function request(path, opts = {}) {
  const headers = { ...(opts.headers || {}), ...identityHeaders(opts.orgId) }
  const { orgId: _ignored, ...init } = opts
  const res = await fetch(`${config.apiBase}${path}`, { ...init, headers })
  if (!res.ok) {
    let body = null
    try { body = await res.clone().json() } catch { /* non-JSON error body */ }
    const message =
      detailOf(body)
      || STATUS_MESSAGE[res.status]
      || 'That project action did not go through — nothing changed.'
    const e = new Error(message)
    e.status = res.status // callers gate on status without string-matching
    e.body = body
    // Kept OFF `message`/`error` on purpose: errorHumanize.js reads only those
    // two, so the raw route stays available for logs without ever reaching a user.
    e.transport = `${init.method || 'GET'} ${path}`
    throw e
  }
  if (res.status === 204) return {}
  return res.json()
}

function mutation(method, body) {
  const opts = {
    method,
    headers: { 'Idempotency-Key': idempotencyKey() },
  }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  return opts
}

// POST /api/projects/blank {name} -> {project:{project_id,name,status,profile},
// receipt, replayed}. The idempotent blank-browser-entry factory
// (platform/project_lifecycle.py create_blank_project) and the ONE project
// creation route this surface uses — /api/projects (get_or_create_project)
// mints no lifecycle receipt and no owner membership binding, so a project made
// through it is invisible to every route below.
export async function createBlankProject(name, orgId) {
  const trimmed = String(name ?? '').trim()
  if (!trimmed) throw new Error('A project needs a name before it can be created.')
  const data = await request('/api/projects/blank', { ...mutation('POST', { name: trimmed }), orgId })
  return data.project
}

// GET /api/projects/{id}/lifecycle -> {project, members, files, receipts}.
// The single read every lifecycle component is fed from; see
// useProjectLifecycle.js for the one-fetch/refetch-after-mutation contract.
export async function getProjectLifecycle(projectId) {
  const id = requireUuid(projectId, 'project')
  return request(`/api/projects/${id}/lifecycle`)
}

// POST /api/projects/{id}/members {binding_id, role} -> {member, receipt}.
// Also the ROLE-CHANGE route: platform/project_lifecycle.py:496-503 updates the
// role in place when that binding is already an active member, so an invite of
// an existing member with a new role IS the demotion/promotion. There is no
// separate PATCH route to call.
export async function inviteMember(projectId, bindingId, role) {
  const id = requireUuid(projectId, 'project')
  const binding = requireUuid(bindingId, 'member binding')
  if (!API_ROLES.has(role)) {
    throw new Error('Pick one of owner, editor, reviewer, or read-only for that member.')
  }
  return request(`/api/projects/${id}/members`, mutation('POST', { binding_id: binding, role }))
}

// DELETE /api/projects/{id}/members/{membership_id} -> {member, receipt}.
export async function revokeMember(projectId, membershipId) {
  const id = requireUuid(projectId, 'project')
  const membership = requireUuid(membershipId, 'membership')
  return request(`/api/projects/${id}/members/${membership}`, mutation('DELETE'))
}

// POST /api/projects/{id}/clone {name} -> {project, source_project_id,
// copied_file_count, receipt}. The clone is a NEW project the caller is bound
// to; the response's `project` is the server's answer to "what did you create".
export async function cloneProject(projectId, name) {
  const id = requireUuid(projectId, 'project')
  const trimmed = String(name ?? '').trim()
  if (!trimmed) throw new Error('A clone needs a name before it can be created.')
  return request(`/api/projects/${id}/clone`, mutation('POST', { name: trimmed }))
}

// POST /api/projects/{id}/export -> {export, export_sha256, file_count,
// member_count, receipt}. `export` is the sanitized artifact itself
// (leaf.project-export.v1), already stripped of draft/working state server-side.
// `signal` is honored so a caller's own timeout bound (ExportDialog's
// ExportTimeoutError) actually cancels the in-flight request instead of leaving
// it running behind a dismissed dialog.
export async function exportProject(projectId, { signal } = {}) {
  const id = requireUuid(projectId, 'project')
  return request(`/api/projects/${id}/export`, { ...mutation('POST'), signal })
}

// POST /api/projects/{id}/reset -> {..., receipt}. Deletes every file; the
// project, its members, and its receipt history survive. TERMINAL: the server
// mints no restore token, so no undo affordance may be offered for it.
export async function resetProject(projectId) {
  const id = requireUuid(projectId, 'project')
  return request(`/api/projects/${id}/reset`, mutation('POST'))
}

// DELETE /api/projects/{id} -> {..., receipt}. TERMINAL, same as reset.
export async function deleteProject(projectId) {
  const id = requireUuid(projectId, 'project')
  return request(`/api/projects/${id}`, mutation('DELETE'))
}
