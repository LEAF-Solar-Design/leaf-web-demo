import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { isWorkspaceBootstrapRequired } from '../api.js'
import { createSessionController } from '../controllers/session/createSessionController.js'
import { humanizeError } from '../errorHumanize.js'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))

const source = readFileSync(`${process.cwd()}/src/site/ToolCast.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const effects = []
function walk(node) {
  if (!node || typeof node.type !== 'string') return
  if (node.type === 'CallExpression' && node.callee.name === 'useEffect'
    && source.slice(node.start, node.end).includes('let probeLive = true')) effects.push(node.arguments[0])
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value.type === 'string') walk(value)
  }
}
walk(tree)
if (effects.length !== 1) throw new Error('Expected the single ToolCast account/drawing session effect')
const effectSource = source.slice(effects[0].start, effects[0].end)

// Execute the actual production effect with its real session controller.
// Transport and unrelated drawing setters are supplied here; no effect body
// is copied, and no renderer or staging-transport acceptance is claimed.
function fixture(overrides = {}) {
  const controller = createSessionController({ storage: null })
  const identity = { tenant: 'account-tenant', tier: 'hosted_pro', org: 'account-org' }
  const inputs = {
    active: true, MODE_DRAWING_ID: null, PROOF_MODE: false, PUBLIC_DEMO: false,
    drawingId: null, drawing: {}, isSignedIn: vi.fn(() => true),
    setSessionError: vi.fn(), setWorkspaceBootstrapRequired: vi.fn(),
    setError: vi.fn(), setPhase: vi.fn(), setSessionTier: vi.fn(), setSessionOrg: vi.fn(),
    setTenantId: vi.fn(), setGuestDrawing: vi.fn(),
    getSessionIdentity: vi.fn().mockResolvedValue(identity),
    getSession: vi.fn().mockRejectedValue(Object.assign(new Error('Drawing broker unavailable'), { status: 503 })),
    getDrawingVersions: vi.fn().mockResolvedValue({ head: 4 }),
    sessionDrawingState: vi.fn(() => ({ drawing_id: 'drawing-1', head: 4 })),
    seatIntake: vi.fn(), mockVersions: { seedBase: vi.fn() },
    accountSessionObservedRef: { current: false },
    platformSession: { actions: controller.actions },
    requireAuth: vi.fn(controller.actions.requireAuth),
    isWorkspaceBootstrapRequired, humanizeError,
    ...overrides,
  }
  const effect = Function(...Object.keys(inputs), `return (${effectSource})`)(...Object.values(inputs))
  return { inputs, controller, identity, run: effect }
}

describe('ToolCast empty workspace identity', () => {
  it('activates an empty signed-in workspace even when drawing intake is unavailable', async () => {
    const { inputs, controller, identity, run } = fixture()
    run()
    await waitFor(() => expect(controller.getSnapshot().status).toBe('active'))
    expect(controller.getSnapshot().session).toEqual(identity)
    expect(inputs.getSessionIdentity).toHaveBeenCalledExactlyOnceWith(false)
    expect(inputs.getSession).not.toHaveBeenCalled()
    expect(inputs.seatIntake).not.toHaveBeenCalled()
    expect(inputs.accountSessionObservedRef.current).toBe(true)
    expect(inputs.setSessionTier).toHaveBeenLastCalledWith(identity.tier)
    expect(inputs.setSessionOrg).toHaveBeenLastCalledWith(identity.org)
    expect(inputs.setPhase).toHaveBeenLastCalledWith('empty')
  })

  it('retains the authentication gate on an identity 401', async () => {
    const probe = vi.fn().mockRejectedValue({ status: 401 })
    const { inputs, controller, run } = fixture({ getSessionIdentity: probe })
    run()
    await waitFor(() => expect(controller.getSnapshot().status).toBe('required'))
    expect(inputs.requireAuth).toHaveBeenCalledWith('/api/session')
    expect(inputs.getSession).not.toHaveBeenCalled()
    expect(inputs.accountSessionObservedRef.current).toBe(false)
  })

  it('retains workspace bootstrap for the active-binding 403', async () => {
    const probe = vi.fn().mockRejectedValue({ status: 403,
      body: { detail: 'verified subject has no active platform identity binding' } })
    const { inputs, controller, run } = fixture({ getSessionIdentity: probe })
    run()
    await waitFor(() => expect(inputs.setWorkspaceBootstrapRequired).toHaveBeenLastCalledWith(true))
    expect(controller.getSnapshot().status).not.toBe('active')
    expect(inputs.requireAuth).not.toHaveBeenCalled()
    expect(inputs.getSession).not.toHaveBeenCalled()
  })

  it('does not treat another forbidden response as a workspace bootstrap grant', async () => {
    const { inputs, controller, run } = fixture({
      getSessionIdentity: vi.fn().mockRejectedValue({ status: 403, body: { detail: 'forbidden' } }),
    })
    run()
    await waitFor(() => expect(inputs.setPhase).toHaveBeenLastCalledWith('failed'))
    expect(inputs.setWorkspaceBootstrapRequired).not.toHaveBeenCalledWith(true)
    expect(controller.getSnapshot().status).not.toBe('active')
    expect(inputs.getSession).not.toHaveBeenCalled()
  })

  it('keeps a signed-out empty workspace gated without probing either endpoint', () => {
    const { inputs, controller, run } = fixture({ isSignedIn: () => false })
    run()
    expect(controller.getSnapshot().status).toBe('required')
    expect(inputs.getSessionIdentity).not.toHaveBeenCalled()
    expect(inputs.getSession).not.toHaveBeenCalled()
  })

  it('ignores an identity proof that resolves after this effect is cleaned up', async () => {
    let resolve
    const pending = new Promise(done => { resolve = done })
    const { inputs, controller, identity, run } = fixture({ getSessionIdentity: vi.fn(() => pending) })
    const stop = run()
    expect(inputs.getSessionIdentity).toHaveBeenCalledExactlyOnceWith(false)
    stop()
    resolve(identity)
    await pending
    expect(controller.getSnapshot().status).not.toBe('active')
    expect(inputs.accountSessionObservedRef.current).toBe(false)
    expect(inputs.setSessionTier).not.toHaveBeenCalled()
  })

  it('keeps the selected drawing on its actual intake and version path', async () => {
    const intake = { drawing: 'actual drawing' }
    const data = { intake, tenant: 'account-tenant', tier: 'hosted_pro', org: 'account-org' }
    const { inputs, controller, run } = fixture({ drawingId: 'drawing-1',
      getSession: vi.fn().mockResolvedValue(data) })
    run()
    await waitFor(() => expect(inputs.setPhase).toHaveBeenLastCalledWith('ready'))
    expect(inputs.getSession).toHaveBeenCalledExactlyOnceWith(false, 'drawing-1')
    expect(inputs.getSessionIdentity).not.toHaveBeenCalled()
    expect(inputs.getDrawingVersions).toHaveBeenCalledExactlyOnceWith(false, 'drawing-1')
    expect(inputs.seatIntake).toHaveBeenCalledWith(intake, {
      drawingId: 'drawing-1', drawingState: { drawing_id: 'drawing-1', head: 4 }, apply: true,
    })
    expect(controller.getSnapshot().status).toBe('active')
  })
})

// Execute the actual production createWorkspaceOrg callback body. Its state
// setters and the platform trust controller are supplied here; nothing of the
// callback is copied.
let createOrgNode = null
function findCreateOrg(node) {
  if (!node || typeof node.type !== 'string' || createOrgNode) return
  if (node.type === 'VariableDeclarator' && node.id?.name === 'createWorkspaceOrg'
    && node.init?.type === 'CallExpression' && node.init.callee.name === 'useCallback') {
    createOrgNode = node.init.arguments[0]
    return
  }
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(findCreateOrg)
    else if (value && typeof value.type === 'string') findCreateOrg(value)
  }
}
findCreateOrg(tree)
if (!createOrgNode) throw new Error('Expected the ToolCast createWorkspaceOrg callback')
const createOrgSource = source.slice(createOrgNode.start, createOrgNode.end)

function createOrgFixture(overrides = {}) {
  const networkBanner = humanizeError(new TypeError('Failed to fetch'))
  const state = { sessionError: networkBanner, error: networkBanner, workspaceBootstrapRequired: true, sessionRetry: 0 }
  const refreshAll = vi.fn()
  const inputs = {
    sessionReady: false,
    workspaceBootstrapRequired: true,
    workspace: { createOrg: vi.fn().mockResolvedValue({ org_id: 'org-1', name: 'Acme' }) },
    platform: { actions: { refreshAll } },
    setSessionError: vi.fn((value) => { state.sessionError = value }),
    setError: vi.fn((value) => { state.error = value }),
    setWorkspaceBootstrapRequired: vi.fn((value) => { state.workspaceBootstrapRequired = value }),
    setSessionRetry: vi.fn((update) => { state.sessionRetry = update(state.sessionRetry) }),
    ...overrides,
  }
  const create = Function(...Object.keys(inputs), `return (${createOrgSource})`)(...Object.values(inputs))
  return { inputs, state, refreshAll, networkBanner, create }
}

describe('ToolCast Create workspace refresh', () => {
  it('clears the stale service banners and re-reads platform trust after Create workspace', async () => {
    const { inputs, state, refreshAll, networkBanner, create } = createOrgFixture()
    expect(networkBanner).toBe("Couldn't reach the service. Check the connection and retry.")
    const before = refreshAll.mock.calls.length
    const org = await create('Acme')
    expect(org).toEqual({ org_id: 'org-1', name: 'Acme' })
    expect(inputs.workspace.createOrg).toHaveBeenCalledExactlyOnceWith('Acme')
    expect(refreshAll.mock.calls.length).toBe(before + 1)
    expect(state.sessionError).toBeNull()
    expect(state.error).toBeNull()
    expect(JSON.stringify(state)).not.toContain("Couldn't reach the service")
    expect(state.workspaceBootstrapRequired).toBe(false)
    expect(state.sessionRetry).toBe(1)
  })

  it('also refreshes trust when an already-bound session creates an org', async () => {
    const { state, refreshAll, create } = createOrgFixture({ sessionReady: true, workspaceBootstrapRequired: false })
    state.workspaceBootstrapRequired = false
    await create('Second org')
    expect(refreshAll).toHaveBeenCalledTimes(1)
    expect(state.sessionError).toBeNull()
    expect(state.error).toBeNull()
    expect(state.sessionRetry).toBe(0)
  })

  it('does not refresh trust or clear banners when createOrg fails', async () => {
    const { inputs, state, refreshAll, networkBanner, create } = createOrgFixture({
      workspace: { createOrg: vi.fn().mockResolvedValue(null) },
    })
    const org = await create('Acme')
    expect(org).toBeNull()
    expect(refreshAll).not.toHaveBeenCalled()
    expect(inputs.setSessionError).not.toHaveBeenCalled()
    expect(inputs.setError).not.toHaveBeenCalled()
    expect(state.sessionError).toBe(networkBanner)
    expect(state.workspaceBootstrapRequired).toBe(true)
    expect(state.sessionRetry).toBe(0)
  })
})
