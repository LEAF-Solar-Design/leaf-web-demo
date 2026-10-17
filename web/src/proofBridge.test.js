// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import { installSurfaceConfigProofBridge, PROOF_BRIDGE_HOSTS, PROOF_BRIDGE_FLAG } from './proofBridge.js'

const staging = 'platform-staging.leafdesign.ai'
const makeClients = () => ({ getSession: vi.fn(), getSurfaceConfig: vi.fn(), submitSurfaceConfig: vi.fn() })

describe('surface-config proof bridge', () => {
  it('installs the frozen version 1 bridge on staging', () => {
    const target = {}
    expect(PROOF_BRIDGE_HOSTS).toEqual([staging])
    expect(PROOF_BRIDGE_FLAG).toBe('VITE_LEAF_PROOF_BRIDGE')
    expect(installSurfaceConfigProofBridge(target, { hostname: staging, clients: makeClients() })).toBe(true)
    expect(Object.keys(target.__leafSurfaceConfigProof)).toEqual([
      'version', 'getSession', 'getSurfaceConfig', 'submitSurfaceConfig',
    ])
    expect(target.__leafSurfaceConfigProof.version).toBe(1)
    expect(Object.isFrozen(target.__leafSurfaceConfigProof)).toBe(true)
    for (const name of ['getSession', 'getSurfaceConfig', 'submitSurfaceConfig']) {
      expect(typeof target.__leafSurfaceConfigProof[name]).toBe('function')
    }
  })

  it.each(['localhost', 'proof.example.test'])('allows the exact flag on %s', (hostname) => {
    expect(installSurfaceConfigProofBridge({}, {
      hostname, env: { VITE_LEAF_PROOF_BRIDGE: '1' }, clients: makeClients(),
    })).toBe(true)
  })

  it('refuses production and absent hostnames even with the flag', () => {
    for (const hostname of ['platform.leafdesign.ai', '', undefined, null]) {
      for (const flag of [undefined, '1', '0', 'true']) {
        const target = {}
        expect(installSurfaceConfigProofBridge(target, {
          hostname, env: { VITE_LEAF_PROOF_BRIDGE: flag }, clients: makeClients(),
        })).toBe(false)
        expect('__leafSurfaceConfigProof' in target).toBe(false)
      }
    }
  })

  it.each([undefined, '0', 'true', 1, true])('refuses other hosts with flag %s', (flag) => {
    const target = {}
    expect(installSurfaceConfigProofBridge(target, {
      hostname: 'localhost', env: { VITE_LEAF_PROOF_BRIDGE: flag }, clients: makeClients(),
    })).toBe(false)
    expect('__leafSurfaceConfigProof' in target).toBe(false)
  })

  it('forwards every argument and exact result, resolving clients at call time', () => {
    const target = {}, clients = makeClients()
    installSurfaceConfigProofBridge(target, { hostname: staging, clients })
    for (const name of Object.keys(clients)) {
      const args = [false, { fresh: true }, 'extra']
      const result = Promise.resolve({ client: name })
      clients[name].mockReturnValue(result)
      expect(target.__leafSurfaceConfigProof[name](...args)).toBe(result)
      expect(clients[name]).toHaveBeenCalledWith(...args)
    }
    const original = clients.getSession
    clients.getSession = vi.fn(() => 'new shell client')
    expect(target.__leafSurfaceConfigProof.getSession('new')).toBe('new shell client')
    expect(clients.getSession).toHaveBeenCalledWith('new')
    expect(original).toHaveBeenCalledTimes(1)
  })

  it('preserves a pre-existing frozen bridge', () => {
    const bridge = Object.freeze({ version: 1 })
    const target = { __leafSurfaceConfigProof: bridge }
    expect(installSurfaceConfigProofBridge(target, { hostname: staging, clients: makeClients() })).toBe(false)
    expect(target.__leafSurfaceConfigProof).toBe(bridge)
  })

  it('keeps the installed bridge and its clients when installation is repeated', () => {
    const target = {}, clients = makeClients()
    expect(installSurfaceConfigProofBridge(target, { hostname: staging, clients })).toBe(true)
    const bridge = target.__leafSurfaceConfigProof
    const replacementClients = makeClients()
    expect(installSurfaceConfigProofBridge(target, {
      hostname: staging, clients: replacementClients,
    })).toBe(false)
    expect(target.__leafSurfaceConfigProof).toBe(bridge)
    clients.getSession = vi.fn(() => 'current client')
    expect(bridge.getSession()).toBe('current client')
    expect(replacementClients.getSession).not.toHaveBeenCalled()
  })

  it('does not require target.location and never throws on installation failures', () => {
    expect(installSurfaceConfigProofBridge({}, { hostname: staging, clients: makeClients() })).toBe(true)
    const throwing = new Proxy({}, { has() { throw new Error('unavailable') } })
    for (const target of [null, undefined, Object.freeze({}), throwing]) {
      expect(installSurfaceConfigProofBridge(target, { hostname: staging, clients: makeClients() })).toBe(false)
    }
    expect(installSurfaceConfigProofBridge({})).toBe(false)
    expect(installSurfaceConfigProofBridge({}, { hostname: staging })).toBe(false)
  })
})
