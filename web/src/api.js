// API layer — the single seam between the frontend and the backend.
// In MOCK mode (default) it serves /api/tools, /api/run, /api/author from
// local fixtures + a client-side engine, so `npm run dev` demos with no
// backend. In LIVE mode it proxies to the Lane D server.
//
// LIVE /api/run is ASYNC (CONTRACT-ADDENDUM §7): POST returns HTTP 202
// {job_id, status:"submitted"}; the job runs in the background and is polled
// (GET /api/jobs/{id}) / streamed (SSE /api/jobs/{id}/stream) to a terminal
// state. `runToolAsync` hides all of that and resolves with the §3 envelope
// (job.result on complete; a §3-shaped error envelope on failed) — so callers
// see the same shape the old synchronous /api/run used to return.

import { createRunSubmissionRequest } from './runIntent.js'
import { runMock } from './mock/mockEngine.js'
import { authorMock } from './mock/mockAuthor.js'
import { matchPrompt } from './mock/mockNlPrompt.js'
import { humanizeError } from './errorHumanize.js'
import { groupToolsByFamily } from './mock/mockCapabilities.js'
import { listMockCatalogTools, registerMockCatalogTool } from './mock/mockCatalog.js'
import { fetchWithBudget } from './fetchBudget.js'
import * as mockVersions from './mock/mockVersions.js'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8130'
// Default to mock unless explicitly disabled (VITE_MOCK=0).
const MOCK_DEFAULT = import.meta.env.VITE_MOCK !== '0'
// Tenant stub (X-Tenant-Id header) until real auth lands — matches the
// server default so the broker ledger / job list stay consistent.
const TENANT = import.meta.env.VITE_TENANT_ID || 'demo-tenant'
// Auth seam (auth0-identity-signup): the platform JWT lives in localStorage
// under `leaf.jwt`. When present we attach `Authorization: Bearer <token>` to
// EVERY /api call so LEAF_AUTH_LIVE=1 works from the UI; when absent, headers are
// byte-identical to the open-demo path (zero behavior change off-auth).
const AUTH_KEY = 'leaf.jwt'
const GUEST_SESSION_KEY = 'leaf.guest_session'
const unauthorizedListeners = new Set()

export const config = { apiBase: API_BASE, mockDefault: MOCK_DEFAULT, tenant: TENANT }

// Read the bearer token at CALL time (not module load) so a sign-in mid-session
// is picked up. Returns {} when absent -> merging it is a no-op off-auth.
export function authHeaders() {
  try {
    const tok = localStorage.getItem(AUTH_KEY)
    return tok ? { Authorization: `Bearer ${tok}` } : {}
  } catch { return {} }
}

// Guest authority is intentionally narrower than account authority. Reuse it
// only for the uploaded-drawing endpoints that the server guest allowlist
// exposes. It must never flow into run, solve, author, converse, or job calls.
function storedGuestSession() {
  try { return localStorage.getItem(GUEST_SESSION_KEY) }
  catch { return null }
}

function guestDrawingHeaders(drawingId) {
  if (!String(drawingId || '').startsWith('u-')) return {}
  const guestSession = storedGuestSession()
  return guestSession ? { 'X-Guest-Session': guestSession } : {}
}

function rememberUploadAuthority(receipt) {
  try {
    if (receipt?.tenant_kind === 'guest' && receipt.guest_session) {
      localStorage.setItem(GUEST_SESSION_KEY, receipt.guest_session)
    } else if (receipt?.tenant_kind === 'account') {
      localStorage.removeItem(GUEST_SESSION_KEY)
    }
  } catch { /* storage unavailable */ }
}

export function subscribeUnauthorized(listener) {
  unauthorizedListeners.add(listener)
  return () => unauthorizedListeners.delete(listener)
}

export function noteUnauthorized(response, source = 'api') {
  if (response?.status !== 401) return response
  try { localStorage.removeItem(AUTH_KEY) } catch { /* storage unavailable */ }
  for (const listener of unauthorizedListeners) {
    try { listener(source) } catch { /* observers cannot change transport behavior */ }
  }
  return response
}

async function apiFetch(input, init, source = 'api') {
  return noteUnauthorized(await fetch(input, init), source)
}

// A tiny artificial delay so mock runs show loading states like the real thing.
const nap = (ms) => new Promise((r) => setTimeout(r, ms))

async function http(path, opts, timeoutMs = null) {
  const headers = { ...(opts?.headers || {}), ...authHeaders() }
  const request = timeoutMs == null
    ? fetch(`${API_BASE}${path}`, { ...opts, headers })
    : fetchWithBudget(fetch, `${API_BASE}${path}`, { ...opts, headers }, timeoutMs)
  const res = noteUnauthorized(await request, path)
  if (!res.ok) {
    const e = new Error(`${opts?.method || 'GET'} ${path} -> ${res.status}`)
    e.status = res.status // callers gate on 401/403 without string-matching
    throw e
  }
  return res.json()
}

