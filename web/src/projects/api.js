// Projects surface transport (card B-U1) — the signed-in user's own project
// list, and blank-project creation (platform/api.py `/api/projects`,
// POST `/api/projects/blank`).
//
// Self-contained on purpose: this module sends only a bearer token, never a
// client-picked org or actor id, so identity and membership are resolved
// server-side from the JWT (platform/api.py get_org_id / _get_lifecycle_actor).
// listProjects() returns the server's list verbatim — this module never
// filters, sorts by ownership, or otherwise re-derives "yours" client-side.

const AUTH_KEY = 'leaf.jwt'

// Read at call time (not module load) so a sign-in mid-session is picked up.
function authHeaders() {
  try {
    const tok = localStorage.getItem(AUTH_KEY)
    return tok ? { Authorization: `Bearer ${tok}` } : {}
  } catch { return {} }
}

// Fresh per call — a retried submit must never collide with an id already
// used, which would replay the wrong receipt instead of creating anew.
function idempotencyKey() {
  const uuid = globalThis.crypto?.randomUUID?.()
  return uuid || `k-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function request(path, opts) {
  const headers = { ...(opts?.headers || {}), ...authHeaders() }
  const res = await fetch(path, { ...opts, headers })
  if (!res.ok) {
    const e = new Error(`${opts?.method || 'GET'} ${path} -> ${res.status}`)
    e.status = res.status // callers gate on status without string-matching
    try { e.body = await res.clone().json() } catch { /* non-JSON errors keep the stable status-only contract */ }
    throw e
  }
  return res.json()
}

// GET /api/projects -> {projects:[{project_id,name,status,created_at,...}]}.
// Server-derived membership only: whatever the server sends back IS the
// signed-in user's project list, rendered as-is.
export async function listProjects() {
  const data = await request('/api/projects')
  return data.projects || []
}

// POST /api/projects/blank {name} -> {project:{project_id,name,status,profile},
// receipt, replayed}. The "blank browser entry" profile (project_lifecycle.py
// create_blank_project) — the caller lands in `project` on success.
export async function createProject(name) {
  const data = await request('/api/projects/blank', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey(),
    },
    body: JSON.stringify({ name }),
  })
  return data.project
}
