/**
 * operatorClient.js — the transport seam every operator call rides.
 *
 * The security test the plan requires by name lives here: a forged
 * X-Operator-Subject from the UI must be structurally impossible, so this
 * file is grepped for the header name (source-level proof) AND every fetch
 * call this module makes is asserted to carry no such header (behavioral
 * proof) — belt and braces, matching the pattern review already approved in
 * this codebase (OverlayDecisionCard's double-click test).
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as operatorClient from './operatorClient.js'

// Resolved from the web/ package root (vitest cwd), not import.meta.url:
// vitest serves modules over a non-file scheme, where fileURLToPath throws.
const SOURCE = readFileSync(join(process.cwd(), 'src', 'operatorClient.js'), 'utf8')

beforeEach(() => {
  operatorClient.resetOperatorProbeForTests()
  vi.stubGlobal('fetch', vi.fn())
  try { localStorage.clear() } catch { /* jsdom always has it, guard anyway */ }
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function jsonResponse(status, body) {
  return { status, ok: status >= 200 && status < 300, json: async () => body }
}

describe('no operator identity header is ever forgeable from the UI', () => {
  it('the source never references X-Operator-Subject or X-Operator-Profile', () => {
    // Source-level proof: the string literally cannot appear, so no future
    // edit can slip a client-set identity header back in without failing here.
    expect(SOURCE).not.toMatch(/X-Operator-Subject/i)
    expect(SOURCE).not.toMatch(/X-Operator-Profile/i)
  })

  it('every outgoing header set omits an operator identity header, behaviorally', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { sessions: [] }))
    await operatorClient.listSessions()
    const [, init] = fetch.mock.calls[0]
    const headerNames = Object.keys(init?.headers || {}).map((k) => k.toLowerCase())
    expect(headerNames).not.toContain('x-operator-subject')
    expect(headerNames).not.toContain('x-operator-profile')
  })
})

describe('probe (acceptance #6: exactly one request)', () => {
  it('hides on 404 (router unmounted)', async () => {
    fetch.mockResolvedValue(jsonResponse(404, { detail: 'operator_not_found' }))
    await expect(operatorClient.probeOperatorConsole()).resolves.toBe(false)
  })
  it('hides on 401', async () => {
    fetch.mockResolvedValue(jsonResponse(401, {}))
    await expect(operatorClient.probeOperatorConsole()).resolves.toBe(false)
  })
  it('hides on 503 (store unavailable)', async () => {
    fetch.mockResolvedValue(jsonResponse(503, { detail: 'operator_store_unavailable' }))
    await expect(operatorClient.probeOperatorConsole()).resolves.toBe(false)
  })
  it('hides on a network failure rather than throwing', async () => {
    fetch.mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(operatorClient.probeOperatorConsole()).resolves.toBe(false)
  })
  it('shows on 200', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { sessions: [] }))
    await expect(operatorClient.probeOperatorConsole()).resolves.toBe(true)
  })
  it('caches: repeated calls issue exactly one fetch', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { sessions: [] }))
    await operatorClient.probeOperatorConsole()
    await operatorClient.probeOperatorConsole()
    await operatorClient.probeOperatorConsole()
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('signed-out reset (acceptance #4)', () => {
  it.each([401, 403, 404])('fires signed-out listeners on %i from any operator call', async (status) => {
    fetch.mockResolvedValue(jsonResponse(status, { detail: 'denied' }))
    const listener = vi.fn()
    const unsubscribe = operatorClient.subscribeOperatorSignedOut(listener)
    await expect(operatorClient.listSessions()).rejects.toThrow()
    expect(listener).toHaveBeenCalledTimes(1)
    unsubscribe()
  })

  it('does not fire on a clean success', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { sessions: [] }))
    const listener = vi.fn()
    const unsubscribe = operatorClient.subscribeOperatorSignedOut(listener)
    await operatorClient.listSessions()
    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('does not fire on an unrelated server error (500)', async () => {
    fetch.mockResolvedValue(jsonResponse(500, { detail: 'boom' }))
    const listener = vi.fn()
    const unsubscribe = operatorClient.subscribeOperatorSignedOut(listener)
    await expect(operatorClient.listSessions()).rejects.toThrow()
    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('isOperatorDenied classifies exactly 401/403/404', () => {
    expect(operatorClient.isOperatorDenied({ status: 401 })).toBe(true)
    expect(operatorClient.isOperatorDenied({ status: 403 })).toBe(true)
    expect(operatorClient.isOperatorDenied({ status: 404 })).toBe(true)
    expect(operatorClient.isOperatorDenied({ status: 500 })).toBe(false)
    expect(operatorClient.isOperatorDenied({ status: 503 })).toBe(false)
    expect(operatorClient.isOperatorDenied(null)).toBe(false)
  })
})

describe('route shapes (unowned server contract, asserted so a rename is caught)', () => {
  it('postMessage posts {text} to the session messages route', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { turn_id: 't-1', status: 'complete' }))
    await operatorClient.postMessage('opsess-1', 'hello')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toContain('/api/operator/sessions/opsess-1/messages')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ text: 'hello' })
  })

  it('tenantAgentPropose posts {tenant_id} to the verb-scoped propose route', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { authority_id: 'opauth-1' }))
    await operatorClient.tenantAgentPropose('pause', 'acme-solar')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toContain('/api/operator/runbooks/tenant-agent/pause/propose')
    expect(JSON.parse(init.body)).toEqual({ tenant_id: 'acme-solar' })
  })

  it('dispatchWorker never widens workspace/network from the client (server owns that)', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { jobId: 'op-1' }))
    await operatorClient.dispatchWorker(['echo hi'])
    const [, init] = fetch.mock.calls[0]
    const body = JSON.parse(init.body)
    expect(body).not.toHaveProperty('workspace')
    expect(body).not.toHaveProperty('network')
    expect(body.commands).toEqual(['echo hi'])
  })

  it('cancelWorker sends only the exact worker/run tuple', async () => {
    fetch.mockResolvedValue(jsonResponse(200, { worker_id: 'worker-1', run_id: 'run-1', status: 'cancelled' }))
    await operatorClient.cancelWorker('worker-1', 'run-1')
    const [url, init] = fetch.mock.calls[0]
    expect(url).toContain('/api/operator/worker/cancel')
    expect(JSON.parse(init.body)).toEqual({ worker_id: 'worker-1', run_id: 'run-1' })
  })
})

describe('failure surfaces a status-tagged error, never a silent open', () => {
  it('rejects with .status and .body on a non-2xx', async () => {
    fetch.mockResolvedValue(jsonResponse(409, { detail: 'precondition_state_conflict' }))
    await expect(operatorClient.tenantAgentExecute('pause', 'acme-solar', 'opauth-1'))
      .rejects.toMatchObject({ status: 409, body: { detail: 'precondition_state_conflict' } })
  })
})