// --- Session / intake ---------------------------------------------------
// Returns {intake, tenant}. `tenant` is the resolved tenant id echoed by
// /api/session when auth is live (deps.tenant_echo adds tenant_id/org_id);
// null in mock or when auth is off. The UI uses it for the honest tier chip
// (falls back to "demo" when absent).
export async function getSession(mock, dwg = 'rooftop_demo') {
  if (mock) {
    const res = await fetchWithBudget(fetch, '/sample.intake.json')
    if (!res.ok) throw new Error('failed to load sample.intake.json')
    return { intake: await res.json(), tenant: null, tier: null, org: null }
  }
  const data = await http(`/api/session?dwg=${encodeURIComponent(dwg)}`, undefined, 5000)
  // tier/org_id are echoed by deps.tenant_echo only when auth is live; null off-auth.
  return {
    intake: data.intake,
    tenant: data.tenant_id || null,
    tier: data.tier || null,
    org: data.org_id || null,
  }
}

// --- NL prompt routing (CONTRACT-ADDENDUM §12) --------------------------
// POST /api/nl-prompt {text} -> {lane, tool, params, confidence, rationale}.
// MOCK: pure client-side stub (matchPrompt) — zero network. LIVE: hits the
// server router; if the sibling lane has not deployed it yet (404/unreachable),
// falls back to the SAME client stub and flags `stub:true` so the UI can say it
// routed locally. Always resolves to a normalized route object with
// `alternatives` defaulted to [].
export async function nlPrompt(mock, text, tools = []) {
  if (mock) return { ...matchPrompt(text, tools), stub: true }
  try {
    const res = await apiFetch(`${API_BASE}/api/nl-prompt`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
      body: JSON.stringify({ text }),
    }, '/api/nl-prompt')
    if (!res.ok) {
      const error = new Error(`POST /api/nl-prompt -> ${res.status}`)
      error.status = res.status
      throw error
    }
    const body = await res.json()
    return { alternatives: [], ...body, stub: false }
  } catch (e) {
    if (e?.status === 401) throw e
    // Endpoint not live yet (sibling lands it concurrently) — route locally.
    return { ...matchPrompt(text, tools), stub: true, stubReason: humanizeError(e) }
  }
}

// --- Health (GET /api/health) -------------------------------------------
// {ok, aps_live, data_file_present, engine_registry_present, da_client_present,
//  n_tools, n_authored, ...}. Feeds the footer's real diagnostic chips (live
// only). Returns null on any error so the footer falls back to calm static text
// instead of surfacing a red failure.
export async function getHealth() {
  try {
    return await http('/api/health')
  } catch {
    return null
  }
}

// --- Tools --------------------------------------------------------------
export async function getTools(mock) {
  if (mock) {
    await nap(150)
    return listMockCatalogTools()
  }
  const data = await http('/api/tools', undefined, 5000)
  return data.tools
}

// --- Capability families (CONTRACT-ADDENDUM §9) -------------------------
// GET /api/capabilities -> { families: [{family_id, label, description,
//   capabilities:[{name, version, description, params_schema, capabilities,
//   provenance}]}] }. Returns a normalized shape the left-rail catalog renders
// grouped:  { families:[{..., capabilities:[toolShaped]}], source }.
//   source: 'endpoint'      — real /api/capabilities (server-side filtered)
//           'flat-fallback' — endpoint errored; grouped the flat /api/tools into
//                             one "Tools" family so the rail stays populated
//           'mock'          — grouped the local registry client-side (no /api)
// Capability entries carry `params_schema`; ToolsPanel consumes `params` + `kind`,
// so we normalize each into the tool shape it already renders.
const normalizeCap = (c) => ({
  ...c,
  params: c.params_schema || c.params || { type: 'object', properties: {} },
  kind: c.kind || 'script',
})

const normalizeFamilies = (families) =>
  (families || []).map((f) => ({
    family_id: f.family_id,
    label: f.label || f.family_id,
    description: f.description || '',
    capabilities: (f.capabilities || []).map(normalizeCap),
  }))

export async function getCapabilities(mock) {
  if (mock) {
    await nap(150)
    const tools = listMockCatalogTools()
    return { families: normalizeFamilies(groupToolsByFamily(tools)), source: 'mock' }
  }
  try {
    const data = await http('/api/capabilities', undefined, 5000)
    return { families: normalizeFamilies(data.families || []), source: 'endpoint' }
  } catch (error) {
    if (error?.status !== 404) throw error
    // Endpoint unavailable — fall back calmly to the flat catalog as one family.
    const tools = await getTools(false)
    return {
      families: [{
        family_id: 'all',
        label: 'Tools',
        description: 'The classic flat catalog (families endpoint unavailable).',
        capabilities: tools.map(normalizeCap),
      }],
      source: 'flat-fallback',
    }
  }
}

