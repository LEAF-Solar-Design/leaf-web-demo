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

import registry from './mock/registry.json'
import { runMock } from './mock/mockEngine.js'
import { authorMock } from './mock/mockAuthor.js'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8130'
// Default to mock unless explicitly disabled (VITE_MOCK=0).
const MOCK_DEFAULT = import.meta.env.VITE_MOCK !== '0'
// Tenant stub (X-Tenant-Id header) until real auth lands — matches the
// server default so the broker ledger / job list stay consistent.
const TENANT = import.meta.env.VITE_TENANT_ID || 'demo-tenant'

export const config = { apiBase: API_BASE, mockDefault: MOCK_DEFAULT, tenant: TENANT }

// A tiny artificial delay so mock runs show loading states like the real thing.
const nap = (ms) => new Promise((r) => setTimeout(r, ms))

async function http(path, opts) {
  const res = await fetch(`${API_BASE}${path}`, opts)
  if (!res.ok) throw new Error(`${opts?.method || 'GET'} ${path} -> ${res.status}`)
  return res.json()
}

// --- Session / intake ---------------------------------------------------
export async function getSession(mock, dwg = 'rooftop_demo') {
  if (mock) {
    const res = await fetch('/sample.intake.json')
    if (!res.ok) throw new Error('failed to load sample.intake.json')
    return await res.json()
  }
  const data = await http(`/api/session?dwg=${encodeURIComponent(dwg)}`)
  return data.intake
}

// --- Tools --------------------------------------------------------------
export async function getTools(mock) {
  if (mock) {
    await nap(150)
    // Clone so callers can safely append authored tools.
    return registry.tools.map((t) => ({ ...t }))
  }
  const data = await http('/api/tools')
  return data.tools
}

// --- Async job model (CONTRACT-ADDENDUM §7) -----------------------------
const TERMINAL = new Set(['complete', 'failed'])

// GET one durable job record. Body already carries `result` (§3 envelope on
// complete), `error` ({error_code,message,retryable} on failed), `status`,
// `progress`, `elapsed_ms`, `degraded_mode`.
export async function getJob(jobId) {
  return http(`/api/jobs/${encodeURIComponent(jobId)}`)
}

// GET recent jobs for a tenant (reconnect-after-tab-close list).
export async function listJobs(tenantId = TENANT, limit = 20) {
  const data = await http(`/api/jobs?tenant_id=${encodeURIComponent(tenantId)}&limit=${limit}`)
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

// --- Run (LIVE async) ---------------------------------------------------
// POST /api/run -> 202 {job_id} -> subscribe -> resolve with the §3 envelope.
// `opts.onSubmit(job_id)` fires the instant the job_id is known (for durable
// localStorage persistence); `opts.onStatus` streams progress.
export async function runToolAsync(tool, params, dwg = 'rooftop_demo', opts = {}) {
  const toolName = typeof tool === 'string' ? tool : tool.name
  const res = await fetch(`${API_BASE}/api/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT },
    body: JSON.stringify({ tool: toolName, params: params || {}, dwg }),
  })
  const body = await res.json().catch(() => null)

  if (res.status !== 202 || !body || !body.job_id) {
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

// --- Author -------------------------------------------------------------
export async function authorTool(mock, description) {
  if (mock) {
    await nap(700)
    return authorMock(description)
  }
  return http('/api/author', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  })
}
