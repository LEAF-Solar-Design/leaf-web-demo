/**
 * T1 overlay client: read the tenant overlay on load, decide a pending one.
 *
 * Mirrors converse.js conventions exactly, on purpose: same API_BASE, the same
 * X-Tenant-Id and authHeaders on every call, and errors tagged with
 * .status/.body so callers gate on those rather than matching message text.
 *
 * WHY A READ ON LOAD AND NOT ONLY A PUSH. The overlay must be correct for a
 * browser that just opened, missed every event, or reconnected after the
 * decision it cared about. The read is the floor; the stream is the latency
 * optimisation on top. Building it the other way round is exactly how
 * `question_required` shipped as an intermittent bug: correct only when a
 * race went the right way.
 */

import { config, authHeaders, noteUnauthorized } from './api.js'

const API_BASE = config.apiBase
const TENANT = config.tenant

function tagged(res, body, fallback) {
  const e = new Error(
    (body && body.error && (body.error.message || body.error.error_code)) || fallback,
  )
  e.status = res.status
  e.body = body
  e.errorCode = (body && body.error && body.error.error_code) || null
  return e
}

/**
 * The tokens this session should be rendering right now.
 *
 * Resolves to a SAFE EMPTY overlay on any failure rather than throwing. A
 * theme read is not worth breaking the app over: the committed CSS defaults
 * are already on screen, and returning empty leaves them exactly as they are.
 * Throwing here would turn a cosmetic outage into a blank page.
 */
export async function fetchOverlay(sessionId) {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
  try {
    const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
    const res = await fetch(`${API_BASE}/api/overlay${qs}`, {
      method: 'GET',
      headers,
    })
    const body = await res.json().catch(() => null)
    noteUnauthorized(res, '/api/overlay', headers.Authorization)
    if (!res.ok || !body) return { tokens: {}, documentVersion: 0, pendingProposalId: null }
    return {
      tokens: body.tokens || {},
      documentVersion: Number(body.document_version || 0),
      pendingProposalId: body.pending_proposal_id || null,
    }
  } catch {
    return { tokens: {}, documentVersion: 0, pendingProposalId: null }
  }
}

/**
 * The operator's tap. Unlike the read, this THROWS on failure.
 *
 * A decision that silently failed would tell the operator their approval
 * landed when the tenant never changed, which is the one outcome the card is
 * built to prevent. `documentVersion` is the CAS witness the card was rendered
 * against and is required, not defaulted: a caller that cannot say which
 * version it saw has no business deciding.
 */
export async function decideOverlay(proposalId, { approve, decisionKey, documentVersion, actor }) {
  if (!Number.isInteger(documentVersion)) {
    throw new Error('documentVersion is required to decide')
  }
  const headers = {
    'Content-Type': 'application/json',
    'X-Tenant-Id': TENANT,
    ...(actor ? { 'X-Actor': actor } : {}),
    ...authHeaders(),
  }
  const res = await fetch(`${API_BASE}/api/overlay/decisions`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      proposal_id: proposalId,
      approve: !!approve,
      decision_key: decisionKey,
      document_version: documentVersion,
    }),
  })
  const body = await res.json().catch(() => null)
  noteUnauthorized(res, '/api/overlay/decisions', headers.Authorization)
  if (!res.ok || !body || body.error) {
    throw tagged(res, body, `POST /api/overlay/decisions -> ${res.status}`)
  }
  return { state: body.state, documentVersion: Number(body.document_version || 0) }
}