// --- Projects / Orgs workspace (platform/api.py; UI wave 2) --------------
// The canonical org-scoped Project/Job entity. Every project/job read is scoped
// by an `X-Org-Id` header; the org id is minted by POST /api/orgs and persisted
// in localStorage under `leaf.org_id`. LIVE only — mock mode never calls these
// (the header chip stays a static label with zero /api calls).
const ORG_KEY = 'leaf.org_id'

export function getStoredOrgId() {
  try { return localStorage.getItem(ORG_KEY) || null } catch { return null }
}
export function setStoredOrgId(id) {
  try { if (id) localStorage.setItem(ORG_KEY, id); else localStorage.removeItem(ORG_KEY) } catch { /* noop */ }
}
// X-Org-Id header for the stored org (or {} when none — a no-op off-workspace).
function orgHeaders(orgId) {
  const id = orgId || getStoredOrgId()
  return id ? { 'X-Org-Id': id } : {}
}

// POST /api/orgs {name, tier?} -> {org:{org_id,name,tier,status,created_at}}.
// Sibling contract (backend lane lands it concurrently); throws if not live yet
// so the caller can surface a calm "couldn't create workspace" note.
export async function createOrg(name, tier) {
  const body = tier ? { name, tier } : { name }
  const data = await http('/api/orgs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return data.org
}

// GET /api/orgs/{org_id} -> {org}. X-Org-Id scoped. Used to confirm a stored org
// still resolves; returns null on any error (stale/unknown id) so the UI can
// re-offer the create affordance instead of wedging.
export async function getOrg(orgId) {
  try {
    const data = await http(`/api/orgs/${encodeURIComponent(orgId)}`, { headers: { ...orgHeaders(orgId) } })
    return data.org || null
  } catch {
    return null
  }
}

// GET /api/projects -> {projects:[{project_id,name,status,created_at,...}]}. X-Org-Id scoped.
export async function listProjects(orgId) {
  const data = await http('/api/projects', { headers: { ...orgHeaders(orgId) } })
  return data.projects || []
}

// POST /api/projects {name} -> {project}. X-Org-Id scoped.
export async function createProject(name, orgId) {
  const data = await http('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...orgHeaders(orgId) },
    body: JSON.stringify({ name }),
  })
  return data.project
}

// GET /api/projects/{id} -> {project, drawing_versions[], jobs[], built_tools[]}.
// The workspace hydration payload the summary card renders. X-Org-Id scoped.
export async function openProject(projectId, orgId) {
  return http(`/api/projects/${encodeURIComponent(projectId)}`, { headers: { ...orgHeaders(orgId) } })
}

// --- Ops surface (INTERNAL; ?ops=1 only) ---------------------------------
// GET /api/ops/tenants -> {tenants:[{tenant_id,runs,usd_est,disabled}]}.
// POST /api/ops/tenants/{tid}/disable|enable -> enveloped ack. A 403 (not
// authorized) throws an Error tagged .status=403 so the drawer renders a calm
// "ops role required" instead of a red failure. Sibling contract.
//
// AUTH (1C-followup): the backend now gates ops on a real internal credential,
// `X-Ops-Secret`, NOT the legacy `X-Internal-Role: qa` dev seam. When the
// operator has provisioned the browser-local `leaf.ops_secret` value, we
// attach it. We still send the legacy
// role header so an ungated LOCAL dev backend keeps working (the demo path is
// byte-unchanged off-secret). The secret is read at call time and never logged.
const OPS_SECRET_KEY = 'leaf.ops_secret'
const OPS_HEADERS = { 'X-Internal-Role': 'qa' }

function opsSecret() {
  try {
    const ls = localStorage.getItem(OPS_SECRET_KEY)
    if (ls) return ls
  } catch { /* localStorage unavailable */ }
  return null
}

// Ops request headers: the legacy role seam + the real secret (when present) +
// any bearer. Never logs the secret.
function opsHeaders() {
  const s = opsSecret()
  return { ...OPS_HEADERS, ...(s ? { 'X-Ops-Secret': s } : {}), ...authHeaders() }
}

export async function getOpsTenants() {
  const res = await apiFetch(`${API_BASE}/api/ops/tenants`, { headers: opsHeaders() }, '/api/ops/tenants')
  if (res.status === 403) { const e = new Error('ops role required'); e.status = 403; throw e }
  if (!res.ok) throw new Error(`GET /api/ops/tenants -> ${res.status}`)
  const body = await res.json()
  return body.tenants || []
}

export async function setTenantDisabled(tenantId, disabled) {
  const action = disabled ? 'disable' : 'enable'
  const res = await apiFetch(
    `${API_BASE}/api/ops/tenants/${encodeURIComponent(tenantId)}/${action}`,
    { method: 'POST', headers: opsHeaders() },
    `/api/ops/tenants/${tenantId}/${action}`,
  )
  if (res.status === 403) { const e = new Error('ops role required'); e.status = 403; throw e }
  if (!res.ok) throw new Error(`POST /api/ops/tenants/${tenantId}/${action} -> ${res.status}`)
  return res.json()
}

