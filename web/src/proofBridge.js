export const PROOF_BRIDGE_HOSTS = Object.freeze(['platform-staging.leafdesign.ai'])
export const PROOF_BRIDGE_FLAG = 'VITE_LEAF_PROOF_BRIDGE'

export function installSurfaceConfigProofBridge(target, options) {
  try {
    const { hostname, env, clients } = options || {}
    if (typeof hostname !== 'string' || !hostname || hostname === 'platform.leafdesign.ai') return false
    if (!PROOF_BRIDGE_HOSTS.includes(hostname) && env?.[PROOF_BRIDGE_FLAG] !== '1') return false
    if (!target || (typeof target !== 'object' && typeof target !== 'function')) return false
    if ('__leafSurfaceConfigProof' in target && Object.isFrozen(target.__leafSurfaceConfigProof)) return false
    if (!clients || !['getSession', 'getSurfaceConfig', 'submitSurfaceConfig'].every(
      (name) => typeof clients[name] === 'function',
    )) return false

    Object.defineProperty(target, '__leafSurfaceConfigProof', {
      value: Object.freeze({
        version: 1,
        getSession: (...args) => clients.getSession(...args),
        getSurfaceConfig: (...args) => clients.getSurfaceConfig(...args),
        submitSurfaceConfig: (...args) => clients.submitSurfaceConfig(...args),
      }),
      configurable: true,
    })
    return true
  } catch {
    return false
  }
}
