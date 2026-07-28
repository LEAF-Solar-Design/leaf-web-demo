import {
  PLATFORM_TRUST_AUTH_SOURCE,
  classifyHealth,
  entitlementAllowed,
  isUnauthorized,
} from './platformTrustModel.js'

const plainError = (error) => String(error?.message || error || 'Something went wrong.')
const RESOURCE_NAMES = ['usage', 'entitlements', 'health', 'grant']

function initialState(mock) {
  return {
    mock: !!mock,
    usage: null,
    usageAt: 0,
    usageLoading: false,
    usageError: null,
    entitlements: null,
    entLoading: false,
    entError: null,
    health: null,
    healthLoading: false,
    healthError: null,
    grant: null,
    grantLoading: false,
    grantBusy: false,
    grantErr: null,
    authRequired: false,
    authSources: [],
  }
}

/**
 * Transport-neutral owner for tenant trust reads and Claude grant mutations.
 * The store does not start work itself. A host calls refreshAll after mounting.
 */
export function createPlatformTrustController({
  mock = false,
  services = {},
  formatError = plainError,
  now = () => Date.now(),
  onAuthRequired,
} = {}) {
  let state = initialState(mock)
  let destroyed = false
  const listeners = new Set()
  const generations = new Map(RESOURCE_NAMES.map((name) => [name, 0]))
  const authSources = new Set()

  const publish = (patch) => {
    if (destroyed) return
    state = { ...state, ...patch }
    for (const listener of listeners) listener()
  }

  const snapshotAuth = () => {
    const sources = [...authSources].sort()
    const required = sources.length > 0
    publish({ authRequired: required, authSources: sources })
    try { onAuthRequired?.(required, sources) } catch { /* observers cannot break state */ }
  }

  const reportAuthRequired = (required = true, source = 'external') => {
    if (required) authSources.add(source)
    else authSources.delete(source)
    snapshotAuth()
  }

  const recordOutcome = (resource, error = null) => {
    const source = `${PLATFORM_TRUST_AUTH_SOURCE}:${resource}`
    if (error && isUnauthorized(error)) authSources.add(source)
    else authSources.delete(source)
    snapshotAuth()
  }

  const begin = (resource) => {
    const generation = (generations.get(resource) || 0) + 1
    generations.set(resource, generation)
    return generation
  }

  const current = (resource, generation) => !destroyed && generations.get(resource) === generation

  const safeError = (error) => {
    try { return formatError(error) } catch { return plainError(error) }
  }

  const resetForMock = () => {
    for (const name of RESOURCE_NAMES) begin(name)
    authSources.clear()
    state = initialState(true)
    for (const listener of listeners) listener()
    try { onAuthRequired?.(false, []) } catch { /* observer only */ }
  }

  const setMock = (nextMock) => {
    const next = !!nextMock
    if (next === state.mock) return
    if (next) resetForMock()
    else publish({ mock: false })
  }

  const loadUsage = async () => {
    if (state.mock) {
      publish({ usage: null, usageAt: 0, usageLoading: false, usageError: null })
      return null
    }
    const generation = begin('usage')
    publish({ usageLoading: true, usageError: null })
    try {
      const value = await services.getUsage?.()
      if (!current('usage', generation)) return null
      recordOutcome('usage')
      const stamp = Number(now()) || 0
      publish({ usage: value ?? null, usageAt: stamp, usageLoading: false })
      return value ?? null
    } catch (error) {
      if (!current('usage', generation)) return null
      recordOutcome('usage', error)
      publish({ usage: null, usageLoading: false, usageError: safeError(error) })
      return null
    }
  }

  const loadEntitlements = async () => {
    if (state.mock) {
      publish({ entitlements: null, entLoading: false, entError: null })
      return null
    }
    const generation = begin('entitlements')
    publish({ entLoading: true, entError: null })
    try {
      const value = await services.getEntitlements?.()
      if (!current('entitlements', generation)) return null
      recordOutcome('entitlements')
      publish({ entitlements: value ?? null, entLoading: false })
      return value ?? null
    } catch (error) {
      if (!current('entitlements', generation)) return null
      recordOutcome('entitlements', error)
      publish({ entitlements: null, entLoading: false, entError: safeError(error) })
      return null
    }
  }

  const loadHealth = async () => {
    if (state.mock) {
      publish({ health: null, healthLoading: false, healthError: null })
      return null
    }
    const generation = begin('health')
    publish({ healthLoading: true, healthError: null })
    try {
      const value = await services.getHealth?.()
      if (!current('health', generation)) return null
      recordOutcome('health')
      publish({ health: value ?? null, healthLoading: false })
      return value ?? null
    } catch (error) {
      if (!current('health', generation)) return null
      recordOutcome('health', error)
      publish({ health: null, healthLoading: false, healthError: safeError(error) })
      return null
    }
  }

  const loadGrant = async () => {
    if (state.mock) {
      publish({ grant: null, grantLoading: false, grantBusy: false, grantErr: null })
      return null
    }
    // A read must not supersede a link or unlink mutation and strand its busy state.
    if (state.grantBusy) return state.grant
    const generation = begin('grant')
    publish({ grantLoading: true, grantErr: null })
    try {
      const value = await services.getClaudeGrant?.()
      if (!current('grant', generation)) return null
      recordOutcome('grant')
      publish({ grant: value ?? null, grantLoading: false })
      return value ?? null
    } catch (error) {
      if (!current('grant', generation)) return null
      recordOutcome('grant', error)
      publish({ grant: null, grantLoading: false, grantErr: safeError(error) })
      return null
    }
  }

  const linkClaude = async (token, kind, label, plan) => {
    if (state.mock) return null
    const generation = begin('grant')
    publish({ grantBusy: true, grantLoading: false, grantErr: null })
    try {
      const value = await services.linkClaudeGrant?.(token, kind, label, plan)
      if (!current('grant', generation)) return null
      recordOutcome('grant')
      const grant = value ?? {
        linked: true,
        linked_at: new Date(now()).toISOString(),
        kind: kind || null,
        accounts: [],
      }
      publish({ grant, grantBusy: false })
      return grant
    } catch (error) {
      if (!current('grant', generation)) return null
      recordOutcome('grant', error)
      publish({ grantBusy: false, grantErr: safeError(error) })
      return null
    }
  }

  const activateClaude = async (accountId) => {
    if (state.mock) return null
    const generation = begin('grant')
    publish({ grantBusy: true, grantLoading: false, grantErr: null })
    try {
      const value = await services.activateClaudeGrant?.(accountId)
      if (!current('grant', generation)) return null
      recordOutcome('grant')
      publish({ grant: value ?? state.grant, grantBusy: false })
      return value ?? state.grant
    } catch (error) {
      if (!current('grant', generation)) return null
      recordOutcome('grant', error)
      publish({ grantBusy: false, grantErr: safeError(error) })
      return null
    }
  }

  const unlinkClaude = async (accountId) => {
    if (state.mock) return null
    const generation = begin('grant')
    publish({ grantBusy: true, grantLoading: false, grantErr: null })
    try {
      const value = await services.unlinkClaudeGrant?.(accountId)
      if (!current('grant', generation)) return null
      recordOutcome('grant')
      const grant = value ?? { linked: false, linked_at: null, accounts: [] }
      publish({ grant, grantBusy: false })
      return grant
    } catch (error) {
      if (!current('grant', generation)) return null
      recordOutcome('grant', error)
      publish({ grantBusy: false, grantErr: safeError(error) })
      return null
    }
  }

  const refreshAll = () => {
    if (state.mock) return Promise.resolve([null, null, null, null])
    return Promise.all([loadUsage(), loadEntitlements(), loadHealth(), loadGrant()])
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    setMock,
    reportAuthRequired,
    loadUsage,
    loadEntitlements,
    loadHealth,
    loadGrant,
    linkClaude,
    activateClaude,
    unlinkClaude,
    refreshAll,
    isEntitled: (key, fallback = true) => entitlementAllowed(state.entitlements, key, fallback),
    getHealthStatus: () => classifyHealth(state.health),
    start() {
      destroyed = false
    },
    destroy() {
      destroyed = true
      for (const name of RESOURCE_NAMES) begin(name)
      listeners.clear()
    },
  }
}

export default createPlatformTrustController