// --- Per-tenant spend / quota meter (thin ledger-backed read) -----------
// GET /api/usage -> {tenant_id, today:{runs, usd_est}, total:{runs, usd_est},
//   cap:{usd_cap, remaining, enabled}, updated_at} (§10-enveloped). LIVE only —
// mock hides the spend chip (no fabricated numbers). Returns null when the
// sibling endpoint isn't deployed yet (404/unreachable) so the chip stays hidden.
export async function getUsage() {
  try {
    return await http('/api/usage', { headers: { 'X-Tenant-Id': TENANT } })
  } catch {
    return null
  }
}

// --- Entitlements (REAL plan gates) --------------------------------------
// GET /api/entitlements -> {tier, entitlements:{run_read, run_write, build},
// source:"policy"} (§10-enveloped: those fields sit at top level alongside
// error/degraded_mode). This drives the UI's REAL gates — write-tool Run and the
// Build lane's Generate — and the entitlement panel. It is ENFORCED server-side
// (POST /api/run write tools + POST /api/author return 403 entitlement_required
// on a disallowed tier), so the UI states the real plan, never a fake claim.
// LIVE only. Returns null in the caller's mock path, and null here when the
// sibling endpoint isn't deployed yet (404/unreachable) — the UI treats null as
// the honest off-auth demo state (full access, all capabilities permitted).
export async function getEntitlements() {
  try {
    return await http('/api/entitlements', { headers: { 'X-Tenant-Id': TENANT } })
  } catch {
    return null
  }
}

// --- Claude account grant (CONCERN 2 — the user's Claude login) ----------
// This is the SIBLING of the platform JWT (Concern 1), and NEVER the same thing
// (AUTH.md §0). It answers "whose Anthropic credit runs the authoring agent?" —
// a per-user Claude OAuth token minted by `claude setup-token`, stored per tenant
// server-side. The token is WRITE-ONLY from the UI: we POST it once and never
// read it back (GET never returns it), never log it, never echo it.
//   GET    /api/tenant/claude-grant -> {linked: bool, linked_at: str|null}
//   POST   /api/tenant/claude-grant {token} -> {linked: true, linked_at}
//   DELETE /api/tenant/claude-grant -> {linked: false}
// All tenant-scoped (X-Tenant-Id / auth). LIVE only — mock hides the panel.
// getClaudeGrant returns null when the sibling endpoint isn't deployed yet
// (404/unreachable) so the affordance degrades to today's ungated behavior
// (no proactive authoring gate) instead of surfacing a red failure.
export async function getClaudeGrant() {
  try {
    return await http('/api/tenant/claude-grant', { headers: { 'X-Tenant-Id': TENANT } })
  } catch {
    return null
  }
}

// POST the token ONCE. Callers must clear their field immediately after; this
// layer keeps no reference to it beyond the request body and logs nothing.
// `kind` ("oauth" = a Claude subscription token from `claude setup-token`;
// "api_key" = an Anthropic org API key billed at API rates) is sent explicitly
// from the user's choice. It is OPTIONAL in the contract (the server auto-detects
// from the token shape when omitted); we send it so the linked kind is honest
// even if detection is ambiguous. The token itself is never stored/echoed/logged.
export async function linkClaudeGrant(token, kind) {
  const body = kind ? { token, kind } : { token }
  return http('/api/tenant/claude-grant', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT },
    body: JSON.stringify(body),
  })
}

export async function unlinkClaudeGrant() {
  return http('/api/tenant/claude-grant', {
    method: 'DELETE',
    headers: { 'X-Tenant-Id': TENANT },
  })
}

// Defensive detector for the authoring "grant required" signal (Concern 2). The
// sibling backend documents the exact field; until it lands we key on EITHER an
// explicit `grant_required` flag OR the §10 error code/message mentioning a
// Claude grant / sign-in. Turns an authoring rejection into the calm "link your
// Claude account" gate instead of a red failure. Accepts a §10 error object, a
// full envelope, or an Error carrying a parsed `.body`.
export function isGrantRequired(input) {
  if (!input) return false
  const body = input.body || input
  if (body.grant_required === true) return true
  const e = body.error || body
  if (e && e.grant_required === true) return true
  const code = String((e && e.error_code) || '').toLowerCase()
  const msg = String((e && e.message) || input.message || '').toLowerCase()
  const hay = `${code} ${msg}`
  return /grant[_ -]?required|link (your )?claude|sign ?in with claude|claude account|claude subscription|claude (grant|token|oauth|login)/.test(hay)
}

// --- Async job model (CONTRACT-ADDENDUM §7) -----------------------------
const TERMINAL = new Set(['complete', 'failed'])

// GET one durable job record. Body already carries `result` (§3 envelope on
// complete), `error` ({error_code,message,retryable} on failed), `status`,
// `progress`, `elapsed_ms`, `degraded_mode`.
export async function getJob(jobId) {
  // Job reads are tenant-scoped server-side (security-audit F8) — send our tenant so
  // a non-default VITE_TENANT_ID still resolves to its own jobs. (SSE + sendBeacon
  // cannot set headers and rely on the demo-tenant default; documented follow-up.)
  return http(`/api/jobs/${encodeURIComponent(jobId)}`, { headers: { 'X-Tenant-Id': TENANT } })
}

// GET recent jobs for a tenant (reconnect-after-tab-close list).
export async function listJobs(tenantId = TENANT, limit = 20) {
  // tenant scope is enforced from the header server-side; the query param is legacy/ignored
  const data = await http(`/api/jobs?limit=${limit}`, { headers: { 'X-Tenant-Id': tenantId } })
  return data.jobs || []
}

// Turn a terminal job record into a §3 result envelope (the shape the UI
// renders). Complete -> the stored envelope verbatim; failed (or a
// complete-without-result edge) -> a §3-shaped error envelope.
export function recordToEnvelope(rec) {
  if (rec && rec.status === 'complete' && rec.result) return rec.result
  const err = (rec && rec.error) || { error_code: 'INTERNAL', message: 'job failed', retryable: false }
  return {
    ok: false,
    tool: (rec && rec.tool) || null,
    version: null,
    result: null,
    overlay: null,
    timing_ms: (rec && rec.elapsed_ms) || 0,
    cost: null,
    error: err,
    degraded_mode: !!(rec && rec.degraded_mode),
  }
}

// Subscribe to a job until it reaches a terminal state, then resolve with its
// §3 envelope. Belt-and-suspenders: an EventSource stream for instant status
// transitions PLUS a 1s poll fallback (works even if SSE is blocked). The
// authoritative resolution always comes from a GET record (the SSE payload
// carries no `result`), so a terminal SSE event just triggers one more poll.
// `onStatus({status, progress, elapsed_ms, error, job_id})` fires on updates.
function subscribeJob(jobId, onStatus) {
  return new Promise((resolve, reject) => {
    let settled = false
    let es = null
    let poll = null
    let errStreak = 0

    const cleanup = () => {
      if (es) { try { es.close() } catch { /* noop */ } es = null }
      if (poll) { clearInterval(poll); poll = null }
    }
    const finish = (rec) => {
      if (settled) return
      settled = true
      cleanup()
      resolve(recordToEnvelope(rec))
    }
    const fail = (e) => {
      if (settled) return
      settled = true
      cleanup()
      reject(e instanceof Error ? e : new Error(String(e)))
    }
    const emit = (o) => {
      if (settled || !onStatus) return
      onStatus({ status: o.status, progress: o.progress, elapsed_ms: o.elapsed_ms, error: o.error, job_id: jobId })
    }
    const handleRec = (rec) => {
      if (settled || !rec) return
      emit(rec)
      if (TERMINAL.has(rec.status)) finish(rec)
    }
    const pollOnce = async () => {
      try {
        const rec = await getJob(jobId)
        errStreak = 0
        handleRec(rec)
      } catch (e) {
        errStreak += 1
        if (errStreak >= 12) fail(new Error(`lost job ${jobId}: ${e.message || e}`))
      }
    }

    // Poll fallback (also the immediate first read).
    poll = setInterval(pollOnce, 1000)
    pollOnce()

    // SSE for low-latency transitions; poll covers it if this is unavailable.
    if (typeof EventSource !== 'undefined') {
      try {
        es = new EventSource(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/stream`)
        es.onmessage = (ev) => {
          let p
          try { p = JSON.parse(ev.data) } catch { return }
          if (!p || p.status === 'unknown') return
          emit(p)
          if (TERMINAL.has(p.status)) pollOnce() // fetch the authoritative record (incl. result)
        }
        es.onerror = () => { /* rely on the poll fallback */ }
      } catch { /* EventSource construction failed; poll covers it */ }
    }
  })
}

// Re-attach to an EXISTING job (tab-close survivability) without re-submitting.
export function attachToJob(jobId, opts = {}) {
  return subscribeJob(jobId, opts.onStatus)
}

// --- Tab-close reap beacon (CONTRACT-ADDENDUM §7 / routers/jobs.py:121) --
// Fire-and-forget POST /api/jobs/{id}/close via navigator.sendBeacon on tab-close
// so an abandoned in-flight WorkItem is flagged closable server-side (the orphan
// reaper fails it / cancels the WorkItem broker-side) instead of billing until the
// heartbeat-stale window. Bodyless; the tenant resolves to the demo default when
// no header is sent (off-auth), matching the run's tenant. Absolute URL so it
// respects VITE_API_BASE. Returns the sendBeacon boolean (queued or not).
export function closeJobBeacon(jobId) {
  if (!jobId || typeof navigator === 'undefined' || !navigator.sendBeacon) return false
  try {
    return navigator.sendBeacon(`${API_BASE}/api/jobs/${encodeURIComponent(jobId)}/close`)
  } catch {
    return false
  }
}

// --- Run (LIVE async) ---------------------------------------------------
// POST /api/run -> 202 {job_id} -> subscribe -> resolve with the §3 envelope.
// `opts.onSubmit(job_id)` fires the instant the job_id is known (for durable
// localStorage persistence); `opts.onStatus` streams progress.
export async function runToolAsync(tool, params, dwg = 'rooftop_demo', opts = {}) {
  const toolName = typeof tool === 'string' ? tool : tool.name
  // When a project is open, X-Org-Id + X-Project-Id make the backend record a
  // canonical platform Job row (spine_ref-linked) so the workspace jobs[] grows.
  // Absent -> byte-identical to the plain tenant-only run (no linkage).
  const submission = createRunSubmissionRequest(toolName, params, dwg, opts)
  const res = await apiFetch(`${API_BASE}/api/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...submission.headers, ...authHeaders() },
    body: JSON.stringify(submission.body),
  }, '/api/run')
  const body = await res.json().catch(() => null)

  if (res.status !== 202 || !body || !body.job_id) {
    // Entitlement rejection (HTTP 403 {entitlement_required, required, tier}) —
    // a write tool the tenant's plan doesn't include. Tag the envelope so the UI
    // renders it CALM (amber), like a spend cap, not a red failure. Checked FIRST
    // so the tag survives even when the body also carries `ok`.
    if (res.status === 403 && body && body.entitlement_required) {
      return {
        ok: false, tool: toolName, version: null, result: null, overlay: null,
        timing_ms: 0, cost: null, degraded_mode: false,
        error: body.error || { error_code: 'ENTITLEMENT_REQUIRED', message: 'This action is not included in your plan.', retryable: false },
        entitlement_required: true,
        required: body.required || null,
        tier: body.tier || null,
      }
    }
    // Coarse per-tenant DAILY run-quota over-cap (HTTP 429 {error, tier, limit,
    // used, retryable}). Distinct from the 402 spend-cap even though both carry
    // error_code:"quota_exceeded" — tag it `quota_kind:"daily_runs"` so the UI
    // renders the calm "daily run limit reached" card (amber), never the spend
    // card and never a red failure. Nothing ran; the job was rejected pre-APS.
    if (res.status === 429) {
      const qErr = (body && body.error) ||
        { error_code: 'quota_exceeded', message: 'Daily run limit reached.', retryable: true }
      return {
        ok: false, tool: toolName, version: null, result: null, overlay: null,
        timing_ms: 0, cost: null, degraded_mode: false,
        error: qErr,
        quota_kind: 'daily_runs',
        tier: (body && body.tier) || null,
        limit: (body && Number.isFinite(Number(body.limit))) ? Number(body.limit) : null,
        used: (body && Number.isFinite(Number(body.used))) ? Number(body.used) : null,
      }
    }
    // Submission itself failed. If the server already returned a §3 envelope
    // (e.g. UNKNOWN_TOOL 404), pass it straight through; else synthesize one.
    if (body && 'ok' in body) return body
    if (body && body.error) {
      return { ok: false, tool: toolName, version: null, result: null, overlay: null,
        timing_ms: 0, cost: null, error: body.error, degraded_mode: false }
    }
    throw new Error(`POST /api/run -> ${res.status}`)
  }

  const jobId = body.job_id
  if (opts.onSubmit) opts.onSubmit(jobId)
  if (opts.onStatus) opts.onStatus({ status: 'submitted', progress: 'queued', elapsed_ms: null, job_id: jobId })
  return subscribeJob(jobId, opts.onStatus)
}

// --- Run (dispatch) -----------------------------------------------------
// `intake` is only used in mock mode (client-side engine). Mock stays fully
// client-side and never touches the jobs API; live delegates to the async path.
export async function runTool(mock, tool, params, intake, dwg = 'rooftop_demo') {
  if (mock) {
    await nap(400 + Math.random() * 500)
    return runMock(tool, params, intake)
  }
  return runToolAsync(tool, params, dwg)
}

// --- Versioned drawing write loop (CONTRACT-ADDENDUM §11) ----------------
// A `drawing.write` run stamps `result.new_version` into its §3 envelope; these
// endpoints serve the versions it creates. Each returns the enveloped body
// verbatim: intake reads carry {intake, version, head, latest}; undo/redo carry
// {version, head, latest, intake}. The X-Tenant-Id header MUST match the one
// /api/run uses (same TENANT) so the per-tenant store resolves the same drawing.
// In MOCK the same four calls are served by the pure in-memory chain in
// mock/mockVersions.js (same shapes, zero network); the LIVE branch below is
// untouched.
export async function getDrawingIntake(mock, drawingId, version = 'head') {
  if (mock) { await nap(120); return mockVersions.view(version) }
  return http(
    `/api/drawings/${encodeURIComponent(drawingId)}/intake?version=${encodeURIComponent(version)}`,
    { headers: { 'X-Tenant-Id': TENANT, ...guestDrawingHeaders(drawingId) } },
  )
}

// --- Drawing upload and extraction (CONTRACT-ADDENDUM section 19) -------
export async function getGuestUploadPolicy() {
  return http('/api/site/guest-upload-policy', undefined, 5000)
}

export async function uploadDrawing(file, guestSession = null) {
  const form = new FormData()
  form.append('file', file)
  const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
  const uploadGuestSession = guestSession || storedGuestSession()
  if (uploadGuestSession) headers['X-Guest-Session'] = uploadGuestSession
  const res = await apiFetch(`${API_BASE}/api/drawings/upload`, { method: 'POST', headers, body: form }, '/api/drawings/upload')
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const error = new Error(body?.error?.message || `POST /api/drawings/upload -> ${res.status}`)
    error.status = res.status
    error.body = body
    throw error
  }
  rememberUploadAuthority(body)
  return body
}

export async function getDrawingUploadStatus(drawingId, guestSession = null, tenantId = null) {
  const headers = { 'X-Tenant-Id': tenantId || TENANT, ...authHeaders() }
  if (guestSession) headers['X-Guest-Session'] = guestSession
  return http(`/api/drawings/${encodeURIComponent(drawingId)}/upload-status`, { headers }, 5000)
}

export async function getUploadedDrawingIntake(drawingId, guestSession = null, tenantId = null) {
  const headers = { 'X-Tenant-Id': tenantId || TENANT, ...authHeaders() }
  if (guestSession) headers['X-Guest-Session'] = guestSession
  return http(`/api/drawings/${encodeURIComponent(drawingId)}/intake?version=head`, { headers }, 8000)
}

// Version-history chain (sibling contract): GET /api/drawings/{id}/versions ->
// {drawing_id, head, latest, versions:[{v, parent, created, bytes, sha256, tool,
// workitem_id, note}]}. LIVE only. Throws if the sibling endpoint isn't live yet
// so the caller can render a calm "history unavailable" note.
export async function getDrawingVersions(mock, drawingId) {
  if (mock) { await nap(120); return mockVersions.list() }
  return http(`/api/drawings/${encodeURIComponent(drawingId)}/versions`, {
    headers: { 'X-Tenant-Id': TENANT, ...guestDrawingHeaders(drawingId) },
  })
}

// --- Single-writer checkout take / release (3B) -------------------------
// Wave-2 landed the checkout as a DISPLAY-ONLY chip (read from /versions). These
// two calls make it MUTABLE: acquire the single-writer lock, or release it.
//
// POST /api/drawings/{id}/checkout {holder?, ttl_s?}
//   200 {acquired:true, holder, checkout:{holder,acquired,expires}}
//   409 {acquired:false, locked_by, ...}   (someone else holds it — EXPECTED)
// The 409 is a normal coordination outcome, not an error: we fold it into the
// resolved value (`acquired:false`) instead of throwing, so the caller just
// refetches /versions to show the real lock. LIVE only.
export async function takeCheckout(drawingId, holder, ttlS) {
  const payload = {}
  if (holder) payload.holder = holder
  if (ttlS) payload.ttl_s = ttlS
  const res = await apiFetch(`${API_BASE}/api/drawings/${encodeURIComponent(drawingId)}/checkout`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify(payload),
  }, `/api/drawings/${drawingId}/checkout`)
  const data = await res.json().catch(() => null)
  if (res.status === 409) {
    return {
      acquired: false,
      locked_by: (data && (data.locked_by || (data.checkout && data.checkout.holder))) || null,
      checkout: (data && data.checkout) || null,
    }
  }
  if (!res.ok) throw new Error(`POST /api/drawings/${drawingId}/checkout -> ${res.status}`)
  return { acquired: true, ...(data || {}) }
}

// DELETE /api/drawings/{id}/checkout?holder=
//   200 {released, checkout:null}
//   403 (not the holder) — tagged .status=403 so the caller can say "you don't
//   hold this lock" calmly. LIVE only.
export async function releaseCheckout(drawingId, holder) {
  const q = holder ? `?holder=${encodeURIComponent(holder)}` : ''
  const res = await apiFetch(`${API_BASE}/api/drawings/${encodeURIComponent(drawingId)}/checkout${q}`, {
    method: 'DELETE',
    headers: { 'X-Tenant-Id': TENANT, ...authHeaders() },
  }, `/api/drawings/${drawingId}/checkout`)
  if (res.status === 403) { const e = new Error('not the lock holder'); e.status = 403; throw e }
  const data = await res.json().catch(() => null)
  if (!res.ok) throw new Error(`DELETE /api/drawings/${drawingId}/checkout -> ${res.status}`)
  return data || { released: true, checkout: null }
}

export async function undoDrawing(mock, drawingId) {
  if (mock) { await nap(180); return mockVersions.undo() }
  return http(`/api/drawings/${encodeURIComponent(drawingId)}/undo`, {
    method: 'POST',
    headers: { 'X-Tenant-Id': TENANT },
  })
}

export async function redoDrawing(mock, drawingId) {
  if (mock) { await nap(180); return mockVersions.redo() }
  return http(`/api/drawings/${encodeURIComponent(drawingId)}/redo`, {
    method: 'POST',
    headers: { 'X-Tenant-Id': TENANT },
  })
}

// --- Author -------------------------------------------------------------
// LIVE authoring is tenant-scoped (X-Tenant-Id) — the same tenant whose Claude
// grant (Concern 2) funds the agent. On a non-2xx we parse the §10 body and
// throw an Error carrying it (`.body`, `.status`, `.grantRequired`) so the
// AuthorPanel can render the calm "link your Claude account" gate for a
// grant-required rejection instead of a red failure. The templated path (no
// agent) still returns a normal 2xx envelope, so template authoring is
// unaffected by whether a Claude account is linked.
export async function authorTool(mock, description) {
  if (mock) {
    await nap(700)
    return authorMock(description)
  }
  const res = await apiFetch(`${API_BASE}/api/author`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify({ description }),
  }, '/api/author')
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    // The message can reach the DOM (AuthorPanel renders it), so it is
    // humanized here — no verb/path/status ever leaks onto the stage. The
    // machine-readable signal lives on .status/.body, which callers use.
    const err = new Error(humanizeError(
      (body && body.error && (body.error.message || body.error.error_code)) ||
      `POST /api/author -> ${res.status}`,
    ))
    err.status = res.status
    err.body = body
    err.grantRequired = isGrantRequired(body)
    // Build-lane entitlement rejection (HTTP 403 {entitlement_required}) — the
    // tenant's plan doesn't include tool authoring. Distinct from grantRequired
    // (which is about linking a Claude account). The AuthorPanel renders this as
    // a calm "upgrade to build" gate, not a red failure.
    err.entitlementRequired = res.status === 403 && !!(body && body.entitlement_required)
    if (err.entitlementRequired) { err.required = body.required || 'build'; err.tier = body.tier || null }
    throw err
  }
  return body
}

function customizationError(method, path, status, body) {
  const err = new Error(humanizeError(
    (body && body.error && (body.error.message || body.error.code)) || `${method} ${path} -> ${status}`,
  ))
  err.status = status
  err.body = body
  err.grantRequired = isGrantRequired(body)
  err.entitlementRequired = status === 403 && !!(body && body.entitlement_required)
  return err
}

function requestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `leaf-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

// R5: stage only. The server resolves base and policy fields, while this client
// carries the public contract request and never treats staging as publication.
export async function stageAuthorTool(mock, description) {
  if (mock) {
    await nap(700)
    const authored = authorMock(description)
    if (authored.unsupported) {
      const error = new Error(authored.message)
      error.unsupported = true
      throw error
    }
    return {
      ...authored,
      demo: true,
      receipt: { change_set_id: `demo-${requestId()}`, state: 'staged' },
      diff_summary: 'Demo preview only. No live tenant catalog changed.',
      validation: { status: 'demo' },
    }
  }
  const path = '/api/author/stage'
  const res = await apiFetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify({ description, mode: 'build', idempotency_key: requestId() }),
  }, path)
  const body = await res.json().catch(() => null)
  if (!res.ok) throw customizationError('POST', path, res.status, body)
  return body
}

// Keep confirmation issuance isolated: a server lane may change this additive
// endpoint without changing the frozen R6 register request below.
async function issuePublishConfirmation(receipt) {
  const path = '/api/author/confirmations'
  const res = await apiFetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify(receipt),
  }, path)
  const body = await res.json().catch(() => null)
  if (!res.ok) throw customizationError('POST', path, res.status, body)
  if (!body || typeof body.confirmation_id !== 'string' || !body.confirmation_id) {
    throw new Error('The publish confirmation was unavailable. The staged tool was not published.')
  }
  return body.confirmation_id
}

// R6: obtain one server confirmation, then post exactly the receipt-bound fields.
export async function publishStagedAuthor(mock, staged) {
  if (!staged || !staged.receipt) throw new Error('A staged tool is required before publishing.')
  if (mock) {
    await nap(250)
    registerMockCatalogTool(staged.tool)
    return { ...staged, published: true, demo: true, demo_session_only: true }
  }
  const receipt = staged.receipt
  const confirmation_id = await issuePublishConfirmation(receipt)
  const path = '/api/author/register'
  const publish = {
    change_set_id: receipt.change_set_id,
    staged_commit: receipt.staged_commit,
    catalog_digest: receipt.catalog_digest,
    platform_release: receipt.platform_release,
    workspace_contract_digest: receipt.workspace_contract_digest,
    confirmation_id,
    idempotency_key: requestId(),
  }
  const res = await apiFetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify(publish),
  }, path)
  const body = await res.json().catch(() => null)
  if (!res.ok) throw customizationError('POST', path, res.status, body)
  return { ...staged, ...body, published: true }
}
